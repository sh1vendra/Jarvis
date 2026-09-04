"""Shared fixtures for the whole suite (unit and integration).

Nothing in here should require real Mac state or a real network call -
that split belongs to individual tests/modules (see tests/unit/ vs
tests/integration/, and pytest.ini's `integration` marker).
"""

import importlib
import sys
from pathlib import Path

import pytest

# backend/ itself, so `import main`, `from tools.mac_control import ...`,
# etc. resolve the same way they do for the real app - matches
# pytest.ini's `pythonpath = .`, restated here as a belt-and-suspenders in
# case a test file is ever run directly (`python tests/unit/test_x.py`)
# rather than through pytest.
_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


@pytest.fixture
def isolated_memory_store(tmp_path, monkeypatch):
    """A fresh, isolated SQLite file per test - never the real
    jarvis_memory.db next to memory/store.py.

    memory/store.py resolves its DB path once, at import time, from the
    JARVIS_MEMORY_DB env var (falling back to the real file next to the
    module). So isolating a test means: point that env var at a tmp file,
    then reload the already-imported module so it re-resolves _DB_PATH
    against the new value and re-runs its CREATE TABLE IF NOT EXISTS
    against a database that doesn't exist yet - exactly what a real fresh
    process pointed at a different DB file would do.
    """
    db_path = tmp_path / "test_memory.db"
    monkeypatch.setenv("JARVIS_MEMORY_DB", str(db_path))
    from memory import store

    importlib.reload(store)
    try:
        yield store
    finally:
        # Restore the env var, then reload once more so the module is left
        # bound to whatever it should be for anything that runs after this
        # test (the real DB path, or another test's isolated one) - a
        # module left pointed at this test's now-torn-down tmp_path would
        # be a real, if quiet, source of test pollution otherwise.
        monkeypatch.undo()
        importlib.reload(store)
