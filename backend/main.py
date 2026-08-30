"""Entry point for Jarvis backend.

Loads env vars, wires up the Orchestrator -> Planner agent chain, and runs it
against a couple of hardcoded test commands to prove the chain works before
any Mac control / voice / UI is added.
"""

import asyncio
import logging

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types

from agents.action import action_agent
from agents.orchestrator import orchestrator_agent
from agents.planner import Milestone, MilestonePlan
from servers.browser_bridge_server import serve_forever as serve_browser_bridge_forever

# Load GOOGLE_API_KEY (and anything else) from .env before any ADK/Gemini
# calls are made.
load_dotenv()

# So the verifier callback's logger.info/error calls in
# agents/verifier_callbacks.py actually show up on the console.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

APP_NAME = "jarvis"
USER_ID = "test_user"


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


async def main() -> None:
    # The browser bridge server (backend/servers/browser_bridge_server.py)
    # runs as an in-process background task sharing this event loop, not a
    # separate OS process - required so browser_tools.py's
    # click_web_element/type_in_web_field can await the same asyncio.Event
    # objects the server's WebSocket handler sets. See planning.md's
    # "browser bridge" entry for the full reasoning. A brief sleep gives it
    # a moment to actually bind before anything tries to use it; if the
    # Chrome extension isn't loaded/connected, browser-tool calls simply
    # report "bridge is not connected" rather than hanging silently.
    asyncio.create_task(serve_browser_bridge_forever())
    await asyncio.sleep(0.5)

    orchestrator_runner = InMemoryRunner(agent=orchestrator_agent, app_name=APP_NAME)

    # Conversational input - the orchestrator should answer this itself and
    # never transfer to the planner.
    session = await orchestrator_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    await run_command(orchestrator_runner, session.id, "hello")

    # Real task - the orchestrator should transfer to the planner, which
    # should return an ordered, outcome-based milestone plan.
    session2 = await orchestrator_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    await run_command(orchestrator_runner, session2.id, "open Spotify and play Billie Jean by Michael Jackson")

    # Real task that ends in an actual Mac-control action: Orchestrator ->
    # Planner produces a milestone plan, then we hand the reminder-creation
    # milestone to the Action agent, which extracts task/date/time itself
    # and calls the real create_reminder tool.
    session3 = await orchestrator_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    plan = await run_command(orchestrator_runner, session3.id, "set a reminder to call mom tomorrow at 5pm")

    if plan is not None:
        action_runner = InMemoryRunner(agent=action_agent, app_name=APP_NAME)
        action_session = await action_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
        for milestone in plan.milestones:
            await run_action(action_runner, action_session.id, milestone)

    # Full Spotify command, executed for real end to end: Orchestrator ->
    # Planner produces a milestone plan, then every milestone goes to the
    # Action agent, which now has open_app, click_ui, and type_in_field
    # available - open Spotify, search a specific track, click the Top
    # result card's play button. A specific track/artist query (rather
    # than a genre term like "lo-fi") reliably surfaces a "Top result"
    # card with a persistent, directly clickable play button - unlike a
    # genre-landing page, where a click navigates instead of playing (see
    # planning.md). click_ui/type_in_field print which tier actually
    # resolved each target via their tool result dicts.
    session4 = await orchestrator_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    spotify_plan = await run_command(orchestrator_runner, session4.id, "open Spotify and play Billie Jean by Michael Jackson")

    if spotify_plan is not None:
        action_runner2 = InMemoryRunner(agent=action_agent, app_name=APP_NAME)
        action_session2 = await action_runner2.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
        for milestone in spotify_plan.milestones:
            await run_action(action_runner2, action_session2.id, milestone)

    # Full browser-control command, executed for real end to end: same
    # Orchestrator -> Planner -> Action chain, but this milestone's Action
    # agent calls resolve to find_web_element/type_in_web_field
    # (tools/browser_tools.py) instead of any native-app tool. Kayak, not
    # Google Flights - Google Flights' destination field rejects
    # JS-dispatched input entirely (every synthetic event has
    # event.isTrusted === false, and its input handling appears to check
    # for that specifically), confirmed via a direct side-by-side test
    # against Kayak's own, comparably React-controlled field, which
    # accepted the same code path with no changes. See planning.md's
    # "browser bridge" entries for the full diagnosis. Assumes Kayak is
    # already the active tab - there is no tool yet for navigating to a
    # URL, only for acting on whatever page is already open, same
    # precondition the Spotify/Reminders cases above have for their
    # respective apps already being in a known state.
    session5 = await orchestrator_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    kayak_plan = await run_command(
        orchestrator_runner, session5.id, "on the Kayak website that's already open, search for a flight to New York"
    )

    if kayak_plan is not None:
        action_runner3 = InMemoryRunner(agent=action_agent, app_name=APP_NAME)
        action_session3 = await action_runner3.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
        for milestone in kayak_plan.milestones:
            await run_action(action_runner3, action_session3.id, milestone)


if __name__ == "__main__":
    asyncio.run(main())
