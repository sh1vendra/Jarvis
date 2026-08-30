"""Push-to-talk microphone capture for Jarvis.

Deliberately minimal: a keyboard-driven recorder whose only job is to prove
the STT mechanism works against real microphone audio. The real trigger in
the shipped product is an Electron global hotkey (a frontend concern, built
later); this is the backend-only stand-in for it - press Enter to start,
press Enter again to stop.

Uses `sounddevice`, whose wheel bundles its own PortAudio binary, so there
is no Homebrew `portaudio` dependency to install. Records raw int16 PCM
frames and returns a `speech_recognition.AudioData` that drops straight
into `voice.stt.transcribe_audio`.

First run triggers a one-time macOS microphone-permission prompt for
whichever process is hosting Python (Terminal, iTerm, or the IDE). If it's
denied the recording comes back silent; grant it under System Settings >
Privacy & Security > Microphone and re-run.
"""

from __future__ import annotations

import queue

import sounddevice as sd
import speech_recognition as sr

from .stt import SAMPLE_RATE, SAMPLE_WIDTH

_START_PROMPT = "Press Enter to start recording..."
_STOP_PROMPT = "Recording... press Enter to stop."


def record_push_to_talk(*, sample_rate: int = SAMPLE_RATE, device: int | None = None) -> sr.AudioData:
    """Record from the microphone between two Enter presses and return the
    captured audio as an `AudioData` (16 kHz mono 16-bit PCM by default).

    `device` is a `sounddevice` input-device index; `None` uses the system
    default input.
    """
    chunks: "queue.Queue[bytes]" = queue.Queue()

    def callback(indata, frame_count, time_info, status) -> None:
        # Runs on PortAudio's own thread - just stash the bytes and get out.
        if status:
            print(f"[capture] stream status: {status}")
        chunks.put(bytes(indata))

    input(_START_PROMPT)
    with sd.RawInputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
        device=device,
        callback=callback,
    ):
        input(_STOP_PROMPT)

    raw = b"".join(_drain(chunks))
    seconds = len(raw) / (sample_rate * SAMPLE_WIDTH) if raw else 0.0
    print(f"[capture] captured {len(raw)} bytes (~{seconds:.1f}s of audio)")
    return sr.AudioData(raw, sample_rate, SAMPLE_WIDTH)


def _drain(q: "queue.Queue[bytes]") -> list[bytes]:
    items: list[bytes] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


if __name__ == "__main__":
    # Manual check: record a clip, transcribe it, print what Google heard.
    from .stt import TranscriptionError, transcribe_audio

    audio = record_push_to_talk()
    try:
        print("transcript:", repr(transcribe_audio(audio)))
    except TranscriptionError as exc:
        print("transcription failed:", exc)
