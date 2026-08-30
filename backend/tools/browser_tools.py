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

from google.adk.tools import FunctionTool

from browser.bridge import browser_bridge
from browser.models import ActionRequest, ElementRef
from browser.store import browser_store

_RESULT_TIMEOUT = 10.0
_SETTLE_TIMEOUT = 5.0


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


find_web_element_tool = FunctionTool(find_web_element)
click_web_element_tool = FunctionTool(click_web_element)
type_in_web_field_tool = FunctionTool(type_in_web_field)
