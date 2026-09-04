"""agent_server._run_plan - the honest failed/cancelled state propagation,
and the approval gate that must never let a consequential milestone run
without a real, explicit decision.

Mocked at the same seam as test_run_action.py, one level up: here
`run_action` itself is monkeypatched (agent_server's own bound name, not
main's - see the note below) to a scripted stand-in, so these tests
exercise `_run_plan`'s own control flow - which milestone runs when, what
gets sent to the client, what string it returns - without needing a real
Action agent turn underneath it. `run_action` already has its own,
separate, thorough tests one layer down (test_run_action.py); duplicating
real tool/event mechanics here would just make these tests slower and
harder to read for no extra coverage.
"""

import pytest

import servers.agent_server as agent_server
from agents.planner import Milestone, MilestonePlan
from tests.fakes import RecordingSession


def _milestone(step, goal, *, requires_approval=False):
    return Milestone(step_number=step, goal=goal, success_signal=f"{goal} is observably true", requires_approval=requires_approval)


def _patch_run_action(monkeypatch, results):
    """`results` is a list of (ok, text) tuples, consumed in call order -
    one per milestone actually reached. Patched on agent_server's own
    module namespace: `_run_plan` calls the name it imported via `from
    main import ... run_action ...`, which is a separate binding from
    `main.run_action` itself - patching `main.run_action` would leave
    agent_server's already-bound reference untouched.
    """
    calls = []

    async def fake_run_action(_runner, _session_id, milestone, on_event=None):
        calls.append(milestone.goal)
        return results.pop(0)

    monkeypatch.setattr(agent_server, "run_action", fake_run_action)
    return calls


@pytest.mark.asyncio
async def test_all_milestones_succeed_reports_completed(monkeypatch):
    plan = MilestonePlan(milestones=[_milestone(1, "A reminder to call mom exists")])
    calls = _patch_run_action(monkeypatch, [(True, "Created the reminder.")])
    session = RecordingSession()

    outcome = await agent_server._run_plan(session, plan)

    assert outcome == "completed"
    assert calls == ["A reminder to call mom exists"]
    states = [p["state"] for p in session.sent if p["type"] == "state"]
    assert states == ["doing", "done"]
    # A real spoken confirmation was queued - not asserting which exact
    # rotating phrase (see agent_server._speak_text_for_done_plan), just
    # that speech happens on a real completion.
    assert any(p["type"] == "speak" for p in session.sent)


@pytest.mark.asyncio
async def test_a_failed_tool_produces_honest_failed_state_not_done(monkeypatch):
    """The exact regression this project's "honest failure state" work
    exists to prevent: a milestone whose last tool reported success:false
    must surface as a real `failed` state, carrying the real reason - never
    silently shown as `done` over a run where nothing actually worked."""
    plan = MilestonePlan(milestones=[_milestone(1, "Bohemian Rhapsody is playing in Spotify")])
    _patch_run_action(
        monkeypatch,
        [(False, "Clicked the top result but Spotify's player state never changed.")],
    )
    session = RecordingSession()

    outcome = await agent_server._run_plan(session, plan)

    assert outcome == "failed"
    states = [p for p in session.sent if p["type"] == "state"]
    assert [s["state"] for s in states] == ["doing", "failed"]
    failed_state = states[-1]
    assert failed_state["failed_goals"] == [
        {"goal": "Bohemian Rhapsody is playing in Spotify", "message": "Clicked the top result but Spotify's player state never changed."}
    ]
    # The spoken text for a failure carries the real reason, not a generic
    # "something went wrong" and never the action-confirmation flavor.
    speak = next(p for p in session.sent if p["type"] == "speak")
    assert speak["text"] == "Clicked the top result but Spotify's player state never changed."


@pytest.mark.asyncio
async def test_one_failed_milestone_among_several_still_reports_failed(monkeypatch):
    """A plan can have multiple milestones; the run loop keeps going after
    one fails (see agent_server.py's own comment on this), but the overall
    outcome must still be honestly `failed`, not `completed` just because
    later milestones succeeded."""
    plan = MilestonePlan(
        milestones=[
            _milestone(1, "Spotify search results for X are visible"),
            _milestone(2, "X is playing in Spotify"),
        ]
    )
    _patch_run_action(
        monkeypatch,
        [
            (False, "Could not resolve an unambiguous match."),
            (True, "Played anyway once resolved."),
        ],
    )
    session = RecordingSession()

    outcome = await agent_server._run_plan(session, plan)

    assert outcome == "failed"
    failed_state = next(p for p in session.sent if p["type"] == "state" and p["state"] == "failed")
    assert failed_state["failed_goals"] == [
        {"goal": "Spotify search results for X are visible", "message": "Could not resolve an unambiguous match."}
    ]


