"""GET /v1/system/checks: FFmpeg, FFprobe and data directory readiness (ADR 023)."""

from __future__ import annotations

import errno
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from clipforge.api import system
from clipforge.services.runtime import Runtime

from .support import RuntimeFactory, client_for

URL = "/v1/system/checks"
SECRET = "SECRET-DETAIL-4711"


def check_dir(runtime: Runtime) -> Path:
    return runtime.database.data_dir / system.CHECK_DIRNAME


def leftovers(runtime: Runtime) -> list[str]:
    directory = check_dir(runtime)
    return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []


def test_both_tools_present_and_data_dir_writable(client: TestClient, runtime: Runtime) -> None:
    tools = runtime.media_tools
    assert tools is not None
    response = client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ffmpeg", "ffprobe", "data_dir"}
    assert body["ffmpeg"] == {"ok": True, "version": tools.ffmpeg_version}
    assert body["ffprobe"] == {"ok": True, "version": tools.ffprobe_version}
    assert set(body["data_dir"]) == {"ok", "free_bytes"}
    assert body["data_dir"]["ok"] is True
    free = body["data_dir"]["free_bytes"]
    assert isinstance(free, int)
    assert not isinstance(free, bool)
    assert free > 0


def test_free_bytes_comes_from_the_disk_of_the_data_directory(
    client: TestClient, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[Path] = []

    def disk_usage(path: Path) -> SimpleNamespace:
        asked.append(Path(path))
        return SimpleNamespace(total=10_000, used=4_000, free=6_000)

    monkeypatch.setattr("clipforge.api.system.shutil.disk_usage", disk_usage)
    body = client.get(URL).json()
    assert body["data_dir"] == {"ok": True, "free_bytes": 6_000}
    assert asked == [runtime.database.data_dir]


def test_unknown_free_space_is_null_and_does_not_fail_the_request(
    client: TestClient,
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def disk_usage(path: Path) -> SimpleNamespace:
        raise OSError(errno.EIO, f"{SECRET} {path}")

    monkeypatch.setattr("clipforge.api.system.shutil.disk_usage", disk_usage)
    caplog.set_level(logging.DEBUG)
    response = client.get(URL)
    assert response.status_code == 200
    assert response.json()["data_dir"] == {"ok": True, "free_bytes": None}
    assert SECRET not in response.text
    assert SECRET not in caplog.text
    assert str(runtime.database.data_dir) not in caplog.text


def test_missing_ffmpeg_is_reported_without_a_version(make_runtime: RuntimeFactory) -> None:
    runtime = make_runtime(with_tools=False)
    assert runtime.media_tools is None
    response = client_for(runtime).get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["ffmpeg"] == {"ok": False, "version": None}
    assert body["ffprobe"] == {"ok": False, "version": None}
    assert body["data_dir"]["ok"] is True
    assert isinstance(body["data_dir"]["free_bytes"], int)


def test_tools_are_not_started_per_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a process was started")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for _ in range(2):
        response = client.get(URL)
        assert response.status_code == 200
        assert response.json()["ffmpeg"]["ok"] is True


def test_unwritable_data_dir_is_ok_false_not_500(
    client: TestClient,
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_dir = runtime.database.data_dir

    def refuse(target: Path) -> None:
        raise PermissionError(errno.EACCES, f"{SECRET} {target}", str(data_dir))

    monkeypatch.setattr(system, "_write_probe_file", refuse)
    caplog.set_level(logging.DEBUG)
    response = client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["data_dir"]["ok"] is False
    assert isinstance(body["data_dir"]["free_bytes"], int)
    assert body["ffmpeg"]["ok"] is True
    assert SECRET not in response.text
    assert SECRET not in caplog.text
    assert str(data_dir) not in caplog.text
    assert leftovers(runtime) == []


def test_data_dir_where_the_temp_subdirectory_cannot_be_made_is_ok_false(
    client: TestClient, runtime: Runtime, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = check_dir(runtime)
    blocker.write_bytes(b"not a directory")
    caplog.set_level(logging.DEBUG)
    response = client.get(URL)
    assert response.status_code == 200
    assert response.json()["data_dir"]["ok"] is False
    assert str(runtime.database.data_dir) not in caplog.text
    assert blocker.read_bytes() == b"not a directory"


def test_check_file_is_unique_deleted_and_other_files_survive(
    client: TestClient, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = check_dir(runtime)
    directory.mkdir()
    (directory / "keep.txt").write_bytes(b"mine")

    created: list[Path] = []
    write = system._write_probe_file

    def spy(target: Path) -> None:
        created.append(target)
        write(target)
        assert target.is_file()

    monkeypatch.setattr(system, "_write_probe_file", spy)
    for _ in range(3):
        assert client.get(URL).json()["data_dir"]["ok"] is True

    assert len(created) == 3
    assert len({path.name for path in created}) == 3
    for path in created:
        assert path.parent == directory
        assert path.name.startswith(system.CHECK_FILE_PREFIX)
        assert not path.exists()
    assert leftovers(runtime) == ["keep.txt"]
    assert (directory / "keep.txt").read_bytes() == b"mine"


def test_without_a_token_it_is_401_and_nothing_is_checked(
    anonymous: TestClient, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(target: Path) -> None:
        raise AssertionError("the data directory was touched")

    monkeypatch.setattr(system, "_write_probe_file", forbidden)
    response = anonymous.get(URL)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert "ffmpeg" not in response.text
    wrong = anonymous.get(URL, headers={"Authorization": "Bearer wrong-token"})
    assert wrong.status_code == 401
    assert not check_dir(runtime).exists()


@pytest.mark.parametrize("scenario", ["ready", "no_ffmpeg", "unwritable"])
def test_response_has_no_paths(
    scenario: str,
    tmp_path: Path,
    make_runtime: RuntimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = make_runtime(with_tools=scenario != "no_ffmpeg")
    if scenario == "unwritable":

        def refuse(target: Path) -> None:
            raise PermissionError(errno.EACCES, str(target))

        monkeypatch.setattr(system, "_write_probe_file", refuse)

    response = client_for(runtime).get(URL)
    assert response.status_code == 200
    forbidden = {str(tmp_path), str(runtime.database.data_dir), runtime.database.data_dir.name}
    if runtime.media_tools is not None:
        forbidden |= {
            str(runtime.media_tools.ffmpeg),
            str(runtime.media_tools.ffprobe),
            str(runtime.media_tools.ffmpeg.parent),
        }
    for text in forbidden:
        assert text not in response.text
        assert text.replace("\\", "\\\\") not in response.text
        assert text.replace("\\", "/") not in response.text
