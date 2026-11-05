"""FastAPI app serving the server-rendered web UI."""

import argparse
import hashlib
import multiprocessing
import os
import signal
import sqlite3
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date
from math import ceil
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator, ValidationError
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile as StarletteUploadFile

from plasma import duo, influencers, library, watch_data
from plasma.db import connect
from plasma.importer import (
    IMAGE_FOLDERS,
    IMAGES_FOLDER,
    INFO_DIR,
    ImportFileError,
    InputsCreatedError,
    ensure_inputs,
)
from plasma.models import (
    FAVORITES_PLAN_NAME,
    GAME_GENRES,
    GENRES,
    MAX_RATING,
    MAX_SEASONS,
    PLANNED_PLAN_NAME,
    SCREEN_GENRES,
    AgeRestriction,
    Details,
    Kind,
    MultiplayerStatus,
    Ownership,
    PlanInput,
    PlatformInput,
    PlatformKind,
    SeasonInput,
    Status,
    TitleInput,
    describe_errors,
)

PACKAGE_DIR = Path(__file__).parent
PAGE_SIZE = 50

templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
templates.env.globals["statuses"] = list(Status)
templates.env.globals["picker_statuses"] = [s for s in Status if s is not Status.NONE]
templates.env.globals["multiplayer_picker_statuses"] = [
    s for s in MultiplayerStatus if s is not MultiplayerStatus.NONE
]
templates.env.globals["multiplayer_statuses"] = list(MultiplayerStatus)
templates.env.globals["ownerships"] = list(Ownership)
templates.env.globals["ownership_choices"] = [o for o in Ownership if o is not Ownership.NONE]
templates.env.globals["age_restrictions"] = list(AgeRestriction)
templates.env.globals["platform_kinds"] = list(PlatformKind)
templates.env.globals["favorites_plan_name"] = FAVORITES_PLAN_NAME
templates.env.globals["planned_plan_name"] = PLANNED_PLAN_NAME


def static_url(name: str) -> str:
    """Link a static file by its content, so every browser fetches it again once it changes."""
    digest = hashlib.sha256((PACKAGE_DIR / "static" / name).read_bytes()).hexdigest()[:12]
    return f"/static/{name}?v={digest}"


templates.env.globals["static_url"] = static_url

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
    if episode["rating"]:
        parts.append(f"★ {episode['rating']}")
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


def require_series(conn: sqlite3.Connection, title_id: int) -> sqlite3.Row:
    title = require_title(conn, title_id)
    if title["kind"] != Kind.SERIES.value:
        raise HTTPException(status_code=404)
    return title


def require_platform(conn: sqlite3.Connection, platform_id: int) -> sqlite3.Row:
    platform = library.get_platform(conn, platform_id)
    if platform is None:
        raise HTTPException(status_code=404)
    return platform


