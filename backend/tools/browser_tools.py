"""Tools that control the browser via the Chrome extension bridge
(browser/bridge.py, browser/store.py, servers/browser_bridge_server.py).

Unlike tools/mac_control.py's click_ui/type_in_field, which have to verify
their own outcome via screenshots and vision because macOS gives no
structured view into an app's UI, these tools get a real, honest
verification signal for free: the bridge's generation-based settlement.
An action only counts as done once a *newer* page snapshot has actually
arrived (browser_bridge.wait_for_snapshot) confirming the DOM changed -
not a fixed sleep, not trusting the extension's own "ok: true", and for
type_in_web_field, not even trusting that the DOM changed *at all* is
enough - the freshly-snapshotted field's actual value is checked too, the
same "read the real state back" principle mac_control.py's AX-value check
uses, just via the DOM's own state instead of the Accessibility API's.

ADK's FunctionTool supports async callables directly (it checks
inspect.iscoroutinefunction and awaits accordingly), so click_web_element/
type_in_web_field are real `async def` functions that await the bridge's
asyncio.Event-based waiting rather than needing to fake synchronicity.
"""

import re
import subprocess
import time
from urllib.parse import urlparse

from google.adk.tools import FunctionTool

from browser.bridge import browser_bridge
from browser.models import ActionRequest, ElementRef
from browser.store import browser_store
from tools.mac_control import _frontmost_app_name

_RESULT_TIMEOUT = 10.0
_SETTLE_TIMEOUT = 5.0

_CHROME_APP = "Google Chrome"
# kayak.com and sites like it are heavy - the tab has to load AND the
# content script has to build and push its first snapshot before we can
# confirm anything. This budget is for that whole sequence, including a
# cold Chrome launch and the extension's service worker reconnecting.
_NAV_SETTLE_TIMEOUT = 30.0


def _normalize_url(url: str) -> tuple[str, str]:
    """Returns (full_url_with_scheme, bare_host). Host has a leading
    'www.' stripped so 'kayak.com' and 'www.kayak.com' compare equal."""
    target = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", target):
        target = "https://" + target
    host = urlparse(target).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return target, host


