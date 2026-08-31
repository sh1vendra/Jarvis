"""Speech-to-text for Jarvis.

Wraps Google's free Speech Recognition endpoint via the `speech_recognition`
library (no API key, no billing - the library ships a generic key that works
out of the box; Google may rate-limit or revoke it, so this is fine for a
demo but not for production). This matches the approach documented in
Moonwalk's own architecture notes.

The pipeline that consumes a transcript (orchestrator -> planner -> action)
is being built and tested before a real microphone is wired in. To make that
possible, `transcribe_audio` accepts a `SimulatedAudio` object carrying a
known transcript and returns it verbatim - no network call, no audio - so
the rest of the chain can be exercised exactly as it will be with real
speech, just with the STT step stubbed at its output.
"""

from __future__ import annotations

import wave
from pathlib import Path

import speech_recognition as sr

# Mono 16-bit PCM. SAMPLE_WIDTH (bytes per sample) is fixed - int16 is what
# the capture layer records and what AudioData expects. SAMPLE_RATE is only
# a fallback default: real capture uses the input device's native rate, and
# Google's endpoint accepts anything >= 8 kHz, so the rate isn't pinned.
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2


class TranscriptionError(RuntimeError):
    """Raised when audio could not be transcribed - either the speech was
    unintelligible or the recognition request itself failed (no network,
    rate-limited, key revoked)."""


class SimulatedAudio:
    """Stand-in for real captured audio that carries a pre-known transcript.

    `transcribe_audio` returns `.transcript` unchanged when handed one of
    these, so the voice -> agent pipeline can be tested end to end without a
    microphone: it simulates "STT already ran, and this is what it returned."
    """

    def __init__(self, transcript: str) -> None:
        self.transcript = transcript

    def __repr__(self) -> str:
        return f"SimulatedAudio({self.transcript!r})"


def transcribe_audio(audio_data: "sr.AudioData | SimulatedAudio", *, language: str = "en-US") -> str:
    """Transcribe captured audio to text.

    Accepts either a real `speech_recognition.AudioData` (sent to Google's
    free endpoint) or a `SimulatedAudio` (whose transcript is returned
    directly). Returns the transcript, stripped. Raises `TranscriptionError`
    if real recognition fails.
    """
    if isinstance(audio_data, SimulatedAudio):
        return audio_data.transcript.strip()

    recognizer = sr.Recognizer()
    try:
        transcript = recognizer.recognize_google(audio_data, language=language)
    except sr.UnknownValueError as exc:
        raise TranscriptionError("speech was unintelligible") from exc
    except sr.RequestError as exc:
        raise TranscriptionError(f"recognition request failed: {exc}") from exc

    return transcript.strip()


def audio_from_wav(path: "str | Path") -> sr.AudioData:
    """Load a WAV file into an `AudioData` ready for `transcribe_audio` -
    used to transcribe a previously recorded clip without re-recording."""
    recognizer = sr.Recognizer()
    with sr.AudioFile(str(path)) as source:
        return recognizer.record(source)


def save_wav(audio_data: sr.AudioData, path: "str | Path") -> Path:
    """Write an `AudioData` out as a 16-bit PCM WAV, for inspection or replay."""
    path = Path(path)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(audio_data.sample_width)
        wav.setframerate(audio_data.sample_rate)
        wav.writeframes(audio_data.get_raw_data())
    return path
