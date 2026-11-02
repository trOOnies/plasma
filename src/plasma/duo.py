"""Suggestions for two people, from this library and the other person's watch data file.

Nothing here writes to the library. A title can appear in several sections. A title only
the other person has can only appear where it doesn't need this library, with no title_id.

Dropped counts for nothing here, the same as no status. People drop things for all sorts
of reasons, often just for a while, so it doesn't rule a title in or out.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from plasma import library
from plasma.models import Kind, MultiplayerStatus, Ownership, Status, WatchEntry, WatchFile

LOVED_RATING = 4
SEEN_STATUSES = (Status.WATCHING.value, Status.COMPLETED.value)


@dataclass(frozen=True)
class Side:
    """What one person's data says about a title."""

    status: str = Status.NONE.value
    multiplayer_status: str = MultiplayerStatus.NONE.value
    rating: int | None = None
    favorite: bool = False
    ownership: str = Ownership.NONE.value
    multiplayer: bool = False

    @property
    def planned(self) -> bool:
        planned = Status.PLANNED.value
        return planned in (self.status, self.multiplayer_status)

    @property
    def seen(self) -> bool:
        return (
            self.rating is not None
            or self.status in SEEN_STATUSES
            or self.multiplayer_status in SEEN_STATUSES
        )

    @property
    def loved(self) -> bool:
        return (self.rating or 0) >= LOVED_RATING

    @property
    def owned(self) -> bool:
        return self.ownership == Ownership.OWNED.value

    @property
    def wanted(self) -> bool:
        return self.ownership == Ownership.WANTED.value


@dataclass(frozen=True)
class Pair:
    """One title, with both people's data. Whoever doesn't have it gets a blank Side."""

    kind: str
    name: str
    year: int | None
    mine: Side
    theirs: Side
    title_id: int | None = None
    entry: WatchEntry | None = None

    @property
    def is_game(self) -> bool:
        return self.kind == Kind.GAME.value

    @property
    def is_multiplayer(self) -> bool:
        return self.is_game and (self.mine.multiplayer or self.theirs.multiplayer)


@dataclass(frozen=True)
class Item:
    pair: Pair
    note: str


@dataclass(frozen=True)
class Section:
    heading: str
    blurb: str
    items: list[Item]


def _my_side(title: sqlite3.Row) -> Side:
    return Side(
        status=title["status"],
        multiplayer_status=title["multiplayer_status"],
        rating=title["rating"],
        favorite=bool(title["favorite"]),
        ownership=title["ownership"],
        multiplayer=bool(title["multiplayer"]),
    )


def _their_side(entry: WatchEntry) -> Side:
    return Side(
        status=entry.status.value,
        multiplayer_status=entry.multiplayer_status.value,
        rating=entry.rating,
        favorite=entry.favorite,
        ownership=entry.ownership.value,
        multiplayer=bool(entry.multiplayer),
    )


def _same_year(mine: int | None, theirs: int | None) -> bool:
    return mine == theirs


def _either_year_missing(mine: int | None, theirs: int | None) -> bool:
    return mine is None or theirs is None


def _match(titles: list[sqlite3.Row], entries: list[WatchEntry]) -> dict[int, WatchEntry]:
    """Pair titles with entries by id, trying equal years before a year missing on one side."""
    left: dict[tuple[str, str], list[WatchEntry]] = {}
    for entry in entries:
        left.setdefault((entry.kind.value, entry.name.casefold()), []).append(entry)
    matches: dict[int, WatchEntry] = {}
    for years_fit in (_same_year, _either_year_missing):
        for title in titles:
            candidates = left.get((title["kind"], title["name"].casefold()), [])
            found = next((e for e in candidates if years_fit(title["year"], e.year)), None)
            if found and title["id"] not in matches:
                matches[title["id"]] = found
                candidates.remove(found)
    return matches


def _pairs(conn: sqlite3.Connection, file: WatchFile) -> list[Pair]:
    titles = library.list_titles(conn)
    matches = _match(titles, file.titles)
    pairs = []
    for title in titles:
        entry = matches.get(title["id"])
        theirs = _their_side(entry) if entry else Side()
        pairs.append(
            Pair(
                title["kind"],
                title["name"],
                title["year"],
                _my_side(title),
                theirs,
                title["id"],
                entry,
            )
        )
    matched = {id(entry) for entry in matches.values()}
    for entry in file.titles:
        if id(entry) not in matched:
            kind, theirs = entry.kind.value, _their_side(entry)
            pairs.append(Pair(kind, entry.name, entry.year, Side(), theirs, None, entry))
    return pairs


