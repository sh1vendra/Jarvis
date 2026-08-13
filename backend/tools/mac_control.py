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
import subprocess
import time
from datetime import datetime

import dateparser
import Quartz
from AppKit import NSScreen
from google import genai
from google.adk.tools import FunctionTool
from google.genai import types as genai_types
from PIL import Image, ImageChops

from .perception import capture_region, capture_screenshot, get_field_values, get_frontmost_window_frame, get_ui_tree

logger = logging.getLogger(__name__)


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
    - AppleScript has no native way to construct an arbitrary date from
      numbers in one call, so the idiom is: grab `(current date)` (today,
      right now) and then overwrite its `year`/`month`/`day`/`hours`/
      `minutes`/`seconds` properties one at a time until it represents the
      date/time we actually want.
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
    set dueDate to (current date)
    set year of dueDate to {due_date.year}
    set month of dueDate to {due_date.month}
    set day of dueDate to {due_date.day}
    set hours of dueDate to {due_date.hour}
    set minutes of dueDate to {due_date.minute}
    set seconds of dueDate to 0
    tell list "{safe_list_name}"
        make new reminder with properties {{name:"{safe_task}", due date:dueDate}}
    end tell
end tell
return "ok"
'''


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
            success: bool, whether the reminder was actually created
            message: human-readable summary of what happened
            error: the raw error string if something went wrong, else None
    """
    # dateparser understands both natural language ("tomorrow", "5pm") and
    # ISO-style input, so we don't hand-roll fragile string matching for
    # every phrasing the Planner/Action agent might produce.
    parsed = dateparser.parse(f"{due_date} {due_time}")
    if parsed is None:
        return {
            "success": False,
            "message": f"Could not understand date/time: {due_date!r} {due_time!r}",
            "error": "date_parse_failed",
        }

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

    return {
        "success": True,
        "message": f"Created reminder '{task}' due {parsed.strftime('%Y-%m-%d %H:%M')} in list '{list_name}'.",
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
    target_description: str, max_iterations: int = _MAX_ZOOM_ITERATIONS
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
    whole-screen guess fails.
    """
    full_screenshot = capture_screenshot()
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
_APP_SEARCH_SHORTCUTS = {
    "Spotify": (37, Quartz.kCGEventFlagMaskCommand),
}

# Where the opened field lands, as an offset from its window's own top-left
# corner (x as a fraction of window width, y as an absolute point offset
# from the window's top edge) - confirmed directly by screenshotting right
# after dispatching the shortcut: Spotify's search bar renders near the top
# of its window, spanning most of its width. Not yet used by any caller.
_APP_SEARCH_FIELD_OFFSET = {
    "Spotify": (0.5, 65.0),
}


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
    coordinates = _locate_via_vision_zoom(target_description)
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
        refined = _locate_via_vision_zoom(target_description)
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


_VERIFY_REGION_SIZE = (400.0, 160.0)  # width, height in points, centered on the field
_NO_CHANGE_DIFF_THRESHOLD = 2.0  # mean 0-255 grayscale diff below this = "nothing visibly happened"


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
    after_region_png = capture_region(x, y, width, height)
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


def click_ui(target_description: str, expected_app_name: str) -> dict:
    """Clicks a UI element described in plain language, in a specific app.

    Tries the Accessibility API first; falls back to a screenshot + Gemini
    vision if the element can't be found that way. Refuses to act if the
    named app isn't actually frontmost right now, rather than guessing and
    clicking whatever else happens to be in front.

    Args:
        target_description: Plain-language description of what to click,
            e.g. "search button", "first search result".
        expected_app_name: The app this click is meant for, e.g. "Spotify".
            Must currently be the frontmost app, or the call is refused.

    Returns:
        A dict with:
            success: bool
            message: human-readable summary, including which tier was used
            tier: "accessibility" or "vision" if a click was dispatched, else None
            error: raw error string if something went wrong, else None
    """
    guard_error = _verify_expected_app_frontmost(expected_app_name)
    if guard_error is not None:
        return guard_error

    located = _locate_element(expected_app_name, target_description)
    if "error" in located:
        return {"success": False, "message": located["error"], "tier": None, "error": located["error"]}

    _dispatch_click(located["x"], located["y"])

    if located["tier"] == "accessibility":
        tier_detail = f"matched AX label '{located['matched_label']}' (score {located['match_score']})"
    elif located.get("reveal_expanded"):
        tier_detail = "used Gemini vision - first click revealed a collapsed field, then vision re-located it"
    else:
        tier_detail = "used Gemini vision on a screenshot"

    return {
        "success": True,
        "message": f"Clicked '{target_description}' via {located['tier']} tier ({tier_detail}) at ({located['x']:.0f}, {located['y']:.0f}).",
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
    before_region_png = capture_region(*region_center, *_VERIFY_REGION_SIZE)

    if not used_shortcut:
        _dispatch_click(located["x"], located["y"])
        time.sleep(0.2)  # give the field a moment to actually gain focus

    # Typing via the clipboard (pbcopy + Cmd+V) is far more reliable than
    # synthesizing one CGEvent per character: it doesn't depend on mapping
    # every character to a virtual keycode (which breaks for anything
    # non-ASCII) and it's a single paste event instead of dozens of
    # keystrokes that could be dropped if the app's event loop is busy.
    subprocess.run(["pbcopy"], input=text.encode(), check=True)

    key_down = Quartz.CGEventCreateKeyboardEvent(None, 9, True)  # 9 = 'v'
    Quartz.CGEventSetFlags(key_down, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, key_down)
    time.sleep(0.05)
    key_up = Quartz.CGEventCreateKeyboardEvent(None, 9, False)
    Quartz.CGEventSetFlags(key_up, Quartz.kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, key_up)

    tier_label = "keyboard_shortcut" if used_shortcut else located["tier"]
    if used_shortcut:
        tier_detail = (
            f"opened via {expected_app_name}'s keyboard shortcut instead of clicking a small icon; "
            f"verification region anchored on the app's own window frame ({located['tier']})"
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
