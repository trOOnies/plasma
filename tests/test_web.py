import re
import signal
from datetime import date, timedelta
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from conftest import movie, series
from plasma import library, web
from plasma.models import Details, Kind, Ownership, PlatformInput, Status, TitleInput


def add(conn, data) -> int:
    with conn:
        return library.create_title(conn, data)


def test_home_on_empty_library(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Nothing in progress" in response.text
    assert "Nothing planned yet" in response.text


def test_static_stylesheet_is_served(client):
    assert client.get("/static/style.css").status_code == 200


def test_icon_is_served_and_shown_in_the_header_and_help(client):
    response = client.get("/static/plasma.svg")
    assert response.status_code == 200 and response.headers["content-type"] == "image/svg+xml"
    ElementTree.fromstring(response.text)
    icon = 'src="/static/plasma.svg"'
    assert icon in client.get("/").text.split("</header>")[0]
    assert icon in client.get("/help").text.split("</header>")[1]


def test_icon_is_used_for_the_browser_tab(client, monkeypatch):
    monkeypatch.setattr(web, "stop_server", lambda: None)
    favicon = '<link rel="icon" href="/static/plasma.svg" type="image/svg+xml">'
    assert favicon in client.get("/").text.split("</head>")[0]
    assert favicon in client.get("/help").text.split("</head>")[0]
    assert favicon in client.post("/exit").text.split("</head>")[0]


def test_home_offers_next_episode_and_returns_home(client, conn):
    title_id = add(conn, series("Show", seasons=(2, 2)))
    client.post(f"/titles/{title_id}/episodes/1/1")

    page = client.get("/").text
    assert "Watched S01E02" in page

    response = client.post(f"/titles/{title_id}/next", data={"back": "home"})
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "Watched S02E01" in client.get("/").text


def test_add_movie_through_the_form(client):
    form = {"kind": "movie", "name": "Arrival", "year": "2016"}
    response = client.post("/titles", data=form)
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "Arrival" in page and "2016" in page


def test_add_movie_as_completed_stamps_today(client, conn):
    form = {"kind": "movie", "name": "Arrival", "status": "completed"}
    client.post("/titles", data=form)
    assert library.list_titles(conn)[0]["watched_on"] == date.today().isoformat()


def test_add_series_starts_without_seasons(client, conn):
    page = client.get("/titles/new?kind=series").text
    assert 'name="seasons"' not in page
    client.post("/titles", data={"kind": "series", "name": "Show"})
    assert library.list_titles(conn)[0]["episodes_total"] == 0


@pytest.mark.parametrize(
    "form, message",
    [
        ({"kind": "movie", "name": ""}, "name"),
        ({"kind": "movie", "name": "Film", "year": "abc"}, "year"),
    ],
)
def test_invalid_form_is_shown_again_with_values(client, conn, form, message):
    response = client.post("/titles", data=form)
    assert response.status_code == 422
    assert 'class="errors"' in response.text and message in response.text
    assert library.list_titles(conn) == []


def test_invalid_form_keeps_what_was_typed(client):
    form = {"kind": "movie", "name": "Keep me", "year": "abc"}
    assert 'value="Keep me"' in client.post("/titles", data=form).text


def test_duplicate_title_is_rejected(client, conn):
    add(conn, movie("Film", year=2000))
    response = client.post("/titles", data={"kind": "movie", "name": "film", "year": "2000"})
    assert response.status_code == 409
    assert "already in your library" in response.text


def test_edit_page_is_prefilled(client, conn):
    title_id = add(conn, series("Show", seasons=(7, 13), year=2008, rating=4))
    page = client.get(f"/titles/{title_id}/edit").text
    assert 'value="Show"' in page and 'value="2008"' in page


def test_edit_updates_and_keeps_kind(client, conn):
    title_id = add(conn, movie("Old"))
    response = client.post(f"/titles/{title_id}", data={"name": "New", "kind": "series"})
    assert response.status_code == 303
    title = library.get_title(conn, title_id)
    assert (title["name"], title["kind"]) == ("New", "movie")


def test_edit_invalid_stays_on_the_edit_form(client, conn):
    title_id = add(conn, movie("Old"))
    response = client.post(f"/titles/{title_id}", data={"name": ""})
    assert response.status_code == 422
    assert f'action="/titles/{title_id}"' in response.text
    assert library.get_title(conn, title_id)["name"] == "Old"


def test_edit_rename_to_duplicate(client, conn):
    add(conn, movie("One"))
    title_id = add(conn, movie("Two"))
    assert client.post(f"/titles/{title_id}", data={"name": "One"}).status_code == 409
    assert library.get_title(conn, title_id)["name"] == "Two"


def test_new_season_on_a_finished_series(client, conn):
    title_id = add(conn, series("Show", seasons=(2,), status=Status.COMPLETED))
    assert f'href="/titles/{title_id}/seasons/new"' in client.get(f"/titles/{title_id}").text
    assert "season 2" in client.get(f"/titles/{title_id}/seasons/new").text
    response = client.post(f"/titles/{title_id}/seasons", data={"episodes": "3"})
    assert response.headers["location"] == f"/titles/{title_id}#season-2"
    title = library.get_title(conn, title_id)
    assert (title["status"], title["episodes_watched"]) == ("watching", 2)
    assert library.season_counts(conn, title_id) == [2, 3]


@pytest.mark.parametrize("episodes", ["", "0", "1000", "lots"])
def test_new_season_rejects_a_bad_size(client, conn, episodes):
    title_id = add(conn, series(seasons=(2,)))
    response = client.post(f"/titles/{title_id}/seasons", data={"episodes": episodes})
    assert response.status_code == 422
    assert 'class="errors"' in response.text
    assert library.season_counts(conn, title_id) == [2]


def test_new_season_stops_at_the_limit(client, conn):
    title_id = add(conn, series(seasons=(1,) * 99))
    response = client.post(f"/titles/{title_id}/seasons", data={"episodes": "1"})
    assert response.status_code == 422
    assert "up to 99 seasons" in response.text


def test_new_season_is_only_for_series(client, conn):
    title_id = add(conn, movie())
    assert client.get(f"/titles/{title_id}/seasons/new").status_code == 404
    assert client.post(f"/titles/{title_id}/seasons", data={"episodes": "1"}).status_code == 404


def test_each_season_has_an_edit_icon(client, conn):
    title_id = add(conn, series(seasons=(2, 2)))
    page = client.get(f"/titles/{title_id}").text
    assert 'aria-label="Edit season 1"' in page and 'aria-label="Edit season 2"' in page
    assert 'name="episodes" type="number" min="1" max="999" value="2"' in client.get(
        f"/titles/{title_id}/seasons/1/edit"
    ).text


def test_edit_season_changes_its_size_and_details(client, conn):
    title_id = add(conn, series(seasons=(3, 2)))
    form = {"episodes": "2", "duration_1": "45"}
    response = client.post(f"/titles/{title_id}/seasons/1/details", data=form)
    assert response.headers["location"] == f"/titles/{title_id}/seasons/1"
    assert library.season_counts(conn, title_id) == [2, 2]
    assert library.get_episode(conn, title_id, 1, 1)["duration"] == 45


def test_edit_season_rejects_a_bad_size_and_keeps_it(client, conn):
    title_id = add(conn, series(seasons=(3,)))
    data = {"episodes": "0", "duration_1": "45"}
    response = client.post(f"/titles/{title_id}/seasons/1/details", data=data)
    assert response.status_code == 422
    assert 'value="0"' in response.text
    assert library.season_counts(conn, title_id) == [3]
    assert library.get_episode(conn, title_id, 1, 1)["duration"] is None


def test_only_the_last_season_can_be_deleted(client, conn):
    title_id = add(conn, series(seasons=(2, 3)))
    assert "Delete season" not in client.get(f"/titles/{title_id}/seasons/1/edit").text
    assert "Delete season" in client.get(f"/titles/{title_id}/seasons/2/edit").text
    assert client.post(f"/titles/{title_id}/seasons/1/delete").status_code == 404
    response = client.post(f"/titles/{title_id}/seasons/2/delete")
    assert response.headers["location"] == f"/titles/{title_id}"
    assert library.season_counts(conn, title_id) == [2]


def test_saving_the_edit_form_keeps_seasons(client, conn):
    title_id = add(conn, series("Show", seasons=(2, 3)))
    client.post(f"/titles/{title_id}", data={"name": "Renamed"})
    assert library.season_counts(conn, title_id) == [2, 3]


def test_delete(client, conn):
    title_id = add(conn, movie())
    response = client.post(f"/titles/{title_id}/delete")
    assert response.headers["location"] == "/titles?kind=movie"
    assert client.get(f"/titles/{title_id}").status_code == 404


def test_status_buttons_change_the_status_in_one_click(client, conn):
    title_id = add(conn, movie())
    page = client.get(f"/titles/{title_id}").text
    assert f'action="/titles/{title_id}/status"' in page
    response = client.post(f"/titles/{title_id}/status", data={"status": "completed"})
    assert response.headers["location"] == f"/titles/{title_id}"
    assert library.get_title(conn, title_id)["status"] == "completed"
    page = client.get(f"/titles/{title_id}").text
    assert 'class="status-completed" name="status" value="none" aria-pressed="true"' in page


def test_new_title_has_no_status_selected(client, conn):
    title_id = add(conn, movie())
    assert 'aria-pressed="true"' not in client.get(f"/titles/{title_id}").text.split("</h1>")[1]
    assert library.get_title(conn, title_id)["status"] == "none"
    assert "badge-" not in client.get("/titles").text


def test_clicking_the_active_status_clears_it(client, conn):
    title_id = add(conn, movie(status=Status.PLANNED))
    client.post(f"/titles/{title_id}/status", data={"status": "none"})
    assert library.get_title(conn, title_id)["status"] == "none"


def test_up_next_only_lists_planned_titles(client, conn):
    add(conn, movie("Chosen", status=Status.PLANNED))
    add(conn, movie("Undecided"))
    page = client.get("/").text
    assert "Chosen" in page and "Undecided" not in page


def test_no_status_filter(client, conn):
    add(conn, movie("Chosen", status=Status.PLANNED))
    add(conn, movie("Undecided"))
    page = client.get("/titles?status=none").text
    assert "Undecided" in page and "Chosen" not in page
    assert "No status" in page


def test_status_buttons_disable_what_the_episodes_rule_out(client, conn):
    title_id = add(conn, series(seasons=(2,)))
    client.post(f"/titles/{title_id}/episodes/1/1")
    page = client.get(f"/titles/{title_id}").text
    assert 'value="planned" aria-pressed="false" disabled' in page
    assert 'value="completed" aria-pressed="false" disabled' not in page
    assert 'value="dropped" aria-pressed="false" title' not in page
    assert 'value="dropped" aria-pressed="false" disabled' not in page


def test_status_rejects_an_unknown_value(client, conn):
    title_id = add(conn, movie())
    assert client.post(f"/titles/{title_id}/status", data={"status": "nope"}).status_code == 422


def test_favorite_button_toggles(client, conn):
    title_id = add(conn, movie())
    assert 'aria-label="Add to favorites"' in client.get(f"/titles/{title_id}").text
    assert client.post(f"/titles/{title_id}/favorite").status_code == 303
    assert 'aria-label="Remove from favorites"' in client.get(f"/titles/{title_id}").text
    assert library.get_title(conn, title_id)["favorite"] == 1
    client.post(f"/titles/{title_id}/favorite")
    assert library.get_title(conn, title_id)["favorite"] == 0


def test_library_favorites_filter(client, conn):
    title_id = add(conn, movie("Loved"))
    add(conn, movie("Other"))
    client.post(f"/titles/{title_id}/favorite")
    page = client.get("/titles?favorite=true").text
    assert "Loved" in page and "Other" not in page


def test_description_and_notes_are_saved_and_shown_separately(client, conn):
    title_id = add(conn, movie("Film"))
    form = {"name": "Film", "description": "The plot", "notes": "My take"}
    client.post(f"/titles/{title_id}", data=form)
    title = library.get_title(conn, title_id)
    assert (title["description"], title["notes"]) == ("The plot", "My take")
    page = client.get(f"/titles/{title_id}").text
    assert "The plot" in page and "Your notes" in page and "My take" in page
    assert ">The plot</textarea>" in client.get(f"/titles/{title_id}/edit").text


def test_image_is_shown_and_served_from_info(client, conn, tmp_path):
    (tmp_path / "info" / "images" / "movie").mkdir(parents=True)
    (tmp_path / "info" / "images" / "movie" / "my poster.png").write_bytes(b"png")
    title_id = add(conn, movie())
    with conn:
        library.set_image(conn, title_id, "my poster.png")
    assert 'src="/images/movie/my%20poster.png"' in client.get(f"/titles/{title_id}").text
    assert client.get("/images/movie/my%20poster.png").content == b"png"


def test_library_shows_titles_as_images(client, conn):
    title_id = add(conn, movie())
    with conn:
        library.set_image(conn, title_id, "my poster.png")
    page = client.get("/titles?kind=movie").text
    assert '<ul class="tiles">' in page
    assert 'src="/images/movie/my%20poster.png"' in page
    assert f'href="/titles/{title_id}"' in page


def test_library_keeps_titles_without_an_image(client, conn):
    add(conn, movie())
    page = client.get("/titles?kind=movie").text
    assert 'class="tile-blank"' in page
    assert "/images/" not in page


def test_library_only_shows_the_kind_when_titles_are_mixed(client, conn):
    add(conn, movie())
    tab = client.get("/titles?kind=movie").text
    assert 'class="muted tile-facts"><span class="kind-icon"' not in tab
    assert 'class="muted tile-facts"><span class="kind-icon"' in client.get("/titles").text


def test_titles_without_an_image_have_no_poster(client, conn):
    title_id = add(conn, movie())
    assert "/images/" not in client.get(f"/titles/{title_id}").text


def test_images_outside_the_folder_are_not_served(client, tmp_path):
    (tmp_path / "info" / "images" / "movie").mkdir(parents=True)
    (tmp_path / "info" / "images" / "other").mkdir()
    (tmp_path / "info" / "images" / "other" / "a.png").write_text("secret")
    (tmp_path / "info" / "game.yaml").write_text("secret")
    assert client.get("/images/movie/..%2F..%2Fgame.yaml").status_code == 404
    assert client.get("/images/other/a.png").status_code == 404


def test_missing_image_gives_a_404(client):
    assert client.get("/images/movie/gone.jpg").status_code == 404


def test_platform_image_is_shown_and_served(client, conn, tmp_path):
    (tmp_path / "info" / "images" / "platform").mkdir(parents=True)
    (tmp_path / "info" / "images" / "platform" / "n.png").write_bytes(b"png")
    with conn:
        library.fill_platform(conn, PlatformInput(name="Netflix", image="n.png"))
    assert 'src="/images/platform/n.png"' in client.get("/platforms").text
    assert 'src="/images/platform/n.png"' in client.get("/platforms/1").text
    assert client.get("/images/platform/n.png").content == b"png"


def test_saving_a_platform_keeps_its_image(client, conn):
    with conn:
        library.fill_platform(conn, PlatformInput(name="Netflix", image="n.png"))
    client.post("/platforms/1", data={"name": "Netflix", "kind": "streaming"})
    assert library.get_platform(conn, 1)["image"] == "n.png"


def test_clicking_an_earlier_watched_episode_regresses(client, conn):
    title_id = add(conn, series(seasons=(3,)))
    client.post(f"/titles/{title_id}/episodes/1/3")
    client.post(f"/titles/{title_id}/episodes/1/1")
    assert library.get_title(conn, title_id)["episodes_watched"] == 1


def test_movie_duration_through_the_form(client, conn):
    form = {"kind": "movie", "name": "Film", "duration": "169"}
    location = client.post("/titles", data=form).headers["location"]
    assert "2h 49m" in client.get(location).text
    assert 'value="169"' in client.get(f"{location}/edit").text


def test_series_form_has_no_duration_field(client):
    assert 'name="duration"' not in client.get("/titles/new?kind=series").text
    assert 'name="duration"' in client.get("/titles/new?kind=movie").text


@pytest.mark.parametrize(
    "minutes, text", [(45, "45m"), (60, "1h"), (169, "2h 49m")]
)
def test_format_duration(minutes, text):
    assert web.format_duration(minutes) == text


def test_season_and_episode_pages_404_for_unknown_ones_and_movies(client, conn):
    show = add(conn, series(seasons=(1,)))
    film = add(conn, movie())
    assert client.get(f"/titles/{show}/seasons/9").status_code == 404
    assert client.get(f"/titles/{film}/seasons/1").status_code == 404
    assert client.get(f"/titles/{show}/episodes/1/9").status_code == 404
    assert client.get(f"/titles/{film}/episodes/1/1").status_code == 404


def test_season_page_saves_episode_details(client, conn):
    title_id = add(conn, series(seasons=(2, 2)))
    assert f'href="/titles/{title_id}/seasons/2"' in client.get(f"/titles/{title_id}").text
    page = client.get(f"/titles/{title_id}/seasons/2/edit").text
    assert "S02E01" in page and "S02E02" in page

    form = {"duration_1": "45", "description_1": "Plot", "release_date_1": "2024-03-02"}
    response = client.post(f"/titles/{title_id}/seasons/2/details", data=form)
    assert response.headers["location"] == f"/titles/{title_id}/seasons/2"

    first = library.episodes_by_season(conn, title_id)[2][0]
    assert (first["duration"], first["description"]) == (45, "Plot")
    assert first["release_date"] == "2024-03-02"
    assert 'title="S02E01 · 45m · Plot"' in client.get(f"/titles/{title_id}").text
    assert ">Plot</textarea>" in client.get(f"/titles/{title_id}/seasons/2/edit").text


def test_season_page_invalid_details_save_nothing(client, conn):
    title_id = add(conn, series(seasons=(2,)))
    form = {"duration_1": "45", "duration_2": "0"}
    response = client.post(f"/titles/{title_id}/seasons/1/details", data=form)
    assert response.status_code == 422
    assert "S01E02 duration" in response.text and 'value="45"' in response.text
    assert library.episodes_by_season(conn, title_id)[1][0]["duration"] is None


def test_season_page_lists_episodes_and_links_to_them(client, conn):
    title_id = add(conn, series(seasons=(2, 2, 1)))
    with conn:
        library.update_episode(conn, title_id, 2, 1, Details(duration=45, description="Plot"))
    page = client.get(f"/titles/{title_id}/seasons/2").text
    assert f'href="/titles/{title_id}/episodes/2/1"' in page
    assert "45m" in page and "Plot" in page
    assert f'href="/titles/{title_id}/seasons/1"' in page
    assert f'href="/titles/{title_id}/seasons/3"' in page


def test_season_page_buttons_stay_on_the_season_page(client, conn):
    title_id = add(conn, series(seasons=(2, 2)))
    url = f"/titles/{title_id}/seasons/2"
    data = {"back": "season"}
    response = client.post(f"/titles/{title_id}/episodes/2/1", data=data)
    assert response.headers["location"] == url
    response = client.post(url, data={**data, "watched": "true"})
    assert response.headers["location"] == url
    assert library.get_title(conn, title_id)["episodes_watched"] == 4


def test_episode_page(client, conn):
    title_id = add(conn, series(seasons=(2, 2)))
    data = Details(duration=45, release_date="2024-03-02", description="Plot")
    with conn:
        library.update_episode(conn, title_id, 1, 2, data)
    page = client.get(f"/titles/{title_id}/episodes/1/2").text
    assert "S01E02" in page and "45m" in page and "Mar 2, 2024" in page and "Plot" in page
    assert f'href="/titles/{title_id}/seasons/1"' in page
    assert f'href="/titles/{title_id}/episodes/1/1"' in page
    assert f'href="/titles/{title_id}/episodes/2/1"' in page


def test_episode_page_toggle_stays_on_the_episode(client, conn):
    title_id = add(conn, series(seasons=(2,)))
    url = f"/titles/{title_id}/episodes/1/1"
    assert "Mark watched" in client.get(url).text
    assert client.post(url, data={"back": "episode"}).headers["location"] == url
    assert "Unmark" in client.get(url).text


def test_episode_edit_page(client, conn):
    title_id = add(conn, series(seasons=(1,)))
    url = f"/titles/{title_id}/episodes/1/1"
    form = {"duration": "50", "release_date": "2024-03-02", "description": "Plot"}
    assert client.post(f"{url}/details", data=form).headers["location"] == url
    assert 'value="50"' in client.get(f"{url}/edit").text
    assert ">Plot</textarea>" in client.get(f"{url}/edit").text


def test_episode_edit_rejects_bad_values(client, conn):
    title_id = add(conn, series(seasons=(1,)))
    response = client.post(f"/titles/{title_id}/episodes/1/1/details", data={"duration": "0"})
    assert response.status_code == 422
    assert 'class="errors"' in response.text


def test_home_links_to_the_next_episode(client, conn):
    title_id = add(conn, series(seasons=(2,)))
    client.post(f"/titles/{title_id}/episodes/1/1")
    assert f'href="/titles/{title_id}/episodes/1/2"' in client.get("/").text


def test_nav_order_and_no_library_tab(client):
    page = client.get("/").text
    nav = page.split("<nav>")[1].split("</nav>")[0]
    tabs = ["Series", "Movies", "Games", "Plans", "Platforms", "Help"]
    assert re.findall(r">(\w+)</a>", nav) == tabs
    assert page.index("+ Series") < page.index("+ Movie") < page.index("+ Game")


def test_home_shows_upcoming_and_just_released(client, conn):
    today = date.today()
    add(conn, movie("Soon", release_date=today + timedelta(days=10)))
    add(conn, movie("Fresh", release_date=today - timedelta(days=3)))
    add(conn, movie("Old", release_date=today - timedelta(days=200)))
    page = client.get("/").text
    upcoming = page.split("<h2>Upcoming</h2>")[1].split("<h2>")[0]
    released = page.split("<h2>Just released</h2>")[1].split("<h2>")[0]
    assert "Soon" in upcoming and "in 10 days" in upcoming
    assert "Fresh" in released and "3 days ago" in released
    assert "Old" not in page


def test_home_links_an_episode_release_to_its_page(client, conn):
    title_id = add(conn, series(seasons=(1, 1)))
    with conn:
        soon = date.today() + timedelta(days=5)
        library.update_episode(conn, title_id, 2, 1, Details(release_date=soon))
    page = client.get("/").text
    assert f'href="/titles/{title_id}/episodes/2/1"' in page and "S02E01" in page


@pytest.mark.parametrize(
    "days, text",
    [(0, "today"), (1, "tomorrow"), (-1, "yesterday"), (5, "in 5 days"), (-5, "5 days ago")],
)
def test_relative_day(days, text):
    assert web.relative_day((date.today() + timedelta(days=days)).isoformat()) == text


def test_release_date_through_the_title_forms(client, conn):
    form = {"kind": "series", "name": "Show", "release_date": "2024-03-02"}
    location = client.post("/titles", data=form).headers["location"]
    assert "Mar 2, 2024" in client.get(location).text
    assert 'value="2024-03-02"' in client.get(f"{location}/edit").text


def test_platforms_page_adds_and_lists(client):
    assert "None yet" in client.get("/platforms").text
    form = {"name": "Netflix", "url": "https://www.netflix.com"}
    location = client.post("/platforms", data=form).headers["location"]
    assert "Netflix" in client.get("/platforms").text
    assert 'href="https://www.netflix.com"' in client.get(location).text


def test_platform_duplicates_ignore_case(client):
    client.post("/platforms", data={"name": "Netflix"})
    response = client.post("/platforms", data={"name": "NETFLIX"})
    assert response.status_code == 409
    assert "already exists" in response.text


@pytest.mark.parametrize("url", ["javascript:alert(1)", "netflix.com", "ftp://x.com"])
def test_platform_url_must_be_http(client, url):
    response = client.post("/platforms", data={"name": "Bad", "url": url})
    assert response.status_code == 422
    assert f'href="{url}"' not in response.text


def test_platform_edit_and_delete(client, conn):
    first = client.post("/platforms", data={"name": "One"}).headers["location"]
    client.post("/platforms", data={"name": "Two"})
    assert client.post(first, data={"name": "Uno"}).status_code == 303
    assert "Uno" in client.get("/platforms").text
    assert client.post(first, data={"name": "Two"}).status_code == 409
    assert client.post(first, data={"name": ""}).status_code == 422

    title_id = add(conn, movie())
    platform_id = int(first.rsplit("/", 1)[1])
    with conn:
        library.set_title_platforms(conn, title_id, [platform_id])
    assert client.post(f"{first}/delete").headers["location"] == "/platforms"
    assert library.title_platforms(conn, title_id) == []
    assert library.get_title(conn, title_id) is not None


def test_title_form_picks_platforms(client, conn):
    with conn:
        netflix = library.create_platform(conn, PlatformInput(name="Netflix"))
        prime = library.create_platform(conn, PlatformInput(name="Prime Video"))
    assert 'name="platform_ids"' in client.get("/titles/new?kind=movie").text

    form = {"kind": "movie", "name": "Film", "platform_ids": [netflix, prime]}
    location = client.post("/titles", data=form).headers["location"]
    page = client.get(location).text
    assert f'href="/platforms/{netflix}"' in page and "Prime Video" in page

    client.post(location, data={"name": "Film", "platform_ids": [prime]})
    title_id = int(location.rsplit("/", 1)[1])
    assert [p["name"] for p in library.title_platforms(conn, title_id)] == ["Prime Video"]
    assert "Film" in client.get(f"/platforms/{prime}").text
    assert "Film" not in client.get(f"/platforms/{netflix}").text

    client.post(location, data={"name": "Film"})
    assert library.title_platforms(conn, title_id) == []


def test_title_form_keeps_picked_platforms_on_errors(client, conn):
    with conn:
        netflix = library.create_platform(conn, PlatformInput(name="Netflix"))
    form = {"kind": "movie", "name": "", "platform_ids": [netflix]}
    response = client.post("/titles", data=form)
    assert response.status_code == 422
    assert f'value="{netflix}" checked' in response.text


def test_title_form_ignores_unknown_platforms(client, conn):
    response = client.post("/titles", data={"kind": "movie", "name": "F", "platform_ids": [99]})
    assert response.status_code == 303
    assert library.title_platforms(conn, library.list_titles(conn)[0]["id"]) == []


def add_game(client, **fields) -> str:
    form = {"kind": "game", "name": "Hades", "singleplayer": "true", **fields}
    return client.post("/titles", data=form).headers["location"]


def test_games_tab_lists_only_games(client, conn):
    add(conn, movie("A Movie"))
    add_game(client)
    page = client.get("/titles?kind=game").text
    assert "<h1>Games</h1>" in page and "Hades" in page and "A Movie" not in page


def test_game_form_fields(client):
    page = client.get("/titles/new?kind=game").text
    assert "Add game" in page and "Hours played" in page and "Finished on" in page
    assert "Where to play" in page
    assert 'name="duration"' not in page and 'name="seasons"' not in page
    assert 'name="hours_played"' not in client.get("/titles/new?kind=movie").text


def test_game_page_says_playing_and_hours(client):
    location = add_game(client, hours_played="42", status="watching")
    page = client.get(location).text
    assert ">Playing</button>" in page and ">Watching<" not in page
    assert "42h played" in page
    assert 'value="42"' in client.get(f"{location}/edit").text
    assert ">playing</span>" in client.get("/titles?kind=game").text


def test_zero_hours_played_is_shown(client):
    assert "0h played" in client.get(add_game(client, hours_played="0")).text


def test_movie_still_says_watching(client, conn):
    title_id = add(conn, movie(status=Status.WATCHING))
    assert ">Watching</button>" in client.get(f"/titles/{title_id}").text
    assert ">watching</span>" in client.get("/titles").text


def test_game_status_filter_says_playing(client):
    assert ">Playing</option>" in client.get("/titles?kind=game").text
    assert ">Watching</option>" in client.get("/titles?kind=movie").text


def test_finishing_a_game_stamps_the_date(client):
    location = add_game(client, status="completed")
    assert f"Finished {date.today().isoformat()}" in client.get(location).text


def test_home_shows_games_being_played_and_finished(client, conn):
    assert "Playing now" not in client.get("/").text
    add_game(client, status="watching")
    add_game(client, name="Celeste", status="completed")
    page = client.get("/").text
    assert "Playing now" in page and "Hades" in page
    assert "<strong>1</strong> games finished" in page


def test_game_can_be_on_a_platform(client, conn):
    with conn:
        steam = library.create_platform(conn, PlatformInput(name="Steam"))
    location = add_game(client, platform_ids=[steam])
    assert "Steam" in client.get(location).text
    assert "Hades" in client.get(f"/platforms/{steam}").text


def test_invalid_game_form_keeps_the_game_fields(client):
    form = {"kind": "game", "name": "", "hours_played": "7"}
    response = client.post("/titles", data=form)
    assert response.status_code == 422
    assert 'name="hours_played"' in response.text and 'value="7"' in response.text


@pytest.mark.parametrize("kind", ["movie", "series", "game"])
def test_producer_through_the_forms(client, kind):
    assert 'name="producer"' in client.get(f"/titles/new?kind={kind}").text
    form = {"kind": kind, "name": "Title", "producer": "A24", "singleplayer": "true"}
    location = client.post("/titles", data=form).headers["location"]
    assert "Producer: A24" in client.get(location).text
    assert 'value="A24"' in client.get(f"{location}/edit").text


def test_ownership_buttons_on_games(client, conn):
    location = add_game(client)
    title_id = int(location.rsplit("/", 1)[1])
    assert 'aria-label="Collection"' in client.get(location).text
    response = client.post(f"{location}/ownership", data={"ownership": "wanted"})
    assert response.headers["location"] == location
    assert library.get_title(conn, title_id)["ownership"] == "wanted"
    assert ">wanted<" in client.get("/titles?kind=game").text
    page = client.get(location).text
    assert 'name="ownership" value="none" aria-pressed="true"' in page
    client.post(f"{location}/ownership", data={"ownership": "owned"})
    assert library.get_title(conn, title_id)["ownership"] == "owned"
    client.post(f"{location}/ownership", data={"ownership": "none"})
    assert library.get_title(conn, title_id)["ownership"] == "none"
    bad = client.post(f"{location}/ownership", data={"ownership": "stolen"})
    assert bad.status_code == 422


@pytest.mark.parametrize("data", [movie(), series()])
def test_ownership_is_only_for_games(client, conn, data):
    title_id = add(conn, data)
    assert 'aria-label="Collection"' not in client.get(f"/titles/{title_id}").text
    url = f"/titles/{title_id}/ownership"
    assert client.post(url, data={"ownership": "owned"}).status_code == 404


def test_ownership_filter_is_on_the_games_tab(client):
    assert 'name="ownership"' in client.get("/titles?kind=game").text
    assert 'name="ownership"' not in client.get("/titles?kind=movie").text
    client.post(f"{add_game(client)}/ownership", data={"ownership": "wanted"})
    add_game(client, name="Celeste")
    page = client.get("/titles?kind=game&ownership=wanted").text
    assert "Hades" in page and "Celeste" not in page
    page = client.get("/titles?kind=game&ownership=none").text
    assert "Celeste" in page and "Hades" not in page
    assert "Celeste" in client.get("/titles?kind=game&ownership=").text


def test_editing_a_game_through_the_form_keeps_its_ownership_and_rating(client, conn):
    location = add_game(client)
    client.post(f"{location}/ownership", data={"ownership": "owned"})
    client.post(f"{location}/rating", data={"rating": "4"})
    data = {"name": "Hades", "hours_played": "5", "singleplayer": "true"}
    assert client.post(location, data=data).status_code == 303
    title = library.list_titles(conn)[0]
    assert (title["ownership"], title["rating"], title["hours_played"]) == ("owned", 4, 5)


def make_plan(client, name: str = "Family") -> str:
    return client.post("/plans", data={"name": name}).headers["location"]


def test_plans_page_always_shows_favorites_first(client):
    make_plan(client, "Alone")
    page = client.get("/plans").text
    assert page.index('href="/plans/favorites"') < page.index("Alone")


def test_plan_mixes_series_movies_and_games(client, conn):
    plan = make_plan(client)
    show = add(conn, series("A show"))
    film = add(conn, movie("A film"))
    game = int(add_game(client, name="A game").rsplit("/", 1)[1])
    for title_id in (show, film, game):
        assert client.post(f"{plan}/items", data={"title_id": title_id}).status_code == 303
    page = client.get(plan).text
    assert "A show" in page and "A film" in page and "A game" in page
    assert "3 titles" in client.get("/plans").text


def test_plan_add_list_only_offers_what_is_missing(client, conn):
    plan = make_plan(client)
    inside = add(conn, movie("Inside"))
    outside = add(conn, movie("Outside"))
    client.post(f"{plan}/items", data={"title_id": inside})
    page = client.get(plan).text
    assert f'<option value="{outside}">' in page
    assert f'<option value="{inside}">' not in page


def test_plan_remove_button(client, conn):
    plan = make_plan(client)
    film = add(conn, movie("A film"))
    client.post(f"{plan}/items", data={"title_id": film})
    assert client.post(f"{plan}/items/{film}/delete").headers["location"] == plan
    assert "This plan is empty" in client.get(plan).text
    assert library.get_title(conn, film) is not None


def test_plan_buttons_on_the_title_page(client, conn):
    plan = make_plan(client)
    plan_id = plan.rsplit("/", 1)[1]
    film = add(conn, movie("A film"))
    url = f"/titles/{film}/plans/{plan_id}"
    assert f'formaction="{url}" aria-pressed="false"' in client.get(f"/titles/{film}").text
    assert client.post(url).headers["location"] == f"/titles/{film}#plans"
    assert f'formaction="{url}" aria-pressed="true"' in client.get(f"/titles/{film}").text
    assert "A film" in client.get(plan).text
    client.post(url)
    assert "This plan is empty" in client.get(plan).text


def test_title_page_without_plans_points_to_the_tab(client, conn):
    assert "No plans yet" in client.get(f"/titles/{add(conn, movie())}").text


def test_plan_edit_and_delete(client):
    plan = make_plan(client, "One")
    make_plan(client, "Two")
    form = {"name": "Uno", "description": "For the family"}
    assert client.post(plan, data=form).status_code == 303
    assert "For the family" in client.get(plan).text
    assert client.post(plan, data={"name": "two"}).status_code == 409
    assert client.post(plan, data={"name": ""}).status_code == 422
    assert client.post(f"{plan}/delete").headers["location"] == "/plans"
    assert client.get(plan).status_code == 404


def test_duplicate_plan_is_rejected(client):
    make_plan(client)
    response = client.post("/plans", data={"name": "FAMILY"})
    assert response.status_code == 409
    assert "already exists" in response.text


def test_favorites_plan_follows_the_hearts(client, conn):
    film = add(conn, movie("Loved film"))
    show = add(conn, series("Loved show"))
    add(conn, movie("Other"))
    client.post(f"/titles/{film}/favorite")
    client.post(f"/titles/{show}/favorite")

    page = client.get("/plans/favorites").text
    assert "Loved film" in page and "Loved show" in page and "Other" not in page
    assert "2 titles" in client.get("/plans").text

    client.post(f"/titles/{film}/favorite")
    assert "Loved film" not in client.get("/plans/favorites").text


def test_favorites_plan_is_there_on_an_empty_library(client):
    page = client.get("/plans/favorites")
    assert page.status_code == 200
    assert "No favorites yet" in page.text
    assert 'href="/plans/favorites"' in client.get("/plans").text


def test_favorites_plan_cannot_be_changed_or_deleted(client):
    assert 'action="/plans/favorites' not in client.get("/plans/favorites").text
    assert client.post("/plans/favorites/delete").status_code == 422
    assert client.get("/plans/favorites").status_code == 200


@pytest.mark.parametrize("name", ["Favorites", "FAVORITES"])
def test_no_plan_can_take_the_favorites_name(client, name):
    response = client.post("/plans", data={"name": name})
    assert response.status_code == 422
    assert "reserved" in response.text
    response = client.post(make_plan(client), data={"name": name})
    assert response.status_code == 422


def test_exit_says_goodbye_and_stops_the_server(client, monkeypatch):
    stopped = []
    monkeypatch.setattr(web, "stop_server", lambda: stopped.append(True))
    response = client.post("/exit")
    assert response.status_code == 200
    assert "Plasma has stopped" in response.text and "window.close()" in response.text
    assert stopped == [True]


def test_pages_are_never_cached_so_they_cannot_show_old_data(client, conn):
    title_id = add(conn, movie())
    for url in ("/", "/titles?kind=movie", f"/titles/{title_id}", "/missing"):
        assert client.get(url).headers["cache-control"] == "no-store"


def test_images_and_styles_can_still_be_cached(client):
    assert "cache-control" not in client.get("/static/style.css").headers


def test_exit_button_is_on_every_page(client):
    assert 'action="/exit"' in client.get("/").text
    assert 'action="/exit"' in client.get("/help").text


@pytest.mark.parametrize("parent_pid, target", [(4321, 4321), (None, 1234)])
def test_stop_server_signals_the_reloader_when_there_is_one(monkeypatch, parent_pid, target):
    calls = []
    parent = SimpleNamespace(pid=parent_pid) if parent_pid else None
    monkeypatch.setattr(web.multiprocessing, "parent_process", lambda: parent)
    monkeypatch.setattr(web.os, "getpid", lambda: 1234)
    monkeypatch.setattr(web.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    web.stop_server()
    assert calls == [(target, signal.SIGINT)]


def test_help_page_covers_the_repo_and_license(client):
    page = client.get("/help")
    assert page.status_code == 200
    assert "https://github.com/trOOnies/plasma" in page.text
    assert "🍿" in page.text and "help-hero" in page.text
    assert "GNU General Public License" in page.text
    assert 'href="/help"' in client.get("/").text


def test_episode_toggle_returns_to_its_season(client, conn):
    title_id = add(conn, series(seasons=(2, 2)))
    response = client.post(f"/titles/{title_id}/episodes/2/1")
    assert response.headers["location"] == f"/titles/{title_id}#season-2"
    assert library.get_title(conn, title_id)["episodes_watched"] == 3


def test_season_buttons(client, conn):
    title_id = add(conn, series(seasons=(2, 2)))
    assert "Mark season watched" in client.get(f"/titles/{title_id}").text
    client.post(f"/titles/{title_id}/seasons/1", data={"watched": "true"})
    assert "Clear season" in client.get(f"/titles/{title_id}").text
    client.post(f"/titles/{title_id}/seasons/1", data={"watched": "false"})
    assert library.get_title(conn, title_id)["episodes_watched"] == 0


def test_finished_series_has_no_next_button(client, conn):
    title_id = add(conn, series(seasons=(1,), status=Status.COMPLETED))
    assert "/next" not in client.get(f"/titles/{title_id}").text


def test_series_without_episodes_prompts_for_seasons(client, conn):
    title_id = add(conn, series(seasons=()))
    page = client.get(f"/titles/{title_id}").text
    assert "No seasons yet" in page and f"/titles/{title_id}/seasons/new" in page


def test_library_filters(client, conn):
    add(conn, movie("Alien", status=Status.COMPLETED))
    add(conn, series("Fargo"))
    assert "Fargo" not in client.get("/titles?kind=movie").text
    assert "Alien" not in client.get("/titles?status=planned").text
    assert "Alien" not in client.get("/titles?q=far").text
    assert "No titles match" in client.get("/titles?q=zzz").text


def test_library_filter_with_blank_status_shows_everything(client, conn):
    add(conn, movie("Alien"))
    response = client.get("/titles?q=&status=")
    assert response.status_code == 200
    assert "Alien" in response.text


def test_names_are_escaped(client, conn):
    title_id = add(conn, movie("<script>alert(1)</script>"))
    assert "<script>alert" not in client.get(f"/titles/{title_id}").text
    assert "<script>alert" not in client.get("/titles").text


@pytest.mark.parametrize(
    "method, url",
    [
        ("get", "/titles/999"),
        ("get", "/titles/999/edit"),
        ("post", "/titles/999/delete"),
        ("post", "/titles/999/favorite"),
        ("get", "/titles/999/seasons/1"),
        ("get", "/titles/999/seasons/1/edit"),
        ("get", "/titles/999/episodes/1/1"),
        ("get", "/titles/999/episodes/1/1/edit"),
        ("post", "/titles/999/episodes/1/1/details"),
        ("get", "/platforms/999"),
        ("get", "/plans/999"),
        ("post", "/plans/999"),
        ("post", "/plans/999/delete"),
        ("post", "/plans/999/items/1/delete"),
        ("post", "/titles/999/plans/1"),
        ("post", "/platforms/999"),
        ("post", "/platforms/999/delete"),
        ("post", "/titles/999/seasons/1/details"),
        ("post", "/titles/999/next"),
        ("post", "/titles/999/episodes/1/1"),
        ("get", "/nowhere"),
    ],
)
def test_missing_pages_give_a_404_page(client, method, url):
    response = getattr(client, method)(url)
    assert response.status_code == 404
    assert "Not found" in response.text


@pytest.fixture
def uvicorn_calls(monkeypatch, tmp_path) -> list:
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(web.uvicorn, "run", lambda *args, **kwargs: calls.append(args))
    return calls


def test_serve_creates_missing_inputs_and_stops_with_the_reason(uvicorn_calls, tmp_path, capsys):
    assert web.main([]) == 1
    assert uvicorn_calls == []
    assert (tmp_path / "info" / "platform.yaml").read_text() == ""
    err = capsys.readouterr().err
    assert "created empty" in err and "platform.yaml" in err and "make import" in err


def test_serve_starts_on_the_run_after_inputs_were_created(uvicorn_calls):
    web.main([])
    assert web.main([]) == 0
    assert len(uvicorn_calls) == 1


def test_serve_stops_when_only_the_file_is_missing(uvicorn_calls, tmp_path, capsys):
    (tmp_path / "info").mkdir()
    assert web.main([]) == 1
    err = capsys.readouterr().err
    assert uvicorn_calls == [] and "info/platform.yaml" in err


def test_serve_reports_inputs_it_cannot_create(uvicorn_calls, tmp_path, capsys):
    (tmp_path / "info").write_text("")
    assert web.main([]) == 1
    assert uvicorn_calls == [] and "error:" in capsys.readouterr().err


def add_movies(conn, count: int) -> None:
    with conn:
        for number in range(1, count + 1):
            library.create_title(conn, movie(f"Film {number:03}"))


def pagination_navs(page: str) -> list[str]:
    return re.findall(r'<nav class="pagination".*?</nav>', page, re.DOTALL)


def test_short_list_has_no_pagination(client, conn):
    add_movies(conn, web.PAGE_SIZE)
    page = client.get("/titles?kind=movie").text
    assert "Film 025" in page and pagination_navs(page) == []


def test_pagination_is_mirrored_above_and_below_the_list(client, conn):
    add_movies(conn, web.PAGE_SIZE + 1)
    page = client.get("/titles?kind=movie").text
    top, bottom = pagination_navs(page)
    strip_label = re.compile(r'aria-label="[^"]*"')
    assert strip_label.sub("", top) == strip_label.sub("", bottom)
    assert page.index(top) < page.index('<ul class="tiles">') < page.index(bottom)
    assert "Film 025" in page and "Film 026" not in page
    assert '<span class="button small" aria-disabled="true">Previous</span>' in top


@pytest.mark.parametrize(
    "number, first, last",
    [("2", "Film 026", "Film 030"), ("99", "Film 026", "Film 030"), ("-1", "Film 001", "Film 025")],
)
def test_pages_split_the_list_and_stay_in_range(client, conn, number, first, last):
    add_movies(conn, 30)
    page = client.get(f"/titles?kind=movie&page={number}").text
    assert first in page and last in page
    assert "Film 025" not in page or first == "Film 001"


def test_page_links_keep_the_filters(client, conn):
    add_movies(conn, 30)
    page = client.get("/titles?kind=movie&q=Film+0&status=&page=1").text
    assert 'href="/titles?kind=movie&amp;q=Film+0&amp;page=2"' in page


def test_filter_form_starts_again_at_the_first_page(client, conn):
    add_movies(conn, 30)
    assert 'name="page"' not in client.get("/titles?kind=movie&page=2").text


@pytest.mark.parametrize(
    "page, last, numbers",
    [
        (1, 1, [1]),
        (1, 3, [1, 2, 3]),
        (1, 11, [1, 2, 3, None, 11]),
        (6, 11, [1, None, 4, 5, 6, 7, 8, None, 11]),
        (11, 11, [1, None, 9, 10, 11]),
    ],
)
def test_page_numbers_skip_distant_pages(page, last, numbers):
    assert web.page_numbers(page, last) == numbers


def test_footer_is_on_every_page(client):
    for url in ("/", "/help", "/titles", "/missing"):
        page = client.get(url).text
        assert '<footer class="site-footer">' in page
        assert "&copy; 2026 Plasma by trOOnies" in page and "Made with Claude Code" in page


def test_game_form_offers_both_modes_and_excludes_completed_from_multiplayer(client):
    page = client.get("/titles/new?kind=game").text
    assert 'name="singleplayer" value="true" checked' in page
    assert 'name="multiplayer" value="true" >' in page
    multiplayer_options = page.split('name="multiplayer_status"')[1].split("</select>")[0]
    assert "Playing" in multiplayer_options and "Completed" not in multiplayer_options


def test_game_needs_a_mode(client, conn):
    response = client.post("/titles", data={"kind": "game", "name": "Hades"})
    assert response.status_code == 422
    assert "singleplayer, multiplayer or both" in response.text
    assert library.list_titles(conn) == []


def test_game_with_both_modes_has_two_pickers(client, conn):
    form = {"status": "completed", "multiplayer": "true", "multiplayer_status": "watching"}
    location = add_game(client, **form)
    page = client.get(location).text
    assert 'aria-label="Singleplayer"' in page and 'aria-label="Multiplayer"' in page
    multiplayer = page.split('aria-label="Multiplayer"')[1].split("</form>")[0]
    assert "Playing" in multiplayer and "Completed" not in multiplayer
    assert "SP · completed" in client.get("/titles?kind=game").text
    assert "MP · playing" in client.get("/titles?kind=game").text


def test_multiplayer_cannot_be_completed(client, conn):
    location = add_game(client, multiplayer="true")
    data = {"status": "completed"}
    assert client.post(f"{location}/multiplayer-status", data=data).status_code == 422
    data = {"status": "dropped"}
    assert client.post(f"{location}/multiplayer-status", data=data).status_code == 303
    assert library.list_titles(conn)[0]["multiplayer_status"] == "dropped"
    assert library.list_titles(conn)[0]["status"] == "none"


def test_a_mode_the_game_lacks_has_no_status(client, conn):
    location = add_game(client)
    data = {"status": "planned"}
    assert client.post(f"{location}/multiplayer-status", data=data).status_code == 404
    form = {"kind": "game", "name": "Online", "multiplayer": "true"}
    online = client.post("/titles", data=form).headers["location"]
    assert client.post(f"{online}/status", data=data).status_code == 404
    page = client.get(online).text
    assert 'aria-label="Singleplayer"' not in page


def test_unticking_a_mode_clears_its_status(client, conn):
    form = {"multiplayer": "true", "status": "completed", "multiplayer_status": "planned"}
    title_id = int(add_game(client, **form).rsplit("/", 1)[1])
    data = {"name": "Hades", "multiplayer": "true", "status": "completed"}
    assert client.post(f"/titles/{title_id}", data=data).status_code == 303
    title = library.get_title(conn, title_id)
    assert (title["singleplayer"], title["status"], title["watched_on"]) == (0, "none", None)
    assert title["multiplayer_status"] == "none"


def test_edit_form_shows_the_saved_modes(client):
    location = add_game(client, multiplayer="true")
    page = client.get(f"{location}/edit").text
    assert 'name="multiplayer" value="true" checked' in page


def test_movie_has_no_game_modes(client, conn):
    assert 'name="multiplayer"' not in client.get("/titles/new?kind=movie").text
    title_id = add(conn, movie())
    url = f"/titles/{title_id}/multiplayer-status"
    assert client.post(url, data={"status": "planned"}).status_code == 404
    assert "Singleplayer" not in client.get(f"/titles/{title_id}").text


def test_genres_and_age_through_the_form(client, conn):
    form = {
        "kind": "movie",
        "name": "Film",
        "genres": ["Drama", "Sci-Fi"],
        "age_restriction": "16+",
    }
    location = client.post("/titles", data=form).headers["location"]
    page = client.get(location).text
    assert "/titles?kind=movie&amp;genre=Sci-Fi" in page and ">16+</a>" in page
    edit_page = client.get(f"{location}/edit").text
    assert 'value="Drama" checked' in edit_page and 'value="Comedy" >' in edit_page
    assert 'value="16+" selected' in edit_page


def test_form_offers_the_genres_of_its_kind(client):
    assert 'value="RPG"' in client.get("/titles/new?kind=game").text
    assert 'value="RPG"' not in client.get("/titles/new?kind=series").text


def test_unknown_genre_is_rejected(client, conn):
    form = {"kind": "movie", "name": "Film", "genres": "RPG"}
    response = client.post("/titles", data=form)
    assert response.status_code == 422 and "unknown genre" in response.text


def test_library_filters_by_genre_and_age(client, conn):
    add(conn, movie("Grim", genres=["Horror"], age_restriction="18+"))
    add(conn, movie("Light", genres=["Comedy"], age_restriction="all"))
    page = client.get("/titles?kind=movie&genre=Horror&age=").text
    assert "Grim" in page and "Light" not in page
    page = client.get("/titles?kind=movie&age=all").text
    assert "Light" in page and "Grim" not in page and "All ages" in page


def test_platforms_page_has_a_section_for_each_kind(client):
    client.post("/platforms", data={"name": "Netflix", "kind": "streaming"})
    client.post("/platforms", data={"name": "Odd Box", "kind": "console"})
    page = client.get("/platforms").text
    assert page.index("Streaming Platforms") < page.index("Netflix")
    assert page.index("Netflix") < page.index("Game Consoles") < page.index("Odd Box")


def test_platform_with_a_bad_kind_is_rejected(client):
    assert client.post("/platforms", data={"name": "X", "kind": "vhs"}).status_code == 422


def test_platform_kind_can_be_changed(client, conn):
    location = client.post("/platforms", data={"name": "X"}).headers["location"]
    client.post(location, data={"name": "X", "kind": "console"})
    assert "Game Console" in client.get(location).text


def test_title_form_shows_only_its_kind_of_platform(client):
    client.post("/platforms", data={"name": "Netflix", "kind": "streaming"})
    client.post("/platforms", data={"name": "Odd Box", "kind": "console"})
    movie_page = client.get("/titles/new?kind=movie").text
    assert "Netflix" in movie_page and "Odd Box" not in movie_page
    game_page = client.get("/titles/new?kind=game").text
    assert "Odd Box" in game_page and "Netflix" not in game_page


def test_editing_keeps_a_linked_platform_of_the_other_kind(client, conn):
    title_id = add(conn, movie())
    client.post("/platforms", data={"name": "Odd Box", "kind": "console"})
    with conn:
        library.set_title_platforms(conn, title_id, [1])
    assert "Odd Box" in client.get(f"/titles/{title_id}/edit").text


def test_plan_marks_each_title_with_a_kind_icon(client, conn):
    plan_id = client.post("/plans", data={"name": "Mix"}).headers["location"].rsplit("/", 1)[1]
    ids = [
        add(conn, series(seasons=(1,))),
        add(conn, movie()),
        add(conn, TitleInput(kind=Kind.GAME, name="Hades")),
    ]
    for title_id in ids:
        client.post(f"/plans/{plan_id}/items", data={"title_id": title_id})
    page = client.get(f"/plans/{plan_id}").text
    for label in ("Series", "Movie", "Game"):
        assert f'role="img" aria-label="{label}"' in page


def test_favorites_come_first_on_a_tab(client, conn):
    add(conn, movie("Alpha"))
    zulu = add(conn, movie("Zulu"))
    add(conn, movie("Mike"))
    add(conn, series("Aardvark"))
    with conn:
        library.toggle_favorite(conn, zulu)
    page = client.get("/titles?kind=movie").text
    assert page.index("Zulu") < page.index("Alpha") < page.index("Mike")
    assert "Aardvark" not in page


def test_home_has_no_planned_card_but_keeps_up_next(client, conn):
    add(conn, movie("Later", status=Status.PLANNED))
    page = client.get("/").text
    assert "</strong> planned" not in page
    assert "Up next" in page and "Later" in page


def test_anime_is_offered_for_series_and_movies(client):
    assert 'value="Anime"' in client.get("/titles/new?kind=series").text
    assert 'value="Anime"' in client.get("/titles/new?kind=movie").text


@pytest.mark.parametrize("data", [movie(), TitleInput(kind=Kind.GAME, name="Hades"), series()])
def test_rating_a_title_with_stars(client, conn, data):
    title_id = add(conn, data)
    url = f"/titles/{title_id}"
    page = client.get(url).text
    assert page.count('class="star"') == 5 and 'class="star filled"' not in page
    response = client.post(f"{url}/rating", data={"rating": "3"})
    assert response.headers["location"] == url
    assert library.get_title(conn, title_id)["rating"] == 3
    page = client.get(url).text
    assert page.count('class="star filled"') == 3 and page.count('class="star"') == 2
    assert 'name="rating" value="0"' in page
    client.post(f"{url}/rating", data={"rating": "0"})
    assert library.get_title(conn, title_id)["rating"] is None


@pytest.mark.parametrize("rating", ["6", "-1", "lots", ""])
def test_rating_out_of_range_is_refused(client, conn, rating):
    title_id = add(conn, movie())
    assert client.post(f"/titles/{title_id}/rating", data={"rating": rating}).status_code == 422
    assert library.get_title(conn, title_id)["rating"] is None


def test_rating_an_episode_with_stars(client, conn):
    title_id = add(conn, series(seasons=(2,)))
    url = f"/titles/{title_id}/episodes/1/2"
    assert client.get(url).text.count('class="star"') == 5
    response = client.post(f"{url}/rating", data={"rating": "5"})
    assert response.headers["location"] == url
    assert library.get_episode(conn, title_id, 1, 2)["rating"] == 5
    assert client.get(url).text.count('class="star filled"') == 5
    assert "★ 5" in client.get(f"/titles/{title_id}").text


def test_rating_from_the_season_page_stays_there(client, conn):
    title_id = add(conn, series(seasons=(2,)))
    season = f"/titles/{title_id}/seasons/1"
    assert client.get(season).text.count('class="star"') == 10
    url = f"/titles/{title_id}/episodes/1/1/rating"
    response = client.post(url, data={"rating": "2", "back": "season"})
    assert response.headers["location"] == season


def test_rating_a_missing_episode_is_a_404(client, conn):
    title_id = add(conn, series(seasons=(1,)))
    url = f"/titles/{title_id}/episodes/1/9/rating"
    assert client.post(url, data={"rating": "2"}).status_code == 404


def test_title_form_has_no_rating_field(client):
    assert 'name="rating"' not in client.get("/titles/new?kind=movie").text


def test_franchise_links_titles_in_release_order(client, conn):
    first = add(conn, movie("Part One", year=2001, franchise="Saga"))
    second = add(conn, series("Part Two", year=2005, franchise="Saga"))
    third = add(conn, TitleInput(kind=Kind.GAME, name="Part Three", year=2010, franchise="Saga"))
    page = client.get(f"/titles/{second}").text
    assert f'href="/titles/{first}"' in page and f'href="/titles/{third}"' in page
    assert "/titles?franchise=Saga" in page
    listing = client.get("/titles?franchise=Saga").text
    assert listing.index("Part One") < listing.index("Part Two") < listing.index("Part Three")
    assert 'name="franchise" value="Saga"' in listing


def test_favorites_do_not_reorder_a_franchise(client, conn):
    add(conn, movie("Older", year=2001, franchise="Saga"))
    newer = add(conn, movie("Newer", year=2005, franchise="Saga"))
    with conn:
        library.toggle_favorite(conn, newer)
    listing = client.get("/titles?franchise=Saga").text
    assert listing.index("Older") < listing.index("Newer")


def test_franchise_through_the_form(client, conn):
    add(conn, movie("One", franchise="Saga"))
    page = client.get("/titles/new?kind=movie").text
    assert '<option value="Saga">' in page
    form = {"kind": "movie", "name": "Two", "franchise": "Saga"}
    location = client.post("/titles", data=form).headers["location"]
    assert 'name="franchise" value="Saga"' in client.get(f"{location}/edit").text
    client.post(location, data={"name": "Two", "franchise": ""})
    assert "Franchise:" not in client.get(location).text


def test_lone_franchise_title_has_no_pager(client, conn):
    title_id = add(conn, movie("Only", franchise="Saga"))
    page = client.get(f"/titles/{title_id}").text
    assert "Franchise:" in page and "franchise-pager" not in page


def test_games_tab_sorts_favorites_then_wanted_then_owned(client, conn):
    ids = {}
    for name in ("Alpha", "Beta", "Delta", "Gamma", "Zeta"):
        ids[name] = add(conn, TitleInput(kind=Kind.GAME, name=name))
    with conn:
        library.set_ownership(conn, ids["Beta"], Ownership.OWNED)
        library.set_ownership(conn, ids["Delta"], Ownership.WANTED)
        library.toggle_favorite(conn, ids["Zeta"])
    page = client.get("/titles?kind=game").text
    order = sorted(ids, key=page.index)
    assert order == ["Zeta", "Delta", "Beta", "Alpha", "Gamma"]


@pytest.mark.parametrize(
    "url, data",
    [
        ("/titles/999/ownership", {"ownership": "owned"}),
        ("/titles/999/rating", {"rating": "3"}),
        ("/titles/999/episodes/1/1/rating", {"rating": "3"}),
    ],
)
def test_new_buttons_on_a_missing_title_give_a_404(client, url, data):
    assert client.post(url, data=data).status_code == 404


def test_planned_plan_collects_every_planned_title(client, conn):
    add(conn, movie("Soon", status=Status.PLANNED))
    add(conn, series("Later", status=Status.PLANNED))
    add(conn, TitleInput(kind=Kind.GAME, name="Hades", status=Status.PLANNED))
    add(
        conn,
        TitleInput(
            kind=Kind.GAME, name="Online", multiplayer=True, multiplayer_status="planned"
        ),
    )
    done = add(conn, movie("Done", status=Status.COMPLETED))
    page = client.get("/plans/planned").text
    for name in ("Soon", "Later", "Hades", "Online"):
        assert name in page
    assert "Done" not in page
    assert "/plans/planned" in client.get("/plans").text and "4 titles" in client.get("/plans").text
    client.post(f"/titles/{done}/status", data={"status": "planned"})
    assert "Done" in client.get("/plans/planned").text


def test_planned_plan_when_empty(client):
    assert "Nothing planned yet" in client.get("/plans/planned").text


def test_planned_name_is_reserved_in_the_form(client):
    response = client.post("/plans", data={"name": "planned"})
    assert response.status_code == 422 and "reserved" in response.text


def test_console_release_year_through_the_forms(client, conn):
    form = {"name": "Wii", "kind": "console", "release_year": "2006"}
    location = client.post("/platforms", data=form).headers["location"]
    assert "Released in 2006" in client.get(location).text
    assert "2006 · 0 titles" in client.get("/platforms").text
    assert 'value="2006"' in client.get(location).text
    bad = client.post(location, data={**form, "release_year": "12"})
    assert bad.status_code == 422 and 'value="12"' in bad.text
    client.post(location, data={**form, "release_year": ""})
    assert library.list_platforms(conn)[0]["release_year"] is None


def test_platform_forms_show_a_blank_release_year(client):
    location = client.post("/platforms", data={"name": "Wii", "kind": "console"}).headers[
        "location"
    ]
    assert 'value="None"' not in client.get(location).text


def test_streaming_platform_drops_a_release_year(client, conn):
    form = {"name": "Odd", "kind": "console", "release_year": "2006"}
    location = client.post("/platforms", data=form).headers["location"]
    client.post(location, data={**form, "kind": "streaming"})
    platform = library.list_platforms(conn)[0]
    assert (platform["kind"], platform["release_year"]) == ("streaming", None)
