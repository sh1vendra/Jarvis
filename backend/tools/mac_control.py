"""Tools that actually control the Mac. First one: creating a real reminder
in the macOS Reminders app via AppleScript.

AppleScript is macOS's native scripting language for talking to apps that
expose a "scripting dictionary" (Reminders, Calendar, Finder, etc.). We don't
call any private API - we run a small AppleScript program through the
`osascript` CLI, which is the standard way to drive AppleScript from another
process. macOS treats this as "an app (Terminal/Python) is asking to control
Reminders," which is why the first run triggers a permission dialog under
System Settings -> Privacy & Security -> Automation.
"""

import difflib
import io
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime

import dateparser
from google import genai
from google.adk.tools import FunctionTool
from google.genai import types as genai_types
from PIL import Image, ImageChops

from .perception import (
    capture_region_unverified,
    capture_screenshot,
    get_field_values,
    get_frontmost_window_frame,
    get_ui_tree,
)

try:
    import Quartz
    from AppKit import NSScreen
except ImportError:
    # Not macOS. The Cloud Run deployment of the agent server pulls in the
    # full agent -> tools import chain, and there is no pyobjc there and no
    # screen to drive. Every function in this module that actually posts a
    # CGEvent or reads NSScreen is Mac-only and raises a clear error via
    # _require_macos() if reached off a Mac - but the import itself must
    # stay clean so the agent pipeline can load.
    Quartz = None
    NSScreen = None

_MACOS = Quartz is not None

logger = logging.getLogger(__name__)


def _require_macos() -> None:
    if not _MACOS:
        raise RuntimeError(
            "macOS control APIs unavailable - open_app / click_ui / type_in_field only work on a "
            "local Mac, not in the Cloud Run deployment (which runs the Gemini agent pipeline only)."
        )


def _parse_datetime(date_str: str, time_str: str) -> tuple[datetime | None, str | None]:
    """Parses a natural-language or ISO date+time pair via dateparser - the
    one shared date/time understanding every tool that needs a real datetime
    from user-facing text reuses (create_reminder here, create_calendar_event
    in native_apps.py), so phrasing is understood identically everywhere
    instead of being reimplemented per tool. dateparser understands both
    natural language ("tomorrow", "5pm") and ISO-style input, so callers
    don't need fragile string matching for every phrasing the Planner/Action
    agent might produce.

    Returns (parsed_datetime, None) on success, or (None, error_message) if
    dateparser couldn't make sense of the input.
    """
    parsed = dateparser.parse(f"{date_str} {time_str}")
    if parsed is None:
        return None, f"Could not understand date/time: {date_str!r} {time_str!r}"
    return parsed, None


def _applescript_date_lines(var_name: str, dt: datetime) -> str:
    """Emits the AppleScript lines that build a `date` object representing
    dt into var_name - the standard idiom every tool needing an exact
    AppleScript date/time shares (create_reminder here, create_calendar_event
    in native_apps.py): AppleScript has no native way to construct an
    arbitrary date from numbers in one call, so the idiom is to grab
    `(current date)` (today, right now) and then overwrite its `year`/
    `month`/`day`/`hours`/`minutes`/`seconds` properties one at a time until
    it represents the date/time actually wanted. Seconds are always zeroed -
    nothing in this codebase works at sub-minute precision.
    """
    return (
        f"set {var_name} to (current date)\n"
        f"    set year of {var_name} to {dt.year}\n"
        f"    set month of {var_name} to {dt.month}\n"
        f"    set day of {var_name} to {dt.day}\n"
        f"    set hours of {var_name} to {dt.hour}\n"
        f"    set minutes of {var_name} to {dt.minute}\n"
        f"    set seconds of {var_name} to 0\n"
    )


def _build_applescript(task: str, due_date: datetime, list_name: str) -> str:
    """Builds the AppleScript source that creates one reminder in a specific
    named list, creating that list first if it doesn't already exist.

    Notes on the syntax, since AppleScript looks nothing like Python:
    - `tell application "Reminders" ... end tell` scopes the following
      commands to the Reminders app, like a `with` block targeting a specific
      app's scripting dictionary.
    - `if not (exists list "X") then make new list with properties {...}`
      is AppleScript's existence check - there's no get-or-create helper, so
      we ask "does this named object exist" before creating it, same idea as
      `if not os.path.exists(...): os.makedirs(...)`.
    - `list "X"` refers to a specific named list by name, as opposed to
      `default list` (whichever list is the user's default, usually
      "Reminders") - we target a specific list explicitly so automated/test
      reminders land somewhere separate from the user's real lists.
    - `make new reminder with properties {...}` is AppleScript's constructor
      pattern: create a new object of a given class with an initial property
      record, similar to calling `Reminder(name=..., due_date=...)`.
    - String values are escaped by doubling any embedded quotes, since
      AppleScript string literals use double quotes with no backslash
      escaping.
    """
    safe_task = task.replace('"', '""')
    safe_list_name = list_name.replace('"', '""')

    return f'''
tell application "Reminders"
    if not (exists list "{safe_list_name}") then
        make new list with properties {{name:"{safe_list_name}"}}
    end if
    {_applescript_date_lines("dueDate", due_date)}
    tell list "{safe_list_name}"
        make new reminder with properties {{name:"{safe_task}", due date:dueDate}}
    end tell
end tell
return "ok"
'''


def _verify_reminder_exists(task: str, due_date: datetime, list_name: str) -> bool:
    """Real read-back: queries Reminders' own live object model for a
    reminder matching both name and due date, via a fresh `osascript`
    process - the same "ask again independently" pattern
    _frontmost_app_name() already uses elsewhere in this module - rather
    than trusting the creation script's own successful exit code as proof
    the reminder actually persisted.
    """
    safe_task = task.replace('"', '""')
    safe_list_name = list_name.replace('"', '""')
    script = f'''
tell application "Reminders"
    {_applescript_date_lines("dueDate", due_date)}
    tell list "{safe_list_name}"
        return count of (every reminder whose name is "{safe_task}" and due date is dueDate)
    end tell
end tell
'''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip()) >= 1
    except ValueError:
        return False


def create_reminder(task: str, due_date: str, due_time: str, list_name: str = "Jarvis Test") -> dict:
    """Creates a real reminder in the macOS Reminders app.

    Args:
        task: The reminder text, e.g. "Call mom".
        due_date: A natural-language or ISO date, e.g. "tomorrow", "2026-08-13".
        due_time: A natural-language or clock time, e.g. "5pm", "17:00".
        list_name: Which Reminders list to create it in. Defaults to
            "Jarvis Test" (created automatically if it doesn't exist yet) so
            automated/test reminders stay out of the user's real lists.

    Returns:
        A dict with:
            success: bool, True only if the reminder was independently
                confirmed to exist by querying Reminders back afterward -
                not just that the creation command didn't error
            message: human-readable summary of what happened
            error: the raw error string if something went wrong, else None
    """
    _require_macos()
    parsed, err = _parse_datetime(due_date, due_time)
    if parsed is None:
        return {"success": False, "message": err, "error": "date_parse_failed"}

    script = _build_applescript(task, parsed, list_name)

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "message": "osascript timed out - Reminders may be waiting on a permission dialog.",
            "error": str(exc),
        }

    if result.returncode != 0:
        stderr = result.stderr.strip()
        # macOS surfaces permission denials as AppleScript errors like
        # "Not authorized to send Apple events to Reminders" (error -1743).
        if "-1743" in stderr or "not authorized" in stderr.lower():
            return {
                "success": False,
                "message": (
                    "macOS denied automation access to Reminders. Grant it under "
                    "System Settings -> Privacy & Security -> Automation, then "
                    "allow this process to control Reminders, and try again."
                ),
                "error": stderr,
            }
        return {
            "success": False,
            "message": "osascript failed while creating the reminder.",
            "error": stderr,
        }

    # Don't report success just because the creation script's own exit code
    # was clean - independently query Reminders back to confirm the
    # reminder actually persisted, the same standard every tool in this
    # module is held to elsewhere (click_ui, type_in_field, open_app).
    if not _verify_reminder_exists(task, parsed, list_name):
        return {
            "success": False,
            "message": (
                f"osascript reported no error creating '{task}' but it could not be found on "
                f"read-back in list '{list_name}' - treating this as not actually created."
            ),
            "error": "verify_failed",
        }

    return {
        "success": True,
        "message": (
            f"Created reminder '{task}' due {parsed.strftime('%Y-%m-%d %H:%M')} in list "
            f"'{list_name}', confirmed by reading it back."
        ),
        "error": None,
    }


# Wrapping create_reminder as an ADK FunctionTool exposes it to an LlmAgent.
# ADK reads the function's signature, type hints, and docstring to build the
# tool's schema/description that gets sent to Gemini, so the docstring above
# doubles as the tool description Gemini sees.
create_reminder_tool = FunctionTool(create_reminder)


