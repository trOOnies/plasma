from pathlib import Path

import pytest

from plasma import importer, library, seed
from plasma.models import Details, Kind, Ownership, TitleInput


def write(directory: Path, text: str, name: str = "library.yaml") -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


FILE = """
movie:
  - name: 1917
    year: 2019
    status: completed
    watched_on: 2024-03-02
    rating: 4
series:
  - name: Show
    year: 2020
    seasons: [3, 2]
    watched_through: S01E03
    notes: Good
  - name: Done
    seasons: [2]
    status: completed
"""


def test_import_creates_titles_with_progress(conn, tmp_path):
    result = importer.import_paths(conn, [write(tmp_path, FILE)])
    assert result.added == 3

    film = library.find_title(conn, Kind.MOVIE, "1917", 2019)
    assert film["watched_on"] == "2024-03-02"
    show = library.find_title(conn, Kind.SERIES, "show", 2020)
    assert (show["status"], show["episodes_watched"]) == ("watching", 3)
    done = library.find_title(conn, Kind.SERIES, "Done", None)
    assert (done["status"], done["episodes_watched"]) == ("completed", 2)


def test_imported_movie_without_date_stays_undated(conn, tmp_path):
    path = write(tmp_path, "movie:\n  - name: Film\n    status: completed\n")
    importer.import_paths(conn, [path])
    assert library.find_title(conn, Kind.MOVIE, "Film", None)["watched_on"] is None


def test_reimport_changes_nothing(conn, tmp_path):
    path = write(tmp_path, FILE)
    importer.import_paths(conn, [path])
    result = importer.import_paths(conn, [path])
    assert (result.added, result.updated, result.unchanged) == (0, 0, 3)


def test_reimport_keeps_changes_made_in_the_app(conn, tmp_path):
    path = write(tmp_path, FILE)
    importer.import_paths(conn, [path])
    show = library.find_title(conn, Kind.SERIES, "Show", 2020)
    library.toggle_episode(conn, show["id"], 1, 3)
    conn.execute("UPDATE titles SET rating = 4, notes = 'Mine' WHERE id = ?", (show["id"],))

    importer.import_paths(conn, [path])

    show = library.find_title(conn, Kind.SERIES, "Show", 2020)
    assert (show["episodes_watched"], show["rating"], show["notes"]) == (2, 4, "Mine")


def test_reimport_adds_a_new_season_and_reopens_the_series(conn, tmp_path):
    importer.import_paths(conn, [write(tmp_path, FILE)])
    updated = write(tmp_path, "series:\n  - name: Done\n    seasons: [2, 4]\n", "more.yaml")

    result = importer.import_paths(conn, [updated])

    assert result.updated == 1
    done = library.find_title(conn, Kind.SERIES, "Done", None)
    assert (done["status"], done["episodes_watched"], done["episodes_total"]) == ("watching", 2, 6)


def test_reimport_never_removes_episodes(conn, tmp_path):
    importer.import_paths(conn, [write(tmp_path, FILE)])
    shorter = write(tmp_path, "series:\n  - name: Done\n    seasons: [1]\n", "less.yaml")
    importer.import_paths(conn, [shorter])
    assert library.find_title(conn, Kind.SERIES, "Done", None)["episodes_total"] == 2


def test_reimport_fills_blank_rating_and_notes(conn, tmp_path):
    importer.import_paths(conn, [write(tmp_path, "movie:\n  - name: Film\n")])
    rated = write(tmp_path, "movie:\n  - name: Film\n    rating: 3\n    notes: Nice\n", "b.yaml")
    assert importer.import_paths(conn, [rated]).updated == 1
    film = library.find_title(conn, Kind.MOVIE, "Film", None)
    assert (film["rating"], film["notes"]) == (3, "Nice")


def test_reimport_fills_a_blank_description_but_keeps_an_existing_one(conn, tmp_path):
    text = "movie:\n  - name: Film\n    description: {}\n"
    importer.import_paths(conn, [write(tmp_path, "movie:\n  - name: Film\n")])
    assert importer.import_paths(conn, [write(tmp_path, text.format("First"))]).updated == 1
    assert importer.import_paths(conn, [write(tmp_path, text.format("Second"))]).updated == 0
    assert library.find_title(conn, Kind.MOVIE, "Film", None)["description"] == "First"


