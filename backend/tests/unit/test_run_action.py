"""main.run_action's honest bool - the single most load-bearing piece of
verification logic in this project.

Every "did it actually work" decision downstream (agent_server._run_plan's
failed/cancelled propagation, command_history's success column) rests on
`run_action` correctly deriving milestone_ok from the tool's own reported
`success` field - never from whether the agent *said* it worked, never
from whether anything merely ran without raising. These tests exercise
that derivation directly, against scripted ADK event sequences (see
tests/fakes.py for why that's the right mock boundary and not a shallower
or deeper one) - no real Gemini call, no real Mac state, and deliberately
not testing ADK itself, only this project's own logic on top of it.
"""

import pytest

from main import run_action
from agents.planner import Milestone
from tests.fakes import FakeRunner, text_event, tool_call_event, tool_result_event

_MILESTONE = Milestone(
    step_number=1,
    goal="A reminder to call mom tomorrow at 5pm exists",
    success_signal="The reminder is visible in the Reminders app",
)


async def _run(events) -> tuple[bool, str | None]:
    """Drops the 3rd (flight_candidates) return value for every test that
    doesn't care about it - see test_a_read_kayak_flight_results_call_
    surfaces_its_candidates below for the one that does."""
    runner = FakeRunner(events)
    ok, text, _candidates = await run_action(runner, session_id="test-session", milestone=_MILESTONE)
    return ok, text


@pytest.mark.asyncio
async def test_last_tool_success_true_means_milestone_ok():
    events = [
        tool_call_event("action_agent", "create_reminder", {"task": "call mom"}),
        tool_result_event(
            "action_agent",
            "create_reminder",
            {"success": True, "message": "Created reminder 'call mom'.", "error": None},
        ),
    ]
    ok, _text = await _run(events)
    assert ok is True


@pytest.mark.asyncio
async def test_last_tool_success_false_means_milestone_not_ok():
    """The core regression this whole project is built around: a tool that
    ran and returned success:false must never be reported as done."""
    events = [
        tool_call_event("action_agent", "click_ui", {"target_description": "play button"}),
        tool_result_event(
            "action_agent",
            "click_ui",
            {"success": False, "message": "no visual change detected - the click likely missed", "error": "click_outcome_not_verified"},
        ),
    ]
    ok, _text = await _run(events)
    assert ok is False


@pytest.mark.asyncio
async def test_no_tool_called_at_all_means_milestone_not_ok():
    """The Action agent can respond with pure text and never call a tool -
    e.g. surfacing a genuinely ambiguous Spotify result instead of
    guessing (see planning.md). That must be an honest non-completion, not
    a silent success just because nothing raised."""
    events = [
        text_event(
            "action_agent",
            'There are multiple versions of "Mad World" - which one would you like me to play?',
        ),
    ]
    ok, text = await _run(events)
    assert ok is False
    assert "Mad World" in text


@pytest.mark.asyncio
async def test_outcome_is_the_last_tool_not_any_tool_or_all_tools():
    """A milestone whose FIRST tool call succeeded but whose LAST tool call
    (a subsequent, corrective, or verifying step) failed must report not
    ok - "any success in the sequence" would be a real, dangerous
    weakening of this guarantee."""
    events = [
        tool_call_event("action_agent", "search_spotify_candidates", {"query": "Bohemian Rhapsody"}),
        tool_result_event(
            "action_agent",
            "search_spotify_candidates",
            {"success": False, "read_ok": True, "candidates": [], "message": "read-only step"},
        ),
        tool_call_event("action_agent", "click_ui", {}),
        tool_result_event(
            "action_agent",
            "click_ui",
            {"success": True, "message": "verified via Spotify's player state"},
        ),
    ]
    ok, _text = await _run(events)
    assert ok is True

    # Same shape, reversed final outcome - the corrective/verifying step
    # this time is the one that failed, so the milestone must not be ok
    # even though an earlier tool in the same sequence reported success.
    events_reversed_outcome = [
        tool_call_event("action_agent", "click_ui", {}),
        tool_result_event("action_agent", "click_ui", {"success": True, "message": "clicked"}),
        tool_call_event("action_agent", "click_ui", {}),
        tool_result_event(
            "action_agent",
            "click_ui",
            {"success": False, "message": "no playback change per Spotify's player state"},
        ),
    ]
    ok2, _text2 = await _run(events_reversed_outcome)
    assert ok2 is False


@pytest.mark.asyncio
async def test_missing_success_key_is_not_treated_as_true():
    """A tool result dict with no `success` key at all (malformed, or a
    non-dict response) must not be silently treated as truthy - only an
    explicit `success: true` should ever count."""
    events = [
        tool_call_event("action_agent", "some_tool", {}),
        tool_result_event("action_agent", "some_tool", {"message": "did something, forgot to report success"}),
    ]
    ok, _text = await _run(events)
    assert ok is False