def _frontmost_app_name() -> str | None:
    """Returns the name of the currently frontmost app via a fresh System
    Events query.

    This used to go through AppKit's NSWorkspace.frontmostApplication()
    in-process, which turned out to be the actual root cause of a long,
    confusing chase: in a script that fires many subprocess calls back to
    back (exactly what open_app's poll loop and a real ADK agent run both
    do), that in-process cached value can go stale and then never update
    again for the rest of the process's lifetime - confirmed directly by
    polling it for 15+ seconds while the real frontmost app (verified via
    an independent System Events query at the same moments) had already
    changed. It looked exactly like "the other app keeps stealing focus
    back," but the other app had actually quit already; we were just
    reading a frozen value. Querying via a brand new `osascript` process
    each time has no such cache to go stale - each call is a fresh,
    real answer.
    """
    _require_macos()
    result = subprocess.run(
        [
            "osascript",
            "-e",
            'tell application "System Events" to get name of first application process whose frontmost is true',
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def open_app(app_name: str) -> dict:
    """Launches a macOS application and verifies it actually became the
    frontmost (foreground) app - not just that the launch command succeeded.

    Args:
        app_name: The application's name as it appears in /Applications,
            e.g. "Spotify", "Safari" (with or without the ".app" suffix).

    Returns:
        A dict with:
            success: bool, True only if the app was confirmed frontmost
            message: human-readable summary of what happened
            error: the raw error string if something went wrong, else None
    """
    _require_macos()
    clean_name = app_name.removesuffix(".app")

    launch = subprocess.run(
        ["open", "-a", clean_name],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if launch.returncode != 0:
        stderr = launch.stderr.strip()
        return {
            "success": False,
            "message": f"Could not launch '{clean_name}' - the 'open' command itself failed.",
            "error": stderr or f"open exited with code {launch.returncode}",
        }

    # `open -a` reliably starts the process, but in practice doesn't always
    # steal focus - we saw it launch Spotify successfully while some other
    # app (e.g. the terminal/IDE we're running from) stayed frontmost. An
    # explicit AppleScript `activate` is what actually forces the foreground
    # switch. But a single activate call can race the app's own startup (if
    # it fires before the app's process is far enough along to accept
    # activation, it's silently a no-op) - measured directly, that race
    # made a single upfront activate flaky: instant on some runs, timing out
    # on others with no code change at all. So instead of one activate call
    # before polling, we resend it on every poll iteration until the app is
    # confirmed frontmost or we give up.
    def _activate():
        subprocess.run(
            ["osascript", "-e", f'tell application "{clean_name}" to activate'],
            capture_output=True,
            text=True,
            timeout=15,
        )

    _activate()

    # Neither `open`'s nor `activate`'s exit code guarantees the app is
    # actually in the foreground yet - launch/activation is asynchronous.
    # So we poll the real frontmost-app state instead of trusting either
    # exit code: check every 150ms for up to ~8 seconds. Measured directly:
    # a cold launch of a heavy Electron app (Spotify, with its GPU/renderer/
    # helper processes) is highly variable and can take anywhere from
    # ~0.4s to several seconds depending on system load and disk cache
    # state - a native app like Reminders is comfortably ready well within
    # 1-2s, so this budget mostly exists for Electron-style apps.
    deadline = time.monotonic() + 8.0
    last_seen = None
    while time.monotonic() < deadline:
        last_seen = _frontmost_app_name()
        if last_seen and last_seen.lower() == clean_name.lower():
            return {
                "success": True,
                "message": f"'{clean_name}' is running and in the foreground.",
                "error": None,
            }
        time.sleep(0.15)
        _activate()

    return {
        "success": False,
        "message": (
            f"'{clean_name}' was launched but never became the frontmost app "
            f"within 8 seconds (frontmost app is currently '{last_seen}')."
        ),
        "error": "frontmost_verification_timeout",
    }


open_app_tool = FunctionTool(open_app)


# --- click_ui / type_in_field: two-tier element location -------------------
#
# Tier 1 (Accessibility API): ask the frontmost app's AX tree for an element
# whose label fuzzy-matches what we're looking for, and click its actual
# on-screen position. Fast, exact, no LLM call - but only works for apps
# that expose a real accessibility tree. Electron/Chromium apps often don't.
#
# Tier 2 (vision fallback): only reached if tier 1 finds nothing above a
# similarity threshold. Take a screenshot, ask Gemini's vision model for
# approximate pixel coordinates, convert those to point-space (screenshots
# are captured at the display's native pixel resolution, which on Retina
# displays is 2x the point coordinates every other macOS API uses), and
# click there. Approximate and slower, but works regardless of how the app
# exposes (or doesn't expose) accessibility info.

_FUZZY_MATCH_THRESHOLD = 0.45


def _best_ax_match(app_name: str, target_description: str, roles: set[str] | None = None):
    """Searches the current app's AX tree for the element whose label best
    matches target_description. Returns (element_dict, score) or (None, 0)
    if the tree is empty or nothing clears the similarity threshold."""
    tree = get_ui_tree(app_name)
    if not tree["found_app"] or not tree["elements"]:
        return None, 0.0

    best_element, best_score = None, 0.0
    target_lower = target_description.lower()
    for element in tree["elements"]:
        if roles and element["role"] not in roles:
            continue
        score = difflib.SequenceMatcher(None, target_lower, element["label"].lower()).ratio()
        if score > best_score:
            best_element, best_score = element, score

    if best_element and best_score >= _FUZZY_MATCH_THRESHOLD:
        return best_element, best_score
    return None, best_score


def _screen_scale_factor() -> float:
    """Screenshots come back at native pixel resolution; every click/AX
    coordinate is in point-space. On a Retina display those differ by this
    factor (2.0 is typical), so vision-tier coordinates must be divided by
    it before we dispatch a click."""
    _require_macos()
    return NSScreen.mainScreen().backingScaleFactor()


def _ask_vision_for_coordinates_in_image(image_bytes: bytes, target_description: str) -> tuple[float, float] | None:
    """Asks Gemini vision for the pixel coordinates of target_description
    within image_bytes, relative to that image's own top-left origin.

    This is the shared primitive behind both a single whole-screen guess
    and every zoomed-in crop guess in _locate_via_vision_zoom below - vision
    naturally reports coordinates relative to whatever image it's actually
    shown, so a cropped image needs no different prompt, just a smaller
    image.
    """
    api_key = os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-flash-lite-latest",
        contents=[
            genai_types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            (
                f"Find the UI element best described as: {target_description!r}.\n"
                "Reply with ONLY raw JSON (no markdown fences) of the form "
                '{"x": <int>, "y": <int>} giving the pixel coordinates of its center.'
            ),
        ],
    )

    text = response.text.strip()
    # Gemini sometimes wraps JSON in ```json ... ``` fences despite being told
    # not to - strip those defensively rather than assuming a clean reply.
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()

    try:
        coords = json.loads(text)
        return float(coords["x"]), float(coords["y"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


_ZOOM_CROP_SIZE = (600.0, 400.0)  # pixel width/height of the first zoomed-in crop
_ZOOM_SHRINK_FACTOR = 2.0  # each subsequent zoom level crops half as wide/tall as the last
_ZOOM_TIGHT_THRESHOLD = 180.0  # stop zooming once a crop would be this small (pixels) or smaller
_MAX_ZOOM_ITERATIONS = 2  # caps total vision calls at 3 (1 whole-screen + up to 2 crops)


def _locate_via_vision_zoom(
    target_description: str, app_name: str, max_iterations: int = _MAX_ZOOM_ITERATIONS
) -> tuple[float, float] | None:
    """Iteratively zooms in on a screenshot to get a more precise coordinate
    guess for small UI elements than a single whole-screen vision call can
    reliably give - measured directly, a whole-screen guess for Spotify's
    collapsed search icon landed ~100pt off the real element.

    Each iteration: crop tightly around the current best guess and ask
    vision again on the smaller image - the same element occupies a much
    larger fraction of what vision sees there, which should make each
    successive guess more precise. Every crop is taken from the *original*
    full screenshot (not crop-of-crop) so each iteration's answer only ever
    needs one offset translation back to full-screenshot pixel space,
    rather than compounding rounding error across nested crops. Capped at
    max_iterations zoom levels (on top of the initial whole-screen guess) so
    this can't spiral into unbounded latency, and stops early once the crop
    would already be tighter than _ZOOM_TIGHT_THRESHOLD - a crop that small
    has nothing meaningful left to zoom into.

    Returns final coordinates in point-space (already divided by the Retina
    scale factor, ready for _dispatch_click), or None if even the initial
    whole-screen guess fails (including if app_name has no verified
    on-screen window to scope the capture to - see capture_screenshot).
    """
    try:
        full_screenshot = capture_screenshot(app_name=app_name)
    except RuntimeError as exc:
        logger.info("zoom search for %r: could not capture %r's window - %s", target_description, app_name, exc)
        return None
    coords = _ask_vision_for_coordinates_in_image(full_screenshot, target_description)
    if coords is None:
        logger.info("zoom search for %r: initial whole-screen guess failed", target_description)
        return None
    logger.info("zoom search for %r: level 0 (whole screen) guess = (%.0f, %.0f)", target_description, *coords)

    crop_width, crop_height = _ZOOM_CROP_SIZE
    for level in range(1, max_iterations + 1):
        if crop_width <= _ZOOM_TIGHT_THRESHOLD or crop_height <= _ZOOM_TIGHT_THRESHOLD:
            logger.info(
                "zoom search for %r: stopping before level %d - crop %.0fx%.0f already at/below tight threshold",
                target_description,
                level,
                crop_width,
                crop_height,
            )
            break

        cropped_bytes, crop_left, crop_top = _crop_image(full_screenshot, coords[0], coords[1], crop_width, crop_height)
        refined = _ask_vision_for_coordinates_in_image(cropped_bytes, target_description)
        if refined is None:
            # This zoom level's vision call failed - keep the last good
            # guess rather than discarding all refinement progress so far.
            logger.info("zoom search for %r: level %d vision call failed, keeping prior guess", target_description, level)
            break

        coords = _translate_crop_coords(refined[0], refined[1], crop_left, crop_top)
        logger.info(
            "zoom search for %r: level %d crop=%.0fx%.0f at (%.0f, %.0f) -> guess = (%.0f, %.0f)",
            target_description,
            level,
            crop_width,
            crop_height,
            crop_left,
            crop_top,
            *coords,
        )
        crop_width /= _ZOOM_SHRINK_FACTOR
        crop_height /= _ZOOM_SHRINK_FACTOR

    scale = _screen_scale_factor()
    final = (coords[0] / scale, coords[1] / scale)
    logger.info("zoom search for %r: final point-space coordinates = (%.0f, %.0f)", target_description, *final)
    return final


def _crop_image(
    image_bytes: bytes, center_x: float, center_y: float, crop_width: float, crop_height: float
) -> tuple[bytes, float, float]:
    """Crops a PNG (in pixel space, e.g. a full-screen screenshot) to a
    crop_width x crop_height box centered on (center_x, center_y).

    The box is clamped to stay fully within the source image's bounds
    rather than letting PIL pad an off-edge crop with black - a black band
    could easily be mistaken by vision for actual (dark) screen content.

    Returns (cropped_png_bytes, left, top). (left, top) is the crop box's
    origin in the *original* image's pixel space - callers need this to
    translate any coordinate vision reports relative to the crop back into
    the original image's coordinate system, since vision only ever sees
    the cropped image and has no idea it was cropped from something larger.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img_width, img_height = img.size

    left = max(0.0, min(center_x - crop_width / 2, img_width - crop_width))
    top = max(0.0, min(center_y - crop_height / 2, img_height - crop_height))
    right = left + crop_width
    bottom = top + crop_height

    cropped = img.crop((int(left), int(top), int(right), int(bottom)))
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue(), left, top


def _translate_crop_coords(crop_x: float, crop_y: float, crop_left: float, crop_top: float) -> tuple[float, float]:
    """Converts a coordinate vision reported relative to a cropped image's
    own top-left origin back into the original (uncropped) image's pixel
    space, by adding back the crop's offset."""
    return crop_x + crop_left, crop_y + crop_top


def _dispatch_click(x: float, y: float) -> None:
    """Synthesizes a real left-click at (x, y) in point-space using Quartz's
    low-level event APIs (CGEventCreateMouseEvent + CGEventPost), the same
    layer the OS itself uses to deliver hardware mouse events. We use this
    instead of AppleScript's System Events click because System Events
    clicks can steal focus / route to the wrong window when apps aren't
    frontmost; posting a raw HID-level event at a specific point behaves
    like a real click regardless of that.
    """
    move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    time.sleep(0.05)

    down = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    time.sleep(0.05)

    up = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def _dispatch_keyboard_shortcut(keycode: int, flags) -> None:
    """Synthesizes a keyboard shortcut (e.g. Cmd+L) via the same low-level
    Quartz event APIs _dispatch_click uses, for the same reason: it behaves
    like a real keypress regardless of focus/routing quirks that an
    AppleScript System Events keystroke can run into."""
    key_down = Quartz.CGEventCreateKeyboardEvent(None, keycode, True)
    Quartz.CGEventSetFlags(key_down, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, key_down)
    time.sleep(0.05)
    key_up = Quartz.CGEventCreateKeyboardEvent(None, keycode, False)
    Quartz.CGEventSetFlags(key_up, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, key_up)


# Some apps expose a keyboard shortcut that opens their search UI already
# focused, sidestepping the need to click a small icon at all. Added after
# measuring that vision's coordinate guessing for Spotify's collapsed
# magnifying-glass icon has a real accuracy ceiling, not just noise: against
# a known crop with a known ground-truth icon location, both a generic
# description and a much more specific one landed on the same wrong spot
# 7/7 tries - confidently and repeatably wrong, which iterative zooming
# can't fix since zooming only helps when the miss is noise, not a
# consistent misidentification. Not yet wired into type_in_field.
#
# Cmd+L is Spotify's real search shortcut - confirmed directly by reading
# Spotify's own Edit menu via the Accessibility API (AXMenuItemCmdChar /
# AXMenuItemCmdModifiers on the "Search" menu item), not assumed. Keycode
# 37 is 'L' on a standard US keyboard layout.
_APP_SEARCH_SHORTCUTS = {"Spotify": (37, Quartz.kCGEventFlagMaskCommand)} if _MACOS else {}

# Where the opened field lands, as an offset from its window's own top-left
# corner (x as a fraction of window width, y as an absolute point offset
# from the window's top edge) - confirmed directly by screenshotting right
# after dispatching the shortcut: Spotify's search bar renders near the top
# of its window, spanning most of its width. Not yet used by any caller.
_APP_SEARCH_FIELD_OFFSET = {
    "Spotify": (0.5, 65.0),
}


# Demo-only simplification, not a general fix - see planning.md's "demo
# command swapped" entry for the full reasoning. Vision's coordinate guess
# for "first search result" was measured to have a systematic bias, not
# per-image imprecision: both a single-shot whole-screen guess and a 5-point
# grid search around it consistently landed in the same sidebar-adjacent
# region regardless of query or actual on-screen content (0/5 hit rate
# against this exact target). Meanwhile, Spotify's "Top result" card - the
# thing a specific track/artist query surfaces - has a fixed position
# confirmed directly, twice, on two independent queries: clicking point
# (448, 218) started real playback both times, verified via
# _spotify_player_state(). Expressed as a window-frame offset (same pattern
# as _APP_SEARCH_FIELD_OFFSET) rather than a bare screen coordinate so it
# still tracks the window if it's ever moved/resized.
_APP_TOP_RESULT_OFFSET = {
    "Spotify": (0.56, 179.0),
}

# Descriptions that plausibly refer to Spotify's "Top result" card - the
# specific, narrow case _APP_TOP_RESULT_OFFSET covers. Matching an exact
# phrase like "first search result" turned out too brittle in practice:
# the Action agent phrased the same real milestone as "the first track
# result for Billie Jean" in one live run, which the original exact-phrase
# check missed entirely, silently falling through to the slow, unreliable
# vision path this whole thing exists to avoid. Matching on "a position
# word (first/top) AND a result-ish word (result/track/song)" both being
# present is more robust to that kind of natural paraphrasing, without
# being so broad it fires on unrelated targets - and even a wrong fire
# here is caught downstream by click_ui's own outcome verification rather
# than silently reported as success.
_TOP_RESULT_POSITION_WORDS = ("first", "top")
_TOP_RESULT_NOUN_WORDS = ("result", "track", "song")


def _looks_like_top_result(target_description: str) -> bool:
    lowered = target_description.lower()
    has_position = any(word in lowered for word in _TOP_RESULT_POSITION_WORDS)
    has_noun = any(word in lowered for word in _TOP_RESULT_NOUN_WORDS)
    return has_position and has_noun


# Descriptions that plausibly name a search entry point that might currently
# be a collapsed icon rather than an open, visible field - e.g. Spotify's
# Home view shows only a magnifying-glass icon until it's clicked. Confirmed
# directly: asking vision for "search field" coordinates on that view
# returned a confident-looking guess pointing at an unrelated podcast tile,
# because there was no actual field on screen yet for it to find.
_COLLAPSIBLE_FIELD_HINTS = ("search field", "search bar", "search box", "search input")


def _looks_collapsible(target_description: str) -> bool:
    lowered = target_description.lower()
    return any(hint in lowered for hint in _COLLAPSIBLE_FIELD_HINTS)


def _locate_via_window_offset(app_name: str, offset: tuple[float, float]) -> tuple[float, float] | None:
    """Resolves a point-space (x, y) as an offset from app_name's own
    window frame - x as a fraction of window width, y as an absolute point
    offset from the window's top edge (the same offset shape
    _APP_SEARCH_FIELD_OFFSET and _APP_TOP_RESULT_OFFSET both use). Retries
    briefly since a freshly-launched window can report itself frontmost
    slightly before its AX window is actually queryable - the same race
    type_in_field's inline version of this logic (search field lookup)
    already retries for. Returns None if the window frame never resolves.
    """
    window_frame = None
    for _ in range(3):
        window_frame = get_frontmost_window_frame(app_name)
        if window_frame is not None:
            break
        time.sleep(0.2)
    if window_frame is None:
        return None
    win_x, win_y, win_w, _win_h = window_frame
    x_fraction, y_offset = offset
    return (win_x + win_w * x_fraction, win_y + y_offset)


def _locate_element(
    app_name: str, target_description: str, roles: set[str] | None = None, skip_reveal: bool = False
) -> dict:
    """Runs the two-tier location strategy and returns a dict describing
    what was found and how: {"x", "y", "tier": "accessibility" | "vision",
    "reveal_expanded": bool} or {"error": str} if both tiers failed.
    reveal_expanded is only ever True for the vision tier - see
    _looks_collapsible. skip_reveal forces the click-to-reveal step off even
    for a collapsible-looking description - used when the caller already
    knows the field is open (e.g. opened via a keyboard shortcut instead of
    a click) and an extra speculative click here would risk landing on the
    now-open UI unpredictably."""
    element, score = _best_ax_match(app_name, target_description, roles)
    if element is not None:
        return {
            "x": element["x"] + element["width"] / 2,
            "y": element["y"] + element["height"] / 2,
            "tier": "accessibility",
            "matched_label": element["label"],
            "match_score": round(score, 2),
            "reveal_expanded": False,
        }

    # Tier 1 found nothing above threshold - fall back to vision, via the
    # iterative crop-and-zoom search rather than a single whole-screen
    # guess. Measured directly: a single-shot guess for Spotify's search
    # icon landed ~100pt off the real element; zooming in narrows that
    # because the target occupies a much larger fraction of what vision
    # sees once cropped.
    coordinates = _locate_via_vision_zoom(target_description, app_name)
    if coordinates is None:
        return {"error": f"Could not locate '{target_description}' via accessibility or vision."}

    reveal_expanded = False
    if not skip_reveal and _looks_collapsible(target_description):
        # The first guess might be a collapsed icon, not the real field -
        # click it, give the UI a moment to expand, then re-run the zoom
        # search against a fresh screenshot rather than trusting the first
        # guess was already the actual field. Only done when the
        # description plausibly names something collapsible, so a field
        # that's already open and correctly located on the first try
        # doesn't pay this extra click + screenshot + model round-trip.
        _dispatch_click(coordinates[0], coordinates[1])
        time.sleep(0.5)
        refined = _locate_via_vision_zoom(target_description, app_name)
        if refined is not None:
            coordinates = refined
            reveal_expanded = True

    return {"x": coordinates[0], "y": coordinates[1], "tier": "vision", "reveal_expanded": reveal_expanded}


def _verify_expected_app_frontmost(expected_app_name: str) -> dict | None:
    """Guards against acting on the wrong app: click_ui/type_in_field only
    ever operate on whatever app happens to be frontmost at call time, and
    focus can shift for reasons that have nothing to do with the task (the
    user clicking elsewhere, a screenshot tool briefly stealing focus, etc).
    Without this check a stray focus change turns "type into Spotify's
    search box" into "type into whatever text field happens to be in front" -
    which is exactly how a test run once typed a search query into an
    unrelated file open in an editor instead of Spotify. Returns an error
    dict if the frontmost app doesn't match, else None.
    """
    frontmost = _frontmost_app_name()
    if frontmost is None:
        return {
            "success": False,
            "message": "Could not determine the frontmost app.",
            "tier": None,
            "error": "no_frontmost_app",
        }
    if frontmost != expected_app_name:
        return {
            "success": False,
            "message": (
                f"Refusing to act: expected '{expected_app_name}' to be frontmost, "
                f"but '{frontmost}' is frontmost instead. Bring '{expected_app_name}' "
                "to the foreground (e.g. via open_app) before retrying."
            ),
            "tier": None,
            "error": "wrong_frontmost_app",
        }
    return None


def _spotify_player_state() -> dict | None:
    """Queries Spotify's actual player state via AppleScript - whether it's
    playing/paused, and which track. Returns None if the query fails for
    any reason (Spotify not running, no track loaded yet, unexpected
    AppleScript error) - callers must treat None as "state unknown", not as
    "not playing".

    Confirmed directly: `player state` returns "playing" or "paused" as an
    enum (read here `as string`), and `current track`'s `name`/`artist`
    raise an AppleScript error rather than returning something falsy when
    no track has ever been loaded - hence the try/on error wrapper, not a
    None-check on the values. Also confirmed: `tell application "Spotify"`
    launches Spotify if it isn't already running (standard AppleScript
    behavior for any `tell application`) - not a concern for how this is
    actually called here, since callers only reach this after already
    confirming Spotify is the frontmost app, but worth knowing before
    reusing this pattern elsewhere.

    "|||" is used as a field delimiter when concatenating the four values in
    one AppleScript return (rather than four separate osascript calls,
    which would quadruple the subprocess overhead and risk state changing
    between the calls) - not bulletproof against a track name containing
    "|||" itself, but adequate for this use case.

    Includes the track's own Spotify URI - confirmed directly, necessary:
    on the Free tier, "player state" legitimately transitions to "playing"
    for an inserted ad (its URI looks like "spotify:ad:...", not
    "spotify:track:...") exactly the same way it does for a real track -
    ad playback is otherwise indistinguishable from real playback by
    player_state/track_name/track_artist alone (a live ad genuinely
    reported track_name "CHRISTUS Health", track_artist "" - not obviously
    fake-looking data). Callers that need to confirm a *specific requested
    track* is playing, not just that *something* is, must check this.
    """
    script = (
        'tell application "Spotify"\n'
        "try\n"
        "  set ps to player state as string\n"
        "  set tn to name of current track\n"
        "  set ta to artist of current track\n"
        "  set tu to spotify url of current track\n"
        '  return ps & "|||" & tn & "|||" & ta & "|||" & tu\n'
        "on error\n"
        '  return "error||||||"\n'
        "end try\n"
        "end tell"
    )
    # Found necessary directly: under rapid repeated queries (e.g. a grid
    # search's several attempts in quick succession), osascript occasionally
    # hangs past the timeout rather than erroring cleanly - an uncaught
    # subprocess.TimeoutExpired here would crash the entire caller instead
    # of degrading to "state unknown" the way every other failure mode
    # already does.
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split("|||")
    if len(parts) != 4 or parts[0] == "error":
        return None
    return {"player_state": parts[0], "track_name": parts[1], "track_artist": parts[2], "track_uri": parts[3]}


def _spotify_state_is_ad(state: dict | None) -> bool:
    """True if a _spotify_player_state() snapshot is a Spotify Free ad
    rather than a real track - see _spotify_player_state's docstring for
    why this needs its own explicit check rather than trusting
    player_state/track_name alone."""
    return bool(state) and str(state.get("track_uri", "")).startswith("spotify:ad:")


def _spotify_playback_changed(before: dict | None, after: dict | None) -> bool:
    """True if Spotify's real player state shows playback meaningfully
    changed between two _spotify_player_state() snapshots - either
    transitioning into "playing" from something else, or the loaded track
    itself changing. Either signal alone can indicate a real "play" click
    worked: clicking play on a *different* track while something was
    already playing changes the track without necessarily toggling
    player_state; clicking play from a stopped/paused state changes
    player_state without necessarily changing the track (e.g. resuming the
    same track). Not yet used by any caller.
    """
    if before is None or after is None:
        return False
    started_playing = before["player_state"] != "playing" and after["player_state"] == "playing"
    track_changed = (before["track_name"], before["track_artist"]) != (after["track_name"], after["track_artist"])
    return started_playing or track_changed


# --- play_spotify_track: Spotify Web API search + AppleScript play ---------
#
# Replaces the click_ui/type_in_field path for playing a specific track.
# That path drove Spotify's search box and clicked the "Top result" card at
# a fixed pixel offset - which planning.md documents as ~1/3 reliable
# ("systematic bias, not per-image imprecision") and which failed outright
# in the regression test. Spotify has a real scripting API: once we have a
# `spotify:track:...` URI, `tell application "Spotify" to play track "<uri>"`
# starts it deterministically (verified: instant, no window focus needed,
# launches Spotify itself). The only thing pixels were buying us was
# resolving a query string to a URI - which the Spotify Web API's /search
# endpoint does exactly, so that's what we use.

import base64 as _base64
import urllib.error as _urlerror
import urllib.parse as _urlparse
import urllib.request as _urlrequest

_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SPOTIFY_SEARCH_URL = "https://api.spotify.com/v1/search"
# Client-credentials tokens last 3600s; cache and refresh a minute early.
_spotify_token_cache: dict = {"token": None, "expires_at": 0.0}


def _spotify_access_token() -> tuple[str | None, str | None]:
    """Client-credentials token for the Spotify Web API (search only - no
    user context). Needs SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in the
    environment (create a free app at developer.spotify.com; no redirect URI
    needed for this grant). Returns (token, error)."""
    now = time.time()
    if _spotify_token_cache["token"] and now < _spotify_token_cache["expires_at"]:
        return _spotify_token_cache["token"], None

    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return None, "spotify_not_configured"

    basic = _base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = _urlrequest.Request(
        _SPOTIFY_TOKEN_URL,
        data=b"grant_type=client_credentials",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with _urlrequest.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
    except _urlerror.HTTPError as exc:
        return None, f"spotify_auth_failed ({exc.code})"
    except Exception as exc:  # noqa: BLE001
        return None, f"spotify_auth_error ({exc})"

    token = payload.get("access_token")
    if not token:
        return None, "spotify_auth_no_token"
    _spotify_token_cache["token"] = token
    _spotify_token_cache["expires_at"] = now + int(payload.get("expires_in", 3600)) - 60
    return token, None


def _spotify_search_track(query: str, token: str) -> tuple[dict | None, str | None]:
    """First track match for `query`. Returns (track_dict, error) where
    track_dict has uri / name / artist."""
    url = f"{_SPOTIFY_SEARCH_URL}?" + _urlparse.urlencode({"q": query, "type": "track", "limit": 1})
    req = _urlrequest.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with _urlrequest.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
    except _urlerror.HTTPError as exc:
        return None, f"spotify_search_failed ({exc.code})"
    except Exception as exc:  # noqa: BLE001
        return None, f"spotify_search_error ({exc})"

    items = (payload.get("tracks") or {}).get("items") or []
    if not items:
        return None, "no_match"
    t = items[0]
    return {
        "uri": t.get("uri", ""),
        "name": t.get("name", ""),
        "artist": ", ".join(a.get("name", "") for a in t.get("artists", [])),
    }, None


def play_spotify_track(query: str) -> dict:
    """Plays a specific song in Spotify. Give a natural query like
    "Billie Jean by Michael Jackson" or "Bohemian Rhapsody".

    Resolves the query to a track via the Spotify Web API, then plays it
    with AppleScript (`play track "<uri>"`), which launches Spotify if
    needed. Verified against Spotify's real player state afterwards.

    Returns a dict with:
        success: bool - True only if Spotify's real state confirms the
            resolved track is now playing
        message: human-readable summary
        error: raw error string if something went wrong, else None
    """
    _require_macos()

    token, err = _spotify_access_token()
    if err == "spotify_not_configured":
        return {
            "success": False,
            "message": (
                "Spotify playback needs API credentials. Add SPOTIFY_CLIENT_ID and "
                "SPOTIFY_CLIENT_SECRET to the repo-root .env (create a free app at "
                "developer.spotify.com - no redirect URI needed)."
            ),
            "error": "spotify_not_configured",
        }
    if err:
        return {"success": False, "message": f"Could not authenticate with Spotify: {err}", "error": err}

    track, err = _spotify_search_track(query, token)
    if err == "no_match":
        return {"success": False, "message": f"Spotify search found no track for {query!r}.", "error": "no_match"}
    if err:
        return {"success": False, "message": f"Spotify search failed: {err}", "error": err}
    if not track["uri"]:
        return {"success": False, "message": f"Spotify returned a match for {query!r} with no playable URI.", "error": "no_uri"}

    before = _spotify_player_state()

    script = (
        'tell application "Spotify"\n'
        f'  play track "{track["uri"]}"\n'
        "  activate\n"
        "end tell"
    )
    launch = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    if launch.returncode != 0:
        return {
            "success": False,
            "message": f"Resolved {track['name']!r} by {track['artist']!r} but the AppleScript play command failed.",
            "error": launch.stderr.strip() or "osascript_failed",
        }

    # Give Spotify a moment to actually start, then read the real state back.
    after = None
    for _ in range(6):
        time.sleep(0.4)
        after = _spotify_player_state()
        if after and after.get("player_state") == "playing":
            break

    if after is None:
        return {
            "success": False,
            "message": f"Sent play for {track['name']!r} but couldn't read Spotify's player state back to confirm it.",
            "error": "state_unreadable",
        }

    playing = after.get("player_state") == "playing"
    # The API's track name is the authority; Spotify's own current-track name
    # should now match it (allow a loose contains-match for remaster suffixes).
    want = track["name"].lower()
    got = (after.get("track_name") or "").lower()
    right_track = want in got or got in want or _spotify_playback_changed(before, after)

    if playing and right_track:
        return {
            "success": True,
            "message": (
                f"Playing '{after['track_name']}' by {after['track_artist']} in Spotify "
                f"(resolved from {query!r}, verified via player state)."
            ),
            "error": None,
        }
    return {
        "success": False,
        "message": (
            f"Sent play for '{track['name']}' by {track['artist']}, but Spotify's state is "
            f"player_state={after.get('player_state')!r}, current track={after.get('track_name')!r} "
            f"- not the confirmed 'playing the right track' outcome."
        ),
        "error": "playback_not_verified",
    }


play_spotify_track_tool = FunctionTool(play_spotify_track)


# --- search_spotify_candidates: read-before-you-act search selection -------
#
# play_spotify_track above resolves a query to a URI via the Spotify Web
# API and plays it deterministically - but that API is unavailable here (no
# SPOTIFY_CLIENT_ID/SECRET configured, and this account's Free tier was
# confirmed blocked from it). Without a resolvable URI, the only way to
# start a specific track is Spotify's own in-app search, and Spotify's own
# top-ranked result for an ambiguous query (e.g. "Mad World") is not always
# the one the user meant (a cover can outrank the studio original).
#
# This function only reads back what Spotify's search actually shows -
# never plays anything - so the Action agent can reason about whether the
# top result is a confident match before acting. Investigated and confirmed
# directly (see planning.md): Spotify's Electron UI exposes ~nothing via
# the Accessibility API (re-verified three separate ways, most recently a
# 40-level-deep AX walk that found 17 total nodes, all window chrome), so
# vision-on-a-scoped-screenshot is the only viable way to read the results.
#
# Real limits this design works within, confirmed by direct testing, not
# assumed: there is no keyboard/AX/AppleScript path to select or play any
# result other than the top one (arrow keys + Tab produce no reachable
# selection state; Spotify's own scripting dictionary - read straight from
# Spotify.app's Spotify.sdef - has no search verb and `play track` requires
# a URI we don't have here). So the only two honest outcomes downstream of
# this read are: accept the top result via click_ui's existing fixed-offset
# "Top result" click (already verified reliable, see _APP_TOP_RESULT_OFFSET),
# or don't act at all and say so - never guess at a non-top position.


def _ask_vision_for_spotify_candidates(image_bytes: bytes, query: str) -> list[dict] | None:
    """Asks Gemini vision to read back Spotify's visible search results as
    structured data, instead of just a location - the Action agent needs
    to reason about which one (if any) is a confident match, not just where
    it is on screen. Returns a list of {"position", "title", "artist",
    "kind"} dicts in on-screen top-to-bottom order, or None if the reply
    wasn't usable JSON."""
    api_key = os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-flash-lite-latest",
        contents=[
            genai_types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            (
                f"This is a screenshot of Spotify's search results for the query {query!r}. "
                "List every visible track/song result, in the order they actually appear on "
                "screen (top to bottom). For each one give its exact title, its artist(s) as "
                "shown, and your best read of what kind of version it is - one of: \"song\" "
                "for a normal studio track, \"cover\", \"live\", \"remix\", \"music video\", "
                "or \"other\" if you can't tell. Only list actual song/track results - skip "
                "albums, artists, playlists, and podcasts unless no track results are visible "
                "at all.\n"
                "Reply with ONLY raw JSON (no markdown fences): a list like "
                '[{"position": 1, "title": "...", "artist": "...", "kind": "song"}, ...]. '
                "If you see no track results at all, reply with an empty list []."
            ),
        ],
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _normalize_track_title(title: str) -> str:
    """Normalizes a track title for same-song comparison: lowercase, drop
    parenthetical/bracketed suffixes ('(feat. ...)', '[Live]', remaster
    years, etc.) and non-alphanumeric noise, so 'Mad World' and 'Mad World
    (feat. K.J. Apa...)' compare as the same underlying title."""
    lowered = title.lower()
    lowered = re.sub(r"[\(\[].*?[\)\]]", "", lowered)
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def _detect_spotify_ambiguity(query: str, candidates: list[dict]) -> str | None:
    """Deterministic same-title/different-artist check - the reason this
    exists instead of leaving it to the Action agent's own judgment: tested
    directly against a real case (query 'Mad World', top result Gary Jules,
    a Tears For Fears result one position below), the small, fast model this
    project uses for the Action agent twice accepted the top result without
    flagging it, even with an explicit prompt instruction to check for
    exactly this. A real title/artist collision is a hard, mechanical fact
    the code can check directly rather than trust a small model's judgment
    for - same "don't trust self-reported/inferred judgment when a real
    check exists" principle this codebase already applies to click/type
    verification (pixel-diff before vision, tool result before agent prose).

    Only counts as ambiguous when the user's own query doesn't already name
    one of the conflicting artists (that's the user disambiguating already)
    and titles match after normalization (not just loosely similar, to
    avoid false ambiguity on merely-similar titles). Returns a human-
    readable reason naming the conflicting artists, or None if no
    conflict is detected - this says nothing about live/remix/music-video
    badging, which is left to the Action agent's own reasoning over `kind`.
    """
    groups: dict[str, set[str]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        title, artist = candidate.get("title"), candidate.get("artist")
        if not title or not artist:
            continue
        key = _normalize_track_title(str(title))
        if not key:
            continue
        groups.setdefault(key, set()).add(str(artist).strip())

    query_lower = query.lower()

    def named_in_query(artist: str) -> bool:
        # Candidate artist fields are often comma-joined ("Gary Jules,
        # Michael Andrews") - check each named artist individually, since a
        # query naming just one of them ("Mad World by Gary Jules") still
        # means the user disambiguated, even though the joined string as a
        # whole never appears verbatim in the query.
        return any(part.strip() and part.strip() in query_lower for part in artist.lower().split(","))

    conflicts: list[str] = []
    for artists in groups.values():
        if len(artists) < 2:
            continue
        if any(named_in_query(artist) for artist in artists):
            continue  # the user already named one of these - not ambiguous
        conflicts.append(", ".join(sorted(artists)))

    if not conflicts:
        return None
    return "different artists have a matching title: " + "; ".join(conflicts)


def search_spotify_candidates(query: str) -> dict:
    """Searches Spotify for `query` and reads back the visible results
    WITHOUT starting playback - the first step in picking the right track
    when Spotify's own search ranking might not match what the user asked
    for (a cover/live/remix outranking the studio original, etc).

    Launches/foregrounds Spotify itself if needed (same as play_spotify_track
    - no open_app call needed first), opens its search via the keyboard
    shortcut (no click), types the query, submits it, and reads the visible
    result cards via Gemini vision on a window-scoped screenshot.

    `success` in the returned dict is ALWAYS False - this is a read-only
    step, not a completion signal. Reading candidates back is never the
    same as the requested song actually playing, so treating this as a
    milestone's last/deciding tool call must never be mistaken for
    success. `ambiguous`/`ambiguity_reason` are a deterministic check (same
    title, different artists, and the query didn't already name one) - not
    a suggestion: when ambiguous is True, do not call click_ui, respond
    with a clarifying question instead. When it's False, still reason over
    `candidates`' `kind` before playing the top one (e.g. a live/remix/
    music-video badge the user didn't ask for is still a reason to ask) -
    the deterministic check only covers the same-title-different-artist
    case, nothing else. There is no reliable way to select any result
    other than the top one either way.

    Returns a dict with:
        success: bool - always False, see above
        ambiguous: bool - True if a same-title/different-artist conflict
            was detected and the query didn't already name one of them
        ambiguity_reason: str | None - human-readable detail when ambiguous
        read_ok: bool - True if at least one candidate was read back
        candidates: list of {"position", "title", "artist", "kind"}, in
            on-screen order
        message: human-readable summary
        error: raw error string if something went wrong, else None
    """
    _require_macos()

    launch = open_app("Spotify")
    if not launch["success"]:
        return {
            "success": False,
            "read_ok": False,
            "candidates": [],
            "message": f"Could not bring Spotify to the foreground: {launch['message']}",
            "error": launch["error"],
        }
    time.sleep(0.5)  # let a just-launched window finish settling before driving it

    shortcut = _APP_SEARCH_SHORTCUTS.get("Spotify")
    if shortcut is None:
        return {
            "success": False,
            "read_ok": False,
            "candidates": [],
            "message": "No search keyboard shortcut configured for Spotify.",
            "error": "no_search_shortcut",
        }

    _dispatch_keyboard_shortcut(53, 0)  # escape - clear any stray dropdown/focus state first
    time.sleep(0.3)
    _dispatch_keyboard_shortcut(*shortcut)  # Cmd+L - opens the search field already focused
    time.sleep(0.6)

    _select_all_and_paste(query)
    time.sleep(0.2)

    _dispatch_keyboard_shortcut(36, 0)  # return - submits to the full results page (does not play anything)
    # Measured necessary directly: the results page's render is not
    # immediate, and testing in separate process invocations with shorter
    # gaps captured stale/unrelated content mid-transition - this is a
    # single continuous call with a generous settle time instead.
    time.sleep(2.5)

    try:
        screenshot = capture_screenshot(app_name="Spotify")
    except RuntimeError as exc:
        return {
            "success": False,
            "read_ok": False,
            "candidates": [],
            "message": f"Could not capture Spotify's window to read results: {exc}",
            "error": "capture_failed",
        }

    candidates = _ask_vision_for_spotify_candidates(screenshot, query)
    if not candidates:
        return {
            "success": False,
            "read_ok": False,
            "candidates": [],
            "message": f"Searched Spotify for {query!r} but could not read back any visible track results.",
            "error": "no_candidates_read",
        }

    ambiguity_reason = _detect_spotify_ambiguity(query, candidates)
    if ambiguity_reason is not None:
        message = (
            f"Searched Spotify for {query!r} and read back {len(candidates)} visible result(s) "
            f"- AMBIGUOUS ({ambiguity_reason}). Do not call click_ui for this - respond with a "
            "clarifying question naming the specific candidates involved, since the user didn't "
            "say which one they meant."
        )
    else:
        message = (
            f"Searched Spotify for {query!r} and read back {len(candidates)} visible result(s) "
            "with no title/artist conflict detected. Still confirm the top one is a real match "
            "(e.g. not a live/remix/music-video version the user didn't ask for) before calling "
            "click_ui to play it."
        )

    return {
        "success": False,
        "read_ok": True,
        "candidates": candidates,
        "ambiguous": ambiguity_reason is not None,
        "ambiguity_reason": ambiguity_reason,
        "message": message,
        "error": None,
    }


search_spotify_candidates_tool = FunctionTool(search_spotify_candidates)


# --- read_kayak_flight_results: Stage 3 of the clarification/booking -------
# subsystem (see planning.md). Same read-before-you-act shape as
# search_spotify_candidates above - never plays/books anything, only reads
# back what a real, already-submitted Kayak search actually shows, as
# structured data, so the user can be asked which one they want (Stage 1/2's
# pause primitive) rather than Jarvis silently picking one. Vision-based for
# the same reason search_spotify_candidates is: Kayak's result cards carry
# no single, stable, easily-queried DOM shape worth building a bespoke
# parser against (badges, logos, and layout details change card to card),
# and a screenshot of a rendered results page is something Gemini vision
# reads cleanly, confirmed directly against a real page (see planning.md) -
# 3/3 real test runs against the same real screenshot came back with every
# field (airline, price, both times, duration, stops, badge) exactly
# correct, including correctly reading the third card's airline name off a
# logo when that card was partially cut off at the screenshot's bottom edge.


def _ask_vision_for_kayak_flight_candidates(image_bytes: bytes) -> list[dict] | None:
    """Asks Gemini vision to read back Kayak's visible flight results as
    structured data. Returns a list of {"position", "airline", "price",
    "depart_time", "arrive_time", "duration", "stops", "badge"} dicts in
    on-screen top-to-bottom order, or None if the reply wasn't usable JSON."""
    api_key = os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-flash-lite-latest",
        contents=[
            genai_types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            (
                "This is a screenshot of a Kayak.com flight search results page. "
                "List the top 3 visible flight results, in the order they actually appear on "
                "screen (top to bottom). For each one give: the airline name, the price as "
                "shown (with currency symbol), the departure time, the arrival time, the total "
                "duration, and the number of stops (0 for nonstop, or the number of stops). "
                "Also include any badge label Kayak shows on that result (e.g. \"Best\", "
                "\"Cheapest\", \"Cheapest nonstop\"), or null if none is shown. Only list actual "
                "flight result cards - skip filter bars, ads, or any other page chrome.\n"
                "Reply with ONLY raw JSON (no markdown fences): a list like "
                '[{"position": 1, "airline": "...", "price": "...", "depart_time": "...", '
                '"arrive_time": "...", "duration": "...", "stops": 0, "badge": "Best"}, ...]. '
                "If you see no flight results at all, reply with an empty list []."
            ),
        ],
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def read_kayak_flight_results() -> dict:
    """Reads back the top 3 flight results from an already-submitted real
    Kayak search - does NOT navigate, search, or click anything itself.
    Meant to run as the milestone right after "the flight search is
    submitted" (the existing approval-gated step), while the results page
    Chrome already has open is still the frontmost window.

    `success` is ALWAYS False, same convention as search_spotify_candidates
    and for the same reason: this is a read-only step, never a completion
    signal, so it can never be mistaken for "the task is done" even if it's
    the last tool called in its milestone (see main.run_action's
    _LOOKUP_ONLY_TOOLS/deciding-tool logic for the general version of this
    problem this convention sidesteps).

    If some other app has stolen the foreground since the search was
    submitted, this tries one real, non-destructive `open_app("Google
    Chrome")` re-activation before giving up - real, live testing found
    another app genuinely grabbing focus between milestones on occasion,
    and the Action agent's own fallback (calling navigate_to_url again)
    made things worse: a fresh navigation reloads Kayak's homepage,
    destroying the very results this tool exists to read. `open_app` only
    brings the existing window forward - no URL, no reload, no lost state.

    Returns a dict with:
        success: bool - always False, see above
        read_ok: bool - True if at least one candidate was read back
        candidates: list of {"position", "airline", "price", "depart_time",
            "arrive_time", "duration", "stops", "badge"}, in on-screen order
        message: human-readable summary, safe to read aloud/present as-is
        error: raw error string if something went wrong, else None
    """
    _require_macos()

    frontmost = _frontmost_app_name()
    if frontmost != "Google Chrome":
        open_app("Google Chrome")
        frontmost = _frontmost_app_name()
    if frontmost != "Google Chrome":
        return {
            "success": False,
            "read_ok": False,
            "candidates": [],
            "message": (
                f"Cannot read Kayak's results - expected Google Chrome to be frontmost, but "
                f"{frontmost!r} is frontmost instead, even after trying to bring it forward."
            ),
            "error": "wrong_frontmost_app",
        }

    try:
        screenshot = capture_screenshot(app_name="Google Chrome")
    except RuntimeError as exc:
        return {
            "success": False,
            "read_ok": False,
            "candidates": [],
            "message": f"Could not capture Chrome's window to read Kayak's results: {exc}",
            "error": "capture_failed",
        }

    candidates = _ask_vision_for_kayak_flight_candidates(screenshot)
    if not candidates:
        return {
            "success": False,
            "read_ok": False,
            "candidates": [],
            "message": "Could not read back any visible flight results from the current page.",
            "error": "no_candidates_read",
        }

    lines = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        stops = c.get("stops")
        stops_text = "nonstop" if stops == 0 else f"{stops} stop{'s' if stops != 1 else ''}"
        badge = f" ({c['badge']})" if c.get("badge") else ""
        lines.append(
            f"{c.get('position')}) {c.get('airline')} - {c.get('price')} - "
            f"{c.get('depart_time')} to {c.get('arrive_time')}, {c.get('duration')}, "
            f"{stops_text}{badge}"
        )
    summary = "; ".join(lines)

    return {
        "success": False,
        "read_ok": True,
        "candidates": candidates,
        "message": (
            f"Read back {len(candidates)} flight result(s) from Kayak: {summary}. "
            "Present these to the user (in this order) and ask which one they want - "
            "never pick one automatically."
        ),
        "error": None,
    }


read_kayak_flight_results_tool = FunctionTool(read_kayak_flight_results)


_VERIFY_REGION_SIZE = (400.0, 160.0)  # width, height in points, centered on the field
_NO_CHANGE_DIFF_THRESHOLD = 2.0  # mean 0-255 grayscale diff below this = "nothing visibly happened"

# The one honest justification every capture_region_unverified() call in this
# module shares - see that function's docstring in perception.py for why it
# requires one at all. Every call site below passes a point this same call
# already resolved via _locate_element (AX or vision) or a fixed window
# offset, for before/after diff-checking a click/type it's about to perform
# (or just performed) inside expected_app_name - which _verify_expected_app_
# frontmost already confirmed is actually frontmost before any of this ran.
# Not independently guessed coordinates the way the real incident's did.
_DIFF_REGION_CAPTURE_REASON = (
    "small before/after diff-check region centered on a point this same call already resolved "
    "(AX/vision/fixed offset), inside an app already verified frontmost - not guessed coordinates"
)


def _region_pixel_diff_score(before_png: bytes, after_png: bytes) -> float:
    """Mean per-pixel grayscale difference (0-255) between two same-region
    screenshots. Cheap, deterministic, and immune to hallucination - unlike
    a single vision yes/no call, which measurably gave a false positive on
    this exact scenario (asked to confirm text was visible on an unchanged
    Spotify screen, it said yes once out of five otherwise-correct tries).
    Used as a first, harder gate before ever trusting vision's judgment.
    """
    before_img = Image.open(io.BytesIO(before_png)).convert("L")
    after_img = Image.open(io.BytesIO(after_png)).convert("L")
    if before_img.size != after_img.size:
        after_img = after_img.resize(before_img.size)
    diff = ImageChops.difference(before_img, after_img)
    histogram = diff.histogram()
    pixel_count = sum(histogram)
    if pixel_count == 0:
        return 0.0
    weighted_sum = sum(value * count for value, count in enumerate(histogram))
    return weighted_sum / pixel_count


def _verify_text_entered(
    app_name: str, expected_text: str, before_region_png: bytes, region_center: tuple[float, float]
) -> tuple[bool, str]:
    """Confirms typed text actually landed somewhere, instead of reporting
    success just because the paste command didn't error.

    Tier A: a fresh (uncached) accessibility query for real field *values*
    (not labels - see perception.get_field_values) - fast, no model call,
    and exact when it works.

    Tier B (only when tier A finds no text fields at all - confirmed with
    Spotify, whose AX tree exposes none): a before/after pixel diff of the
    region around where we clicked. If nothing visibly changed there, that's
    a confident, hallucination-free "the click missed" - no need to even
    ask vision. Only if something DID visibly change do we ask vision to
    confirm it's actually the expected text, and only on that small cropped
    region rather than the whole screen (a much less ambiguous question,
    which is what produced the false positive in the first place).
    """
    field_values = get_field_values(app_name)
    if field_values:
        for value in field_values:
            if expected_text.lower() in (value or "").lower():
                return True, f"confirmed via accessibility - a field's value contains {expected_text!r}"
        return False, (
            f"accessibility shows text field(s) present but none contain {expected_text!r} "
            f"(saw: {field_values!r})"
        )

    x, y = region_center
    width, height = _VERIFY_REGION_SIZE
    after_region_png = capture_region_unverified(x, y, width, height, reason=_DIFF_REGION_CAPTURE_REASON)
    diff_score = _region_pixel_diff_score(before_region_png, after_region_png)

    if diff_score < _NO_CHANGE_DIFF_THRESHOLD:
        return False, (
            f"no visual change detected near the click point (diff score {diff_score:.2f}) "
            "- the click likely missed its target"
        )

    api_key = os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-flash-lite-latest",
        contents=[
            genai_types.Part.from_bytes(data=after_region_png, mime_type="image/png"),
            (
                f"Does this cropped screenshot show the text {expected_text!r} - e.g. typed "
                "into a search field or input box? Reply with ONLY raw JSON (no markdown "
                'fences): {"visible": true or false}'
            ),
        ],
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(text)
        visible = bool(parsed.get("visible"))
    except (json.JSONDecodeError, AttributeError, TypeError):
        visible = False

    detail = (
        f"visual change detected near the click (diff score {diff_score:.2f}) and vision "
        f"confirms {expected_text!r} is visible there"
        if visible
        else f"visual change detected near the click (diff score {diff_score:.2f}) but vision "
        f"could not confirm {expected_text!r} is the text shown"
    )
    return visible, detail


# A click's success signal depends entirely on what the click was supposed
# to do - there's no single universal "did the click work" check the way
# there was for typed text (which always has the same signal: did the
# expected text appear). Two verification paths, tried in order:
#
# 1. OS-level state, when the app exposes one relevant to the requested
#    outcome. Strictly more reliable than any screenshot-based check when
#    available, since it reads the actual application state rather than
#    inferring it from pixels. Currently only Spotify/playback is wired up.
# 2. pixel-diff-then-vision-tiebreaker (same base pattern as
#    _verify_text_entered), for everything else. The vision call is asked
#    about the *specific* expected_outcome the caller supplied, not a
#    generic "did this work" - a vague question is exactly what produced
#    the measured false positive that motivated pixel-diff-first in the
#    type_in_field case.
_APP_PLAYER_STATE_CHECKS = {
    "Spotify": _spotify_player_state,
}

# Outcome descriptions that plausibly concern playback, and therefore that
# Spotify's real player state (rather than a screenshot) can actually speak
# to. A click whose expected outcome is unrelated to playback (e.g. "the
# Podcasts filter is selected") wouldn't be confirmed OR refuted by player
# state, so it should fall through to the pixel-diff/vision path instead of
# being incorrectly judged by a check that has nothing to say about it.
_PLAYBACK_OUTCOME_HINTS = (
    "play",
    "playing",
    "played",
    "plays",
    "pause",
    "paused",
    "pausing",
    "pauses",
    "track",
    "tracks",
    "song",
    "songs",
    "music",
)

# Matches _PLAYBACK_OUTCOME_HINTS as whole words only, not substrings. Found
# necessary directly, twice: first, a plain `in` check matched "play" inside
# "playlist", so an outcome like "the playlist page opens" (pure navigation,
# nothing to do with playback) was wrongly gated into the player-state
# check, which then reported a false "no playback change" for something
# player state was never able to speak to in the first place. Second, after
# fixing that by matching only the bare word "play", a real playback
# outcome phrased as "...now playing bar displays..." (a completely normal,
# common phrasing) stopped matching at all, since "playing" isn't the exact
# word "play" - missing the OS-state check entirely and falling through to
# the less reliable vision fallback for a case player state could have
# answered directly. Fixed by listing the actual word forms instead of
# trying to stem them (English's spelling irregularities - e.g. "pause" ->
# "pausing" drops the "e" - make a generic suffix regex more error-prone
# than just enumerating the forms that actually occur in practice).
_PLAYBACK_OUTCOME_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(hint) for hint in _PLAYBACK_OUTCOME_HINTS) + r")\b"
)


def _looks_like_playback_outcome(text: str) -> bool:
    return bool(_PLAYBACK_OUTCOME_PATTERN.search(text.lower()))


_SPOTIFY_AD_RETRY_ATTEMPTS = 5  # ~5s total absorbs a normal short pre-roll ad without blocking indefinitely
_SPOTIFY_AD_RETRY_INTERVAL = 1.0


def _verify_click_outcome(
    app_name: str,
    expected_outcome: str,
    before_player_state: dict | None,
    before_region_png: bytes,
    region_center: tuple[float, float],
) -> tuple[bool, str]:
    """Confirms a click actually produced its intended outcome, instead of
    reporting success just because a click was dispatched without erroring.

    before_player_state is the app's OS-level state (if any) captured
    *before* the click - passed in rather than queried here so the caller
    controls exactly when the "before" snapshot is taken relative to the
    click. Not yet called by click_ui.
    """
    state_check = _APP_PLAYER_STATE_CHECKS.get(app_name)
    if state_check is not None and before_player_state is not None and _looks_like_playback_outcome(expected_outcome):
        after_player_state = state_check()
        # Spotify's Free tier can insert an ad on a play action - confirmed
        # directly: player_state legitimately went paused -> playing with a
        # plausible-looking track_name/track_artist that was actually an ad
        # (its URI was "spotify:ad:...", not "spotify:track:..."), which the
        # plain "did playback change" check below can't tell apart from a
        # real track starting. Give a short window for the ad to clear
        # before judging - not indefinitely (ads can run well past what's
        # reasonable to block a single tool call on), but long enough to
        # absorb a normal short pre-roll ad rather than reporting a
        # confident-looking false success.
        for _ in range(_SPOTIFY_AD_RETRY_ATTEMPTS):
            if not _spotify_state_is_ad(after_player_state):
                break
            time.sleep(_SPOTIFY_AD_RETRY_INTERVAL)
            after_player_state = state_check()

        if after_player_state is not None:
            detail = (
                f"Spotify's real player state before={before_player_state}, after={after_player_state}"
            )
            if _spotify_state_is_ad(after_player_state):
                return False, (
                    f"Spotify started playing an ad instead of the requested track - {detail}. "
                    "Cannot verify the requested track is playing while an ad is active."
                )
            changed = _spotify_playback_changed(before_player_state, after_player_state)
            if changed:
                return True, f"confirmed via Spotify's player state - {detail}"
            return False, f"no playback change per Spotify's player state - {detail}"
        # State check returned nothing usable - fall through to the
        # screenshot-based path rather than treating that as a hard failure.

    x, y = region_center
    width, height = _VERIFY_REGION_SIZE
    after_region_png = capture_region_unverified(x, y, width, height, reason=_DIFF_REGION_CAPTURE_REASON)
    diff_score = _region_pixel_diff_score(before_region_png, after_region_png)

    if diff_score < _NO_CHANGE_DIFF_THRESHOLD:
        return False, (
            f"no visual change detected near the click point (diff score {diff_score:.2f}) "
            "- the click likely missed its target"
        )

    api_key = os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-flash-lite-latest",
        contents=[
            genai_types.Part.from_bytes(data=after_region_png, mime_type="image/png"),
            (
                f"This screenshot was taken right after a click that was expected to cause: "
                f"{expected_outcome!r}. Does this image show that this specific expected "
                "outcome actually happened - not just that something changed, but that this "
                'particular outcome did? Reply with ONLY raw JSON (no markdown fences): '
                '{"outcome_happened": true or false}'
            ),
        ],
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(text)
        happened = bool(parsed.get("outcome_happened"))
    except (json.JSONDecodeError, AttributeError, TypeError):
        happened = False

    detail = (
        f"visual change detected near the click (diff score {diff_score:.2f}) and vision "
        f"confirms {expected_outcome!r} happened"
        if happened
        else f"visual change detected near the click (diff score {diff_score:.2f}) but vision "
        f"could not confirm {expected_outcome!r} happened"
    )
    return happened, detail


# Grid-click-and-verify: for targets where a single vision coordinate guess
# has already been measured unreliable (Spotify's search results - see
# planning.md), rather than trying to make one guess more precise, try a
# small number of candidate points around the guess and use the outcome
# verification above (already proven reliable via real state checks) to
# detect which one, if any, actually worked. Not yet wired into click_ui.
_GRID_SEARCH_MAX_ATTEMPTS = 5
_GRID_SEARCH_SPACING = 120.0  # points between adjacent candidates


def _generate_grid_candidates(center_x: float, center_y: float, spacing: float) -> list[tuple[float, float]]:
    """5-point cross pattern - center first (vision's own best guess is
    still the single most likely candidate), then one spacing-unit up,
    down, left, and right. Tried in this order so a near-miss in any one
    direction is covered without needing a full grid."""
    return [
        (center_x, center_y),
        (center_x, center_y - spacing),
        (center_x, center_y + spacing),
        (center_x - spacing, center_y),
        (center_x + spacing, center_y),
    ]


_GRID_SEARCH_NAV_DIFF_THRESHOLD = 40.0  # wide-region diff above this suggests navigation, not just a local miss


def locate_and_click_via_grid_search(
    app_name: str, target_description: str, expected_outcome: str
) -> dict:
    """Locates and clicks a target by trying a small number of candidate
    points around vision's rough guess, instead of trusting one coordinate
    guess to be precise. For targets where that single guess has already
    been measured unreliable (see planning.md's crop-and-zoom entry for
    Spotify's search results) - not a general-purpose replacement for
    click_ui's normal two-tier location.

    Each candidate is tried in order: dispatch the click, immediately check
    via _verify_click_outcome (the same OS-state-first / pixel-diff-plus-
    vision verification click_ui already uses) whether expected_outcome
    actually happened, and stop as soon as one hits. Capped at
    _GRID_SEARCH_MAX_ATTEMPTS candidates so a bad initial guess can't turn
    into unbounded clicking.

    Guards against a wrong click doing something worse than nothing: after
    each failed candidate, a wide-region screenshot diff checks whether the
    click appears to have navigated away from the expected view entirely
    (as opposed to just missing its target with no effect) - if so, the
    remaining candidates are not tried, since clicking blindly in a now
    different, unknown context could make things worse rather than better.
    """
    guard_error = _verify_expected_app_frontmost(app_name)
    if guard_error is not None:
        return guard_error

    try:
        full_screenshot = capture_screenshot(app_name=app_name)
    except RuntimeError as exc:
        return {
            "success": False,
            "message": f"Could not capture '{app_name}''s window: {exc}",
            "tier": None,
            "error": "capture_failed",
        }
    rough = _ask_vision_for_coordinates_in_image(full_screenshot, target_description)
    if rough is None:
        return {
            "success": False,
            "message": f"Vision could not produce even a rough guess for '{target_description}'.",
            "tier": None,
            "error": "no_vision_guess",
        }

    scale = _screen_scale_factor()
    center_x, center_y = rough[0] / scale, rough[1] / scale
    candidates = _generate_grid_candidates(center_x, center_y, _GRID_SEARCH_SPACING)[:_GRID_SEARCH_MAX_ATTEMPTS]

    # A wide region around the rough guess, snapshotted once up front - used
    # after every failed candidate to check for accidental navigation,
    # independent of which candidate point was actually tried.
    wide_region_center = (center_x, center_y)
    wide_before = capture_region_unverified(
        *wide_region_center, 800.0, 500.0, reason=_DIFF_REGION_CAPTURE_REASON
    )

    state_check = _APP_PLAYER_STATE_CHECKS.get(app_name)
    before_player_state = state_check() if state_check is not None else None

    attempts_log = []
    for i, (x, y) in enumerate(candidates, start=1):
        region_center = (x, y)
        before_region_png = capture_region_unverified(
            *region_center, *_VERIFY_REGION_SIZE, reason=_DIFF_REGION_CAPTURE_REASON
        )

        _dispatch_click(x, y)
        time.sleep(0.3)

        verified, detail = _verify_click_outcome(
            app_name, expected_outcome, before_player_state, before_region_png, region_center
        )
        attempts_log.append(f"attempt {i} at ({x:.0f},{y:.0f}): {detail}")
        logger.info("grid search for %r: %s", target_description, attempts_log[-1])

        if verified:
            return {
                "success": True,
                "message": (
                    f"Grid search found a working click for '{target_description}' on attempt "
                    f"{i}/{len(candidates)} at ({x:.0f}, {y:.0f}); verified: {detail}."
                ),
                "tier": "grid_search",
                "error": None,
            }

        wide_after = capture_region_unverified(
            *wide_region_center, 800.0, 500.0, reason=_DIFF_REGION_CAPTURE_REASON
        )
        wide_diff = _region_pixel_diff_score(wide_before, wide_after)
        if wide_diff > _GRID_SEARCH_NAV_DIFF_THRESHOLD:
            return {
                "success": False,
                "message": (
                    f"Grid search aborted after attempt {i}/{len(candidates)}: a click near "
                    f"({x:.0f}, {y:.0f}) appears to have navigated away from the expected view "
                    f"(wide-region diff score {wide_diff:.2f}), so remaining candidates were not "
                    f"tried. Attempts so far: {'; '.join(attempts_log)}"
                ),
                "tier": "grid_search",
                "error": "grid_search_navigation_detected",
            }

    return {
        "success": False,
        "message": (
            f"Grid search tried {len(candidates)} candidate points for '{target_description}' "
            f"without verifying the expected outcome. Attempts: {'; '.join(attempts_log)}"
        ),
        "tier": "grid_search",
        "error": "grid_search_exhausted",
    }


def _select_all_and_paste(text: str) -> None:
    """Selects all text in whatever field currently has keyboard focus, then
    replaces it via clipboard paste (Cmd+V), not per-character CGEvents.

    Select-all-first matters: found necessary directly when a field that
    already had leftover text in it (e.g. a relaunched app resuming its
    last search rather than starting blank) ended up with the new text
    silently concatenated onto the old instead of replacing it - exactly
    the kind of silent-wrong-state bug this project is built to catch, just
    this time in a place nothing was checking yet.

    Clipboard-paste, rather than one CGEvent per character, is far more
    reliable: it doesn't depend on mapping every character to a virtual
    keycode (which breaks for anything non-ASCII) and it's a single paste
    event instead of dozens of keystrokes that could be dropped if the
    app's event loop is busy.
    """
    select_all_down = Quartz.CGEventCreateKeyboardEvent(None, 0, True)  # 0 = 'a'
    Quartz.CGEventSetFlags(select_all_down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, select_all_down)
    time.sleep(0.05)
    select_all_up = Quartz.CGEventCreateKeyboardEvent(None, 0, False)
    Quartz.CGEventSetFlags(select_all_up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, select_all_up)
    time.sleep(0.05)

    subprocess.run(["pbcopy"], input=text.encode(), check=True)

    key_down = Quartz.CGEventCreateKeyboardEvent(None, 9, True)  # 9 = 'v'
    Quartz.CGEventSetFlags(key_down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, key_down)
    time.sleep(0.05)
    key_up = Quartz.CGEventCreateKeyboardEvent(None, 9, False)
    Quartz.CGEventSetFlags(key_up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, key_up)


def click_ui(target_description: str, expected_app_name: str, expected_outcome: str) -> dict:
    """Clicks a UI element described in plain language, in a specific app,
    and verifies the click actually produced its intended effect.

    Tries the Accessibility API first; falls back to a screenshot + Gemini
    vision if the element can't be found that way. Refuses to act if the
    named app isn't actually frontmost right now, rather than guessing and
    clicking whatever else happens to be in front.

    Args:
        target_description: Plain-language description of what to click,
            e.g. "search button", "first search result".
        expected_app_name: The app this click is meant for, e.g. "Spotify".
            Must currently be the frontmost app, or the call is refused.
        expected_outcome: Plain-language description of what this click
            should actually cause, e.g. "a lo-fi track starts playing" or
            "the Podcasts filter becomes selected". Used to verify the
            click worked - be specific about the real, observable effect,
            not just that "the click succeeds".

    Returns:
        A dict with:
            success: bool, True only if the expected outcome was verified
            message: human-readable summary, including which tier and
                verification path were used
            tier: "accessibility", "vision", or "fixed_offset" if a click was
                dispatched, else None
            error: raw error string if something went wrong, else None
    """
    guard_error = _verify_expected_app_frontmost(expected_app_name)
    if guard_error is not None:
        return guard_error

    # Demo-only simplification for one specific, confirmed-fixed target -
    # see _APP_TOP_RESULT_OFFSET's comment and planning.md for why: vision
    # measurably can't locate "first search result" reliably (0/5 even with
    # a grid search), but Spotify's "Top result" card sits at a fixed,
    # independently-verified position relative to its window. Only takes
    # effect for this narrow description match - anything else still goes
    # through the normal two-tier location below.
    located = None
    if expected_app_name in _APP_TOP_RESULT_OFFSET and _looks_like_top_result(target_description):
        offset_point = _locate_via_window_offset(expected_app_name, _APP_TOP_RESULT_OFFSET[expected_app_name])
        if offset_point is not None:
            located = {
                "x": offset_point[0],
                "y": offset_point[1],
                "tier": "fixed_offset",
                "reveal_expanded": False,
            }

    if located is None:
        located = _locate_element(expected_app_name, target_description)
    if "error" in located:
        return {"success": False, "message": located["error"], "tier": None, "error": located["error"]}

    region_center = (located["x"], located["y"])
    before_region_png = capture_region_unverified(
        *region_center, *_VERIFY_REGION_SIZE, reason=_DIFF_REGION_CAPTURE_REASON
    )
    state_check = _APP_PLAYER_STATE_CHECKS.get(expected_app_name)
    before_player_state = state_check() if state_check is not None else None

    _dispatch_click(located["x"], located["y"])
    time.sleep(0.3)  # give the UI/app state a moment to actually update

    if located["tier"] == "fixed_offset":
        tier_detail = (
            f"used a known, fixed offset from {expected_app_name}'s window frame "
            "(demo-specific simplification for this one target - see planning.md)"
        )
    elif located["tier"] == "accessibility":
        tier_detail = f"matched AX label '{located['matched_label']}' (score {located['match_score']})"
    elif located.get("reveal_expanded"):
        tier_detail = "used Gemini vision - first click revealed a collapsed field, then vision re-located it"
    else:
        tier_detail = "used Gemini vision on a screenshot"

    # Don't report success just because the click was dispatched without
    # erroring - confirm it actually produced expected_outcome. This is
    # exactly the check that was missing when this tool once reported
    # success clicking Spotify's "first search result play button" while
    # the previously-playing track kept playing unchanged.
    verified, verify_detail = _verify_click_outcome(
        expected_app_name, expected_outcome, before_player_state, before_region_png, region_center
    )

    if not verified:
        return {
            "success": False,
            "message": (
                f"Clicked '{target_description}' via {located['tier']} tier ({tier_detail}) at "
                f"({located['x']:.0f}, {located['y']:.0f}), but could not verify the expected "
                f"outcome: {verify_detail}."
            ),
            "tier": located["tier"],
            "error": "click_outcome_not_verified",
        }

    return {
        "success": True,
        "message": (
            f"Clicked '{target_description}' via {located['tier']} tier ({tier_detail}) at "
            f"({located['x']:.0f}, {located['y']:.0f}); verified: {verify_detail}."
        ),
        "tier": located["tier"],
        "error": None,
    }


def type_in_field(target_description: str, text: str, expected_app_name: str) -> dict:
    """Clicks a text field described in plain language (same two-tier
    location strategy as click_ui), then types the given text into it.
    Refuses to act if the named app isn't actually frontmost right now.

    Args:
        target_description: Plain-language description of the field to type
            into, e.g. "search box".
        text: The text to type.
        expected_app_name: The app this applies to, e.g. "Spotify". Must
            currently be the frontmost app, or the call is refused.

    Returns:
        A dict with:
            success: bool
            message: human-readable summary, including which tier was used
            tier: "accessibility" or "vision" if typing was attempted, else None
            error: raw error string if something went wrong, else None
    """
    guard_error = _verify_expected_app_frontmost(expected_app_name)
    if guard_error is not None:
        return guard_error

    # If this app has a known keyboard shortcut for the kind of field we're
    # after, use it instead of clicking - see _APP_SEARCH_SHORTCUTS for why.
    # The shortcut opens the field already focused, so no click is needed.
    shortcut = _APP_SEARCH_SHORTCUTS.get(expected_app_name)
    used_shortcut = shortcut is not None and _looks_collapsible(target_description)

    if used_shortcut:
        _dispatch_keyboard_shortcut(*shortcut)
        time.sleep(0.4)  # let the now-expanded search UI finish rendering

    # We still need an approximate location to build the before/after
    # verification region below. Re-running the vision zoom search for that
    # purpose was measured to be just as unreliable as locating the
    # original icon - it guessed everywhere from mid-window to the window's
    # bottom edge, nowhere near the actual search bar. A window's own
    # AX-reported frame doesn't depend on vision at all, so prefer it when
    # we have a known offset for this app.
    located = None
    if used_shortcut:
        field_offset = _APP_SEARCH_FIELD_OFFSET.get(expected_app_name)
        window_frame = None
        if field_offset:
            # Right after a fresh open_app relaunch, the process can report
            # itself frontmost (via the System Events check the guard above
            # uses) slightly before its AX window is actually queryable -
            # measured directly: this returned None immediately after a
            # cold-started Spotify in some runs and not others, purely
            # timing-dependent. A few short retries absorbs that race
            # without a long fixed sleep on every call.
            for _ in range(3):
                window_frame = get_frontmost_window_frame(expected_app_name)
                if window_frame is not None:
                    break
                time.sleep(0.2)
        if window_frame is not None:
            win_x, win_y, win_w, _win_h = window_frame
            x_fraction, y_offset = field_offset
            located = {
                "x": win_x + win_w * x_fraction,
                "y": win_y + y_offset,
                "tier": "window_frame",
                "reveal_expanded": False,
            }

    if located is None:
        # skip_reveal=True when used_shortcut, since the field is already
        # open - an extra speculative reveal-click here would land blind on
        # the now-open UI instead of the collapsed icon it's meant for.
        located = _locate_element(
            expected_app_name, target_description, roles={"AXTextField", "AXSearchField"}, skip_reveal=used_shortcut
        )
        if "error" in located:
            # Text fields are frequently mislabeled/unlabeled even in apps
            # with otherwise decent AX trees (we saw this with Spotify's
            # search box), so if a role-restricted AX search fails, retry
            # without the role filter before giving up to vision - a
            # field's fuzzy-matched label is still useful signal even if
            # its AXRole wasn't what we expected.
            located = _locate_element(expected_app_name, target_description, skip_reveal=used_shortcut)
    if "error" in located:
        return {"success": False, "message": located["error"], "tier": None, "error": located["error"]}

    # Captured before the click/paste so _verify_text_entered can diff
    # against it afterward - this is what actually catches a click that
    # missed its target, rather than trusting a single after-the-fact vision
    # opinion in isolation.
    region_center = (located["x"], located["y"])
    before_region_png = capture_region_unverified(
        *region_center, *_VERIFY_REGION_SIZE, reason=_DIFF_REGION_CAPTURE_REASON
    )

    if not used_shortcut:
        _dispatch_click(located["x"], located["y"])
        time.sleep(0.2)  # give the field a moment to actually gain focus

    _select_all_and_paste(text)

    if used_shortcut:
        # Pasting into Spotify's search field only populates the live
        # autocomplete dropdown - it does not by itself navigate to the
        # full results page (with the "Top result" card click_ui's
        # fixed-offset path targets). Found necessary directly: without
        # this, a screenshot taken immediately after the paste showed only
        # the dropdown, with nothing resembling a results page at all -
        # the caller would then be clicking blind at a page that doesn't
        # exist yet. Pressing Return is the natural completion of "search
        # for X" for this shortcut-opened field specifically, so it's
        # done here rather than left for a caller that has no tool to do
        # it itself.
        time.sleep(0.15)
        return_down = Quartz.CGEventCreateKeyboardEvent(None, 36, True)  # 36 = Return
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, return_down)
        time.sleep(0.05)
        return_up = Quartz.CGEventCreateKeyboardEvent(None, 36, False)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, return_up)
        time.sleep(0.6)  # let the results page finish rendering

    tier_label = "keyboard_shortcut" if used_shortcut else located["tier"]
    if used_shortcut:
        # located["tier"] can still be "vision" here if the window-frame
        # lookup (with its retries) genuinely didn't resolve in time and
        # _locate_element's fallback ran instead - say so plainly rather
        # than always claiming the window-frame anchor was used.
        region_source = (
            "the app's own window frame"
            if located["tier"] == "window_frame"
            else f"a fallback {located['tier']} lookup, since the window frame wasn't available in time"
        )
        tier_detail = (
            f"opened via {expected_app_name}'s keyboard shortcut instead of clicking a small icon; "
            f"verification region anchored on {region_source}"
        )
    elif located["tier"] == "accessibility":
        tier_detail = f"matched AX label '{located['matched_label']}' (score {located['match_score']})"
    elif located.get("reveal_expanded"):
        tier_detail = "used Gemini vision - first click revealed a collapsed field, then vision re-located it"
    else:
        tier_detail = "used Gemini vision on a screenshot"

    # Don't report success just because the paste command didn't error -
    # confirm the text is actually there. This is exactly the check that was
    # missing when this tool once reported success on Spotify while "lo-fi"
    # had actually landed nowhere (the vision-located click missed).
    time.sleep(0.3)  # give the UI a moment to render the paste before checking
    verified, verify_detail = _verify_text_entered(expected_app_name, text, before_region_png, region_center)

    if not verified:
        return {
            "success": False,
            "message": (
                f"Typed into '{target_description}' via {tier_label} ({tier_detail}), "
                f"but could not verify the text actually landed: {verify_detail}."
            ),
            "tier": tier_label,
            "error": "type_not_verified",
        }

    return {
        "success": True,
        "message": (
            f"Typed into '{target_description}' via {tier_label} ({tier_detail}); "
            f"verified: {verify_detail}."
        ),
        "tier": tier_label,
        "error": None,
    }


click_ui_tool = FunctionTool(click_ui)
type_in_field_tool = FunctionTool(type_in_field)
