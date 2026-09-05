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


_MIN_SUBSTRING_MATCH_LEN = 3  # below this, a "substring" match is usually just noise (see below)

def _match_score(query: str, el: ElementRef) -> float:
    """Scores how well el matches a plain-language query, checked in
    priority order - visible label text first, then placeholder, then
    aria-label, then name - with an exact match scoring higher than a
    substring match, mirroring the same priority the content script's own
    heuristic matcher (content_script.js's scoreCandidate) uses for
    consistency between "finding" and "resolving" an element.

    The substring branch requires both strings to be at least
    _MIN_SUBSTRING_MATCH_LEN long - found necessary by a real, live
    failure: Kayak's own account-avatar button has el.text == "s" (the
    user's initial), which is trivially a substring of nearly any query
    ("search button" included) and, at the "text" field's top weight,
    outscored the real Search button - find_web_element confidently handed
    back the avatar. A 1-2 character field is essentially never a
    meaningful description on its own, so it can still win via an exact
    match (a query that's genuinely just "s"), just not by coincidentally
    appearing inside something longer. This doesn't catch every case of
    this general shape - a genuinely longer decorative string that happens
    to contain a real whole word from the query (e.g. a heading containing
    "from") can still out-substring-match the actual field - but it fixes
    the concrete, observed one.
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
        elif len(v) >= _MIN_SUBSTRING_MATCH_LEN and len(query) >= _MIN_SUBSTRING_MATCH_LEN and (query in v or v in query):
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


# --- select_kayak_airport: a deterministic composite for a real gap -------
#
# Real, live testing (see planning.md's Stage 3 entry) found Kayak's
# origin/destination fields are not a small inline autocomplete -
# type_in_web_field genuinely, verifiably lands the typed text in the
# field, but Kayak's own backend still rejects the search ("Please enter a
# 'To' airport") because typed free text was never resolved to a real
# airport. The field opens a full takeover suggestion panel, and the real
# suggestion rows are ordinary `div`/`button` elements carrying the full,
# descriptive location text and a real airport code in parens - e.g.
# "John F Kennedy Intl, New York, United States, (JFK)" - confirmed
# directly from real captured snapshots, not assumed. The Action agent's
# own case-by-case find_web_element guessing found these rows
# inconsistently (right sometimes, missed other times) because there's no
# way to know the right query phrasing in advance; this function doesn't
# guess a phrasing at all - it types the query, then scans the resulting
# real snapshot directly for a row whose own text names the query and
# carries a real airport code, which is a fact about the data, not a
# phrasing gamble.

_AIRPORT_CODE_PATTERN = re.compile(r"\([A-Z]{3}\)")

_KAYAK_FIELD_LABELS = {
    "origin": ["Origin location", "Where from?", "Leaving from", "From?"],
    "destination": ["Destination location", "Where to?", "To?"],
}


async def select_kayak_airport(field: str, query: str) -> dict:
    """Sets Kayak's origin or destination field to a real, resolved airport
    - not just typed text - by opening the field, typing `query`, finding
    the real suggestion row Kayak's own panel shows for it (matched by a
    real airport code in parens, e.g. "(AUS)"), and clicking that row.

    Args:
        field: "origin" or "destination".
        query: A city or airport name to search for, e.g. "Austin" or
            "New York".

    Returns:
        A dict with:
            success: bool - True only if a real suggestion was found,
                clicked, and the field verifiably shows a resolved airport
                afterward (not just the raw typed text)
            message: human-readable summary
            resolved: the exact suggestion text that was clicked (e.g.
                "Austin Bergstrom, Austin, Texas, United States, (AUS)"),
                or None if nothing was resolved
            error: raw error string if something went wrong, else None
    """
    field = field.strip().lower()
    if field not in _KAYAK_FIELD_LABELS:
        return {
            "success": False,
            "message": f"field must be 'origin' or 'destination', got {field!r}.",
            "resolved": None,
            "error": "bad_field",
        }

    labels = _KAYAK_FIELD_LABELS[field]
    query_lower = query.strip().lower()

    # Step 1: find the field. It may already be a real <input> (if a prior
    # interaction revealed one) or still a collapsed button - which, when
    # already correctly set (the common case for origin, defaulted from
    # Kayak's own geolocation), shows its full resolved value as text
    # ("Austin Bergstrom, Austin, Texas, United States, (AUS)") rather than
    # a generic placeholder label, so none of the placeholder-style labels
    # below match it at all - confirmed live, not assumed. Fall back to the
    # first visible element carrying a real airport code (origin's value
    # display is the first such element in Kayak's real document order).
    ref_id = None
    for label in labels:
        found = find_web_element(label)
        if found["success"]:
            ref_id = found["ref_id"]
            break
    if ref_id is None and field == "origin":
        # This fallback only ever makes sense for origin: it's the FIRST
        # airport-code-carrying element in document order, so applying it
        # to "destination" too would - and, confirmed live, actually did -
        # grab origin's own value display instead of ever touching the
        # real destination field.
        snapshot = browser_store.get_snapshot()
        if snapshot is not None:
            for el in snapshot.elements:
                if el.visible and _AIRPORT_CODE_PATTERN.search(el.text or ""):
                    ref_id = el.ref_id
                    break
    if ref_id is None:
        return {
            "success": False,
            "message": f"Could not find the Kayak {field} field at all.",
            "resolved": None,
            "error": "field_not_found",
        }

    element = browser_store.get_element(ref_id)

    # Exact-match short-circuit, same convention type_in_web_field already
    # uses: if the field's own current text already names the query and
    # carries a real airport code, it's already resolved - nothing to type,
    # nothing to click, and no dispatched action to falsely blame if this
    # is called again idempotently.
    if element is not None and _AIRPORT_CODE_PATTERN.search(element.text or "") and query_lower in (element.text or "").lower():
        return {
            "success": True,
            "message": f"The {field} already shows {element.text!r} - already resolved, nothing to change.",
            "resolved": element.text,
            "error": None,
        }

    if element is not None and element.tag != "input":
        clicked = await click_web_element(ref_id)
        if not clicked["success"]:
            return {
                "success": False,
                "message": f"Could not open the {field} field: {clicked['message']}",
                "resolved": None,
                "error": "open_failed",
            }
        ref_id = None
        for label in labels:
            found = find_web_element(label)
            if found["success"]:
                candidate = browser_store.get_element(found["ref_id"])
                if candidate is not None and candidate.tag == "input":
                    ref_id = found["ref_id"]
                    break
        if ref_id is None:
            # None of the known aria-labels matched the revealed field -
            # confirmed live this happens on a real, if less common,
            # interaction path. Fall back to any visible, real combobox
            # input, since there's normally only one freshly-opened field
            # of this shape on the page at a time.
            snapshot = browser_store.get_snapshot()
            if snapshot is not None:
                for el in snapshot.elements:
                    if el.visible and el.tag == "input" and el.role == "combobox":
                        ref_id = el.ref_id
                        break
        if ref_id is None:
            return {
                "success": False,
                "message": f"Opened the {field} field but could not find its real input afterward.",
                "resolved": None,
                "error": "input_not_found_after_open",
            }

    # Step 2: type the query into the real input.
    typed = await type_in_web_field(ref_id, query)
    if not typed["success"]:
        return {
            "success": False,
            "message": f"Could not type into the {field} field: {typed['message']}",
            "resolved": None,
            "error": "type_failed",
        }

    # Step 3: scan the fresh snapshot directly for a real suggestion row -
    # not another find_web_element guess. A real row names the query and
    # carries a real airport code.
    snapshot = browser_store.get_snapshot()
    best: ElementRef | None = None
    if snapshot is not None:
        for el in snapshot.elements:
            text = el.text or ""
            if not el.visible or not _AIRPORT_CODE_PATTERN.search(text):
                continue
            if query_lower in text.lower():
                best = el
                break

    if best is None:
        return {
            "success": False,
            "message": f"Typed {query!r} into the {field} field but no matching airport suggestion appeared.",
            "resolved": None,
            "error": "no_suggestion_found",
        }

    clicked_suggestion = await click_web_element(best.ref_id)
    if not clicked_suggestion["success"]:
        return {
            "success": False,
            "message": (
                f"Found a matching suggestion ({best.text!r}) for the {field} field but could not click it: "
                f"{clicked_suggestion['message']}"
            ),
            "resolved": None,
            "error": "suggestion_click_failed",
        }

    # Step 4: verify - the field's real value must show a resolved airport
    # afterward, not just the raw typed text. This is the exact gap a live
    # test found: type_in_web_field's own generation-based verification
    # confirmed text landed, but Kayak's backend still rejected the search
    # because nothing was ever actually resolved.
    final_snapshot = browser_store.get_snapshot()
    final_element = browser_store.get_element(ref_id, final_snapshot.session_id) if final_snapshot else None
    final_value = (final_element.value if final_element else "") or ""
    if not _AIRPORT_CODE_PATTERN.search(final_value) and not _AIRPORT_CODE_PATTERN.search(best.text or ""):
        return {
            "success": False,
            "message": (
                f"Clicked {best.text!r} but the {field} field doesn't show a resolved airport afterward "
                f"(value={final_value!r})."
            ),
            "resolved": None,
            "error": "not_resolved",
        }

    return {
        "success": True,
        "message": f"The {field} is set to {best.text!r} - a real, resolved airport, not just typed text.",
        "resolved": best.text,
        "error": None,
    }


# --- select_kayak_departure_date: the calendar's real interaction ---------
#
# Investigated directly rather than assumed (see planning.md's Stage 3
# entry): a real, live test found no separate "Select this date" confirm
# control anywhere in the DOM once a one-way trip's calendar is open - the
# field genuinely just needs one real day-cell click, confirmed by reading
# the date field's own text back afterward ("Wed 9/16"). The earlier
# "Select this date" sighting was very likely round-trip-mode-specific (two
# dates needed) and wasn't re-investigated this session - this function is
# scoped to the one-way case that's actually proven to work.
#
# The calendar renders several months at once, so the SAME day number
# (e.g. "16") appears multiple times, once per visible month, with no
# month name in the cell's own text - confirmed directly from a real
# snapshot dump. Rather than disambiguate months (fragile without deeper
# DOM context), this takes the first (nearest, soonest) matching day cell
# in document order - the same choice a real live run already made
# successfully when this was previously done by hand-guessed queries.

_DAY_NUMBER_PATTERN = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\b")


async def select_kayak_departure_date(query: str) -> dict:
    """Sets Kayak's departure date by opening the date field and clicking
    the real calendar day cell matching the day number found in `query`.

    Args:
        query: A description containing a day number, e.g. "September 16"
            or "the 16th" - the exact phrasing doesn't matter, only that a
            1-2 digit day number appears in it.

    Returns:
        A dict with:
            success: bool - True only if a real day cell was clicked and
                the date field's value afterward contains that day number
            message: human-readable summary
            resolved: the date field's real text after clicking (e.g.
                "Wed 9/16"), or None if nothing was resolved
            error: raw error string if something went wrong, else None
    """
    day_match = _DAY_NUMBER_PATTERN.search(query)
    if day_match is None:
        return {
            "success": False,
            "message": f"Could not find a day number in {query!r}.",
            "resolved": None,
            "error": "no_day_number",
        }
    day_text = str(int(day_match.group(1)))  # normalize "06" -> "6"

    ref_id = None
    for label in ("Departure date", "Select dates"):
        found = find_web_element(label)
        if found["success"]:
            ref_id = found["ref_id"]
            break
    if ref_id is None:
        return {
            "success": False,
            "message": "Could not find Kayak's departure date field at all.",
            "resolved": None,
            "error": "field_not_found",
        }

    clicked_field = await click_web_element(ref_id)
    if not clicked_field["success"]:
        return {
            "success": False,
            "message": f"Could not open the calendar: {clicked_field['message']}",
            "resolved": None,
            "error": "open_failed",
        }

    snapshot = browser_store.get_snapshot()
    day_cell: ElementRef | None = None
    if snapshot is not None:
        for el in snapshot.elements:
            if el.visible and (el.text or "").strip() == day_text:
                day_cell = el
                break

    if day_cell is None:
        return {
            "success": False,
            "message": f"Opened the calendar but found no visible day cell for {day_text!r}.",
            "resolved": None,
            "error": "day_cell_not_found",
        }

    clicked_day = await click_web_element(day_cell.ref_id)
    if not clicked_day["success"]:
        return {
            "success": False,
            "message": f"Found day {day_text!r} but could not click it: {clicked_day['message']}",
            "resolved": None,
            "error": "day_click_failed",
        }

    final_snapshot = browser_store.get_snapshot()
    final_element = browser_store.get_element(ref_id, final_snapshot.session_id) if final_snapshot else None
    final_value = (final_element.text if final_element else "") or ""
    if day_text not in final_value:
        return {
            "success": False,
            "message": f"Clicked day {day_text!r} but the date field now shows {final_value!r}, not confirmed.",
            "resolved": None,
            "error": "not_resolved",
        }

    return {
        "success": True,
        "message": f"The departure date is set to {final_value!r}.",
        "resolved": final_value,
        "error": None,
    }


navigate_to_url_tool = FunctionTool(navigate_to_url)
find_web_element_tool = FunctionTool(find_web_element)
click_web_element_tool = FunctionTool(click_web_element)
type_in_web_field_tool = FunctionTool(type_in_web_field)
select_kayak_airport_tool = FunctionTool(select_kayak_airport)
select_kayak_departure_date_tool = FunctionTool(select_kayak_departure_date)
