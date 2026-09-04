"""tools/mac_control.py's click-outcome verification - the logic that
decides whether a click actually did what it was supposed to, instead of
reporting success just because a click was dispatched without erroring.

Deliberately tests the deterministic pieces directly (word-boundary outcome
matching, the real player-state-change decision, real pixel-diff math on
real synthesized images) and the state-check branch's real decision logic
(injected via a fake app entry in _APP_PLAYER_STATE_CHECKS) - not the full
`_verify_click_outcome` end to end, which would need a real screenshot and
a real Gemini vision call for its pixel-diff/vision fallback path. That
fallback is exactly the kind of thing that would need so much mocking to
fake convincingly (a real click's visual effect, a real vision judgment)
that the test would stop meaning anything - see tests/integration/ for
where that's actually exercised for real instead.
"""

import io

import pytest
from PIL import Image

import tools.mac_control as mac_control
from tools.mac_control import (
    _looks_like_playback_outcome,
    _region_pixel_diff_score,
    _spotify_playback_changed,
    _spotify_state_is_ad,
    _verify_click_outcome,
)


# -- _looks_like_playback_outcome: word-boundary matching, not substring ----
# This exact distinction was a real, twice-found bug (see the function's
# own comment in mac_control.py) - these pin both directions down.

@pytest.mark.parametrize(
    "outcome",
    [
        "a lo-fi track starts playing",
        "the now playing bar displays Bohemian Rhapsody",
        "Bohemian Rhapsody by Queen starts playing",
        "the song is paused",
        "music is playing",
    ],
)
def test_real_playback_phrasings_are_recognized(outcome):
    assert _looks_like_playback_outcome(outcome) is True


@pytest.mark.parametrize(
    "outcome",
    [
        "the Podcasts filter becomes selected",
        "the playlist page opens",  # "play" substring inside "playlist" - must NOT match
        "the search field contains lo-fi",
    ],
)
def test_non_playback_outcomes_are_not_recognized(outcome):
    assert _looks_like_playback_outcome(outcome) is False


# -- _spotify_playback_changed: the real before/after decision --------------

def _state(player_state, name, artist):
    return {"player_state": player_state, "track_name": name, "track_artist": artist, "track_uri": "spotify:track:abc"}


def test_transition_from_paused_to_playing_counts_as_changed():
    before = _state("paused", "Careless Whisper", "George Michael")
    after = _state("playing", "Careless Whisper", "George Michael")
    assert _spotify_playback_changed(before, after) is True


def test_track_changing_while_already_playing_counts_as_changed():
    before = _state("playing", "Careless Whisper", "George Michael")
    after = _state("playing", "Bohemian Rhapsody", "Queen")
    assert _spotify_playback_changed(before, after) is True


def test_identical_state_is_not_a_change():
    before = _state("paused", "Careless Whisper", "George Michael")
    after = _state("paused", "Careless Whisper", "George Michael")
    assert _spotify_playback_changed(before, after) is False


def test_none_state_is_never_a_change():
    assert _spotify_playback_changed(None, _state("playing", "X", "Y")) is False
    assert _spotify_playback_changed(_state("paused", "X", "Y"), None) is False
    assert _spotify_playback_changed(None, None) is False


# -- _spotify_state_is_ad: the real Free-tier ad false-positive fix ---------

def test_real_track_uri_is_not_an_ad():
    assert _spotify_state_is_ad(_state("playing", "Bohemian Rhapsody", "Queen")) is False


def test_ad_uri_is_detected():
    """The exact real incident this exists for: Spotify's Free tier
    started playing an ad with a plausible track_name/track_artist - only
    the URI distinguishes it."""
    ad_state = {"player_state": "playing", "track_name": "CHRISTUS Health", "track_artist": "", "track_uri": "spotify:ad:b7d9dd92a4574f65b2dbd99e21ad377c"}
    assert _spotify_state_is_ad(ad_state) is True


def test_none_state_is_not_an_ad():
    assert _spotify_state_is_ad(None) is False


# -- _region_pixel_diff_score: real image bytes, real pixel math ------------

