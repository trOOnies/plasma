"""Library queries and updates.

Nothing here commits: callers wrap their work in `with conn:` so a request or a
whole import is one transaction.

A title starts with no status, and planned is only ever the user's choice. Once
episodes are watched, a series' status follows them, except that "dropped" sticks
until the user changes it. Series without episode data keep whatever status was set.

Marking an episode or season watched also marks everything before it. Clicking a
watched episode moves progress back to it, or unmarks it if it's the last one watched.
"""

import sqlite3
from datetime import date, timedelta
from itertools import zip_longest

from plasma.models import Details, Kind, PlanInput, PlatformInput, Status, TitleInput

TITLE_SELECT = """
    SELECT t.*, COUNT(e.id) AS episodes_total, COALESCE(SUM(e.watched), 0) AS episodes_watched
    FROM titles t LEFT JOIN episodes e ON e.title_id = t.id
"""
UP_TO_EPISODE = "(season < ? OR (season = ? AND number <= ?))"
AFTER_EPISODE = "(season > ? OR (season = ? AND number > ?))"
BEFORE_EPISODE = "(season < ? OR (season = ? AND number < ?))"

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
    owned: bool = False,
) -> list[sqlite3.Row]:
    clauses, params = [], []
    if kind:
        clauses.append("t.kind = ?")
        params.append(kind.value)
    if status:
        clauses.append("t.status = ?")
        params.append(status.value)
    if favorite:
        clauses.append("t.favorite = 1")
    if owned:
        clauses.append("t.owned = 1")
    if platform_id:
        clauses.append("t.id IN (SELECT title_id FROM title_platforms WHERE platform_id = ?)")
        params.append(platform_id)
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("t.name LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(f"{TITLE_SELECT}{where} GROUP BY t.id ORDER BY {order}", params).fetchall()


def create_title(conn: sqlite3.Connection, data: TitleInput) -> int:
    try:
        title_id = conn.execute(
            "INSERT INTO titles"
            " (kind, name, year, status, rating, duration, release_date, hours_played,"
            " producer, description, notes, watched_on)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data.kind.value,
                data.name,
                data.year,
                data.status.value,
                data.rating,
                data.duration,
                iso_date(data.release_date),
                data.hours_played,
                data.producer,
                data.description,
                data.notes,
                iso_date(data.watched_on),
            ),
        ).lastrowid
    except sqlite3.IntegrityError as error:
        raise DuplicateTitleError(data.name) from error
    _set_seasons(conn, title_id, data.seasons)
    if data.status is Status.COMPLETED:
        conn.execute("UPDATE episodes SET watched = 1 WHERE title_id = ?", (title_id,))
    sync_status(conn, title_id)
    return title_id


def update_title(conn: sqlite3.Connection, title_id: int, data: TitleInput) -> None:
    previous = get_title(conn, title_id)["status"]
    try:
        conn.execute(
            "UPDATE titles SET name = ?, year = ?, status = ?, rating = ?, duration = ?,"
            " release_date = ?, hours_played = ?, producer = ?, description = ?, notes = ?,"
            " watched_on = ?,"
            " updated_at = datetime('now')"
            " WHERE id = ?",
            (
                data.name,
                data.year,
                data.status.value,
                data.rating,
                data.duration,
                iso_date(data.release_date),
                data.hours_played,
                data.producer,
                data.description,
                data.notes,
                iso_date(data.watched_on),
                title_id,
            ),
        )
    except sqlite3.IntegrityError as error:
        raise DuplicateTitleError(data.name) from error
    _set_seasons(conn, title_id, data.seasons)
    # Only a change to completed marks everything watched. A form re-submitted as
    # completed with a new season added must leave that season unwatched.
    if data.status is Status.COMPLETED and previous != Status.COMPLETED.value:
        conn.execute("UPDATE episodes SET watched = 1 WHERE title_id = ?", (title_id,))
    sync_status(conn, title_id)


def delete_title(conn: sqlite3.Connection, title_id: int) -> None:
    conn.execute("DELETE FROM titles WHERE id = ?", (title_id,))


