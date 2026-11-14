from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clipforge.adapters.media_tools import MediaTools, resolve_media_tools
from clipforge.api.app import create_app
from clipforge.config import Mode
from clipforge.services.runtime import Runtime

from .support import BASE_URL, PORT, TOKEN, RuntimeFactory, make_config


@pytest.fixture(scope="session")
def media_tools() -> MediaTools:
    """The FFmpeg/FFprobe the test run uses; resolved only by tests that need media."""
    return resolve_media_tools("test", os.environ)


@pytest.fixture
def make_runtime(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[RuntimeFactory]:
    """``factory(with_tools=True, data_dir=None)``: an open runtime, closed at teardown.

    Every call gets its own workspace unless ``data_dir`` is given, because a workspace can be
    open only once. ``with_tools=False`` simulates a machine without FFmpeg.
    """
    opened: list[Runtime] = []

    def factory(*, with_tools: bool = True, data_dir: Path | None = None) -> Runtime:
        tools: MediaTools | None = request.getfixturevalue("media_tools") if with_tools else None
        runtime = Runtime.start(
            data_dir if data_dir is not None else tmp_path / f"workspace-{len(opened)}",
            media_tools=tools,
        )
        opened.append(runtime)
        return runtime

    try:
        yield factory
    finally:
        for runtime in reversed(opened):
            runtime.close()


@pytest.fixture
def make_app(make_runtime: RuntimeFactory) -> Callable[[Mode], FastAPI]:
    def factory(mode: Mode = "test") -> FastAPI:
        runtime = make_runtime(with_tools=False)
        return create_app(make_config(runtime.database.data_dir, mode), port=PORT, runtime=runtime)

    return factory


@pytest.fixture
def runtime(make_runtime: RuntimeFactory) -> Runtime:
    """The runtime behind ``app``/``client``: real FFmpeg, empty workspace."""
    return make_runtime()


@pytest.fixture
def app(runtime: Runtime) -> FastAPI:
    return create_app(make_config(runtime.database.data_dir, "test"), port=PORT, runtime=runtime)


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
