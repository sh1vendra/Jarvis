"""agent_server._speak_text_for_* - the personality-flavor rule as real,
testable logic, not just a prompt convention.

The rule (see planning.md's TTS entry): flavor ("Right away.", "Very
well.", ...) is allowed on a real action confirmation only - never on a
conversational reply, a failure, a cancellation, or an error. These
functions are pure text-in/text-out, so the rule can be pinned down
exactly, by construction, rather than only spot-checked by ear against
real speech output (which was also done for real when this was built -
see planning.md - but that doesn't substitute for a fast, permanent check
that can't regress silently).
"""

import servers.agent_server as agent_server

_FLAVOR_PHRASES = set(agent_server._ACTION_CONFIRMATIONS)


def test_done_plan_always_uses_a_real_flavor_phrase():
    # Sampled many times since the choice is random - every draw must
    # still land in the real, known set, never something else.
    for _ in range(50):
        assert agent_server._speak_text_for_done_plan() in _FLAVOR_PHRASES


def test_conversational_reply_is_spoken_verbatim_with_no_flavor():
    text = agent_server._speak_text_for_conversational("2 plus 2 is 4.")
    assert text == "2 plus 2 is 4."
    assert text not in _FLAVOR_PHRASES
    assert not any(phrase.split(".")[0] in text for phrase in _FLAVOR_PHRASES)


def test_conversational_reply_is_stripped_of_surrounding_whitespace():
    assert agent_server._speak_text_for_conversational("  2 plus 2 is 4.  \n") == "2 plus 2 is 4."


def test_failed_with_a_real_agent_message_speaks_that_message_verbatim():
    """The real, important case: a genuine clarifying question the Action
    agent asked (e.g. the Spotify ambiguity case) must reach speech
    verbatim, not be replaced with boilerplate - and must carry no
    flavor."""
    failed_goals = [
        {
            "goal": "Mad World is playing in Spotify",
            "message": 'There are multiple versions of "Mad World" on Spotify, notably by Gary Jules and Sickick. Which one would you like me to play?',
        }
    ]
    text = agent_server._speak_text_for_failed(failed_goals)
    assert text == failed_goals[0]["message"]
    assert not any(phrase in text for phrase in _FLAVOR_PHRASES)


def test_failed_with_no_message_falls_back_to_a_generic_line_not_silence():
    failed_goals = [{"goal": "X is playing in Spotify", "message": ""}]
    text = agent_server._speak_text_for_failed(failed_goals)
    assert text == "I couldn't complete that."
    assert not any(phrase in text for phrase in _FLAVOR_PHRASES)


def test_failed_joins_multiple_real_messages():
    failed_goals = [
        {"goal": "A", "message": "First real reason."},
        {"goal": "B", "message": "Second real reason."},
    ]
    text = agent_server._speak_text_for_failed(failed_goals)
    assert "First real reason." in text
    assert "Second real reason." in text


def test_cancelled_names_the_real_refused_goal_with_no_flavor():
    text = agent_server._speak_text_for_cancelled("a message is sent to the team")
    assert "a message is sent to the team" in text
    assert not any(phrase in text for phrase in _FLAVOR_PHRASES)


def test_cancelled_with_no_goal_still_produces_a_real_sentence():
    text = agent_server._speak_text_for_cancelled("")
    assert text == "That was cancelled. Nothing was done."


def test_error_text_has_no_flavor_and_is_not_a_raw_exception_string():
    text = agent_server._speak_text_for_error()
    assert not any(phrase in text for phrase in _FLAVOR_PHRASES)
    assert "Traceback" not in text
    assert text == "Something went wrong completing that."


def test_flavor_phrases_never_leak_into_any_non_confirmation_path():
    """The rule stated as one sweeping check across every non-confirmation
    function this module exposes, so a future new failure/cancel/error
    variant added without updating this test still gets caught if it
    accidentally reuses a flavor phrase."""
    non_confirmation_outputs = [
        agent_server._speak_text_for_conversational("The weather is sunny today."),
        agent_server._speak_text_for_failed([{"goal": "X", "message": ""}]),
        agent_server._speak_text_for_cancelled("X"),
        agent_server._speak_text_for_error(),
    ]
    for text in non_confirmation_outputs:
        for phrase in _FLAVOR_PHRASES:
            assert phrase not in text, f"flavor phrase {phrase!r} leaked into a non-confirmation response: {text!r}"