def test_image_is_imported_and_can_be_replaced(conn, tmp_path):
    (tmp_path / "images" / "movie").mkdir(parents=True)
    (tmp_path / "images" / "movie" / "a.jpg").touch()
    (tmp_path / "images" / "movie" / "b.jpg").touch()
    text = "movie:\n  - name: Film\n    image: {}\n"
    importer.import_paths(conn, [write(tmp_path, text.format("a.jpg"))])
    assert library.find_title(conn, Kind.MOVIE, "Film", None)["image"] == "a.jpg"
    assert importer.import_paths(conn, [write(tmp_path, text.format("b.jpg"))]).updated == 1
    assert library.find_title(conn, Kind.MOVIE, "Film", None)["image"] == "b.jpg"


def test_reimport_without_an_image_keeps_the_current_one(conn, tmp_path):
    (tmp_path / "images" / "movie").mkdir(parents=True)
    (tmp_path / "images" / "movie" / "a.jpg").touch()
    importer.import_paths(conn, [write(tmp_path, "movie:\n  - name: Film\n    image: a.jpg\n")])
    importer.import_paths(conn, [write(tmp_path, "movie:\n  - name: Film\n")])
    assert library.find_title(conn, Kind.MOVIE, "Film", None)["image"] == "a.jpg"


def test_missing_image_file_is_reported_and_nothing_is_written(conn, tmp_path):
    path = write(tmp_path, "movie:\n  - name: Film\n    image: gone.jpg\n")
    with pytest.raises(importer.ImportFileError, match=r"movie\[0\]: image: .*gone.jpg"):
        importer.import_paths(conn, [path])
    assert library.list_titles(conn) == []


def test_duration_and_episode_details_are_imported(conn, tmp_path):
    text = """
movie:
  - name: Film
    duration: 120
series:
  - name: Show
    seasons: [2]
    episodes:
      S01E02:
        duration: 45
        description: Plot
"""
    importer.import_paths(conn, [write(tmp_path, text)])
    assert library.find_title(conn, Kind.MOVIE, "Film", None)["duration"] == 120
    show = library.find_title(conn, Kind.SERIES, "Show", None)
    episode = library.episodes_by_season(conn, show["id"])[1][1]
    assert (episode["duration"], episode["description"]) == (45, "Plot")


def test_reimport_fills_blank_episode_details_for_a_new_season(conn, tmp_path):
    importer.import_paths(conn, [write(tmp_path, "series:\n  - name: Show\n    seasons: [1]\n")])
    text = """
series:
  - name: Show
    seasons: [1, 1]
    episodes:
      S02E01:
        duration: 45
"""
    assert importer.import_paths(conn, [write(tmp_path, text)]).updated == 1
    assert importer.import_paths(conn, [write(tmp_path, text)]).unchanged == 1
    show = library.find_title(conn, Kind.SERIES, "Show", None)
    assert library.episodes_by_season(conn, show["id"])[2][0]["duration"] == 45


def test_reimport_keeps_episode_details_edited_in_the_app(conn, tmp_path):
    text = """
series:
  - name: Show
    seasons: [1]
    episodes:
      S01E01:
        duration: 45
"""
    importer.import_paths(conn, [write(tmp_path, text)])
    show = library.find_title(conn, Kind.SERIES, "Show", None)
    library.update_episode(conn, show["id"], 1, 1, Details(duration=50))
    importer.import_paths(conn, [write(tmp_path, text)])
    assert library.episodes_by_season(conn, show["id"])[1][0]["duration"] == 50


def test_release_dates_and_platforms_are_imported(conn, tmp_path):
    text = """
movie:
  - name: Film
    release_date: 2026-11-20
    platforms: [Netflix, Hulu]
series:
  - name: Show
    seasons: [1]
    episodes:
      S01E01:
        release_date: 2026-12-25
"""
    importer.import_paths(conn, [write(tmp_path, text)])
    film = library.find_title(conn, Kind.MOVIE, "Film", None)
    assert film["release_date"] == "2026-11-20"
    assert [p["name"] for p in library.title_platforms(conn, film["id"])] == ["Hulu", "Netflix"]
    show = library.find_title(conn, Kind.SERIES, "Show", None)
    assert library.episodes_by_season(conn, show["id"])[1][0]["release_date"] == "2026-12-25"


