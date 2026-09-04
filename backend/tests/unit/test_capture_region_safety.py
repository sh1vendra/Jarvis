"""tools/perception.py's capture_region_unverified - a real regression
test for a real safety fix, not a hypothetical one.

Background (see planning.md): a lower-level, unverified capture primitive
(`capture_region`, raw point-space coordinates, no on-screen-window check)
sat right next to the hardened `capture_screenshot` at the same import
level. Under time pressure it got called directly with stale, self-
computed coordinates instead of respecting `capture_screenshot`'s own "no
verified window" refusal - capturing an unrelated app's window instead of
the intended one. The fix made that call shape impossible to construct at
all: `capture_region_unverified` requires a mandatory, keyword-only,
validated `reason` argument with no default. These tests are what actually
proves that - not the docstring, not the name, the real runtime behavior.

Deliberately does not call the real `screencapture` CLI here (that's what
tests/integration/test_capture_screenshot_real.py is for) - every test
below either never reaches the subprocess call at all (the whole point:
the validation happens before it), or mocks `subprocess.run` to keep this
suite fast and independent of anything actually being on screen.
"""

from unittest.mock import MagicMock

import pytest

from tools.perception import capture_region_unverified


def test_calling_without_reason_raises_before_any_capture_happens():
    """The exact call shape that caused the real incident -
    capture_region(x, y, w, h) with no reason - must no longer be
    constructible at all. TypeError, not a silent capture."""
    with pytest.raises(TypeError):
        capture_region_unverified(0, 0, 10, 10)  # type: ignore[call-arg]


def test_positional_reason_is_rejected_reason_must_be_keyword_only():
    """Even someone who remembers a `reason` argument exists but passes it
    positionally (out of habit) must be rejected - keyword-only is what
    keeps the argument self-documenting at every call site, not just
    present."""
    with pytest.raises(TypeError):
        capture_region_unverified(0, 0, 10, 10, "because")  # type: ignore[misc]


def test_empty_reason_is_rejected():
    with pytest.raises(ValueError):
        capture_region_unverified(0, 0, 10, 10, reason="")


def test_whitespace_only_reason_is_rejected():
    """A caller satisfying the type checker with a throwaway value that
    says nothing must not pass - the validation checks content, not just
    presence."""
    with pytest.raises(ValueError):
        capture_region_unverified(0, 0, 10, 10, reason="   ")


def test_a_real_reason_is_accepted_and_the_capture_proceeds(monkeypatch, tmp_path):
    """The safe, legitimate call shape still works - a real reason lets
    the function reach the actual capture logic. subprocess.run is mocked
    here only to avoid depending on real screen content for this unit
    test; the region-rect math and reason validation are exercised for
    real."""
    written_path = tmp_path / "fake_capture.png"
    written_path.write_bytes(b"fake-png-bytes")

    captured_args = {}

    def fake_run(cmd, **kwargs):
        captured_args["cmd"] = cmd
        # screencapture's real contract: write to the destination path
        # it's given, rather than returning bytes directly.
        dest = cmd[-1]
        import shutil

        shutil.copyfile(written_path, dest)
        return MagicMock(returncode=0)

    monkeypatch.setattr("tools.perception.subprocess.run", fake_run)

    result = capture_region_unverified(
        100.0, 200.0, 40.0, 30.0, reason="test: a small region, not the whole-window shape the incident used"
    )

    assert result == b"fake-png-bytes"
    # The rect passed to screencapture -R is left,top,width,height, where
    # left/top are the region's *edge*, derived from the given center -
    # confirms the coordinate math, not just that some call happened.
    rect_arg = captured_args["cmd"][captured_args["cmd"].index("-R") + 1]
    left, top, width, height = (float(v) for v in rect_arg.split(","))
    assert (left, top, width, height) == (80.0, 185.0, 40.0, 30.0)


def test_capture_screenshot_still_works_exactly_as_before(monkeypatch, tmp_path):
    """The safe, public path (capture_screenshot) must be completely
    unaffected by the rename/reason refactor underneath it - this is the
    same real regression coverage for "the safe path still works exactly
    as before" that was checked manually when the fix was built, now
    automated."""
    from tools import perception

    written_path = tmp_path / "fake_window.png"
    written_path.write_bytes(b"fake-window-bytes")

    def fake_run(cmd, **kwargs):
        import shutil

        shutil.copyfile(written_path, cmd[-1])
        return MagicMock(returncode=0)

    monkeypatch.setattr(perception, "_real_window_bounds", lambda app_name: (100.0, 100.0, 460.0, 340.0))
    monkeypatch.setattr("tools.perception.subprocess.run", fake_run)

    result = perception.capture_screenshot(app_name="TestApp")
    assert result == b"fake-window-bytes"


def test_capture_screenshot_still_refuses_without_a_verified_window(monkeypatch):
    """Unrelated to the reason-parameter fix directly, but the other half
    of the same safety story: capture_screenshot's own refusal (built in
    an earlier pass, see planning.md's "a real privacy incident" entry)
    must still be intact after this refactor. _real_window_bounds is
    mocked to return None (rather than letting this hit the real
    CGWindowListCopyWindowInfo) so this stays a true unit test - not
    dependent on real window state, and not Mac-only to even run."""
    from tools import perception

    monkeypatch.setattr(perception, "_real_window_bounds", lambda app_name: None)

    with pytest.raises(RuntimeError, match="no verified on-screen window"):
        perception.capture_screenshot(app_name="SomeAppWithNoRealWindow")
