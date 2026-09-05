"""tools.browser_tools._match_score - the scoring function find_web_element
relies on to turn a plain-language description into the right element.

Pure logic, no browser bridge, no real page - see tests/README.md's
unit/integration boundary reasoning. The real-bug case here is not
hypothetical: a live Stage 3 test run had find_web_element("Search button")
confidently match Kayak's own account-avatar button (el.text == "s", the
user's initial) instead of the real Search button, because "s" is
trivially a substring of almost any query. See planning.md's Stage 3 entry.
"""

from browser.models import ElementRef
from tools.browser_tools import _match_score


def _el(**kwargs) -> ElementRef:
    return ElementRef(ref_id=kwargs.pop("ref_id", "jw_1"), generation=1, **kwargs)


def test_a_single_character_label_does_not_win_via_substring_coincidence():
    avatar = _el(text="s", tag="div", role="button")
    assert _match_score("search button", avatar) == 0.0


def test_a_single_character_label_still_wins_on_a_genuine_exact_match():
    avatar = _el(text="s", tag="div", role="button")
    assert _match_score("s", avatar) > 0.0


def test_a_real_short_label_still_matches_a_longer_natural_query():
    # The real "Where to?" -> Kayak destination field case from planning.md:
    # placeholder "to?" must still be found via a longer natural phrasing.
    destination_field = _el(placeholder="To?", aria_label="Destination location", tag="input", role="combobox")
    assert _match_score("where to?", destination_field) > 0.0


def test_the_real_search_button_beats_the_avatar_for_a_search_query():
    avatar = _el(text="s", tag="div", role="button")
    search_button = _el(text="Search", tag="div", role="button")
    query = "search button"
    assert _match_score(query, search_button) > _match_score(query, avatar)


def test_exact_match_still_scores_higher_than_a_substring_match():
    exact = _el(text="Search")
    substring = _el(text="Search flights and hotels")
    query = "search"
    assert _match_score(query, exact) > _match_score(query, substring)
