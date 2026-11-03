import json
import re
import sqlite3

import pytest

from conftest import movie, series
from plasma import library, watch_data
from plasma.db import connect
from plasma.importer import ImportFileError, import_entries
from plasma.models import ImportEntry, Kind, MultiplayerStatus, Ownership, Status, TitleInput


def add(conn, data) -> int:
    with conn:
        return library.create_title(conn, data)


def game(name: str = "Game", **fields) -> TitleInput:
    return TitleInput(kind=Kind.GAME, name=name, **fields)


def file_with(*titles: dict) -> bytes:
    return json.dumps({"plasma_watch_data": 1, "titles": list(titles)}).encode()


def apply(conn, content: bytes) -> tuple[int, int]:
    with conn:
        return watch_data.load(conn, watch_data.parse(content))


def watched(conn, title_id: int) -> set[tuple[int, int]]:
    rows = conn.execute(
        "SELECT season, number FROM episodes WHERE title_id = ? AND watched = 1", (title_id,)
    )
    return {(row["season"], row["number"]) for row in rows}


def test_export_keeps_only_titles_with_data_and_leaves_blanks_out(conn):
    add(conn, movie("Untouched", year=2000, notes="private"))
    add(conn, movie("Seen", year=2016, status=Status.COMPLETED, rating=5, watched_on="2024-03-02"))
    data = watch_data.export(conn)
    assert data["plasma_watch_data"] == 2
    assert data["titles"] == [
        {
            "kind": "movie",
            "name": "Seen",
            "year": 2016,
            "status": "completed",
            "rating": 5,
            "watched_on": "2024-03-02",
        }
    ]


def test_export_groups_watched_episodes_and_keeps_gaps(conn):
    title_id = add(conn, series("Show", seasons=(3, 5)))
    with conn:
        conn.execute("UPDATE episodes SET watched = 1 WHERE season = 2 AND number IN (1, 2, 5)")
        library.set_episode_rating(conn, title_id, 2, 5, 4)
    entry = watch_data.export(conn)["titles"][0]
    assert entry["seasons"] == [3, 5]
    assert entry["watched"] == [[2, 1, 2], [2, 5, 5]]
    assert entry["episodes"] == {"S02E05": {"rating": 4}}


def test_a_rated_episode_alone_is_enough_to_be_exported(conn):
    title_id = add(conn, series("Show", seasons=(2,)))
    with conn:
        library.set_episode_rating(conn, title_id, 1, 2, 3)
    assert [t["name"] for t in watch_data.export(conn)["titles"]] == ["Show"]


def test_export_can_be_loaded_into_an_empty_library(conn, tmp_path):
    add(conn, movie("Film", year=1999, status=Status.PLANNED, rating=3))
    show = add(conn, series("Show", seasons=(2, 2), rating=4))
    add(
        conn,
        game(
            "Duo",
            singleplayer=False,
            multiplayer=True,
            multiplayer_status=MultiplayerStatus.WATCHING,
        ),
    )
    with conn:
        library.toggle_episode(conn, show, 2, 1)
        library.set_episode_rating(conn, show, 1, 1, 5)

    other = connect(tmp_path / "other.db")
    assert apply(other, watch_data.dump(watch_data.export(conn)).encode()) == (3, 0)

    copied = watch_data.export(other)
    assert copied == watch_data.export(conn)
    title = library.find_title(other, Kind.SERIES, "Show", None)
    assert title["status"] == Status.WATCHING.value
    assert library.get_episode(other, title["id"], 1, 1)["rating"] == 5
    other.close()


def test_favorites_and_ownership_travel_in_the_file(conn, tmp_path):
    with conn:
        library.toggle_favorite(conn, library.create_title(conn, movie("Liked")))
        library.set_ownership(conn, library.create_title(conn, game("Boxed")), Ownership.OWNED)
    data = watch_data.export(conn)
    assert [t["name"] for t in data["titles"]] == ["Boxed", "Liked"]

    other = connect(tmp_path / "other.db")
    apply(other, watch_data.dump(data).encode())
    assert watch_data.export(other)["titles"] == data["titles"]
    other.close()


