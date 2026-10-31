from datetime import date

import pytest

from conftest import movie, series
from plasma import library
from plasma.models import Kind, Status


def status_of(conn, title_id):
    return library.get_title(conn, title_id)["status"]


def test_create_series_builds_episodes(conn):
    title_id = library.create_title(conn, series(seasons=(3, 2)))
    title = library.get_title(conn, title_id)
    assert (title["episodes_total"], title["episodes_watched"]) == (5, 0)
    assert library.season_counts(conn, title_id) == [3, 2]


def test_duplicates_ignore_case_but_not_year(conn):
    library.create_title(conn, movie("Dune", year=1984))
    library.create_title(conn, movie("Dune", year=2021))
    with pytest.raises(library.DuplicateTitleError):
        library.create_title(conn, movie("DUNE", year=2021))


def test_duplicates_without_a_year(conn):
    library.create_title(conn, movie("Film"))
    with pytest.raises(library.DuplicateTitleError):
        library.create_title(conn, movie("film"))


def test_same_name_allowed_across_kinds(conn):
    library.create_title(conn, movie("Fargo", year=1996))
    library.create_title(conn, series("Fargo", year=1996))


def test_rename_onto_existing_title_is_a_duplicate(conn):
    library.create_title(conn, movie("One"))
    second = library.create_title(conn, movie("Two"))
    with pytest.raises(library.DuplicateTitleError):
        library.update_title(conn, second, movie("One"))


def test_status_follows_episodes(conn):
    title_id = library.create_title(conn, series(seasons=(2,)))
    assert status_of(conn, title_id) == "planned"
    library.toggle_episode(conn, title_id, 1, 1)
    assert status_of(conn, title_id) == "watching"
    library.toggle_episode(conn, title_id, 1, 2)
    assert status_of(conn, title_id) == "completed"
    library.toggle_episode(conn, title_id, 1, 2)
    assert status_of(conn, title_id) == "watching"
    library.toggle_episode(conn, title_id, 1, 1)
    assert status_of(conn, title_id) == "planned"


def test_dropped_series_stays_dropped(conn):
    title_id = library.create_title(conn, series(status=Status.DROPPED))
    library.toggle_episode(conn, title_id, 1, 1)
    assert status_of(conn, title_id) == "dropped"


def test_undropping_recovers_status_from_episodes(conn):
    title_id = library.create_title(conn, series(status=Status.DROPPED))
    library.toggle_episode(conn, title_id, 1, 1)
    library.update_title(conn, title_id, series(seasons=(3, 2), status=Status.PLANNED))
    assert status_of(conn, title_id) == "watching"


def test_series_without_episodes_keeps_manual_status(conn):
    title_id = library.create_title(conn, series(seasons=(), status=Status.WATCHING))
    assert status_of(conn, title_id) == "watching"


def test_setting_completed_marks_everything_watched(conn):
    title_id = library.create_title(conn, series(seasons=(2,)))
    library.update_title(conn, title_id, series(seasons=(2,), status=Status.COMPLETED))
    title = library.get_title(conn, title_id)
    assert title["episodes_watched"] == 2
    assert title["status"] == "completed"


def test_new_season_reopens_completed_series_without_marking_it_watched(conn):
    title_id = library.create_title(conn, series(seasons=(2,), status=Status.COMPLETED))
    library.update_title(conn, title_id, series(seasons=(2, 4), status=Status.COMPLETED))
    title = library.get_title(conn, title_id)
    assert (title["episodes_watched"], title["episodes_total"]) == (2, 6)
    assert title["status"] == "watching"
    assert library.next_episode(conn, title_id)["season"] == 2


def test_resizing_keeps_watch_state_of_remaining_episodes(conn):
    title_id = library.create_title(conn, series(seasons=(3, 2)))
    library.toggle_episode(conn, title_id, 1, 2)
    library.toggle_episode(conn, title_id, 2, 2)
    library.update_title(conn, title_id, series(seasons=(4, 1)))
    assert library.season_counts(conn, title_id) == [4, 1]
    title = library.get_title(conn, title_id)
    assert title["episodes_watched"] == 1
    assert library.episodes_by_season(conn, title_id)[1][1]["watched"] == 1


