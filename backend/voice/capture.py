"""Push-to-talk microphone capture for Jarvis.

Jarvis is voice-first - real spoken audio is the product's only entry point.
This module is the backend stand-in for the trigger: press Enter to start
recording, press Enter again to stop. The shipped trigger is an Electron
global hotkey (frontend, built later); nothing downstream of here cares how
recording was started.

Uses `sounddevice`, whose wheel bundles its own PortAudio binary, so there
is no Homebrew `portaudio` dependency. Records raw int16 PCM frames at the
input device's native sample rate and returns a
`speech_recognition.AudioData` that drops straight into
`voice.stt.transcribe_audio` (Google's endpoint accepts any rate >= 8 kHz,
so there's no need to force-resample on the way in).

First run triggers a one-time macOS microphone-permission prompt for
whichever process is hosting Python (Terminal, iTerm, or the IDE). If it's
denied the recording comes back silent (all-zero samples); grant it under
System Settings > Privacy & Security > Microphone and re-run.
"""

from __future__ import annotations

import queue

import sounddevice as sd
import speech_recognition as sr

from .stt import SAMPLE_RATE, SAMPLE_WIDTH

_START_PROMPT = "Press Enter to START recording..."
_STOP_PROMPT = "  recording... speak now, then press Enter to STOP."


def list_input_devices() -> str:
    """Human-readable list of every device that can record, for picking a
    `--device` index when the default isn't the one you want."""
    lines = ["Input-capable audio devices:"]
    for index, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            lines.append(
                f"  [{index}] {dev['name']} "
                f"({dev['max_input_channels']} ch, {int(round(dev['default_samplerate']))} Hz)"
            )
    return "\n".join(lines)


def _resolve_input_device(device: int | None) -> int:
    """Turn `None` into a concrete device index, and make sure whatever we
    land on can actually record. The system default input changes at
    runtime (plugging in AirPods makes them the default), so this is
    resolved fresh on every call rather than cached."""
    if device is not None:
        info = sd.query_devices(device)
        if info["max_input_channels"] < 1:
            raise ValueError(f"device {device} ({info['name']!r}) has no input channels")
        return device

    try:
        info = sd.query_devices(kind="input")
        if info["max_input_channels"] > 0:
            return info["index"]
    except Exception:
        pass

    for index, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            return index
    raise RuntimeError("no input-capable audio device found")


def record_push_to_talk(*, sample_rate: int | None = None, device: int | None = None) -> sr.AudioData:
    """Record from the microphone between two Enter presses and return the
    captured audio as an `AudioData`.

    `device` is a `sounddevice` input-device index (`None` = system default,
    resolved fresh). `sample_rate` defaults to the device's native rate;
    override only if you know the device rejects it.
    """
    device = _resolve_input_device(device)
    dev_info = sd.query_devices(device)
    if sample_rate is None:
        sample_rate = int(round(dev_info["default_samplerate"])) or SAMPLE_RATE

    print(f"[capture] input device [{device}] {dev_info['name']!r} @ {sample_rate} Hz")

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
    peak = _peak_amplitude(raw)
    print(f"[capture] captured {len(raw)} bytes (~{seconds:.1f}s), peak amplitude {peak}/32767")
    if peak == 0:
        print("[capture] WARNING: audio is completely silent - mic permission denied, "
              "or the wrong input device. See `python main.py --list-devices`.")
    return sr.AudioData(raw, sample_rate, SAMPLE_WIDTH)


def _drain(q: "queue.Queue[bytes]") -> list[bytes]:
    items: list[bytes] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def _peak_amplitude(raw: bytes) -> int:
    """Largest absolute int16 sample - a cheap "did the mic actually hear
    anything" check without pulling in numpy."""
    if not raw:
        return 0
    import struct

    count = len(raw) // 2
    return max((abs(s) for s in struct.unpack(f"<{count}h", raw[: count * 2])), default=0)


if __name__ == "__main__":
    # Manual check: record a clip, transcribe it, print what Google heard.
    from .stt import TranscriptionError, transcribe_audio

    audio = record_push_to_talk()
    try:
        print("transcript:", repr(transcribe_audio(audio)))
    except TranscriptionError as exc:
        print("transcription failed:", exc)
