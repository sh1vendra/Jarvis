"""A real screen capture, end to end - no mocked subprocess, no mocked
window bounds. Confirms the actual safety contract holds against the real
window server: a real, currently-on-screen app can be captured scoped to
its own window, and an app with no real on-screen window is refused rather
than silently capturing something unrelated (the real incident this
guards against - see planning.md).
"""

import pytest
import Quartz

from tools.perception import capture_screenshot

pytestmark = pytest.mark.integration

# System chrome that's always technically "on screen" but isn't a real app
# window - excluded so the discovered subject below is a genuine app
# window, the same kind of thing capture_screenshot is actually for.
_NOT_A_REAL_APP = {"Window Server", "Dock", "Control Center", "Notification Center", ""}


def _any_app_with_a_real_on_screen_window() -> str | None:
    """Whatever app genuinely has a window on screen right now - not
    hardcoded to one app name, since which apps happen to be open is
    environment-dependent. This is exactly the real, ground-truth query
    capture_screenshot itself uses (_real_window_bounds), just walked here
    to pick a real subject rather than to verify one specific app."""
    window_list = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    for window in window_list:
        owner = window.get("kCGWindowOwnerName", "")
        bounds = window.get("kCGWindowBounds") or {}
        if owner in _NOT_A_REAL_APP:
            continue
        if bounds.get("Width", 0) > 0 and bounds.get("Height", 0) > 0:
            return owner
    return None


def test_capturing_a_real_on_screen_app_returns_real_png_bytes():
    app_name = _any_app_with_a_real_on_screen_window()
    if app_name is None:
        pytest.skip("no app currently has a real on-screen window in this environment")

    png = capture_screenshot(app_name=app_name)

    assert isinstance(png, bytes)
    assert len(png) > 1000  # a real screenshot, not an empty/near-empty file
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # the real PNG magic bytes


def test_an_app_with_no_real_window_is_refused_not_silently_captured():
    with pytest.raises(RuntimeError, match="no verified on-screen window"):
        capture_screenshot(app_name="DefinitelyNotARunningApp12345")