def test_reimport_fills_in_a_release_date_and_platforms(conn, tmp_path):
    importer.import_paths(conn, [write(tmp_path, "movie:\n  - name: Film\n")])
    text = "movie:\n  - name: Film\n    release_date: 2026-11-20\n    platforms: [Netflix]\n"
    assert importer.import_paths(conn, [write(tmp_path, text)]).updated == 1
    film = library.find_title(conn, Kind.MOVIE, "Film", None)
    assert film["release_date"] == "2026-11-20"
    assert len(library.title_platforms(conn, film["id"])) == 1
    assert importer.import_paths(conn, [write(tmp_path, text)]).unchanged == 1


def test_reimport_keeps_platforms_changed_in_the_app(conn, tmp_path):
    text = "movie:\n  - name: Film\n    platforms: [Netflix, Hulu]\n"
    importer.import_paths(conn, [write(tmp_path, text)])
    film = library.find_title(conn, Kind.MOVIE, "Film", None)
    hulu = library.title_platforms(conn, film["id"])[0]["id"]
    library.set_title_platforms(conn, film["id"], [hulu])
    importer.import_paths(conn, [write(tmp_path, text)])
    assert [p["name"] for p in library.title_platforms(conn, film["id"])] == ["Hulu"]


def test_games_are_imported(conn, tmp_path):
    text = """
game:
  - name: Hades
    year: 2020
    status: playing
    hours_played: 42
    platforms: [Steam]
  - name: Celeste
    status: completed
    watched_on: 2024-08-17
"""
    assert importer.import_paths(conn, [write(tmp_path, text)]).added == 2
    hades = library.find_title(conn, Kind.GAME, "Hades", 2020)
    assert (hades["status"], hades["hours_played"]) == ("watching", 42)
    assert [p["name"] for p in library.title_platforms(conn, hades["id"])] == ["Steam"]
    celeste = library.find_title(conn, Kind.GAME, "Celeste", None)
    assert (celeste["status"], celeste["watched_on"]) == ("completed", "2024-08-17")


def test_reimport_fills_in_hours_played_but_keeps_app_changes(conn, tmp_path):
    importer.import_paths(conn, [write(tmp_path, "game:\n  - name: Hades\n")])
    text = "game:\n  - name: Hades\n    hours_played: {}\n"
    assert importer.import_paths(conn, [write(tmp_path, text.format(10))]).updated == 1
    assert importer.import_paths(conn, [write(tmp_path, text.format(99))]).unchanged == 1
    assert library.find_title(conn, Kind.GAME, "Hades", None)["hours_played"] == 10


def test_game_with_seasons_is_reported(conn, tmp_path):
    path = write(tmp_path, "game:\n  - name: Hades\n    seasons: [3]\n")
    with pytest.raises(importer.ImportFileError, match=r"game\[0\]"):
        importer.import_paths(conn, [path])


def test_producer_and_ownership_are_imported(conn, tmp_path):
    text = """
movie:
  - name: Film
    producer: A24
game:
  - name: Hades
    producer: Supergiant Games
    ownership: wanted
  - name: Celeste
"""
    importer.import_paths(conn, [write(tmp_path, text)])
    assert library.find_title(conn, Kind.MOVIE, "Film", None)["producer"] == "A24"
    hades = library.find_title(conn, Kind.GAME, "Hades", None)
    assert (hades["producer"], hades["ownership"]) == ("Supergiant Games", "wanted")
    assert library.find_title(conn, Kind.GAME, "Celeste", None)["ownership"] == "none"


def test_reimport_fills_a_blank_producer_but_keeps_ownership_changes(conn, tmp_path):
    text = "game:\n  - name: Hades\n    ownership: wanted\n"
    importer.import_paths(conn, [write(tmp_path, text)])
    hades = library.find_title(conn, Kind.GAME, "Hades", None)
    library.set_ownership(conn, hades["id"], Ownership.OWNED)

    text += "    producer: Supergiant Games\n"
    assert importer.import_paths(conn, [write(tmp_path, text)]).updated == 1

    hades = library.find_title(conn, Kind.GAME, "Hades", None)
    assert (hades["producer"], hades["ownership"]) == ("Supergiant Games", "owned")