def require_plan(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
    plan = library.get_plan(conn, plan_id)
    if plan is None:
        raise HTTPException(status_code=404)
    return plan


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def page_numbers(page: int, last: int) -> list[int | None]:
    """The first, last and nearby pages, with None wherever some are skipped."""
    numbers: list[int | None] = []
    for number in sorted({1, *range(page - 2, page + 3), last} & set(range(1, last + 1))):
        if numbers and number - numbers[-1] > 1:
            numbers.append(None)
        numbers.append(number)
    return numbers


def paginate(request: Request, items: Sequence, page: int) -> tuple[Sequence, dict]:
    """One page of items, and links to the others that keep the current filters."""
    last = max(ceil(len(items) / PAGE_SIZE), 1)
    page = min(max(page, 1), last)
    filters = {key: value for key, value in request.query_params.items() if value and key != "page"}

    def url(number: int) -> str:
        return f"{request.url.path}?{urlencode({**filters, 'page': number})}"

    pages = {
        "current": page,
        "last": last,
        "previous": url(page - 1) if page > 1 else None,
        "next": url(page + 1) if page < last else None,
        "links": [(number, number and url(number)) for number in page_numbers(page, last)],
    }
    return items[(page - 1) * PAGE_SIZE : page * PAGE_SIZE], pages


def title_form(
    name: str = Form(""),
    year: str = Form(""),
    status: str = Form("none"),
    singleplayer: str = Form(""),
    multiplayer: str = Form(""),
    multiplayer_status: str = Form("none"),
    age_restriction: str = Form(""),
    genres: list[str] = Form([]),
    duration: str = Form(""),
    release_date: str = Form(""),
    hours_played: str = Form(""),
    producer: str = Form(""),
    franchise: str = Form(""),
    description: str = Form(""),
    notes: str = Form(""),
    watched_on: str = Form(""),
) -> dict[str, Any]:
    return {
        "name": name,
        "year": year,
        "status": status,
        "singleplayer": singleplayer,
        "multiplayer": multiplayer,
        "multiplayer_status": multiplayer_status,
        "age_restriction": age_restriction,
        "genres": genres,
        "duration": duration,
        "release_date": release_date,
        "hours_played": hours_played,
        "producer": producer,
        "franchise": franchise,
        "description": description,
        "notes": notes,
        "watched_on": watched_on,
    }


TitleForm = Annotated[dict[str, Any], Depends(title_form)]
# A library filter's "Any" option submits an empty value.
StatusFilter = Annotated[Status | None, BeforeValidator(lambda value: value or None)]
AgeFilter = Annotated[AgeRestriction | None, BeforeValidator(lambda value: value or None)]
OwnershipFilter = Annotated[Ownership | None, BeforeValidator(lambda value: value or None)]
# Clicking the current star submits 0, which clears the rating.
StarRating = Annotated[int, Form(ge=0, le=MAX_RATING)]


def parse_form(form: dict[str, Any]) -> TitleInput:
    values = dict(form)
    if form["kind"] != Kind.GAME.value:
        for key in ("singleplayer", "multiplayer", "multiplayer_status"):
            del values[key]
    else:
        # Unticking a mode clears what the form still shows for it.
        if not form["singleplayer"]:
            values.update(status=Status.NONE.value, watched_on="")
        if not form["multiplayer"]:
            values["multiplayer_status"] = MultiplayerStatus.NONE.value
    data = TitleInput(**values)
    if data.kind is not Kind.SERIES and data.status is Status.COMPLETED and not data.watched_on:
        return data.model_copy(update={"watched_on": date.today()})
    return data


def render_form(
    request: Request,
    conn: sqlite3.Connection,
    form: dict[str, Any],
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
        "franchises": library.list_franchises(conn, Kind(form["kind"])),
        "genre_options": GAME_GENRES if form["kind"] == "game" else SCREEN_GENRES,
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
        "playing": library.list_titles(
            conn, Kind.GAME, Status.WATCHING, order="t.updated_at DESC, t.id DESC"
        ),
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
    ownership: OwnershipFilter = None,
    genre: str = "",
    age: AgeFilter = None,
    franchise: str = "",
    page: int = 1,
) -> Response:
    if franchise:
        order = library.FRANCHISE_ORDER
    elif kind is Kind.GAME:
        order = library.GAME_TAB_ORDER
    else:
        order = library.TAB_ORDER
    matches = library.list_titles(
        conn,
        kind,
        status,
        q.strip(),
        order=order,
        favorite=favorite,
        ownership=ownership,
        genre=genre,
        age_restriction=age,
        franchise=franchise,
    )
    shown, pages = paginate(request, matches, page)
    context = {
        "titles": shown,
        "pages": pages,
        "kind": kind,
        "status": status,
        "q": q,
        "favorite": favorite,
        "ownership": ownership,
        "genre": genre,
        "age": age,
        "franchise": franchise,
        "genre_options": GENRES[kind] if kind else sorted({*SCREEN_GENRES, *GAME_GENRES}),
    }
    return templates.TemplateResponse(request, "library.html", context)


@router.get("/images/{folder}/{filename}")
def image(request: Request, folder: str, filename: str) -> Response:
    path = request.app.state.images_dir / folder / filename
    if folder not in IMAGE_FOLDERS or path.name != filename or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request) -> Response:
    return templates.TemplateResponse(request, "help.html")


@router.get("/titles/new", response_class=HTMLResponse)
def new_title(request: Request, conn: Conn, kind: Kind = Kind.MOVIE) -> Response:
    form = {"kind": kind.value, "status": Status.NONE.value, "singleplayer": "true"}
    return render_form(request, conn, form, [])


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
    earlier, later = library.franchise_neighbors(conn, title)
    context = {
        "title": title,
        "earlier": earlier,
        "later": later,
        "locked_statuses": library.locked_statuses(title),
        "seasons": library.episodes_by_season(conn, title_id),
        "next_episode": library.next_episode(conn, title_id),
        "platforms": library.title_platforms(conn, title_id),
        "genres": library.title_genres(conn, title_id),
        "plans": library.plans_for_title(conn, title_id),
    }
    return templates.TemplateResponse(request, "title.html", context)


