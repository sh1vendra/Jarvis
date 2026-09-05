"""main.py's flight-slot resolution logic - the deterministic gap-check
that decides "genuinely needs a clarifying question" vs. "has a real
default, just proceed", never an LLM judgment call (see
agents/flight_slots.py's own docstring and planning.md for why).

Worth its own dedicated, fast tests for the same reason
test_spotify_ambiguity.py exists: a real behavioral guarantee (never ask
about something already known; always ask about something genuinely
missing; silently use a stored preference when one applies) is only
actually true if the code implementing it is right, and a live end-to-end
run costing a real Gemini call is the wrong tool for pinning down the
exact boundary cases (a stated return_date implying round_trip, a
resolved one-way trip never asking for a return date, etc.).
"""

from main import _build_flight_clarifying_question, _resolve_flight_slots
from agents.flight_slots import FlightSlots


def test_fully_specified_request_has_no_gaps(isolated_memory_store):
    resolved, missing, defaulted = _resolve_flight_slots(
        FlightSlots(destination="Denver", origin="Austin", depart_date="next Friday", trip_type="one_way")
    )
    assert missing == []
    assert defaulted == {}
    assert resolved == {
        "destination": "Denver",
        "origin": "Austin",
        "depart_date": "next Friday",
        "trip_type": "one_way",
    }


def test_fully_underspecified_request_is_missing_everything_except_destination(isolated_memory_store):
    resolved, missing, defaulted = _resolve_flight_slots(FlightSlots(destination="New York"))
    assert missing == ["origin", "depart_date", "trip_type"]
    assert defaulted == {}
    assert resolved == {"destination": "New York"}


def test_combined_question_names_every_real_gap_in_one_sentence(isolated_memory_store):
    question = _build_flight_clarifying_question(["origin", "depart_date", "trip_type"])
    assert "where you're flying from" in question
    assert "what date you'd like to leave" in question
    assert "whether it's one-way or round-trip" in question
    # One combined question, not three - a real, deliberate v1 scope
    # decision (see planning.md), not an accident of string formatting.
    assert question.count("?") == 1


def test_single_missing_slot_still_reads_naturally(isolated_memory_store):
    question = _build_flight_clarifying_question(["depart_date"])
    assert question == "I need a bit more to search that - can you tell me what date you'd like to leave?"


def test_a_stored_preference_silently_fills_a_real_gap(isolated_memory_store):
    from memory import store as memory_store

    memory_store.set_preference("default_flight_destination", "Denver, Colorado")
    resolved, missing, defaulted = _resolve_flight_slots(
        FlightSlots(origin="Austin", depart_date="next Friday", trip_type="one_way")
    )
    assert missing == [], "a stored preference should have covered the only real gap"
    assert defaulted == {"destination": "Denver, Colorado"}
    assert resolved["destination"] == "Denver, Colorado"


def test_a_stored_preference_never_overrides_a_stated_value(isolated_memory_store):
    from memory import store as memory_store

    memory_store.set_preference("default_flight_destination", "Denver, Colorado")
    resolved, missing, defaulted = _resolve_flight_slots(
        FlightSlots(destination="Miami", origin="Austin", depart_date="next Friday", trip_type="one_way")
    )
    assert resolved["destination"] == "Miami", "the stated destination must win over any stored default"
    assert defaulted == {}


def test_no_preference_and_nothing_stated_is_a_real_gap_not_silently_ignored(isolated_memory_store):
    resolved, missing, defaulted = _resolve_flight_slots(
        FlightSlots(destination="Miami", depart_date="next Friday", trip_type="one_way")
    )
    assert "origin" in missing
    assert "origin" not in defaulted
    assert "origin" not in resolved


def test_a_stated_return_date_implies_round_trip_even_without_trip_type(isolated_memory_store):
    resolved, missing, defaulted = _resolve_flight_slots(
        FlightSlots(destination="Denver", origin="Austin", depart_date="next Friday", return_date="next Monday")
    )
    assert resolved["trip_type"] == "round_trip"
    assert missing == []


def test_a_resolved_round_trip_with_no_return_date_is_a_real_gap(isolated_memory_store):
    resolved, missing, defaulted = _resolve_flight_slots(
        FlightSlots(destination="Denver", origin="Austin", depart_date="next Friday", trip_type="round_trip")
    )
    assert missing == ["return_date"]


def test_a_resolved_one_way_trip_never_asks_for_a_return_date(isolated_memory_store):
    resolved, missing, defaulted = _resolve_flight_slots(
        FlightSlots(destination="Denver", origin="Austin", depart_date="next Friday", trip_type="one_way")
    )
    assert "return_date" not in missing


def test_a_second_round_only_fills_in_what_was_still_missing(isolated_memory_store):
    """The real shape of the clarification loop's second pass (main.py's
    run_command_with_clarification): the first round's resolved values are
    passed as `prior`, and a second extraction (from the user's answer)
    only ever fills in what's still missing - it must never silently
    overwrite something already known from round one."""
    resolved1, missing1, _ = _resolve_flight_slots(FlightSlots(destination="New York"))
    assert missing1 == ["origin", "depart_date", "trip_type"]

    resolved2, missing2, _ = _resolve_flight_slots(
        FlightSlots(destination="Boston", origin="Austin", depart_date="next Friday", trip_type="one_way"),
        prior=resolved1,
    )
    # destination came from round one (New York) and must NOT be
    # clobbered by round two's extractor re-reading "New York" plus the
    # answer and hallucinating/mis-extracting a different city.
    assert resolved2["destination"] == "New York"
    assert resolved2["origin"] == "Austin"
    assert missing2 == []