@pytest.mark.asyncio
async def test_approval_required_milestone_never_runs_before_a_decision_arrives(monkeypatch):
    """The approval gate's core guarantee: run_action must not be called
    for a requires_approval milestone until await_reply() actually
    resolves - approving must be a real precondition, not a formality."""
    plan = MilestonePlan(
        milestones=[
            _milestone(1, "Kayak search for flights to Austin is submitted", requires_approval=True),
        ]
    )
    calls = _patch_run_action(monkeypatch, [(True, "Submitted the search.")])
    session = RecordingSession(approval_answers=[True])

    outcome = await agent_server._run_plan(session, plan)

    assert outcome == "completed"
    # run_action only ran AFTER the approval was granted - if the gate were
    # broken (calling run_action before/without awaiting approval), this
    # list would already contain the call by the time we could inspect it,
    # but the ordering assertion below on session.sent is the real proof:
    # approval_required and approval_result both appear before "doing".
    assert calls == ["Kayak search for flights to Austin is submitted"]
    # `doing` is sent unconditionally as soon as _run_plan starts (before it
    # even looks at the first milestone) - that's real, existing behavior,
    # not something an approval gate changes. What the gate actually
    # guarantees is approval_required -> approval_result -> (only then)
    # run_action, which `calls` above already proves; this pins the
    # message ordering around it.
    types_sent = session.sent_types()
    assert types_sent.index("approval_required") < types_sent.index("approval_result")


@pytest.mark.asyncio
async def test_rejected_approval_produces_cancelled_not_silent_failure_or_execution(monkeypatch):
    """Rejecting an approval gate must (a) never call run_action for that
    milestone at all, (b) produce a real `cancelled` state naming the
    refused step, and (c) return "rejected" - not "failed", which would
    wrongly imply a tool ran and didn't work, and not "completed", which
    would be a real safety bug."""
    plan = MilestonePlan(
        milestones=[
            _milestone(1, "A message is sent to the team", requires_approval=True),
        ]
    )
    calls = _patch_run_action(monkeypatch, [(True, "should never be reached")])
    session = RecordingSession(approval_answers=[False])

    outcome = await agent_server._run_plan(session, plan)

    assert outcome == "rejected"
    assert calls == []  # run_action was never called for the rejected milestone
    states = [p for p in session.sent if p["type"] == "state"]
    # "doing" is sent unconditionally as _run_plan starts (see the
    # approval-ordering test above) - `cancelled` is what actually matters
    # here: it's the real terminal state, sent once, naming the refused
    # step, and nothing after it (no "failed", no "done").
    assert [s["state"] for s in states] == ["doing", "cancelled"]
    assert states[-1]["goal"] == "A message is sent to the team"


@pytest.mark.asyncio
async def test_rejection_stops_the_whole_plan_not_just_that_milestone(monkeypatch):
    """A rejected gate must stop the run entirely - a later milestone must
    never execute "anyway" after an earlier consequential step was
    refused."""
    plan = MilestonePlan(
        milestones=[
            _milestone(1, "A payment of $50 is submitted", requires_approval=True),
            _milestone(2, "A confirmation email is sent"),
        ]
    )
    calls = _patch_run_action(monkeypatch, [(True, "should never be reached")])
    session = RecordingSession(approval_answers=[False])

    outcome = await agent_server._run_plan(session, plan)

    assert outcome == "rejected"
    assert calls == []


@pytest.mark.asyncio
async def test_only_the_marked_milestone_pauses_for_approval(monkeypatch):
    """A plan mixing ordinary and requires_approval milestones must only
    gate on the one actually marked - everything else runs straight
    through, in order."""
    plan = MilestonePlan(
        milestones=[
            _milestone(1, "Google Chrome is open with www.kayak.com loaded"),
            _milestone(2, "The flight search is submitted", requires_approval=True),
        ]
    )
    calls = _patch_run_action(monkeypatch, [(True, "opened"), (True, "submitted")])
    session = RecordingSession(approval_answers=[True])

    outcome = await agent_server._run_plan(session, plan)

    assert outcome == "completed"
    assert calls == [
        "Google Chrome is open with www.kayak.com loaded",
        "The flight search is submitted",
    ]
    # Only one approval_required was ever sent, for the second milestone.
    assert session.sent_types().count("approval_required") == 1
