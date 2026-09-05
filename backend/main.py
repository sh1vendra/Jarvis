"""Entry point for Jarvis backend.

Jarvis is voice-first: real spoken audio is the only entry point the product
has, so a demo command doesn't count as working until it's been driven by
real voice, not typed text.

    python main.py                 record and run all three demo commands
                                   (Spotify, Reminders, Kayak) by voice,
                                   one at a time
    python main.py spotify         run just one of them by voice
    python main.py --list-devices  show input devices, then exit
    python main.py --device 3 ...  force a specific input device index
    python main.py --typed         old typed-command regression pass - for
                                   isolating agent-logic changes from the
                                   audio path; NOT a substitute for a voice
                                   run

`load_dotenv()` pulls `GOOGLE_API_KEY` from the repo-root `.env` before any
ADK/Gemini call. `logging.basicConfig(...)` is set up so the verifier
callback and mac_control's zoom-search logger actually print.
"""

import asyncio
import logging
import re
import sys

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types

from agents.action import action_agent
from agents.flight_slots import FlightSlots, build_flight_slot_extractor_agent
from agents.orchestrator import orchestrator_agent
from agents.planner import Milestone, MilestonePlan, build_planner_agent
from memory import store as memory_store
from servers.browser_bridge_server import serve_forever as serve_browser_bridge_forever
from voice.stt import transcribe_audio

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

APP_NAME = "jarvis"
USER_ID = "test_user"

# Held reference to the running browser bridge server task - asyncio only
# keeps a weak reference otherwise, and the GC will reap it mid-run (see
# planning.md's "browser bridge gc bug" entry).
_browser_bridge_task = None

# Tools whose own `success: true` means only "this lookup found a match" -
# never "the milestone's goal was accomplished." Every other tool wired
# into the Action agent either performs a real, verifiable action
# (click_ui, type_in_field, click_web_element, ...) or is deliberately
# hardcoded to report success: False when it's read-only by design
# (search_spotify_candidates, read_kayak_flight_results) specifically so it
# can never be mistaken for a milestone's deciding call. find_web_element
# has no such self-correction - a real, live test found run_action's "last
# tool wins" rule taking find_web_element's own success (it found *some*
# element - once, a "swap origin/destination" button, not a real field) as
# proof a "flight search parameters are entered" milestone was done, when
# no type_in_web_field call had happened at all. See planning.md's Stage 3
# entry for the real run that surfaced this.
_LOOKUP_ONLY_TOOLS = {"find_web_element"}


async def _emit(on_event, payload: dict) -> None:
    """Forwards a structured pipeline event to an optional async consumer.

    `on_event` is None for every command-line run, which keeps the printed
    output below the single source of truth for the CLI. The WebSocket
    server (servers/agent_server.py) passes a real callback so the Electron
    UI can render the same pipeline as state transitions rather than by
    scraping stdout.
    """
    if on_event is not None:
        await on_event(payload)


def summarize_plan(plan: MilestonePlan) -> str:
    """A one-line, human-readable digest of a plan, for the command_history
    log. Just the milestone goals, numbered, pipe-separated."""
    return " | ".join(f"{m.step_number}. {m.goal}" for m in plan.milestones)


def _with_preferences(text: str) -> tuple[str, dict[str, str]]:
    """Tier 1 memory read (see memory/store.py): if any stored preference is
    relevant to this command, append it as context so the Planner can fill
    in details the user didn't say - a default flight city, who "mom" is.

    Returns `(effective_text, applied_prefs)`. When nothing is relevant,
    `effective_text` is `text` unchanged and `applied_prefs` is empty, so a
    plain command reaches the Planner exactly as before.
    """
    prefs = memory_store.relevant_preferences(text)
    if not prefs:
        return text, {}
    lines = "\n".join(f"- {k}: {v}" for k, v in prefs.items())
    print(f"\n[MEMORY] applying {len(prefs)} stored preference(s):\n{lines}")
    effective = (
        f"{text}\n\n"
        "[Known user preferences - use these to fill in any detail the user did not "
        "state explicitly in the command above (e.g. a destination, a city, a "
        "contact). Do not act on a preference that isn't relevant to this command.]\n"
        f"{lines}"
    )
    return effective, prefs


