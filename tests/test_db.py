import sqlite3

from plasma import library
from plasma.db import SCHEMA_VERSION, connect
from plasma.models import Kind, Status, TitleInput

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


def test_upgrade_adds_release_dates_and_platforms(db_path):
    old_database(db_path)
    conn = connect(db_path)
    assert library.get_title(conn, 1)["release_date"] is None
    assert library.episodes_by_season(conn, 2)[1][0]["release_date"] is None
    assert library.list_platforms(conn) == []
    conn.close()


def test_upgrade_from_version_1_keeps_planned_titles(db_path):
    conn = connect(db_path)
    with conn:
        conn.execute("INSERT INTO titles (kind, name, status) VALUES ('movie', 'Film', 'planned')")
    conn.execute("ALTER TABLE titles DROP COLUMN release_date")
    conn.execute("ALTER TABLE episodes DROP COLUMN release_date")
    conn.executescript("DROP TABLE title_platforms; DROP TABLE platforms;")
    conn.execute("PRAGMA user_version = 1")
    conn.close()

    conn = connect(db_path)
    assert library.get_title(conn, 1)["status"] == "planned"
    assert library.get_title(conn, 1)["release_date"] is None
    assert library.list_platforms(conn) == []
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


# Version 2, the release before games: titles couldn't have the game kind.
VERSION_2_SCHEMA = """
CREATE TABLE titles (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('movie', 'series')),
    name TEXT NOT NULL,
    year INTEGER,
    status TEXT NOT NULL DEFAULT 'none'
        CHECK (status IN ('none', 'planned', 'watching', 'completed', 'dropped')),
    rating INTEGER CHECK (rating BETWEEN 1 AND 10),
    duration INTEGER,
    release_date TEXT,
    description TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    favorite INTEGER NOT NULL DEFAULT 0,
    image TEXT,
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
    duration INTEGER,
    release_date TEXT,
    description TEXT NOT NULL DEFAULT '',
    watched INTEGER NOT NULL DEFAULT 0,
    watched_on TEXT,
    UNIQUE (title_id, season, number)
);
CREATE TABLE platforms (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    url TEXT NOT NULL DEFAULT ''
);
CREATE TABLE title_platforms (
    title_id INTEGER NOT NULL REFERENCES titles (id) ON DELETE CASCADE,
    platform_id INTEGER NOT NULL REFERENCES platforms (id) ON DELETE CASCADE,
    PRIMARY KEY (title_id, platform_id)
);
INSERT INTO titles (id, kind, name, status, favorite, release_date)
    VALUES (1, 'movie', 'Film', 'planned', 1, '2026-11-20'),
           (2, 'series', 'Show', 'watching', 0, NULL);
INSERT INTO episodes (title_id, season, number, watched, duration)
    VALUES (2, 1, 1, 1, 45), (2, 1, 2, 0, NULL);
INSERT INTO platforms (id, name) VALUES (1, 'Netflix');
INSERT INTO title_platforms (title_id, platform_id) VALUES (1, 1), (2, 1);
PRAGMA user_version = 2;
"""


def test_upgrade_from_version_2_keeps_everything_and_allows_games(db_path):
    old_database(db_path, VERSION_2_SCHEMA)
    conn = connect(db_path)

    film, show = library.get_title(conn, 1), library.get_title(conn, 2)
    assert (film["status"], film["favorite"], film["release_date"]) == (
        "planned",
        1,
        "2026-11-20",
    )
    assert (show["episodes_total"], show["episodes_watched"]) == (2, 1)
    assert library.episodes_by_season(conn, 2)[1][0]["duration"] == 45
    assert [row["name"] for row in library.title_platforms(conn, 1)] == ["Netflix"]
    assert (film["producer"], film["ownership"]) == ("", "none")
    assert library.list_plans(conn) == []
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    with conn:
        library.create_title(conn, TitleInput(kind="game", name="Hades", hours_played=5))
    assert library.list_titles(conn, Kind.GAME)[0]["hours_played"] == 5
    conn.close()