async def navigate_to_url(url: str) -> dict:
    """Opens Google Chrome (launching it if it isn't running) and loads
    `url`, then verifies for real that it worked - not just that the launch
    command exited cleanly.

    Verification has two independent signals, both required:
      1. Chrome is confirmed the frontmost app (fresh System Events query,
         same check open_app uses).
      2. The browser bridge has received a page snapshot whose own URL is
         on `url`'s host - which only happens if the page actually loaded
         AND the Jarvis extension's content script is live on it. That
         single snapshot is also what find_web_element needs next, so this
         tool leaves the browser in exactly the state the following
         milestones expect.

    Use this as the first step of any task that acts on a website, so the
    user never has to pre-open or pre-navigate the browser by hand.

    Args:
        url: The address to open, e.g. "https://www.kayak.com" or just
            "kayak.com" (https:// is added if no scheme is given).

    Returns:
        A dict with:
            success: bool, True only if both signals above checked out
            message: human-readable summary
            url: the confirming snapshot's actual URL, if verified
            generation: that snapshot's generation, if verified
            error: raw error string if something went wrong, else None
    """
    target, host = _normalize_url(url)
    if not host:
        return {
            "success": False,
            "message": f"Could not parse a host out of {url!r}.",
            "url": None,
            "generation": None,
            "error": "bad_url",
        }

    pre_generation = browser_store.current_generation()

    launch = subprocess.run(
        ["open", "-a", _CHROME_APP, target],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if launch.returncode != 0:
        return {
            "success": False,
            "message": f"Could not open Chrome with {target!r} - the 'open' command itself failed.",
            "url": None,
            "generation": None,
            "error": launch.stderr.strip() or f"open exited with code {launch.returncode}",
        }

    # `open -a` starts/loads reliably but doesn't always steal focus - the
    # same thing open_app works around. Re-send `activate` on each poll
    # until Chrome is really frontmost or the budget runs out.
    frontmost_deadline = time.monotonic() + 8.0
    frontmost_ok = False
    last_seen = None
    while time.monotonic() < frontmost_deadline:
        subprocess.run(
            ["osascript", "-e", f'tell application "{_CHROME_APP}" to activate'],
            capture_output=True,
            text=True,
            timeout=15,
        )
        last_seen = _frontmost_app_name()
        if last_seen and last_seen.lower() == _CHROME_APP.lower():
            frontmost_ok = True
            break
        time.sleep(0.15)

    # Real verification: wait for a bridge snapshot that's both newer than
    # anything we had before AND on the target host. A newer-but-wrong-host
    # snapshot (e.g. the tab that was already open) just raises the floor
    # and we keep waiting.
    confirming = None
    settle_deadline = time.monotonic() + _NAV_SETTLE_TIMEOUT
    while time.monotonic() < settle_deadline:
        remaining = settle_deadline - time.monotonic()
        snap = await browser_bridge.wait_for_snapshot(min_generation=pre_generation, timeout=max(0.5, remaining))
        if snap is None:
            break
        _, snap_host = _normalize_url(snap.url)
        if host == snap_host or host in snap_host or snap_host in host:
            confirming = snap
            break
        pre_generation = max(pre_generation, snap.generation)

    if confirming is None:
        if not browser_bridge.is_connected():
            detail = "the browser bridge is not connected - is the Jarvis Chrome extension loaded and enabled?"
        else:
            detail = f"no page snapshot from {host!r} arrived within {_NAV_SETTLE_TIMEOUT:.0f}s"
        frontmost_note = "Chrome is frontmost" if frontmost_ok else f"frontmost app is {last_seen!r}, not Chrome"
        return {
            "success": False,
            "message": f"Chrome was told to open {target!r} ({frontmost_note}), but {detail}, so navigation is unverified.",
            "url": None,
            "generation": None,
            "error": "nav_not_verified",
        }

    return {
        "success": True,
        "message": (
            f"Chrome is open at {confirming.url!r} (frontmost confirmed) and the Jarvis extension has a "
            f"live snapshot of it (generation {confirming.generation})."
        ),
        "url": confirming.url,
        "generation": confirming.generation,
        "error": None,
    }


def _match_score(query: str, el: ElementRef) -> float:
    """Scores how well el matches a plain-language query, checked in
    priority order - visible label text first, then placeholder, then
    aria-label, then name - with an exact match scoring higher than a
    substring match, mirroring the same priority the content script's own
    heuristic matcher (content_script.js's scoreCandidate) uses for
    consistency between "finding" and "resolving" an element.
    """
    fields_in_priority = [
        (el.text, 4.0),
        (el.placeholder, 3.0),
        (el.aria_label, 2.0),
        (el.name, 1.0),
    ]
    best = 0.0
    for value, weight in fields_in_priority:
        v = (value or "").strip().lower()
        if not v:
            continue
        if v == query:
            best = max(best, weight * 2.0)
        elif query in v or v in query:
            best = max(best, weight * 1.0)
    return best


def find_web_element(description: str) -> dict:
    """Searches the most recent browser page snapshot for an element
    matching a plain-language description.

    This reads the backend's already-stored snapshot directly - it does
    not round-trip to the browser extension, since every snapshot already
    carries full label/placeholder/aria-label/name metadata for every
    tagged element.

    Args:
        description: Plain-language description of the element to find,
            e.g. "the destination field" or "Where to?".

    Returns:
        A dict with:
            success: bool
            message: human-readable summary
            ref_id: the matched element's stable ref_id (e.g. "jw_12"),
                or None if nothing matched - pass this to
                click_web_element/type_in_web_field
            label: the matched element's primary label, for confirmation
            error: raw error string if something went wrong, else None
    """
    snapshot = browser_store.get_snapshot()
    if snapshot is None:
        return {
            "success": False,
            "message": "No page snapshot available yet - is the browser extension connected and a page loaded?",
            "ref_id": None,
            "label": None,
            "error": "no_snapshot",
        }

    query = description.strip().lower()
    if not query:
        return {"success": False, "message": "Empty description.", "ref_id": None, "label": None, "error": "empty_query"}

    best: ElementRef | None = None
    best_score = 0.0
    for el in snapshot.elements:
        score = _match_score(query, el)
        if score > best_score:
            best, best_score = el, score

    if best is None or best_score <= 0:
        return {
            "success": False,
            "message": f"No element matching {description!r} found in the current page snapshot ({len(snapshot.elements)} elements).",
            "ref_id": None,
            "label": None,
            "error": "no_match",
        }

    return {
        "success": True,
        "message": f"Found element matching {description!r}: {best.primary_label()!r} ({best.tag}, role={best.role or 'none'}).",
        "ref_id": best.ref_id,
        "label": best.primary_label(),
        "error": None,
    }


async def click_web_element(ref_id: str) -> dict:
    """Clicks a web page element by its stable ref_id (from
    find_web_element), and verifies the click actually did something -
    not just that the extension reported success - by waiting for a
    genuinely newer page snapshot (strictly greater generation) to arrive
    after the click.

    Args:
        ref_id: The element's stable ref_id, e.g. "jw_12".

    Returns:
        A dict with:
            success: bool, True only if a newer snapshot confirmed settlement
            message: human-readable summary
            generation: the confirming snapshot's generation, if successful
            error: raw error string if something went wrong, else None
    """
    queued = browser_bridge.queue_action(ActionRequest(action="click", ref_id=ref_id))
    if not queued.ok:
        return {"success": False, "message": queued.message, "generation": None, "error": "queue_failed"}

    action_result = await browser_bridge.wait_for_result(queued.action_id, timeout=_RESULT_TIMEOUT)
    if action_result is None:
        return {
            "success": False,
            "message": f"No result from the browser extension within {_RESULT_TIMEOUT}s for click on {ref_id}.",
            "generation": None,
            "error": "result_timeout",
        }
    if not action_result.ok:
        return {"success": False, "message": action_result.message, "generation": None, "error": "click_failed"}

    newer_snapshot = await browser_bridge.wait_for_snapshot(min_generation=queued.pre_generation, timeout=_SETTLE_TIMEOUT)
    if newer_snapshot is None or newer_snapshot.generation <= queued.pre_generation:
        return {
            "success": False,
            "message": (
                f"Click on {ref_id} was dispatched and the extension reported success, but no newer page "
                f"snapshot arrived within {_SETTLE_TIMEOUT}s to confirm the DOM actually changed "
                f"(pre_generation={queued.pre_generation})."
            ),
            "generation": None,
            "error": "no_newer_snapshot",
        }

    return {
        "success": True,
        "message": (
            f"Clicked {ref_id} ({action_result.message}); confirmed via a newer page snapshot "
            f"(generation {newer_snapshot.generation}, was {queued.pre_generation})."
        ),
        "generation": newer_snapshot.generation,
        "error": None,
    }


async def type_in_web_field(ref_id: str, text: str) -> dict:
    """Types text into a web page field by its stable ref_id (from
    find_web_element), and verifies the text actually landed - not just
    that the extension reported success and not just that *some* DOM
    change happened - by reading the field's real value back from a fresh
    snapshot taken after the action.

    Args:
        ref_id: The field's stable ref_id, e.g. "jw_12".
        text: The text to type.

    Returns:
        A dict with:
            success: bool, True only if the field's real post-action value
                was confirmed to contain the typed text
            message: human-readable summary
            generation: the confirming snapshot's generation, if successful
            error: raw error string if something went wrong, else None
    """
    # If the field already holds the target text, there's nothing to type
    # and nothing that dispatching a type action could newly verify -
    # found necessary directly: retyping an already-correct value doesn't
    # reliably produce a fresh snapshot (nothing observably changes, so
    # the content script's MutationObserver batch threshold may never be
    # crossed), which made a real, already-correct field report a false
    # *failure* (`no_newer_snapshot`) rather than the true state. Checking
    # the current value first and treating an exact match as an immediate
    # success sidesteps that entirely, rather than trying to make
    # generation-based verification more lenient in a way that would also
    # weaken it for the case it actually exists to catch.
    current_snapshot = browser_store.get_snapshot()
    if current_snapshot is not None:
        current_element = browser_store.get_element(ref_id, current_snapshot.session_id)
        if current_element is not None and text.strip().lower() in (current_element.value or "").lower():
            return {
                "success": True,
                "message": (
                    f"{ref_id} already contains {text!r} (value={current_element.value!r}) - "
                    "nothing to type, no action dispatched."
                ),
                "generation": current_snapshot.generation,
                "error": None,
            }

    queued = browser_bridge.queue_action(ActionRequest(action="type", ref_id=ref_id, text=text))
    if not queued.ok:
        return {"success": False, "message": queued.message, "generation": None, "error": "queue_failed"}

    action_result = await browser_bridge.wait_for_result(queued.action_id, timeout=_RESULT_TIMEOUT)
    if action_result is None:
        return {
            "success": False,
            "message": f"No result from the browser extension within {_RESULT_TIMEOUT}s for typing into {ref_id}.",
            "generation": None,
            "error": "result_timeout",
        }
    if not action_result.ok:
        return {"success": False, "message": action_result.message, "generation": None, "error": "type_failed"}

    newer_snapshot = await browser_bridge.wait_for_snapshot(min_generation=queued.pre_generation, timeout=_SETTLE_TIMEOUT)
    if newer_snapshot is None or newer_snapshot.generation <= queued.pre_generation:
        return {
            "success": False,
            "message": (
                f"Typed into {ref_id} and the extension reported success, but no newer page snapshot "
                f"arrived within {_SETTLE_TIMEOUT}s to confirm - not verified."
            ),
            "generation": None,
            "error": "no_newer_snapshot",
        }

    # Don't stop at "a newer snapshot exists" - read the field's actual
    # value back from it and confirm the typed text is really there.
    element = browser_store.get_element(ref_id, newer_snapshot.session_id)
    field_value = element.value if element else ""
    if text.strip().lower() not in field_value.lower():
        return {
            "success": False,
            "message": (
                f"Typed into {ref_id}; a newer snapshot arrived (generation {newer_snapshot.generation}), but "
                f"the field's actual value ({field_value!r}) does not contain the typed text ({text!r})."
            ),
            "generation": newer_snapshot.generation,
            "error": "value_not_verified",
        }

    return {
        "success": True,
        "message": (
            f"Typed {text!r} into {ref_id}; verified via snapshot generation {newer_snapshot.generation} - "
            f"field value is now {field_value!r}."
        ),
        "generation": newer_snapshot.generation,
        "error": None,
    }


navigate_to_url_tool = FunctionTool(navigate_to_url)
find_web_element_tool = FunctionTool(find_web_element)
click_web_element_tool = FunctionTool(click_web_element)
type_in_web_field_tool = FunctionTool(type_in_web_field)