def test_mark_season_counts_only_that_season(conn):
    title_id = library.create_title(conn, series(seasons=(2, 2)))
    library.mark_season(conn, title_id, 1, True)
    assert library.get_title(conn, title_id)["episodes_watched"] == 2
    assert status_of(conn, title_id) == "watching"
    library.mark_season(conn, title_id, 1, False)
    assert status_of(conn, title_id) == "planned"


def test_mark_season_keeps_earlier_watch_dates(conn):
    title_id = library.create_title(conn, series(seasons=(2,)))
    conn.execute(
        "UPDATE episodes SET watched = 1, watched_on = '2020-01-01'"
        " WHERE title_id = ? AND number = 1",
        (title_id,),
    )
    library.mark_season(conn, title_id, 1, True)
    first, second = library.episodes_by_season(conn, title_id)[1]
    assert first["watched_on"] == "2020-01-01"
    assert second["watched_on"] == date.today().isoformat()


def test_watch_next_walks_across_seasons_and_stops_at_the_end(conn):
    title_id = library.create_title(conn, series(seasons=(1, 1)))
    library.watch_next(conn, title_id)
    assert library.next_episode(conn, title_id)["season"] == 2
    library.watch_next(conn, title_id)
    assert library.next_episode(conn, title_id) is None
    library.watch_next(conn, title_id)
    assert library.get_title(conn, title_id)["episodes_watched"] == 2


def test_watch_next_fills_gaps_first(conn):
    title_id = library.create_title(conn, series(seasons=(3,)))
    library.toggle_episode(conn, title_id, 1, 2)
    assert library.next_episode(conn, title_id)["number"] == 1


def test_toggle_unknown_episode(conn):
    title_id = library.create_title(conn, series(seasons=(1,)))
    assert library.toggle_episode(conn, title_id, 1, 9) is False


def test_mark_through(conn):
    title_id = library.create_title(conn, series(seasons=(3, 3)))
    library.mark_through(conn, title_id, 2, 1)
    assert library.get_title(conn, title_id)["episodes_watched"] == 4
    assert library.next_episode(conn, title_id)["season"] == 2


def test_movie_watched_and_unwatched(conn):
    title_id = library.create_title(conn, movie())
    library.set_movie_watched(conn, title_id, True)
    title = library.get_title(conn, title_id)
    assert (title["status"], title["watched_on"]) == ("completed", date.today().isoformat())
    library.set_movie_watched(conn, title_id, False)
    title = library.get_title(conn, title_id)
    assert (title["status"], title["watched_on"]) == ("planned", None)


def test_delete_removes_episodes(conn):
    title_id = library.create_title(conn, series())
    library.delete_title(conn, title_id)
    assert library.get_title(conn, title_id) is None
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0


def test_list_filters(conn):
    library.create_title(conn, movie("Alien", status=Status.COMPLETED))
    library.create_title(conn, movie("Aliens"))
    library.create_title(conn, series("Fargo"))

    def names(**filters):
        return [row["name"] for row in library.list_titles(conn, **filters)]

    assert names() == ["Alien", "Aliens", "Fargo"]
    assert names(kind=Kind.SERIES) == ["Fargo"]
    assert names(status=Status.COMPLETED) == ["Alien"]
    assert names(query="alien") == ["Alien", "Aliens"]
    assert names(kind=Kind.MOVIE, status=Status.PLANNED, query="ali") == ["Aliens"]


def test_search_treats_wildcards_literally(conn):
    library.create_title(conn, movie("100% Wolf"))
    library.create_title(conn, movie("Plain"))
    assert [row["name"] for row in library.list_titles(conn, query="%")] == ["100% Wolf"]
    assert library.list_titles(conn, query="_") == []


def test_stats(conn):
    assert library.stats(conn)["movies_watched"] == 0
    library.create_title(conn, movie("A", status=Status.COMPLETED, rating=8))
    library.create_title(conn, movie("B", rating=5))
    library.create_title(conn, series("C", seasons=(2,), status=Status.COMPLETED))
    going = library.create_title(conn, series("D", seasons=(2,)))
    library.toggle_episode(conn, going, 1, 1)
    assert library.stats(conn) == {
        "movies_watched": 1,
        "series_completed": 1,
        "planned": 1,
        "average_rating": 6.5,
        "episodes_watched": 3,
    }
