"""Validated input shared by the web forms and the info/ importer."""

import re
from datetime import date
from enum import Enum
from typing import Any

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


def describe_errors(error: ValidationError) -> list[str]:
    """Readable one-line messages, such as "rating: Input should be less than or equal to 10"."""
    messages = []
    for item in error.errors():
        field = ".".join(str(part) for part in item["loc"])
        message = item["msg"].removeprefix("Value error, ")
        messages.append(f"{field}: {message}" if field else message)
    return messages


class Kind(str, Enum):
    MOVIE = "movie"
    SERIES = "series"


class Status(str, Enum):
    PLANNED = "planned"
    WATCHING = "watching"
    COMPLETED = "completed"
    DROPPED = "dropped"


class TitleInput(BaseModel):
    # Numbers become strings so a YAML `name: 1917` is the film, not an error.
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, coerce_numbers_to_str=True
    )

    kind: Kind
    name: str = Field(min_length=1, max_length=200)
    year: int | None = Field(default=None, ge=1870, le=2100)
    status: Status = Status.PLANNED
    rating: int | None = Field(default=None, ge=1, le=10)
    notes: str = ""
    watched_on: date | None = None
    seasons: list[int] = Field(default_factory=list, max_length=MAX_SEASONS)

    @field_validator("year", "rating", "watched_on", mode="before")
    @classmethod
    def blank_is_none(cls, value: Any) -> Any:
        return None if value == "" else value

    @field_validator("notes", mode="before")
    @classmethod
    def none_is_blank(cls, value: Any) -> Any:
        return "" if value is None else value

    @field_validator("seasons", mode="before")
    @classmethod
    def parse_seasons(cls, value: Any) -> Any:
        """Accept a YAML list, a bare count, or text such as "7, 13, 13"."""
        if isinstance(value, int) and not isinstance(value, bool):
            return [value]
        if isinstance(value, str):
            parts = [part for part in re.split(r"[,\s]+", value) if part]
            if not all(part.isdecimal() for part in parts):
                raise ValueError('use episode counts per season, such as "7, 13, 13"')
            return [int(part) for part in parts]
        return value

    @field_validator("seasons")
    @classmethod
    def check_season_sizes(cls, value: list[int]) -> list[int]:
        if any(not 1 <= count <= MAX_EPISODES_PER_SEASON for count in value):
            raise ValueError(f"each season needs 1 to {MAX_EPISODES_PER_SEASON} episodes")
        return value

    @model_validator(mode="after")
    def check_kind_fields(self) -> "TitleInput":
        if self.kind is Kind.MOVIE and self.seasons:
            raise ValueError("seasons only apply to series")
        if self.kind is Kind.SERIES and self.watched_on:
            raise ValueError("watched_on only applies to movies")
        return self


class ImportEntry(TitleInput):
    """A title in an info/ file, which can also say how far the user has got."""

    watched_through: tuple[int, int] | None = None

    @field_validator("watched_through", mode="before")
    @classmethod
    def parse_episode_code(cls, value: Any) -> Any:
        if value is None:
            return None
        match = re.fullmatch(r"S(\d+)E(\d+)", str(value).strip(), re.IGNORECASE)
        if not match:
            raise ValueError("use an episode code such as S02E05")
        return int(match[1]), int(match[2])

    @model_validator(mode="after")
    def check_watched_through(self) -> "ImportEntry":
        if self.watched_through is None:
            return self
        season, number = self.watched_through
        if not (1 <= season <= len(self.seasons) and 1 <= number <= self.seasons[season - 1]):
            raise ValueError("watched_through is not an episode listed in seasons")
        return self