def _fill_blanks(
    conn: sqlite3.Connection, table: str, row: sqlite3.Row, values: dict[str, int | str | None]
) -> bool:
    changes = {
        column: value
        for column, value in values.items()
        if value not in (None, "") and row[column] in (None, "")
    }
    if changes:
        assignments = ", ".join(f"{column} = ?" for column in changes)
        conn.execute(
            f"UPDATE {table} SET {assignments} WHERE id = ?", (*changes.values(), row["id"])
        )
    return bool(changes)


def fill_blanks(conn: sqlite3.Connection, title_id: int, **values: int | str | None) -> bool:
    """Set the given columns only where the title has no value. Returns True if any changed."""
    return _fill_blanks(conn, "titles", get_title(conn, title_id), values)


def set_image(conn: sqlite3.Connection, title_id: int, image: str) -> None:
    conn.execute("UPDATE titles SET image = ? WHERE id = ?", (image, title_id))


def toggle_favorite(conn: sqlite3.Connection, title_id: int) -> None:
    conn.execute("UPDATE titles SET favorite = NOT favorite WHERE id = ?", (title_id,))


def toggle_owned(conn: sqlite3.Connection, title_id: int) -> None:
    conn.execute("UPDATE titles SET owned = NOT owned WHERE id = ?", (title_id,))


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
    """Grow the episode list to at least `counts`. Returns True if anything was added."""
    current = season_counts(conn, title_id)
    merged = [max(pair) for pair in zip_longest(current, counts, fillvalue=0)]
    if merged == current:
        return False
    _set_seasons(conn, title_id, merged)
    sync_status(conn, title_id)
    return True


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
    }
    return _fill_blanks(conn, "episodes", get_episode(conn, title_id, season, number), values)


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


def get_platform(conn: sqlite3.Connection, platform_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM platforms WHERE id = ?", (platform_id,)).fetchone()


def create_platform(conn: sqlite3.Connection, data: PlatformInput) -> int:
    try:
        return conn.execute(
            "INSERT INTO platforms (name, url) VALUES (?, ?)", (data.name, data.url)
        ).lastrowid
    except sqlite3.IntegrityError as error:
        raise DuplicatePlatformError(data.name) from error


def update_platform(conn: sqlite3.Connection, platform_id: int, data: PlatformInput) -> None:
    try:
        conn.execute(
            "UPDATE platforms SET name = ?, url = ? WHERE id = ?",
            (data.name, data.url, platform_id),
        )
    except sqlite3.IntegrityError as error:
        raise DuplicatePlatformError(data.name) from error


def delete_platform(conn: sqlite3.Connection, platform_id: int) -> None:
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
    conn.execute("DELETE FROM title_platforms WHERE title_id = ?", (title_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO title_platforms (title_id, platform_id)"
        " SELECT ?, id FROM platforms WHERE id = ?",
        [(title_id, platform_id) for platform_id in platform_ids],
    )


def fill_title_platforms(conn: sqlite3.Connection, title_id: int, names: list[str]) -> bool:
    """Link platforms by name, creating new ones, unless the title already has some."""
    if not names or title_platforms(conn, title_id):
        return False
    ids = []
    for name in names:
        conn.execute("INSERT OR IGNORE INTO platforms (name) VALUES (?)", (name,))
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


def favorite_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM titles WHERE favorite = 1").fetchone()[0]


def stats(conn: sqlite3.Connection) -> dict[str, int | float | None]:
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(kind = 'movie' AND status = 'completed'), 0) AS movies_watched,
            COALESCE(SUM(kind = 'series' AND status = 'completed'), 0) AS series_completed,
            COALESCE(SUM(kind = 'game' AND status = 'completed'), 0) AS games_finished,
            COALESCE(SUM(status = 'planned'), 0) AS planned,
            ROUND(AVG(rating), 1) AS average_rating,
            (SELECT COUNT(*) FROM episodes WHERE watched = 1) AS episodes_watched
        FROM titles
        """
    ).fetchone()
    return dict(row)
