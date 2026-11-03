import json

import pytest

from conftest import movie, series
from plasma import duo, library, watch_data
from plasma.models import Kind, MultiplayerStatus, Ownership, Status, TitleInput


def add(conn, data, favorite: bool = False, ownership: Ownership = Ownership.NONE) -> int:
    with conn:
        title_id = library.create_title(conn, data)
        library.set_favorite(conn, title_id, favorite)
        library.set_ownership(conn, title_id, ownership)
    return title_id


def game(name: str = "Game", **fields) -> TitleInput:
    return TitleInput(kind=Kind.GAME, name=name, **fields)


def content(*titles: dict, version: int = 2) -> bytes:
    return json.dumps({"plasma_watch_data": version, "titles": list(titles)}).encode()


def sections(conn, *titles: dict) -> dict[str, list[duo.Item]]:
    found = duo.suggestions(conn, watch_data.parse(content(*titles)))
    return {section.heading: section.items for section in found}


def names(conn, heading: str, *titles: dict) -> list[str]:
    return [item.pair.name for item in sections(conn, *titles).get(heading, [])]


def test_favorites_of_you_both(conn):
    add(conn, movie("Shared"), favorite=True)
    add(conn, movie("Only mine"), favorite=True)
    theirs = [
        {"kind": "movie", "name": "shared", "favorite": True},
        {"kind": "movie", "name": "Only theirs", "favorite": True},
    ]
    assert names(conn, "Favorites of you both", *theirs) == ["Shared"]


def test_planned_by_you_both_counts_multiplayer_plans(conn):
    add(conn, movie("Film", status=Status.PLANNED))
    add(
        conn,
        game(
            "Duo",
            singleplayer=False,
            multiplayer=True,
            multiplayer_status=MultiplayerStatus.PLANNED,
        ),
    )
    add(conn, movie("Seen", status=Status.COMPLETED))
    theirs = [
        {"kind": "movie", "name": "Film", "status": "planned"},
        {
            "kind": "game",
            "name": "Duo",
            "singleplayer": False,
            "multiplayer": True,
            "multiplayer_status": "planned",
        },
        {"kind": "movie", "name": "Seen", "status": "planned"},
    ]
    assert names(conn, "Planned by you both", *theirs) == ["Duo", "Film"]


def test_in_progress_says_where_each_of_you_is(conn):
    title_id = add(conn, series("Show", seasons=(3, 3)))
    with conn:
        library.toggle_episode(conn, title_id, 1, 2)
    theirs = {
        "kind": "series",
        "name": "Show",
        "status": "watching",
        "seasons": [3, 3],
        "watched": [[1, 1, 3], [2, 1, 1]],
    }
    item = sections(conn, theirs)["In progress for you both"][0]
    assert item.note == "You're up to S01E03 · They're up to S02E02"


def test_rewatch_skips_games_low_ratings_and_unfinished(conn):
    add(conn, movie("Great", status=Status.COMPLETED, rating=5))
    add(conn, movie("Unrated", status=Status.COMPLETED))
    add(conn, movie("Meh", status=Status.COMPLETED, rating=2))
    add(conn, movie("Half", status=Status.WATCHING))
    add(conn, game("Played", status=Status.COMPLETED))
    theirs = [
        {"kind": kind, "name": name, "status": "completed"}
        for kind, name in (
            ("movie", "Great"),
            ("movie", "Unrated"),
            ("movie", "Meh"),
            ("movie", "Half"),
            ("game", "Played"),
        )
    ]
    assert names(conn, "Rewatch together", *theirs) == ["Great", "Unrated"]


def test_they_loved_covers_titles_you_lack_and_plan(conn):
    add(conn, movie("Planned here", status=Status.PLANNED))
    add(conn, movie("Dropped here", status=Status.DROPPED))
    theirs = [
        {"kind": "movie", "name": name, "status": "completed", "rating": rating}
        for name, rating in (
            ("Planned here", 4),
            ("Dropped here", 5),
            ("New here", 5),
            ("So-so", 3),
        )
    ]
    items = sections(conn, *theirs)["They loved, you haven't seen"]
    assert [(i.pair.name, i.pair.title_id is None, i.note) for i in items] == [
        ("Dropped here", False, "Them ★ 5"),
        ("New here", True, "Them ★ 5 · Not in your library"),
        ("Planned here", False, "Them ★ 4 · On your Planned list"),
    ]


def test_a_title_they_dropped_can_still_be_loved_and_new_to_them(conn):
    add(conn, movie("Rated", rating=5))
    add(conn, movie("Liked here", status=Status.COMPLETED, rating=4))
    theirs = [
        {"kind": "movie", "name": "Rated", "status": "dropped", "rating": 5},
        {"kind": "movie", "name": "Liked here", "status": "dropped"},
        {"kind": "movie", "name": "Elsewhere", "status": "dropped", "rating": 5},
    ]
    found = sections(conn, *theirs)
    assert [i.pair.name for i in found["They loved, you haven't seen"]] == ["Elsewhere"]
    assert [i.pair.name for i in found["You loved, they haven't seen"]] == ["Liked here"]


def test_singleplayer_games_are_not_suggested_for_two(conn):
    add(conn, game("Solo", status=Status.COMPLETED, rating=5))
    add(conn, game("Party", multiplayer=True, rating=5))
    assert names(conn, "You loved, they haven't seen") == ["Party"]


def test_you_loved_mentions_their_plans(conn):
    add(conn, movie("Film", status=Status.COMPLETED, rating=5))
    item = sections(conn, {"kind": "movie", "name": "Film", "status": "planned"})[
        "You loved, they haven't seen"
    ][0]
    assert item.note == "You ★ 5 · On their Planned list"


