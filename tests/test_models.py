import pytest
from pydantic import ValidationError

from plasma.models import (
    Details,
    ImportEntry,
    Kind,
    PlanInput,
    PlatformInput,
    Status,
    TitleInput,
)


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
    with pytest.raises(ValidationError, match="watched_on doesn't apply to series"):
        TitleInput(kind="series", name="Show", watched_on="2024-01-01")


@pytest.mark.parametrize("code", ["S02E05", "s2e5", " S2E05 "])
def test_watched_through_codes(code):
    entry = ImportEntry(kind="series", name="Show", seasons=[3, 5], watched_through=code)
    assert entry.watched_through == (2, 5)


@pytest.mark.parametrize("code", ["S03E01", "S02E06", "S00E01", "S01E00", "next", "2x5"])
def test_watched_through_must_be_a_listed_episode(code):
    with pytest.raises(ValidationError):
        ImportEntry(kind="series", name="Show", seasons=[3, 5], watched_through=code)


@pytest.mark.parametrize("image", ["poster.jpg", "My Poster.PNG", "a.webp"])
def test_image_file_names(image):
    assert ImportEntry(kind="movie", name="Film", image=image).image == image


@pytest.mark.parametrize(
    "image", ["../secret.jpg", "sub/poster.jpg", "/etc/poster.jpg", "a\\b.jpg", "poster.svg", ".."]
)
def test_image_must_be_a_plain_image_file_name(image):
    with pytest.raises(ValidationError):
        ImportEntry(kind="movie", name="Film", image=image)


def test_status_defaults_to_none():
    assert TitleInput(kind="movie", name="Film").status is Status.NONE


@pytest.mark.parametrize("duration", [0, -5, 6001, "long"])
def test_duration_range(duration):
    with pytest.raises(ValidationError):
        Details(duration=duration)


def test_blank_details():
    details = Details(duration="", description=None)
    assert (details.duration, details.description) == (None, "")


@pytest.mark.parametrize("fields", [{"seasons": [3]}, {"duration": 90}])
def test_game_rejects_fields_of_other_kinds(fields):
    with pytest.raises(ValidationError):
        TitleInput(kind="game", name="Hades", **fields)


@pytest.mark.parametrize("kind", ["movie", "series"])
def test_hours_played_only_applies_to_games(kind):
    with pytest.raises(ValidationError, match="hours_played only applies to games"):
        TitleInput(kind=kind, name="Title", hours_played=5)


@pytest.mark.parametrize("hours", [0, 100_000])
def test_hours_played_range(hours):
    assert TitleInput(kind="game", name="Hades", hours_played=hours).hours_played == hours


@pytest.mark.parametrize("hours", [-1, 100_001])
def test_hours_played_out_of_range(hours):
    with pytest.raises(ValidationError):
        TitleInput(kind="game", name="Hades", hours_played=hours)


def test_game_can_have_a_finish_date():
    data = TitleInput(kind="game", name="Hades", watched_on="2024-08-17")
    assert data.watched_on.isoformat() == "2024-08-17"


def test_import_accepts_playing_as_a_status():
    entry = ImportEntry(kind="game", name="Hades", status="playing")
    assert entry.status is Status.WATCHING


def test_producer_is_stored_on_every_kind():
    for kind in ("movie", "series", "game"):
        assert TitleInput(kind=kind, name="Title", producer=" Studio ").producer == "Studio"


def test_blank_producer_from_yaml():
    assert TitleInput(kind="movie", name="Film", producer=None).producer == ""


@pytest.mark.parametrize("kind", ["movie", "series"])
def test_owned_only_applies_to_games(kind):
    with pytest.raises(ValidationError, match="owned only applies to games"):
        ImportEntry(kind=kind, name="Title", owned=True)


def test_plan_needs_a_name():
    with pytest.raises(ValidationError):
        PlanInput(name="   ")


@pytest.mark.parametrize("name", ["Favorites", "favorites", " FAVORITES "])
def test_favorites_is_a_reserved_plan_name(name):
    with pytest.raises(ValidationError, match="reserved"):
        PlanInput(name=name)


def test_series_duration_is_rejected():
    with pytest.raises(ValidationError, match="duration only applies to movies"):
        TitleInput(kind="series", name="Show", duration=45)


def test_episode_details_in_an_import_entry():
    entry = ImportEntry(
        kind="series",
        name="Show",
        seasons=[2],
        episodes={"s1e2": {"duration": 45, "description": "Plot"}},
    )
    assert entry.episodes[(1, 2)].duration == 45


@pytest.mark.parametrize(
    "episodes",
    [{"S09E01": {}}, {"pilot": {}}, {"S01E01": {"lenght": 45}}, {"S01E01": {"duration": 0}}, "x"],
)
def test_bad_episode_details_are_rejected(episodes):
    with pytest.raises(ValidationError):
        ImportEntry(kind="series", name="Show", seasons=[2], episodes=episodes)


@pytest.mark.parametrize("url", ["", "https://a.com", "http://a.com/b", "HTTPS://A.COM"])
def test_platform_urls(url):
    assert PlatformInput(name="A", url=url).url == url


@pytest.mark.parametrize("url", ["javascript:alert(1)", "a.com", "https://", "https://a b.com"])
def test_platform_urls_rejected(url):
    with pytest.raises(ValidationError):
        PlatformInput(name="A", url=url)


def test_platform_needs_a_name():
    with pytest.raises(ValidationError):
        PlatformInput(name="  ")


def test_import_platforms_accept_one_name_or_a_list():
    assert ImportEntry(kind="movie", name="F", platforms="Netflix").platforms == ["Netflix"]
    assert ImportEntry(kind="movie", name="F", platforms=["A", "B"]).platforms == ["A", "B"]
    with pytest.raises(ValidationError):
        ImportEntry(kind="movie", name="F", platforms=[""])


def test_release_date_accepts_blank_and_rejects_nonsense():
    assert Details(release_date="").release_date is None
    with pytest.raises(ValidationError):
        Details(release_date="soon")


def test_unknown_import_field_is_rejected():
    with pytest.raises(ValidationError):
        ImportEntry(kind="movie", name="Film", raiting=9)