async def _run_orchestrator_turn(
    runner: InMemoryRunner, session_id: str, text: str, on_event=None
) -> tuple[str | None, str | None]:
    """One real agent turn: sends `text` (preference-augmented, see
    _with_preferences) into the given runner/session and returns
    (final_text, responding_agent) - the raw ingredients each caller
    interprets differently depending on which agent actually produced the
    final response. `run_command` only ever sees `planner_agent` (a plan)
    or the orchestrator itself (a conversational reply); this same helper
    is reused by `run_command_with_clarification` for the same runner and
    also for the flight_slot_extractor_agent/planner_agent calls it makes
    directly - the turn-taking and event printing/emitting is identical
    either way, only the *interpretation* of the final response differs.
    """
    print(f"\n{'=' * 60}")
    print(f"USER COMMAND: {text!r}")
    print("=" * 60)

    effective_text, applied_prefs = _with_preferences(text)
    if applied_prefs:
        await _emit(on_event, {"type": "preferences_applied", "preferences": applied_prefs})

    message = types.Content(role="user", parts=[types.Part(text=effective_text)])

    final_text = None
    responding_agent = None

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=message,
    ):
        # Each event carries which agent produced it, useful for seeing the
        # orchestrator -> planner (or -> flight_slot_extractor) handoff
        # happen in real time.
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    print(f"[{event.author}] {part.text.strip()}")
                if getattr(part, "function_call", None):
                    print(f"[{event.author}] -> calling tool: {part.function_call.name}")

        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[0].text
            responding_agent = event.author

    print(f"\nFinal responder: {responding_agent}")
    return final_text, responding_agent


async def _parse_and_emit_plan(plan_json: str, on_event=None) -> MilestonePlan:
    """Shared by run_command and run_command_with_clarification - parses a
    real MilestonePlan out of the Planner's own structured JSON reply and
    emits the same `{"type": "plan", ...}` event either caller's client
    already expects, so a plan looks identical to the UI regardless of
    which path produced it."""
    plan = MilestonePlan.model_validate_json(plan_json)
    print("\nPARSED MILESTONE PLAN:")
    for m in plan.milestones:
        print(f"  {m.step_number}. {m.goal}")
        print(f"     success_signal: {m.success_signal}")
        if m.requires_approval:
            print("     requires_approval: TRUE")
    await _emit(
        on_event,
        {
            "type": "plan",
            "milestones": [
                {
                    "step_number": m.step_number,
                    "goal": m.goal,
                    "success_signal": m.success_signal,
                    "requires_approval": m.requires_approval,
                }
                for m in plan.milestones
            ],
        },
    )
    return plan


async def run_command(
    runner: InMemoryRunner, session_id: str, text: str, on_event=None
) -> MilestonePlan | None:
    """Sends one text command through the Orchestrator agent. Returns the
    parsed MilestonePlan if the Planner responded, else None (e.g.
    conversational input handled directly by the Orchestrator).

    Does NOT handle a flight-slot-extractor response - a plain flight
    command routed here (not through run_command_with_clarification) would
    fall through to the conversational branch below, which is why
    agent_server.py's real user-facing "text"/"audio" path calls
    run_command_with_clarification instead of this directly. This
    function stays exactly as it always has for every other caller
    (main.py's CLI demo commands for Spotify/Reminders, this module's own
    tests) - unchanged behavior, confirmed by the existing test suite.
    """
    final_text, responding_agent = await _run_orchestrator_turn(runner, session_id, text, on_event)

    if responding_agent == "planner_agent" and final_text:
        return await _parse_and_emit_plan(final_text, on_event)

    # Conversational reply handled by the Orchestrator itself - no plan.
    await _emit(on_event, {"type": "reply", "text": (final_text or "").strip()})
    return None


# ── Flight-slot clarification loop ──────────────────────────────────────────
#
# Deterministic gap-checking, not an LLM judgment call (see
# agents/flight_slots.py's own docstring for why): a slot counts as
# resolved if the request itself states it, or - for origin/destination
# only, the two slots with a real, standing personal default - a stored
# preference covers it. Anything left over after that is a genuine gap,
# and v1 asks about every genuine gap in ONE combined question, not one
# per turn - a deliberate, bounded scope (see planning.md) rather than an
# open-ended slot-filling dialogue.

_FLIGHT_SLOT_REQUIRED = ("destination", "origin", "depart_date", "trip_type")

# Only these two slots have a sensible standing personal default - a
# preferred home airport/city and a commonly-flown-to destination are
# real, stable facts about a person; a travel date or trip type is not,
# so those are never defaulted, only ever stated or asked about.
_FLIGHT_SLOT_PREFERENCE_KEYS = {
    "origin": "default_flight_origin",
    "destination": "default_flight_destination",
}

_FLIGHT_SLOT_QUESTION_PHRASES = {
    "destination": "where you're flying to",
    "origin": "where you're flying from",
    "depart_date": "what date you'd like to leave",
    "trip_type": "whether it's one-way or round-trip",
    "return_date": "when you'd like to return",
}