def test_a_newer_file_clears_favorite_and_ownership_it_leaves_blank(conn):
    title_id = add(conn, game("Boxed"))
    with conn:
        library.toggle_favorite(conn, title_id)
        library.set_ownership(conn, title_id, Ownership.OWNED)
    apply(conn, b'{"plasma_watch_data": 2, "titles": [{"kind": "game", "name": "Boxed"}]}')
    title = library.get_title(conn, title_id)
    assert (title["favorite"], title["ownership"]) == (0, "none")


def test_an_older_file_leaves_favorite_and_ownership_alone(conn):
    title_id = add(conn, game("Boxed"))
    with conn:
        library.toggle_favorite(conn, title_id)
        library.set_ownership(conn, title_id, Ownership.OWNED)
    apply(conn, file_with({"kind": "game", "name": "Boxed", "rating": 3}))
    title = library.get_title(conn, title_id)
    assert (title["favorite"], title["ownership"], title["rating"]) == (1, "owned", 3)


def test_load_overwrites_tracking_and_leaves_the_rest(conn):
    title_id = add(conn, movie("Film", year=2000, status=Status.DROPPED, rating=2, notes="mine"))
    other = add(conn, movie("Other", status=Status.PLANNED, rating=1))

    counts = apply(conn, file_with({"kind": "movie", "name": "film", "year": 2000, "rating": 5}))

    assert counts == (0, 1)
    title = library.get_title(conn, title_id)
    assert (title["status"], title["rating"], title["notes"]) == ("none", 5, "mine")
    assert library.get_title(conn, other)["rating"] == 1


def test_load_blank_in_the_file_clears_and_survives_an_import(conn):
    title_id = add(conn, movie("Film", rating=2))
    apply(conn, file_with({"kind": "movie", "name": "Film", "status": "planned"}))
    assert library.get_title(conn, title_id)["rating"] is None

    with conn:
        import_entries(conn, [ImportEntry(kind=Kind.MOVIE, name="Film", rating=4)], [])
    assert library.get_title(conn, title_id)["rating"] is None


def test_cleared_episode_rating_survives_an_import(conn):
    title_id = add(conn, series("Show", seasons=(2,)))
    with conn:
        library.set_episode_rating(conn, title_id, 1, 1, 3)
    apply(conn, file_with({"kind": "series", "name": "Show", "seasons": [2], "status": "planned"}))
    assert library.get_episode(conn, title_id, 1, 1)["rating"] is None

    entry = ImportEntry(
        kind=Kind.SERIES, name="Show", seasons=[2], episodes={"S01E01": {"rating": 5}}
    )
    with conn:
        import_entries(conn, [entry], [])
    assert library.get_episode(conn, title_id, 1, 1)["rating"] is None


def test_load_overwrites_episodes_and_keeps_dates_that_stay_watched(conn):
    title_id = add(conn, series("Show", seasons=(3,)))
    with conn:
        library.toggle_episode(conn, title_id, 1, 2)
    apply(
        conn,
        file_with(
            {"kind": "series", "name": "Show", "seasons": [3], "watched": [[1, 2, 3]]},
        ),
    )
    assert watched(conn, title_id) == {(1, 2), (1, 3)}
    assert library.get_episode(conn, title_id, 1, 1)["watched_on"] is None
    assert library.get_episode(conn, title_id, 1, 2)["watched_on"] is not None
    assert library.get_episode(conn, title_id, 1, 3)["watched_on"] is None
    assert library.get_title(conn, title_id)["status"] == Status.WATCHING.value


def test_load_grows_seasons_and_leaves_ones_the_file_does_not_list(conn):
    title_id = add(conn, series("Show", seasons=(2, 2)))
    with conn:
        library.mark_season(conn, title_id, 2, True)
    apply(
        conn,
        file_with({"kind": "series", "name": "Show", "seasons": [3], "watched": [[1, 3, 3]]}),
    )
    assert library.season_counts(conn, title_id) == [3, 2]
    assert watched(conn, title_id) == {(1, 3), (2, 1), (2, 2)}


def test_completed_series_without_seasons_in_the_file_marks_local_episodes(conn):
    title_id = add(conn, series("Show", seasons=(2,)))
    apply(conn, file_with({"kind": "series", "name": "Show", "status": "completed"}))
    assert watched(conn, title_id) == {(1, 1), (1, 2)}
    assert library.get_title(conn, title_id)["status"] == Status.COMPLETED.value


