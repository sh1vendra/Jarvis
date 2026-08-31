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
import sys

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types

from agents.action import action_agent
from agents.orchestrator import orchestrator_agent
from agents.planner import Milestone, MilestonePlan
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


async def run_command(runner: InMemoryRunner, session_id: str, text: str) -> MilestonePlan | None:
    """Sends one text command through the Orchestrator agent and prints
    every event it and its sub-agents produce. Returns the parsed
    MilestonePlan if the Planner responded, else None (e.g. conversational
    input handled directly by the Orchestrator)."""

    print(f"\n{'=' * 60}")
    print(f"USER COMMAND: {text!r}")
    print("=" * 60)

    message = types.Content(role="user", parts=[types.Part(text=text)])

    final_text = None
    responding_agent = None

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=message,
    ):
        # Each event carries which agent produced it, useful for seeing the
        # orchestrator -> planner handoff happen in real time.
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

    if responding_agent == "planner_agent" and final_text:
        plan = MilestonePlan.model_validate_json(final_text)
        print("\nPARSED MILESTONE PLAN:")
        for m in plan.milestones:
            print(f"  {m.step_number}. {m.goal}")
            print(f"     success_signal: {m.success_signal}")
            if m.requires_approval:
                print("     requires_approval: TRUE")
        return plan

    return None


async def run_action(runner: InMemoryRunner, session_id: str, milestone: Milestone) -> None:
    """Sends one milestone (goal + success_signal) to the Action agent and
    prints the tool call it makes plus the tool's own success/failure
    result.

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
                if getattr(part, "function_response", None):
                    print(f"[{event.author}] tool result: {part.function_response.response}")
                if part.text:
                    print(f"[{event.author}] {part.text.strip()}")


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

    Returns the paused Milestone, or None if every milestone ran without
    hitting an approval gate.
    """
    for milestone in milestones:
        if milestone.requires_approval:
            print(f"\n{'!' * 60}")
            print(f"AWAITING APPROVAL: {milestone.goal!r}")
            print(f"(success_signal: {milestone.success_signal!r})")
            print("Execution paused here - this milestone was NOT run.")
            print("!" * 60)
            return milestone
        await run_action(action_runner, session_id, milestone)
    return None


async def run_plan_with_approval_gate(
    action_runner: InMemoryRunner, session_id: str, milestones: list[Milestone]
) -> None:
    """Run a whole plan through the Action agent, pausing at every
    requires_approval milestone. Each pause waits on a real Enter press
    standing in for the user clicking "approve" in the (not-yet-built)
    modal - so the gate visibly holds, then visibly resumes, rather than
    being auto-approved. The ADK session is unchanged across the pause, so
    the Action agent keeps full context from the milestones that already
    ran."""
    remaining = list(milestones)
    while remaining:
        pending = await run_milestones_until_approval(action_runner, session_id, remaining)
        if pending is None:
            return
        input("\n[APPROVAL GATE] Paused. Press Enter to simulate the user approving this step...")
        print(f"[TEST] Simulated approval granted for: {pending.goal!r}")
        await run_action(action_runner, session_id, pending)
        remaining = remaining[remaining.index(pending) + 1:]


async def run_voice_command(runner: InMemoryRunner, session_id: str, audio) -> MilestonePlan | None:
    """The voice entry point: transcribe captured audio to text, then send
    that text through exactly the same Orchestrator path a typed command
    would take (`run_command`).

    `audio` is whatever the capture layer produced - a real
    `speech_recognition.AudioData`, or a `voice.stt.SimulatedAudio` in a
    unit test. `transcribe_audio` handles both; nothing below this line
    knows or cares which it was.
    """
    transcript = transcribe_audio(audio)
    print(f"\n{'*' * 60}")
    print(f"GOOGLE STT TRANSCRIPT: {transcript!r}")
    print("*" * 60)
    return await run_command(runner, session_id, transcript)


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
    plan = await run_voice_command(orchestrator_runner, session.id, audio)
    if plan is None:
        print(
            "\n[VOICE] No plan produced - the orchestrator treated this as conversational. "
            "Check the transcript above; STT may have mangled the command."
        )
        return

    action_runner = InMemoryRunner(agent=action_agent, app_name=APP_NAME)
    action_session = await action_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    await run_plan_with_approval_gate(action_runner, action_session.id, plan.milestones)


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
        await run_plan_with_approval_gate(action_runner, action_session.id, spotify_plan.milestones)

    # Reminder, full chain.
    session3 = await orchestrator_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    reminder_plan = await run_command(
        orchestrator_runner, session3.id, "set a reminder to call mom tomorrow at 5pm"
    )
    if reminder_plan is not None:
        action_runner2 = InMemoryRunner(agent=action_agent, app_name=APP_NAME)
        action_session2 = await action_runner2.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
        await run_plan_with_approval_gate(action_runner2, action_session2.id, reminder_plan.milestones)

    # Kayak, full chain including the approval-gate pause. Jarvis opens
    # Chrome and navigates to Kayak itself now (first milestone,
    # navigate_to_url) - no manual browser setup. See planning.md for why
    # Kayak, not Google Flights.
    session4 = await orchestrator_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    kayak_plan = await run_command(
        orchestrator_runner, session4.id, "open Kayak and search for a flight to New York"
    )
    if kayak_plan is not None:
        action_runner3 = InMemoryRunner(agent=action_agent, app_name=APP_NAME)
        action_session3 = await action_runner3.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
        await run_plan_with_approval_gate(action_runner3, action_session3.id, kayak_plan.milestones)


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
