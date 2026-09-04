"""Real, live checks for the setup screen - is Jarvis actually usable right
now, not just "did the process start."

A cold-start user currently hits seven scattered, silent failure points
with no guidance if any of them are missing: an invalid/missing
GOOGLE_API_KEY, the backend not running, no microphone permission, no
Accessibility permission, no per-app Automation permission, no Screen
Recording permission, or the Chrome extension not connected. Each check
here answers one of those honestly - "passed", "failed" with a real reason
and a real fix, or (for the ones macOS genuinely doesn't expose a
proactive answer for) "unknown, will be confirmed the first time it's
actually needed."

Every check does the real thing - a real Gemini call, a real
AXIsProcessTrusted()/CGPreflightScreenCaptureAccess() query, a real
minimal AppleScript probe - never a guess based on whether an env var is
merely present or a permission was merely requested at some point.
"""

from __future__ import annotations

import os
import subprocess

try:
    import ApplicationServices as AS
    import Quartz
    from AppKit import NSWorkspace
except ImportError:
    # Not macOS - see perception.py's identical guard. Cloud Run's agent
    # pipeline imports this module transitively; every function below
    # degrades to an honest "can't check here" rather than crashing.
    AS = None
    Quartz = None
    NSWorkspace = None

# System Settings deep links - verified directly against this machine's
# real System Settings (macOS 15/Sequoia-era "System Settings", not the
# older "System Preferences" app), not assumed from documentation: each
# URL was opened for real and screenshotted to confirm it lands on the
# exact right pane (Privacy & Security > <Section>), not just something
# with a similar name (macOS has a *separate*, unrelated top-level
# "Accessibility" pane for assistive-use features, distinct from Privacy &
# Security's Accessibility *permissions* list - this URL scheme reliably
# reaches the permissions list specifically). See planning.md.
_SETTINGS_MICROPHONE = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
_SETTINGS_ACCESSIBILITY = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
_SETTINGS_AUTOMATION = "x-apple.systempreferences:com.apple.preference.security?Privacy_Automation"
_SETTINGS_SCREEN_RECORDING = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"


def _result(check_id: str, label: str, status: str, detail: str, fix_url: str | None = None) -> dict:
    """status is one of "passed" | "failed" | "unknown" - "unknown" is a
    real, distinct outcome (see check_automation below), not a synonym for
    failure: it means this specific fact genuinely cannot be determined
    proactively on macOS, and will only be confirmed the first time it's
    actually needed."""
    return {"id": check_id, "label": label, "status": status, "detail": detail, "fix_url": fix_url}


def check_google_api_key() -> dict:
    """A real, minimal Gemini call (models.list() - a cheap metadata read,
    not a generation call) - not just "is the env var set". Confirmed
    directly: an invalid key fails this in ~0.1s with a structured
    google.genai.errors.ClientError (code 400, reason API_KEY_INVALID);
    a real key returns real model metadata in ~0.2s."""
    key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not key:
        return _result(
            "google_api_key",
            "Gemini API key",
            "failed",
            "GOOGLE_API_KEY is not set. Add it to the repo-root .env file (get a key at aistudio.google.com/apikey).",
        )

    from google import genai
    from google.genai import errors as genai_errors

    try:
        client = genai.Client(api_key=key)
        list(client.models.list())
    except genai_errors.ClientError as exc:
        return _result(
            "google_api_key",
            "Gemini API key",
            "failed",
            f"GOOGLE_API_KEY is set but Gemini rejected it: {exc.message if hasattr(exc, 'message') else exc}",
        )
    except Exception as exc:  # noqa: BLE001 - network error, etc. - still an honest failure, not a crash
        return _result(
            "google_api_key",
            "Gemini API key",
            "failed",
            f"Could not reach Gemini to verify the key: {exc}",
        )
    return _result("google_api_key", "Gemini API key", "passed", "A real Gemini call succeeded with this key.")


def check_accessibility() -> dict:
    """AXIsProcessTrusted() - the real, documented, proactive API for
    exactly this - no attempted click needed. This is what click_ui's
    CGEvent-based dispatch (mac_control.py) needs to actually work."""
    if AS is None:
        return _result("accessibility", "Accessibility", "unknown", "Not running on macOS - cannot check.")
    trusted = bool(AS.AXIsProcessTrusted())
    if trusted:
        return _result("accessibility", "Accessibility", "passed", "This process is Accessibility-trusted.")
    return _result(
        "accessibility",
        "Accessibility",
        "failed",
        "Not granted - clicking/typing into apps (click_ui, type_in_field) will not work until this is on.",
        fix_url=_SETTINGS_ACCESSIBILITY,
    )


