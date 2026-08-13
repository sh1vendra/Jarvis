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
import json
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

from .perception import capture_screenshot, get_ui_tree


def _build_applescript(task: str, due_date: datetime) -> str:
    """Builds the AppleScript source that creates one reminder.

    Notes on the syntax, since AppleScript looks nothing like Python:
    - `tell application "Reminders" ... end tell` scopes the following
      commands to the Reminders app, like a `with` block targeting a specific
      app's scripting dictionary.
    - AppleScript has no native way to construct an arbitrary date from
      numbers in one call, so the idiom is: grab `(current date)` (today,
      right now) and then overwrite its `year`/`month`/`day`/`hours`/
      `minutes`/`seconds` properties one at a time until it represents the
      date/time we actually want.
    - `default list` is Reminders' name for whichever list is set as the
      user's default (usually "Reminders").
    - `make new reminder with properties {...}` is AppleScript's constructor
      pattern: create a new object of a given class with an initial property
      record, similar to calling `Reminder(name=..., due_date=...)`.
    - String values are escaped by doubling any embedded quotes, since
      AppleScript string literals use double quotes with no backslash
      escaping.
    """
    safe_task = task.replace('"', '""')

    return f'''
tell application "Reminders"
    set dueDate to (current date)
    set year of dueDate to {due_date.year}
    set month of dueDate to {due_date.month}
    set day of dueDate to {due_date.day}
    set hours of dueDate to {due_date.hour}
    set minutes of dueDate to {due_date.minute}
    set seconds of dueDate to 0
    tell default list
        make new reminder with properties {{name:"{safe_task}", due date:dueDate}}
    end tell
end tell
return "ok"
'''


def create_reminder(task: str, due_date: str, due_time: str) -> dict:
    """Creates a real reminder in the macOS Reminders app (default list).

    Args:
        task: The reminder text, e.g. "Call mom".
        due_date: A natural-language or ISO date, e.g. "tomorrow", "2026-08-13".
        due_time: A natural-language or clock time, e.g. "5pm", "17:00".

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

    script = _build_applescript(task, parsed)

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
        "message": f"Created reminder '{task}' due {parsed.strftime('%Y-%m-%d %H:%M')}.",
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


def _ask_vision_for_coordinates(target_description: str) -> tuple[float, float] | None:
    """Tier 2: screenshots the whole screen and asks Gemini's vision model
    for the pixel coordinates of the described element."""
    png_bytes = capture_screenshot()

    api_key = os.environ.get("GOOGLE_API_KEY")
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-flash-lite-latest",
        contents=[
            genai_types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
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
        pixel_x, pixel_y = float(coords["x"]), float(coords["y"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None

    scale = _screen_scale_factor()
    return pixel_x / scale, pixel_y / scale


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


def _locate_element(app_name: str, target_description: str, roles: set[str] | None = None) -> dict:
    """Runs the two-tier location strategy and returns a dict describing
    what was found and how: {"x", "y", "tier": "accessibility" | "vision"}
    or {"error": str} if both tiers failed."""
    element, score = _best_ax_match(app_name, target_description, roles)
    if element is not None:
        return {
            "x": element["x"] + element["width"] / 2,
            "y": element["y"] + element["height"] / 2,
            "tier": "accessibility",
            "matched_label": element["label"],
            "match_score": round(score, 2),
        }

    # Tier 1 found nothing above threshold - fall back to vision. (In
    # practice the AX query itself is near-instant, so there's no real ~1.5s
    # wait to insert; the timeout budget is spent on Gemini's vision call
    # instead of an idle sleep.)
    coordinates = _ask_vision_for_coordinates(target_description)
    if coordinates is None:
        return {"error": f"Could not locate '{target_description}' via accessibility or vision."}

    return {"x": coordinates[0], "y": coordinates[1], "tier": "vision"}


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

    tier_detail = (
        f"matched AX label '{located['matched_label']}' (score {located['match_score']})"
        if located["tier"] == "accessibility"
        else "used Gemini vision on a screenshot"
    )
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

    located = _locate_element(expected_app_name, target_description, roles={"AXTextField", "AXSearchField"})
    if "error" in located:
        # Text fields are frequently mislabeled/unlabeled even in apps with
        # otherwise decent AX trees (we saw this with Spotify's search box),
        # so if a role-restricted AX search fails, retry without the role
        # filter before giving up to vision - a field's fuzzy-matched label
        # is still useful signal even if its AXRole wasn't what we expected.
        located = _locate_element(expected_app_name, target_description)
    if "error" in located:
        return {"success": False, "message": located["error"], "tier": None, "error": located["error"]}

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

    tier_detail = (
        f"matched AX label '{located['matched_label']}' (score {located['match_score']})"
        if located["tier"] == "accessibility"
        else "used Gemini vision on a screenshot"
    )
    return {
        "success": True,
        "message": f"Typed into '{target_description}' via {located['tier']} tier ({tier_detail}).",
        "tier": located["tier"],
        "error": None,
    }


click_ui_tool = FunctionTool(click_ui)
type_in_field_tool = FunctionTool(type_in_field)
