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

import subprocess
from datetime import datetime

import dateparser
from google.adk.tools import FunctionTool


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
