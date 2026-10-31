"""Load titles from the YAML files in info/ into the library.

A title's tracking fields (status, watch date, progress, owned) are read the first
time it is imported. After that the app owns them, so a re-import cannot undo anything
done in the web UI. Re-imports only add new titles, add newly listed episodes, fill
in a rating, duration, hours played, producer, release date, description, notes or
platforms that are still blank, and apply the image, which can only be set from info/.
The platforms list adds services and consoles that no title uses yet, and fills in any
URL that is still blank.

The format is documented in the README, and demo.yaml is a working example.
"""

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from plasma import library
from plasma.db import connect
from plasma.models import ImportEntry, Kind, PlatformInput, describe_errors

INFO_DIR = Path("info")
IMAGES_FOLDER = "images"
REQUIRED_FOLDERS = (IMAGES_FOLDER,)
REQUIRED_FILES = ("library.yaml",)
SECTIONS = {"movies": Kind.MOVIE, "series": Kind.SERIES, "games": Kind.GAME}
PLATFORMS_SECTION = "platforms"


class ImportFileError(Exception):
    """One or more problems in the input, as a list of readable messages."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


class InputsCreatedError(Exception):
    """Required inputs were missing and have been created empty."""

    def __init__(self, created: list[Path]):
        names = ", ".join(str(path) for path in created)
        super().__init__(
            f"Required input was missing, so it was created empty: {names}. "
            "Add your movies and series, run `make import`, then start the app again."
        )
        self.created = created


@dataclass
class ImportResult:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    platforms_added: int = 0


def ensure_inputs(directory: Path = INFO_DIR) -> list[Path]:
    """Create any missing required folder or file, empty. Returns what was created."""
    created = []
    if not directory.exists():
        directory.mkdir(parents=True)
        created.append(directory)
    for name in REQUIRED_FOLDERS:
        path = directory / name
        if not path.exists():
            path.mkdir()
            created.append(path)
    for name in REQUIRED_FILES:
        path = directory / name
        if not path.exists():
            path.touch()
            created.append(path)
    return created


def parse_platforms(path: Path, items: object) -> tuple[list[PlatformInput], list[str]]:
    if not isinstance(items, list):
        return [], [f"{path}: '{PLATFORMS_SECTION}' must be a list"]
    platforms, problems = [], []
    for index, item in enumerate(items):
        where = f"{path}: {PLATFORMS_SECTION}[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where}: expected a mapping with at least a name")
            continue
        try:
            platforms.append(PlatformInput(**item))
        except ValidationError as error:
            problems.extend(f"{where}: {message}" for message in describe_errors(error))
    return platforms, problems


def parse_file(path: Path) -> tuple[list[PlatformInput], list[ImportEntry]]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ImportFileError([f"{path}: invalid YAML: {error}"]) from error
    if document is None:
        return [], []
    if not isinstance(document, dict) or set(document) - {*SECTIONS, PLATFORMS_SECTION}:
        message = "top level must only have 'platforms', 'movies', 'series' and 'games' lists"
        raise ImportFileError([f"{path}: {message}"])

    platforms, problems = parse_platforms(path, document.get(PLATFORMS_SECTION) or [])
    entries = []
    for section, kind in SECTIONS.items():
        items = document.get(section) or []
        if not isinstance(items, list):
            problems.append(f"{path}: '{section}' must be a list")
            continue
        for index, item in enumerate(items):
            where = f"{path}: {section}[{index}]"
            if not isinstance(item, dict):
                problems.append(f"{where}: expected a mapping with at least a name")
                continue
            try:
                entry = ImportEntry(**{**item, "kind": kind})
            except ValidationError as error:
                problems.extend(f"{where}: {message}" for message in describe_errors(error))
                continue
            image = path.parent / IMAGES_FOLDER / entry.image if entry.image else None
            if image and not image.is_file():
                problems.append(f"{where}: image: {image} does not exist")
                continue
            entries.append(entry)
    if problems:
        raise ImportFileError(problems)
    return platforms, entries


def yaml_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.suffix in (".yaml", ".yml"))


def _fill_episodes(conn: sqlite3.Connection, title_id: int, entry: ImportEntry) -> bool:
    changes = [
        library.fill_episode_blanks(conn, title_id, season, number, details)
        for (season, number), details in entry.episodes.items()
    ]
    return any(changes)


def import_entries(
    conn: sqlite3.Connection, entries: list[ImportEntry], platforms: list[PlatformInput]
) -> ImportResult:
    result = ImportResult()
    for platform in platforms:
        result.platforms_added += library.fill_platform(conn, platform)
    for entry in entries:
        existing = library.find_title(conn, entry.kind, entry.name, entry.year)
        if existing is None:
            title_id = library.create_title(
                conn, entry.model_copy(update={"watched_through": None})
            )
            if entry.watched_through:
                library.mark_through(conn, title_id, *entry.watched_through)
            if entry.image:
                library.set_image(conn, title_id, entry.image)
            _fill_episodes(conn, title_id, entry)
            library.fill_title_platforms(conn, title_id, entry.platforms)
            if entry.owned:
                library.toggle_owned(conn, title_id)
            result.added += 1
            continue
        grew = library.extend_seasons(conn, existing["id"], entry.seasons)
        filled = library.fill_blanks(
            conn,
            existing["id"],
            rating=entry.rating,
            duration=entry.duration,
            hours_played=entry.hours_played,
            producer=entry.producer,
            release_date=library.iso_date(entry.release_date),
            description=entry.description,
            notes=entry.notes,
        )
        filled = _fill_episodes(conn, existing["id"], entry) or filled
        filled = library.fill_title_platforms(conn, existing["id"], entry.platforms) or filled
        new_image = entry.image is not None and entry.image != existing["image"]
        if new_image:
            library.set_image(conn, existing["id"], entry.image)
        if grew or filled or new_image:
            result.updated += 1
        else:
            result.unchanged += 1
    return result


def import_paths(conn: sqlite3.Connection, paths: list[Path]) -> ImportResult:
    """Import files as one transaction. Any problem in any file means nothing is written."""
    platforms, entries, problems = [], [], []
    for path in paths:
        try:
            file_platforms, file_entries = parse_file(path)
        except ImportFileError as error:
            problems.extend(error.problems)
            continue
        platforms.extend(file_platforms)
        entries.extend(file_entries)
    if problems:
        raise ImportFileError(problems)
    with conn:
        return import_entries(conn, entries, platforms)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import movies and series from YAML files.")
    parser.add_argument("directory", type=Path, help="folder holding the YAML files")
    args = parser.parse_args(argv)

    try:
        created = ensure_inputs(args.directory)
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for path in created:
        print(f"Created {path}, which was missing.")
    paths = yaml_files(args.directory)

    conn = connect()
    try:
        result = import_paths(conn, paths)
    except ImportFileError as error:
        for problem in error.problems:
            print(f"error: {problem}", file=sys.stderr)
        print("Nothing was imported.", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(
        f"{len(paths)} file(s): {result.added} added, {result.updated} updated, "
        f"{result.unchanged} unchanged, {result.platforms_added} new platform(s)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
