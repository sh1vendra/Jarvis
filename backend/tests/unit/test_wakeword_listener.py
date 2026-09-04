"""voice/wakeword.py's WakeWordListener - the pause()/resume() mic-handoff
state machine specifically, not the real audio pipeline.

pause()/resume() only ever toggle a threading.Event (`_capturing`) -
confirmed directly by reading the source, not assumed - so they're
testable without ever calling start() (which is what actually touches the
microphone/model/thread). That's the real unit/integration boundary for
this module: the state machine is a unit test, real detection against a
real microphone is tests/integration/test_wakeword_real_detection.py.
"""

from voice.wakeword import WakeWordListener


def _listener(on_detected=None) -> WakeWordListener:
    return WakeWordListener(on_detected=on_detected or (lambda score: None))


def test_a_fresh_listener_has_not_started_capturing():
    listener = _listener()
    assert listener._capturing.is_set() is False


def test_pause_clears_the_capturing_flag():
    listener = _listener()
    listener._capturing.set()  # simulate an already-running listener
    listener.pause()
    assert listener._capturing.is_set() is False


def test_resume_sets_the_capturing_flag():
    listener = _listener()
    listener.pause()
    listener.resume()
    assert listener._capturing.is_set() is True


def test_pause_is_idempotent():
    """Calling pause() twice in a row (e.g. wake-word detection pausing it,
    then a mic_state(active=true) also arriving) must not raise or leave
    it in a confused state - it's still just "not capturing" either way."""
    listener = _listener()
    listener.resume()
    listener.pause()
    listener.pause()
    assert listener._capturing.is_set() is False


def test_resume_is_idempotent():
    listener = _listener()
    listener.resume()
    listener.resume()
    assert listener._capturing.is_set() is True


def test_unavailable_reason_is_none_when_dependencies_are_present():
    """This test environment has openwakeword/sounddevice/numpy installed
    (see backend/requirements.txt) - a real, meaningful assertion here,
    not a tautology, since it would genuinely go non-None if a dependency
    were missing (see voice/wakeword.py's own guarded import)."""
    assert _listener().unavailable_reason is None
