"""Tools for the native Mac apps beyond Reminders/Spotify: Calendar, Notes,
and Messages. Each follows the exact rig create_reminder proved
(mac_control.py): parse the request, build a small AppleScript, run it via
osascript, then independently query the app's own real state back to
confirm the change actually landed - never trusting the AppleScript
creation command's own successful exit as proof by itself.

Calendar and Notes route new items into a dedicated "Jarvis Test"
destination (a calendar, and a Notes folder) - the same reasoning
create_reminder's "Jarvis Test" list already established: automated/test
items should never land in the user's real primary calendar or real Notes
folders. Messages has no such destination to isolate into - see
send_message's own docstring for why it's scoped narrower instead, and
planning.md for the full investigation behind that scope decision.
"""

import subprocess
from datetime import timedelta

from google.adk.tools import FunctionTool

from .mac_control import _applescript_date_lines, _parse_datetime, _require_macos

_DEFAULT_CALENDAR = "Jarvis Test"


# --- create_calendar_event --------------------------------------------------


def _build_calendar_applescript(title: str, start, end, calendar_name: str) -> str:
    """Same shape as mac_control._build_applescript: get-or-create the
    named calendar first (a dedicated "Jarvis Test" calendar by default, so
    automated/test events never land in the user's real Home/Work/synced
    calendars - confirmed directly this machine's real calendars are named
    exactly that, none of them a sensible default to write test data into),
    then create the event inside it."""
    safe_title = title.replace('"', '""')
    safe_cal = calendar_name.replace('"', '""')
    return f'''
tell application "Calendar"
    if not (exists calendar "{safe_cal}") then
        make new calendar with properties {{name:"{safe_cal}"}}
    end if
    tell calendar "{safe_cal}"
        {_applescript_date_lines("startDate", start)}
        {_applescript_date_lines("endDate", end)}
        make new event with properties {{summary:"{safe_title}", start date:startDate, end date:endDate}}
    end tell
end tell
return "ok"
'''


def _verify_calendar_event(title: str, start, end, calendar_name: str) -> bool:
    """Real read-back, same pattern as _verify_reminder_exists: a fresh
    osascript query against Calendar's own live object model, matching on
    title AND both dates, rather than trusting the creation script's exit
    code."""
    safe_title = title.replace('"', '""')
    safe_cal = calendar_name.replace('"', '""')
    script = f'''
tell application "Calendar"
    tell calendar "{safe_cal}"
        {_applescript_date_lines("startDate", start)}
        {_applescript_date_lines("endDate", end)}
        return count of (every event whose summary is "{safe_title}" and start date is startDate and end date is endDate)
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


def create_calendar_event(
    title: str,
    event_date: str,
    event_time: str,
    duration_minutes: int = 60,
    calendar_name: str = _DEFAULT_CALENDAR,
) -> dict:
    """Creates a real event in the macOS Calendar app.

    Args:
        title: The event's title, e.g. "Dentist appointment".
        event_date: A natural-language or ISO date, e.g. "tomorrow", "2026-08-13".
        event_time: A natural-language or clock time, e.g. "5pm", "17:00".
        duration_minutes: How long the event lasts. Defaults to 60.
        calendar_name: Which calendar to create it in. Defaults to
            "Jarvis Test" (created automatically if it doesn't exist yet) so
            automated/test events stay out of the user's real calendars -
            same reasoning as create_reminder's "Jarvis Test" list.

    Returns:
        A dict with:
            success: bool, True only if the event was independently
                confirmed to exist by querying Calendar back afterward -
                not just that the creation command didn't error
            message: human-readable summary of what happened
            error: the raw error string if something went wrong, else None
    """
    _require_macos()
    start, err = _parse_datetime(event_date, event_time)
    if start is None:
        return {"success": False, "message": err, "error": "date_parse_failed"}
    end = start + timedelta(minutes=duration_minutes)

    script = _build_calendar_applescript(title, start, end, calendar_name)
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "message": "osascript timed out - Calendar may be waiting on a permission dialog.",
            "error": str(exc),
        }

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "-1743" in stderr or "not authorized" in stderr.lower():
            return {
                "success": False,
                "message": (
                    "macOS denied automation access to Calendar. Grant it under "
                    "System Settings -> Privacy & Security -> Automation, then "
                    "allow this process to control Calendar, and try again."
                ),
                "error": stderr,
            }
        return {"success": False, "message": "osascript failed while creating the calendar event.", "error": stderr}

    if not _verify_calendar_event(title, start, end, calendar_name):
        return {
            "success": False,
            "message": (
                f"osascript reported no error creating '{title}' but it could not be found on "
                f"read-back in calendar '{calendar_name}' - treating this as not actually created."
            ),
            "error": "verify_failed",
        }

    return {
        "success": True,
        "message": (
            f"Created event '{title}' from {start.strftime('%Y-%m-%d %H:%M')} to "
            f"{end.strftime('%H:%M')} in calendar '{calendar_name}', confirmed by reading it back."
        ),
        "error": None,
    }


create_calendar_event_tool = FunctionTool(create_calendar_event)
