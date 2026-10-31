import pytest
from pydantic import ValidationError

from plasma.models import ImportEntry, Kind, TitleInput


def test_blank_form_fields_become_none():
    data = TitleInput(kind="movie", name="Film", year="", rating="", watched_on="")
    assert (data.year, data.rating, data.watched_on) == (None, None, None)


def test_name_is_stripped_and_required():
    assert TitleInput(kind="movie", name="  Film  ").name == "Film"
    with pytest.raises(ValidationError):
        TitleInput(kind="movie", name="   ")


def test_numeric_name_is_kept_as_text():
    assert TitleInput(kind="movie", name=1917).name == "1917"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("7, 13, 13", [7, 13, 13]),
        ("7 13", [7, 13]),
        ("7,", [7]),
        ("", []),
        (10, [10]),
        ([8, 10], [8, 10]),
    ],
)
def test_seasons_formats(value, expected):
    assert TitleInput(kind=Kind.SERIES, name="Show", seasons=value).seasons == expected


@pytest.mark.parametrize("value", ["a, b", "0", "-3", "1000", ", ".join(["1"] * 100)])
def test_seasons_rejected(value):
    with pytest.raises(ValidationError):
        TitleInput(kind=Kind.SERIES, name="Show", seasons=value)


@pytest.mark.parametrize("rating", [0, 11])
def test_rating_range(rating):
    with pytest.raises(ValidationError):
        TitleInput(kind="movie", name="Film", rating=rating)


def test_kind_specific_fields():
    with pytest.raises(ValidationError, match="seasons only apply"):
        TitleInput(kind="movie", name="Film", seasons=[3])
    with pytest.raises(ValidationError, match="watched_on only applies"):
        TitleInput(kind="series", name="Show", watched_on="2024-01-01")


@pytest.mark.parametrize("code", ["S02E05", "s2e5", " S2E05 "])
def test_watched_through_codes(code):
    entry = ImportEntry(kind="series", name="Show", seasons=[3, 5], watched_through=code)
    assert entry.watched_through == (2, 5)


@pytest.mark.parametrize("code", ["S03E01", "S02E06", "S00E01", "S01E00", "next", "2x5"])
def test_watched_through_must_be_a_listed_episode(code):
    with pytest.raises(ValidationError):
        ImportEntry(kind="series", name="Show", seasons=[3, 5], watched_through=code)


def test_unknown_import_field_is_rejected():
    with pytest.raises(ValidationError):
        ImportEntry(kind="movie", name="Film", raiting=9)
