"""Agent Server - the Electron frontend's counterparty.

A WebSocket server on a loopback port that exposes the existing
Orchestrator -> Planner -> Action pipeline to the UI. Until now that
pipeline only ran from `main.py`'s command-line harness, where the approval
gate was an `input()` call and "state" was printed text. Neither of those
is usable from a real UI, so this file provides the same pipeline with two
things changed:

- **Structured events instead of stdout.** `main.run_command` /
  `main.run_action` take an optional `on_event` callback (defaulting to
  None, so the CLI behaves exactly as before); this server passes one that
  forwards each pipeline event to the connected client as JSON.
- **A real approval gate instead of a simulated one.** When the pipeline
  reaches a `requires_approval` milestone it sends `approval_required` and
  then *awaits an asyncio.Future* that only resolves when the client sends
  back an `approval_response`. The milestone is never executed until a real
  human decision arrives - and on rejection it is never executed at all.

Deliberately mirrors `browser_bridge_server.py`'s shape (loopback
`websockets` server, JSON messages, one handler coroutine) so there is one
pattern in this codebase, not two. Both servers run as in-process asyncio
tasks on the same event loop, which is a hard requirement rather than a
convenience: `browser/bridge.py`'s asyncio.Events are only awaitable from
the loop that created them, and the Action agent's browser tools await
them from inside this server's request handling.

Protocol (JSON, one message per WebSocket frame):

  client -> server
    {"type": "ping"}
    {"type": "audio", "sample_rate": int, "sample_width": int,
     "pcm_base64": str}          raw little-endian int16 mono PCM
    {"type": "text", "text": str}   typed command, for testing without a mic
    {"type": "approval_response", "approved": bool}
    {"type": "cancel"}

  server -> client
    {"type": "pong", "server": "jarvis-agent"}
    {"type": "state", "state": "thinking"|"doing"|"done"|"idle"}
    {"type": "transcript", "text": str}
    {"type": "plan", "milestones": [...]}
    {"type": "milestone_start"|"milestone_done", "step_number", "goal"}
    {"type": "tool_call", "tool": str}
    {"type": "tool_result", "tool": str, "success": bool, "message": str}
    {"type": "agent_text", "text": str}
    {"type": "reply", "text": str}        conversational, no plan produced
    {"type": "approval_required", "milestone": {...}}
    {"type": "approval_result", "approved": bool}
    {"type": "error", "message": str}
"""

import asyncio
import base64
import json
import logging
import os
import sys
from http import HTTPStatus
from typing import Any, Dict, Optional

import websockets
from google.adk.runners import InMemoryRunner

_backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from agents.action import action_agent  # noqa: E402
from agents.orchestrator import orchestrator_agent  # noqa: E402
from agents.planner import MilestonePlan  # noqa: E402
from main import APP_NAME, USER_ID, run_action, run_command  # noqa: E402
from voice.stt import TranscriptionError, transcribe_audio  # noqa: E402

logger = logging.getLogger(__name__)

# Cloud Run injects PORT (and K_SERVICE). When PORT is set we're in a
# container that must accept connections from outside it, so bind 0.0.0.0;
# locally we stay on loopback so nothing off the machine can reach the
# pipeline. JARVIS_AGENT_HOST/PORT still override either way.
_IN_CONTAINER = bool(os.environ.get("PORT"))
AGENT_HOST = os.environ.get("JARVIS_AGENT_HOST", "0.0.0.0" if _IN_CONTAINER else "127.0.0.1")
AGENT_PORT = int(os.environ.get("PORT") or os.environ.get("JARVIS_AGENT_PORT") or "8766")

# On Cloud Run there is no browser and no Electron client - only the
# Gemini-facing agent pipeline is exercised, reachable for a health check.
# K_SERVICE is always set by Cloud Run.
_CLOUD_RUN = bool(os.environ.get("K_SERVICE"))

# websockets defaults max_size to 1 MiB, which real captures exceed: mono
# int16 at the mic's native 48 kHz is ~94 KB/s raw, ~125 KB/s base64-encoded,
# so anything past ~8 seconds would be rejected outright as too large. Raised
# well past any plausible push-to-talk clip. (Measured, not guessed - see
# planning.md.)
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


