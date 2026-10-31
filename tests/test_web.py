import signal
from datetime import date
from types import SimpleNamespace

import pytest

from conftest import movie, series
from plasma import library, web
from plasma.models import Status


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
    form = {"kind": "movie", "name": "Arrival", "year": "2016", "rating": "9"}
    response = client.post("/titles", data=form)
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "Arrival" in page and "2016" in page and "9/10" in page


def test_add_movie_as_completed_stamps_today(client, conn):
    form = {"kind": "movie", "name": "Arrival", "status": "completed"}
    client.post("/titles", data=form)
    assert library.list_titles(conn)[0]["watched_on"] == date.today().isoformat()


def test_add_series_with_seasons(client, conn):
    client.post("/titles", data={"kind": "series", "name": "Show", "seasons": "2, 3"})
    title = library.list_titles(conn)[0]
    assert title["episodes_total"] == 5


@pytest.mark.parametrize(
    "form, message",
    [
        ({"kind": "movie", "name": ""}, "name"),
        ({"kind": "movie", "name": "Film", "rating": "11"}, "rating"),
        ({"kind": "series", "name": "Show", "seasons": "lots"}, "seasons"),
        ({"kind": "movie", "name": "Film", "year": "abc"}, "year"),
    ],
)
def test_invalid_form_is_shown_again_with_values(client, conn, form, message):
    response = client.post("/titles", data=form)
    assert response.status_code == 422
    assert 'class="errors"' in response.text and message in response.text
    assert library.list_titles(conn) == []


def test_invalid_form_keeps_what_was_typed(client):
    form = {"kind": "movie", "name": "Keep me", "rating": "11"}
    assert 'value="Keep me"' in client.post("/titles", data=form).text


def test_duplicate_title_is_rejected(client, conn):
    add(conn, movie("Film", year=2000))
    response = client.post("/titles", data={"kind": "movie", "name": "film", "year": "2000"})
    assert response.status_code == 409
    assert "already in your library" in response.text


def test_edit_page_is_prefilled(client, conn):
    title_id = add(conn, series("Show", seasons=(7, 13), year=2008, rating=9))
    page = client.get(f"/titles/{title_id}/edit").text
    assert 'value="Show"' in page and 'value="7, 13"' in page and 'value="2008"' in page


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


def test_adding_a_season_to_a_finished_series_through_the_form(client, conn):
    title_id = add(conn, series("Show", seasons=(2,), status=Status.COMPLETED))
    form = {"name": "Show", "status": "completed", "seasons": "2, 3"}
    client.post(f"/titles/{title_id}", data=form)
    title = library.get_title(conn, title_id)
    assert (title["status"], title["episodes_watched"]) == ("watching", 2)


def test_delete(client, conn):
    title_id = add(conn, movie())
    response = client.post(f"/titles/{title_id}/delete")
    assert response.headers["location"] == "/titles"
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
    (tmp_path / "info" / "images").mkdir(parents=True)
    (tmp_path / "info" / "images" / "my poster.png").write_bytes(b"png")
    title_id = add(conn, movie())
    with conn:
        library.set_image(conn, title_id, "my poster.png")
    assert 'src="/images/my%20poster.png"' in client.get(f"/titles/{title_id}").text
    assert client.get("/images/my%20poster.png").content == b"png"


def test_titles_without_an_image_have_no_poster(client, conn):
    title_id = add(conn, movie())
    assert "/images/" not in client.get(f"/titles/{title_id}").text


def test_images_outside_the_folder_are_not_served(client, tmp_path):
    (tmp_path / "info" / "images").mkdir(parents=True)
    (tmp_path / "info" / "library.yaml").write_text("secret")
    assert client.get("/images/..%2Flibrary.yaml").status_code == 404


def test_missing_image_gives_a_404(client):
    assert client.get("/images/gone.jpg").status_code == 404


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


def test_season_page_404s_for_unknown_seasons_and_movies(client, conn):
    assert client.get(f"/titles/{add(conn, series(seasons=(1,)))}/seasons/9").status_code == 404
    assert client.get(f"/titles/{add(conn, movie())}/seasons/1").status_code == 404


def test_season_page_saves_episode_details(client, conn):
    title_id = add(conn, series(seasons=(2, 2)))
    assert "Episode details" in client.get(f"/titles/{title_id}").text
    page = client.get(f"/titles/{title_id}/seasons/2").text
    assert "S02E01" in page and "S02E02" in page

    form = {"duration_1": "45", "description_1": "Plot", "duration_2": "", "description_2": ""}
    response = client.post(f"/titles/{title_id}/seasons/2/details", data=form)
    assert response.headers["location"] == f"/titles/{title_id}#season-2"

    first = library.episodes_by_season(conn, title_id)[2][0]
    assert (first["duration"], first["description"]) == (45, "Plot")
    assert 'title="S02E01 · 45m · Plot"' in client.get(f"/titles/{title_id}").text
    assert ">Plot</textarea>" in client.get(f"/titles/{title_id}/seasons/2").text


def test_season_page_invalid_details_save_nothing(client, conn):
    title_id = add(conn, series(seasons=(2,)))
    form = {"duration_1": "45", "duration_2": "0"}
    response = client.post(f"/titles/{title_id}/seasons/1/details", data=form)
    assert response.status_code == 422
    assert "S01E02 duration" in response.text and 'value="45"' in response.text
    assert library.episodes_by_season(conn, title_id)[1][0]["duration"] is None


def test_exit_says_goodbye_and_stops_the_server(client, monkeypatch):
    stopped = []
    monkeypatch.setattr(web, "stop_server", lambda: stopped.append(True))
    response = client.post("/exit")
    assert response.status_code == 200
    assert "Plasma has stopped" in response.text and "window.close()" in response.text
    assert stopped == [True]


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
    assert "Add the season sizes" in client.get(f"/titles/{title_id}").text


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
    assert (tmp_path / "info" / "library.yaml").read_text() == ""
    err = capsys.readouterr().err
    assert "created empty" in err and "library.yaml" in err and "make import" in err


def test_serve_starts_on_the_run_after_inputs_were_created(uvicorn_calls):
    web.main([])
    assert web.main([]) == 0
    assert len(uvicorn_calls) == 1


def test_serve_stops_when_only_the_file_is_missing(uvicorn_calls, tmp_path, capsys):
    (tmp_path / "info").mkdir()
    assert web.main([]) == 1
    err = capsys.readouterr().err
    assert uvicorn_calls == [] and "info/library.yaml" in err


def test_serve_reports_inputs_it_cannot_create(uvicorn_calls, tmp_path, capsys):
    (tmp_path / "info").write_text("")
    assert web.main([]) == 1
    assert uvicorn_calls == [] and "error:" in capsys.readouterr().err