def test_series_status_follows_episodes_when_this_machine_has_more_seasons(conn):
    title_id = add(conn, series("Show", seasons=(2, 2)))
    apply(
        conn,
        file_with(
            {
                "kind": "series",
                "name": "Show",
                "status": "completed",
                "seasons": [2],
                "watched": [[1, 1, 2]],
            }
        ),
    )
    assert library.get_title(conn, title_id)["status"] == Status.WATCHING.value


def test_load_adds_a_title_deleted_here(conn):
    title_id = add(conn, movie("Film"))
    with conn:
        library.delete_title(conn, title_id)
    assert apply(conn, file_with({"kind": "movie", "name": "Film", "rating": 4})) == (1, 0)
    assert library.find_title(conn, Kind.MOVIE, "Film", None)["rating"] == 4


def test_load_turns_on_the_modes_a_game_status_needs(conn):
    title_id = add(conn, game("Duo", singleplayer=False, multiplayer=True))
    apply(conn, file_with({"kind": "game", "name": "Duo", "status": "completed"}))
    title = library.get_title(conn, title_id)
    assert (title["singleplayer"], title["multiplayer"], title["status"]) == (1, 1, "completed")


@pytest.mark.parametrize(
    "content",
    [b"", b"not json", b"\xff\xfe", b"[]", b'{"titles": []}', b'{"plasma_watch_data": 1, "1": 2}'],
)
def test_parse_rejects_other_files(content):
    with pytest.raises(ImportFileError):
        watch_data.parse(content)


def test_parse_reports_every_invalid_title():
    content = file_with(
        {"kind": "movie", "name": "Film", "rating": 9},
        {"kind": "series", "name": "Show", "seasons": [2], "watched": [[1, 1, 3]]},
        {"kind": "movie", "name": "Film", "watched": [[1, 2, 1]]},
    )
    with pytest.raises(ImportFileError) as caught:
        watch_data.parse(content)
    assert len(caught.value.problems) == 3
    assert "titles.0.rating" in caught.value.problems[0]


def test_parse_rejects_a_newer_format():
    with pytest.raises(ImportFileError):
        watch_data.parse(b'{"plasma_watch_data": 3, "titles": []}')


def test_data_page_is_in_the_navigation_and_help(client):
    assert 'href="/duo"' in client.get("/").text.split("</nav>")[0]
    assert 'id="data"' in client.get("/help").text
    assert "Export watch data" in client.get("/duo").text


def test_download_is_a_json_attachment(client, conn):
    add(conn, movie("Film", rating=4))
    response = client.get("/data/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert re.fullmatch(
        r'attachment; filename="plasma-watch-data-\d{4}-\d\d-\d\d\.json"',
        response.headers["content-disposition"],
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["titles"] == [{"kind": "movie", "name": "Film", "rating": 4}]


def test_upload_loads_the_file_reports_it_and_saves_a_backup(client, conn, db_path):
    title_id = add(conn, movie("Film", rating=2))
    content = file_with(
        {"kind": "movie", "name": "Film", "rating": 5},
        {"kind": "movie", "name": "New", "status": "planned"},
    )

    response = client.post("/data/load", files={"file": ("data.json", content)})

    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "1 title added, 1 updated" in page
    assert library.get_title(conn, title_id)["rating"] == 5
    backup = next(db_path.parent.glob("backup-before-load-*.db"))
    assert backup.name in page
    saved = sqlite3.connect(backup)
    assert saved.execute("SELECT rating FROM titles").fetchall() == [(2,)]
    saved.close()


def test_invalid_upload_changes_nothing_and_shows_every_problem(client, conn, db_path):
    title_id = add(conn, movie("Film", rating=2))
    content = file_with(
        {"kind": "movie", "name": "Film", "rating": 5},
        {"kind": "movie", "name": "Bad", "rating": 9},
    )

    response = client.post("/data/load", files={"file": ("data.json", content)})

    assert response.status_code == 422
    assert 'class="errors"' in response.text and "titles.1.rating" in response.text
    assert library.get_title(conn, title_id)["rating"] == 2
    assert not list(db_path.parent.glob("backup-before-load-*.db"))


def test_load_without_a_file_is_explained(client):
    response = client.post("/data/load")
    assert response.status_code == 422
    assert "a Plasma watch data file" in response.text
