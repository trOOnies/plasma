from datetime import date, timedelta

import pytest

from conftest import movie, series
from plasma import library
from plasma.models import Details, Kind, PlatformInput, Status


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
    assert status_of(conn, title_id) == "none"
    library.toggle_episode(conn, title_id, 1, 1)
    assert status_of(conn, title_id) == "watching"
    library.toggle_episode(conn, title_id, 1, 2)
    assert status_of(conn, title_id) == "completed"
    library.toggle_episode(conn, title_id, 1, 2)
    assert status_of(conn, title_id) == "watching"
    library.toggle_episode(conn, title_id, 1, 1)
    assert status_of(conn, title_id) == "none"


def test_titles_start_with_no_status(conn):
    assert status_of(conn, library.create_title(conn, movie())) == "none"
    assert status_of(conn, library.create_title(conn, series())) == "none"


def test_planned_survives_episode_changes_with_nothing_watched(conn):
    title_id = library.create_title(conn, series(seasons=(2,), status=Status.PLANNED))
    library.update_title(conn, title_id, series(seasons=(2, 2), status=Status.PLANNED))
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
    library.toggle_episode(conn, title_id, 2, 2)
    library.update_title(conn, title_id, series(seasons=(4, 1)))
    assert library.season_counts(conn, title_id) == [4, 1]
    title = library.get_title(conn, title_id)
    assert title["episodes_watched"] == 4
    assert library.episodes_by_season(conn, title_id)[1][3]["watched"] == 0


def test_marking_an_episode_marks_everything_before_it(conn):
    title_id = library.create_title(conn, series(seasons=(2, 3, 1)))
    library.toggle_episode(conn, title_id, 2, 2)
    seasons = library.episodes_by_season(conn, title_id)
    watched = [[episode["watched"] for episode in season] for season in seasons.values()]
    assert watched == [[1, 1], [1, 1, 0], [0]]


def watched_flags(conn, title_id):
    seasons = library.episodes_by_season(conn, title_id).values()
    return [[episode["watched"] for episode in season] for season in seasons]


def test_clicking_an_earlier_watched_episode_regresses_to_it(conn):
    title_id = library.create_title(conn, series(seasons=(3, 2)))
    library.toggle_episode(conn, title_id, 2, 2)
    library.toggle_episode(conn, title_id, 1, 2)
    assert watched_flags(conn, title_id) == [[1, 1, 0], [0, 0]]
    assert status_of(conn, title_id) == "watching"


def test_regressing_clears_the_later_watch_dates(conn):
    title_id = library.create_title(conn, series(seasons=(3,)))
    library.toggle_episode(conn, title_id, 1, 3)
    library.toggle_episode(conn, title_id, 1, 1)
    first, second, _ = library.episodes_by_season(conn, title_id)[1]
    assert first["watched_on"] is not None
    assert second["watched_on"] is None


def test_clicking_the_last_watched_episode_unmarks_only_it(conn):
    title_id = library.create_title(conn, series(seasons=(3,)))
    library.toggle_episode(conn, title_id, 1, 2)
    library.toggle_episode(conn, title_id, 1, 2)
    assert watched_flags(conn, title_id) == [[1, 0, 0]]


def test_regressing_then_clicking_again_steps_back_one_more(conn):
    title_id = library.create_title(conn, series(seasons=(3,)))
    library.toggle_episode(conn, title_id, 1, 3)
    library.toggle_episode(conn, title_id, 1, 2)
    library.toggle_episode(conn, title_id, 1, 2)
    assert watched_flags(conn, title_id) == [[1, 0, 0]]


def test_regressing_reopens_a_completed_series(conn):
    title_id = library.create_title(conn, series(seasons=(2,), status=Status.COMPLETED))
    library.toggle_episode(conn, title_id, 1, 1)
    assert status_of(conn, title_id) == "watching"


def test_marking_a_season_marks_earlier_seasons_but_not_later_ones(conn):
    title_id = library.create_title(conn, series(seasons=(2, 2, 2)))
    library.mark_season(conn, title_id, 2, True)
    assert library.get_title(conn, title_id)["episodes_watched"] == 4
    assert status_of(conn, title_id) == "watching"
    assert library.next_episode(conn, title_id)["season"] == 3