def test_ownership_on_a_movie_is_reported(conn, tmp_path):
    path = write(tmp_path, "movie:\n  - name: Film\n    ownership: owned\n")
    with pytest.raises(importer.ImportFileError, match="ownership only applies to games"):
        importer.import_paths(conn, [path])


def test_imported_titles_have_no_status_unless_given_one(conn, tmp_path):
    text = "movie:\n  - name: A\n  - name: B\n    status: planned\n"
    importer.import_paths(conn, [write(tmp_path, text)])
    assert library.find_title(conn, Kind.MOVIE, "A", None)["status"] == "none"
    assert library.find_title(conn, Kind.MOVIE, "B", None)["status"] == "planned"


def test_same_title_in_two_files_imports_once(conn, tmp_path):
    first = write(tmp_path, "movie:\n  - name: Film\n", "a.yaml")
    second = write(tmp_path, "movie:\n  - name: film\n", "b.yaml")
    result = importer.import_paths(conn, [first, second])
    assert (result.added, result.unchanged) == (1, 1)


@pytest.mark.parametrize(
    "text, message",
    [
        ("movies: [", "invalid YAML"),
        ("- name: Film\n", "top level"),
        ("films:\n  - name: Film\n", "top level"),
        ("movie: Film\n", "must be a list"),
        ("movie:\n  - Film\n", "expected a mapping"),
        ("movie:\n  - year: 2000\n", "movie[0]: name"),
        ("movie:\n  - name: Film\n    rating: 11\n", "movie[0]: rating"),
        ("series:\n  - name: Show\n    seasons: [2]\n    watched_through: S09E01\n", "series[0]"),
    ],
)
def test_bad_input_is_reported(conn, tmp_path, text, message):
    with pytest.raises(importer.ImportFileError, match=message.replace("[", r"\[")):
        importer.import_paths(conn, [write(tmp_path, text)])


def test_one_bad_file_means_nothing_is_written(conn, tmp_path):
    good = write(tmp_path, "movie:\n  - name: Film\n", "a.yaml")
    bad = write(tmp_path, "movie:\n  - name: Other\n    rating: 99\n", "b.yaml")
    with pytest.raises(importer.ImportFileError):
        importer.import_paths(conn, [good, bad])
    assert library.list_titles(conn) == []


def test_empty_file_is_fine(conn, tmp_path):
    assert importer.import_paths(conn, [write(tmp_path, "# nothing yet\n")]).added == 0


def test_main_reads_yaml_files_from_a_folder(tmp_path, db_path, monkeypatch, capsys):
    monkeypatch.setenv("PLASMA_DB", str(db_path))
    write(tmp_path, "movie:\n  - name: A\n", "a.yaml")
    write(tmp_path, "movie:\n  - name: B\n", "b.yml")
    write(tmp_path, "movie:\n  - name: C\n", "notes.txt")
    assert importer.main([str(tmp_path)]) == 0
    assert "2 added" in capsys.readouterr().out


def test_main_reports_errors(tmp_path, db_path, monkeypatch, capsys):
    monkeypatch.setenv("PLASMA_DB", str(db_path))
    write(tmp_path, "movie:\n  - name: A\n    rating: 99\n")
    assert importer.main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "rating" in err and "Nothing was imported" in err


def full_layout(tmp_path):
    for folder in ("movie", "series", "game", "platform"):
        (tmp_path / "images" / folder).mkdir(parents=True)
    for name in ("movie", "series", "game", "platform"):
        (tmp_path / f"{name}.yaml").touch()


def test_ensure_inputs_creates_a_missing_folder_and_files_empty(tmp_path):
    info = tmp_path / "nested" / "info"
    images = info / "images"
    assert importer.ensure_inputs(info) == [
        info,
        images,
        images / "movie",
        images / "series",
        images / "game",
        images / "platform",
        info / "movie.yaml",
        info / "series.yaml",
        info / "game.yaml",
        info / "platform.yaml",
    ]
    assert (info / "platform.yaml").read_text() == ""


def test_ensure_inputs_only_creates_what_is_missing(tmp_path):
    full_layout(tmp_path)
    assert importer.ensure_inputs(tmp_path) == []
    (tmp_path / "game.yaml").unlink()
    (tmp_path / "images" / "series").rmdir()
    created = [tmp_path / "images" / "series", tmp_path / "game.yaml"]
    assert importer.ensure_inputs(tmp_path) == created


