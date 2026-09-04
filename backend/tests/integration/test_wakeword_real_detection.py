"""A real wake-word detection test - real microphone, real openWakeWord
model, real audio played through the actual speakers. This is the same
real test performed manually when openWakeWord was first built (see
planning.md), formalized here rather than left as something only ever
checked by hand.

Not a unit test, deliberately: mocking the model/audio pipeline convincingly
enough to prove "Hey Jarvis" is actually recognized would mean faking the
one thing this test exists to check - so this is exactly what
tests/integration/ is for. Needs a real Mac with a working microphone and
speakers, and takes a few real seconds (model load + a short spoken
phrase).

A real precondition worth stating plainly, found by this test failing
honestly the first time it ran: system output must not be muted. A muted
Mac plays `afplay`'s audio nowhere, so the real microphone hears nothing
and detection correctly never fires - not a bug in this test or in
openWakeWord, just a real environment precondition, the same category as
the Reminders test below needing Automation access already granted.
"""

import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from voice.wakeword import WakeWordListener

pytestmark = pytest.mark.integration


def _require_unmuted_output() -> None:
    """A real precondition this test found by failing honestly, twice, on
    this exact machine: something outside this project (observed both
    muting output entirely and separately dropping it to 0) intermittently
    changes system output volume during a real session. When that happens,
    `afplay`'s audio reaches nowhere, the real microphone hears nothing,
    and detection correctly never fires - indistinguishable, from inside
    this test, from a real detection bug unless checked explicitly. Skips
    with the real reason rather than let that read as a code failure.
    """
    result = subprocess.run(
        ["osascript", "-e", "get volume settings"], capture_output=True, text=True, timeout=5
    )
    settings = result.stdout.strip()
    if "output muted:true" in settings or "output volume:0" in settings:
        pytest.skip(f"system output is muted/silent in this environment ({settings}) - can't reach the real mic")


def _synthesize_speech(text: str, wav_path: Path) -> None:
    aiff_path = wav_path.with_suffix(".aiff")
    subprocess.run(["say", "-o", str(aiff_path), text], check=True, timeout=15)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff_path), str(wav_path)],
        check=True,
        timeout=15,
    )
    aiff_path.unlink(missing_ok=True)


def test_real_speech_through_the_speakers_triggers_real_detection(tmp_path):
    _require_unmuted_output()
    listener = WakeWordListener(on_detected=lambda score: None)
    if listener.unavailable_reason:
        pytest.skip(f"wake word unavailable in this environment: {listener.unavailable_reason}")

    detected = threading.Event()
    scores = []

    def on_detected(score: float) -> None:
        scores.append(score)
        detected.set()

    listener = WakeWordListener(on_detected=on_detected)
    started = listener.start()
    assert started, "expected the real model/mic to start cleanly"

    try:
        time.sleep(1.0)  # let the input stream actually open before speaking
        wav_path = tmp_path / "hey_jarvis.wav"
        _synthesize_speech("Hey Jarvis", wav_path)
        subprocess.run(["afplay", str(wav_path)], check=True, timeout=15)

        fired = detected.wait(timeout=8.0)
        assert fired, "real 'Hey Jarvis' speech through the speakers did not trigger detection"
        assert scores[0] >= 0.5  # the same real threshold the listener itself uses
    finally:
        listener.stop()


def test_pause_genuinely_stops_real_detection_not_just_the_flag(tmp_path):
    """The real-hardware half of the pause() guarantee already unit-tested
    in test_wakeword_listener.py: while paused, real speech through the
    speakers must produce zero detections, because the microphone is
    actually closed - not merely "ignored" while still open."""
    _require_unmuted_output()  # muted audio would trivially "pass" this without proving anything real
    listener = WakeWordListener(on_detected=lambda score: None)
    if listener.unavailable_reason:
        pytest.skip(f"wake word unavailable in this environment: {listener.unavailable_reason}")

    events = []
    listener = WakeWordListener(on_detected=lambda score: events.append(score))
    assert listener.start()

    try:
        time.sleep(0.5)
        listener.pause()
        time.sleep(0.5)
        assert listener._stream is None, "pause() must fully close the input stream, not just stop predicting"

        wav_path = tmp_path / "hey_jarvis_paused.wav"
        _synthesize_speech("Hey Jarvis", wav_path)
        subprocess.run(["afplay", str(wav_path)], check=True, timeout=15)
        time.sleep(1.0)

        assert events == [], "no detection should fire while genuinely paused"
    finally:
        listener.stop()