def test_clearing_a_season_only_affects_that_season(conn):
    title_id = library.create_title(conn, series(seasons=(2, 2)))
    library.mark_season(conn, title_id, 2, True)
    library.mark_season(conn, title_id, 2, False)
    assert library.get_title(conn, title_id)["episodes_watched"] == 2
    assert status_of(conn, title_id) == "watching"


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
    library.mark_season(conn, title_id, 1, True)
    library.toggle_episode(conn, title_id, 1, 3)
    assert library.next_episode(conn, title_id)["number"] == 3
    title_id = library.create_title(conn, series("Gap", seasons=(1, 1)))
    library.mark_season(conn, title_id, 2, True)
    library.mark_season(conn, title_id, 1, False)
    assert library.next_episode(conn, title_id)["season"] == 1


def test_toggle_unknown_episode(conn):
    title_id = library.create_title(conn, series(seasons=(1,)))
    assert library.toggle_episode(conn, title_id, 1, 9) is False


def test_mark_through(conn):
    title_id = library.create_title(conn, series(seasons=(3, 3)))
    library.mark_through(conn, title_id, 2, 1)
    assert library.get_title(conn, title_id)["episodes_watched"] == 4
    assert library.next_episode(conn, title_id)["season"] == 2


def test_set_status_on_a_movie_tracks_the_watch_date(conn):
    title_id = library.create_title(conn, movie())
    library.set_status(conn, title_id, Status.COMPLETED)
    title = library.get_title(conn, title_id)
    assert (title["status"], title["watched_on"]) == ("completed", date.today().isoformat())
    library.set_status(conn, title_id, Status.PLANNED)
    title = library.get_title(conn, title_id)
    assert (title["status"], title["watched_on"]) == ("planned", None)
    library.set_status(conn, title_id, Status.NONE)
    assert status_of(conn, title_id) == "none"


def test_set_status_keeps_an_existing_movie_date(conn):
    title_id = library.create_title(conn, movie(watched_on="2020-05-01"))
    library.set_status(conn, title_id, Status.COMPLETED)
    assert library.get_title(conn, title_id)["watched_on"] == "2020-05-01"


def test_set_status_completed_marks_a_whole_series(conn):
    title_id = library.create_title(conn, series(seasons=(2, 2)))
    library.set_status(conn, title_id, Status.COMPLETED)
    title = library.get_title(conn, title_id)
    assert (title["status"], title["episodes_watched"]) == ("completed", 4)


def test_set_status_can_drop_and_resume_a_series(conn):
    title_id = library.create_title(conn, series(seasons=(2, 2)))
    library.toggle_episode(conn, title_id, 1, 1)
    library.set_status(conn, title_id, Status.DROPPED)
    assert status_of(conn, title_id) == "dropped"
    library.set_status(conn, title_id, Status.WATCHING)
    assert status_of(conn, title_id) == "watching"


def test_set_status_cannot_contradict_the_episodes(conn):
    title_id = library.create_title(conn, series(seasons=(2,)))
    library.toggle_episode(conn, title_id, 1, 1)
    library.set_status(conn, title_id, Status.PLANNED)
    assert status_of(conn, title_id) == "watching"
    library.set_status(conn, title_id, Status.NONE)
    assert status_of(conn, title_id) == "watching"


def test_planned_series_can_be_set_and_cleared_before_watching(conn):
    title_id = library.create_title(conn, series(seasons=(2,)))
    library.set_status(conn, title_id, Status.PLANNED)
    assert status_of(conn, title_id) == "planned"
    library.set_status(conn, title_id, Status.NONE)
    assert status_of(conn, title_id) == "none"


def test_set_status_on_a_series_without_episodes_is_free(conn):
    title_id = library.create_title(conn, series(seasons=()))
    library.set_status(conn, title_id, Status.WATCHING)
    assert status_of(conn, title_id) == "watching"


@pytest.mark.parametrize(
    "marks, locked",
    [(0, {"watching"}), (1, {"planned"}), (2, {"planned", "watching"})],
)
def test_locked_statuses_follow_the_episodes(conn, marks, locked):
    title_id = library.create_title(conn, series(seasons=(2,)))
    for number in range(1, marks + 1):
        library.toggle_episode(conn, title_id, 1, number)
    assert set(library.locked_statuses(library.get_title(conn, title_id))) == locked