def _resolve_flight_slots(
    slots: FlightSlots, prior: dict[str, str] | None = None
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Deterministic slot resolution - see the module comment above.

    `prior` carries anything already resolved from an earlier round (the
    initial extraction), so a second pass (after the user's clarifying
    answer) only ever fills in what's still missing, never re-asks or
    silently overwrites something already known.

    Returns (resolved, missing, defaulted):
      resolved:  slot name -> value, for every slot successfully resolved
      missing:   slot names still genuinely unresolved, in question order
      defaulted: slot name -> value, for slots filled from a stored
                 preference rather than stated in the request - reported
                 separately so a caller can log/announce it, mirroring how
                 _with_preferences already surfaces its own defaults
    """
    resolved = dict(prior or {})
    defaulted: dict[str, str] = {}

    def _take(name: str, stated_value: str | None) -> None:
        if resolved.get(name):
            return
        if stated_value:
            resolved[name] = stated_value

    _take("destination", slots.destination)
    _take("origin", slots.origin)
    _take("depart_date", slots.depart_date)
    _take("trip_type", slots.trip_type)
    _take("return_date", slots.return_date)

    # A stated return date unambiguously implies a round trip, even if the
    # extractor didn't separately set trip_type - a real, deterministic
    # implication, not a guess.
    if not resolved.get("trip_type") and resolved.get("return_date"):
        resolved["trip_type"] = "round_trip"

    for slot_name, pref_key in _FLIGHT_SLOT_PREFERENCE_KEYS.items():
        if resolved.get(slot_name):
            continue
        pref_value = memory_store.get_preference(pref_key)
        if pref_value:
            resolved[slot_name] = pref_value
            defaulted[slot_name] = pref_value

    missing = [s for s in _FLIGHT_SLOT_REQUIRED if not resolved.get(s)]
    # return_date is conditionally required - only once trip_type is
    # actually round_trip - never asked for a resolved one-way trip.
    if resolved.get("trip_type") == "round_trip" and not resolved.get("return_date"):
        missing.append("return_date")

    return resolved, missing, defaulted


def _build_flight_clarifying_question(missing: list[str]) -> str:
    """One natural, combined question naming every genuine gap - never one
    question per slot (see the module comment above for why)."""
    phrases = [_FLIGHT_SLOT_QUESTION_PHRASES[s] for s in missing]
    if len(phrases) == 1:
        joined = phrases[0]
    else:
        joined = ", ".join(phrases[:-1]) + ", and " + phrases[-1]
    return f"I need a bit more to search that - can you tell me {joined}?"


def _build_full_flight_task_text(original_text: str, resolved: dict[str, str], still_missing: list[str]) -> str:
    """Deterministically assembles the complete, unambiguous task
    description handed to the Planner - built in code from real resolved
    values, not by relying on any LLM's memory of an earlier turn (see
    planning.md for why this was the deliberate choice here). `still_missing`
    is only ever non-empty in the honest v1 edge case where the user's one
    clarifying answer didn't cover every real gap - named plainly so the
    Planner/Action agent see it rather than silently proceeding as if
    nothing were missing."""
    # v1 is explicitly locked to Kayak (see planning.md) - the only real
    # browser-automation target this project has actually proven reliable.
    # Confirmed live this needed to be explicit: a bare "book me a flight
    # to X" with no site named left the Planner's own site choice
    # inconsistent - one real run picked Google Flights instead, the exact
    # site this project already found unreliable for automation and chose
    # Kayak specifically to avoid.
    parts = [
        original_text.rstrip(". ") + ".",
        "Search Kayak (kayak.com) for this - not Google Flights or any other site.",
    ]
    if resolved.get("origin"):
        parts.append(f"Flying from {resolved['origin']}.")
    if resolved.get("destination"):
        parts.append(f"Flying to {resolved['destination']}.")
    if resolved.get("depart_date"):
        parts.append(f"Departure date: {resolved['depart_date']}.")
    if resolved.get("trip_type"):
        parts.append(f"Trip type: {resolved['trip_type'].replace('_', ' ')}.")
    if resolved.get("return_date"):
        parts.append(f"Return date: {resolved['return_date']}.")
    if still_missing:
        parts.append(f"(Could not determine: {', '.join(still_missing)}.)")
    return " ".join(parts)


async def run_command_with_clarification(
    orchestrator_runner: InMemoryRunner,
    session_id: str,
    text: str,
    ask_clarification,
    on_event=None,
) -> MilestonePlan | None:
    """Like run_command, but recognizes when the Orchestrator routes to
    flight_slot_extractor_agent instead of straight to the Planner, and -
    only then - runs the real, deterministic clarification loop before
    ever reaching the Planner. Every other outcome (a conversational
    reply, or a task the Orchestrator sent straight to planner_agent)
    behaves exactly like run_command, because it calls the same shared
    helpers - this only adds the one new branch on top.

    `ask_clarification`: `async def ask_clarification(question: str) -> str`
    - the one real integration point a caller supplies for actually
    pausing and getting a real answer back. agent_server.py implements
    this with the generalized ClientSession.await_reply() primitive
    (Stage 1) plus the clarification_needed/clarification_response
    message pair; main.py's own CLI demo implements it with a real
    blocking input() prompt, mirroring how the CLI's approval gate already
    works (see run_milestones_until_approval).
    """
    final_text, responding_agent = await _run_orchestrator_turn(orchestrator_runner, session_id, text, on_event)

    if responding_agent != "flight_slot_extractor_agent" or not final_text:
        # Not a flight task (or the model, unusually, produced no
        # response) - behaves exactly like run_command from here.
        if responding_agent == "planner_agent" and final_text:
            return await _parse_and_emit_plan(final_text, on_event)
        await _emit(on_event, {"type": "reply", "text": (final_text or "").strip()})
        return None

    slots = FlightSlots.model_validate_json(final_text)
    resolved, missing, defaulted = _resolve_flight_slots(slots)
    if defaulted:
        print(f"\n[MEMORY] filled from stored preference(s): {defaulted}")
        await _emit(on_event, {"type": "preferences_applied", "preferences": defaulted})

    if missing:
        question = _build_flight_clarifying_question(missing)
        print(f"\n[CLARIFICATION NEEDED] {question}")
        await _emit(on_event, {"type": "clarification_needed", "question": question, "missing": missing})

        answer = await ask_clarification(question)
        print(f"[CLARIFICATION ANSWER] {answer!r}")
        await _emit(on_event, {"type": "clarification_received", "text": answer})

        # A second, independent extraction call over the original request
        # plus the real answer, combined as one piece of text - not a
        # second turn relying on any LLM's memory of the first (Stage 1
        # proved that would actually work, but this is simpler and just as
        # correct, so it's what v1 actually uses - see planning.md).
        combined_text = f"{text} Additional details: {answer}"
        extractor_runner = InMemoryRunner(agent=build_flight_slot_extractor_agent(), app_name=APP_NAME)
        extractor_session = await extractor_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
        answer_text, answer_agent = await _run_orchestrator_turn(
            extractor_runner, extractor_session.id, combined_text, on_event=None
        )
        if answer_text and answer_agent == "flight_slot_extractor_agent":
            answer_slots = FlightSlots.model_validate_json(answer_text)
            resolved, missing, _ = _resolve_flight_slots(answer_slots, prior=resolved)
        # If genuinely still missing after this one round, v1 does not
        # loop again - it proceeds honestly (see _build_full_flight_task_text)
        # rather than forcing an open-ended back-and-forth.

    full_text = _build_full_flight_task_text(text, resolved, missing)
    planner_runner = InMemoryRunner(agent=build_planner_agent(), app_name=APP_NAME)
    planner_session = await planner_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    plan_text, plan_agent = await _run_orchestrator_turn(planner_runner, planner_session.id, full_text, on_event)
    if plan_agent == "planner_agent" and plan_text:
        return await _parse_and_emit_plan(plan_text, on_event)

    await _emit(on_event, {"type": "reply", "text": (plan_text or "").strip()})
    return None


async def run_action(
    runner: InMemoryRunner, session_id: str, milestone: Milestone, on_event=None
) -> tuple[bool, str | None, list[dict] | None]:
    """Sends one milestone (goal + success_signal) to the Action agent and
    prints the tool call it makes plus the tool's own success/failure
    result.

    Returns (milestone_ok, last_agent_text). milestone_ok is True only if
    the milestone's last *deciding* tool call reported `success: true` -
    i.e. the tools that actually ran confirmed the outcome. A milestone
    whose last deciding tool reported `success: false`, or that called no
    deciding tool at all, is False. This is the honest per-milestone signal
    the pipeline uses to decide whether the whole run succeeded (see
    planning.md's "honest failure state" entry) - never the agent's own
    prose summary.

    "Deciding" excludes _LOOKUP_ONLY_TOOLS (currently just
    find_web_element): a lookup tool's own success only means it found a
    matching element, not that the milestone's goal was reached, so a
    milestone that ends on one of these (a real, observed case: the agent
    gave up on typing after only ever calling find_web_element) is never
    considered complete on that basis.

    last_agent_text is the Action agent's own final piece of reply text (if
    any) - e.g. a clarifying question it asked instead of guessing at an
    ambiguous Spotify result. Callers that need to show *why* a milestone
    didn't complete (not just that it didn't) use this; it plays no part in
    deciding milestone_ok itself.

    flight_candidates is the raw candidates list from a read_kayak_flight_
    results call made during this milestone (None if that tool wasn't
    called, or was called but read nothing) - Stage 3's real hook for the
    orchestration layer (run_plan_with_approval_gate / agent_server._run_plan)
    to know a real flight pick is needed, without keyword-matching the
    milestone's own goal text (fragile - see planning.md's paraphrasing
    fixes elsewhere) or widening this function's honest-completion logic to
    somehow cover it.

    success_signal is included alongside goal (not just goal alone) so the
    Action agent has a concrete, observable description to draw on when it
    has to fill in click_ui's expected_outcome argument - goal alone is
    often the desired end state in general terms ("lo-fi music is
    playing"), while success_signal is meant to already be the specific,
    observable signal for it ("audio is playing and a lo-fi track is shown
    as currently playing"), which is exactly what expected_outcome needs.
    """

    print(f"\n{'-' * 60}")
    print(f"ACTION AGENT MILESTONE: {milestone.goal!r}")
    print("-" * 60)

    message_text = f"Goal: {milestone.goal}\nSuccess signal: {milestone.success_signal}"
    message = types.Content(role="user", parts=[types.Part(text=message_text)])

    await _emit(
        on_event,
        {"type": "milestone_start", "step_number": milestone.step_number, "goal": milestone.goal},
    )

    tool_calls: list[tuple[str, bool | None]] = []
    last_agent_text: str | None = None
    flight_candidates: list[dict] | None = None

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=message,
    ):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "function_call", None):
                    print(
                        f"[{event.author}] -> calling tool: "
                        f"{part.function_call.name}({part.function_call.args})"
                    )
                    await _emit(on_event, {"type": "tool_call", "tool": part.function_call.name})
                if getattr(part, "function_response", None):
                    print(f"[{event.author}] tool result: {part.function_response.response}")
                    response = part.function_response.response
                    # The tool's own success field - never the agent's summary
                    # of it. Same "don't trust the self-report" principle the
                    # rest of this project runs on.
                    success = response.get("success") if isinstance(response, dict) else None
                    tool_calls.append(
                        (part.function_response.name, bool(success) if success is not None else None)
                    )
                    if (
                        part.function_response.name == "read_kayak_flight_results"
                        and isinstance(response, dict)
                        and response.get("read_ok")
                    ):
                        flight_candidates = response.get("candidates") or None
                    await _emit(
                        on_event,
                        {
                            "type": "tool_result",
                            "tool": part.function_response.name,
                            "success": bool(success) if success is not None else None,
                            "message": str(response.get("message", "")) if isinstance(response, dict) else str(response),
                        },
                    )
                if part.text:
                    stripped = part.text.strip()
                    print(f"[{event.author}] {stripped}")
                    if stripped:
                        last_agent_text = stripped
                    await _emit(on_event, {"type": "agent_text", "text": stripped})

    deciding_successes = [ok for name, ok in tool_calls if name not in _LOOKUP_ONLY_TOOLS]
    milestone_ok = bool(deciding_successes) and deciding_successes[-1] is True
    print(f"[milestone {milestone.step_number}] outcome: {'OK' if milestone_ok else 'FAILED'} "
          f"(tool calls: {tool_calls})")
    await _emit(
        on_event,
        {
            "type": "milestone_done",
            "step_number": milestone.step_number,
            "goal": milestone.goal,
            "success": milestone_ok,
        },
    )
    return milestone_ok, last_agent_text, flight_candidates


async def run_milestones_until_approval(
    action_runner: InMemoryRunner, session_id: str, milestones: list[Milestone]
) -> Milestone | None:
    """Executes milestones via the Action agent in order, but stops BEFORE
    ever executing the first one with requires_approval=True - that
    milestone is returned, never sent to run_action here, so a caller
    (eventually a real approval-modal UI; a simulated approval step in
    tests today) decides whether it actually runs at all.

    This is deliberately enforced here, at the orchestration level (the
    loop that decides which milestone to run next), rather than inside
    the Action agent itself - the Action agent's job is executing one
    milestone it's been handed, not deciding whether it should have been
    handed one in the first place. Putting the gate any lower (e.g. inside
    click_web_element) would mean trusting the agent to police its own
    execution, which is exactly the kind of self-reported gate this
    project has repeatedly found unreliable elsewhere (see planning.md).

    Returns `(paused_milestone, all_ok, flight_candidates)` - `paused_milestone`
    is the first requires_approval milestone reached (None if the whole list
    ran), `all_ok` is False if any milestone that actually ran reported
    failure, and `flight_candidates` (Stage 3) carries whatever a
    read_kayak_flight_results call in the last-run milestone read back, so
    a caller can pause for a real flight pick once the list ran out.
    """
    all_ok = True
    flight_candidates = None
    for milestone in milestones:
        if milestone.requires_approval:
            print(f"\n{'!' * 60}")
            print(f"AWAITING APPROVAL: {milestone.goal!r}")
            print(f"(success_signal: {milestone.success_signal!r})")
            print("Execution paused here - this milestone was NOT run.")
            print("!" * 60)
            return milestone, all_ok, flight_candidates
        ok, _last_text, candidates_this_milestone = await run_action(action_runner, session_id, milestone)
        if candidates_this_milestone is not None:
            flight_candidates = candidates_this_milestone
        all_ok = all_ok and ok
    return None, all_ok, flight_candidates


async def run_plan_with_approval_gate(
    action_runner: InMemoryRunner, session_id: str, milestones: list[Milestone], ask_clarification=None
) -> str:
    """Run a whole plan through the Action agent, pausing at every
    requires_approval milestone. Each pause waits on a real Enter press
    standing in for the user clicking "approve" in the overlay's approval
    card. The ADK session is unchanged across the pause, so the Action agent
    keeps full context from the milestones that already ran.

    Stage 3: if the plan's last milestone read back real Kayak flight
    candidates (read_kayak_flight_results), this pauses AGAIN after the
    plan finishes - a real question naming the candidates, via the exact
    same pause primitive Stage 1/2 built (`ask_clarification`, defaulting
    to the same real blocking input() the approval gate above already
    uses) - and waits for a real pick before returning, rather than just
    reporting the run as "failed" because the read-only milestone's own
    success is (deliberately) always False. There is no booking step yet
    (Stage 5), so the pick is only acknowledged back, not acted on.

    Returns "completed" if every milestone that ran reported success (and,
    when a real flight pick happened, the pick was acknowledged),
    "failed" if any of them didn't (the CLI has no reject path - Enter
    always approves)."""
    if ask_clarification is None:
        ask_clarification = _ask_clarification_via_input

    remaining = list(milestones)
    all_ok = True
    flight_candidates = None
    while remaining:
        pending, ran_ok, candidates = await run_milestones_until_approval(action_runner, session_id, remaining)
        all_ok = all_ok and ran_ok
        if candidates is not None:
            flight_candidates = candidates
        if pending is None:
            break
        input("\n[APPROVAL GATE] Paused. Press Enter to simulate the user approving this step...")
        print(f"[TEST] Simulated approval granted for: {pending.goal!r}")
        ok, _last_text, candidates_this_milestone = await run_action(action_runner, session_id, pending)
        if candidates_this_milestone is not None:
            flight_candidates = candidates_this_milestone
        all_ok = ok and all_ok
        remaining = remaining[remaining.index(pending) + 1:]

    if flight_candidates:
        await _pause_for_flight_pick(flight_candidates, ask_clarification)

    return "completed" if all_ok else "failed"


def build_flight_pick_question(candidates: list[dict]) -> str:
    """Renders the real candidates read back from Kayak into one question,
    shared by both the CLI pause (_pause_for_flight_pick, below) and
    agent_server.py's WS-based equivalent so the two real callers never
    drift into presenting the same data two different ways."""
    lines = []
    for c in candidates:
        stops = c.get("stops")
        stops_text = "nonstop" if stops == 0 else f"{stops} stop(s)"
        badge = f" ({c['badge']})" if c.get("badge") else ""
        lines.append(f"{c.get('position')}) {c.get('airline')} - {c.get('price')} - {stops_text}{badge}")
    return "Here are the flights I found:\n" + "\n".join(lines) + "\nWhich one would you like?"


async def _pause_for_flight_pick(candidates: list[dict], ask_clarification) -> str:
    """Stage 3's real pause-and-pick: present the real candidates read back
    from Kayak and wait for a real answer via the same `ask_clarification`
    pause primitive the flight-slot clarification loop uses (Stage 1's
    generalized await_reply(), not a new mechanism). Interpretation is
    deliberately simple, not a second LLM call - matches a bare position
    number/ordinal ("2", "the second one") or an airline name substring
    against `candidates`, falling back to just relaying the raw answer if
    neither matches, since there is nothing to act on the pick with yet
    (Stage 5, not built) - this only needs to prove the real pause/resume
    round-trip and an honest acknowledgement, not a booking decision.
    """
    question = build_flight_pick_question(candidates)

    answer = await ask_clarification(question)
    print(f"[FLIGHT PICK] {answer!r}")

    picked = _match_flight_pick(answer, candidates)
    if picked is not None:
        print(f"[FLIGHT PICK] matched: {picked}")
    return answer


_ORDINAL_WORDS = {"first": 1, "second": 2, "third": 3, "1st": 1, "2nd": 2, "3rd": 3}


def _match_flight_pick(answer: str, candidates: list[dict]) -> dict | None:
    """Deterministic, not an LLM guess - matches a bare position number, an
    ordinal word, or an airline-name substring against the real candidates.
    Returns None (not a guess) if nothing matches confidently, same "don't
    guess, say so" standard as everywhere else in this project."""
    lowered = answer.strip().lower()
    for word, position in _ORDINAL_WORDS.items():
        if word in lowered:
            for c in candidates:
                if c.get("position") == position:
                    return c
    digits = re.findall(r"\d+", lowered)
    if digits:
        position = int(digits[0])
        for c in candidates:
            if c.get("position") == position:
                return c
    for c in candidates:
        airline = str(c.get("airline") or "").strip().lower()
        if airline and airline in lowered:
            return c
    return None


async def _ask_clarification_via_input(question: str) -> str:
    """CLI stand-in for a real clarifying question - the same real,
    blocking `input()` pattern the CLI's own approval gate already uses
    (see run_plan_with_approval_gate), not a stub answer. Used by every
    demo/regression command below, not just Kayak - harmless for
    non-flight commands, since the Orchestrator only ever routes a
    genuinely flight-shaped request to the clarifier in the first place."""
    print(f"\n[CLARIFICATION NEEDED] {question}")
    return input("> ")


async def run_voice_command(
    runner: InMemoryRunner, session_id: str, audio
) -> tuple[str, MilestonePlan | None]:
    """The voice entry point: transcribe captured audio to text, then send
    that text through the same Orchestrator path a typed command would
    take, now clarification-aware (`run_command_with_clarification`) so a
    genuinely underspecified flight request (the Kayak demo command
    included) gets a real question instead of being silently mishandled
    as a conversational reply.

    `audio` is whatever the capture layer produced - a real
    `speech_recognition.AudioData`, or a `voice.stt.SimulatedAudio` in a
    unit test. `transcribe_audio` handles both; nothing below this line
    knows or cares which it was.

    Returns `(transcript, plan)` - the transcript is handed back so the
    caller can log the command to memory verbatim.
    """
    transcript = transcribe_audio(audio)
    print(f"\n{'*' * 60}")
    print(f"GOOGLE STT TRANSCRIPT: {transcript!r}")
    print("*" * 60)
    plan = await run_command_with_clarification(runner, session_id, transcript, _ask_clarification_via_input)
    return transcript, plan


# The three demo commands, in test order. `say` is a suggested STT-friendly
# phrasing - the user can word it however; the transcript is what actually
# drives the chain. `precondition` is what must already be true on screen:
# Jarvis acts on whatever app/tab is in the right state, it has no "launch
# and arrange" or "navigate to URL" tool.
DEMO_COMMANDS: dict[str, dict] = {
    "spotify": {
        "label": "SPOTIFY - play a specific track",
        "say": "open Spotify and play Billie Jean by Michael Jackson",
        "precondition": "Spotify desktop app is running (foreground or background).",
    },
    "reminder": {
        "label": "REMINDERS - create a reminder",
        "say": "set a reminder to call mom tomorrow at 5 p.m.",
        "precondition": "None - the reminder lands on the 'Jarvis Test' list.",
    },
    "kayak": {
        "label": "KAYAK - flight search, with an approval-gate pause",
        "say": "open Kayak and search for a flight to New York",
        "precondition": (
            "None - Jarvis opens Chrome and navigates to Kayak itself. "
            "Only requirement: the Jarvis Chrome extension is installed/enabled "
            "(same kind of assumption as Spotify being installed)."
        ),
    },
}


async def run_spoken_command(name: str, *, device: int | None) -> None:
    """Record one spoken demo command, transcribe it, and run the full
    Orchestrator -> Planner -> Action chain with the approval gate."""
    from voice.capture import record_push_to_talk

    spec = DEMO_COMMANDS[name]
    print("\n" + "#" * 64)
    print(f"#  {spec['label']}")
    print(f"#  precondition: {spec['precondition']}")
    print(f'#  suggested phrasing: "{spec["say"]}"')
    print("#" * 64)

    audio = record_push_to_talk(device=device)

    orchestrator_runner = InMemoryRunner(agent=orchestrator_agent, app_name=APP_NAME)
    session = await orchestrator_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    transcript, plan = await run_voice_command(orchestrator_runner, session.id, audio)
    if plan is None:
        print(
            "\n[VOICE] No plan produced - the orchestrator treated this as conversational. "
            "Check the transcript above; STT may have mangled the command."
        )
        return

    action_runner = InMemoryRunner(agent=action_agent, app_name=APP_NAME)
    action_session = await action_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    outcome = "failed"
    try:
        outcome = await run_plan_with_approval_gate(action_runner, action_session.id, plan.milestones)
    finally:
        # Tier 1 memory write: every real task command that runs gets a
        # command_history row, and `success` reflects whether the Action
        # agent's own tools actually confirmed the outcome - not just "the
        # pipeline didn't crash" (see memory/store.py, planning.md).
        success = outcome == "completed"
        memory_store.log_command(transcript, summarize_plan(plan), success)
        print(f"\n[MEMORY] logged command (transcript={transcript!r}, outcome={outcome}, success={success})")


async def run_voice_session(only: str | None = None, *, device: int | None = None) -> None:
    """Voice-driven entry point. Runs every demo command (or just `only`) by
    real microphone, one at a time, pausing between so preconditions can be
    set up."""
    global _browser_bridge_task
    # The Kayak command needs the in-process browser bridge (same event loop
    # so browser_tools can await the server's asyncio.Events). Harmless for
    # the others - if the Chrome extension isn't connected, browser-tool
    # calls just report "bridge not connected".
    _browser_bridge_task = asyncio.create_task(serve_browser_bridge_forever())
    await asyncio.sleep(0.5)

    names = [only] if only else list(DEMO_COMMANDS)
    for i, name in enumerate(names, start=1):
        if len(names) > 1:
            input(f"\n>>> Command {i} of {len(names)} ({name}). Set up its precondition, then press Enter.")
        await run_spoken_command(name, device=device)

    print("\n" + "=" * 60)
    print("Voice session complete.")
    print("=" * 60)


async def run_typed_regression() -> None:
    """Typed-command pass - kept for isolating agent-logic changes from the
    audio path. This is NOT the completion bar: a demo command only counts
    as working once `run_voice_session` has driven it by real voice."""
    global _browser_bridge_task
    _browser_bridge_task = asyncio.create_task(serve_browser_bridge_forever())
    await asyncio.sleep(0.5)

    orchestrator_runner = InMemoryRunner(agent=orchestrator_agent, app_name=APP_NAME)

    # Conversational input - orchestrator answers directly, never transfers.
    session = await orchestrator_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    await run_command(orchestrator_runner, session.id, "hello")

    # Spotify, full chain.
    session2 = await orchestrator_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    spotify_plan = await run_command(
        orchestrator_runner, session2.id, "open Spotify and play Billie Jean by Michael Jackson"
    )
    if spotify_plan is not None:
        action_runner = InMemoryRunner(agent=action_agent, app_name=APP_NAME)
        action_session = await action_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
        outcome = await run_plan_with_approval_gate(action_runner, action_session.id, spotify_plan.milestones)
        memory_store.log_command("open Spotify and play Billie Jean by Michael Jackson", summarize_plan(spotify_plan), outcome == "completed")

    # Reminder, full chain.
    session3 = await orchestrator_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    reminder_plan = await run_command(
        orchestrator_runner, session3.id, "set a reminder to call mom tomorrow at 5pm"
    )
    if reminder_plan is not None:
        action_runner2 = InMemoryRunner(agent=action_agent, app_name=APP_NAME)
        action_session2 = await action_runner2.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
        outcome = await run_plan_with_approval_gate(action_runner2, action_session2.id, reminder_plan.milestones)
        memory_store.log_command("set a reminder to call mom tomorrow at 5pm", summarize_plan(reminder_plan), outcome == "completed")

    # Kayak, full chain including the approval-gate pause. Jarvis opens
    # Chrome and navigates to Kayak itself now (first milestone,
    # navigate_to_url) - no manual browser setup. See planning.md for why
    # Kayak, not Google Flights.
    session4 = await orchestrator_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    kayak_plan = await run_command_with_clarification(
        orchestrator_runner, session4.id, "open Kayak and search for a flight to New York", _ask_clarification_via_input
    )
    if kayak_plan is not None:
        action_runner3 = InMemoryRunner(agent=action_agent, app_name=APP_NAME)
        action_session3 = await action_runner3.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
        outcome = await run_plan_with_approval_gate(action_runner3, action_session3.id, kayak_plan.milestones)
        memory_store.log_command("open Kayak and search for a flight to New York", summarize_plan(kayak_plan), outcome == "completed")


def _parse_args(argv: list[str]) -> dict:
    opts = {"mode": "voice", "only": None, "device": None, "list_devices": False}
    it = iter(argv)
    for arg in it:
        if arg == "--typed":
            opts["mode"] = "typed"
        elif arg == "--list-devices":
            opts["list_devices"] = True
        elif arg == "--device":
            opts["device"] = int(next(it))
        elif arg in DEMO_COMMANDS:
            opts["only"] = arg
        else:
            raise SystemExit(f"unknown argument: {arg!r}\n\n{__doc__}")
    return opts


if __name__ == "__main__":
    opts = _parse_args(sys.argv[1:])

    if opts["list_devices"]:
        from voice.capture import list_input_devices

        print(list_input_devices())
    elif opts["mode"] == "typed":
        asyncio.run(run_typed_regression())
    else:
        asyncio.run(run_voice_session(opts["only"], device=opts["device"]))
