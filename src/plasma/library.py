"""Library queries and updates.

Nothing here commits: callers wrap their work in `with conn:` so a request or a
whole import is one transaction.

A game can have a singleplayer status, held in `status` like any other title's, and a
multiplayer status of its own. The library filters and lists go by either one.

A title starts with no status, and planned is only ever the user's choice. Once
episodes are watched, a series' status follows them, except that "dropped" sticks
until the user changes it. Series without episode data keep whatever status was set.

Marking an episode or season watched also marks everything before it. Clicking a
watched episode moves progress back to it, or unmarks it if it's the last one watched.
"""

import sqlite3
from collections.abc import Iterable
from datetime import date, timedelta
from itertools import zip_longest

from plasma.db import fold
from plasma.models import (
    AgeRestriction,
    Details,
    Kind,
    MultiplayerStatus,
    Ownership,
    PlanInput,
    PlatformInput,
    PlatformKind,
    Status,
    TitleInput,
    WatchEntry,
)

TITLE_SELECT = """
    SELECT t.*, COUNT(e.id) AS episodes_total, COALESCE(SUM(e.watched), 0) AS episodes_watched
    FROM titles t LEFT JOIN episodes e ON e.title_id = t.id
"""
UP_TO_EPISODE = "(season < ? OR (season = ? AND number <= ?))"
AFTER_EPISODE = "(season > ? OR (season = ? AND number > ?))"
BEFORE_EPISODE = "(season < ? OR (season = ? AND number < ?))"

TAB_ORDER = "t.favorite DESC, t.name COLLATE NOCASE"
GAME_TAB_ORDER = (
    "t.favorite DESC,"
    " CASE t.ownership WHEN 'wanted' THEN 0 WHEN 'owned' THEN 1 ELSE 2 END,"
    " t.name COLLATE NOCASE"
)
# A release date or year, so a title that has neither comes last.
RELEASED = "COALESCE(t.release_date, CAST(t.year AS TEXT))"
FRANCHISE_ORDER = f"{RELEASED} IS NULL, {RELEASED}, t.name COLLATE NOCASE"

UPCOMING_DAYS = 90
JUST_RELEASED_DAYS = 30

# Episodes come first so that, on the same date, a title links to the actual episode.
RELEASES_QUERY = """
    SELECT t.id AS title_id, t.name, t.kind, e.season, e.number, e.release_date
    FROM episodes e JOIN titles t ON t.id = e.title_id
    WHERE e.release_date BETWEEN ? AND ?
    UNION ALL
    SELECT id, name, kind, NULL, NULL, release_date
    FROM titles WHERE release_date BETWEEN ? AND ?
"""


class DuplicateTitleError(Exception):
    pass


class DuplicatePlatformError(Exception):
    pass


class DuplicatePlanError(Exception):
    pass


def _today() -> str:
    return date.today().isoformat()


def iso_date(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _touch(conn: sqlite3.Connection, title_id: int) -> None:
    conn.execute("UPDATE titles SET updated_at = datetime('now') WHERE id = ?", (title_id,))


def get_title(conn: sqlite3.Connection, title_id: int) -> sqlite3.Row | None:
    return conn.execute(TITLE_SELECT + " WHERE t.id = ? GROUP BY t.id", (title_id,)).fetchone()


def find_title(
    conn: sqlite3.Connection, kind: Kind, name: str, year: int | None
) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT id FROM titles"
        " WHERE kind = ? AND name = ? COLLATE NOCASE AND COALESCE(year, 0) = ?",
        (kind.value, name, year or 0),
    ).fetchone()
    return get_title(conn, row["id"]) if row else None


