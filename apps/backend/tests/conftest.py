from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.config import Mode

from .support import BASE_URL, PORT, TOKEN, make_config


@pytest.fixture
def make_app(tmp_path: Path) -> Callable[[Mode], FastAPI]:
    def factory(mode: Mode = "test") -> FastAPI:
        return create_app(make_config(tmp_path, mode), port=PORT)

    return factory


@pytest.fixture
def app(make_app: Callable[[Mode], FastAPI]) -> FastAPI:
    return make_app("test")


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(
        app,
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {TOKEN}"},
        raise_server_exceptions=False,
    )


@pytest.fixture
def anonymous(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=BASE_URL, raise_server_exceptions=False)