def test_ensure_inputs_leaves_existing_files_alone(tmp_path):
    full_layout(tmp_path)
    path = write(tmp_path, "movie:\n  - name: Film\n", "movie.yaml")
    assert importer.ensure_inputs(tmp_path) == []
    assert path.read_text() == "movie:\n  - name: Film\n"


def test_main_creates_missing_inputs_and_imports_nothing(tmp_path, db_path, monkeypatch, capsys):
    monkeypatch.setenv("PLASMA_DB", str(db_path))
    info = tmp_path / "info"
    assert importer.main([str(info)]) == 0
    out = capsys.readouterr().out
    assert f"Created {info / 'platform.yaml'}" in out and "0 added" in out


def test_main_when_the_folder_path_is_a_file(tmp_path, db_path, monkeypatch, capsys):
    monkeypatch.setenv("PLASMA_DB", str(db_path))
    not_a_folder = write(tmp_path, "", "info")
    assert importer.main([str(not_a_folder)]) == 1
    assert "error:" in capsys.readouterr().err


def test_seed_loads_demo_file_and_is_repeatable(conn, db_path, monkeypatch, capsys):
    monkeypatch.setenv("PLASMA_DB", str(db_path))
    assert seed.main() == 0
    assert seed.main() == 0
    assert "13 already present" in capsys.readouterr().out
    assert len(library.list_titles(conn)) == 13
    wire = library.find_title(conn, Kind.SERIES, "The Wire", 2002)
    assert (wire["status"], wire["episodes_watched"]) == ("watching", 17)
    lost = library.find_title(conn, Kind.SERIES, "Lost", 2004)
    assert lost["status"] == "dropped"


def test_platforms_list_adds_platforms_no_title_uses(conn, tmp_path):
    text = "streaming_service:\n  - name: Netflix\n    url: https://netflix.com\n  - name: Wii\n"
    result = importer.import_paths(conn, [write(tmp_path, text)])
    assert result.platforms_added == 2 and result.added == 0
    platforms = {row["name"]: row["url"] for row in library.list_platforms(conn)}
    assert platforms == {"Netflix": "https://netflix.com", "Wii": ""}


def test_platforms_list_fills_blank_urls_and_keeps_the_rest(conn, tmp_path):
    first = "streaming_service:\n  - name: Steam\n  - name: Netflix\n    url: https://old.example\n"
    importer.import_paths(conn, [write(tmp_path, first)])
    second = (
        "streaming_service:\n  - name: steam\n    url: https://store.steampowered.com\n"
        "  - name: Netflix\n    url: https://new.example\n"
    )
    assert importer.import_paths(conn, [write(tmp_path, second)]).platforms_added == 0
    platforms = {row["name"]: row["url"] for row in library.list_platforms(conn)}
    assert platforms == {"Netflix": "https://old.example", "Steam": "https://store.steampowered.com"}


def test_platforms_list_and_titles_share_platforms(conn, tmp_path):
    text = (
        "streaming_service:\n  - name: Steam\n    url: https://store.steampowered.com\n"
        "game:\n  - name: Hades\n    platforms: [Steam]\n"
    )
    importer.import_paths(conn, [write(tmp_path, text)])
    [platform] = library.list_platforms(conn)
    assert platform["title_count"] == 1 and platform["url"]


@pytest.mark.parametrize(
    "text, message",
    [
        ("streaming_service: Netflix\n", "'streaming_service' must be a list"),
        ("streaming_service:\n  - Netflix\n", r"streaming_service\[0\]: expected a mapping"),
        ("streaming_service:\n  - url: https://example.com\n", r"streaming_service\[0\]: name"),
        (
            "streaming_service:\n  - name: Bad\n    url: javascript:alert(1)\n",
            r"streaming_service\[0\]: url",
        ),
        (
            "streaming_service:\n  - name: Bad\n    colour: red\n",
            r"streaming_service\[0\]: colour",
        ),
    ],
)
def test_bad_platforms_are_reported(conn, tmp_path, text, message):
    with pytest.raises(importer.ImportFileError, match=message):
        importer.import_paths(conn, [write(tmp_path, text)])
    assert library.list_platforms(conn) == []


