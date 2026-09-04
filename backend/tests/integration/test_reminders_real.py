"""A real integration test against the real macOS Reminders app.

No mocking of any kind - this is exactly the case the project's own
standard ("real results, not assumed") asks for: create_reminder's
success:true is only meaningful if it's backed by something that would
show a real failure honestly too. Lands in the same "Jarvis Test" list
every other real Reminders testing in this project has used throughout
its build, to stay out of the user's real lists.

Requires: a real Mac, the Reminders app, and (the first time) having
already granted this process Automation access to Reminders under System
Settings -> Privacy & Security -> Automation.
"""

import subprocess

import pytest

from tools.mac_control import create_reminder

pytestmark = pytest.mark.integration

_TEST_LIST = "Jarvis Test"


def _reminder_exists(task: str) -> bool:
    """Reads Reminders' own real state back via AppleScript - not trusting
    create_reminder's own report of success, the same "don't trust the
    self-report" principle the rest of this project runs on."""
    script = f'''
    tell application "Reminders"
        set matchCount to 0
        if exists list "{_TEST_LIST}" then
            tell list "{_TEST_LIST}"
                set matchCount to count of (reminders whose name is "{task}")
            end tell
        end if
        return matchCount
    end tell
    '''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    return result.returncode == 0 and result.stdout.strip() not in ("0", "")


def _delete_reminder(task: str) -> None:
    script = f'''
    tell application "Reminders"
        if exists list "{_TEST_LIST}" then
            tell list "{_TEST_LIST}"
                delete (reminders whose name is "{task}")
            end tell
        end if
    end tell
    '''
    subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)


def test_create_reminder_actually_creates_a_real_reminder():
    task = "pytest integration check - safe to delete"
    _delete_reminder(task)  # clean slate, in case a previous failed run left one behind
    assert not _reminder_exists(task)

    try:
        result = create_reminder(task=task, due_date="tomorrow", due_time="9am", list_name=_TEST_LIST)

        assert result["success"] is True
        assert result["error"] is None
        # The real, load-bearing assertion: Reminders' own state, not
        # create_reminder's report of it.
        assert _reminder_exists(task)
    finally:
        _delete_reminder(task)
        assert not _reminder_exists(task)


def test_create_reminder_reports_honest_failure_for_an_unparseable_date():
    """The other half of the same principle: a real failure (a date/time
    dateparser genuinely can't parse) must be reported as success:false,
    not silently swallowed - and must not create anything."""
    task = "pytest integration check - should never be created"
    _delete_reminder(task)

    result = create_reminder(task=task, due_date="not a real date at all", due_time="also not a time", list_name=_TEST_LIST)

    assert result["success"] is False
    assert result["error"] == "date_parse_failed"
    assert not _reminder_exists(task)