FORM_COLUMNS = (
    "kind",
    "name",
    "year",
    "status",
    "multiplayer_status",
    "age_restriction",
    "duration",
    "release_date",
    "hours_played",
    "producer",
    "franchise",
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
    form["singleplayer"] = "true" if title["singleplayer"] else ""
    form["multiplayer"] = "true" if title["multiplayer"] else ""
    form["genres"] = library.title_genres(conn, title_id)
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
    title = require_title(conn, title_id)
    if not title["singleplayer"]:
        raise HTTPException(status_code=404)
    with conn:
        library.set_status(conn, title_id, status)
    return redirect(f"/titles/{title_id}")


@router.post("/titles/{title_id}/multiplayer-status")
def set_multiplayer_status(
    conn: Conn, title_id: int, status: MultiplayerStatus = Form(...)
) -> Response:
    title = require_title(conn, title_id)
    if not title["multiplayer"]:
        raise HTTPException(status_code=404)
    with conn:
        library.set_multiplayer_status(conn, title_id, status)
    return redirect(f"/titles/{title_id}")


@router.post("/titles/{title_id}/favorite")
def toggle_favorite(conn: Conn, title_id: int) -> Response:
    require_title(conn, title_id)
    with conn:
        library.toggle_favorite(conn, title_id)
    return redirect(f"/titles/{title_id}")


@router.post("/titles/{title_id}/ownership")
def set_ownership(conn: Conn, title_id: int, ownership: Ownership = Form(...)) -> Response:
    title = require_title(conn, title_id)
    if title["kind"] != Kind.GAME.value:
        raise HTTPException(status_code=404)
    with conn:
        library.set_ownership(conn, title_id, ownership)
    return redirect(f"/titles/{title_id}")


@router.post("/titles/{title_id}/rating")
def set_rating(conn: Conn, title_id: int, rating: StarRating) -> Response:
    require_title(conn, title_id)
    with conn:
        library.set_rating(conn, title_id, rating or None)
    return redirect(f"/titles/{title_id}")


@router.post("/titles/{title_id}/plans/{plan_id}")
def toggle_plan(conn: Conn, title_id: int, plan_id: int) -> Response:
    require_title(conn, title_id)
    require_plan(conn, plan_id)
    included = {plan["id"] for plan in library.plans_for_title(conn, title_id) if plan["included"]}
    with conn:
        if plan_id in included:
            library.remove_from_plan(conn, plan_id, title_id)
        else:
            library.add_to_plan(conn, plan_id, title_id)
    return redirect(f"/titles/{title_id}#plans")


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


@router.post("/titles/{title_id}/episodes/{season}/{number}/rating")
def set_episode_rating(
    conn: Conn,
    title_id: int,
    season: int,
    number: int,
    rating: StarRating,
    back: str = Form(""),
) -> Response:
    require_episode(conn, title_id, season, number)
    with conn:
        library.set_episode_rating(conn, title_id, season, number, rating or None)
    if back == "season":
        return redirect(f"/titles/{title_id}/seasons/{season}")
    return redirect(f"/titles/{title_id}/episodes/{season}/{number}")


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
    episodes: str,
    is_last: bool,
    rows: list[dict],
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    context = {
        "title": title,
        "season": season,
        "episodes": episodes,
        "is_last": is_last,
        "rows": rows,
        "errors": errors or [],
    }
    return templates.TemplateResponse(
        request, "season_edit.html", context, status_code=status_code
    )


def render_new_season(
    request: Request,
    title: sqlite3.Row,
    season: int,
    episodes: str = "",
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    context = {"title": title, "season": season, "episodes": episodes, "errors": errors or []}
    return templates.TemplateResponse(request, "season_new.html", context, status_code=status_code)


def next_season(conn: sqlite3.Connection, title_id: int) -> int:
    return len(library.season_counts(conn, title_id)) + 1


@router.get("/titles/{title_id}/seasons/new", response_class=HTMLResponse)
def new_season(request: Request, conn: Conn, title_id: int) -> Response:
    title = require_series(conn, title_id)
    return render_new_season(request, title, next_season(conn, title_id))


@router.post("/titles/{title_id}/seasons")
def create_season(
    request: Request, conn: Conn, title_id: int, episodes: str = Form("")
) -> Response:
    title = require_series(conn, title_id)
    season = next_season(conn, title_id)
    if season > MAX_SEASONS:
        errors = [f"A series can have up to {MAX_SEASONS} seasons."]
        return render_new_season(request, title, season, episodes, errors, 422)
    try:
        data = SeasonInput(episodes=episodes)
    except ValidationError as error:
        return render_new_season(request, title, season, episodes, describe_errors(error), 422)
    with conn:
        library.add_season(conn, title_id, data.episodes)
    return redirect(f"/titles/{title_id}#season-{season}")


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
    episodes = require_season(conn, title_id, season)
    rows = [
        {
            "season": season,
            "number": episode["number"],
            "duration": "" if episode["duration"] is None else str(episode["duration"]),
            "release_date": episode["release_date"] or "",
            "description": episode["description"],
        }
        for episode in episodes
    ]
    is_last = season == len(library.season_counts(conn, title_id))
    return render_season_form(request, title, season, str(rows[-1]["number"]), is_last, rows)


@router.post("/titles/{title_id}/seasons/{season}/details")
async def save_season_details(
    request: Request, conn: Conn, title_id: int, season: int
) -> Response:
    title = require_title(conn, title_id)
    form = await request.form()
    episodes = require_season(conn, title_id, season)
    size = str(form.get("episodes", episodes[-1]["number"]))
    is_last = season == len(library.season_counts(conn, title_id))
    rows, details, errors = [], {}, []
    try:
        new_size = SeasonInput(episodes=size).episodes
    except ValidationError as error:
        errors.extend(describe_errors(error))
    for episode in episodes:
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
        return render_season_form(request, title, season, size, is_last, rows, errors, 422)
    with conn:
        for number, data in details.items():
            library.update_episode(conn, title_id, season, number, data)
        library.resize_season(conn, title_id, season, new_size)
    return redirect(f"/titles/{title_id}/seasons/{season}")


@router.post("/titles/{title_id}/seasons/{season}/delete")
def delete_season(conn: Conn, title_id: int, season: int) -> Response:
    """Only the last season can go, since the others are numbered by position."""
    require_season(conn, title_id, season)
    if season != len(library.season_counts(conn, title_id)):
        raise HTTPException(status_code=404)
    with conn:
        library.delete_last_season(conn, title_id)
    return redirect(f"/titles/{title_id}")


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


def parse_platform(form: dict[str, str]) -> PlatformInput:
    """Read the platform forms, where a year left over from a console type doesn't count."""
    is_console = form["kind"] == PlatformKind.CONSOLE.value
    return PlatformInput(**{**form, "release_year": form["release_year"] if is_console else ""})


def render_platforms(
    request: Request,
    conn: sqlite3.Connection,
    form: dict[str, str] | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    context = {
        "platforms": library.platforms_by_kind(conn),
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
    request: Request,
    conn: Conn,
    name: str = Form(""),
    url: str = Form(""),
    kind: str = Form(PlatformKind.STREAMING.value),
    release_year: str = Form(""),
) -> Response:
    form = {"name": name, "url": url, "kind": kind, "release_year": release_year}
    try:
        with conn:
            platform_id = library.create_platform(conn, parse_platform(form))
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
    request: Request,
    conn: Conn,
    platform_id: int,
    name: str = Form(""),
    url: str = Form(""),
    kind: str = Form(PlatformKind.STREAMING.value),
    release_year: str = Form(""),
) -> Response:
    platform = require_platform(conn, platform_id)
    form = {"name": name, "url": url, "kind": kind, "release_year": release_year}
    try:
        with conn:
            library.update_platform(conn, platform_id, parse_platform(form))
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


def require_influencer(conn: sqlite3.Connection, influencer_id: int) -> sqlite3.Row:
    influencer = influencers.get_influencer(conn, influencer_id)
    if influencer is None:
        raise HTTPException(status_code=404)
    return influencer


def require_collage(
    conn: sqlite3.Connection, influencer_id: int, collage_id: int
) -> tuple[sqlite3.Row, sqlite3.Row]:
    influencer = require_influencer(conn, influencer_id)
    collage = influencers.get_collage(conn, influencer_id, collage_id)
    if collage is None:
        raise HTTPException(status_code=404)
    return influencer, collage


def require_item(conn: sqlite3.Connection, collage_id: int, item_id: int) -> sqlite3.Row:
    item = influencers.get_item(conn, collage_id, item_id)
    if item is None:
        raise HTTPException(status_code=404)
    return item


def read_picture(file: Any, errors: list[str], label: str = "Picture") -> bytes | None:
    """The bytes of an uploaded picture, or None if none was picked. Problems go in errors."""
    # request.form() gives Starlette's UploadFile, which FastAPI's only extends.
    content = file.file.read() if isinstance(file, StarletteUploadFile) else b""
    if not content:
        return None
    try:
        influencers.image_type(content)
    except ValueError as error:
        errors.append(f"{label}: {error}")
        return None
    return content


def influencer_form_values(form: Mapping) -> dict[str, str]:
    keys = ["name", "website", *(f"social_{network.key}" for network in influencers.NETWORKS)]
    return {key: str(form.get(key, "")) for key in keys}


def render_influencer_form(
    request: Request,
    form: dict[str, str],
    influencer: sqlite3.Row | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    context = {
        "form": form,
        "influencer": influencer,
        "networks": influencers.NETWORKS,
        "errors": errors or [],
    }
    return templates.TemplateResponse(
        request, "influencer_form.html", context, status_code=status_code
    )


@router.get("/uploads/{upload_id}")
def upload(conn: Conn, upload_id: int) -> Response:
    row = influencers.get_upload(conn, upload_id)
    if row is None:
        raise HTTPException(status_code=404)
    # A changed picture gets a new id, so this one never changes.
    headers = {"Cache-Control": "private, max-age=31536000, immutable"}
    return Response(row["content"], media_type=row["media_type"], headers=headers)


@router.get("/influencers", response_class=HTMLResponse)
def influencers_page(request: Request, conn: Conn, q: str = "", page: int = 1) -> Response:
    shown, pages = paginate(request, influencers.list_influencers(conn, q.strip()), page)
    context = {"influencers": shown, "pages": pages, "q": q}
    return templates.TemplateResponse(request, "influencers.html", context)


@router.get("/influencers/new", response_class=HTMLResponse)
def new_influencer(request: Request) -> Response:
    return render_influencer_form(request, influencer_form_values({}))


@router.post("/influencers")
async def create_influencer(request: Request, conn: Conn) -> Response:
    form = await request.form()
    values = influencer_form_values(form)
    data, errors = influencers.parse_influencer(values)
    picture = read_picture(form.get("picture"), errors)
    if errors:
        return render_influencer_form(request, values, errors=errors, status_code=422)
    try:
        with conn:
            influencer_id = influencers.create_influencer(conn, data, picture)
    except influencers.DuplicateInfluencerError:
        errors = ["That influencer is already here."]
        return render_influencer_form(request, values, errors=errors, status_code=409)
    return redirect(f"/influencers/{influencer_id}")


def render_influencer(
    request: Request,
    conn: sqlite3.Connection,
    influencer: sqlite3.Row,
    form: Mapping | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    website = influencer["website"]
    context = {
        "influencer": influencer,
        "socials": influencers.influencer_socials(conn, influencer["id"]),
        "website_warnings": influencers.link_warnings(website) if website else [],
        "collages": influencers.list_collages(conn, influencer["id"]),
        "form": form or {},
        "errors": errors or [],
    }
    return templates.TemplateResponse(
        request, "influencer.html", context, status_code=status_code
    )


@router.get("/influencers/{influencer_id}", response_class=HTMLResponse)
def influencer_page(request: Request, conn: Conn, influencer_id: int) -> Response:
    return render_influencer(request, conn, require_influencer(conn, influencer_id))


@router.get("/influencers/{influencer_id}/edit", response_class=HTMLResponse)
def edit_influencer(request: Request, conn: Conn, influencer_id: int) -> Response:
    influencer = require_influencer(conn, influencer_id)
    values = {"name": influencer["name"], "website": influencer["website"]}
    for social in influencers.influencer_socials(conn, influencer_id):
        values[f"social_{social['network']}"] = social["url"]
    return render_influencer_form(request, influencer_form_values(values), influencer)


@router.post("/influencers/{influencer_id}")
async def update_influencer(request: Request, conn: Conn, influencer_id: int) -> Response:
    influencer = require_influencer(conn, influencer_id)
    form = await request.form()
    values = influencer_form_values(form)
    data, errors = influencers.parse_influencer(values)
    picture = read_picture(form.get("picture"), errors)
    if errors:
        return render_influencer_form(request, values, influencer, errors, 422)
    try:
        with conn:
            influencers.update_influencer(
                conn, influencer_id, data, picture, remove_picture=bool(form.get("remove_picture"))
            )
    except influencers.DuplicateInfluencerError:
        errors = ["That influencer is already here."]
        return render_influencer_form(request, values, influencer, errors, 409)
    return redirect(f"/influencers/{influencer_id}")


@router.post("/influencers/{influencer_id}/delete")
def delete_influencer(conn: Conn, influencer_id: int) -> Response:
    require_influencer(conn, influencer_id)
    with conn:
        influencers.delete_influencer(conn, influencer_id)
    return redirect("/influencers")


def parse_collage(name: str, description: str) -> tuple[str, str, list[str]]:
    errors = []
    try:
        name = influencers.check_name(name)
    except ValueError as error:
        errors.append(f"Name: {error}")
    try:
        description = influencers.check_text(description, "description")
    except ValueError as error:
        errors.append(f"Description: {error}")
    return name, description, errors


@router.post("/influencers/{influencer_id}/collages")
def create_collage(
    request: Request,
    conn: Conn,
    influencer_id: int,
    name: str = Form(""),
    description: str = Form(""),
) -> Response:
    influencer = require_influencer(conn, influencer_id)
    form = {"name": name, "description": description}
    name, description, errors = parse_collage(name, description)
    if errors:
        return render_influencer(request, conn, influencer, form, errors, 422)
    try:
        with conn:
            collage_id = influencers.create_collage(conn, influencer_id, name, description)
    except influencers.DuplicateCollageError:
        errors = [f"{influencer['name']} already has a collage called {name}."]
        return render_influencer(request, conn, influencer, form, errors, 409)
    return redirect(f"/influencers/{influencer_id}/collages/{collage_id}")


def render_collage(
    request: Request,
    conn: sqlite3.Connection,
    influencer: sqlite3.Row,
    collage: sqlite3.Row,
    *,
    form: Mapping | None = None,
    errors: list[str] | None = None,
    adding: str = "",
    status_code: int = 200,
) -> Response:
    items = influencers.collage_items(conn, collage["id"])
    links = [(item["id"], item["url"]) for item in items if item["kind"] == "link"]
    warnings = {
        item_id: influencers.link_warnings(url, [other for other_id, other in links
                                                 if other_id != item_id])
        for item_id, url in links
    }
    context = {
        "influencer": influencer,
        "collage": collage,
        "items": items,
        "warnings": warnings,
        "embed_url": influencers.embed_url,
        "is_vertical": influencers.is_vertical,
        "form": form or {},
        "adding": adding,
        "errors": errors or [],
    }
    return templates.TemplateResponse(request, "collage.html", context, status_code=status_code)


def collage_url(influencer_id: int, collage_id: int, anchor: str = "") -> str:
    url = f"/influencers/{influencer_id}/collages/{collage_id}"
    return f"{url}#{anchor}" if anchor else url


@router.get("/influencers/{influencer_id}/collages/{collage_id}", response_class=HTMLResponse)
def collage_page(request: Request, conn: Conn, influencer_id: int, collage_id: int) -> Response:
    influencer, collage = require_collage(conn, influencer_id, collage_id)
    return render_collage(request, conn, influencer, collage)


@router.post("/influencers/{influencer_id}/collages/{collage_id}")
def update_collage(
    request: Request,
    conn: Conn,
    influencer_id: int,
    collage_id: int,
    name: str = Form(""),
    description: str = Form(""),
) -> Response:
    influencer, collage = require_collage(conn, influencer_id, collage_id)
    form = {"name": name, "description": description}
    name, description, errors = parse_collage(name, description)
    if errors:
        return render_collage(
            request, conn, influencer, collage,
            form=form, errors=errors, adding="collage", status_code=422,
        )
    try:
        with conn:
            influencers.update_collage(conn, collage_id, name, description)
    except influencers.DuplicateCollageError:
        errors = [f"{influencer['name']} already has a collage called {name}."]
        return render_collage(
            request, conn, influencer, collage,
            form=form, errors=errors, adding="collage", status_code=409,
        )
    return redirect(collage_url(influencer_id, collage_id))


@router.post("/influencers/{influencer_id}/collages/{collage_id}/delete")
def delete_collage(conn: Conn, influencer_id: int, collage_id: int) -> Response:
    require_collage(conn, influencer_id, collage_id)
    with conn:
        influencers.delete_collage(conn, collage_id)
    return redirect(f"/influencers/{influencer_id}")


@router.post("/influencers/{influencer_id}/collages/{collage_id}/items")
async def add_collage_item(
    request: Request, conn: Conn, influencer_id: int, collage_id: int
) -> Response:
    influencer, collage = require_collage(conn, influencer_id, collage_id)
    form = await request.form()
    kind = str(form.get("kind", ""))
    if kind not in influencers.ITEM_KINDS:
        raise HTTPException(status_code=422)
    values = {"text": str(form.get("text", "")), "url": str(form.get("url", ""))}
    errors: list[str] = []
    fields: dict[str, Any] = {}
    label = {"note": "Note", "link": "Title", "video": "Title", "image": "Caption"}[kind]
    try:
        fields["text"] = influencers.check_text(values["text"], label.lower(), kind == "note")
    except ValueError as error:
        errors.append(f"{label}: {error}")
    if kind in ("link", "video"):
        try:
            if kind == "link":
                fields["url"] = influencers.check_url(values["url"])
            else:
                fields["url"], *video = influencers.check_video(values["url"])
                fields["video"] = tuple(video)
        except ValueError as error:
            errors.append(f"{'Link' if kind == 'link' else 'Video'}: {error}")
    picture = None
    if kind == "image":
        picture = read_picture(form.get("image"), errors, "Image")
        if picture is None and not errors:
            errors.append("Image: pick a picture to add")
    if errors:
        return render_collage(
            request, conn, influencer, collage,
            form=values, errors=errors, adding=kind, status_code=422,
        )
    with conn:
        if picture:
            fields["upload_id"] = influencers.add_upload(conn, picture)
        item_id = influencers.add_item(conn, collage_id, kind, **fields)
    return redirect(collage_url(influencer_id, collage_id, f"item-{item_id}"))


@router.post("/influencers/{influencer_id}/collages/{collage_id}/items/{item_id}/text")
def update_collage_item(
    request: Request,
    conn: Conn,
    influencer_id: int,
    collage_id: int,
    item_id: int,
    text: str = Form(""),
) -> Response:
    influencer, collage = require_collage(conn, influencer_id, collage_id)
    item = require_item(conn, collage_id, item_id)
    try:
        text = influencers.check_text(text, "text", required=item["kind"] == "note")
    except ValueError as error:
        return render_collage(
            request, conn, influencer, collage,
            errors=[f"Editing an item: {error}"], status_code=422,
        )
    with conn:
        influencers.update_item_text(conn, item_id, text)
    return redirect(collage_url(influencer_id, collage_id, f"item-{item_id}"))


@router.post("/influencers/{influencer_id}/collages/{collage_id}/items/{item_id}/move")
def move_collage_item(
    conn: Conn,
    influencer_id: int,
    collage_id: int,
    item_id: int,
    step: Annotated[int, Form(ge=-1, le=1)],
) -> Response:
    require_collage(conn, influencer_id, collage_id)
    require_item(conn, collage_id, item_id)
    with conn:
        influencers.move_item(conn, collage_id, item_id, step)
    return redirect(collage_url(influencer_id, collage_id, f"item-{item_id}"))


@router.post("/influencers/{influencer_id}/collages/{collage_id}/items/{item_id}/delete")
def delete_collage_item(conn: Conn, influencer_id: int, collage_id: int, item_id: int) -> Response:
    require_collage(conn, influencer_id, collage_id)
    require_item(conn, collage_id, item_id)
    with conn:
        influencers.delete_item(conn, item_id)
    return redirect(collage_url(influencer_id, collage_id))


def render_plans(
    request: Request,
    conn: sqlite3.Connection,
    form: dict[str, str] | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    context = {
        "plans": library.list_plans(conn),
        "favorite_count": library.favorite_count(conn),
        "planned_count": library.planned_count(conn),
        "form": form or {},
        "errors": errors or [],
    }
    return templates.TemplateResponse(request, "plans.html", context, status_code=status_code)


def render_plan(
    request: Request,
    conn: sqlite3.Connection,
    plan: sqlite3.Row,
    form: Mapping | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> Response:
    context = {
        "plan": plan,
        "titles": library.plan_titles(conn, plan["id"]),
        "available": library.titles_outside_plan(conn, plan["id"]),
        "form": form or plan,
        "errors": errors or [],
    }
    return templates.TemplateResponse(request, "plan.html", context, status_code=status_code)


@router.get("/plans", response_class=HTMLResponse)
def plans(request: Request, conn: Conn) -> Response:
    return render_plans(request, conn)


@router.post("/plans")
def create_plan(
    request: Request, conn: Conn, name: str = Form(""), description: str = Form("")
) -> Response:
    form = {"name": name, "description": description}
    try:
        with conn:
            plan_id = library.create_plan(conn, PlanInput(**form))
    except ValidationError as error:
        return render_plans(request, conn, form, describe_errors(error), 422)
    except library.DuplicatePlanError:
        return render_plans(request, conn, form, ["That plan already exists."], 409)
    return redirect(f"/plans/{plan_id}")


@router.get("/plans/favorites", response_class=HTMLResponse)
def favorites_plan(request: Request, conn: Conn) -> Response:
    context = {"titles": library.list_titles(conn, favorite=True)}
    return templates.TemplateResponse(request, "plan_favorites.html", context)


@router.get("/plans/planned", response_class=HTMLResponse)
def planned_plan(request: Request, conn: Conn) -> Response:
    context = {"titles": library.list_titles(conn, status=Status.PLANNED)}
    return templates.TemplateResponse(request, "plan_planned.html", context)


@router.get("/plans/{plan_id}", response_class=HTMLResponse)
def plan_page(request: Request, conn: Conn, plan_id: int) -> Response:
    return render_plan(request, conn, require_plan(conn, plan_id))


@router.post("/plans/{plan_id}")
def update_plan(
    request: Request, conn: Conn, plan_id: int, name: str = Form(""), description: str = Form("")
) -> Response:
    plan = require_plan(conn, plan_id)
    form = {"name": name, "description": description}
    try:
        with conn:
            library.update_plan(conn, plan_id, PlanInput(**form))
    except ValidationError as error:
        return render_plan(request, conn, plan, form, describe_errors(error), 422)
    except library.DuplicatePlanError:
        return render_plan(request, conn, plan, form, ["That plan already exists."], 409)
    return redirect(f"/plans/{plan_id}")


@router.post("/plans/{plan_id}/delete")
def delete_plan(conn: Conn, plan_id: int) -> Response:
    require_plan(conn, plan_id)
    with conn:
        library.delete_plan(conn, plan_id)
    return redirect("/plans")


@router.post("/plans/{plan_id}/items")
def add_to_plan(conn: Conn, plan_id: int, title_id: int = Form(...)) -> Response:
    require_plan(conn, plan_id)
    require_title(conn, title_id)
    with conn:
        library.add_to_plan(conn, plan_id, title_id)
    return redirect(f"/plans/{plan_id}")


@router.post("/plans/{plan_id}/items/{title_id}/delete")
def remove_from_plan(conn: Conn, plan_id: int, title_id: int) -> Response:
    require_plan(conn, plan_id)
    with conn:
        library.remove_from_plan(conn, plan_id, title_id)
    return redirect(f"/plans/{plan_id}")


def render_data(
    request: Request,
    conn: sqlite3.Connection,
    *,
    duo_errors: list[str] | None = None,
    load_errors: list[str] | None = None,
    loaded: dict | None = None,
    status_code: int = 200,
) -> Response:
    """The Duo tab: Duo first, then exporting and importing your own file."""
    content = library.get_duo_file(conn)
    file = watch_data.parse(content) if content else None
    sections = duo.suggestions(conn, file) if file else []
    matches = {
        (item.pair.kind, item.pair.name.casefold(), item.pair.year)
        for section in sections
        for item in section.items
    }
    context = {
        "file": file,
        "sections": sections,
        "matches": len(matches),
        "duo_errors": duo_errors or [],
        "load_errors": load_errors or [],
        "loaded": loaded,
    }
    return templates.TemplateResponse(request, "duo.html", context, status_code=status_code)


@router.get("/duo", response_class=HTMLResponse)
def duo_page(
    request: Request,
    conn: Conn,
    added: int | None = None,
    updated: int = 0,
    backup: str = "",
) -> Response:
    loaded = None if added is None else {"added": added, "updated": updated, "backup": backup}
    return render_data(request, conn, loaded=loaded)


@router.get("/data/export")
def export_data(conn: Conn) -> Response:
    filename = f"plasma-watch-data-{date.today().isoformat()}.json"
    return Response(
        watch_data.dump(watch_data.export(conn)),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/data/load")
def load_data(request: Request, conn: Conn, file: UploadFile | None = File(None)) -> Response:
    try:
        parsed = watch_data.parse(file.file.read() if file else b"")
    except ImportFileError as error:
        return render_data(request, conn, load_errors=error.problems, status_code=422)
    backup = watch_data.backup_database(conn)
    with conn:
        added, updated = watch_data.load(conn, parsed)
    query = urlencode({"added": added, "updated": updated, "backup": backup.name})
    return redirect(f"/duo?{query}#your-data")


@router.get("/data")
def data_page() -> Response:
    """Export and import used to have their own Data tab, so old links still land on them."""
    return redirect("/duo#your-data")


@router.post("/duo/file")
def set_duo_file(request: Request, conn: Conn, file: UploadFile | None = File(None)) -> Response:
    content = file.file.read() if file else b""
    try:
        watch_data.parse(content)
    except ImportFileError as error:
        return render_data(request, conn, duo_errors=error.problems, status_code=422)
    with conn:
        library.set_duo_file(conn, content)
    return redirect("/duo")


@router.post("/duo/file/delete")
def delete_duo_file(conn: Conn) -> Response:
    with conn:
        library.clear_duo_file(conn)
    return redirect("/duo")


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

    @app.middleware("http")
    async def never_cache_pages(request: Request, call_next: Callable) -> Response:
        # A cached page shows old data, and clicking a toggle on one undoes a saved change.
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/"):
            # Check back every time, so a changed stylesheet shows up without a hard refresh.
            # An unchanged file is only a quick 304 thanks to its ETag.
            response.headers["Cache-Control"] = "no-cache"
        return response

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