def test_main_reports_new_platforms(conn, tmp_path, monkeypatch, capsys, db_path):
    monkeypatch.setenv("PLASMA_DB", str(db_path))
    write(tmp_path, "streaming_service:\n  - name: Wii\n")
    assert importer.main([str(tmp_path)]) == 0
    assert "1 new platform(s)" in capsys.readouterr().out


def test_import_reads_game_modes_genres_and_age(conn, tmp_path):
    path = write(
        tmp_path,
        """
game:
  - name: Hades
    singleplayer: false
    multiplayer: true
    multiplayer_status: playing
    genres: [roguelike]
    age_restriction: 13
""",
    )
    importer.import_paths(conn, [path])
    title = library.list_titles(conn)[0]
    assert (title["singleplayer"], title["multiplayer"]) == (0, 1)
    assert title["multiplayer_status"] == "watching"
    assert title["age_restriction"] == "13+"
    assert library.title_genres(conn, title["id"]) == ["Roguelike"]


def test_import_rejects_a_completed_multiplayer(conn, tmp_path):
    path = write(
        tmp_path,
        """
game:
  - name: Hades
    multiplayer: true
    multiplayer_status: completed
""",
    )
    with pytest.raises(importer.ImportFileError, match="multiplayer_status"):
        importer.import_paths(conn, [path])


def test_reimport_fills_new_details_on_an_existing_game(conn, tmp_path):
    importer.import_paths(conn, [write(tmp_path, "game:\n  - name: Hades\n")])
    text = """
game:
  - name: Hades
    multiplayer: true
    multiplayer_status: planned
    genres: [RPG]
    age_restriction: 16+
"""
    assert importer.import_paths(conn, [write(tmp_path, text)]).updated == 1
    title = library.list_titles(conn)[0]
    assert (title["multiplayer"], title["multiplayer_status"]) == (1, "planned")
    assert title["age_restriction"] == "16+"
    assert library.title_genres(conn, title["id"]) == ["RPG"]
    assert importer.import_paths(conn, [write(tmp_path, text)]).unchanged == 1


def test_reimport_does_not_undo_a_choice_made_in_the_app(conn, tmp_path):
    text = "game:\n  - name: Hades\n    multiplayer: true\n"
    importer.import_paths(conn, [write(tmp_path, text)])
    title = library.list_titles(conn)[0]
    data = TitleInput(kind="game", name="Hades", multiplayer=False)
    library.update_title(conn, title["id"], data)
    assert importer.import_paths(conn, [write(tmp_path, text)]).unchanged == 1
    assert library.list_titles(conn)[0]["multiplayer"] == 0


def test_platform_sections_set_the_kind(conn, tmp_path):
    text = """
streaming_service:
  - name: Netflix
game_console:
  - name: Cloud Thing
"""
    importer.import_paths(conn, [write(tmp_path, text, "platform.yaml")])
    kinds = {p["name"]: p["kind"] for p in library.list_platforms(conn)}
    assert kinds == {"Netflix": "streaming", "Cloud Thing": "console"}


def test_files_are_split_by_section(conn, tmp_path):
    paths = [
        write(tmp_path, "movie:\n  - name: Film\n", "movie.yaml"),
        write(tmp_path, "series:\n  - name: Show\n", "series.yaml"),
        write(tmp_path, "game:\n  - name: Play\n", "game.yaml"),
    ]
    assert importer.import_paths(conn, paths).added == 3


@pytest.mark.parametrize("section", ["movies", "games", "platforms"])
def test_old_section_names_are_rejected(conn, tmp_path, section):
    path = write(tmp_path, f"{section}:\n  - name: Old\n")
    with pytest.raises(importer.ImportFileError, match="top level"):
        importer.import_paths(conn, [path])


def test_image_must_be_in_the_folder_of_its_kind(conn, tmp_path):
    (tmp_path / "images" / "game").mkdir(parents=True)
    (tmp_path / "images" / "movie").mkdir()
    (tmp_path / "images" / "game" / "a.jpg").touch()
    path = write(tmp_path, "movie:\n  - name: Film\n    image: a.jpg\n")
    with pytest.raises(importer.ImportFileError, match="does not exist"):
        importer.import_paths(conn, [path])
    (tmp_path / "images" / "movie" / "a.jpg").touch()
    assert importer.import_paths(conn, [path]).added == 1


