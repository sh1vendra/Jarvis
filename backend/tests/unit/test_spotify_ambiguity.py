"""tools/mac_control.py's _detect_spotify_ambiguity - the deterministic
same-title/different-artist check that replaced trusting the Action
agent's own judgment.

Worth its own dedicated tests, not just end-to-end coverage, for a real
reason documented in planning.md: the small/fast model this project uses
for the Action agent was measured to miss this exact ambiguity twice, even
with an explicit prompt instruction to check for it. Making the check
deterministic Python code instead is only actually safer if the code
itself is right - these tests are what earns that claim, using the real
candidate data read back from Spotify during that investigation (not
synthetic examples invented for the test).
"""

from tools.mac_control import _detect_spotify_ambiguity


# The real candidates vision read back for "Mad World" during this
# project's actual investigation (see planning.md) - Gary Jules' cover
# ranked first, Tears For Fears' original one row down, plus two
# unrelated/other-artist results.
_MAD_WORLD_CANDIDATES = [
    {"position": 1, "title": "Mad World", "artist": "Gary Jules, Michael Andrews", "kind": "song"},
    {"position": 2, "title": "Mad World", "artist": "Tears For Fears", "kind": "music video"},
    {
        "position": 3,
        "title": "Mad World (feat. K.J. Apa, Camila Mendes & Lili Reinhart)",
        "artist": "Riverdale Cast, Camila Mendes",
        "kind": "song",
    },
    {"position": 4, "title": "Mad World", "artist": "Sickick", "kind": "song"},
    {"position": 5, "title": "Mad World", "artist": "Sickick", "kind": "other"},
]

# The real candidates for the clean/unambiguous case this project also
# tested for real - one artist, a remaster suffix on one entry.
_BOHEMIAN_RHAPSODY_CANDIDATES = [
    {"position": 1, "title": "Bohemian Rhapsody", "artist": "Queen", "kind": "song"},
    {"position": 2, "title": "Bohemian Rhapsody", "artist": "Queen", "kind": "music video"},
    {"position": 3, "title": "Bohemian Rhapsody - Remastered", "artist": "Queen", "kind": "song"},
    {"position": 4, "title": "Bohemian Rhapsody -", "artist": "Queen", "kind": "song"},
]


def test_mad_world_with_no_artist_named_is_ambiguous():
    reason = _detect_spotify_ambiguity("Mad World", _MAD_WORLD_CANDIDATES)
    assert reason is not None
    assert "Gary Jules" in reason
    assert "Tears For Fears" in reason


def test_mad_world_with_the_top_artist_named_is_no_longer_ambiguous():
    """The user disambiguating in the query itself resolves it - even
    though other conflicting artists are still in the candidate list."""
    reason = _detect_spotify_ambiguity("Mad World by Gary Jules", _MAD_WORLD_CANDIDATES)
    assert reason is None


def test_mad_world_naming_a_different_conflicting_artist_also_resolves_it():
    reason = _detect_spotify_ambiguity("Mad World Tears For Fears", _MAD_WORLD_CANDIDATES)
    assert reason is None


def test_bohemian_rhapsody_by_queen_is_not_ambiguous():
    """The clean case: every candidate shares one artist (a remaster
    suffix doesn't create a false conflict) - this must not be flagged."""
    reason = _detect_spotify_ambiguity("Bohemian Rhapsody Queen", _BOHEMIAN_RHAPSODY_CANDIDATES)
    assert reason is None


def test_multi_artist_field_matches_the_query_on_any_named_artist():
    """A real bug found and fixed during this project's build: an artist
    field like "Gary Jules, Michael Andrews" must match a query naming
    just one of them ("...by Gary Jules"), not require the whole joined
    string to appear verbatim."""
    candidates = [
        {"position": 1, "title": "Mad World", "artist": "Gary Jules, Michael Andrews", "kind": "song"},
        {"position": 2, "title": "Mad World", "artist": "Sickick", "kind": "song"},
    ]
    assert _detect_spotify_ambiguity("Mad World by Gary Jules", candidates) is None
    assert _detect_spotify_ambiguity("Mad World by Michael Andrews", candidates) is None
    # But naming neither still leaves it genuinely ambiguous.
    reason = _detect_spotify_ambiguity("Mad World", candidates)
    assert reason is not None


def test_no_conflict_when_only_one_song_like_candidate_exists():
    candidates = [{"position": 1, "title": "Bohemian Rhapsody", "artist": "Queen", "kind": "song"}]
    assert _detect_spotify_ambiguity("Bohemian Rhapsody", candidates) is None


def test_empty_candidate_list_is_not_ambiguous():
    assert _detect_spotify_ambiguity("anything", []) is None


def test_candidates_missing_title_or_artist_are_ignored_not_crashed_on():
    """Real, malformed-looking vision output (a candidate dict missing a
    field) must degrade gracefully, not raise."""
    candidates = [
        {"position": 1, "title": "Mad World", "artist": "Gary Jules"},
        {"position": 2, "artist": "Tears For Fears"},  # no title
        {"position": 3, "title": "Mad World"},  # no artist
    ]
    # Only the first candidate has both fields, so there's nothing to
    # conflict with - and this must not raise on the incomplete entries.
    assert _detect_spotify_ambiguity("Mad World", candidates) is None


def test_similar_but_not_identical_titles_do_not_falsely_conflict():
    """Loosely-similar titles (a live version, a feature credit) must not
    be treated as the "same song" just because they share some words -
    only a normalized-exact title match counts, to avoid false ambiguity."""
    candidates = [
        {"position": 1, "title": "Mad World", "artist": "Gary Jules", "kind": "song"},
        {"position": 2, "title": "It's a Mad World", "artist": "Someone Else", "kind": "song"},
    ]
    assert _detect_spotify_ambiguity("Mad World", candidates) is None
