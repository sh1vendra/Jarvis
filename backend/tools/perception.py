"""Perception helpers: reading what's actually on screen.

Two ways to "see" the UI:
1. The macOS Accessibility API (AX) - the same API screen readers use. It
   lets us ask a running app "what elements do you have and where are they"
   without a screenshot. This only works well for apps that actually expose
   a real accessibility tree (most native Cocoa apps do; Electron/Chromium
   apps like Spotify often expose almost nothing).
2. A raw screenshot, for the vision fallback when AX comes up empty.
"""

import os
import subprocess
import tempfile
import time

try:
    import ApplicationServices as AS
    import Quartz
except ImportError:
    # Not macOS. The whole agent -> tools import chain is pulled in by the
    # Cloud Run deployment of the agent server (backend/servers/agent_server.py),
    # which only runs the Gemini-facing pipeline - it never has a screen and
    # never calls anything in this module. A clean import is all that's needed
    # there; every function below that touches AS/Quartz raises a clear error
    # if it is somehow reached off a Mac.
    AS = None
    Quartz = None


def _require_accessibility_api() -> None:
    if AS is None:
        raise RuntimeError(
            "macOS Accessibility API unavailable - perception only works on a local Mac, "
            "not in the Cloud Run deployment."
        )

# Roles worth surfacing to the caller - purely structural containers
# (AXGroup, AXScrollArea, AXUnknown, ...) are skipped since they're never
# themselves something you'd click or type into.
_INTERESTING_ROLES = {
    "AXButton",
    "AXTextField",
    "AXSearchField",
    "AXStaticText",
    "AXLink",
    "AXMenuItem",
    "AXCell",
    "AXRadioButton",
    "AXCheckBox",
    "AXPopUpButton",
}

# app_name -> (timestamp, tree) so repeated calls within a couple seconds
# don't re-walk the whole AX tree again.
_ui_tree_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_SECONDS = 2.0