class ClientSession:
    """Per-connection state: the socket, and the one pending approval the
    pipeline may currently be blocked on."""

    def __init__(self, websocket):
        self.websocket = websocket
        self._pending_approval: Optional[asyncio.Future] = None
        self._task: Optional[asyncio.Task] = None

    async def send(self, payload: Dict[str, Any]) -> None:
        try:
            await self.websocket.send(json.dumps(payload))
        except websockets.exceptions.ConnectionClosed:
            logger.info("agent server: client gone, dropping event %s", payload.get("type"))

    def await_approval(self) -> asyncio.Future:
        """Creates the Future the pipeline blocks on. Resolved by
        `resolve_approval` when the client's decision arrives."""
        loop = asyncio.get_running_loop()
        self._pending_approval = loop.create_future()
        return self._pending_approval

    def resolve_approval(self, approved: bool) -> bool:
        future = self._pending_approval
        if future is None or future.done():
            return False
        future.set_result(approved)
        self._pending_approval = None
        return True

    def cancel_pending(self) -> None:
        """A disconnect mid-approval must not leave the pipeline blocked
        forever - resolve it as a rejection so the run unwinds cleanly."""
        if self._pending_approval is not None and not self._pending_approval.done():
            self._pending_approval.set_result(False)
            self._pending_approval = None
        if self._task is not None and not self._task.done():
            self._task.cancel()


async def _run_plan(session: ClientSession, plan: MilestonePlan) -> None:
    """Executes a plan's milestones in order, pausing at every
    `requires_approval` one until a real client decision arrives.

    The gate lives here, in the loop that decides which milestone runs next
    - not inside the Action agent or a tool - for the same reason it did in
    `main.run_milestones_until_approval`: the agent should not be trusted to
    police its own execution. The only change from the CLI version is what
    the pause waits on: a client message instead of a keypress.
    """
    action_runner = InMemoryRunner(agent=action_agent, app_name=APP_NAME)
    action_session = await action_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)

    await session.send({"type": "state", "state": "doing"})

    for milestone in plan.milestones:
        if milestone.requires_approval:
            logger.info("agent server: awaiting real approval for %r", milestone.goal)
            await session.send(
                {
                    "type": "approval_required",
                    "milestone": {
                        "step_number": milestone.step_number,
                        "goal": milestone.goal,
                        "success_signal": milestone.success_signal,
                    },
                }
            )
            approved = await session.await_approval()
            await session.send({"type": "approval_result", "approved": approved})
            if not approved:
                logger.info("agent server: user REJECTED %r - not executing it", milestone.goal)
                await session.send(
                    {"type": "state", "state": "done", "reason": "rejected", "goal": milestone.goal}
                )
                return

        await run_action(action_runner, action_session.id, milestone, on_event=session.send)

    await session.send({"type": "state", "state": "done", "reason": "completed"})


async def _handle_command(session: ClientSession, text: str) -> None:
    """One full command: Orchestrator -> Planner -> (approval gate) -> Action."""
    try:
        await session.send({"type": "state", "state": "thinking"})

        orchestrator_runner = InMemoryRunner(agent=orchestrator_agent, app_name=APP_NAME)
        orch_session = await orchestrator_runner.session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID
        )
        plan = await run_command(orchestrator_runner, orch_session.id, text, on_event=session.send)

        if plan is None:
            # Conversational input the Orchestrator answered itself - the
            # `reply` event was already emitted by run_command.
            await session.send({"type": "state", "state": "done", "reason": "conversational"})
            return

        await _run_plan(session, plan)

    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface the real failure to the UI
        logger.exception("agent server: command failed")
        await session.send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        await session.send({"type": "state", "state": "done", "reason": "error"})


async def _transcribe(session: ClientSession, data: Dict[str, Any]) -> Optional[str]:
    """Decodes raw PCM from the renderer and runs it through the same
    `transcribe_audio` the Python capture path uses.

    The renderer sends the mic's *native* sample rate with no resampling
    anywhere - Google's endpoint accepts anything >= 8 kHz - so what reaches
    Google here is bit-identical to what the AudioWorklet captured.
    """
    import speech_recognition as sr

    try:
        pcm = base64.b64decode(data.get("pcm_base64", ""))
    except Exception as exc:  # noqa: BLE001
        await session.send({"type": "error", "message": f"bad audio payload: {exc}"})
        return None

    sample_rate = int(data.get("sample_rate", 48000))
    sample_width = int(data.get("sample_width", 2))
    if not pcm:
        await session.send({"type": "error", "message": "empty audio payload - nothing was captured"})
        return None

    seconds = len(pcm) / max(1, sample_rate * sample_width)
    logger.info("agent server: got %d bytes of PCM (~%.1fs @ %dHz)", len(pcm), seconds, sample_rate)

    audio = sr.AudioData(pcm, sample_rate, sample_width)
    try:
        # Blocking HTTP call to Google - off the event loop so the browser
        # bridge (sharing this loop) keeps servicing the extension.
        transcript = await asyncio.to_thread(transcribe_audio, audio)
    except TranscriptionError as exc:
        await session.send({"type": "error", "message": f"transcription failed: {exc}"})
        return None

    await session.send({"type": "transcript", "text": transcript})
    return transcript


