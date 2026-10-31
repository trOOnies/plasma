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
from plasma.models import Details, Kind, PlatformInput, Status, TitleInput, describe_errors

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


def long_date(value: str) -> str:
    day = date.fromisoformat(value)
    return f"{day:%b} {day.day}, {day.year}"


def relative_day(value: str) -> str:
    days = (date.fromisoformat(value) - date.today()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    return f"in {days} days" if days > 0 else f"{-days} days ago"


templates.env.filters["long_date"] = long_date
templates.env.filters["relative_day"] = relative_day
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


def require_season(conn: sqlite3.Connection, title_id: int, season: int) -> list[sqlite3.Row]:
    episodes = library.episodes_by_season(conn, title_id).get(season)
    if episodes is None:
        raise HTTPException(status_code=404)
    return episodes


def require_episode(
    conn: sqlite3.Connection, title_id: int, season: int, number: int
) -> sqlite3.Row:
    episode = library.get_episode(conn, title_id, season, number)
    if episode is None:
        raise HTTPException(status_code=404)
    return episode


def require_platform(conn: sqlite3.Connection, platform_id: int) -> sqlite3.Row:
    platform = library.get_platform(conn, platform_id)
    if platform is None:
        raise HTTPException(status_code=404)
    return platform


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def title_form(
    name: str = Form(""),
    year: str = Form(""),
    status: str = Form("none"),
    rating: str = Form(""),
    duration: str = Form(""),
    release_date: str = Form(""),
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
        "release_date": release_date,
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
    conn: sqlite3.Connection,
    form: dict[str, str],
    platform_ids: list[int],
    errors: list[str] | None = None,
    title_id: int | None = None,
    status_code: int = 200,
) -> Response:
    context = {
        "form": form,
        "errors": errors or [],
        "title_id": title_id,
        "platforms": library.list_platforms(conn),
        "platform_ids": platform_ids,
    }
    return templates.TemplateResponse(request, "form.html", context, status_code=status_code)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, conn: Conn) -> Response:
    watching = library.list_titles(
        conn, Kind.SERIES, Status.WATCHING, order="t.updated_at DESC, t.id DESC"
    )
    today = date.today()
    context = {
        "stats": library.stats(conn),
        "just_released": library.just_released(conn, today),
        "upcoming": library.upcoming(conn, today),
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
def new_title(request: Request, conn: Conn, kind: Kind = Kind.MOVIE) -> Response:
    return render_form(request, conn, {"kind": kind.value, "status": Status.NONE.value}, [])


@router.post("/titles")
def create_title(
    request: Request,
    conn: Conn,
    form: TitleForm,
    kind: str = Form(...),
    platform_ids: list[int] = Form([]),
) -> Response:
    form = {**form, "kind": kind}
    try:
        with conn:
            title_id = library.create_title(conn, parse_form(form))
            library.set_title_platforms(conn, title_id, platform_ids)
    except ValidationError as error:
        errors = describe_errors(error)
        return render_form(request, conn, form, platform_ids, errors, status_code=422)
    except library.DuplicateTitleError:
        errors = ["That title is already in your library."]
        return render_form(request, conn, form, platform_ids, errors, status_code=409)
    return redirect(f"/titles/{title_id}")


@router.get("/titles/{title_id}", response_class=HTMLResponse)
def title_detail(request: Request, conn: Conn, title_id: int) -> Response:
    title = require_title(conn, title_id)
    context = {
        "title": title,
        "locked_statuses": library.locked_statuses(title),
        "seasons": library.episodes_by_season(conn, title_id),
        "next_episode": library.next_episode(conn, title_id),
        "platforms": library.title_platforms(conn, title_id),
    }
    return templates.TemplateResponse(request, "title.html", context)


FORM_COLUMNS = (
    "kind",
    "name",
    "year",
    "status",
    "rating",
    "duration",
    "release_date",
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
    platform_ids = [platform["id"] for platform in library.title_platforms(conn, title_id)]
    return render_form(request, conn, form, platform_ids, title_id=title_id)


@router.post("/titles/{title_id}")
def update_title(
    request: Request,
    conn: Conn,
    form: TitleForm,
    title_id: int,
    platform_ids: list[int] = Form([]),
) -> Response:
    title = require_title(conn, title_id)
    form = {**form, "kind": title["kind"]}
    try:
        with conn:
            library.update_title(conn, title_id, parse_form(form))
            library.set_title_platforms(conn, title_id, platform_ids)
    except ValidationError as error:
        errors = describe_errors(error)
        return render_form(request, conn, form, platform_ids, errors, title_id, 422)
    except library.DuplicateTitleError:
        errors = ["That title is already in your library."]
        return render_form(request, conn, form, platform_ids, errors, title_id, 409)
    return redirect(f"/titles/{title_id}")


@router.post("/titles/{title_id}/delete")
def delete_title(conn: Conn, title_id: int) -> Response:
    title = require_title(conn, title_id)
    with conn:
        library.delete_title(conn, title_id)
    return redirect(f"/titles?kind={title['kind']}")


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
def toggle_episode(
    conn: Conn, title_id: int, season: int, number: int, back: str = Form("")
) -> Response:
    with conn:
        found = library.toggle_episode(conn, title_id, season, number)
    if not found:
        raise HTTPException(status_code=404)
    if back == "episode":
        return redirect(f"/titles/{title_id}/episodes/{season}/{number}")
    if back == "season":
        return redirect(f"/titles/{title_id}/seasons/{season}")
    return redirect(f"/titles/{title_id}#season-{season}")


@router.get("/titles/{title_id}/episodes/{season}/{number}", response_class=HTMLResponse)
def episode_page(
    request: Request, conn: Conn, title_id: int, season: int, number: int
) -> Response:
    title = require_title(conn, title_id)
    earlier, later = library.adjacent_episodes(conn, title_id, season, number)
    context = {
        "title": title,
        "episode": require_episode(conn, title_id, season, number),
        "earlier": earlier,
        "later": later,
    }
    return templates.TemplateResponse(request, "episode.html", context)


def render_episode_form(
    request: Request,
    title: sqlite3.Row,
    episode: sqlite3.Row,
    form: dict[str, str],
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    context = {"title": title, "episode": episode, "form": form, "errors": errors or []}
    return templates.TemplateResponse(
        request, "episode_edit.html", context, status_code=status_code
    )


@router.get("/titles/{title_id}/episodes/{season}/{number}/edit", response_class=HTMLResponse)
def edit_episode(
    request: Request, conn: Conn, title_id: int, season: int, number: int
) -> Response:
    title = require_title(conn, title_id)
    episode = require_episode(conn, title_id, season, number)
    form = {
        key: "" if episode[key] is None else str(episode[key])
        for key in ("duration", "release_date", "description")
    }
    return render_episode_form(request, title, episode, form)


@router.post("/titles/{title_id}/episodes/{season}/{number}/details")
def save_episode(
    request: Request,
    conn: Conn,
    title_id: int,
    season: int,
    number: int,
    duration: str = Form(""),
    release_date: str = Form(""),
    description: str = Form(""),
) -> Response:
    title = require_title(conn, title_id)
    episode = require_episode(conn, title_id, season, number)
    form = {"duration": duration, "release_date": release_date, "description": description}
    try:
        data = Details(**form)
    except ValidationError as error:
        return render_episode_form(
            request, title, episode, form, describe_errors(error), status_code=422
        )
    with conn:
        library.update_episode(conn, title_id, season, number, data)
    return redirect(f"/titles/{title_id}/episodes/{season}/{number}")


def render_season_form(
    request: Request,
    title: sqlite3.Row,
    season: int,
    rows: list[dict],
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    context = {"title": title, "season": season, "rows": rows, "errors": errors or []}
    return templates.TemplateResponse(
        request, "season_edit.html", context, status_code=status_code
    )


@router.get("/titles/{title_id}/seasons/{season}", response_class=HTMLResponse)
def season_page(request: Request, conn: Conn, title_id: int, season: int) -> Response:
    title = require_title(conn, title_id)
    episodes = require_season(conn, title_id, season)
    seasons = sorted(library.episodes_by_season(conn, title_id))
    index = seasons.index(season)
    context = {
        "title": title,
        "season": season,
        "episodes": episodes,
        "watched": sum(episode["watched"] for episode in episodes),
        "earlier": seasons[index - 1] if index > 0 else None,
        "later": seasons[index + 1] if index + 1 < len(seasons) else None,
    }
    return templates.TemplateResponse(request, "season.html", context)


@router.get("/titles/{title_id}/seasons/{season}/edit", response_class=HTMLResponse)
def edit_season(request: Request, conn: Conn, title_id: int, season: int) -> Response:
    title = require_title(conn, title_id)
    rows = [
        {
            "season": season,
            "number": episode["number"],
            "duration": "" if episode["duration"] is None else str(episode["duration"]),
            "release_date": episode["release_date"] or "",
            "description": episode["description"],
        }
        for episode in require_season(conn, title_id, season)
    ]
    return render_season_form(request, title, season, rows)


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
            "release_date": str(form.get(f"release_date_{number}", "")),
            "description": str(form.get(f"description_{number}", "")),
        }
        rows.append({"season": season, "number": number, **values})
        try:
            details[number] = Details(**values)
        except ValidationError as error:
            code = episode_code(episode)
            errors.extend(f"{code} {message}" for message in describe_errors(error))
    if errors:
        return render_season_form(request, title, season, rows, errors, status_code=422)
    with conn:
        for number, data in details.items():
            library.update_episode(conn, title_id, season, number, data)
    return redirect(f"/titles/{title_id}/seasons/{season}")


@router.post("/titles/{title_id}/seasons/{season}")
def mark_season(
    conn: Conn, title_id: int, season: int, watched: bool = Form(...), back: str = Form("")
) -> Response:
    require_title(conn, title_id)
    with conn:
        library.mark_season(conn, title_id, season, watched)
    if back == "season":
        return redirect(f"/titles/{title_id}/seasons/{season}")
    return redirect(f"/titles/{title_id}#season-{season}")


def render_platforms(
    request: Request,
    conn: sqlite3.Connection,
    form: dict[str, str] | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    context = {
        "platforms": library.list_platforms(conn),
        "form": form or {},
        "errors": errors or [],
    }
    return templates.TemplateResponse(
        request, "platforms.html", context, status_code=status_code
    )


def render_platform(
    request: Request,
    conn: sqlite3.Connection,
    platform: sqlite3.Row,
    form: Mapping | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    context = {
        "platform": platform,
        "titles": library.list_titles(conn, platform_id=platform["id"]),
        "form": form or platform,
        "errors": errors or [],
    }
    return templates.TemplateResponse(request, "platform.html", context, status_code=status_code)


@router.get("/platforms", response_class=HTMLResponse)
def platforms(request: Request, conn: Conn) -> Response:
    return render_platforms(request, conn)


@router.post("/platforms")
def create_platform(
    request: Request, conn: Conn, name: str = Form(""), url: str = Form("")
) -> Response:
    form = {"name": name, "url": url}
    try:
        with conn:
            platform_id = library.create_platform(conn, PlatformInput(**form))
    except ValidationError as error:
        return render_platforms(request, conn, form, describe_errors(error), 422)
    except library.DuplicatePlatformError:
        return render_platforms(request, conn, form, ["That platform already exists."], 409)
    return redirect(f"/platforms/{platform_id}")


@router.get("/platforms/{platform_id}", response_class=HTMLResponse)
def platform_page(request: Request, conn: Conn, platform_id: int) -> Response:
    return render_platform(request, conn, require_platform(conn, platform_id))


@router.post("/platforms/{platform_id}")
def update_platform(
    request: Request, conn: Conn, platform_id: int, name: str = Form(""), url: str = Form("")
) -> Response:
    platform = require_platform(conn, platform_id)
    form = {"name": name, "url": url}
    try:
        with conn:
            library.update_platform(conn, platform_id, PlatformInput(**form))
    except ValidationError as error:
        return render_platform(request, conn, platform, form, describe_errors(error), 422)
    except library.DuplicatePlatformError:
        errors = ["That platform already exists."]
        return render_platform(request, conn, platform, form, errors, 409)
    return redirect(f"/platforms/{platform_id}")


@router.post("/platforms/{platform_id}/delete")
def delete_platform(conn: Conn, platform_id: int) -> Response:
    require_platform(conn, platform_id)
    with conn:
        library.delete_platform(conn, platform_id)
    return redirect("/platforms")


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
