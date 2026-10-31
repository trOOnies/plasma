import sqlite3

from plasma import library
from plasma.db import SCHEMA_VERSION, connect
from plasma.models import Status

# The schema as first released, without the columns added since.
FIRST_SCHEMA = """
CREATE TABLE titles (
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
CREATE UNIQUE INDEX titles_identity
    ON titles (kind, name COLLATE NOCASE, COALESCE(year, 0));
CREATE TABLE episodes (
    id INTEGER PRIMARY KEY,
    title_id INTEGER NOT NULL REFERENCES titles (id) ON DELETE CASCADE,
    season INTEGER NOT NULL,
    number INTEGER NOT NULL,
    watched INTEGER NOT NULL DEFAULT 0,
    watched_on TEXT,
    UNIQUE (title_id, season, number)
);
INSERT INTO titles (id, kind, name, status, notes, updated_at)
    VALUES (1, 'movie', 'Film', 'planned', 'Mine', '2020-01-01 00:00:00'),
           (2, 'series', 'Show', 'watching', '', '2020-01-01 00:00:00'),
           (3, 'movie', 'Seen', 'completed', '', '2020-01-01 00:00:00');
INSERT INTO episodes (title_id, season, number, watched, watched_on)
    VALUES (2, 1, 1, 1, '2020-01-01'), (2, 1, 2, 0, NULL);
"""

# The release after that, which had added description, favorite and image.
SECOND_RELEASE_COLUMNS = """
ALTER TABLE titles ADD COLUMN description TEXT NOT NULL DEFAULT '';
ALTER TABLE titles ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0;
ALTER TABLE titles ADD COLUMN image TEXT;
UPDATE titles SET description = 'Plot', favorite = 1, image = 'a.jpg' WHERE id = 1;
"""


def old_database(path, script=FIRST_SCHEMA):
    conn = sqlite3.connect(path)
    conn.executescript(script)
    conn.close()


def test_new_database_starts_at_the_latest_version(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_upgrade_clears_the_old_default_status_and_keeps_chosen_ones(db_path):
    old_database(db_path)
    conn = connect(db_path)
    statuses = dict(conn.execute("SELECT name, status FROM titles").fetchall())
    conn.close()
    assert statuses == {"Film": "none", "Show": "watching", "Seen": "completed"}


def test_upgrade_keeps_episodes_and_their_links(db_path):
    old_database(db_path)
    conn = connect(db_path)
    title = library.get_title(conn, 2)
    first = library.episodes_by_season(conn, 2)[1][0]
    assert (title["episodes_total"], title["episodes_watched"]) == (2, 1)
    assert first["watched_on"] == "2020-01-01"
    assert (first["duration"], first["description"]) == (None, "")
    conn.execute("DELETE FROM titles WHERE id = 2")
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0
    conn.close()


def test_upgrade_keeps_other_columns_and_the_identity_index(db_path):
    old_database(db_path, FIRST_SCHEMA + SECOND_RELEASE_COLUMNS)
    conn = connect(db_path)
    title = library.get_title(conn, 1)
    assert (title["notes"], title["updated_at"]) == ("Mine", "2020-01-01 00:00:00")
    assert (title["description"], title["favorite"], title["image"]) == ("Plot", 1, "a.jpg")
    indexes = [row["name"] for row in conn.execute("PRAGMA index_list(titles)")]
    conn.close()
    assert "titles_identity" in indexes


def test_upgrade_only_runs_once(db_path):
    old_database(db_path)
    conn = connect(db_path)
    with conn:
        library.set_status(conn, 1, Status.PLANNED)
    conn.close()

    conn = connect(db_path)
    assert library.get_title(conn, 1)["status"] == "planned"
    conn.close()


def test_failed_upgrade_leaves_the_database_untouched(db_path, monkeypatch):
    old_database(db_path)
    monkeypatch.setattr("plasma.db.COPIED_COLUMNS", "id, no_such_column")
    try:
        connect(db_path)
    except sqlite3.OperationalError:
        pass
    monkeypatch.undo()

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT status FROM titles WHERE id = 1").fetchone() == ("planned",)
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone() == (2,)
    assert conn.execute("PRAGMA user_version").fetchone() == (0,)
    conn.close()

    conn = connect(db_path)
    assert library.get_title(conn, 2)["episodes_total"] == 2
    conn.close()