def test_platform_image_is_checked_and_applied(conn, tmp_path):
    (tmp_path / "images" / "platform").mkdir(parents=True)
    text = "streaming_service:\n  - name: Netflix\n    image: n.png\n"
    path = write(tmp_path, text, "platform.yaml")
    with pytest.raises(importer.ImportFileError, match="does not exist"):
        importer.import_paths(conn, [path])
    (tmp_path / "images" / "platform" / "n.png").touch()
    importer.import_paths(conn, [path])
    assert library.list_platforms(conn)[0]["image"] == "n.png"


def test_reimport_replaces_a_platform_image(conn, tmp_path):
    (tmp_path / "images" / "platform").mkdir(parents=True)
    (tmp_path / "images" / "platform" / "n.png").touch()
    importer.import_paths(conn, [write(tmp_path, "streaming_service:\n  - name: Netflix\n")])
    text = "streaming_service:\n  - name: Netflix\n    image: n.png\n"
    importer.import_paths(conn, [write(tmp_path, text)])
    assert library.list_platforms(conn)[0]["image"] == "n.png"


def test_import_reads_franchise_and_episode_rating(conn, tmp_path):
    text = """
series:
  - name: Show
    franchise: Saga
    seasons: [2]
    episodes:
      S01E02:
        rating: 4
"""
    importer.import_paths(conn, [write(tmp_path, text)])
    title = library.list_titles(conn)[0]
    assert title["franchise"] == "Saga"
    assert library.get_episode(conn, title["id"], 1, 2)["rating"] == 4


def test_reimport_fills_a_blank_franchise_and_episode_rating(conn, tmp_path):
    importer.import_paths(conn, [write(tmp_path, "series:\n  - name: Show\n    seasons: [1]\n")])
    text = """
series:
  - name: Show
    seasons: [1]
    franchise: Saga
    episodes:
      S01E01:
        rating: 2
"""
    assert importer.import_paths(conn, [write(tmp_path, text)]).updated == 1
    title = library.list_titles(conn)[0]
    assert title["franchise"] == "Saga"
    assert library.get_episode(conn, title["id"], 1, 1)["rating"] == 2


def test_old_ten_point_rating_is_reported(conn, tmp_path):
    path = write(tmp_path, "movie:\n  - name: Film\n    rating: 9\n")
    with pytest.raises(importer.ImportFileError, match="rating"):
        importer.import_paths(conn, [path])


def test_console_release_year_is_imported_and_fills_a_blank(conn, tmp_path):
    importer.import_paths(conn, [write(tmp_path, "game_console:\n  - name: Wii\n")])
    assert library.list_platforms(conn)[0]["release_year"] is None
    text = "game_console:\n  - name: Wii\n    release_year: 2006\n"
    importer.import_paths(conn, [write(tmp_path, text)])
    assert library.list_platforms(conn)[0]["release_year"] == 2006


def test_release_year_on_a_streaming_service_is_reported(conn, tmp_path):
    text = "streaming_service:\n  - name: Netflix\n    release_year: 2007\n"
    with pytest.raises(importer.ImportFileError, match="game consoles"):
        importer.import_paths(conn, [write(tmp_path, text)])


def test_reimport_can_make_an_existing_game_multiplayer_only(conn, tmp_path):
    importer.import_paths(conn, [write(tmp_path, "game:\n  - name: Online\n")])
    text = "game:\n  - name: Online\n    singleplayer: false\n    multiplayer: true\n"
    importer.import_paths(conn, [write(tmp_path, text)])
    title = library.list_titles(conn)[0]
    assert (title["singleplayer"], title["multiplayer"]) == (0, 1)


def test_reimport_keeps_singleplayer_that_has_a_status(conn, tmp_path):
    first = "game:\n  - name: Online\n    status: completed\n"
    importer.import_paths(conn, [write(tmp_path, first)])
    text = "game:\n  - name: Online\n    singleplayer: false\n    multiplayer: true\n"
    importer.import_paths(conn, [write(tmp_path, text)])
    title = library.list_titles(conn)[0]
    assert (title["singleplayer"], title["multiplayer"]) == (1, 1)
