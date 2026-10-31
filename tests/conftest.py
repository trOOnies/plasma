import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plasma.db import connect
from plasma.models import Kind, TitleInput
from plasma.web import create_app


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    yield connection
    connection.close()


@pytest.fixture
def client(db_path: Path) -> TestClient:
    return TestClient(create_app(db_path), follow_redirects=False)


def series(name: str = "Show", seasons: tuple[int, ...] = (3, 2), **fields) -> TitleInput:
    return TitleInput(kind=Kind.SERIES, name=name, seasons=list(seasons), **fields)


def movie(name: str = "Film", **fields) -> TitleInput:
    return TitleInput(kind=Kind.MOVIE, name=name, **fields)
