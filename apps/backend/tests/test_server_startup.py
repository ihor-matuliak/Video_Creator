"""Start-up wiring of the real sidecar process: workspace, migrations and FFmpeg (ADR 017)."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx2

from clipforge.adapters.media_tools import ENV_FFMPEG_PATH, ENV_FFPROBE_PATH, MediaTools
from clipforge.storage import ProjectRepository, SourceRepository, open_database
from clipforge.storage.workspace import WorkspaceLock

from .support import TOKEN, fixture_file
from .test_server_process import TIMEOUT, clean_env, config_line, first_stdout_line, sidecar

AUTH = {"Authorization": f"Bearer {TOKEN}"}


def call(port: int, method: str, path: str, body: object = None) -> httpx2.Response:
    with httpx2.Client(trust_env=False, timeout=30.0) as http:
        return http.request(method, f"http://127.0.0.1:{port}{path}", json=body, headers=AUTH)


def test_locked_workspace_exits_2_without_ready_line(tmp_path: Path) -> None:
    data_dir = tmp_path / "дані"
    with open_database(data_dir), sidecar("--port", "0", "--config-stdin") as process:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(config_line(data_dir))
        process.stdin.flush()
        assert process.wait(timeout=TIMEOUT) == 2
        assert process.stdout.read() == b""
        message = process.stderr.read().decode("utf-8")
    assert message.startswith("clipforge: Робочу теку вже використовує інший екземпляр")
    assert TOKEN not in message
    assert str(tmp_path) not in message


def test_failed_migration_exits_2_and_leaves_the_database_untouched(tmp_path: Path) -> None:
    data_dir = tmp_path / "дані"
    data_dir.mkdir()
    database = data_dir / "clipforge.sqlite3"
    with closing(sqlite3.connect(database)) as connection:
        connection.isolation_level = None
        assert connection.execute("PRAGMA journal_mode=delete").fetchone() == ("delete",)
        connection.execute("CREATE TABLE foreign_table (value TEXT)")
        connection.execute("INSERT INTO foreign_table VALUES ('keep me')")
    digest = hashlib.sha256(database.read_bytes()).hexdigest()

    with sidecar("--port", "0", "--config-stdin") as process:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(config_line(data_dir))
        process.stdin.flush()
        assert process.wait(timeout=TIMEOUT) == 2
        assert process.stdout.read() == b""
        message = process.stderr.read().decode("utf-8")
    assert message.startswith("clipforge: База даних не має версії схеми")
    assert str(tmp_path) not in message

    # Refused, not merely unchanged in content: still journal_mode=delete (ADR 019).
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest
    assert sorted(path.name for path in data_dir.glob("clipforge.sqlite3-*")) == []
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert tables == {"foreign_table"}
        assert connection.execute("SELECT value FROM foreign_table").fetchall() == [("keep me",)]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == digest
    lock = WorkspaceLock(data_dir / "workspace.lock")
    lock.acquire()  # the failed start released the workspace
    lock.release()


def test_missing_ffmpeg_gives_503_on_media_routes_and_health_stays_200(tmp_path: Path) -> None:
    missing = tmp_path / "нема" / "ffmpeg.exe"
    env = clean_env(**{ENV_FFMPEG_PATH: str(missing), ENV_FFPROBE_PATH: str(missing)})
    with sidecar("--port", "0", "--config-stdin", env=env) as process:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(config_line(tmp_path / "дані"))
        process.stdin.flush()
        port = json.loads(first_stdout_line(process))["port"]

        assert call(port, "GET", "/v1/health").status_code == 200
        project = call(port, "POST", "/v1/projects", {"name": "Без FFmpeg"})
        assert project.status_code == 201

        source = call(
            port,
            "POST",
            "/v1/sources",
            {"project_id": project.json()["id"], "path": str(fixture_file("color_gop.mp4"))},
        )
        assert source.status_code == 503
        assert source.json()["error"]["code"] == "MEDIA_TOOLS_UNAVAILABLE"
        assert str(tmp_path) not in source.text

        assert call(port, "GET", "/v1/projects").status_code == 200
        assert call(port, "GET", "/v1/health").status_code == 200

        process.stdin.close()
        assert process.wait(timeout=TIMEOUT) == 0
        warning = process.stderr.read().decode("utf-8")
    assert warning.startswith("clipforge: ffmpeg не знайдено")
    assert str(tmp_path) not in warning
    assert TOKEN not in warning


def test_sidecar_registers_hashes_and_persists_a_source(
    tmp_path: Path, media_tools: MediaTools
) -> None:
    data_dir = tmp_path / "дані"
    media = tmp_path / "media" / "Київ запис.mp4"
    media.parent.mkdir()
    shutil.copyfile(fixture_file("Київ запис.mp4"), media)
    env = clean_env(
        **{ENV_FFMPEG_PATH: str(media_tools.ffmpeg), ENV_FFPROBE_PATH: str(media_tools.ffprobe)}
    )

    with sidecar("--port", "0", "--config-stdin", env=env) as process:
        assert process.stdin is not None
        process.stdin.write(config_line(data_dir))
        process.stdin.flush()
        port = json.loads(first_stdout_line(process))["port"]

        project = call(port, "POST", "/v1/projects", {"name": "Реєстрація"}).json()
        response = call(
            port, "POST", "/v1/sources", {"project_id": project["id"], "path": str(media)}
        )
        assert response.status_code == 201, response.text
        source: dict[str, Any] = response.json()
        assert source["duration_ms"] == 12000

        deadline = time.monotonic() + TIMEOUT
        job: dict[str, Any] = {}
        while time.monotonic() < deadline:
            job = call(port, "GET", f"/v1/jobs/{source['hash_job_id']}").json()
            if job["state"] in ("succeeded", "failed"):
                break
            time.sleep(0.05)
        assert job["state"] == "succeeded", job
        stored = call(port, "GET", f"/v1/sources/{source['id']}").json()
        assert stored["hash_status"] == "computed"

        process.stdin.close()
        assert process.wait(timeout=TIMEOUT) == 0

    # The workspace is free again and holds what the sidecar saved.
    with open_database(data_dir) as database, database.session() as session:
        saved = SourceRepository(session).get(source["id"])
        assert saved is not None
        assert saved.sha256 == stored["sha256"]
        assert saved.hash_status == "computed"
        assert [p.name for p in ProjectRepository(session).list_page().items] == ["Реєстрація"]
