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

import html
import re
import subprocess
from datetime import timedelta

from google.adk.tools import FunctionTool

from .mac_control import _applescript_date_lines, _parse_datetime, _require_macos

_DEFAULT_CALENDAR = "Jarvis Test"
_DEFAULT_NOTES_ACCOUNT = "iCloud"
_DEFAULT_NOTES_FOLDER = "Jarvis Test"


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


# --- create_note -------------------------------------------------------------

_NOTE_TITLE_MAX_LEN = 60


def _derive_note_title(content: str) -> str:
    """Notes derives a note's *displayed* name from its body's first line
    when no explicit `name` property is given (confirmed directly) - but
    create_note always sets `name` explicitly instead of relying on that,
    so a note's title is deterministic. When the caller doesn't supply one,
    this derives the same kind of title Notes itself would: the content's
    first line, capped to a sane length."""
    first_line = content.strip().splitlines()[0] if content.strip() else "Untitled Note"
    if len(first_line) > _NOTE_TITLE_MAX_LEN:
        first_line = first_line[:_NOTE_TITLE_MAX_LEN].rstrip() + "…"
    return first_line


def _notes_body_html(title: str, content: str) -> str:
    """Builds Notes' HTML body: title as the first line, content below it,
    real newlines converted to `<br>`. Content is HTML-escaped first since
    Notes' `body` property is real HTML, not plain text - confirmed
    directly this matters: an unescaped '<' or '&' in the note's own
    content would otherwise be interpreted as markup instead of shown
    literally."""
    escaped_title = html.escape(title)
    escaped_content = html.escape(content).replace("\n", "<br>")
    return f"{escaped_title}<br>{escaped_content}"


def _build_note_applescript(title: str, content: str, account: str, folder: str) -> str:
    safe_account = account.replace('"', '""')
    safe_folder = folder.replace('"', '""')
    safe_title = title.replace('"', '""')
    body_html = _notes_body_html(title, content).replace('"', '""')
    return f'''
tell application "Notes"
    tell account "{safe_account}"
        if not (exists folder "{safe_folder}") then
            make new folder with properties {{name:"{safe_folder}"}}
        end if
        tell folder "{safe_folder}"
            make new note with properties {{name:"{safe_title}", body:"{body_html}"}}
        end tell
    end tell
end tell
return "ok"
'''


def _verify_note_exists(title: str, content: str, account: str, folder: str) -> bool:
    """Real read-back: queries the note's own rendered `plaintext` (Notes'
    computed plain-text view of its HTML body, confirmed directly) and
    checks the actual content is present in it - stronger than a title-only
    match, since a stale note with a coincidentally matching title would
    fail this check on content."""
    safe_account = account.replace('"', '""')
    safe_folder = folder.replace('"', '""')
    safe_title = title.replace('"', '""')
    script = f'''
tell application "Notes"
    tell account "{safe_account}"
        tell folder "{safe_folder}"
            try
                return plaintext of note "{safe_title}"
            on error
                return ""
            end try
        end tell
    end tell
end tell
'''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return False
    content_stripped = content.strip()
    return bool(content_stripped) and content_stripped in result.stdout


def create_note(content: str, title: str | None = None, account: str = _DEFAULT_NOTES_ACCOUNT, folder: str = _DEFAULT_NOTES_FOLDER) -> dict:
    """Creates a real note in the macOS Notes app.

    Args:
        content: The note's body text.
        title: The note's title. If omitted, one is derived from content's
            first line (the same way Notes itself would, if left to infer
            it).
        account: Which Notes account to use. Defaults to "iCloud" (this
            Mac's default Notes account).
        folder: Which folder to create it in. Defaults to "Jarvis Test"
            (created automatically if it doesn't exist yet) so automated/
            test notes stay out of the user's real folders - same
            reasoning as create_reminder's "Jarvis Test" list.

    Returns:
        A dict with:
            success: bool, True only if the note was independently
                confirmed to exist (by title) with the given content
                actually present (by reading its rendered text back) -
                not just that the creation command didn't error
            message: human-readable summary of what happened
            error: the raw error string if something went wrong, else None
    """
    _require_macos()
    real_title = title.strip() if title and title.strip() else _derive_note_title(content)

    script = _build_note_applescript(real_title, content, account, folder)
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "message": "osascript timed out - Notes may be waiting on a permission dialog.",
            "error": str(exc),
        }

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "-1743" in stderr or "not authorized" in stderr.lower():
            return {
                "success": False,
                "message": (
                    "macOS denied automation access to Notes. Grant it under "
                    "System Settings -> Privacy & Security -> Automation, then "
                    "allow this process to control Notes, and try again."
                ),
                "error": stderr,
            }
        return {"success": False, "message": "osascript failed while creating the note.", "error": stderr}

    if not _verify_note_exists(real_title, content, account, folder):
        return {
            "success": False,
            "message": (
                f"osascript reported no error creating note '{real_title}' but its content could "
                f"not be confirmed on read-back in folder '{folder}' - treating this as not "
                "actually created."
            ),
            "error": "verify_failed",
        }

    return {
        "success": True,
        "message": f"Created note '{real_title}' in folder '{folder}', confirmed by reading its content back.",
        "error": None,
    }


create_note_tool = FunctionTool(create_note)


