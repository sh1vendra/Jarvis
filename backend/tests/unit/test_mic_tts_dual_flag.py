"""agent_server._sync_wakeword_pause_state - the dual-flag (mic_state OR
tts_state) pause logic protecting against Jarvis's own voice, through the
speakers, being picked up by its own wake-word mic.

The real bug class this protects against: a naive single-flag design
("TTS ends -> just resume()") would incorrectly resume the wake-word
listener if a mic capture happened to still be active at that exact
moment (or vice versa). This was verified live against the real running
app during that feature's build (see planning.md) - these are that same
real scenario, formalized as a fast, deterministic unit test instead of
something only ever checked by hand again.

Uses a spy in place of a real WakeWordListener - _sync_wakeword_pause_state
only ever calls .pause()/.resume() on whatever object is registered, so a
plain call-counting double is the right boundary: this tests
agent_server's own decision logic (when does it decide to pause/resume),
not voice/wakeword.py's own pause()/resume() implementation, which has its
own tests (test_wakeword_listener.py).
"""

import pytest

import servers.agent_server as agent_server


class SpyListener:
    def __init__(self):
        self.pause_calls = 0
        self.resume_calls = 0

    def pause(self):
        self.pause_calls += 1

    def resume(self):
        self.resume_calls += 1


@pytest.fixture
def spy_listener(monkeypatch):
    listener = SpyListener()
    monkeypatch.setattr(agent_server, "_wakeword_listener", listener)
    monkeypatch.setattr(agent_server, "_mic_active", False)
    monkeypatch.setattr(agent_server, "_tts_speaking", False)
    return listener


def test_mic_active_pauses_the_listener(spy_listener):
    agent_server._set_mic_active(True)
    assert spy_listener.pause_calls == 1
    assert spy_listener.resume_calls == 0


def test_tts_speaking_pauses_the_listener(spy_listener):
    agent_server._set_tts_speaking(True)
    assert spy_listener.pause_calls == 1
    assert spy_listener.resume_calls == 0


def test_mic_release_resumes_when_tts_is_not_speaking(spy_listener):
    agent_server._set_mic_active(True)
    agent_server._set_mic_active(False)
    assert spy_listener.resume_calls == 1


def test_the_real_regression_clearing_one_flag_while_the_other_is_still_true_must_not_resume(spy_listener):
    """The exact scenario verified live against the real app: tts_state
    (speaking=true) arrives, then mic_state (active=false) arrives while
    speech is still genuinely playing - the listener must stay paused,
    not incorrectly resume just because one of the two flags cleared."""
    agent_server._set_tts_speaking(True)
    agent_server._set_mic_active(True)  # both true, e.g. a hotkey press interrupting speech mid-utterance
    agent_server._set_mic_active(False)  # mic released, but TTS is still (in this scenario) speaking

    assert spy_listener.resume_calls == 0, "must still be paused - tts_speaking is still true"

    agent_server._set_tts_speaking(False)  # only now have both cleared
    assert spy_listener.resume_calls == 1


def test_the_same_regression_the_other_order(spy_listener):
    """Same guarantee, flags flipped in the opposite order - mic clears
    first while TTS is still speaking, must not resume until TTS also
    clears."""
    agent_server._set_mic_active(True)
    agent_server._set_tts_speaking(True)
    agent_server._set_tts_speaking(False)  # TTS done, but the mic is still active

    assert spy_listener.resume_calls == 0, "must still be paused - mic_active is still true"

    agent_server._set_mic_active(False)
    assert spy_listener.resume_calls == 1


def test_resume_only_fires_once_both_flags_are_actually_false(spy_listener):
    agent_server._set_mic_active(True)
    agent_server._set_tts_speaking(True)
    assert spy_listener.resume_calls == 0

    agent_server._set_mic_active(False)
    assert spy_listener.resume_calls == 0  # tts_speaking still true

    agent_server._set_tts_speaking(False)
    assert spy_listener.resume_calls == 1  # both clear now


def test_no_listener_registered_is_a_safe_no_op(monkeypatch):
    """Before start_wakeword_listener() has ever run (or if it never
    started, e.g. dependencies missing), _wakeword_listener is None - the
    mic/tts flag setters must not raise just because there's nothing to
    pause/resume."""
    monkeypatch.setattr(agent_server, "_wakeword_listener", None)
    monkeypatch.setattr(agent_server, "_mic_active", False)
    monkeypatch.setattr(agent_server, "_tts_speaking", False)

    agent_server._set_mic_active(True)
    agent_server._set_mic_active(False)
    agent_server._set_tts_speaking(True)
    agent_server._set_tts_speaking(False)
    # No assertion needed beyond "didn't raise" - this is the whole point.
