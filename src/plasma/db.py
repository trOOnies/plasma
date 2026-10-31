"""SQLite storage. The database is a local file, by default local/plasma.db."""

import os
import sqlite3
from pathlib import Path

DEFAULT_PATH = Path("local/plasma.db")

SCHEMA_VERSION = 4

TITLES_COLUMNS = """
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('movie', 'series', 'game')),
    name TEXT NOT NULL,
    year INTEGER,
    status TEXT NOT NULL DEFAULT 'none'
        CHECK (status IN ('none', 'planned', 'watching', 'completed', 'dropped')),
    rating INTEGER CHECK (rating BETWEEN 1 AND 10),
    duration INTEGER,
    release_date TEXT,
    hours_played INTEGER,
    producer TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    favorite INTEGER NOT NULL DEFAULT 0,
    owned INTEGER NOT NULL DEFAULT 0,
    image TEXT,
    watched_on TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS titles ({TITLES_COLUMNS});

CREATE UNIQUE INDEX IF NOT EXISTS titles_identity
    ON titles (kind, name COLLATE NOCASE, COALESCE(year, 0));

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    title_id INTEGER NOT NULL REFERENCES titles (id) ON DELETE CASCADE,
    season INTEGER NOT NULL,
    number INTEGER NOT NULL,
    duration INTEGER,
    release_date TEXT,
    description TEXT NOT NULL DEFAULT '',
    watched INTEGER NOT NULL DEFAULT 0,
    watched_on TEXT,
    UNIQUE (title_id, season, number)
);

CREATE TABLE IF NOT EXISTS platforms (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    url TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS title_platforms (
    title_id INTEGER NOT NULL REFERENCES titles (id) ON DELETE CASCADE,
    platform_id INTEGER NOT NULL REFERENCES platforms (id) ON DELETE CASCADE,
    PRIMARY KEY (title_id, platform_id)
);

CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS plan_items (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES plans (id) ON DELETE CASCADE,
    title_id INTEGER NOT NULL REFERENCES titles (id) ON DELETE CASCADE,
    UNIQUE (plan_id, title_id)
);
"""

# Columns that older databases may be missing.
ADDED_COLUMNS = {
    "titles": {
        "duration": "INTEGER",
        "release_date": "TEXT",
        "hours_played": "INTEGER",
        "producer": "TEXT NOT NULL DEFAULT ''",
        "description": "TEXT NOT NULL DEFAULT ''",
        "favorite": "INTEGER NOT NULL DEFAULT 0",
        "owned": "INTEGER NOT NULL DEFAULT 0",
        "image": "TEXT",
    },
    "episodes": {
        "duration": "INTEGER",
        "release_date": "TEXT",
        "description": "TEXT NOT NULL DEFAULT ''",
    },
}

COPIED_COLUMNS = (
    "id, kind, name, year, rating, duration, release_date, hours_played, producer,"
    " description, notes, favorite, owned, image, watched_on, updated_at"
)


def default_path() -> Path:
    return Path(os.environ.get("PLASMA_DB", DEFAULT_PATH))


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database, creating or upgrading it if needed.

    Cross-thread use is allowed because FastAPI may run a request's dependency
    and its handler on different worker threads. Each request has its own connection.
    """
    path = Path(path) if path else default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if _version(conn) < SCHEMA_VERSION:
        if _has_titles(conn):
            _upgrade(conn)
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _has_titles(conn: sqlite3.Connection) -> bool:
    query = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'titles'"
    return conn.execute(query).fetchone() is not None


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _rebuild_titles(conn: sqlite3.Connection, version: int) -> None:
    """Rebuild titles, since SQLite can't change CHECK constraints or defaults in place.

    Before version 1 every title started as planned, so planned says nothing about
    what the user chose and becomes none. Later versions keep it, as a real choice.
    """
    status = "CASE status WHEN 'planned' THEN 'none' ELSE status END" if version < 1 else "status"
    conn.execute(f"CREATE TABLE titles_new ({TITLES_COLUMNS})")
    conn.execute(
        f"INSERT INTO titles_new ({COPIED_COLUMNS}, status)"
        f" SELECT {COPIED_COLUMNS}, {status} FROM titles"
    )
    conn.execute("DROP TABLE titles")
    conn.execute("ALTER TABLE titles_new RENAME TO titles")


def _upgrade(conn: sqlite3.Connection) -> None:
    """Bring an older database up to date in one transaction.

    Foreign keys must be off here, or dropping the old titles table would cascade
    and delete every episode and platform link.
    """
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            version = _version(conn)
            if version >= SCHEMA_VERSION:
                return
            _add_missing_columns(conn)
            if version < 3:
                _rebuild_titles(conn, version)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