def _solid_png(color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (40, 30), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_identical_images_score_near_zero():
    png = _solid_png((20, 20, 20))
    assert _region_pixel_diff_score(png, png) < 1.0


def test_a_visibly_different_image_scores_high():
    before = _solid_png((10, 10, 10))
    after = _solid_png((240, 240, 240))
    assert _region_pixel_diff_score(before, after) > 100.0


def test_a_small_localized_change_scores_low_but_nonzero():
    """Realistic case: most of the region is unchanged, a small part
    (e.g. new text appearing) differs - the mean score should reflect
    that as a real but modest change, not swing to either extreme."""
    before = Image.new("RGB", (40, 30), color=(20, 20, 20))
    after = before.copy()
    for x in range(5):
        for y in range(5):
            after.putpixel((x, y), (220, 220, 220))
    buf_before, buf_after = io.BytesIO(), io.BytesIO()
    before.save(buf_before, format="PNG")
    after.save(buf_after, format="PNG")
    score = _region_pixel_diff_score(buf_before.getvalue(), buf_after.getvalue())
    assert 0.0 < score < 50.0


# -- _verify_click_outcome's state-check branch: the real decision logic ----
# Injects a fake app into _APP_PLAYER_STATE_CHECKS rather than using real
# Spotify - this is testing _verify_click_outcome's own branching/decision
# logic (does it trust the state check, retry through an ad, fall through
# correctly), not testing Spotify's AppleScript integration, which belongs
# to the integration suite.

@pytest.fixture
def fake_player_state_app(monkeypatch):
    """Registers "TestApp" in _APP_PLAYER_STATE_CHECKS with a
    caller-controlled sequence of state snapshots, and neutralizes
    time.sleep so the ad-retry loop doesn't actually wait for real."""
    states = []

    def state_check():
        return states.pop(0) if states else None

    monkeypatch.setitem(mac_control._APP_PLAYER_STATE_CHECKS, "TestApp", state_check)
    monkeypatch.setattr(mac_control.time, "sleep", lambda _seconds: None)
    return states


def test_real_playback_change_is_verified_via_state_check_alone(fake_player_state_app):
    fake_player_state_app.extend([_state("playing", "Bohemian Rhapsody", "Queen")])
    before = _state("paused", "Careless Whisper", "George Michael")

    verified, detail = _verify_click_outcome(
        "TestApp", "Bohemian Rhapsody by Queen starts playing", before, b"", (0.0, 0.0)
    )
    assert verified is True
    assert "player state" in detail


def test_no_playback_change_is_reported_as_not_verified(fake_player_state_app):
    fake_player_state_app.extend([_state("paused", "Careless Whisper", "George Michael")])
    before = _state("paused", "Careless Whisper", "George Michael")

    verified, detail = _verify_click_outcome(
        "TestApp", "Bohemian Rhapsody by Queen starts playing", before, b"", (0.0, 0.0)
    )
    assert verified is False
    assert "no playback change" in detail


def test_an_ad_that_never_clears_is_reported_as_not_verified_not_a_false_success(fake_player_state_app):
    """The real Free-tier-ad false positive this project found live and
    fixed: player_state legitimately transitions to "playing", but it's an
    ad, not the requested track - must never be reported as verified."""
    ad_state = {"player_state": "playing", "track_name": "CHRISTUS Health", "track_artist": "", "track_uri": "spotify:ad:xyz"}
    # One snapshot per retry attempt, all still an ad - state_check() is
    # called once up front plus once per retry (mac_control._SPOTIFY_AD_RETRY_ATTEMPTS).
    fake_player_state_app.extend([ad_state] * (mac_control._SPOTIFY_AD_RETRY_ATTEMPTS + 1))
    before = _state("paused", "Careless Whisper", "George Michael")

    verified, detail = _verify_click_outcome(
        "TestApp", "Bohemian Rhapsody by Queen starts playing", before, b"", (0.0, 0.0)
    )
    assert verified is False
    assert "ad" in detail.lower()


def test_an_ad_that_clears_partway_through_the_retry_window_still_gets_verified(fake_player_state_app):
    """The other half of the same fix: a short pre-roll ad that clears
    within the retry budget should let the real requested track be
    verified, not be treated as a permanent failure."""
    ad_state = {"player_state": "playing", "track_name": "CHRISTUS Health", "track_artist": "", "track_uri": "spotify:ad:xyz"}
    real_state = _state("playing", "Bohemian Rhapsody", "Queen")
    fake_player_state_app.extend([ad_state, ad_state, real_state])
    before = _state("paused", "Careless Whisper", "George Michael")

    verified, detail = _verify_click_outcome(
        "TestApp", "Bohemian Rhapsody by Queen starts playing", before, b"", (0.0, 0.0)
    )
    assert verified is True
    assert "Bohemian Rhapsody" in detail


def test_an_unrelated_outcome_does_not_consult_player_state_at_all(fake_player_state_app, monkeypatch):
    """expected_outcome that has nothing to do with playback must not be
    judged by a check that has nothing to say about it - it should fall
    through toward the pixel-diff path instead. Confirmed here by leaving
    no state snapshots queued at all (state_check() would return None,
    which is a legitimate "fell through" signal) and by never calling the
    real vision path (patched to a value that would fail the test loudly
    if reached)."""

    def unreachable_capture(*_a, **_kw):
        raise AssertionError("should not reach the pixel-diff/vision fallback in this test")

    monkeypatch.setattr(mac_control, "capture_region_unverified", unreachable_capture)
    before = _state("paused", "Careless Whisper", "George Michael")

    with pytest.raises(AssertionError):
        _verify_click_outcome("TestApp", "the Podcasts filter becomes selected", before, b"", (0.0, 0.0))