def list_titles(
    conn: sqlite3.Connection,
    kind: Kind | None = None,
    status: Status | None = None,
    query: str = "",
    order: str = "t.name COLLATE NOCASE",
    favorite: bool = False,
    platform_id: int | None = None,
    ownership: Ownership | None = None,
    genre: str = "",
    age_restriction: AgeRestriction | None = None,
    franchise: str = "",
) -> list[sqlite3.Row]:
    clauses, params = [], []
    if kind:
        clauses.append("t.kind = ?")
        params.append(kind.value)
    if status is Status.NONE:
        clauses.append("t.status = 'none' AND t.multiplayer_status = 'none'")
    elif status:
        clauses.append("(t.status = ? OR t.multiplayer_status = ?)")
        params.extend([status.value, status.value])
    if favorite:
        clauses.append("t.favorite = 1")
    if ownership:
        clauses.append("t.ownership = ?")
        params.append(ownership.value)
    if franchise:
        clauses.append("t.franchise = ? COLLATE NOCASE")
        params.append(franchise)
    if platform_id:
        clauses.append("t.id IN (SELECT title_id FROM title_platforms WHERE platform_id = ?)")
        params.append(platform_id)
    if genre:
        clauses.append("t.id IN (SELECT title_id FROM title_genres WHERE genre = ?)")
        params.append(genre)
    if age_restriction:
        clauses.append("t.age_restriction = ?")
        params.append(age_restriction.value)
    if query:
        folded = fold(query) or ""
        escaped = folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("fold(t.name) LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(f"{TITLE_SELECT}{where} GROUP BY t.id ORDER BY {order}", params).fetchall()


BLANK = (None, "")
PLATFORM_ENTRY = "platform"


def is_removed(conn: sqlite3.Connection, kind: str, name: str, year: int | None = None) -> bool:
    """Whether the web UI deleted or renamed this entry, so an import must not add it back."""
    row = conn.execute(
        "SELECT 1 FROM removed_entries WHERE kind = ? AND name = ? AND year = ?",
        (kind, name, year or 0),
    ).fetchone()
    return row is not None


def _note_removed(conn: sqlite3.Connection, kind: str, name: str, year: int | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO removed_entries (kind, name, year) VALUES (?, ?, ?)",
        (kind, name, year or 0),
    )


def _forget_removed(
    conn: sqlite3.Connection, kind: str, name: str, year: int | None = None
) -> None:
    conn.execute(
        "DELETE FROM removed_entries WHERE kind = ? AND name = ? AND year = ?",
        (kind, name, year or 0),
    )


def _cleared_fields(
    conn: sqlite3.Connection, title_id: int, season: int = 0, number: int = 0
) -> set[str]:
    rows = conn.execute(
        "SELECT field FROM cleared_fields WHERE title_id = ? AND season = ? AND number = ?",
        (title_id, season, number),
    )
    return {row["field"] for row in rows}


def _note_cleared(
    conn: sqlite3.Connection,
    title_id: int,
    fields: Iterable[str],
    season: int = 0,
    number: int = 0,
) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO cleared_fields (title_id, season, number, field)"
        " VALUES (?, ?, ?, ?)",
        [(title_id, season, number, field) for field in fields],
    )


def _emptied(row: sqlite3.Row, values: dict[str, object]) -> list[str]:
    """The columns that have a value now and are about to lose it."""
    return [
        column
        for column, value in values.items()
        if value in BLANK and row[column] not in BLANK
    ]


def create_title(conn: sqlite3.Connection, data: TitleInput) -> int:
    try:
        title_id = conn.execute(
            "INSERT INTO titles"
            " (kind, name, year, status, singleplayer, multiplayer, multiplayer_status,"
            " age_restriction, rating, duration, release_date, hours_played,"
            " producer, franchise, description, notes, watched_on)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data.kind.value,
                data.name,
                data.year,
                data.status.value,
                data.singleplayer,
                data.multiplayer,
                data.multiplayer_status.value,
                _age_value(data.age_restriction),
                data.rating,
                data.duration,
                iso_date(data.release_date),
                data.hours_played,
                data.producer,
                data.franchise,
                data.description,
                data.notes,
                iso_date(data.watched_on),
            ),
        ).lastrowid
    except sqlite3.IntegrityError as error:
        raise DuplicateTitleError(data.name) from error
    _forget_removed(conn, data.kind.value, data.name, data.year)
    _set_seasons(conn, title_id, data.seasons)
    _set_genres(conn, title_id, data.genres)
    if data.status is Status.COMPLETED:
        conn.execute("UPDATE episodes SET watched = 1 WHERE title_id = ?", (title_id,))
    sync_status(conn, title_id)
    return title_id


