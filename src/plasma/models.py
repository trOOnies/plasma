"""Validated input shared by the web forms and the info/ importer."""

import re
from datetime import date
from enum import Enum
from pathlib import PurePosixPath
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

MAX_SEASONS = 99
MAX_EPISODES_PER_SEASON = 999
MAX_DURATION_MINUTES = 6000
MAX_HOURS_PLAYED = 100_000
MAX_RATING = 5
FAVORITES_PLAN_NAME = "Favorites"
PLANNED_PLAN_NAME = "Planned"
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
SCREEN_GENRES = (
    "Action",
    "Adventure",
    "Animation",
    "Anime",
    "Comedy",
    "Crime",
    "Documentary",
    "Drama",
    "Family",
    "Fantasy",
    "Historical",
    "Horror",
    "Musical",
    "Mystery",
    "Reality",
    "Romance",
    "Sci-Fi",
    "Thriller",
    "War",
    "Western",
)
GAME_GENRES = (
    "Action",
    "Adventure",
    "Battle Royale",
    "Card & Board",
    "Fighting",
    "Horror",
    "Metroidvania",
    "MOBA",
    "Platformer",
    "Puzzle",
    "Racing",
    "Roguelike",
    "RPG",
    "Sandbox",
    "Shooter",
    "Simulation",
    "Sports",
    "Stealth",
    "Strategy",
    "Survival",
)
# Anything that isn't a console or PC game store counts as streaming.
CONSOLE_NAME = re.compile(
    r"playstation|\bps[1-5]\b|xbox|nintendo|\bwii|switch|game ?boy|game ?cube|\bsega\b"
    r"|steam|\bgog\b|epic games|\bpc\b",
    re.IGNORECASE,
)


def describe_errors(error: ValidationError) -> list[str]:
    """Readable one-line messages, such as "rating: Input should be less than or equal to 5"."""
    messages = []
    for item in error.errors():
        field = ".".join(str(part) for part in item["loc"])
        message = item["msg"].removeprefix("Value error, ")
        messages.append(f"{field}: {message}" if field else message)
    return messages


class Kind(str, Enum):
    MOVIE = "movie"
    SERIES = "series"
    GAME = "game"


class Status(str, Enum):
    NONE = "none"
    PLANNED = "planned"
    WATCHING = "watching"
    COMPLETED = "completed"
    DROPPED = "dropped"


class MultiplayerStatus(str, Enum):
    """Multiplayer has no ending, so unlike Status it can't be completed."""

    NONE = "none"
    PLANNED = "planned"
    WATCHING = "watching"
    DROPPED = "dropped"


class Ownership(str, Enum):
    NONE = "none"
    WANTED = "wanted"
    OWNED = "owned"

    @property
    def label(self) -> str:
        return "Neither" if self is Ownership.NONE else self.value.capitalize()


class AgeRestriction(str, Enum):
    ALL = "all"
    AGE_7 = "7+"
    AGE_13 = "13+"
    AGE_16 = "16+"
    AGE_18 = "18+"

    @property
    def label(self) -> str:
        return "All ages" if self is AgeRestriction.ALL else self.value


class PlatformKind(str, Enum):
    STREAMING = "streaming"
    CONSOLE = "console"

    @property
    def label(self) -> str:
        return "Streaming Platforms" if self is PlatformKind.STREAMING else "Game Consoles"


GENRES = {Kind.MOVIE: SCREEN_GENRES, Kind.SERIES: SCREEN_GENRES, Kind.GAME: GAME_GENRES}


def guess_platform_kind(name: str) -> PlatformKind:
    return PlatformKind.CONSOLE if CONSOLE_NAME.search(name) else PlatformKind.STREAMING


def check_image_name(value: str | None) -> str | None:
    if value is None:
        return None
    path = PurePosixPath(value)
    if path.name != value or "\\" in value:
        raise ValueError("use a file name from the matching info/images folder, without any path")
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(f"use one of these image types: {', '.join(IMAGE_SUFFIXES)}")
    return value


def parse_episode_code(value: Any) -> tuple[int, int]:
    match = re.fullmatch(r"S(\d+)E(\d+)", str(value).strip(), re.IGNORECASE)
    if not match:
        raise ValueError("use an episode code such as S02E05")
    return int(match[1]), int(match[2])


