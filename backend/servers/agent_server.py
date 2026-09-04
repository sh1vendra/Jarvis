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

Also owns wake-word ("Hey Jarvis") detection - see `voice/wakeword.py` for
why that lives here rather than in Electron's main process the way the
earlier Porcupine attempt did (`frontend/electron/wakeword.cjs`, kept but
no longer wired in - openWakeWord has no Node binding, so detection has to
run wherever Python already runs). The consequence: the wake-word trigger
can't use Electron's IPC (`jarvis:hotkey`) - it travels over this same
WebSocket instead (`wakeword_detected` below), and the renderer calls the
exact same `beginCapture(source)` convergence point the hotkey already
uses. `_set_mic_active()` is the mic-handoff choke point - every renderer
mic acquisition/release (hotkey- or wake-word-triggered, doesn't matter)
reports through `mic_state`, and the wake-word listener is fully paused/
resumed accordingly, so the two never contend for the microphone. Started
only outside Cloud Run (see `_main()`) - no microphone exists there.

Protocol (JSON, one message per WebSocket frame):

  client -> server
    {"type": "ping"}
    {"type": "audio", "sample_rate": int, "sample_width": int,
     "pcm_base64": str}          raw little-endian int16 mono PCM
    {"type": "text", "text": str}   typed command, for testing without a mic
    {"type": "approval_response", "approved": bool}
    {"type": "mic_state", "active": bool}   the renderer just acquired
                                  (true) or released (false) the
                                  microphone for a command capture,
                                  regardless of trigger source - pauses/
                                  resumes the backend's own wake-word mic
                                  capture so the two never contend
    {"type": "cancel"}

  server -> client
    {"type": "pong", "server": "jarvis-agent"}
    {"type": "state", "state": "thinking"|"doing"|"done"}
    {"type": "state", "state": "failed",
     "failed_goals": [{"goal": str, "message": str}, ...]}   a milestone's
                                       tools didn't verify; message is the
                                       Action agent's own explanation for why
                                       when it has one (e.g. a clarifying
                                       question it asked instead of guessing
                                       at an ambiguous Spotify result)
    {"type": "state", "state": "cancelled", "goal": str}   an approval gate
                                       was rejected before that step ran
    {"type": "transcript", "text": str}
    {"type": "plan", "milestones": [...]}
    {"type": "milestone_start"|"milestone_done", "step_number", "goal"}
    {"type": "tool_call", "tool": str}
    {"type": "tool_result", "tool": str, "success": bool, "message": str}
    {"type": "agent_text", "text": str}
    {"type": "reply", "text": str}        conversational, no plan produced
    {"type": "approval_required", "milestone": {...}}
    {"type": "approval_result", "approved": bool}
    {"type": "wakeword_status", "available": bool, "reason": str | None}
                                  sent once, right after connect - whether
                                  the backend's "Hey Jarvis" listener is
                                  actually running (e.g. False if
                                  openwakeword/sounddevice aren't installed)
    {"type": "wakeword_detected"}   "Hey Jarvis" heard - the renderer should
                                  start listening exactly as if the hotkey
                                  had just been pressed
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
from main import APP_NAME, USER_ID, run_action, run_command, summarize_plan  # noqa: E402
from memory import store as memory_store  # noqa: E402
from voice.stt import TranscriptionError, transcribe_audio  # noqa: E402
from voice.wakeword import WakeWordListener  # noqa: E402

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

# -- Wake word ("Hey Jarvis") - module-level, not per-connection, since -----
# detection runs continuously regardless of which/whether a client is
# connected at a given moment. See voice/wakeword.py for the listener
# itself; everything here is just wiring it to the WebSocket protocol.
_connected_sessions: set["ClientSession"] = set()
_wakeword_listener: Optional[WakeWordListener] = None
_wakeword_available = False
_wakeword_reason: Optional[str] = None
_mic_active = False  # true whenever ANY connected renderer currently holds the mic
_WAKEWORD_MIC_ACK_TIMEOUT = 3.0  # seconds to wait for a mic_state ack before un-pausing anyway


async def _broadcast(payload: Dict[str, Any]) -> None:
    for session in list(_connected_sessions):
        await session.send(payload)


def _set_mic_active(active: bool) -> None:
    """The one place mic ownership actually changes hands. Called both from
    `mic_state` messages (every renderer capture, hotkey- or wake-word-
    triggered) and from disconnect cleanup - the wake-word listener is
    fully paused/resumed here, never left to infer state on its own."""
    global _mic_active
    _mic_active = active
    if _wakeword_listener is None:
        return
    if active:
        logger.info("agent server: renderer acquired the mic - pausing wake word")
        _wakeword_listener.pause()
    else:
        logger.info("agent server: renderer released the mic - resuming wake word")
        _wakeword_listener.resume()


def _on_wakeword_detected(score: float, loop: asyncio.AbstractEventLoop) -> None:
    """Runs on the wake-word listener's own background thread (see
    voice/wakeword.py) - NOT the asyncio event loop, so this must hop back
    onto the loop via run_coroutine_threadsafe rather than touching
    websockets/asyncio state directly.

    Pauses the listener immediately, before even scheduling the broadcast -
    mirrors wakeword.cjs's startListening(), which calls
    wakeWord.pauseCapture() before sending the IPC trigger, for the same
    reason: close the mic-contention window as early as possible rather
    than waiting for the renderer's own acknowledgment to arrive.
    """
    logger.info("agent server: wake word detected (score=%.3f)", score)
    if _wakeword_listener is not None:
        _wakeword_listener.pause()
    asyncio.run_coroutine_threadsafe(_handle_wakeword_detected(), loop)


async def _handle_wakeword_detected() -> None:
    await _broadcast({"type": "wakeword_detected"})
    # Safety net: if the renderer never confirms it actually acquired the
    # mic (disconnected, permission denied, crashed before reporting),
    # don't leave wake word paused forever - same "always recoverable"
    # standard the rest of this pipeline holds itself to. A mic_state
    # ack that DID arrive in time makes this a no-op (the real capture is
    # legitimately still using the mic); resume() then happens normally
    # when that capture's own mic_state(active=False) arrives.
    await asyncio.sleep(_WAKEWORD_MIC_ACK_TIMEOUT)
    if not _mic_active and _wakeword_listener is not None:
        logger.info("agent server: no mic_state ack after wake word detection - resuming wake word")
        _wakeword_listener.resume()


def start_wakeword_listener() -> None:
    """Starts the backend's "Hey Jarvis" listener. Call only outside Cloud
    Run (see `_main()`) - there is no microphone in that container, and
    voice/wakeword.py's own guarded imports mean this degrades to a clear
    "unavailable" rather than crashing even if called somewhere it
    shouldn't be, but it should never actually be reached there."""
    global _wakeword_listener, _wakeword_available, _wakeword_reason
    loop = asyncio.get_running_loop()
    listener = WakeWordListener(on_detected=lambda score: _on_wakeword_detected(score, loop))
    started = listener.start()
    _wakeword_listener = listener if started else None
    _wakeword_available = started
    _wakeword_reason = None if started else listener.unavailable_reason
    if started:
        logger.info("agent server: wake word listening for \"Hey Jarvis\"")
    else:
        logger.info("agent server: wake word unavailable - %s (hotkey still works)", _wakeword_reason)


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


async def _run_plan(session: ClientSession, plan: MilestonePlan) -> str:
    """Executes a plan's milestones in order, pausing at every
    `requires_approval` one until a real client decision arrives.

    The gate lives here, in the loop that decides which milestone runs next
    - not inside the Action agent or a tool - for the same reason it did in
    `main.run_milestones_until_approval`: the agent should not be trusted to
    police its own execution. The only change from the CLI version is what
    the pause waits on: a client message instead of a keypress.

    Returns one of:
      "completed" - every milestone that ran reported its tools succeeded
      "rejected"  - the user rejected an approval gate
      "failed"    - a milestone ran but its tools reported failure

    so the caller can record the honest outcome in command_history and the
    UI can show an honest terminal state - not a silent "done" over a run
    where nothing actually worked (see planning.md).
    """
    action_runner = InMemoryRunner(agent=action_agent, app_name=APP_NAME)
    action_session = await action_runner.session_service.create_session(app_name=APP_NAME, user_id=USER_ID)

    await session.send({"type": "state", "state": "doing"})

    failed_goals: list[dict] = []
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
                await session.send({"type": "state", "state": "cancelled", "goal": milestone.goal})
                return "rejected"

        ok, last_text = await run_action(action_runner, action_session.id, milestone, on_event=session.send)
        if not ok:
            # last_text carries the Action agent's own explanation for why -
            # e.g. a clarifying question it asked instead of guessing at an
            # ambiguous Spotify result (see planning.md) - so the UI can
            # show the real reason, not just the abstract goal that didn't
            # verify.
            failed_goals.append({"goal": milestone.goal, "message": last_text or ""})

    if failed_goals:
        logger.info("agent server: run FAILED - milestones did not verify: %s", failed_goals)
        await session.send({"type": "state", "state": "failed", "failed_goals": failed_goals})
        return "failed"

    await session.send({"type": "state", "state": "done", "reason": "completed"})
    return "completed"


async def _handle_command(session: ClientSession, text: str) -> None:
    """One full command: Orchestrator -> Planner -> (approval gate) -> Action.

    A real task command (one that produced a plan) is written to
    command_history on the way out, pass or fail. Conversational input that
    never produced a plan isn't a command Jarvis "executed", so it isn't
    logged.
    """
    plan_summary: Optional[str] = None
    success = False
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

        plan_summary = summarize_plan(plan)
        outcome = await _run_plan(session, plan)
        success = outcome == "completed"

    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface the real failure to the UI
        logger.exception("agent server: command failed")
        await session.send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        await session.send({"type": "state", "state": "failed", "reason": "error"})
    finally:
        if plan_summary is not None:
            # success reflects the real execution outcome ("completed"), not
            # "the pipeline didn't crash" - a run where the Action agent's
            # own tools failed logs as success=False.
            memory_store.log_command(text, plan_summary, success)
            logger.info("agent server: command logged to memory (success=%s)", success)


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
    _connected_sessions.add(session)
    await session.send({"type": "wakeword_status", "available": _wakeword_available, "reason": _wakeword_reason})

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

            if msg_type == "mic_state":
                _set_mic_active(bool(data.get("active")))
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
        _connected_sessions.discard(session)
        # A client that disconnects mid-capture must not leave wake word
        # paused forever - same reasoning as _handle_wakeword_detected's
        # timeout, just for the "the connection itself died" case.
        if not _connected_sessions:
            _set_mic_active(False)


_HEALTH_BODY = '{"status": "ok", "service": "jarvis-agent"}\n'


def _health_check(connection, request):
    """Answers plain HTTP GETs before the WebSocket upgrade, so Cloud Run
    (and anyone opening the URL in a browser) gets a real liveness signal.

    A real WebSocket client is let straight through on any path - the
    Electron app connects to the bare URL (path "/"), so "/" cannot be
    reserved for health. The tell is the `Upgrade: websocket` header:
    present -> it's the app, return None and proceed to the handshake;
    absent -> it's a browser or a probe, answer with the health body.
    """
    upgrade = (request.headers.get("Upgrade") or "").lower()
    if upgrade == "websocket":
        return None
    return connection.respond(HTTPStatus.OK, _HEALTH_BODY)


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
    a discarded task reference silently dies.

    Also where wake-word listening starts - deliberately only reached from
    the non-Cloud-Run branch of `__main__` below, never from `serve_forever`
    directly (that's what Cloud Run's branch calls). There is no
    microphone in that container; see planning.md for why this needed
    explicit verification, not just "it's gated so it's fine"."""
    from servers.browser_bridge_server import serve_forever as serve_bridge

    start_wakeword_listener()
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
