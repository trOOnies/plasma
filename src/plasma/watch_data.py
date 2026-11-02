"""Watch data files: a small JSON copy of the user's watch statuses and ratings.

A file holds only the titles that have such data, with blanks left out. Loading one
overwrites those titles and adds any that are missing. Everything else stays as it is.
The format is documented in the README.
"""

import json
import sqlite3
from datetime import date, datetime
from itertools import groupby
from pathlib import Path

from pydantic import ValidationError

from plasma import library
from plasma.importer import ImportFileError
from plasma.models import MultiplayerStatus, Status, WatchFile, describe_errors

FORMAT_KEY = "plasma_watch_data"
FORMAT_VERSION = 1
LEFT_OUT = (None, Status.NONE.value, [], {})
NOT_A_WATCH_FILE = "That isn't a Plasma watch data file."


def _ranges(season: int, numbers: list[int]) -> list[list[int]]:
    """Runs of consecutive episode numbers, as [season, first, last]."""
    runs = groupby(enumerate(numbers), key=lambda pair: pair[1] - pair[0])
    ranges = []
    for _, run in runs:
        found = [number for _, number in run]
        ranges.append([season, found[0], found[-1]])
    return ranges


def _episode_data(conn: sqlite3.Connection, title_id: int) -> tuple[list, dict]:
    watched, ratings = [], {}
    for season, episodes in library.episodes_by_season(conn, title_id).items():
        numbers = [episode["number"] for episode in episodes if episode["watched"]]
        watched.extend(_ranges(season, numbers))
        for episode in episodes:
            if episode["rating"]:
                code = f"S{season:02}E{episode['number']:02}"
                ratings[code] = {"rating": episode["rating"]}
    return watched, ratings


def _has_data(title: sqlite3.Row, watched: list, ratings: dict) -> bool:
    return bool(
        title["status"] != Status.NONE.value
        or title["multiplayer_status"] != MultiplayerStatus.NONE.value
        or title["rating"]
        or title["watched_on"]
        or watched
        or ratings
    )


def export(conn: sqlite3.Connection) -> dict:
    """The file's content. Modes are only stated where they differ from a default."""
    entries = []
    for title in library.list_titles(conn, order="t.kind, t.name COLLATE NOCASE, t.year"):
        watched, ratings = _episode_data(conn, title["id"])
        if not _has_data(title, watched, ratings):
            continue
        multiplayer = None if title["multiplayer"] is None else bool(title["multiplayer"])
        entry = {
            "kind": title["kind"],
            "name": title["name"],
            "year": title["year"],
            "status": title["status"],
            "singleplayer": None if title["singleplayer"] else False,
            "multiplayer": multiplayer,
            "multiplayer_status": title["multiplayer_status"],
            "rating": title["rating"],
            "watched_on": title["watched_on"],
            "seasons": library.season_counts(conn, title["id"]),
            "watched": watched,
            "episodes": ratings,
        }
        entries.append({key: value for key, value in entry.items() if value not in LEFT_OUT})
    return {
        FORMAT_KEY: FORMAT_VERSION,
        "exported_on": date.today().isoformat(),
        "titles": entries,
    }


def dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def parse(content: bytes) -> WatchFile:
    """Read a file's content, raising ImportFileError with every problem found."""
    try:
        document = json.loads(content)
    except ValueError as error:  # Covers text that isn't valid UTF-8 as well.
        raise ImportFileError([NOT_A_WATCH_FILE]) from error
    if not isinstance(document, dict) or FORMAT_KEY not in document:
        raise ImportFileError([NOT_A_WATCH_FILE])
    try:
        return WatchFile.model_validate(document)
    except ValidationError as error:
        raise ImportFileError(describe_errors(error)) from error


def load(conn: sqlite3.Connection, file: WatchFile) -> tuple[int, int]:
    """Overwrite the file's titles, adding missing ones. Returns (added, updated)."""
    added = updated = 0
    for entry in file.titles:
        existing = library.find_title(conn, entry.kind, entry.name, entry.year)
        if existing:
            title_id = existing["id"]
            updated += 1
        else:
            title_id = library.create_title(conn, entry)
            added += 1
        library.overwrite_tracking(conn, title_id, entry)
    return added, updated


def backup_database(conn: sqlite3.Connection) -> Path:
    """Copy the database to a new file next to it, and return that file's path."""
    database = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    path = database.with_name(f"backup-before-load-{datetime.now():%Y%m%d-%H%M%S}.db")
    target = sqlite3.connect(path)
    try:
        conn.backup(target)
    finally:
        target.close()
    return path