def test_nothing_is_locked_without_episodes(conn):
    title_id = library.create_title(conn, movie())
    assert library.locked_statuses(library.get_title(conn, title_id)) == []


def test_favorites(conn):
    first = library.create_title(conn, movie("A"))
    library.create_title(conn, movie("B"))
    library.toggle_favorite(conn, first)
    assert [row["name"] for row in library.list_titles(conn, favorite=True)] == ["A"]
    library.toggle_favorite(conn, first)
    assert library.list_titles(conn, favorite=True) == []


def test_movie_duration(conn):
    title_id = library.create_title(conn, movie(duration=120))
    assert library.get_title(conn, title_id)["duration"] == 120
    library.update_title(conn, title_id, movie(duration=95))
    assert library.get_title(conn, title_id)["duration"] == 95


def test_episode_details(conn):
    title_id = library.create_title(conn, series(seasons=(2,)))
    library.update_episode(conn, title_id, 1, 2, Details(duration=45, description="Plot"))
    first, second = library.episodes_by_season(conn, title_id)[1]
    assert (second["duration"], second["description"]) == (45, "Plot")
    assert (first["duration"], first["description"]) == (None, "")


def test_episode_details_survive_marking_and_resizing(conn):
    title_id = library.create_title(conn, series(seasons=(2,)))
    library.update_episode(conn, title_id, 1, 1, Details(duration=45))
    library.toggle_episode(conn, title_id, 1, 2)
    library.update_title(conn, title_id, series(seasons=(3,)))
    assert library.episodes_by_season(conn, title_id)[1][0]["duration"] == 45


def test_fill_episode_blanks_keeps_existing_values(conn):
    title_id = library.create_title(conn, series(seasons=(1,)))
    assert library.fill_episode_blanks(conn, title_id, 1, 1, Details(duration=45))
    assert library.fill_episode_blanks(conn, title_id, 1, 1, Details(duration=50)) is False
    assert library.fill_episode_blanks(conn, title_id, 1, 1, Details(description="Plot"))
    episode = library.episodes_by_season(conn, title_id)[1][0]
    assert (episode["duration"], episode["description"]) == (45, "Plot")


TODAY = date(2026, 6, 15)


def on(days: int) -> date:
    return TODAY + timedelta(days=days)


def names(rows):
    return [row["name"] for row in rows]


def test_release_windows_include_their_edges(conn):
    for name, days in [
        ("Today", 0),
        ("Day 1", 1),
        ("Day 90", 90),
        ("Day 91", 91),
        ("Ago 30", -30),
        ("Ago 31", -31),
    ]:
        library.create_title(conn, movie(name, release_date=on(days)))
    assert names(library.upcoming(conn, TODAY)) == ["Day 1", "Day 90"]
    assert names(library.just_released(conn, TODAY)) == ["Today", "Ago 30"]


def test_titles_without_release_dates_are_left_out(conn):
    library.create_title(conn, movie(year=2026))
    assert library.upcoming(conn, TODAY) == []
    assert library.just_released(conn, TODAY) == []


def test_series_is_upcoming_through_its_next_episode(conn):
    title_id = library.create_title(conn, series(seasons=(1, 2)))
    library.update_episode(conn, title_id, 2, 2, Details(release_date=on(14)))
    library.update_episode(conn, title_id, 2, 1, Details(release_date=on(7)))
    [release] = library.upcoming(conn, TODAY)
    assert (release["season"], release["number"], release["release_date"]) == (
        2,
        1,
        on(7).isoformat(),
    )


def test_series_just_released_shows_its_latest_episode(conn):
    title_id = library.create_title(conn, series(seasons=(2,)))
    library.update_episode(conn, title_id, 1, 1, Details(release_date=on(-14)))
    library.update_episode(conn, title_id, 1, 2, Details(release_date=on(-7)))
    assert [row["number"] for row in library.just_released(conn, TODAY)] == [2]


def test_a_new_season_can_be_upcoming_while_the_last_one_is_just_released(conn):
    title_id = library.create_title(conn, series(seasons=(1, 1)))
    library.update_episode(conn, title_id, 1, 1, Details(release_date=on(-5)))
    library.update_episode(conn, title_id, 2, 1, Details(release_date=on(5)))
    assert library.just_released(conn, TODAY)[0]["season"] == 1
    assert library.upcoming(conn, TODAY)[0]["season"] == 2