def test_owned_games_are_split_by_multiplayer(conn):
    add(conn, game("Party", multiplayer=True), ownership=Ownership.OWNED)
    add(conn, game("Solo"), ownership=Ownership.OWNED)
    add(
        conn,
        game("Paused", multiplayer=True, multiplayer_status=MultiplayerStatus.DROPPED),
        ownership=Ownership.OWNED,
    )
    add(conn, game("Mine only"), ownership=Ownership.OWNED)
    theirs = [
        {"kind": "game", "name": name, "ownership": "owned"}
        for name in ("Party", "Solo", "Paused")
    ]
    assert names(conn, "Multiplayer games you both own", *theirs) == ["Party", "Paused"]
    assert names(conn, "Other games you both own", *theirs) == ["Solo"]


def test_games_you_both_want(conn):
    add(conn, game("Soon"), ownership=Ownership.WANTED)
    add(conn, game("Have"), ownership=Ownership.OWNED)
    theirs = [
        {"kind": "game", "name": "Soon", "ownership": "wanted"},
        {"kind": "game", "name": "Have", "ownership": "wanted"},
    ]
    assert names(conn, "Games you both want", *theirs) == ["Soon"]


@pytest.mark.parametrize(
    "mine, theirs, matches",
    [(2016, 2016, True), (None, 2016, True), (2016, None, True), (2016, 2017, False)],
)
def test_titles_match_by_year_or_a_missing_year(conn, mine, theirs, matches):
    add(conn, movie("Film", year=mine), favorite=True)
    entry = {"kind": "movie", "name": "Film", "year": theirs, "favorite": True}
    assert names(conn, "Favorites of you both", entry) == (["Film"] if matches else [])


def test_an_exact_year_beats_a_missing_one(conn):
    add(conn, movie("Film"), favorite=True)
    add(conn, movie("Film", year=2016), favorite=True)
    entry = {"kind": "movie", "name": "Film", "year": 2016, "favorite": True}
    items = sections(conn, entry)["Favorites of you both"]
    assert [item.pair.year for item in items] == [2016]


def test_same_name_of_another_kind_does_not_match(conn):
    add(conn, movie("Dune"), favorite=True)
    entry = {"kind": "game", "name": "Dune", "favorite": True}
    assert names(conn, "Favorites of you both", entry) == []


def test_best_rated_come_first(conn):
    for name, rating in (("B", 3), ("A", 3), ("C", 5)):
        add(conn, movie(name, rating=rating), favorite=True)
    theirs = [{"kind": "movie", "name": n, "favorite": True} for n in "ABC"]
    assert names(conn, "Favorites of you both", *theirs) == ["C", "A", "B"]


def test_duo_asks_for_a_file_first(client):
    page = client.get("/duo").text
    assert "Add file" in page and "Forget file" not in page


def test_duo_file_is_kept_and_shows_sections_without_changing_the_library(client, conn):
    title_id = add(conn, movie("Shared", rating=2), favorite=True)
    file = content(
        {"kind": "movie", "name": "Shared", "favorite": True, "rating": 5},
        {"kind": "movie", "name": "Stranger", "rating": 5},
    )

    response = client.post("/duo/file", files={"file": ("them.json", file)})

    assert response.status_code == 303 and response.headers["location"] == "/duo"
    page = client.get("/duo").text
    assert "Favorites of you both" in page
    assert f'href="/titles/{title_id}"' in page
    assert "<strong>Stranger</strong>" in page
    assert library.get_title(conn, title_id)["rating"] == 2
    assert len(library.list_titles(conn)) == 1


def test_invalid_duo_file_keeps_the_previous_one(client, conn):
    add(conn, movie("Shared"), favorite=True)
    good = content({"kind": "movie", "name": "Shared", "favorite": True})
    client.post("/duo/file", files={"file": ("them.json", good)})

    response = client.post("/duo/file", files={"file": ("bad.json", b"nope")})

    assert response.status_code == 422
    assert 'class="errors"' in response.text and "Favorites of you both" in response.text


def test_replacing_and_forgetting_the_duo_file(client, conn):
    add(conn, movie("Shared"), favorite=True)
    add(conn, movie("Other"), favorite=True)
    for name in ("Shared", "Other"):
        file = content({"kind": "movie", "name": name, "favorite": True})
        client.post("/duo/file", files={"file": ("them.json", file)})
    assert library.get_duo_file(conn) == file

    assert client.post("/duo/file/delete").headers["location"] == "/duo"
    assert library.get_duo_file(conn) is None
    assert "Add file" in client.get("/duo").text


def test_older_file_is_flagged(client):
    client.post("/duo/file", files={"file": ("old.json", content(version=1))})
    assert "older version" in client.get("/duo").text


def test_no_overlap_says_so(client):
    client.post("/duo/file", files={"file": ("them.json", content())})
    assert "Nothing in common yet" in client.get("/duo").text


def test_duo_is_explained_in_help(client):
    assert 'id="duo"' in client.get("/help").text


def test_old_data_link_lands_on_export_and_import(client):
    response = client.get("/data")
    assert response.status_code == 303 and response.headers["location"] == "/duo#your-data"


def test_duo_tab_shows_duo_before_export_and_import(client):
    page = client.get("/duo").text
    assert page.index('id="duo"') < page.index("Export watch data") < page.index("Import file")


def test_duo_errors_stay_out_of_the_load_card(client):
    response = client.post("/duo/file", files={"file": ("bad.json", b"nope")})
    assert response.status_code == 422
    duo_part, data_part = response.text.split('id="your-data"')
    assert 'class="errors"' in duo_part and 'class="errors"' not in data_part
