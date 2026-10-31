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
from datetime import date
from itertools import zip_longest

from plasma.models import Details, Kind, Status, TitleInput

TITLE_SELECT = """
    SELECT t.*, COUNT(e.id) AS episodes_total, COALESCE(SUM(e.watched), 0) AS episodes_watched
    FROM titles t LEFT JOIN episodes e ON e.title_id = t.id
"""
UP_TO_EPISODE = "(season < ? OR (season = ? AND number <= ?))"
AFTER_EPISODE = "(season > ? OR (season = ? AND number > ?))"


class DuplicateTitleError(Exception):
    pass


def _today() -> str:
    return date.today().isoformat()


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
            " (kind, name, year, status, rating, duration, description, notes, watched_on)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data.kind.value,
                data.name,
                data.year,
                data.status.value,
                data.rating,
                data.duration,
                data.description,
                data.notes,
                data.watched_on.isoformat() if data.watched_on else None,
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
            " description = ?, notes = ?, watched_on = ?, updated_at = datetime('now')"
            " WHERE id = ?",
            (
                data.name,
                data.year,
                data.status.value,
                data.rating,
                data.duration,
                data.description,
                data.notes,
                data.watched_on.isoformat() if data.watched_on else None,
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


def fill_blanks(conn: sqlite3.Connection, title_id: int, **values: int | str | None) -> bool:
    """Set the given columns only where the title has no value. Returns True if any changed."""
    title = get_title(conn, title_id)
    changes = {
        column: value
        for column, value in values.items()
        if value not in (None, "") and title[column] in (None, "")
    }
    if changes:
        assignments = ", ".join(f"{column} = ?" for column in changes)
        conn.execute(
            f"UPDATE titles SET {assignments} WHERE id = ?", (*changes.values(), title_id)
        )
    return bool(changes)


def set_image(conn: sqlite3.Connection, title_id: int, image: str) -> None:
    conn.execute("UPDATE titles SET image = ? WHERE id = ?", (image, title_id))


def toggle_favorite(conn: sqlite3.Connection, title_id: int) -> None:
    conn.execute("UPDATE titles SET favorite = NOT favorite WHERE id = ?", (title_id,))


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


def update_episode(
    conn: sqlite3.Connection, title_id: int, season: int, number: int, data: Details
) -> None:
    conn.execute(
        "UPDATE episodes SET duration = ?, description = ?"
        " WHERE title_id = ? AND season = ? AND number = ?",
        (data.duration, data.description, title_id, season, number),
    )


def fill_episode_blanks(
    conn: sqlite3.Connection, title_id: int, season: int, number: int, data: Details
) -> bool:
    """Set an episode's details only where it has none. Returns True if any changed."""
    cursor = conn.execute(
        "UPDATE episodes SET duration = COALESCE(duration, ?),"
        " description = CASE WHEN description = '' THEN ? ELSE description END"
        " WHERE title_id = ? AND season = ? AND number = ?"
        " AND ((duration IS NULL AND ? IS NOT NULL) OR (description = '' AND ? != ''))",
        (
            data.duration,
            data.description,
            title_id,
            season,
            number,
            data.duration,
            data.description,
        ),
    )
    return cursor.rowcount > 0


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
    if title["kind"] == Kind.MOVIE.value and status is Status.COMPLETED:
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
    episode = conn.execute(
        "SELECT watched FROM episodes WHERE title_id = ? AND season = ? AND number = ?",
        (title_id, season, number),
    ).fetchone()
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


def stats(conn: sqlite3.Connection) -> dict[str, int | float | None]:
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(kind = 'movie' AND status = 'completed'), 0) AS movies_watched,
            COALESCE(SUM(kind = 'series' AND status = 'completed'), 0) AS series_completed,
            COALESCE(SUM(status = 'planned'), 0) AS planned,
            ROUND(AVG(rating), 1) AS average_rating,
            (SELECT COUNT(*) FROM episodes WHERE watched = 1) AS episodes_watched
        FROM titles
        """
    ).fetchone()
    return dict(row)