async def agent_handler(websocket):
    logger.info("agent server: UI client connected")
    session = ClientSession(websocket)

    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await session.send({"type": "error", "message": "Invalid JSON payload"})
                continue

            msg_type = data.get("type")

            if msg_type == "ping":
                await session.send({"type": "pong", "server": "jarvis-agent"})
                continue

            if msg_type == "approval_response":
                approved = bool(data.get("approved"))
                if not session.resolve_approval(approved):
                    await session.send(
                        {"type": "error", "message": "No approval is currently pending."}
                    )
                continue

            if msg_type == "cancel":
                session.cancel_pending()
                await session.send({"type": "state", "state": "idle", "reason": "cancelled"})
                continue

            if msg_type in ("audio", "text"):
                if session._task is not None and not session._task.done():
                    await session.send(
                        {"type": "error", "message": "A command is already running."}
                    )
                    continue

                if msg_type == "audio":
                    transcript = await _transcribe(session, data)
                    if not transcript:
                        await session.send({"type": "state", "state": "idle", "reason": "no_transcript"})
                        continue
                else:
                    transcript = str(data.get("text", "")).strip()
                    if not transcript:
                        await session.send({"type": "error", "message": "empty text command"})
                        continue
                    await session.send({"type": "transcript", "text": transcript})

                # Run the pipeline as a task so the handler loop stays free
                # to receive the approval_response that the pipeline will
                # block on. Awaiting it inline would deadlock: the pipeline
                # waits for a message this loop is no longer reading.
                session._task = asyncio.create_task(_handle_command(session, transcript))
                continue

            await session.send({"type": "error", "message": f"Unknown message type: {msg_type}"})

    except websockets.exceptions.ConnectionClosed as exc:
        logger.info("agent server: UI client disconnected (%s)", exc)
    finally:
        session.cancel_pending()


_HEALTH_BODY = '{"status": "ok", "service": "jarvis-agent"}\n'


def _health_check(connection, request):
    """Answers plain HTTP GETs before the WebSocket upgrade, so Cloud Run
    (and anyone opening the URL in a browser) gets a real liveness signal.
    Returning None lets the request fall through to the WebSocket handshake.
    """
    if request.path in ("/health", "/healthz", "/"):
        return connection.respond(HTTPStatus.OK, _HEALTH_BODY)
    return None


async def serve_forever() -> None:
    """Runs the agent server until cancelled."""
    async with websockets.serve(
        agent_handler,
        AGENT_HOST,
        AGENT_PORT,
        origins=None,
        max_size=MAX_MESSAGE_BYTES,
        process_request=_health_check,
    ):
        logger.info("agent server: listening on %s:%s (ws + GET /health)", AGENT_HOST, AGENT_PORT)
        await asyncio.Future()  # run until cancelled


async def _main() -> None:
    """Runs the agent server alongside the browser bridge, in one process on
    one event loop - required so the Action agent's browser tools can await
    the bridge's asyncio.Events. Both task references are held in locals that
    stay alive for the process lifetime; see planning.md's gc entry for why
    a discarded task reference silently dies."""
    from servers.browser_bridge_server import serve_forever as serve_bridge

    bridge_task = asyncio.create_task(serve_bridge())
    agent_task = asyncio.create_task(serve_forever())
    await asyncio.gather(bridge_task, agent_task)


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if _CLOUD_RUN:
        # No browser bridge in the cloud - nothing to bridge to. Just the
        # agent server, so the pipeline is reachable and the health check
        # answers.
        logger.info("agent server: Cloud Run mode (K_SERVICE=%s) - agent server only", os.environ.get("K_SERVICE"))
        asyncio.run(serve_forever())
    else:
        asyncio.run(_main())