def update_title(conn: sqlite3.Connection, title_id: int, data: TitleInput) -> None:
    title = get_title(conn, title_id)
    previous = title["status"]
    fields = {
        "age_restriction": _age_value(data.age_restriction),
        "duration": data.duration,
        "release_date": iso_date(data.release_date),
        "hours_played": data.hours_played,
        "producer": data.producer,
        "franchise": data.franchise,
        "description": data.description,
        "notes": data.notes,
    }
    cleared = _emptied(title, fields)
    if title_genres(conn, title_id) and not data.genres:
        cleared.append("genres")
    try:
        conn.execute(
            "UPDATE titles SET name = ?, year = ?, status = ?, singleplayer = ?,"
            " multiplayer = ?, multiplayer_status = ?, age_restriction = ?,"
            " duration = ?, release_date = ?, hours_played = ?, producer = ?,"
            " franchise = ?, description = ?, notes = ?,"
            " watched_on = ?,"
            " updated_at = datetime('now')"
            " WHERE id = ?",
            (
                data.name,
                data.year,
                data.status.value,
                data.singleplayer,
                data.multiplayer,
                data.multiplayer_status.value,
                _age_value(data.age_restriction),
                data.duration,
                iso_date(data.release_date),
                data.hours_played,
                data.producer,
                data.franchise,
                data.description,
                data.notes,
                iso_date(data.watched_on),
                title_id,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise DuplicateTitleError(data.name) from error
    _note_cleared(conn, title_id, cleared)
    if (title["name"].casefold(), title["year"] or 0) != (data.name.casefold(), data.year or 0):
        _note_removed(conn, title["kind"], title["name"], title["year"])
        _forget_removed(conn, title["kind"], data.name, data.year)
    _set_genres(conn, title_id, data.genres)
    # Only a change to completed marks everything watched. A form re-submitted as
    # completed must leave any season added since then unwatched.
    if data.status is Status.COMPLETED and previous != Status.COMPLETED.value:
        conn.execute("UPDATE episodes SET watched = 1 WHERE title_id = ?", (title_id,))
    sync_status(conn, title_id)


def _age_value(age_restriction: AgeRestriction | None) -> str | None:
    return age_restriction.value if age_restriction else None


def _set_genres(conn: sqlite3.Connection, title_id: int, genres: list[str]) -> None:
    conn.execute("DELETE FROM title_genres WHERE title_id = ?", (title_id,))
    conn.executemany(
        "INSERT INTO title_genres (title_id, genre) VALUES (?, ?)",
        [(title_id, genre) for genre in genres],
    )


def title_genres(conn: sqlite3.Connection, title_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT genre FROM title_genres WHERE title_id = ? ORDER BY genre", (title_id,)
    )
    return [row["genre"] for row in rows]


def fill_title_genres(conn: sqlite3.Connection, title_id: int, genres: list[str]) -> bool:
    """Set the title's genres unless it already has some. Returns True if it changed."""
    if not genres or title_genres(conn, title_id) or "genres" in _cleared_fields(conn, title_id):
        return False
    _set_genres(conn, title_id, genres)
    return True


def delete_title(conn: sqlite3.Connection, title_id: int) -> None:
    title = get_title(conn, title_id)
    _note_removed(conn, title["kind"], title["name"], title["year"])
    conn.execute("DELETE FROM titles WHERE id = ?", (title_id,))


def _fill_blanks(
    conn: sqlite3.Connection,
    table: str,
    row: sqlite3.Row,
    values: dict[str, bool | int | str | None],
    cleared: set[str],
) -> bool:
    """Fill the row's blanks, leaving alone any the user emptied on purpose."""
    changes = {
        column: value
        for column, value in values.items()
        if value not in BLANK and row[column] in BLANK and column not in cleared
    }
    if changes:
        assignments = ", ".join(f"{column} = ?" for column in changes)
        conn.execute(
            f"UPDATE {table} SET {assignments} WHERE id = ?", (*changes.values(), row["id"])
        )
    return bool(changes)


def fill_blanks(
    conn: sqlite3.Connection, title_id: int, **values: bool | int | str | None
) -> bool:
    """Set the given columns only where the title has no value. Returns True if any changed."""
    cleared = _cleared_fields(conn, title_id)
    return _fill_blanks(conn, "titles", get_title(conn, title_id), values, cleared)


def set_image(conn: sqlite3.Connection, title_id: int, image: str) -> None:
    conn.execute("UPDATE titles SET image = ? WHERE id = ?", (image, title_id))


def toggle_favorite(conn: sqlite3.Connection, title_id: int) -> None:
    conn.execute("UPDATE titles SET favorite = NOT favorite WHERE id = ?", (title_id,))


def set_favorite(conn: sqlite3.Connection, title_id: int, favorite: bool) -> None:
    conn.execute("UPDATE titles SET favorite = ? WHERE id = ?", (favorite, title_id))


def turn_off_singleplayer(conn: sqlite3.Connection, title_id: int) -> None:
    conn.execute("UPDATE titles SET singleplayer = 0 WHERE id = ?", (title_id,))


def set_ownership(conn: sqlite3.Connection, title_id: int, ownership: Ownership) -> None:
    conn.execute("UPDATE titles SET ownership = ? WHERE id = ?", (ownership.value, title_id))


def set_rating(conn: sqlite3.Connection, title_id: int, rating: int | None) -> None:
    _note_cleared(conn, title_id, _emptied(get_title(conn, title_id), {"rating": rating}))
    conn.execute("UPDATE titles SET rating = ? WHERE id = ?", (rating, title_id))


def set_episode_rating(
    conn: sqlite3.Connection, title_id: int, season: int, number: int, rating: int | None
) -> None:
    episode = get_episode(conn, title_id, season, number)
    _note_cleared(conn, title_id, _emptied(episode, {"rating": rating}), season, number)
    conn.execute(
        "UPDATE episodes SET rating = ? WHERE title_id = ? AND season = ? AND number = ?",
        (rating, title_id, season, number),
    )


def list_franchises(conn: sqlite3.Connection, kind: Kind) -> list[str]:
    """Franchise names used by this kind. A franchise only ever holds titles of one kind."""
    rows = conn.execute(
        "SELECT MIN(franchise) AS name FROM titles WHERE kind = ? AND franchise != ''"
        " GROUP BY franchise COLLATE NOCASE ORDER BY name COLLATE NOCASE",
        (kind.value,),
    )
    return [row["name"] for row in rows]


def franchise_neighbors(
    conn: sqlite3.Connection, title: sqlite3.Row
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    """The franchise's titles released just before and just after this one."""
    if not title["franchise"]:
        return None, None
    titles = list_titles(
        conn, Kind(title["kind"]), franchise=title["franchise"], order=FRANCHISE_ORDER
    )
    index = [row["id"] for row in titles].index(title["id"])
    earlier = titles[index - 1] if index > 0 else None
    later = titles[index + 1] if index + 1 < len(titles) else None
    return earlier, later


def season_counts(conn: sqlite3.Connection, title_id: int) -> list[int]:
    """Episodes per season, with the highest episode number standing for the season's size."""
    rows = conn.execute(
        "SELECT season, MAX(number) AS size FROM episodes WHERE title_id = ? GROUP BY season",
        (title_id,),
    ).fetchall()
    sizes = {row["season"]: row["size"] for row in rows}
    return [sizes.get(season, 0) for season in range(1, max(sizes, default=0) + 1)]


def _set_seasons(conn: sqlite3.Connection, title_id: int, counts: list[int]) -> None:
    """Resize the episode list, keeping the watch state of episodes that remain."""
    conn.executemany(
        "INSERT OR IGNORE INTO episodes (title_id, season, number) VALUES (?, ?, ?)",
        [
            (title_id, season, number)
            for season, count in enumerate(counts, start=1)
            for number in range(1, count + 1)
        ],
    )
    existing = conn.execute(
        "SELECT id, season, number FROM episodes WHERE title_id = ?", (title_id,)
    ).fetchall()
    conn.executemany(
        "DELETE FROM episodes WHERE id = ?",
        [
            (row["id"],)
            for row in existing
            if row["season"] > len(counts) or row["number"] > counts[row["season"] - 1]
        ],
    )


def extend_seasons(conn: sqlite3.Connection, title_id: int, counts: list[int]) -> bool:
    """Grow the episode list to at least `counts`. Returns True if anything was added.

    Episodes the user removed stay removed, unless `counts` now goes past them.
    """
    current = season_counts(conn, title_id)
    rows = conn.execute(
        "SELECT season, episodes FROM trimmed_seasons WHERE title_id = ?", (title_id,)
    )
    trimmed = {row["season"]: row["episodes"] for row in rows}
    wanted = [
        count if count > trimmed.get(season, 0) else 0
        for season, count in enumerate(counts, start=1)
    ]
    merged = [max(pair) for pair in zip_longest(current, wanted, fillvalue=0)]
    while merged and merged[-1] == 0:
        merged.pop()
    if merged == current:
        return False
    _set_seasons(conn, title_id, merged)
    sync_status(conn, title_id)
    return True


def add_season(conn: sqlite3.Connection, title_id: int, episodes: int) -> int:
    """Add a season after the last one. Returns its number."""
    counts = [*season_counts(conn, title_id), episodes]
    _replace_seasons(conn, title_id, counts)
    return len(counts)


def resize_season(conn: sqlite3.Connection, title_id: int, season: int, episodes: int) -> None:
    """Change a season's size. Episodes removed lose their details and watch history."""
    counts = season_counts(conn, title_id)
    if episodes < counts[season - 1]:
        _note_trimmed(conn, title_id, season, counts[season - 1])
    counts[season - 1] = episodes
    _replace_seasons(conn, title_id, counts)


def delete_last_season(conn: sqlite3.Connection, title_id: int) -> None:
    counts = season_counts(conn, title_id)
    _note_trimmed(conn, title_id, len(counts), counts[-1])
    _replace_seasons(conn, title_id, counts[:-1])


def _note_trimmed(conn: sqlite3.Connection, title_id: int, season: int, episodes: int) -> None:
    conn.execute(
        "INSERT INTO trimmed_seasons (title_id, season, episodes) VALUES (?, ?, ?)"
        " ON CONFLICT (title_id, season) DO UPDATE"
        " SET episodes = MAX(episodes, excluded.episodes)",
        (title_id, season, episodes),
    )


def _replace_seasons(conn: sqlite3.Connection, title_id: int, counts: list[int]) -> None:
    _set_seasons(conn, title_id, counts)
    _touch(conn, title_id)
    sync_status(conn, title_id)


def episodes_by_season(conn: sqlite3.Connection, title_id: int) -> dict[int, list[sqlite3.Row]]:
    seasons: dict[int, list[sqlite3.Row]] = {}
    rows = conn.execute(
        "SELECT * FROM episodes WHERE title_id = ? ORDER BY season, number", (title_id,)
    )
    for row in rows:
        seasons.setdefault(row["season"], []).append(row)
    return seasons


def get_episode(
    conn: sqlite3.Connection, title_id: int, season: int, number: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM episodes WHERE title_id = ? AND season = ? AND number = ?",
        (title_id, season, number),
    ).fetchone()


def adjacent_episodes(
    conn: sqlite3.Connection, title_id: int, season: int, number: int
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    """The episodes before and after this one, across seasons."""
    params = (title_id, season, season, number)
    earlier = conn.execute(
        f"SELECT * FROM episodes WHERE title_id = ? AND {BEFORE_EPISODE}"
        " ORDER BY season DESC, number DESC LIMIT 1",
        params,
    ).fetchone()
    later = conn.execute(
        f"SELECT * FROM episodes WHERE title_id = ? AND {AFTER_EPISODE}"
        " ORDER BY season, number LIMIT 1",
        params,
    ).fetchone()
    return earlier, later


def update_episode(
    conn: sqlite3.Connection, title_id: int, season: int, number: int, data: Details
) -> None:
    fields = {
        "duration": data.duration,
        "release_date": iso_date(data.release_date),
        "description": data.description,
    }
    episode = get_episode(conn, title_id, season, number)
    _note_cleared(conn, title_id, _emptied(episode, fields), season, number)
    conn.execute(
        "UPDATE episodes SET duration = ?, release_date = ?, description = ?"
        " WHERE title_id = ? AND season = ? AND number = ?",
        (data.duration, iso_date(data.release_date), data.description, title_id, season, number),
    )


def fill_episode_blanks(
    conn: sqlite3.Connection, title_id: int, season: int, number: int, data: Details
) -> bool:
    """Set an episode's details only where it has none. Returns True if any changed."""
    values = {
        "duration": data.duration,
        "release_date": iso_date(data.release_date),
        "description": data.description,
        "rating": data.rating,
    }
    row = get_episode(conn, title_id, season, number)
    return _fill_blanks(
        conn, "episodes", row, values, _cleared_fields(conn, title_id, season, number)
    )


def next_episode(conn: sqlite3.Connection, title_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM episodes WHERE title_id = ? AND watched = 0"
        " ORDER BY season, number LIMIT 1",
        (title_id,),
    ).fetchone()


def sync_status(conn: sqlite3.Connection, title_id: int) -> None:
    title = get_title(conn, title_id)
    total, watched = title["episodes_total"], title["episodes_watched"]
    if title["status"] == Status.DROPPED.value or total == 0:
        return
    if watched == total:
        status = Status.COMPLETED
    elif watched > 0:
        status = Status.WATCHING
    elif title["status"] == Status.PLANNED.value:
        return
    else:
        status = Status.NONE
    conn.execute("UPDATE titles SET status = ? WHERE id = ?", (status.value, title_id))


def locked_statuses(title: sqlite3.Row) -> list[str]:
    """Statuses that can't be picked directly, because the episodes decide against them."""
    total, watched = title["episodes_total"], title["episodes_watched"]
    if total == 0:
        return []
    if watched == 0:
        return [Status.WATCHING.value]
    if watched == total:
        return [Status.PLANNED.value, Status.WATCHING.value]
    return [Status.PLANNED.value]


def set_status(conn: sqlite3.Connection, title_id: int, status: Status) -> None:
    """Apply a one-click status change. Completing a series marks all of it watched."""
    title = get_title(conn, title_id)
    watched_on = None
    if title["kind"] != Kind.SERIES.value and status is Status.COMPLETED:
        watched_on = title["watched_on"] or _today()
    conn.execute(
        "UPDATE titles SET status = ?, watched_on = ?, updated_at = datetime('now') WHERE id = ?",
        (status.value, watched_on, title_id),
    )
    if status is Status.COMPLETED:
        _mark(conn, title_id, "1 = 1", (), True)
    sync_status(conn, title_id)


def set_multiplayer_status(
    conn: sqlite3.Connection, title_id: int, status: MultiplayerStatus
) -> None:
    conn.execute(
        "UPDATE titles SET multiplayer_status = ?, updated_at = datetime('now') WHERE id = ?",
        (status.value, title_id),
    )


def overwrite_tracking(conn: sqlite3.Connection, title_id: int, entry: WatchEntry) -> None:
    """Make a title's watch status and ratings match a watch data file.

    The file has no say over seasons it doesn't list, or over episodes if it lists none.
    """
    extend_seasons(conn, title_id, entry.seasons)
    conn.execute(
        "UPDATE titles SET status = ?, multiplayer_status = ?, watched_on = ?, singleplayer = ?,"
        " multiplayer = COALESCE(?, multiplayer), updated_at = datetime('now') WHERE id = ?",
        (
            entry.status.value,
            entry.multiplayer_status.value,
            iso_date(entry.watched_on),
            entry.singleplayer,
            entry.multiplayer,
            title_id,
        ),
    )
    set_rating(conn, title_id, entry.rating)
    if entry.seasons:
        _overwrite_episodes(conn, title_id, entry)
    elif entry.status is Status.COMPLETED:
        _mark(conn, title_id, "1 = 1", (), True)
    sync_status(conn, title_id)


def _overwrite_episodes(conn: sqlite3.Connection, title_id: int, entry: WatchEntry) -> None:
    """Episodes that stay watched keep their date. The file doesn't hold any."""
    watched = entry.watched_episodes()
    rows = conn.execute("SELECT * FROM episodes WHERE title_id = ?", (title_id,)).fetchall()
    for row in rows:
        episode = (row["season"], row["number"])
        if not entry.is_listed(*episode):
            continue
        if (episode in watched) != bool(row["watched"]):
            conn.execute(
                "UPDATE episodes SET watched = ?, watched_on = NULL WHERE id = ?",
                (episode in watched, row["id"]),
            )
        details = entry.episodes.get(episode)
        rating = details.rating if details else None
        if rating != row["rating"]:
            set_episode_rating(conn, title_id, *episode, rating)


def _mark(conn: sqlite3.Connection, title_id: int, where: str, params: tuple, watched: bool) -> int:
    """Set the watch state of matching episodes that differ, keeping existing dates."""
    cursor = conn.execute(
        f"UPDATE episodes SET watched = ?, watched_on = ?"
        f" WHERE title_id = ? AND watched != ? AND {where}",
        (int(watched), _today() if watched else None, title_id, int(watched), *params),
    )
    if cursor.rowcount:
        _touch(conn, title_id)
        sync_status(conn, title_id)
    return cursor.rowcount


def toggle_episode(conn: sqlite3.Connection, title_id: int, season: int, number: int) -> bool:
    """Apply a click on an episode. Returns False if there is no such episode."""
    episode = get_episode(conn, title_id, season, number)
    if episode is None:
        return False
    if not episode["watched"]:
        _mark(conn, title_id, UP_TO_EPISODE, (season, season, number), True)
    elif not _mark(conn, title_id, AFTER_EPISODE, (season, season, number), False):
        _mark(conn, title_id, "season = ? AND number = ?", (season, number), False)
    return True


def mark_season(conn: sqlite3.Connection, title_id: int, season: int, watched: bool) -> None:
    _mark(conn, title_id, "season <= ?" if watched else "season = ?", (season,), watched)


def mark_through(conn: sqlite3.Connection, title_id: int, season: int, number: int) -> None:
    """Mark everything up to and including an episode, with no watch date."""
    conn.execute(
        f"UPDATE episodes SET watched = 1 WHERE title_id = ? AND {UP_TO_EPISODE}",
        (title_id, season, season, number),
    )
    sync_status(conn, title_id)


def watch_next(conn: sqlite3.Connection, title_id: int) -> None:
    episode = next_episode(conn, title_id)
    if episode:
        toggle_episode(conn, title_id, episode["season"], episode["number"])


def _releases(conn: sqlite3.Connection, start: date, end: date, latest: bool) -> list[dict]:
    """One row per title, with its latest or earliest release in the date range."""
    bounds = (start.isoformat(), end.isoformat())
    rows = conn.execute(RELEASES_QUERY, bounds * 2).fetchall()
    best: dict[int, sqlite3.Row] = {}
    for row in rows:
        current = best.get(row["title_id"])
        if current is None:
            best[row["title_id"]] = row
            continue
        day, best_day = row["release_date"], current["release_date"]
        if day > best_day if latest else day < best_day:
            best[row["title_id"]] = row
    ordered = sorted(best.values(), key=lambda row: row["release_date"], reverse=latest)
    return [dict(row) for row in ordered]


def upcoming(conn: sqlite3.Connection, today: date) -> list[dict]:
    end = today + timedelta(days=UPCOMING_DAYS)
    return _releases(conn, today + timedelta(days=1), end, latest=False)


def just_released(conn: sqlite3.Connection, today: date) -> list[dict]:
    return _releases(conn, today - timedelta(days=JUST_RELEASED_DAYS), today, latest=True)


def list_platforms(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.*, COUNT(tp.title_id) AS title_count"
        " FROM platforms p LEFT JOIN title_platforms tp ON tp.platform_id = p.id"
        " GROUP BY p.id ORDER BY p.name COLLATE NOCASE"
    ).fetchall()


def platforms_by_kind(conn: sqlite3.Connection) -> dict[PlatformKind, list[sqlite3.Row]]:
    platforms = list_platforms(conn)
    return {
        kind: [platform for platform in platforms if platform["kind"] == kind.value]
        for kind in PlatformKind
    }


def get_platform(conn: sqlite3.Connection, platform_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM platforms WHERE id = ?", (platform_id,)).fetchone()


def create_platform(conn: sqlite3.Connection, data: PlatformInput) -> int:
    try:
        platform_id = conn.execute(
            "INSERT INTO platforms (name, url, kind, release_year) VALUES (?, ?, ?, ?)",
            (data.name, data.url, data.kind.value, data.release_year),
        ).lastrowid
    except sqlite3.IntegrityError as error:
        raise DuplicatePlatformError(data.name) from error
    _forget_removed(conn, PLATFORM_ENTRY, data.name)
    return platform_id


def fill_platform(conn: sqlite3.Connection, data: PlatformInput) -> bool:
    """Add a platform, or fill in its blanks and apply its image. Returns whether it's new."""
    if is_removed(conn, PLATFORM_ENTRY, data.name):
        return False
    added = conn.execute(
        "INSERT OR IGNORE INTO platforms (name, url, kind, release_year, image)"
        " VALUES (?, ?, ?, ?, ?)",
        (data.name, data.url, data.kind.value, data.release_year, data.image),
    ).rowcount
    platform_id = conn.execute(
        "SELECT id FROM platforms WHERE name = ?", (data.name,)
    ).fetchone()["id"]
    rows = conn.execute(
        "SELECT field FROM cleared_platform_fields WHERE platform_id = ?", (platform_id,)
    )
    cleared = {row["field"] for row in rows}
    if "url" not in cleared:
        conn.execute(
            "UPDATE platforms SET url = ? WHERE id = ? AND url = ''", (data.url, platform_id)
        )
    if "release_year" not in cleared:
        conn.execute(
            "UPDATE platforms SET release_year = ? WHERE id = ? AND release_year IS NULL",
            (data.release_year, platform_id),
        )
    if data.image:
        conn.execute("UPDATE platforms SET image = ? WHERE name = ?", (data.image, data.name))
    return added > 0


def update_platform(conn: sqlite3.Connection, platform_id: int, data: PlatformInput) -> None:
    platform = get_platform(conn, platform_id)
    cleared = _emptied(platform, {"url": data.url, "release_year": data.release_year})
    try:
        conn.execute(
            "UPDATE platforms SET name = ?, url = ?, kind = ?, release_year = ? WHERE id = ?",
            (data.name, data.url, data.kind.value, data.release_year, platform_id),
        )
    except sqlite3.IntegrityError as error:
        raise DuplicatePlatformError(data.name) from error
    conn.executemany(
        "INSERT OR IGNORE INTO cleared_platform_fields (platform_id, field) VALUES (?, ?)",
        [(platform_id, field) for field in cleared],
    )
    if platform["name"].casefold() != data.name.casefold():
        _note_removed(conn, PLATFORM_ENTRY, platform["name"])
        _forget_removed(conn, PLATFORM_ENTRY, data.name)


def delete_platform(conn: sqlite3.Connection, platform_id: int) -> None:
    _note_removed(conn, PLATFORM_ENTRY, get_platform(conn, platform_id)["name"])
    conn.execute("DELETE FROM platforms WHERE id = ?", (platform_id,))


def title_platforms(conn: sqlite3.Connection, title_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.* FROM platforms p JOIN title_platforms tp ON tp.platform_id = p.id"
        " WHERE tp.title_id = ? ORDER BY p.name COLLATE NOCASE",
        (title_id,),
    ).fetchall()


def set_title_platforms(
    conn: sqlite3.Connection, title_id: int, platform_ids: list[int]
) -> None:
    if title_platforms(conn, title_id) and not platform_ids:
        _note_cleared(conn, title_id, ["platforms"])
    conn.execute("DELETE FROM title_platforms WHERE title_id = ?", (title_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO title_platforms (title_id, platform_id)"
        " SELECT ?, id FROM platforms WHERE id = ?",
        [(title_id, platform_id) for platform_id in platform_ids],
    )


def fill_title_platforms(conn: sqlite3.Connection, title_id: int, names: list[str]) -> bool:
    """Link platforms by name, creating new ones, unless the title already has some."""
    names = [name for name in names if not is_removed(conn, PLATFORM_ENTRY, name)]
    if (
        not names
        or title_platforms(conn, title_id)
        or "platforms" in _cleared_fields(conn, title_id)
    ):
        return False
    is_game = get_title(conn, title_id)["kind"] == Kind.GAME.value
    kind = PlatformKind.CONSOLE if is_game else PlatformKind.STREAMING
    ids = []
    for name in names:
        conn.execute(
            "INSERT OR IGNORE INTO platforms (name, kind) VALUES (?, ?)", (name, kind.value)
        )
        ids.append(conn.execute("SELECT id FROM platforms WHERE name = ?", (name,)).fetchone()[0])
    set_title_platforms(conn, title_id, ids)
    return True


def list_plans(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.*, COUNT(i.id) AS item_count"
        " FROM plans p LEFT JOIN plan_items i ON i.plan_id = p.id"
        " GROUP BY p.id ORDER BY p.name COLLATE NOCASE"
    ).fetchall()


def get_plan(conn: sqlite3.Connection, plan_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()


def create_plan(conn: sqlite3.Connection, data: PlanInput) -> int:
    try:
        return conn.execute(
            "INSERT INTO plans (name, description) VALUES (?, ?)", (data.name, data.description)
        ).lastrowid
    except sqlite3.IntegrityError as error:
        raise DuplicatePlanError(data.name) from error


def update_plan(conn: sqlite3.Connection, plan_id: int, data: PlanInput) -> None:
    try:
        conn.execute(
            "UPDATE plans SET name = ?, description = ? WHERE id = ?",
            (data.name, data.description, plan_id),
        )
    except sqlite3.IntegrityError as error:
        raise DuplicatePlanError(data.name) from error


def delete_plan(conn: sqlite3.Connection, plan_id: int) -> None:
    conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))


def plan_titles(conn: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
    """The plan's titles, in the order they were added."""
    return conn.execute(
        f"{TITLE_SELECT} JOIN plan_items i ON i.title_id = t.id"
        " WHERE i.plan_id = ? GROUP BY t.id ORDER BY i.id",
        (plan_id,),
    ).fetchall()


def titles_outside_plan(conn: sqlite3.Connection, plan_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, name, kind, year FROM titles"
        " WHERE id NOT IN (SELECT title_id FROM plan_items WHERE plan_id = ?)"
        " ORDER BY name COLLATE NOCASE",
        (plan_id,),
    ).fetchall()


def add_to_plan(conn: sqlite3.Connection, plan_id: int, title_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO plan_items (plan_id, title_id) VALUES (?, ?)", (plan_id, title_id)
    )


def remove_from_plan(conn: sqlite3.Connection, plan_id: int, title_id: int) -> None:
    conn.execute(
        "DELETE FROM plan_items WHERE plan_id = ? AND title_id = ?", (plan_id, title_id)
    )


def plans_for_title(conn: sqlite3.Connection, title_id: int) -> list[sqlite3.Row]:
    """Every plan, saying whether it includes this title."""
    return conn.execute(
        "SELECT p.id, p.name, EXISTS ("
        "SELECT 1 FROM plan_items i WHERE i.plan_id = p.id AND i.title_id = ?"
        ") AS included FROM plans p ORDER BY p.name COLLATE NOCASE",
        (title_id,),
    ).fetchall()


def get_duo_file(conn: sqlite3.Connection) -> bytes | None:
    row = conn.execute("SELECT content FROM duo_file").fetchone()
    return row["content"] if row else None


def set_duo_file(conn: sqlite3.Connection, content: bytes) -> None:
    conn.execute(
        "INSERT INTO duo_file (id, content) VALUES (1, ?)"
        " ON CONFLICT (id) DO UPDATE SET content = excluded.content",
        (content,),
    )


def clear_duo_file(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM duo_file")


def planned_count(conn: sqlite3.Connection) -> int:
    return len(list_titles(conn, status=Status.PLANNED))


def favorite_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM titles WHERE favorite = 1").fetchone()[0]


def stats(conn: sqlite3.Connection) -> dict[str, int | float | None]:
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(kind = 'movie' AND status = 'completed'), 0) AS movies_watched,
            COALESCE(SUM(kind = 'series' AND status = 'completed'), 0) AS series_completed,
            COALESCE(SUM(kind = 'game' AND status = 'completed'), 0) AS games_finished,
            ROUND(AVG(rating), 1) AS average_rating,
            (SELECT COUNT(*) FROM episodes WHERE watched = 1) AS episodes_watched
        FROM titles
        """
    ).fetchone()
    return dict(row)
