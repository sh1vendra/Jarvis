"""The real, empirical question Stage 1 of the clarification/booking
subsystem flagged as unresolved (planning.md, memory): if the same
orchestrator ADK session is kept alive across a real pause (the
generalized `ClientSession.await_reply()` primitive - see
servers/agent_server.py), with other real WebSocket traffic processed by
the real running server during that pause window, does the orchestrator's
own conversational memory of turn 1 survive intact into turn 2?

This is the single mechanism the whole clarification loop (Stage 2+)
depends on - so it's tested here for real, not assumed: a real
`websockets` server running the actual, unmodified `agent_handler`
(imported directly, not reimplemented), a real connected client sending
real interleaved traffic (mic_state, tts_state, ping) while a real pause
is outstanding, and a real two-turn Gemini conversation through the same
`InMemoryRunner` session id, spanning that pause. INTEGRATION, not unit -
deliberately: this is exactly the kind of thing a mock would prove nothing
real about (a fake session/fake runner would trivially "remember" whatever
the test wired it to remember; the real question is whether ADK's own
session state and this project's own asyncio plumbing actually cooperate
under real concurrent traffic), so it costs two small real Gemini calls
and needs GOOGLE_API_KEY - the same honest tradeoff every other real
Gemini-backed test in this suite makes.
"""

import asyncio
import json

import pytest
import websockets
from google.adk.runners import InMemoryRunner

from agents.orchestrator import orchestrator_agent
from main import APP_NAME, USER_ID, run_command
from servers import agent_server

pytestmark = pytest.mark.integration


async def _pause_for_clarification(session: "agent_server.ClientSession", question: str) -> str:
    """Exactly the shape Stage 2's real flight-clarification caller will
    use - built here only to exercise the generalized primitive for real,
    not as product code. Uses nothing but the newly-generalized
    await_reply()/the clarification_needed message; no flight-specific
    logic at all."""
    future = session.await_reply()
    await session.send({"type": "clarification_needed", "question": question})
    return await future


@pytest.mark.asyncio
async def test_orchestrator_session_survives_a_real_pause_with_interleaved_ws_traffic():
    # A real websockets server running the real, unmodified per-connection
    # handler - port 0 picks an ephemeral free port so this can't collide
    # with a real agent_server.py instance already running on 8766.
    server = await websockets.serve(agent_server.agent_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as client:
            # agent_handler adds the session to _connected_sessions BEFORE
            # sending wakeword_status - receiving this message is the real
            # synchronization point that guarantees the session already
            # exists, not a sleep-and-hope.
            await client.recv()
            assert len(agent_server._connected_sessions) == 1
            session = next(iter(agent_server._connected_sessions))

            # Turn 1: a real orchestrator session, kept alive for the whole
            # test (not recreated per message the way _handle_command does
            # for an ordinary command today - Stage 2 will need to do the
            # same real thing for a genuine clarification flow).
            runner = InMemoryRunner(agent=orchestrator_agent, app_name=APP_NAME)
            orch_session = await runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
            turn1 = await asyncio.wait_for(
                run_command(
                    runner,
                    orch_session.id,
                    "Remember this exact phrase for later: the secret code is 'purple hedgehog'. "
                    "Just confirm you'll remember it - don't do anything else.",
                ),
                timeout=60,
            )
            assert turn1 is None, "expected a conversational reply (no plan) for an acknowledgement"

            # Start a real pause, using the exact primitive Stage 2 will
            # use for real - not resolved yet.
            pause_task = asyncio.create_task(_pause_for_clarification(session, "test question - ignore"))
            clarification_needed = json.loads(await client.recv())
            assert clarification_needed == {"type": "clarification_needed", "question": "test question - ignore"}

            # Real interleaved traffic, sent to the real server while the
            # pause is genuinely outstanding - the exact realistic shapes
            # already flowing on this connection type (see agent_server.py's
            # dispatch): a mic handoff toggle, a TTS state toggle, a ping.
            await client.send(json.dumps({"type": "mic_state", "active": True}))
            await client.send(json.dumps({"type": "tts_state", "speaking": True}))
            await client.send(json.dumps({"type": "ping"}))
            pong = json.loads(await client.recv())
            assert pong == {"type": "pong", "server": "jarvis-agent"}, (
                "the server's own dispatch loop must keep answering unrelated messages "
                "while a pause is outstanding, not stall behind it"
            )
            await client.send(json.dumps({"type": "mic_state", "active": False}))
            await client.send(json.dumps({"type": "tts_state", "speaking": False}))

            # The pause must still be genuinely unresolved - interleaved
            # traffic must not have accidentally resolved or corrupted it.
            assert not pause_task.done(), "interleaved traffic resolved or broke the pending pause"

            # Resolve it for real, the same way a real clarification_response
            # would.
            await client.send(json.dumps({"type": "clarification_response", "text": "acknowledged, continue"}))
            answer = await asyncio.wait_for(pause_task, timeout=10)
            assert answer == "acknowledged, continue"

            # Turn 2, into the SAME session, after the real pause and the
            # real interleaved traffic - the actual question this test
            # exists to answer. run_command only returns a parsed plan (or
            # None); the conversational reply text itself is only ever
            # emitted via on_event (see main.py) - captured the same way
            # agent_server.py's real WS client always does.
            replies: list[str] = []

            async def _capture(payload):
                if payload.get("type") == "reply":
                    replies.append(payload.get("text", ""))

            turn2 = await asyncio.wait_for(
                run_command(
                    runner,
                    orch_session.id,
                    "What was the secret code I told you to remember? Say only the code.",
                    on_event=_capture,
                ),
                timeout=60,
            )
            assert turn2 is None
            assert replies, "expected a conversational reply"
            assert "purple hedgehog" in replies[-1].lower(), (
                "orchestrator lost conversational context across the pause - expected the real, "
                f"earlier fact to still be remembered, got: {replies[-1]!r}"
            )
    finally:
        server.close()
        await server.wait_closed()
