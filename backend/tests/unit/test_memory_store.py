"""memory/store.py - Tier 1 SQLite memory.

Every test here runs against an isolated tmp DB file (see conftest.py's
`isolated_memory_store` fixture) - never the real jarvis_memory.db next to
the module. Real SQLite, real file I/O, no mocking: this module has no
Mac/network dependency at all, so there's no reason to fake any part of
it - a "unit test" here just means "isolated from the real database file
and real preference state," not "doesn't really touch SQLite."
"""


def test_log_command_records_the_real_success_value_not_just_that_it_ran(isolated_memory_store):
    """The exact historical bug class this guards against: a command whose
    real execution failed (a milestone's tools reported success:false)
    must be logged with success=False - not True just because the pipeline
    itself didn't crash. See planning.md's "honest failure state" entry -
    this is the persistence-layer half of that fix."""
    store = isolated_memory_store

    store.log_command("play Bohemian Rhapsody by Queen in Spotify", "1. Bohemian Rhapsody is playing", success=False)
    store.log_command("create a reminder to call mom tomorrow at 5pm", "1. A reminder exists", success=True)

    rows = store.recent_commands(limit=10)
    by_transcript = {r["transcript"]: r["success"] for r in rows}
    assert by_transcript["play Bohemian Rhapsody by Queen in Spotify"] is False
    assert by_transcript["create a reminder to call mom tomorrow at 5pm"] is True
    # Stored and read back as real booleans, not a leftover 0/1 int - a
    # caller checking `if row["success"]:` must get the right answer.
    assert all(isinstance(r["success"], bool) for r in rows)


def test_recent_commands_orders_newest_first_and_respects_limit(isolated_memory_store):
    store = isolated_memory_store
    for i in range(5):
        store.log_command(f"command {i}", f"plan {i}", success=True)

    rows = store.recent_commands(limit=3)
    assert [r["transcript"] for r in rows] == ["command 4", "command 3", "command 2"]


def test_preference_round_trips_and_updates_in_place(isolated_memory_store):
    store = isolated_memory_store
    store.set_preference("default_flight_destination", "Austin, Texas")
    assert store.get_preference("default_flight_destination") == "Austin, Texas"

    # Setting the same key again updates it, not a second row.
    store.set_preference("default_flight_destination", "Denver, Colorado")
    assert store.get_preference("default_flight_destination") == "Denver, Colorado"
    assert len(store.all_preferences()) == 1


def test_get_preference_missing_key_returns_none_not_an_error(isolated_memory_store):
    assert isolated_memory_store.get_preference("no_such_key") is None


def test_relevant_preference_surfaces_for_a_matching_command(isolated_memory_store):
    """The real example this codebase's own docstring uses, formalized:
    a stored default_flight_destination should surface for a command
    that's actually about a flight."""
    store = isolated_memory_store
    store.set_preference("default_flight_destination", "Austin, Texas")

    hits = store.relevant_preferences("search Kayak for a flight next weekend")
    assert hits == {"default_flight_destination": "Austin, Texas"}


def test_relevant_preference_does_not_leak_into_an_unrelated_command(isolated_memory_store):
    """The control side of the same test, formalized - a stored preference
    must NOT surface for a command it has nothing to do with. This was
    proven manually earlier in this project's build (a reminder command
    with a flight-destination preference stored produced no injected
    preference block); this is that same real check, now automated."""
    store = isolated_memory_store
    store.set_preference("default_flight_destination", "Austin, Texas")

    hits = store.relevant_preferences("create a reminder to call mom tomorrow at 5pm")
    assert hits == {}


def test_relevant_preference_matches_on_a_real_keyword_not_a_substring_accident(isolated_memory_store):
    """who_is_mom should match a command mentioning "mom" as a whole word -
    and, just as importantly, a preference should NOT fire on an
    accidental substring match (e.g. "mom" inside a longer unrelated
    word)."""
    store = isolated_memory_store
    store.set_preference("who_is_mom", "Diane Prince, +1-555-0100")

    assert store.relevant_preferences("remind me to call mom at 5pm") == {"who_is_mom": "Diane Prince, +1-555-0100"}
    assert store.relevant_preferences("remind me to call the momentum team") == {}


def test_multiple_preferences_only_the_relevant_one_surfaces(isolated_memory_store):
    store = isolated_memory_store
    store.set_preference("default_flight_destination", "Austin, Texas")
    store.set_preference("who_is_mom", "Diane Prince, +1-555-0100")

    assert store.relevant_preferences("search Kayak for a flight") == {"default_flight_destination": "Austin, Texas"}
    assert store.relevant_preferences("remind me to call mom") == {"who_is_mom": "Diane Prince, +1-555-0100"}


def test_persistence_across_a_simulated_process_restart(tmp_path, monkeypatch):
    """A real restart: write through one "process" (one module import bound
    to this DB file), then simulate a fresh process starting up pointed at
    the same file (a second, independent reload) and confirm the data is
    still there - not relying on the same Python object/connection still
    being alive, which would prove nothing about real persistence.
    """
    import importlib

    db_path = tmp_path / "restart_test.db"
    monkeypatch.setenv("JARVIS_MEMORY_DB", str(db_path))

    from memory import store

    importlib.reload(store)  # "process 1" starts
    store.log_command("create a reminder to call mom tomorrow at 5pm", "1. A reminder exists", success=True)
    store.set_preference("default_flight_destination", "Austin, Texas")

    importlib.reload(store)  # "process 1" exits, "process 2" starts fresh, same DB file
    rows = store.recent_commands()
    assert len(rows) == 1
    assert rows[0]["transcript"] == "create a reminder to call mom tomorrow at 5pm"
    assert rows[0]["success"] is True
    assert store.get_preference("default_flight_destination") == "Austin, Texas"

    monkeypatch.undo()
    importlib.reload(store)
