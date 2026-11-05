"""SQLite storage. The database is a local file, by default local/plasma.db."""

import os
import sqlite3
import unicodedata
from pathlib import Path

from plasma.models import PlatformKind, guess_platform_kind

DEFAULT_PATH = Path("local/plasma.db")

SCHEMA_VERSION = 11

MULTIPLAYER_STATUS_COLUMN = """TEXT NOT NULL DEFAULT 'none'
        CHECK (multiplayer_status IN ('none', 'planned', 'watching', 'dropped'))"""
AGE_RESTRICTION_COLUMN = """TEXT
        CHECK (age_restriction IN ('all', '7+', '13+', '16+', '18+'))"""
PLATFORM_KIND_COLUMN = """TEXT NOT NULL DEFAULT 'streaming'
        CHECK (kind IN ('streaming', 'console'))"""

RATING_COLUMN = "INTEGER CHECK (rating BETWEEN 1 AND 5)"
OWNERSHIP_COLUMN = """TEXT NOT NULL DEFAULT 'none'
        CHECK (ownership IN ('none', 'wanted', 'owned'))"""

TITLES_COLUMNS = f"""
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('movie', 'series', 'game')),
    name TEXT NOT NULL,
    year INTEGER,
    status TEXT NOT NULL DEFAULT 'none'
        CHECK (status IN ('none', 'planned', 'watching', 'completed', 'dropped')),
    singleplayer INTEGER NOT NULL DEFAULT 1,
    multiplayer INTEGER,
    multiplayer_status {MULTIPLAYER_STATUS_COLUMN},
    age_restriction {AGE_RESTRICTION_COLUMN},
    rating {RATING_COLUMN},
    duration INTEGER,
    release_date TEXT,
    hours_played INTEGER,
    producer TEXT NOT NULL DEFAULT '',
    franchise TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    favorite INTEGER NOT NULL DEFAULT 0,
    ownership {OWNERSHIP_COLUMN},
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
    rating {RATING_COLUMN},
    watched INTEGER NOT NULL DEFAULT 0,
    watched_on TEXT,
    UNIQUE (title_id, season, number)
);

CREATE TABLE IF NOT EXISTS platforms (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    url TEXT NOT NULL DEFAULT '',
    kind {PLATFORM_KIND_COLUMN},
    release_year INTEGER,
    image TEXT
);

CREATE TABLE IF NOT EXISTS title_platforms (
    title_id INTEGER NOT NULL REFERENCES titles (id) ON DELETE CASCADE,
    platform_id INTEGER NOT NULL REFERENCES platforms (id) ON DELETE CASCADE,
    PRIMARY KEY (title_id, platform_id)
);

CREATE TABLE IF NOT EXISTS title_genres (
    title_id INTEGER NOT NULL REFERENCES titles (id) ON DELETE CASCADE,
    genre TEXT NOT NULL,
    PRIMARY KEY (title_id, genre)
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

-- What the web UI took away, so that importing info/ again can't bring it back.
CREATE TABLE IF NOT EXISTS removed_entries (
    kind TEXT NOT NULL,
    name TEXT NOT NULL COLLATE NOCASE,
    year INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, name, year)
);

CREATE TABLE IF NOT EXISTS cleared_fields (
    title_id INTEGER NOT NULL REFERENCES titles (id) ON DELETE CASCADE,
    season INTEGER NOT NULL DEFAULT 0,
    number INTEGER NOT NULL DEFAULT 0,
    field TEXT NOT NULL,
    PRIMARY KEY (title_id, season, number, field)
);

CREATE TABLE IF NOT EXISTS cleared_platform_fields (
    platform_id INTEGER NOT NULL REFERENCES platforms (id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    PRIMARY KEY (platform_id, field)
);

-- The other person's watch data file on the Duo tab. There is only ever one.
CREATE TABLE IF NOT EXISTS duo_file (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    content BLOB NOT NULL
);

-- Pictures added in the web UI. Unlike title images, these don't come from info/.
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY,
    media_type TEXT NOT NULL,
    content BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS influencers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    website TEXT NOT NULL DEFAULT '',
    picture_id INTEGER REFERENCES uploads (id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS influencer_socials (
    influencer_id INTEGER NOT NULL REFERENCES influencers (id) ON DELETE CASCADE,
    network TEXT NOT NULL,
    url TEXT NOT NULL,
    PRIMARY KEY (influencer_id, network)
);

CREATE TABLE IF NOT EXISTS collages (
    id INTEGER PRIMARY KEY,
    influencer_id INTEGER NOT NULL REFERENCES influencers (id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    UNIQUE (influencer_id, name)
);

-- A note's text, a link's or video's title, or an image's caption all go in text.
CREATE TABLE IF NOT EXISTS collage_items (
    id INTEGER PRIMARY KEY,
    collage_id INTEGER NOT NULL REFERENCES collages (id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('note', 'link', 'video', 'image')),
    text TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    video_provider TEXT CHECK (video_provider IN ('youtube', 'tiktok')),
    video_id TEXT,
    upload_id INTEGER REFERENCES uploads (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trimmed_seasons (
    title_id INTEGER NOT NULL REFERENCES titles (id) ON DELETE CASCADE,
    season INTEGER NOT NULL,
    episodes INTEGER NOT NULL,
    PRIMARY KEY (title_id, season)
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
        "singleplayer": "INTEGER NOT NULL DEFAULT 1",
        "multiplayer": "INTEGER",
        "multiplayer_status": MULTIPLAYER_STATUS_COLUMN,
        "age_restriction": AGE_RESTRICTION_COLUMN,
        "franchise": "TEXT NOT NULL DEFAULT ''",
    },
    "platforms": {
        "kind": PLATFORM_KIND_COLUMN,
        "release_year": "INTEGER",
        "image": "TEXT",
    },
    "episodes": {
        "duration": "INTEGER",
        "release_date": "TEXT",
        "description": "TEXT NOT NULL DEFAULT ''",
        "rating": RATING_COLUMN,
    },
}

COPIED_COLUMNS = (
    "id, kind, name, year, duration, release_date, hours_played, producer,"
    " description, notes, favorite, image, watched_on, updated_at,"
    " singleplayer, multiplayer, multiplayer_status, age_restriction, franchise"
)


def fold(text: str | None) -> str | None:
    """Text without accents or case, so "pokemon" matches "Pokémon" and "POKÉMON"."""
    if text is None:
        return None
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


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
    conn.create_function("fold", 1, fold, deterministic=True)
    conn.execute("PRAGMA foreign_keys = ON")
    if _version(conn) < SCHEMA_VERSION:
        if _has_table(conn, "titles"):
            _upgrade(conn)
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            # A table that isn't there yet comes from SCHEMA, already complete.
            if existing and name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _rebuild_titles(conn: sqlite3.Connection, version: int) -> None:
    """Rebuild titles, since SQLite can't change CHECK constraints or defaults in place.

    Before version 1 every title started as planned, so planned says nothing about
    what the user chose and becomes none. Later versions keep it, as a real choice.

    Before version 7 ratings went up to 10, not 5, and a game was either owned or not.
    """
    status = "CASE status WHEN 'planned' THEN 'none' ELSE status END" if version < 1 else "status"
    conn.execute(f"CREATE TABLE titles_new ({TITLES_COLUMNS})")
    conn.execute(
        f"INSERT INTO titles_new ({COPIED_COLUMNS}, status, rating, ownership)"
        f" SELECT {COPIED_COLUMNS}, {status}, (rating + 1) / 2,"
        " CASE WHEN owned THEN 'owned' ELSE 'none' END FROM titles"
    )
    conn.execute("DROP TABLE titles")
    conn.execute("ALTER TABLE titles_new RENAME TO titles")


def _sort_platforms(conn: sqlite3.Connection) -> None:
    """Tell consoles from streaming services by name, or by only being used for games."""
    rows = conn.execute(
        "SELECT p.id, p.name, COUNT(t.id) AS uses, COALESCE(SUM(t.kind = 'game'), 0) AS game_uses"
        " FROM platforms p"
        " LEFT JOIN title_platforms tp ON tp.platform_id = p.id"
        " LEFT JOIN titles t ON t.id = tp.title_id"
        " GROUP BY p.id"
    ).fetchall()
    conn.executemany(
        "UPDATE platforms SET kind = ? WHERE id = ?",
        [
            (PlatformKind.CONSOLE.value, row["id"])
            for row in rows
            if guess_platform_kind(row["name"]) is PlatformKind.CONSOLE
            or (row["uses"] and row["uses"] == row["game_uses"])
        ],
    )


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    query = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
    return conn.execute(query, (name,)).fetchone() is not None


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
            if version < 7:
                _rebuild_titles(conn, version)
            if version < 5 and _has_table(conn, "title_platforms"):
                _sort_platforms(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
