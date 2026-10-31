from datetime import date

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
    assert "Your watchlist is empty" in response.text


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


def test_movie_toggle(client, conn):
    title_id = add(conn, movie())
    assert "Mark watched" in client.get(f"/titles/{title_id}").text
    client.post(f"/titles/{title_id}/watched")
    assert "Mark unwatched" in client.get(f"/titles/{title_id}").text
    client.post(f"/titles/{title_id}/watched")
    assert library.get_title(conn, title_id)["status"] == "planned"


def test_movie_toggle_rejects_series(client, conn):
    title_id = add(conn, series())
    assert client.post(f"/titles/{title_id}/watched").status_code == 404


def test_episode_toggle_returns_to_its_season(client, conn):
    title_id = add(conn, series(seasons=(2, 2)))
    response = client.post(f"/titles/{title_id}/episodes/2/1")
    assert response.headers["location"] == f"/titles/{title_id}#season-2"
    assert library.get_title(conn, title_id)["episodes_watched"] == 1


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
        ("post", "/titles/999/watched"),
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
