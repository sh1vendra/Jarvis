"""Entry point for Jarvis backend.

Loads env vars, wires up the Orchestrator -> Planner agent chain, and runs it
against a couple of hardcoded test commands to prove the chain works before
any Mac control / voice / UI is added.
"""

import asyncio

from dotenv import load_dotenv
from google.adk.runners import InMemoryRunner
from google.genai import types

from agents.orchestrator import orchestrator_agent
from agents.planner import MilestonePlan

# Load GOOGLE_API_KEY (and anything else) from .env before any ADK/Gemini
# calls are made.
load_dotenv()

APP_NAME = "jarvis"
USER_ID = "test_user"


async def run_command(runner: InMemoryRunner, session_id: str, text: str) -> None:
    """Sends one text command through the Orchestrator agent and prints
    every event it and its sub-agents produce, then tries to parse a final
    MilestonePlan if the Planner responded."""

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


async def main() -> None:
    runner = InMemoryRunner(agent=orchestrator_agent, app_name=APP_NAME)

    # Conversational input - the orchestrator should answer this itself and
    # never transfer to the planner.
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    await run_command(runner, session.id, "hello")

    # Real task - the orchestrator should transfer to the planner, which
    # should return an ordered, outcome-based milestone plan.
    session2 = await runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    await run_command(runner, session2.id, "open Spotify and play some lo-fi music")


if __name__ == "__main__":
    asyncio.run(main())