def _pid_for_app(app_name: str) -> int | None:
    """Returns app_name's process ID via a fresh System Events query.

    Not NSWorkspace.sharedWorkspace().runningApplications() - that was
    measured to go stale under the same subprocess-heavy conditions that
    caused mac_control._frontmost_app_name's staleness bug (see that
    function's docstring for the original diagnosis). Confirmed directly
    here too: after quitting and relaunching an app a few times in one
    long-running process, this kept returning the *previous* instance's
    already-dead PID instead of the new one, which meant every AX query
    built on top of it (get_ui_tree, get_field_values,
    get_frontmost_window_frame) was silently querying a process that no
    longer existed. A fresh osascript query each call has no such cache.
    """
    result = subprocess.run(
        [
            "osascript",
            "-e",
            f'tell application "System Events" to get unix id of first process whose name is "{app_name}"',
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _ax_attr(element, attribute):
    """Thin wrapper around AXUIElementCopyAttributeValue: returns the
    attribute's value, or None if the element doesn't have it / errors."""
    err, value = AS.AXUIElementCopyAttributeValue(element, attribute, None)
    if err != 0:
        return None
    return value


def _ax_point(element):
    value = _ax_attr(element, "AXPosition")
    if value is None:
        return None
    # AXPosition comes back as an opaque AXValue wrapping a CGPoint - pyobjc
    # requires unpacking it explicitly via AXValueGetValue.
    ok, point = AS.AXValueGetValue(value, AS.kAXValueCGPointType, None)
    return (point.x, point.y) if ok else None


def _ax_size(element):
    value = _ax_attr(element, "AXSize")
    if value is None:
        return None
    ok, size = AS.AXValueGetValue(value, AS.kAXValueCGSizeType, None)
    return (size.width, size.height) if ok else None


def _walk(element, elements: list, depth: int, max_depth: int, max_elements: int) -> None:
    if depth > max_depth or len(elements) >= max_elements:
        return

    role = _ax_attr(element, "AXRole")
    if role in _INTERESTING_ROLES:
        # Prefer AXDescription since most macOS apps put the human-readable
        # label there (AXTitle is frequently empty on buttons/cells);
        # fall back to AXTitle, then AXValue (useful for text fields).
        label = _ax_attr(element, "AXDescription") or _ax_attr(element, "AXTitle") or _ax_attr(element, "AXValue")
        position = _ax_point(element)
        size = _ax_size(element)
        if label and position and size:
            elements.append(
                {
                    "role": role,
                    "label": str(label),
                    "x": position[0],
                    "y": position[1],
                    "width": size[0],
                    "height": size[1],
                }
            )

    for child in _ax_attr(element, "AXChildren") or []:
        _walk(child, elements, depth + 1, max_depth, max_elements)


def get_ui_tree(app_name: str, use_cache: bool = True) -> dict:
    """Returns the visible interactive elements of `app_name`'s frontmost
    window via the Accessibility API.

    Returns a dict: {"found_app": bool, "elements": [{role, label, x, y,
    width, height}, ...]}. An app that exposes no real accessibility tree
    (common for Electron/Chromium apps) will simply come back with very few
    or zero elements - that's a real signal, not a bug in this function.
    """
    _require_accessibility_api()
    if use_cache:
        cached = _ui_tree_cache.get(app_name)
        if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]

    pid = _pid_for_app(app_name)
    if pid is None:
        result = {"found_app": False, "elements": []}
        _ui_tree_cache[app_name] = (time.monotonic(), result)
        return result

    app_ref = AS.AXUIElementCreateApplication(pid)
    windows = _ax_attr(app_ref, "AXWindows") or []

    elements: list = []
    for window in windows:
        _walk(window, elements, depth=0, max_depth=20, max_elements=500)

    result = {"found_app": True, "elements": elements}
    _ui_tree_cache[app_name] = (time.monotonic(), result)
    return result


def get_field_values(app_name: str) -> list[str]:
    """Fresh (always uncached) query for the actual AXValue of every
    text-like field in app_name's frontmost window.

    This exists specifically to verify typed text actually landed somewhere.
    get_ui_tree's `label` deliberately prefers AXDescription/AXTitle over
    AXValue (those are what make a UI-labeling pass useful - "Search",
    "Add Reminder", etc.), so it's the wrong thing to read here: after
    typing, we need the field's actual current *contents*, not its label.
    """
    _require_accessibility_api()
    pid = _pid_for_app(app_name)
    if pid is None:
        return []

    app_ref = AS.AXUIElementCreateApplication(pid)
    windows = _ax_attr(app_ref, "AXWindows") or []

    values: list[str] = []

    def walk(element, depth=0, max_depth=20, count=[0]):
        if depth > max_depth or count[0] > 500:
            return
        role = _ax_attr(element, "AXRole")
        if role in ("AXTextField", "AXSearchField"):
            value = _ax_attr(element, "AXValue")
            if value:
                values.append(str(value))
        count[0] += 1
        for child in _ax_attr(element, "AXChildren") or []:
            walk(child, depth + 1, max_depth, count)

    for window in windows:
        walk(window)
    return values


def get_frontmost_window_frame(app_name: str) -> tuple[float, float, float, float] | None:
    """Returns (x, y, width, height) in point-space of app_name's frontmost
    window via the Accessibility API, or None if the app/window can't be
    found.

    Unlike get_ui_tree's interior elements, a window's own frame is
    OS-reported chrome info rather than app-internal content, so even apps
    that expose almost nothing else via AX (Electron/Chromium apps like
    Spotify) still expose this reliably. Not yet used by any caller - added
    as a building block for locating UI that appears in a predictable spot
    relative to the window (e.g. a search bar that opens via keyboard
    shortcut), since asking vision to re-locate that kind of target was
    measured to guess wildly inconsistent, sometimes off-window locations.
    """
    _require_accessibility_api()
    pid = _pid_for_app(app_name)
    if pid is None:
        return None

    app_ref = AS.AXUIElementCreateApplication(pid)
    windows = _ax_attr(app_ref, "AXWindows") or []
    if not windows:
        return None

    position = _ax_point(windows[0])
    size = _ax_size(windows[0])
    if position is None or size is None:
        return None
    return (position[0], position[1], size[0], size[1])


def capture_region_unverified(x: float, y: float, width: float, height: float, *, reason: str) -> bytes:
    """Captures a rectangular region of the screen by raw point-space
    coordinates - (x, y) is the region's center - with NO verification that
    those coordinates correspond to anything real or intended. The caller
    is fully responsible for that. Returns PNG bytes.

    This is the exact function whose direct use - called from an ad hoc
    script with coordinates computed from a stale position check, instead
    of going through capture_screenshot's ground-truth window lookup -
    caused a real privacy incident: it captured a different app's window
    than the one intended. See planning.md's "capture_region_unverified"
    entry for the full incident and the reasoning behind this function's
    shape.

    `reason` is mandatory, keyword-only, and validated at call time - not a
    convention someone can miss under pressure, a structural one: there is
    no way to call this function positionally or silently the way the old
    unqualified `capture_region(x, y, w, h)` could be, from either inside
    or outside this module. Every call site must say, in its own words,
    why skipping verification is actually safe here - `capture_screenshot`
    below is the only caller that should ever need this, plus a small,
    already-safe class of uses in `mac_control.py` (before/after diff-check
    regions centered on a point that same call just resolved via AX/vision,
    inside an app already confirmed frontmost - not independently guessed
    coordinates). If what's actually wanted is "a screenshot of app X,"
    that's `capture_screenshot(app_name=X)`, not this.

    `screencapture -R<x>,<y>,<w>,<h>` takes its rect in point-space (the same
    coordinate system as clicks and AX positions) and returns the image at
    the display's native pixel resolution - confirmed directly: requesting
    a 300x200 point region on this Retina display came back as a 600x400
    pixel PNG. So callers pass plain click-coordinate units, no manual
    Retina scale conversion needed here (unlike capture_screenshot's
    whole-screen output, which vision-tier coordinate math does have to
    divide by the scale factor).
    """
    if not reason or not reason.strip():
        raise ValueError(
            "capture_region_unverified() requires a real, non-empty `reason` explaining why "
            "skipping capture_screenshot's on-screen-window verification is actually safe here "
            "- this is not optional, and there is no default to fall back on."
        )

    left = x - width / 2
    top = y - height / 2

    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        result = subprocess.run(
            ["screencapture", "-x", "-R", f"{left},{top},{width},{height}", path],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(f"screencapture -R failed: {result.stderr.decode(errors='replace')}")
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.remove(path)


def _real_window_bounds(app_name: str) -> tuple[float, float, float, float] | None:
    """Ground-truth on-screen window bounds for app_name, via the window
    server (CGWindowListCopyWindowInfo) rather than the Accessibility API.

    This distinction is real, not redundant with get_frontmost_window_frame:
    AX reports what an app *claims* about its window (and can be empty or
    wrong - see get_ui_tree's docstring), while this asks the window server
    what's actually composited on screen right now. It's also how a real
    privacy bug got caught during testing: "frontmost app" (keyboard focus,
    what AX/System Events report) and "app with a visible on-screen window"
    can diverge - a relaunched app can hold keyboard focus with literally no
    window open. Trusting the former for scoping a screenshot in that state
    captured whatever window actually *was* on screen instead (in that
    incident: the IDE and this conversation). Returns None - not a guess,
    not a stale fallback - when app_name has no real on-screen window.

    Windows come back front-to-back ordered (Apple's documented behavior for
    kCGWindowListOptionOnScreenOnly), so the first bounds matching app_name's
    owner name is that app's frontmost on-screen window.
    """
    if Quartz is None:
        return None
    window_list = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    for window in window_list:
        if window.get("kCGWindowOwnerName") != app_name:
            continue
        bounds = window.get("kCGWindowBounds")
        if not bounds:
            continue
        width, height = bounds.get("Width", 0), bounds.get("Height", 0)
        if width <= 0 or height <= 0:
            continue
        return (bounds["X"], bounds["Y"], width, height)
    return None


def capture_screenshot(app_name: str | None = None, *, allow_full_display: bool = False) -> bytes:
    """Captures a screenshot, scoped by default to app_name's real, verified
    on-screen window - never the whole display unless allow_full_display is
    explicitly passed, with a genuine reason to need it.

    This scoping is the fix for a real, demonstrated privacy bug, not a
    theoretical hardening: an earlier full-display capture, taken while
    Spotify (the intended target) happened to have no on-screen window,
    caught whatever *was* actually in front instead - in that case, the IDE
    and this very conversation, including real project IDs and chat text.
    "Frontmost app" (what open_app/type_in_field's guards check) and "app
    with a visible on-screen window" are not the same fact, and only the
    latter is safe to screenshot - see _real_window_bounds. So: given
    app_name, this only ever captures that app's own verified window region
    (via capture_region_unverified, which is where the actual pixels come
    from - the "unverified" in its name refers to *its own* inputs, not to
    the coordinates handed to it here, which _real_window_bounds just
    verified a moment ago); if that app has no real on-screen window right
    now, it refuses rather than silently falling back to a full-display
    capture that could show something unrelated. A full-display capture is
    still available, but only as an explicit, named opt-in.
    """
    if app_name is not None:
        bounds = _real_window_bounds(app_name)
        if bounds is not None:
            x, y, width, height = bounds
            return capture_region_unverified(
                x + width / 2,
                y + height / 2,
                width,
                height,
                reason=f"{app_name!r}'s real on-screen window bounds, just verified via _real_window_bounds",
            )
        if not allow_full_display:
            raise RuntimeError(
                f"'{app_name}' has no verified on-screen window right now - refusing a "
                "full-display capture (it could show unrelated windows/content instead). "
                "Pass allow_full_display=True only with a real reason to need the whole screen."
            )
    elif not allow_full_display:
        raise RuntimeError(
            "capture_screenshot() needs either app_name (preferred - scopes the capture to "
            "that app's own verified window) or allow_full_display=True with a genuine reason. "
            "This guard exists because an unscoped capture once caught unrelated, sensitive "
            "on-screen content instead of the intended app - see planning.md."
        )

    # `-x` suppresses the shutter sound, which matters here since this can
    # run many times per session with no user watching for it.
    #
    # screencapture writes its output by replacing the destination path
    # (rename-over-target), not by writing into an already-open file
    # descriptor - so we create the temp path, close our handle to it
    # *before* calling screencapture, then reopen by path afterward to read
    # the bytes it actually wrote. Keeping the original fd open and reading
    # from it would silently return 0 bytes (a stale fd pointing at the old,
    # empty, now-unlinked inode).
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        result = subprocess.run(["screencapture", "-x", path], capture_output=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError(f"screencapture failed: {result.stderr.decode(errors='replace')}")
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.remove(path)
