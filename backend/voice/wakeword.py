"""Wake-word detection ("Hey Jarvis") via openWakeWord, running on the
backend - not Electron's main process the way the earlier Porcupine attempt
did (`frontend/electron/wakeword.cjs`, kept but no longer the active
detector - see planning.md).

Why the backend, not Electron main: Porcupine ships prebuilt N-API .node
addons that load directly into Electron's main process. openWakeWord is a
Python/onnxruntime library with no Node binding - it has no way to run
inside Electron at all - so detection has to live wherever Python already
runs, which is this backend process. That has a real architectural
consequence: the wake-word *trigger* can no longer reach the renderer via
Electron's IPC (`jarvis:hotkey`) the way the hotkey does, since this
process isn't Electron's main - it has to travel over the existing
WebSocket connection instead (see `servers/agent_server.py`'s
`wakeword_detected` message and `App.jsx`'s handler for it, which calls the
exact same `beginCapture(source)` convergence point the hotkey already
uses).

Confirmed directly (not assumed) before writing this module:
- `Model(wakeword_models=["hey_jarvis_v0.1"], inference_framework="onnx")`
  loads in ~80ms; `hey_jarvis_v0.1.onnx` ships inside the openwakeword
  package itself (`resources/models/`), no download/account/API key needed.
- `predict()` on a 1280-sample (80ms @ 16kHz) int16 chunk takes ~1.4ms -
  comfortably real-time.
- Real synthesized speech (`say -o ... "Hey Jarvis"`, converted to 16kHz
  mono PCM the same way this module chunks live audio) scores 0.98 for
  "Hey Jarvis" and 0.999 for "Hey Jarvis, what's the weather", against
  0.000008 for unrelated speech ("Please play some music on Spotify") -
  a wide margin either side of the 0.5 default threshold.
- `sd.RawInputStream(samplerate=16000, blocksize=1280, ...)` really does
  deliver exactly 1280-sample callbacks (confirmed: 24 callbacks over 2s of
  real mic input, every one exactly 2560 bytes) - no manual buffering of
  partial frames needed.

Mic-contention handling deliberately mirrors `wakeword.cjs`'s already-
verified pattern, not a new design: `pause()`/`resume()` fully close and
reopen the actual input stream, the same "whichever one has the mic
releases it fully" rule Porcupine's implementation already established -
not just "ignore incoming audio while still holding the device open."
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

try:
    import numpy as np
    import sounddevice as sd
    from openwakeword.model import Model
except ImportError as exc:  # pragma: no cover - exercised on Cloud Run, no mic/deps there
    np = None
    sd = None
    Model = None
    _IMPORT_ERROR: Exception | None = exc
else:
    _IMPORT_ERROR = None

_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 1280  # 80ms @ 16kHz - openWakeWord's expected chunk size, confirmed above
_MODEL_NAME = "hey_jarvis_v0.1"
_DEFAULT_THRESHOLD = 0.5
_QUEUE_GET_TIMEOUT = 0.5  # how often the consumer loop re-checks _running/_capturing
_PAUSED_POLL_INTERVAL = 0.2
_STREAM_RETRY_DELAY = 1.0


class WakeWordListener:
    """Continuously listens for "Hey Jarvis" on a background thread and
    calls `on_detected(score)` from that thread - NOT the asyncio event
    loop. A caller that needs to touch asyncio/WebSocket state from the
    callback must hop back onto the loop itself (e.g.
    `loop.call_soon_threadsafe`), the same way `agent_server.py` does.

    Lifecycle: `start()`/`stop()` own the whole thing (model load, thread,
    stream). `pause()`/`resume()` are the mic-handoff calls - `pause()`
    fully closes the input stream so a concurrent renderer-side command
    capture never has to contend with this process for the same input
    device; `resume()` reopens it. Safe to call pause()/resume() from any
    thread (only sets/clears a threading.Event).
    """

    def __init__(
        self,
        on_detected: Callable[[float], None],
        threshold: float = _DEFAULT_THRESHOLD,
        device: int | None = None,
    ) -> None:
        self._on_detected = on_detected
        self._threshold = threshold
        self._device = device
        self._model = None
        self._stream = None
        self._audio_queue: "queue.Queue[bytes]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        self._capturing = threading.Event()

    @property
    def unavailable_reason(self) -> str | None:
        """Why wake word can't run, or None if it can - checked before
        start() so the caller can report an honest status either way."""
        if _IMPORT_ERROR is not None:
            return f"openwakeword/sounddevice/numpy not available: {_IMPORT_ERROR}"
        return None

    def start(self) -> bool:
        """Loads the model and starts listening. Returns True only if it
        actually started - False (with the reason logged) if the required
        packages aren't available or the model failed to load, in which
        case wake word is simply off and the hotkey still works."""
        if self._running:
            return True
        reason = self.unavailable_reason
        if reason:
            logger.info("wake word: unavailable - %s", reason)
            return False
        try:
            self._model = Model(wakeword_models=[_MODEL_NAME], inference_framework="onnx")
        except Exception:
            logger.exception("wake word: failed to load the openWakeWord model")
            return False

        self._running = True
        self._capturing.set()
        self._thread = threading.Thread(target=self._run, name="wakeword-listener", daemon=True)
        self._thread.start()
        logger.info("wake word: listening for %r (threshold=%.2f)", _MODEL_NAME, self._threshold)
        return True

    def stop(self) -> None:
        self._running = False
        self._capturing.set()  # wake the loop if it's currently parked paused
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._close_stream()
        self._thread = None
        self._model = None

    def pause(self) -> None:
        """Fully releases the microphone - call this before/while the
        renderer holds it for a command capture, from any source
        (hotkey or wake word)."""
        logger.info("wake word: pausing (releasing the microphone)")
        self._capturing.clear()

    def resume(self) -> None:
        """Re-acquires the microphone - call once the renderer's capture
        has ended and released it."""
        logger.info("wake word: resuming (re-acquiring the microphone)")
        self._capturing.set()

    # -- internals, all on the background thread except pause()/resume() --

    def _run(self) -> None:
        while self._running:
            if not self._capturing.is_set():
                self._close_stream()
                self._capturing.wait(timeout=_PAUSED_POLL_INTERVAL)
                continue

            if self._stream is None:
                try:
                    self._open_stream()
                except Exception:
                    logger.exception("wake word: failed to open input stream")
                    time.sleep(_STREAM_RETRY_DELAY)
                    continue

            try:
                chunk = self._audio_queue.get(timeout=_QUEUE_GET_TIMEOUT)
            except queue.Empty:
                continue
            if not self._capturing.is_set():
                continue  # paused during the wait - drop this frame, don't predict on it

            audio = np.frombuffer(chunk, dtype=np.int16)
            if audio.shape[0] != _FRAME_SAMPLES:
                continue  # a short/odd chunk (e.g. right at stream startup) - skip rather than crash

            try:
                scores = self._model.predict(audio)
            except Exception:
                logger.exception("wake word: predict() failed")
                continue

            score = float(scores.get(_MODEL_NAME, 0.0))
            if score >= self._threshold:
                logger.info("wake word: detected %r (score=%.3f)", _MODEL_NAME, score)
                self._model.reset()
                self._drain_queue()  # don't let a queued backlog re-trigger on the same utterance
                try:
                    self._on_detected(score)
                except Exception:
                    logger.exception("wake word: on_detected callback failed")

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        # Runs on PortAudio's own realtime thread - stash the bytes and get
        # out, exactly the pattern `voice/capture.py` already uses; the
        # actual predict() work happens on the consumer thread instead.
        if status:
            logger.warning("wake word: stream status: %s", status)
        self._audio_queue.put(bytes(indata))

    def _open_stream(self) -> None:
        self._drain_queue()
        self._stream = sd.RawInputStream(
            samplerate=_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=_FRAME_SAMPLES,
            device=self._device,
            callback=self._audio_callback,
        )
        self._stream.start()
        logger.info("wake word: input stream opened - microphone acquired")

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
                logger.info("wake word: input stream closed - microphone released")
            except Exception:
                logger.exception("wake word: error closing input stream")
            self._stream = None

    def _drain_queue(self) -> None:
        while True:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break