def _rated(side: Side, who: str) -> list[str]:
    return [f"{who} ★ {side.rating}"] if side.rating else []


def _ratings_note(_: sqlite3.Connection, pair: Pair) -> str:
    return " · ".join([*_rated(pair.mine, "You"), *_rated(pair.theirs, "Them")])


def _code(season: int, number: int) -> str:
    return f"S{season:02}E{number:02}"


def _progress_note(conn: sqlite3.Connection, pair: Pair) -> str:
    if pair.kind != Kind.SERIES.value:
        return ""
    parts = []
    mine = library.next_episode(conn, pair.title_id)
    theirs = pair.entry.next_episode()
    if mine:
        parts.append(f"You're up to {_code(mine['season'], mine['number'])}")
    if theirs:
        parts.append(f"They're up to {_code(*theirs)}")
    return " · ".join(parts)


def _they_loved_note(_: sqlite3.Connection, pair: Pair) -> str:
    parts = _rated(pair.theirs, "Them")
    if pair.title_id is None:
        parts.append("Not in your library")
    elif pair.mine.planned:
        parts.append("On your Planned list")
    return " · ".join(parts)


def _you_loved_note(_: sqlite3.Connection, pair: Pair) -> str:
    parts = _rated(pair.mine, "You")
    if pair.theirs.planned:
        parts.append("On their Planned list")
    return " · ".join(parts)


def _both_in_progress(pair: Pair) -> bool:
    watching = Status.WATCHING.value
    return pair.mine.status == pair.theirs.status == watching or (
        pair.mine.multiplayer_status == pair.theirs.multiplayer_status == watching
    )


def _rewatch(pair: Pair) -> bool:
    completed = Status.COMPLETED.value
    ratings = (pair.mine.rating, pair.theirs.rating)
    return (
        not pair.is_game
        and pair.mine.status == pair.theirs.status == completed
        and all(rating is None or rating >= LOVED_RATING for rating in ratings)
    )


def _fits_two(pair: Pair) -> bool:
    return not pair.is_game or pair.is_multiplayer


def _they_loved(pair: Pair) -> bool:
    return pair.theirs.loved and not pair.mine.seen and _fits_two(pair)


def _you_loved(pair: Pair) -> bool:
    return pair.mine.loved and not pair.theirs.seen and _fits_two(pair)


def _both_own_multiplayer(pair: Pair) -> bool:
    return pair.mine.owned and pair.theirs.owned and pair.is_multiplayer


def _both_own_other(pair: Pair) -> bool:
    return pair.mine.owned and pair.theirs.owned and not _both_own_multiplayer(pair)


SECTIONS: tuple[tuple[str, str, Callable[[Pair], bool], Callable], ...] = (
    (
        "Favorites of you both",
        "You've both given these a heart.",
        lambda pair: pair.mine.favorite and pair.theirs.favorite,
        _ratings_note,
    ),
    (
        "Planned by you both",
        "On both of your lists.",
        lambda pair: pair.mine.planned and pair.theirs.planned,
        _ratings_note,
    ),
    (
        "In progress for you both",
        "You're both partway through these.",
        _both_in_progress,
        _progress_note,
    ),
    (
        "Rewatch together",
        "You've both finished these, and neither of you rated them under 4 stars.",
        _rewatch,
        _ratings_note,
    ),
    (
        "They loved, you haven't seen",
        "Rated 4 or 5 stars by them, and new to you.",
        _they_loved,
        _they_loved_note,
    ),
    (
        "You loved, they haven't seen",
        "Rated 4 or 5 stars by you, and new to them.",
        _you_loved,
        _you_loved_note,
    ),
    (
        "Multiplayer games you both own",
        "Ready to play together.",
        _both_own_multiplayer,
        _ratings_note,
    ),
    ("Other games you both own", "Owned by you both.", _both_own_other, _ratings_note),
    (
        "Games you both want",
        "Neither of you has these yet.",
        lambda pair: pair.mine.wanted and pair.theirs.wanted,
        _ratings_note,
    ),
)


def _best_first(pair: Pair) -> tuple[int, str]:
    return -((pair.mine.rating or 0) + (pair.theirs.rating or 0)), pair.name.casefold()


def suggestions(conn: sqlite3.Connection, file: WatchFile) -> list[Section]:
    """The sections that have anything in them, best rated titles first."""
    pairs = sorted(_pairs(conn, file), key=_best_first)
    sections = []
    for heading, blurb, belongs, note in SECTIONS:
        items = [Item(pair, note(conn, pair)) for pair in pairs if belongs(pair)]
        if items:
            sections.append(Section(heading, blurb, items))
    return sections
