"""FastAPI app serving the server-rendered web UI."""

import argparse
import multiprocessing
import os
import signal
import sqlite3
import sys
from collections.abc import Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator, ValidationError
from starlette.background import BackgroundTask

from plasma import library
from plasma.db import connect
from plasma.importer import IMAGES_FOLDER, INFO_DIR, InputsCreatedError, ensure_inputs
from plasma.models import Details, Kind, Status, TitleInput, describe_errors

PACKAGE_DIR = Path(__file__).parent

templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
templates.env.globals["statuses"] = list(Status)
templates.env.globals["picker_statuses"] = [s for s in Status if s is not Status.NONE]

router = APIRouter()


def episode_code(episode: Mapping) -> str:
    return f"S{episode['season']:02}E{episode['number']:02}"


def format_duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest}m"
    return f"{hours}h {rest}m" if rest else f"{hours}h"


def episode_tooltip(episode: Mapping) -> str:
    parts = [episode_code(episode)]
    if episode["duration"]:
        parts.append(format_duration(episode["duration"]))
    if episode["watched_on"]:
        parts.append(f"Watched {episode['watched_on']}")
    if episode["description"]:
        parts.append(episode["description"])
    return " · ".join(parts)


templates.env.filters["episode_code"] = episode_code
templates.env.filters["duration"] = format_duration
templates.env.filters["episode_tooltip"] = episode_tooltip


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
    status: str = Form("none"),
    rating: str = Form(""),
    duration: str = Form(""),
    description: str = Form(""),
    notes: str = Form(""),
    watched_on: str = Form(""),
    seasons: str = Form(""),
) -> dict[str, str]:
    return {
        "name": name,
        "year": year,
        "status": status,
        "rating": rating,
        "duration": duration,
        "description": description,
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
    favorite: bool = False,
) -> Response:
    context = {
        "titles": library.list_titles(conn, kind, status, q.strip(), favorite=favorite),
        "kind": kind,
        "status": status,
        "q": q,
        "favorite": favorite,
    }
    return templates.TemplateResponse(request, "library.html", context)


@router.get("/images/{filename}")
def image(request: Request, filename: str) -> Response:
    path = request.app.state.images_dir / filename
    if path.name != filename or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request) -> Response:
    return templates.TemplateResponse(request, "help.html")


@router.get("/titles/new", response_class=HTMLResponse)
def new_title(request: Request, kind: Kind = Kind.MOVIE) -> Response:
    return render_form(request, {"kind": kind.value, "status": Status.NONE.value})


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
    title = require_title(conn, title_id)
    context = {
        "title": title,
        "locked_statuses": library.locked_statuses(title),
        "seasons": library.episodes_by_season(conn, title_id),
        "next_episode": library.next_episode(conn, title_id),
    }
    return templates.TemplateResponse(request, "title.html", context)


FORM_COLUMNS = (
    "kind",
    "name",
    "year",
    "status",
    "rating",
    "duration",
    "description",
    "notes",
    "watched_on",
)


@router.get("/titles/{title_id}/edit", response_class=HTMLResponse)
def edit_title(request: Request, conn: Conn, title_id: int) -> Response:
    title = require_title(conn, title_id)
    form = {
        key: "" if title[key] is None else str(title[key])
        for key in FORM_COLUMNS
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


@router.post("/titles/{title_id}/status")
def set_status(conn: Conn, title_id: int, status: Status = Form(...)) -> Response:
    require_title(conn, title_id)
    with conn:
        library.set_status(conn, title_id, status)
    return redirect(f"/titles/{title_id}")


@router.post("/titles/{title_id}/favorite")
def toggle_favorite(conn: Conn, title_id: int) -> Response:
    require_title(conn, title_id)
    with conn:
        library.toggle_favorite(conn, title_id)
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


def require_season(conn: sqlite3.Connection, title_id: int, season: int) -> list[sqlite3.Row]:
    episodes = library.episodes_by_season(conn, title_id).get(season)
    if episodes is None:
        raise HTTPException(status_code=404)
    return episodes


def render_season(
    request: Request,
    title: sqlite3.Row,
    season: int,
    rows: list[dict],
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    context = {"title": title, "season": season, "rows": rows, "errors": errors or []}
    return templates.TemplateResponse(request, "season.html", context, status_code=status_code)


@router.get("/titles/{title_id}/seasons/{season}", response_class=HTMLResponse)
def season_details(request: Request, conn: Conn, title_id: int, season: int) -> Response:
    title = require_title(conn, title_id)
    rows = [
        {
            "season": season,
            "number": episode["number"],
            "duration": "" if episode["duration"] is None else str(episode["duration"]),
            "description": episode["description"],
        }
        for episode in require_season(conn, title_id, season)
    ]
    return render_season(request, title, season, rows)


@router.post("/titles/{title_id}/seasons/{season}/details")
async def save_season_details(
    request: Request, conn: Conn, title_id: int, season: int
) -> Response:
    title = require_title(conn, title_id)
    form = await request.form()
    rows, details, errors = [], {}, []
    for episode in require_season(conn, title_id, season):
        number = episode["number"]
        values = {
            "duration": str(form.get(f"duration_{number}", "")),
            "description": str(form.get(f"description_{number}", "")),
        }
        rows.append({"season": season, "number": number, **values})
        try:
            details[number] = Details(**values)
        except ValidationError as error:
            code = episode_code(episode)
            errors.extend(f"{code} {message}" for message in describe_errors(error))
    if errors:
        return render_season(request, title, season, rows, errors, status_code=422)
    with conn:
        for number, data in details.items():
            library.update_episode(conn, title_id, season, number, data)
    return redirect(f"/titles/{title_id}#season-{season}")


@router.post("/titles/{title_id}/seasons/{season}")
def mark_season(conn: Conn, title_id: int, season: int, watched: bool = Form(...)) -> Response:
    require_title(conn, title_id)
    with conn:
        library.mark_season(conn, title_id, season, watched)
    return redirect(f"/titles/{title_id}#season-{season}")


def stop_server() -> None:
    """Ask uvicorn to shut down. Under --reload this process is a worker of the reloader."""
    reloader = multiprocessing.parent_process()
    os.kill(reloader.pid if reloader else os.getpid(), signal.SIGINT)


@router.post("/exit", response_class=HTMLResponse)
def exit_app(request: Request) -> Response:
    # Changes are committed as they are made, so there is nothing left to save here.
    task = BackgroundTask(stop_server)
    return templates.TemplateResponse(request, "goodbye.html", background=task)


def create_app(db_path: Path | str | None = None, info_dir: Path = INFO_DIR) -> FastAPI:
    app = FastAPI(title="Plasma")
    app.state.db_path = db_path
    app.state.images_dir = info_dir / IMAGES_FOLDER
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    app.include_router(router)

    @app.exception_handler(404)
    def not_found(request: Request, _: Exception) -> Response:
        return templates.TemplateResponse(request, "not_found.html", status_code=404)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Plasma web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart when the code changes")
    args = parser.parse_args(argv)

    try:
        created = ensure_inputs()
        if created:
            raise InputsCreatedError(created)
    except (InputsCreatedError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    uvicorn.run(
        "plasma.web:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(PACKAGE_DIR)] if args.reload else None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
