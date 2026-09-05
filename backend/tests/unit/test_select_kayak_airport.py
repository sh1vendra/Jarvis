"""tools.browser_tools.select_kayak_airport / select_kayak_departure_date -
the deterministic composites built for Stage 3's real, live-tested gap:
typing into Kayak's origin/destination fields lands the text (type_in_web_
field's own verification confirms that much), but Kayak's backend still
rejects the search because nothing was ever resolved to a real airport.

Only the paths that return before ever needing a real bridge connection
are covered here (the exact-match short-circuit, and bad input) - see
tests/README.md's unit/integration boundary reasoning: the rest (opening a
field, clicking a real suggestion, verifying a resolved value) was tested
for real, live, against the actual Kayak site (see planning.md's Stage 3
entry) rather than faked here, since mocking the bridge/extension deeply
enough to mean anything would just test the mock.
"""

import pytest

from browser.models import ElementRef, PageSnapshot
from browser.store import browser_store
from tools.browser_tools import select_kayak_airport, select_kayak_departure_date


def _seed_snapshot(elements: list[ElementRef]) -> None:
    browser_store.upsert_snapshot(
        PageSnapshot(session_id="test-session", tab_id="1", url="https://www.kayak.com/", elements=elements)
    )


@pytest.mark.asyncio
async def test_bad_field_name_is_rejected_before_touching_anything():
    ok = await select_kayak_airport("waypoint", "Austin")
    assert ok["success"] is False
    assert ok["error"] == "bad_field"


@pytest.mark.asyncio
async def test_origin_already_resolved_short_circuits_with_no_action_dispatched():
    """The real, common case: Kayak's own geolocation default already shows
    the right origin as full resolved text, not a generic placeholder -
    confirmed live this needs a short-circuit or find_web_element's own
    placeholder-style queries never match it at all."""
    _seed_snapshot(
        [
            ElementRef(
                ref_id="jw_1",
                generation=1,
                tag="div",
                role="button",
                text="Austin Bergstrom, Austin, Texas, United States, (AUS)",
                visible=True,
            )
        ]
    )
    result = await select_kayak_airport("origin", "Austin")
    assert result["success"] is True
    assert "(AUS)" in result["resolved"]
    assert "already resolved" in result["message"]


@pytest.mark.asyncio
async def test_destination_does_not_short_circuit_on_origins_own_value():
    """The real bug this pins down: a fallback meant only for origin's own
    value display must never fire for destination too - a live test found
    it grabbing origin's "(AUS)" button and never touching the real
    destination field at all. With only origin's value in the snapshot (no
    placeholder-labeled destination field either), asking for destination
    must fail honestly, not silently resolve to origin's own airport."""
    _seed_snapshot(
        [
            ElementRef(
                ref_id="jw_1",
                generation=1,
                tag="div",
                role="button",
                text="Austin Bergstrom, Austin, Texas, United States, (AUS)",
                visible=True,
            )
        ]
    )
    result = await select_kayak_airport("destination", "New York")
    assert result["success"] is False
    assert result["error"] == "field_not_found"


@pytest.mark.asyncio
async def test_destination_already_resolved_short_circuits_too():
    _seed_snapshot(
        [
            ElementRef(
                ref_id="jw_2",
                generation=1,
                tag="input",
                role="combobox",
                text="John F Kennedy Intl, New York, United States, (JFK)",
                aria_label="Destination location",
                visible=True,
            )
        ]
    )
    result = await select_kayak_airport("destination", "New York")
    assert result["success"] is True
    assert "(JFK)" in result["resolved"]


@pytest.mark.asyncio
async def test_departure_date_rejects_a_query_with_no_day_number():
    result = await select_kayak_departure_date("next Friday")
    assert result["success"] is False
    assert result["error"] == "no_day_number"


@pytest.mark.asyncio
async def test_departure_date_extracts_the_day_from_an_ordinal():
    """Real bug this pins down: "16th" was never matched by a plain
    \\b(\\d{1,2})\\b pattern, since "th" immediately follows the digits
    with no intervening word boundary - a live run needed the Action agent
    to notice the failure and retry with a bare "16" before it worked."""
    result = await select_kayak_departure_date("no such field exists here anyway")
    assert result["error"] == "no_day_number"
    # A field_not_found (not no_day_number) proves "16th" was parsed fine -
    # it just found nothing on this deliberately empty snapshot.
    _seed_snapshot([])
    result = await select_kayak_departure_date("September 16th")
    assert result["error"] == "field_not_found"
