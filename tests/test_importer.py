from pathlib import Path

import pytest

from plasma import importer, library, seed
from plasma.models import Kind


def write(directory: Path, text: str, name: str = "library.yaml") -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


FILE = """
movies:
  - name: 1917
    year: 2019
    status: completed
    watched_on: 2024-03-02
    rating: 8
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
    path = write(tmp_path, "movies:\n  - name: Film\n    status: completed\n")
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
    importer.import_paths(conn, [write(tmp_path, "movies:\n  - name: Film\n")])
    rated = write(tmp_path, "movies:\n  - name: Film\n    rating: 7\n    notes: Nice\n", "b.yaml")
    assert importer.import_paths(conn, [rated]).updated == 1
    film = library.find_title(conn, Kind.MOVIE, "Film", None)
    assert (film["rating"], film["notes"]) == (7, "Nice")


def test_same_title_in_two_files_imports_once(conn, tmp_path):
    first = write(tmp_path, "movies:\n  - name: Film\n", "a.yaml")
    second = write(tmp_path, "movies:\n  - name: film\n", "b.yaml")
    result = importer.import_paths(conn, [first, second])
    assert (result.added, result.unchanged) == (1, 1)


@pytest.mark.parametrize(
    "text, message",
    [
        ("movies: [", "invalid YAML"),
        ("- name: Film\n", "top level"),
        ("films:\n  - name: Film\n", "top level"),
        ("movies: Film\n", "must be a list"),
        ("movies:\n  - Film\n", "expected a mapping"),
        ("movies:\n  - year: 2000\n", "movies[0]: name"),
        ("movies:\n  - name: Film\n    rating: 11\n", "movies[0]: rating"),
        ("series:\n  - name: Show\n    seasons: [2]\n    watched_through: S09E01\n", "series[0]"),
    ],
)
def test_bad_input_is_reported(conn, tmp_path, text, message):
    with pytest.raises(importer.ImportFileError, match=message.replace("[", r"\[")):
        importer.import_paths(conn, [write(tmp_path, text)])


def test_one_bad_file_means_nothing_is_written(conn, tmp_path):
    good = write(tmp_path, "movies:\n  - name: Film\n", "a.yaml")
    bad = write(tmp_path, "movies:\n  - name: Other\n    rating: 99\n", "b.yaml")
    with pytest.raises(importer.ImportFileError):
        importer.import_paths(conn, [good, bad])
    assert library.list_titles(conn) == []


def test_empty_file_is_fine(conn, tmp_path):
    assert importer.import_paths(conn, [write(tmp_path, "# nothing yet\n")]).added == 0


def test_main_reads_yaml_files_from_a_folder(tmp_path, db_path, monkeypatch, capsys):
    monkeypatch.setenv("PLASMA_DB", str(db_path))
    write(tmp_path, "movies:\n  - name: A\n", "a.yaml")
    write(tmp_path, "movies:\n  - name: B\n", "b.yml")
    write(tmp_path, "movies:\n  - name: C\n", "notes.txt")
    assert importer.main([str(tmp_path)]) == 0
    assert "2 added" in capsys.readouterr().out


def test_main_reports_errors(tmp_path, db_path, monkeypatch, capsys):
    monkeypatch.setenv("PLASMA_DB", str(db_path))
    write(tmp_path, "movies:\n  - name: A\n    rating: 99\n")
    assert importer.main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "rating" in err and "Nothing was imported" in err


def test_main_with_empty_or_missing_folder(tmp_path, db_path, monkeypatch, capsys):
    monkeypatch.setenv("PLASMA_DB", str(db_path))
    assert importer.main([str(tmp_path)]) == 0
    assert "nothing to import" in capsys.readouterr().out
    assert importer.main([str(tmp_path / "missing")]) == 1


def test_seed_loads_demo_file_and_is_repeatable(conn, db_path, monkeypatch, capsys):
    monkeypatch.setenv("PLASMA_DB", str(db_path))
    assert seed.main() == 0
    assert seed.main() == 0
    assert "10 already present" in capsys.readouterr().out
    assert len(library.list_titles(conn)) == 10
    wire = library.find_title(conn, Kind.SERIES, "The Wire", 2002)
    assert (wire["status"], wire["episodes_watched"]) == ("watching", 17)
    lost = library.find_title(conn, Kind.SERIES, "Lost", 2004)
    assert lost["status"] == "dropped"
