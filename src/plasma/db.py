"""SQLite storage. The database is a local file, by default local/plasma.db."""

import os
import sqlite3
from pathlib import Path

DEFAULT_PATH = Path("local/plasma.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS titles (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('movie', 'series')),
    name TEXT NOT NULL,
    year INTEGER,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK (status IN ('planned', 'watching', 'completed', 'dropped')),
    rating INTEGER CHECK (rating BETWEEN 1 AND 10),
    notes TEXT NOT NULL DEFAULT '',
    watched_on TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS titles_identity
    ON titles (kind, name COLLATE NOCASE, COALESCE(year, 0));

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    title_id INTEGER NOT NULL REFERENCES titles (id) ON DELETE CASCADE,
    season INTEGER NOT NULL,
    number INTEGER NOT NULL,
    watched INTEGER NOT NULL DEFAULT 0,
    watched_on TEXT,
    UNIQUE (title_id, season, number)
);
"""


def default_path() -> Path:
    return Path(os.environ.get("PLASMA_DB", DEFAULT_PATH))


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database, creating it if needed.

    Cross-thread use is allowed because FastAPI may run a request's dependency
    and its handler on different worker threads. Each request has its own connection.
    """
    path = Path(path) if path else default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
