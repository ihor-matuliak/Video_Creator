"""POST /v1/sources (probe, SHA-256 job), GET /v1/sources/{id}, POST /v1/sources/{id}/relink."""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from clipforge.adapters.media_tools import MediaTools, run_short
from clipforge.services.runtime import Runtime
from clipforge.storage import SourceRepository

from .support import RuntimeFactory, client_for, fixture_file
from .test_storage_support import add_project, add_source

JOB_TIMEOUT_S = 30.0


def new_project(client: TestClient, name: str = "Проєкт") -> str:
    response = client.post("/v1/projects", json={"name": name})
    assert response.status_code == 201
    return str(response.json()["id"])


def register(client: TestClient, project_id: str, path: Path | str) -> Response:
    return client.post("/v1/sources", json={"project_id": project_id, "path": str(path)})


def register_ok(client: TestClient, path: Path | str) -> dict[str, Any]:
    response = register(client, new_project(client), path)
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def wait_job(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + JOB_TIMEOUT_S
    while time.monotonic() < deadline:
        job: dict[str, Any] = client.get(f"/v1/jobs/{job_id}").json()
        if job["state"] in ("succeeded", "failed", "cancelled", "interrupted"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_fixture(name: str, directory: Path, new_name: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (new_name or name)
    shutil.copyfile(fixture_file(name), target)
    return target


def error_of(response: Response) -> dict[str, Any]:
    error: dict[str, Any] = response.json()["error"]
    return error


# --- probe ---------------------------------------------------------------------------------


def test_register_returns_duration_audio_streams_and_identity(client: TestClient) -> None:
    path = fixture_file("color_gop.mp4")
    source = register_ok(client, path)

    assert source["duration_ms"] == 12000
    assert source["video_stream_index"] == 0
    assert source["audio_streams"] == [
        {
            "index": 1,
            "codec": "aac",
            "channels": 1,
            "language": None,
            "title": None,
            "default": True,
            "start_offset_ms": 0,
        }
    ]
    assert source["display_name"] == "color_gop.mp4"
    assert Path(source["path"]) == path.resolve()
    assert source["size"] == path.stat().st_size
    assert source["mtime_ns"] == path.stat().st_mtime_ns
    assert source["quick_fingerprint"].startswith("qf1:")
    assert source["availability"] == "available"
    assert source["hash_status"] == "pending"
    assert source["sha256"] is None
    assert isinstance(source["hash_job_id"], str)
    assert source["canonical_origin"]["origin_ms"] == 0
    assert "/" in source["canonical_origin"]["time_base"]
    assert source["probe"]["unsupported_reason"] is None
    assert source["probe"]["width"] > 0
    assert client.get(f"/v1/sources/{source['id']}").json()["id"] == source["id"]


def test_two_audio_tracks_keep_their_global_stream_indexes(client: TestClient) -> None:
    source = register_ok(client, fixture_file("two_tracks.mkv"))
    assert [a["index"] for a in source["audio_streams"]] == [1, 2]
    assert [a["default"] for a in source["audio_streams"]] == [True, False]


def test_stream_indexes_are_global_when_audio_comes_before_video(
    client: TestClient, media_tools: MediaTools, tmp_path: Path
) -> None:
    audio_first = tmp_path / "audio_first.mkv"
    argv = [
        str(media_tools.ffmpeg),
        "-v",
        "error",
        "-y",
        "-i",
        str(fixture_file("two_tracks.mkv")),
        "-map",
        "0:a",
        "-map",
        "0:v",
        "-c",
        "copy",
        str(audio_first),
    ]
    completed = run_short(argv, timeout_s=120)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")

    source = register_ok(client, audio_first)
    assert source["video_stream_index"] == 2
    assert [a["index"] for a in source["audio_streams"]] == [0, 1]


def test_late_audio_start_is_reported_against_canonical_zero(client: TestClient) -> None:
    source = register_ok(client, fixture_file("audio_offset.mkv"))
    assert [a["start_offset_ms"] for a in source["audio_streams"]] == [1500]


def test_source_without_audio_has_no_audio_streams(client: TestClient) -> None:
    source = register_ok(client, fixture_file("no_audio.mp4"))
    assert source["audio_streams"] == []
    assert source["duration_ms"] == 12000


def test_vfr_source_duration_comes_from_the_video_stream(client: TestClient) -> None:
    assert register_ok(client, fixture_file("vfr.mkv"))["duration_ms"] == 8966


def test_unicode_and_space_in_file_name_work(client: TestClient) -> None:
    source = register_ok(client, fixture_file("Київ запис.mp4"))
    assert source["display_name"] == "Київ запис.mp4"
    assert source["duration_ms"] == 12000
    assert len(source["audio_streams"]) == 1


def test_unicode_directory_and_file_in_workspace(client: TestClient, tmp_path: Path) -> None:
    copy = copy_fixture("color_gop.mp4", tmp_path / "Відео з грою", "запис №1 (копія).mp4")
    source = register_ok(client, copy)
    assert source["display_name"] == "запис №1 (копія).mp4"


# --- distinct failure codes ----------------------------------------------------------------


def test_missing_unreadable_and_no_video_files_have_distinct_codes(
    client: TestClient, runtime: Runtime, media_tools: MediaTools, tmp_path: Path
) -> None:
    project_id = new_project(client)

    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is not a video file\n" * 200)
    audio_only = tmp_path / "audio_only.mka"
    completed = run_short(
        [
            str(media_tools.ffmpeg),
            "-v",
            "error",
            "-y",
            "-i",
            str(fixture_file("two_tracks.mkv")),
            "-map",
            "0:a:0",
            "-c",
            "copy",
            str(audio_only),
        ],
        timeout_s=120,
    )
    assert completed.returncode == 0
    directory = tmp_path / "папка.mp4"
    directory.mkdir()

    cases = {
        "missing": (tmp_path / "немає.mp4", "SOURCE_UNAVAILABLE"),
        "directory": (directory, "SOURCE_UNAVAILABLE"),
        "garbage": (broken, "SOURCE_UNREADABLE"),
        "no_video": (audio_only, "SOURCE_NO_VIDEO"),
    }
    for name, (path, code) in cases.items():
        response = register(client, project_id, path)
        assert response.status_code == 400, name
        error = error_of(response)
        assert error["code"] == code, name
        assert error["retryable"] is False
        assert error["details"] == {}
        # Fixed text: no path and no tool output.
        assert str(path) not in response.text, name
        assert path.name not in response.text, name
        assert str(tmp_path) not in response.text, name

    assert len({code for _, code in cases.values()}) == 3
    # Nothing was registered by the failures.
    with runtime.database.session() as session:
        assert SourceRepository(session).list_for_project(project_id) == []


@pytest.mark.parametrize("path", ["video.mp4", "relative\\video.mp4", "C:video.mp4", ""])
def test_relative_or_empty_path_is_422(client: TestClient, path: str) -> None:
    response = register(client, new_project(client), path)
    assert response.status_code == 422
    assert error_of(response)["code"] == "INVALID_SOURCE_PATH"


def test_nul_in_path_is_422(client: TestClient) -> None:
    response = client.post(
        "/v1/sources",
        content=b'{"project_id":"x","path":"C:\\\\a\\u0000b.mp4"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert error_of(response)["code"] == "INVALID_SOURCE_PATH"


def test_unknown_project_is_404(client: TestClient) -> None:
    response = register(client, "no-such-project", fixture_file("color_gop.mp4"))
    assert response.status_code == 404
    assert error_of(response)["code"] == "PROJECT_NOT_FOUND"


@pytest.mark.parametrize(
    "payload",
    [{}, {"project_id": "x"}, {"path": "C:\\x.mp4"}, {"project_id": 1, "path": "C:\\x.mp4"}],
)
def test_malformed_source_body_is_422(client: TestClient, payload: object) -> None:
    response = client.post("/v1/sources", json=payload)
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"


# --- missing FFmpeg ------------------------------------------------------------------------


def test_missing_media_tools_give_503_while_everything_else_works(
    make_runtime: RuntimeFactory,
) -> None:
    runtime: Runtime = make_runtime(with_tools=False)
    client = client_for(runtime)
    project_id = new_project(client)

    response = register(client, project_id, fixture_file("color_gop.mp4"))
    assert response.status_code == 503
    error = error_of(response)
    assert error["code"] == "MEDIA_TOOLS_UNAVAILABLE"
    assert error["retryable"] is False
    assert "FFmpeg" in error["message"]

    assert client.get("/v1/health").status_code == 200
    assert client.get("/v1/projects").status_code == 200
    assert client.get("/v1/jobs/none").status_code == 404
    # Nothing was left behind by the refused registration.
    with runtime.database.session() as session:
        assert SourceRepository(session).list_for_project(project_id) == []


# --- SHA-256 job ---------------------------------------------------------------------------


def test_sha256_job_reaches_succeeded_and_stores_the_hash(
    client: TestClient, tmp_path: Path
) -> None:
    original = copy_fixture("color_gop.mp4", tmp_path / "in")
    before = (sha256(original), original.stat().st_mtime_ns, original.stat().st_size)

    source = register_ok(client, original)
    assert source["hash_status"] == "pending"
    job = wait_job(client, source["hash_job_id"])
    assert job["state"] == "succeeded", job
    assert job["type"] == "source_hash"
    assert job["error"] is None
    assert job["progress"] == {"stage": "done"}

    stored = client.get(f"/v1/sources/{source['id']}").json()
    assert stored["sha256"] == before[0]
    assert stored["hash_status"] == "computed"
    assert stored["hash_job_id"] is None  # only the registration response names the job
    assert stored["availability"] == "available"
    # The original is only read.
    assert (sha256(original), original.stat().st_mtime_ns, original.stat().st_size) == before


def test_get_unknown_source_is_404(client: TestClient) -> None:
    response = client.get("/v1/sources/nope")
    assert response.status_code == 404
    assert error_of(response)["code"] == "SOURCE_NOT_FOUND"


def test_availability_follows_the_file(client: TestClient, tmp_path: Path) -> None:
    path = copy_fixture("color_gop.mp4", tmp_path)
    source = register_ok(client, path)
    wait_job(client, source["hash_job_id"])
    url = f"/v1/sources/{source['id']}"
    assert client.get(url).json()["availability"] == "available"

    with path.open("ab") as handle:
        handle.write(b"x")
    assert client.get(url).json()["availability"] == "changed"

    path.unlink()
    assert client.get(url).json()["availability"] == "missing"


# --- relink --------------------------------------------------------------------------------


def test_relink_to_an_identical_copy_succeeds(client: TestClient, tmp_path: Path) -> None:
    original = copy_fixture("color_gop.mp4", tmp_path / "old")
    source = register_ok(client, original)
    assert wait_job(client, source["hash_job_id"])["state"] == "succeeded"

    moved = tmp_path / "нова тека" / "копія.mp4"
    moved.parent.mkdir()
    shutil.copy2(original, moved)
    original.unlink()
    assert client.get(f"/v1/sources/{source['id']}").json()["availability"] == "missing"

    response = client.post(f"/v1/sources/{source['id']}/relink", json={"path": str(moved)})
    assert response.status_code == 200, response.text
    relinked = response.json()
    assert Path(relinked["path"]) == moved.resolve()
    assert relinked["display_name"] == "копія.mp4"
    assert relinked["availability"] == "available"
    assert relinked["id"] == source["id"]
    assert relinked["sha256"] == sha256(moved)
    assert relinked["duration_ms"] == source["duration_ms"]
    assert relinked["audio_streams"] == source["audio_streams"]

    stored = client.get(f"/v1/sources/{source['id']}").json()
    assert Path(stored["path"]) == moved.resolve()
    assert stored["availability"] == "available"


def test_relink_to_the_same_path_reverifies_the_file(client: TestClient, tmp_path: Path) -> None:
    path = copy_fixture("color_gop.mp4", tmp_path)
    source = register_ok(client, path)
    wait_job(client, source["hash_job_id"])
    response = client.post(f"/v1/sources/{source['id']}/relink", json={"path": str(path)})
    assert response.status_code == 200


def test_relink_to_a_different_file_is_source_mismatch(client: TestClient, tmp_path: Path) -> None:
    original = copy_fixture("color_gop.mp4", tmp_path / "a")
    source = register_ok(client, original)
    wait_job(client, source["hash_job_id"])

    same_size = tmp_path / "b" / "same_size.mp4"
    same_size.parent.mkdir()
    data = bytearray(original.read_bytes())
    data[len(data) // 2] ^= 0xFF
    same_size.write_bytes(bytes(data))
    other_size = copy_fixture("no_audio.mp4", tmp_path / "c")

    for candidate in (same_size, other_size):
        response = client.post(f"/v1/sources/{source['id']}/relink", json={"path": str(candidate)})
        assert response.status_code == 409, candidate.name
        error = error_of(response)
        assert error["code"] == "SOURCE_MISMATCH"
        assert error["retryable"] is False
        assert str(candidate) not in response.text

    stored = client.get(f"/v1/sources/{source['id']}").json()
    assert Path(stored["path"]) == original.resolve()
    assert stored["sha256"] == sha256(original)


def test_relink_target_must_be_a_readable_file(client: TestClient, tmp_path: Path) -> None:
    path = copy_fixture("color_gop.mp4", tmp_path)
    source = register_ok(client, path)
    wait_job(client, source["hash_job_id"])
    url = f"/v1/sources/{source['id']}/relink"

    for target in (tmp_path / "немає.mp4", tmp_path):
        response = client.post(url, json={"path": str(target)})
        assert response.status_code == 400
        assert error_of(response)["code"] == "SOURCE_UNAVAILABLE"
    assert client.post(url, json={"path": "relative.mp4"}).status_code == 422
    assert client.post(url, json={}).status_code == 422
    assert client.post("/v1/sources/nope/relink", json={"path": str(path)}).status_code == 404


def test_relink_needs_a_computed_hash(client: TestClient, runtime: Runtime, tmp_path: Path) -> None:
    path = copy_fixture("color_gop.mp4", tmp_path)
    with runtime.database.write_session() as session:
        project_id = add_project(session).id
        pending = add_source(session, project_id)
        failed = add_source(session, project_id)
        SourceRepository(session).set_hash(failed.id, sha256=None, hash_status="failed")

    response = client.post(f"/v1/sources/{pending.id}/relink", json={"path": str(path)})
    assert response.status_code == 409
    assert error_of(response)["code"] == "SOURCE_HASH_PENDING"
    assert error_of(response)["retryable"] is True

    response = client.post(f"/v1/sources/{failed.id}/relink", json={"path": str(path)})
    assert response.status_code == 409
    assert error_of(response)["code"] == "SOURCE_HASH_UNAVAILABLE"
    assert error_of(response)["retryable"] is False