def test_series_release_date_on_the_title_works_without_episode_dates(conn):
    library.create_title(conn, series("Show", release_date=on(5)))
    [release] = library.upcoming(conn, TODAY)
    assert (release["name"], release["season"]) == ("Show", None)


def test_same_date_on_title_and_episode_links_the_episode(conn):
    title_id = library.create_title(conn, series(seasons=(1,), release_date=on(5)))
    library.update_episode(conn, title_id, 1, 1, Details(release_date=on(5)))
    assert library.upcoming(conn, TODAY)[0]["season"] == 1


def test_upcoming_is_soonest_first_and_just_released_newest_first(conn):
    library.create_title(conn, movie("Later", release_date=on(20)))
    library.create_title(conn, movie("Sooner", release_date=on(10)))
    library.create_title(conn, movie("Older", release_date=on(-20)))
    library.create_title(conn, movie("Newer", release_date=on(-10)))
    assert names(library.upcoming(conn, TODAY)) == ["Sooner", "Later"]
    assert names(library.just_released(conn, TODAY)) == ["Newer", "Older"]


def test_adjacent_episodes_cross_seasons(conn):
    title_id = library.create_title(conn, series(seasons=(2, 2)))
    earlier, later = library.adjacent_episodes(conn, title_id, 2, 1)
    assert (earlier["season"], earlier["number"]) == (1, 2)
    assert (later["season"], later["number"]) == (2, 2)
    assert library.adjacent_episodes(conn, title_id, 1, 1)[0] is None
    assert library.adjacent_episodes(conn, title_id, 2, 2)[1] is None


def test_platform_counts_and_title_filter(conn):
    netflix = library.create_platform(conn, PlatformInput(name="Netflix"))
    library.create_platform(conn, PlatformInput(name="Empty"))
    first = library.create_title(conn, movie("A"))
    library.create_title(conn, movie("B"))
    library.set_title_platforms(conn, first, [netflix])
    counts = {row["name"]: row["title_count"] for row in library.list_platforms(conn)}
    assert counts == {"Empty": 0, "Netflix": 1}
    assert names(library.list_titles(conn, platform_id=netflix)) == ["A"]


def test_deleting_a_title_unlinks_its_platforms(conn):
    netflix = library.create_platform(conn, PlatformInput(name="Netflix"))
    title_id = library.create_title(conn, movie())
    library.set_title_platforms(conn, title_id, [netflix])
    library.delete_title(conn, title_id)
    assert library.list_platforms(conn)[0]["title_count"] == 0


def test_fill_title_platforms_reuses_platforms_whatever_the_case(conn):
    library.create_platform(conn, PlatformInput(name="Netflix", url="https://netflix.com"))
    title_id = library.create_title(conn, movie())
    assert library.fill_title_platforms(conn, title_id, ["NETFLIX", "Hulu"])
    assert [row["name"] for row in library.title_platforms(conn, title_id)] == ["Hulu", "Netflix"]
    assert len(library.list_platforms(conn)) == 2


def test_fill_title_platforms_leaves_a_title_that_has_some(conn):
    title_id = library.create_title(conn, movie())
    library.fill_title_platforms(conn, title_id, ["Netflix"])
    assert library.fill_title_platforms(conn, title_id, ["Hulu"]) is False
    assert [row["name"] for row in library.title_platforms(conn, title_id)] == ["Netflix"]


def test_description_is_stored_apart_from_notes(conn):
    title_id = library.create_title(conn, movie(description="Plot", notes="Mine"))
    library.update_title(conn, title_id, movie(description="New plot", notes="Mine"))
    title = library.get_title(conn, title_id)
    assert (title["description"], title["notes"]) == ("New plot", "Mine")


def test_delete_removes_episodes(conn):
    title_id = library.create_title(conn, series())
    library.delete_title(conn, title_id)
    assert library.get_title(conn, title_id) is None
    assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0


def test_list_filters(conn):
    library.create_title(conn, movie("Alien", status=Status.COMPLETED))
    library.create_title(conn, movie("Aliens", status=Status.PLANNED))
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
    library.create_title(conn, movie("B", rating=5, status=Status.PLANNED))
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
