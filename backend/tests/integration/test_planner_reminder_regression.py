"""A real regression test for a real bug: the Planner once emitted two
milestones for a single atomic reminder creation ("the reminder details
are entered" then a separate "the reminder is saved"), which made the
pipeline create the same reminder twice.

This is an INTEGRATION test, not a unit test, and deliberately so - the
fix was a change to the Planner agent's own prompt (see agents/planner.py's
"MILESTONE GRANULARITY" instruction), not to any deterministic Python
logic. A unit test that mocked the LLM's response would only prove "the
mock returns what the mock was told to return" - it would test nothing
real about whether the prompt still says the right thing, and would stay
green even if the prompt regressed. The only test that actually means
anything here calls the real Planner (via the real Orchestrator) and
checks what it really produces. That means this test needs GOOGLE_API_KEY
and network access, and costs a real (tiny) amount of Gemini usage - the
honest tradeoff for a regression test that can't be faked.
"""

import asyncio

import pytest
from google.adk.runners import InMemoryRunner

from agents.orchestrator import orchestrator_agent
from main import APP_NAME, USER_ID, run_command

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_a_reminder_command_produces_exactly_one_milestone():
    runner = InMemoryRunner(agent=orchestrator_agent, app_name=APP_NAME)
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)

    plan = await asyncio.wait_for(
        run_command(runner, session.id, "create a reminder to call mom tomorrow at 5pm"),
        timeout=60,
    )

    assert plan is not None, "expected a real milestone plan, got a conversational reply instead"
    assert len(plan.milestones) == 1, (
        "a reminder is one atomic action - two milestones here is exactly the regression that "
        f"once created the same reminder twice. Got: {[m.goal for m in plan.milestones]}"
    )
    goal = plan.milestones[0].goal.lower()
    assert "remind" in goal or "reminder" in goal
    # A routine personal reminder must never require approval - see
    # planner.py's own instruction on this.
    assert plan.milestones[0].requires_approval is False
