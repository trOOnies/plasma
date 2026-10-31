"""FastAPI app serving the server-rendered web UI."""

import argparse
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator, ValidationError

from plasma import library
from plasma.db import connect
from plasma.models import Kind, Status, TitleInput, describe_errors

PACKAGE_DIR = Path(__file__).parent

templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
templates.env.globals["statuses"] = list(Status)
templates.env.filters["episode_code"] = lambda ep: f"S{ep['season']:02}E{ep['number']:02}"

router = APIRouter()


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def require_title(conn: sqlite3.Connection, title_id: int) -> sqlite3.Row:
    title = library.get_title(conn, title_id)
    if title is None:
        raise HTTPException(status_code=404)
    return title


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def title_form(
    name: str = Form(""),
    year: str = Form(""),
    status: str = Form("planned"),
    rating: str = Form(""),
    notes: str = Form(""),
    watched_on: str = Form(""),
    seasons: str = Form(""),
) -> dict[str, str]:
    return {
        "name": name,
        "year": year,
        "status": status,
        "rating": rating,
        "notes": notes,
        "watched_on": watched_on,
        "seasons": seasons,
    }


TitleForm = Annotated[dict[str, str], Depends(title_form)]
# The library filter's "Any status" option submits an empty value.
StatusFilter = Annotated[Status | None, BeforeValidator(lambda value: value or None)]


def parse_form(form: dict[str, str]) -> TitleInput:
    data = TitleInput(**form)
    if data.kind is Kind.MOVIE and data.status is Status.COMPLETED and not data.watched_on:
        return data.model_copy(update={"watched_on": date.today()})
    return data


def render_form(
    request: Request,
    form: dict[str, str],
    errors: list[str] | None = None,
    title_id: int | None = None,
    status_code: int = 200,
) -> Response:
    context = {"form": form, "errors": errors or [], "title_id": title_id}
    return templates.TemplateResponse(request, "form.html", context, status_code=status_code)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, conn: Conn) -> Response:
    watching = library.list_titles(
        conn, Kind.SERIES, Status.WATCHING, order="t.updated_at DESC, t.id DESC"
    )
    context = {
        "stats": library.stats(conn),
        "continuing": [(title, library.next_episode(conn, title["id"])) for title in watching],
        "planned": library.list_titles(conn, status=Status.PLANNED, order="t.id DESC")[:8],
    }
    return templates.TemplateResponse(request, "home.html", context)


@router.get("/titles", response_class=HTMLResponse)
def titles(
    request: Request,
    conn: Conn,
    kind: Kind | None = None,
    status: StatusFilter = None,
    q: str = "",
) -> Response:
    context = {
        "titles": library.list_titles(conn, kind, status, q.strip()),
        "kind": kind,
        "status": status,
        "q": q,
    }
    return templates.TemplateResponse(request, "library.html", context)


@router.get("/titles/new", response_class=HTMLResponse)
def new_title(request: Request, kind: Kind = Kind.MOVIE) -> Response:
    return render_form(request, {"kind": kind.value, "status": Status.PLANNED.value})


@router.post("/titles")
def create_title(
    request: Request, conn: Conn, form: TitleForm, kind: str = Form(...)
) -> Response:
    form = {**form, "kind": kind}
    try:
        with conn:
            title_id = library.create_title(conn, parse_form(form))
    except ValidationError as error:
        return render_form(request, form, describe_errors(error), status_code=422)
    except library.DuplicateTitleError:
        errors = ["That title is already in your library."]
        return render_form(request, form, errors, status_code=409)
    return redirect(f"/titles/{title_id}")


@router.get("/titles/{title_id}", response_class=HTMLResponse)
def title_detail(request: Request, conn: Conn, title_id: int) -> Response:
    context = {
        "title": require_title(conn, title_id),
        "seasons": library.episodes_by_season(conn, title_id),
        "next_episode": library.next_episode(conn, title_id),
    }
    return templates.TemplateResponse(request, "title.html", context)


@router.get("/titles/{title_id}/edit", response_class=HTMLResponse)
def edit_title(request: Request, conn: Conn, title_id: int) -> Response:
    title = require_title(conn, title_id)
    form = {
        key: "" if title[key] is None else str(title[key])
        for key in ("kind", "name", "year", "status", "rating", "notes", "watched_on")
    }
    form["seasons"] = ", ".join(str(count) for count in library.season_counts(conn, title_id))
    return render_form(request, form, title_id=title_id)


@router.post("/titles/{title_id}")
def update_title(request: Request, conn: Conn, form: TitleForm, title_id: int) -> Response:
    title = require_title(conn, title_id)
    form = {**form, "kind": title["kind"]}
    try:
        with conn:
            library.update_title(conn, title_id, parse_form(form))
    except ValidationError as error:
        return render_form(request, form, describe_errors(error), title_id, status_code=422)
    except library.DuplicateTitleError:
        errors = ["That title is already in your library."]
        return render_form(request, form, errors, title_id, status_code=409)
    return redirect(f"/titles/{title_id}")


@router.post("/titles/{title_id}/delete")
def delete_title(conn: Conn, title_id: int) -> Response:
    require_title(conn, title_id)
    with conn:
        library.delete_title(conn, title_id)
    return redirect("/titles")


@router.post("/titles/{title_id}/watched")
def toggle_movie(conn: Conn, title_id: int) -> Response:
    title = require_title(conn, title_id)
    if title["kind"] != Kind.MOVIE.value:
        raise HTTPException(status_code=404)
    with conn:
        library.set_movie_watched(conn, title_id, title["status"] != Status.COMPLETED.value)
    return redirect(f"/titles/{title_id}")


@router.post("/titles/{title_id}/next")
def watch_next(conn: Conn, title_id: int, back: str = Form("")) -> Response:
    require_title(conn, title_id)
    with conn:
        library.watch_next(conn, title_id)
    return redirect("/" if back == "home" else f"/titles/{title_id}")


@router.post("/titles/{title_id}/episodes/{season}/{number}")
def toggle_episode(conn: Conn, title_id: int, season: int, number: int) -> Response:
    with conn:
        found = library.toggle_episode(conn, title_id, season, number)
    if not found:
        raise HTTPException(status_code=404)
    return redirect(f"/titles/{title_id}#season-{season}")


@router.post("/titles/{title_id}/seasons/{season}")
def mark_season(conn: Conn, title_id: int, season: int, watched: bool = Form(...)) -> Response:
    require_title(conn, title_id)
    with conn:
        library.mark_season(conn, title_id, season, watched)
    return redirect(f"/titles/{title_id}#season-{season}")


def create_app(db_path: Path | str | None = None) -> FastAPI:
    app = FastAPI(title="Plasma")
    app.state.db_path = db_path
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    app.include_router(router)

    @app.exception_handler(404)
    def not_found(request: Request, _: Exception) -> Response:
        return templates.TemplateResponse(request, "not_found.html", status_code=404)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Plasma web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart when the code changes")
    args = parser.parse_args()
    uvicorn.run(
        "plasma.web:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(PACKAGE_DIR)] if args.reload else None,
    )


if __name__ == "__main__":
    main()