@pytest.mark.asyncio
async def test_last_agent_text_captures_the_final_reply_not_the_first():
    events = [
        text_event("action_agent", "Let me check that."),
        tool_call_event("action_agent", "create_reminder", {}),
        tool_result_event("action_agent", "create_reminder", {"success": True, "message": "done"}),
        text_event("action_agent", "Created the reminder for tomorrow at 5pm."),
    ]
    ok, text = await _run(events)
    assert ok is True
    assert text == "Created the reminder for tomorrow at 5pm."


@pytest.mark.asyncio
async def test_a_lookup_only_tool_as_the_last_call_is_not_treated_as_completion():
    """The real bug this pins down: find_web_element's own success only
    means "found some matching element" - never "the milestone's goal was
    reached." A real live run gave up on typing after repeatedly failing
    to locate a field, with its very last tool call a find_web_element
    that happened to match an unrelated element (Kayak's own "swap origin
    and destination" button) - and the old "last tool wins" rule reported
    the whole milestone done, even though no type_in_web_field call ever
    happened. See planning.md's Stage 3 entry for the real run."""
    events = [
        tool_call_event("action_agent", "find_web_element", {"description": "the origin input field"}),
        tool_result_event(
            "action_agent",
            "find_web_element",
            {"success": False, "message": "No element matching 'the origin input field' found.", "ref_id": None},
        ),
        tool_call_event("action_agent", "find_web_element", {"description": "origin"}),
        tool_result_event(
            "action_agent",
            "find_web_element",
            {"success": True, "message": "Found element matching 'origin': 'Swap origin and destination locations'.", "ref_id": "jw_9"},
        ),
    ]
    ok, _text = await _run(events)
    assert ok is False


@pytest.mark.asyncio
async def test_a_real_action_after_a_failed_lookup_still_counts():
    """The exclusion only skips find_web_element as the *deciding* call -
    it must not blind run_action to a real action tool that follows and
    genuinely succeeds."""
    events = [
        tool_call_event("action_agent", "find_web_element", {"description": "destination"}),
        tool_result_event(
            "action_agent",
            "find_web_element",
            {"success": True, "message": "Found element matching 'destination'.", "ref_id": "jw_21"},
        ),
        tool_call_event("action_agent", "type_in_web_field", {"ref_id": "jw_21", "text": "New York"}),
        tool_result_event(
            "action_agent",
            "type_in_web_field",
            {"success": True, "message": "Typed 'New York' into jw_21.", "generation": 2},
        ),
    ]
    ok, _text = await _run(events)
    assert ok is True


@pytest.mark.asyncio
async def test_a_read_kayak_flight_results_call_surfaces_its_candidates():
    """Stage 3's real hook: run_action must surface the raw candidates list
    from a read_kayak_flight_results call so the orchestration layer
    (run_plan_with_approval_gate / agent_server._run_plan) can pause for a
    real pick - without that, the only signal available is prose text,
    which is what main.build_flight_pick_question exists to avoid having
    to re-parse."""
    real_candidates = [
        {"position": 1, "airline": "Delta", "price": "$439", "stops": 0, "badge": "Best"},
        {"position": 2, "airline": "JetBlue", "price": "$391", "stops": 1, "badge": "Cheapest"},
    ]
    events = [
        tool_call_event("action_agent", "read_kayak_flight_results", {}),
        tool_result_event(
            "action_agent",
            "read_kayak_flight_results",
            {"success": False, "read_ok": True, "candidates": real_candidates, "message": "..."},
        ),
    ]
    runner = FakeRunner(events)
    ok, _text, candidates = await run_action(runner, session_id="test-session", milestone=_MILESTONE)
    assert ok is False  # read-only, deliberately never a completion signal
    assert candidates == real_candidates


@pytest.mark.asyncio
async def test_a_read_kayak_flight_results_call_with_nothing_read_surfaces_no_candidates():
    events = [
        tool_call_event("action_agent", "read_kayak_flight_results", {}),
        tool_result_event(
            "action_agent",
            "read_kayak_flight_results",
            {"success": False, "read_ok": False, "candidates": [], "message": "nothing visible"},
        ),
    ]
    runner = FakeRunner(events)
    _ok, _text, candidates = await run_action(runner, session_id="test-session", milestone=_MILESTONE)
    assert candidates is None


@pytest.mark.asyncio
async def test_no_events_at_all_is_an_honest_non_completion():
    """A degenerate case worth pinning down explicitly: if the agent run
    produces nothing at all, that must not default to success."""
    ok, text = await _run([])
    assert ok is False
    assert text is None