# --- send_message ------------------------------------------------------------
#
# Genuinely different from create_calendar_event/create_note above: this
# reaches a real person, not a local, private, trivially-reversible item -
# which is exactly why the Planner marks a milestone that resolves to this
# tool requires_approval=True (see agents/planner.py), the same way it
# already does for Kayak's final search-submit step. This is deliberately
# the second real demonstration of that same approval gate, not a special
# case invented for it.
#
# Two real investigation findings shaped this function's scope, both
# confirmed directly (not assumed) before writing it - see planning.md:
#
# 1. Recipient resolution is deliberately narrow: an exact phone number or
#    exact email only, never a name. Real existing chats on this machine
#    use exactly that shape (e.g. "+15126659036"). There is no reliable
#    public API this project found for resolving an ambiguous name to one
#    specific real contact with confidence - guessing wrong here sends a
#    real message to the wrong real person, so this scopes narrower rather
#    than guessing, exactly per the standing "tell me honestly, don't ship
#    something unreliable for a feature that contacts real people" rule.
# 2. Verification is honestly weaker than the other three tools in this
#    module. Messages' AppleScript dictionary exposes no way to read a
#    chat's message content back (a `chat`'s own `properties` are only
#    id/account/name/class - confirmed directly), and `exists buddy "..."`
#    was confirmed to return true even for an obviously-invalid string, so
#    there is no proactive validity check either. The only stronger check
#    available - reading ~/Library/Messages/chat.db directly - needs Full
#    Disk Access, a far broader permission than anything else this project
#    asks for; confirmed directly this process is blocked from opening
#    that file at all without it. Deliberately not taken on here (a real
#    decision, not an oversight - see planning.md). So `success: True`
#    below means "the recipient passed strict format validation and the
#    AppleScript send command completed with no error" - it does NOT mean
#    confirmed delivery, which nothing available to this process can
#    honestly confirm.

# Matches a phone number after stripping common formatting punctuation
# (spaces, dashes, parens, dots) - "+17373359167", "737-335-9167", and
# "7373359167" all normalize to a run of 8-15 digits, optionally preceded
# by "+". Deliberately strict: this is the ONLY thing standing between a
# real send and Messages' own confirmed-silent no-op for a garbage
# recipient (see the module docstring above), since neither `exists buddy`
# nor `send`'s own exit code catches that.
_PHONE_PATTERN = re.compile(r"^\+?[1-9]\d{7,14}$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _looks_like_valid_recipient(recipient: str) -> bool:
    cleaned = recipient.strip()
    digits_only = re.sub(r"[\s\-().]", "", cleaned)
    return bool(_PHONE_PATTERN.match(digits_only) or _EMAIL_PATTERN.match(cleaned))


def _build_message_applescript(recipient: str, text: str) -> str:
    safe_recipient = recipient.replace('"', '""')
    safe_text = text.replace('"', '""')
    return f'tell application "Messages" to send "{safe_text}" to buddy "{safe_recipient}"'


def send_message(recipient: str, text: str) -> dict:
    """Sends a real iMessage/SMS via the macOS Messages app.

    This is a CONSEQUENTIAL action - it reaches a real person - and every
    milestone that resolves to it must be marked requires_approval=True by
    the Planner, so a real plan pauses for real user approval before this
    ever runs (see agents/planner.py). Never call this as anything but the
    already-approved step of a plan.

    Recipient resolution is deliberately narrow: `recipient` must be an
    exact phone number (any common formatting - "+17373359167",
    "737-335-9167", "7373359167" are all accepted; digits are what's
    matched) or an exact email address. There is NO fuzzy contact-name
    lookup - "text mom" or "message John" will not resolve to a real
    contact here. If the caller only has a name, ask the user for the
    exact number/email instead of guessing - see this module's own
    docstring for why guessing was ruled out rather than attempted.

    Verification is honestly limited - see this module's docstring for the
    full investigation. `success: True` means the recipient passed strict
    format validation and the AppleScript send command completed with no
    error. It does NOT confirm the message actually reached a real device;
    nothing available to this process can honestly confirm that.

    Args:
        recipient: An exact phone number or email address - never a name.
        text: The message text to send.

    Returns:
        A dict with:
            success: bool - see the honest verification limits above
            message: human-readable summary
            error: raw error string if something went wrong, else None
    """
    _require_macos()
    if not _looks_like_valid_recipient(recipient):
        return {
            "success": False,
            "message": (
                f"{recipient!r} doesn't look like a real phone number or email address. "
                "send_message only accepts an exact phone number or email, never a name - "
                "ask the user for the exact contact info instead of guessing."
            ),
            "error": "invalid_recipient_format",
        }

    script = _build_message_applescript(recipient, text)
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "message": "osascript timed out - Messages may be waiting on a permission dialog.",
            "error": str(exc),
        }

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "-1743" in stderr or "not authorized" in stderr.lower():
            return {
                "success": False,
                "message": (
                    "macOS denied automation access to Messages. Grant it under "
                    "System Settings -> Privacy & Security -> Automation, then "
                    "allow this process to control Messages, and try again."
                ),
                "error": stderr,
            }
        return {"success": False, "message": f"osascript failed while sending the message: {stderr}", "error": stderr}

    return {
        "success": True,
        "message": (
            f"Sent to {recipient} via Messages (recipient format validated, AppleScript reported "
            "no error). This does not confirm real delivery - Messages' scripting API exposes no "
            "way to read that back without Full Disk Access, which this project deliberately does "
            "not use (see planning.md)."
        ),
        "error": None,
    }


send_message_tool = FunctionTool(send_message)
