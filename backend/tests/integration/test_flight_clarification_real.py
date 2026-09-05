"""Real, end-to-end coverage of the flight-clarification loop
(main.py's run_command_with_clarification) - the three scenarios the
approved Stage 2 plan explicitly called out as needing real verification,
not assumed:

1. A genuinely underspecified command ("book me a flight to New York")
   correctly identifies the real gaps and asks one combined, sensible
   question.
2. A command partially covered by a stored preference silently uses the
   preference and only asks about the genuine remaining gaps.
3. A fully-specified command skips clarification entirely and reaches the
   existing Planner unchanged.

INTEGRATION, not unit, deliberately: this exercises the real Orchestrator
-> flight_slot_extractor_agent -> (real clarification pause) -> Planner
chain end to end, with real Gemini calls at every step - the same
reasoning as test_planner_reminder_regression.py (a prompt/classification
regression is exactly what a mocked LLM response could never catch). The
deterministic gap-check logic itself (_resolve_flight_slots) has its own
fast, dedicated unit tests (test_flight_slots.py); this file is about
whether the real agents actually produce what that logic expects to
receive, and whether the real pipeline reaches the real Planner
afterward - unchanged, per the approved plan's own explicit promise.
"""

import pytest
from google.adk.runners import InMemoryRunner

from agents.orchestrator import orchestrator_agent
from main import APP_NAME, USER_ID, run_command_with_clarification

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_underspecified_command_asks_one_real_combined_question():
    asked: list[str] = []

    async def ask_clarification(question: str) -> str:
        asked.append(question)
        return "from Austin, next Friday, one-way"

    runner = InMemoryRunner(agent=orchestrator_agent, app_name=APP_NAME)
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    plan = await run_command_with_clarification(runner, session.id, "book me a flight to New York", ask_clarification)

    assert len(asked) == 1, "a genuinely underspecified request must ask exactly one combined question"
    question = asked[0].lower()
    assert "from" in question or "flying from" in question
    assert "date" in question
    assert "one-way" in question or "round" in question

    assert plan is not None, "expected a real plan once the real gaps were answered"
    goals = " ".join(m.goal.lower() for m in plan.milestones)
    assert "kayak" in goals, "v1 is explicitly locked to Kayak - the plan must name it, not Google Flights or any other site"


@pytest.mark.asyncio
async def test_stored_preference_covers_part_of_the_gap_silently(isolated_memory_store):
    from memory import store as memory_store

    memory_store.set_preference("default_flight_destination", "Denver, Colorado")

    asked: list[str] = []

    async def ask_clarification(question: str) -> str:
        asked.append(question)
        return "from Austin, next Tuesday, round trip, returning next Friday"

    runner = InMemoryRunner(agent=orchestrator_agent, app_name=APP_NAME)
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    # Deliberately no destination stated - only the stored preference covers it.
    plan = await run_command_with_clarification(runner, session.id, "book me a flight", ask_clarification)

    assert len(asked) == 1
    question = asked[0].lower()
    assert "denver" not in question and "where you're flying to" not in question, (
        "destination was already covered by a stored preference - it must never appear in the question"
    )
    assert "from" in question or "flying from" in question, "origin was a real gap and must still be asked about"

    assert plan is not None
    goals = " ".join(m.goal.lower() for m in plan.milestones)
    assert "denver" in goals, "the silently-defaulted preference must still reach the real plan"


@pytest.mark.asyncio
async def test_fully_specified_command_skips_clarification_entirely():
    async def ask_clarification(question: str) -> str:
        raise AssertionError(f"a fully-specified request must never be asked anything, but was asked: {question!r}")

    runner = InMemoryRunner(agent=orchestrator_agent, app_name=APP_NAME)
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    plan = await run_command_with_clarification(
        runner, session.id, "find flights to Denver next Friday, one way, from Austin", ask_clarification
    )

    assert plan is not None
    goals = " ".join(m.goal.lower() for m in plan.milestones)
    assert "kayak" in goals