def test_upgrade_from_version_3_keeps_data(db_path):
    conn = connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO titles (kind, name, status, hours_played)"
            " VALUES ('game', 'Hades', 'planned', 5)"
        )
    conn.execute("ALTER TABLE titles DROP COLUMN producer")
    conn.executescript("DROP TABLE plan_items; DROP TABLE plans;")
    conn.execute("PRAGMA user_version = 3")
    conn.close()

    conn = connect(db_path)
    title = library.get_title(conn, 1)
    assert (title["status"], title["hours_played"]) == ("planned", 5)
    assert (title["producer"], title["ownership"]) == ("", "none")
    assert library.list_plans(conn) == []
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


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


def test_upgrade_adds_game_modes_and_sorts_platforms(db_path):
    conn = connect(db_path)
    conn.executescript(
        """
        INSERT INTO titles (id, kind, name) VALUES (1, 'game', 'Hades'), (2, 'movie', 'Film');
        INSERT INTO platforms (id, name) VALUES
            (1, 'Netflix'), (2, 'Steam'), (3, 'Arcade Box'), (4, 'Both Ways'), (5, 'Unused');
        INSERT INTO title_platforms (title_id, platform_id) VALUES
            (2, 1), (1, 3), (1, 4), (2, 4);
        ALTER TABLE titles DROP COLUMN singleplayer;
        ALTER TABLE titles DROP COLUMN multiplayer;
        ALTER TABLE titles DROP COLUMN multiplayer_status;
        ALTER TABLE titles DROP COLUMN age_restriction;
        ALTER TABLE platforms DROP COLUMN kind;
        DROP TABLE title_genres;
        PRAGMA user_version = 4;
        """
    )
    conn.close()

    conn = connect(db_path)
    kinds = dict(conn.execute("SELECT name, kind FROM platforms").fetchall())
    assert kinds == {
        "Netflix": "streaming",
        "Steam": "console",
        "Arcade Box": "console",
        "Both Ways": "streaming",
        "Unused": "streaming",
    }
    game = library.get_title(conn, 1)
    assert (game["singleplayer"], game["multiplayer"], game["multiplayer_status"]) == (
        1,
        None,
        "none",
    )
    assert library.title_genres(conn, 1) == []
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_upgrade_to_stars_and_ownership(db_path):
    conn = connect(db_path)
    conn.executescript(
        """
        PRAGMA ignore_check_constraints = ON;
        INSERT INTO titles (id, kind, name, rating) VALUES
            (1, 'game', 'Ten', 10), (2, 'game', 'Nine', 9), (3, 'movie', 'Eight', 8),
            (4, 'movie', 'Two', 2), (5, 'movie', 'One', 1), (6, 'movie', 'Unrated', NULL);
        INSERT INTO episodes (title_id, season, number) VALUES (6, 1, 1);
        ALTER TABLE titles ADD COLUMN owned INTEGER NOT NULL DEFAULT 0;
        UPDATE titles SET owned = 1 WHERE id = 1;
        ALTER TABLE titles DROP COLUMN ownership;
        ALTER TABLE titles DROP COLUMN franchise;
        ALTER TABLE episodes DROP COLUMN rating;
        PRAGMA user_version = 6;
        PRAGMA ignore_check_constraints = OFF;
        """
    )
    conn.close()

    conn = connect(db_path)
    ratings = dict(conn.execute("SELECT name, rating FROM titles").fetchall())
    assert ratings == {"Ten": 5, "Nine": 5, "Eight": 4, "Two": 1, "One": 1, "Unrated": None}
    owned = conn.execute("SELECT name FROM titles WHERE ownership = 'owned'").fetchall()
    assert [row["name"] for row in owned] == ["Ten"]
    assert library.get_title(conn, 6)["episodes_total"] == 1
    assert library.get_episode(conn, 6, 1, 1)["rating"] is None
    assert library.get_title(conn, 1)["franchise"] == ""
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