class Details(BaseModel):
    """What titles and episodes share. The duration is in minutes, the rating in stars."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    duration: int | None = Field(default=None, ge=1, le=MAX_DURATION_MINUTES)
    release_date: date | None = None
    description: str = ""
    rating: int | None = Field(default=None, ge=1, le=MAX_RATING)

    @field_validator("duration", "release_date", "rating", mode="before")
    @classmethod
    def blank_detail_is_none(cls, value: Any) -> Any:
        return None if value == "" else value

    @field_validator("description", mode="before")
    @classmethod
    def none_description_is_blank(cls, value: Any) -> Any:
        return "" if value is None else value


class TitleInput(Details):
    # Numbers become strings so a YAML `name: 1917` is the film, not an error.
    model_config = ConfigDict(coerce_numbers_to_str=True)

    kind: Kind
    name: str = Field(min_length=1, max_length=200)
    year: int | None = Field(default=None, ge=1870, le=2100)
    status: Status = Status.NONE
    # A game's status above is its singleplayer status.
    singleplayer: bool = True
    # None means nothing has said either way yet, so a re-import may still set it.
    multiplayer: bool | None = None
    multiplayer_status: MultiplayerStatus = MultiplayerStatus.NONE
    age_restriction: AgeRestriction | None = None
    genres: list[Annotated[str, Field(min_length=1, max_length=50)]] = Field(default_factory=list)
    hours_played: int | None = Field(default=None, ge=0, le=MAX_HOURS_PLAYED)
    producer: str = Field(default="", max_length=200)
    franchise: str = Field(default="", max_length=200)
    notes: str = ""
    watched_on: date | None = None
    seasons: list[int] = Field(default_factory=list, max_length=MAX_SEASONS)

    @field_validator("year", "hours_played", "watched_on", mode="before")
    @classmethod
    def blank_is_none(cls, value: Any) -> Any:
        return None if value == "" else value

    @field_validator("producer", "franchise", "notes", mode="before")
    @classmethod
    def none_is_blank(cls, value: Any) -> Any:
        return "" if value is None else value

    @field_validator("singleplayer", "multiplayer", mode="before")
    @classmethod
    def blank_is_false(cls, value: Any) -> Any:
        """An unticked checkbox submits nothing, which the web form passes on as blank."""
        return False if value == "" else value

    @field_validator("age_restriction", mode="before")
    @classmethod
    def parse_age_restriction(cls, value: Any) -> Any:
        """Accept the level itself, or a bare age such as 13 from YAML."""
        if value in ("", None) or isinstance(value, AgeRestriction):
            return value or None
        text = str(value).strip().lower()
        return f"{text}+" if text.isdecimal() else text

    @field_validator("genres", mode="before")
    @classmethod
    def single_genre_is_a_list(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @field_validator("seasons", mode="before")
    @classmethod
    def parse_seasons(cls, value: Any) -> Any:
        """Accept a YAML list or a bare count."""
        return [value] if isinstance(value, int) and not isinstance(value, bool) else value

    @field_validator("seasons")
    @classmethod
    def check_season_sizes(cls, value: list[int]) -> list[int]:
        if any(not 1 <= count <= MAX_EPISODES_PER_SEASON for count in value):
            raise ValueError(f"each season needs 1 to {MAX_EPISODES_PER_SEASON} episodes")
        return value

    @model_validator(mode="after")
    def check_kind_fields(self) -> "TitleInput":
        if self.kind is not Kind.SERIES and self.seasons:
            raise ValueError("seasons only apply to series")
        if self.kind is not Kind.GAME and self.hours_played is not None:
            raise ValueError("hours_played only applies to games")
        if self.kind is Kind.GAME and self.duration:
            raise ValueError("duration doesn't apply to games, so use hours_played")
        if self.kind is Kind.SERIES and self.watched_on:
            raise ValueError("watched_on doesn't apply to series")
        if self.kind is Kind.SERIES and self.duration:
            raise ValueError("duration only applies to movies, so set it on each episode")
        self._check_game_modes()
        self._check_genres()
        return self

    def _check_game_modes(self) -> None:
        if self.kind is not Kind.GAME:
            none = MultiplayerStatus.NONE
            if not self.singleplayer or self.multiplayer or self.multiplayer_status is not none:
                raise ValueError("singleplayer and multiplayer only apply to games")
            return
        if not self.singleplayer and not self.multiplayer:
            raise ValueError("a game needs singleplayer, multiplayer or both")
        if not self.singleplayer and (self.status is not Status.NONE or self.watched_on):
            raise ValueError("status and watched_on need singleplayer")
        if not self.multiplayer and self.multiplayer_status is not MultiplayerStatus.NONE:
            raise ValueError("multiplayer_status needs multiplayer")

    def _check_genres(self) -> None:
        known = {genre.casefold(): genre for genre in GENRES[self.kind]}
        unknown = [genre for genre in self.genres if genre.casefold() not in known]
        if unknown:
            allowed = ", ".join(GENRES[self.kind])
            raise ValueError(f"unknown genre {unknown[0]}, use one of: {allowed}")
        self.genres = list(dict.fromkeys(known[genre.casefold()] for genre in self.genres))


class SeasonInput(BaseModel):
    episodes: int = Field(ge=1, le=MAX_EPISODES_PER_SEASON)


class PlatformInput(BaseModel):
    # Numbers become strings so a YAML `name: 1917` works, as it does for titles.
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, coerce_numbers_to_str=True
    )

    name: str = Field(min_length=1, max_length=100)
    url: str = ""
    kind: PlatformKind = PlatformKind.STREAMING
    release_year: int | None = Field(default=None, ge=1950, le=2100)
    image: str | None = None

    _check_image = field_validator("image")(check_image_name)

    @field_validator("release_year", mode="before")
    @classmethod
    def blank_year_is_none(cls, value: Any) -> Any:
        return None if value == "" else value

    @model_validator(mode="after")
    def check_release_year(self) -> "PlatformInput":
        if self.release_year and self.kind is not PlatformKind.CONSOLE:
            raise ValueError("release_year only applies to game consoles")
        return self

    @field_validator("url")
    @classmethod
    def check_url(cls, value: str) -> str:
        # Shown as a link, so anything but http(s), such as javascript:, must stay out.
        if value and not re.fullmatch(r"https?://\S+", value, re.IGNORECASE):
            raise ValueError("use a full address starting with https://")
        return value


class PlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    description: str = ""

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        for reserved in (FAVORITES_PLAN_NAME, PLANNED_PLAN_NAME):
            if value.casefold() == reserved.casefold():
                raise ValueError(f"{reserved} is reserved for a built-in plan")
        return value


class ImportEntry(TitleInput):
    """A title in an info/ file, which can also say how far the user has got."""

    watched_through: tuple[int, int] | None = None
    image: str | None = None
    ownership: Ownership = Ownership.NONE
    episodes: dict[tuple[int, int], Details] = Field(default_factory=dict)
    platforms: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list
    )

    @field_validator("watched_through", mode="before")
    @classmethod
    def parse_episode_code(cls, value: Any) -> Any:
        return None if value is None else parse_episode_code(value)

    @field_validator("status", "multiplayer_status", mode="before")
    @classmethod
    def playing_means_watching(cls, value: Any) -> Any:
        return "watching" if value == "playing" else value

    @field_validator("platforms", mode="before")
    @classmethod
    def single_platform_is_a_list(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @field_validator("episodes", mode="before")
    @classmethod
    def parse_episode_keys(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {parse_episode_code(code): details for code, details in value.items()}

    _check_image = field_validator("image")(check_image_name)

    @model_validator(mode="after")
    def check_episodes_are_listed(self) -> "ImportEntry":
        if self.ownership is not Ownership.NONE and self.kind is not Kind.GAME:
            raise ValueError("ownership only applies to games")
        if self.watched_through and not self._is_listed(*self.watched_through):
            raise ValueError("watched_through is not an episode listed in seasons")
        for season, number in self.episodes:
            if not self._is_listed(season, number):
                raise ValueError(f"episodes: S{season:02}E{number:02} is not listed in seasons")
        return self

    def _is_listed(self, season: int, number: int) -> bool:
        return 1 <= season <= len(self.seasons) and 1 <= number <= self.seasons[season - 1]
