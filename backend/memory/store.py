"""Tier 1 persistent memory for Jarvis - plain SQLite, no embeddings.

Two things, deliberately no more:

  command_history  every real task command that ran - what was said, the
                   plan that came out of it, and whether execution finished
                   successfully. An append-only log.
  preferences      a handful of explicit user-stated facts (a default
                   flight city, who "mom" is, ...) the Planner can consult
                   before it plans, so it can fill in details the user
                   didn't say out loud.

This is NOT the "Memory Agent" from the original plan doc. That is a
ChromaDB-backed semantic store with embeddings and vector search, and it is
a deliberately separate future step (Tier 2) - see planning.md. SQLite is
the right tool for *this* tier: the data is small, structured, exact-match
(a preference key, a history row), and needs to survive a process restart
with zero infrastructure. A vector DB would buy nothing here.

The DB file lives next to this module (`jarvis_memory.db`), resolved
absolutely so it's found regardless of the working directory. Override with
JARVIS_MEMORY_DB (used by tests so they don't touch the real file).
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path(os.environ.get("JARVIS_MEMORY_DB") or (Path(__file__).parent / "jarvis_memory.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS command_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT    NOT NULL,
    transcript   TEXT    NOT NULL,
    plan_summary TEXT,
    success      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL so a reader (a test inspecting rows) and a writer (the pipeline)
    # don't block each other; harmless for a single-process tool too.
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)


_init()


# ── command history ────────────────────────────────────────────────────────

def log_command(transcript: str, plan_summary: str | None, success: bool) -> int:
    """Append one row to command_history. Returns the new row id."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO command_history (timestamp, transcript, plan_summary, success) VALUES (?, ?, ?, ?)",
            (_now(), transcript, plan_summary, 1 if success else 0),
        )
        return int(cur.lastrowid)


def recent_commands(limit: int = 20) -> list[dict]:
    """Most recent command_history rows, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, transcript, plan_summary, success "
            "FROM command_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{**dict(r), "success": bool(r["success"])} for r in rows]


# ── preferences ────────────────────────────────────────────────────────────

def set_preference(key: str, value: str) -> None:
    """Insert or update one preference."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO preferences (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _now()),
        )


def get_preference(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM preferences WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def all_preferences() -> dict[str, str]:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM preferences ORDER BY key").fetchall()
    return {r["key"]: r["value"] for r in rows}


# Filler words in a preference key that carry no matching signal.
_STOPWORDS = {
    "the", "and", "for", "with", "your", "you", "user", "default", "this",
    "that", "who", "what", "was", "are", "how", "when", "why", "his", "her",
}


def relevant_preferences(text: str) -> dict[str, str]:
    """The (deliberately simple) relevance check wired into the Planner: a
    stored preference counts as relevant to `text` if any word of its key
    (underscores/spaces as separators, 3+ chars, not a stopword) appears in
    `text` as a whole word, case-insensitively.

    So `default_flight_destination` matches "search Kayak for a flight" on
    "flight", and `who_is_mom` matches "remind me to call mom" on "mom".
    This is a keyword gate, not NL understanding - just enough to prove
    preferences reach the Planner and change its output. Automatic
    extraction of preferences from speech is Tier 2.
    """
    lowered = text.lower()
    words = set(re.findall(r"[a-z0-9]+", lowered))
    hits: dict[str, str] = {}
    for key, value in all_preferences().items():
        tokens = [t for t in re.split(r"[^a-z0-9]+", key.lower()) if len(t) >= 3 and t not in _STOPWORDS]
        if any(t in words for t in tokens):
            hits[key] = value
    return hits


def db_path() -> str:
    """Where the DB actually is - handy for tests and status output."""
    return str(_DB_PATH)
