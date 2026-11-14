"""Constants and helpers shared by the backend tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipforge.api.app import create_app
from clipforge.config import Config, Mode
from clipforge.services.runtime import Runtime

TOKEN = "t" * 16 + "0123456789abcdef" + "T" * 16
PORT = 18765
RuntimeFactory = Callable[..., Runtime]
BASE_URL = f"http://127.0.0.1:{PORT}"
BACKEND_DIR = Path(__file__).resolve().parents[1]


def make_config(data_dir: Path, mode: Mode = "test") -> Config:
    return Config(mode=mode, data_dir=data_dir, session_token=TOKEN)


FIXTURES = BACKEND_DIR.parents[1] / "test-results" / "fixtures"


def fixture_file(name: str) -> Path:
    """A generated media fixture (``npm run fixtures``). Tests only read it or copy it."""
    path = FIXTURES / name
    if not path.is_file():
        pytest.fail(f"Fixture {name} is missing: run `npm run fixtures` first")
    return path


def client_for(runtime: Runtime) -> TestClient:
    """An authenticated client for a second app over ``runtime`` (the ``client`` fixture
    covers the usual case)."""
    app = create_app(make_config(runtime.database.data_dir, "test"), port=PORT, runtime=runtime)
    return TestClient(
        app,
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {TOKEN}"},
        raise_server_exceptions=False,
    )