def check_screen_recording() -> dict:
    """CGPreflightScreenCaptureAccess() - real, documented (macOS 10.15+),
    proactive, and confirmed directly to return the real current state
    without prompting or capturing anything."""
    if Quartz is None or not hasattr(Quartz, "CGPreflightScreenCaptureAccess"):
        return _result("screen_recording", "Screen Recording", "unknown", "Not available on this platform.")
    granted = bool(Quartz.CGPreflightScreenCaptureAccess())
    if granted:
        return _result("screen_recording", "Screen Recording", "passed", "Screen capture access is granted.")
    return _result(
        "screen_recording",
        "Screen Recording",
        "failed",
        "Not granted - reading what's on screen (capture_screenshot, vision fallback) will not work until this is on.",
        fix_url=_SETTINGS_SCREEN_RECORDING,
    )


# Automation permission has no public, proactive query API on macOS -
# confirmed by design, not assumed: Apple exposes no equivalent of
# AXIsProcessTrusted()/CGPreflightScreenCaptureAccess() for Apple Events
# authorization. The only real signal is a live AppleScript attempt,
# which either succeeds, or fails with a documented error (-1743 / "not
# authorized"). This project already had to build that exact detection
# once, reactively, for create_reminder's own error handling
# (mac_control.py) - this reuses the same real error signature.
#
# A `tell application "X"` command launches X if it isn't already running
# (confirmed directly, repeatedly, elsewhere in this project) - for a
# background helper like System Events that has no visible window, that's
# a non-issue, but for a real GUI app (Reminders, Spotify, Google Chrome)
# it would mean this setup screen silently popping open apps the user
# never asked for, just to run a permission check. So: System Events is
# always probed directly; Reminders/Spotify/Chrome are only probed if
# NSWorkspace shows them already running (a real check requiring no
# permission at all - confirmed directly) - otherwise this honestly
# reports "not running, will be confirmed when you actually use it"
# rather than forcing a launch or guessing.
_AUTOMATION_TARGETS = [
    ("automation_system_events", "Automation - System Events", "System Events", True),
    ("automation_reminders", "Automation - Reminders", "Reminders", False),
    ("automation_spotify", "Automation - Spotify", "Spotify", False),
    ("automation_chrome", "Automation - Google Chrome", "Google Chrome", False),
]


def _is_app_running(app_name: str) -> bool:
    if NSWorkspace is None:
        return False
    names = {str(a.localizedName()) for a in NSWorkspace.sharedWorkspace().runningApplications()}
    return app_name in names


def _probe_automation(app_name: str) -> dict:
    """A minimal, read-only AppleScript request - `get name`, nothing that
    creates/changes/sends anything - interpreted as the real Automation
    permission signal."""
    script = f'tell application "{app_name}" to get name'
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "detail": f"Timed out asking {app_name} - could not confirm Automation access."}
    if result.returncode == 0:
        return {"status": "passed", "detail": f"Automation access to {app_name} is granted."}
    stderr = result.stderr.strip()
    if "-1743" in stderr or "not authorized" in stderr.lower():
        return {
            "status": "failed",
            "detail": f"Automation access to {app_name} was denied - Jarvis cannot control it until this is granted.",
        }
    return {"status": "failed", "detail": f"Could not confirm Automation access to {app_name}: {stderr or 'unknown error'}"}


def check_automation(check_id: str, label: str, app_name: str, always_probe: bool) -> dict:
    if AS is None:
        return _result(check_id, label, "unknown", "Not running on macOS - cannot check.")
    if not always_probe and not _is_app_running(app_name):
        return _result(
            check_id,
            label,
            "unknown",
            f"{app_name} isn't currently open, so this can't be checked proactively - macOS only reveals "
            f"Automation permission via a real attempt, and Jarvis won't launch {app_name} just to check. "
            f"This will be confirmed honestly the first time a real command actually uses it.",
        )
    probe = _probe_automation(app_name)
    return _result(check_id, label, probe["status"], probe["detail"], fix_url=_SETTINGS_AUTOMATION if probe["status"] == "failed" else None)


def all_automation_checks() -> list[dict]:
    return [check_automation(check_id, label, app_name, always_probe) for check_id, label, app_name, always_probe in _AUTOMATION_TARGETS]


def check_browser_extension() -> dict:
    """Reuses the real, existing browser bridge connection state
    (browser/bridge.py) - not a new signal."""
    from browser.bridge import browser_bridge

    if browser_bridge.is_connected():
        name = browser_bridge.extension_name() or "the Jarvis extension"
        return _result("chrome_extension", "Chrome extension", "passed", f"{name} is connected.")
    return _result(
        "chrome_extension",
        "Chrome extension",
        "failed",
        "The Jarvis Chrome extension isn't connected - web-based tasks (Kayak, etc.) won't work until it's loaded "
        "and Chrome is open. Load it via chrome://extensions (Developer Mode, \"Load unpacked\").",
    )


def run_all_checks() -> list[dict]:
    """Every backend-side check, in the order the setup screen shows them.
    Renderer-side checks (backend reachability, this connection existing
    at all, the renderer's own microphone permission) aren't here - they
    can't be, they're facts about a different process - see App.jsx/
    main.js for those."""
    checks = [
        check_google_api_key(),
        check_accessibility(),
        check_screen_recording(),
    ]
    checks.extend(all_automation_checks())
    checks.append(check_browser_extension())
    return checks
