"""POST /v1/sources (probe, SHA-256 job), GET /v1/sources/{id}, POST /v1/sources/{id}/relink."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from clipforge.adapters.media_tools import MediaTools, run_short
from clipforge.api.media import ByteRange, RangeNotSatisfiableError, parse_byte_range
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


def test_probe_reports_frame_rate_and_vfr_flag(client: TestClient) -> None:
    vfr = register_ok(client, fixture_file("vfr.mkv"))["probe"]
    assert vfr["vfr"] is True
    numerator, denominator = (int(part) for part in vfr["frame_rate"].split("/"))
    assert 20.0 < numerator / denominator < 20.1  # 180 frames over about 9 s, not 30

    for name in ("color_gop.mp4", "two_tracks.mkv"):
        constant = register_ok(client, fixture_file(name))["probe"]
        assert constant["vfr"] is False, name
        assert constant["frame_rate"] == "30/1", name


def test_probe_without_frame_timing_keys_is_served_as_stored(
    client: TestClient, runtime: Runtime
) -> None:
    source = register_ok(client, fixture_file("color_gop.mp4"))
    old_probe = {
        key: value for key, value in source["probe"].items() if key not in ("frame_rate", "vfr")
    }
    with runtime.database.write_session() as session:
        stored = SourceRepository(session).get(source["id"])
        assert stored is not None
        stored.probe_json = old_probe  # JSON columns are assigned whole, never mutated

    response = client.get(f"/v1/sources/{source['id']}")
    assert response.status_code == 200
    assert response.json()["probe"] == old_probe
    assert "vfr" not in response.json()["probe"]


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


# --- list ----------------------------------------------------------------------------------

LIST_FIXTURES = ("color_gop.mp4", "no_audio.mp4", "two_tracks.mkv")


def register_three(client: TestClient, project_id: str, directory: Path) -> list[dict[str, Any]]:
    """Register three files and wait for their SHA-256 jobs, so later reads are stable."""
    sources: list[dict[str, Any]] = []
    for name in LIST_FIXTURES:
        response = register(client, project_id, copy_fixture(name, directory))
        assert response.status_code == 201, response.text
        sources.append(response.json())
    for source in sources:
        assert wait_job(client, source["hash_job_id"])["state"] == "succeeded"
    return sources


def list_sources(client: TestClient, project_id: str, **params: str) -> Response:
    return client.get("/v1/sources", params={"project_id": project_id, **params})


def test_list_is_in_creation_order_and_items_equal_get(client: TestClient, tmp_path: Path) -> None:
    project_id = new_project(client)
    registered = register_three(client, project_id, tmp_path)

    response = list_sources(client, project_id)
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    items = body["items"]
    # The clock is coarse, so sources registered in one tick are ordered by id.
    keys = [(item["created_at"], item["id"]) for item in items]
    assert keys == sorted(keys)
    assert {item["id"] for item in items} == {source["id"] for source in registered}
    for item in items:
        assert item == client.get(f"/v1/sources/{item['id']}").json()
        assert item["hash_job_id"] is None


def test_list_pages_with_a_cursor_cover_every_source_once(
    client: TestClient, tmp_path: Path
) -> None:
    project_id = new_project(client)
    registered = register_three(client, project_id, tmp_path)
    everything = [item["id"] for item in list_sources(client, project_id).json()["items"]]

    first = list_sources(client, project_id, limit="2").json()
    assert len(first["items"]) == 2
    assert first["next_cursor"] == first["items"][-1]["id"]
    second = list_sources(client, project_id, limit="2", cursor=first["next_cursor"]).json()
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None

    seen = [item["id"] for item in first["items"] + second["items"]]
    assert seen == everything
    assert len(set(seen)) == 3
    assert set(seen) == {source["id"] for source in registered}


def test_list_never_shows_sources_of_other_projects(client: TestClient, tmp_path: Path) -> None:
    mine, other = new_project(client, "Мій"), new_project(client, "Чужий")
    own = register_three(client, mine, tmp_path / "mine")
    foreign = register_three(client, other, tmp_path / "other")

    for project_id, expected in ((mine, own), (other, foreign)):
        items = list_sources(client, project_id).json()["items"]
        assert {item["id"] for item in items} == {source["id"] for source in expected}
        assert {item["project_id"] for item in items} == {project_id}
    # A cursor taken from another project never lets its sources in.
    cursor = foreign[0]["id"]
    leaked = list_sources(client, mine, cursor=cursor).json()["items"]
    assert {item["project_id"] for item in leaked} <= {mine}


def test_list_of_a_project_without_sources_is_empty(client: TestClient) -> None:
    response = list_sources(client, new_project(client))
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_list_of_an_unknown_project_is_404(client: TestClient) -> None:
    response = list_sources(client, "no-such-project")
    assert response.status_code == 404
    assert error_of(response)["code"] == "PROJECT_NOT_FOUND"


def test_list_needs_a_project_id(client: TestClient) -> None:
    for params in ({}, {"project_id": ""}):
        response = client.get("/v1/sources", params=params)
        assert response.status_code == 422
        assert error_of(response)["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("limit", ["0", "-1", "101", "abc"])
def test_list_bad_limit_is_422(client: TestClient, limit: str) -> None:
    response = list_sources(client, new_project(client), limit=limit)
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"


def test_list_accepts_limits_from_1_to_100(client: TestClient) -> None:
    project_id = new_project(client)
    for limit in ("1", "100"):
        assert list_sources(client, project_id, limit=limit).status_code == 200


def test_list_garbage_cursor_is_422(client: TestClient, tmp_path: Path) -> None:
    project_id = new_project(client)
    register_three(client, project_id, tmp_path)
    response = list_sources(client, project_id, cursor="garbage-cursor")
    assert response.status_code == 422
    assert error_of(response)["code"] == "INVALID_CURSOR"
    assert "garbage-cursor" not in response.text


def test_list_availability_follows_the_file(client: TestClient, tmp_path: Path) -> None:
    project_id = new_project(client)
    source = register(client, project_id, copy_fixture("color_gop.mp4", tmp_path)).json()
    wait_job(client, source["hash_job_id"])
    path = Path(source["path"])
    assert list_sources(client, project_id).json()["items"][0]["availability"] == "available"

    with path.open("ab") as handle:
        handle.write(b"x")
    assert list_sources(client, project_id).json()["items"][0]["availability"] == "changed"

    path.unlink()
    item = list_sources(client, project_id).json()["items"][0]
    assert item["availability"] == "missing"
    assert item == client.get(f"/v1/sources/{source['id']}").json()


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


# --- media (Range) -------------------------------------------------------------------------


def media_source(client: TestClient, tmp_path: Path, name: str = "color_gop.mp4") -> Any:
    """``(url, path, bytes)`` of a registered private copy of a fixture."""
    path = copy_fixture(name, tmp_path / "media")
    source = register_ok(client, path)
    return f"/v1/sources/{source['id']}/media", path, path.read_bytes()


def test_media_without_range_is_the_whole_file(client: TestClient, tmp_path: Path) -> None:
    url, _, data = media_source(client, tmp_path)
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-length"] == str(len(data))
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"] == "video/mp4"
    assert "content-range" not in response.headers


def test_media_larger_than_one_chunk_is_streamed_whole(client: TestClient, tmp_path: Path) -> None:
    url, _, data = media_source(client, tmp_path, "two_tracks.mkv")
    assert len(data) > 2 * 1024 * 1024
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "video/x-matroska"

    start = 1024 * 1024 - 5
    response = client.get(url, headers={"Range": f"bytes={start}-{start + 9}"})
    assert response.status_code == 206
    assert response.content == data[start : start + 10]


def test_media_small_chunks_cover_the_exact_range(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("clipforge.api.media.CHUNK_SIZE", 1000)
    url, _, data = media_source(client, tmp_path)
    assert client.get(url).content == data
    response = client.get(url, headers={"Range": "bytes=1234-5678"})
    assert response.content == data[1234:5679]


def test_media_explicit_range_is_206_with_exact_bytes(client: TestClient, tmp_path: Path) -> None:
    url, _, data = media_source(client, tmp_path)
    response = client.get(url, headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    assert response.content == data[:100]
    assert response.headers["content-range"] == f"bytes 0-99/{len(data)}"
    assert response.headers["content-length"] == "100"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-type"] == "video/mp4"

    response = client.get(url, headers={"Range": "bytes=100-100"})
    assert response.status_code == 206
    assert response.content == data[100:101]


def test_media_suffix_range_returns_the_tail(client: TestClient, tmp_path: Path) -> None:
    url, _, data = media_source(client, tmp_path)
    response = client.get(url, headers={"Range": "bytes=-100"})
    assert response.status_code == 206
    assert response.content == data[-100:]
    tail_start = len(data) - 100
    assert response.headers["content-range"] == f"bytes {tail_start}-{len(data) - 1}/{len(data)}"
    assert response.headers["content-length"] == "100"

    # A suffix longer than the file is the whole file.
    response = client.get(url, headers={"Range": f"bytes=-{len(data) + 1000}"})
    assert response.status_code == 206
    assert response.content == data
    assert response.headers["content-range"] == f"bytes 0-{len(data) - 1}/{len(data)}"


def test_media_open_ended_range_runs_to_the_end(client: TestClient, tmp_path: Path) -> None:
    url, _, data = media_source(client, tmp_path)
    response = client.get(url, headers={"Range": "bytes=1000-"})
    assert response.status_code == 206
    assert response.content == data[1000:]
    assert response.headers["content-range"] == f"bytes 1000-{len(data) - 1}/{len(data)}"

    last = len(data) - 1
    response = client.get(url, headers={"Range": f"bytes={last}-"})
    assert response.status_code == 206
    assert response.content == data[last:]


def test_media_range_end_past_the_file_is_clamped(client: TestClient, tmp_path: Path) -> None:
    url, _, data = media_source(client, tmp_path)
    for end in (len(data), len(data) + 12345, "9" * 40):
        response = client.get(url, headers={"Range": f"bytes=10-{end}"})
        assert response.status_code == 206, end
        assert response.content == data[10:]
        assert response.headers["content-range"] == f"bytes 10-{len(data) - 1}/{len(data)}"


def test_media_range_unit_is_case_insensitive(client: TestClient, tmp_path: Path) -> None:
    url, _, data = media_source(client, tmp_path)
    response = client.get(url, headers={"Range": "Bytes=0-9"})
    assert response.status_code == 206
    assert response.content == data[:10]


def test_media_start_at_or_past_the_end_is_416(client: TestClient, tmp_path: Path) -> None:
    url, path, data = media_source(client, tmp_path)
    for header in (f"bytes={len(data)}-", f"bytes={len(data)}-{len(data) + 5}", "bytes=999999999-"):
        response = client.get(url, headers={"Range": header})
        assert response.status_code == 416, header
        assert response.headers["content-range"] == f"bytes */{len(data)}"
        assert response.headers["accept-ranges"] == "bytes"
        error = error_of(response)
        assert error["code"] == "RANGE_NOT_SATISFIABLE"
        assert error["retryable"] is False
        assert str(path) not in response.text
        assert path.name not in response.text


@pytest.mark.parametrize(
    "header",
    [
        "bytes=0-9,20-29",
        "bytes=0-9, 20-29",
        "bytes=-5,-10",
        "bytes=",
        "bytes=-",
        "bytes=abc",
        "bytes=a-b",
        "bytes=5-2",
        "bytes=-0",
        "bytes=--5",
        "bytes=0--5",
        "items=0-9",
        "0-9",
        "",
    ],
)
def test_media_multi_range_and_malformed_range_are_416(
    client: TestClient, tmp_path: Path, header: str
) -> None:
    url, _, data = media_source(client, tmp_path)
    response = client.get(url, headers={"Range": header})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(data)}"
    assert response.headers["accept-ranges"] == "bytes"
    assert error_of(response)["code"] == "RANGE_NOT_SATISFIABLE"


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("clip.MP4", "video/mp4"),
        ("clip.mov", "video/quicktime"),
        ("clip.ts", "video/mp2t"),
        ("clip.dat", "application/octet-stream"),
        ("clip", "application/octet-stream"),
    ],
)
def test_media_content_type_follows_the_extension(
    client: TestClient, tmp_path: Path, name: str, content_type: str
) -> None:
    path = copy_fixture("color_gop.mp4", tmp_path / "media", name)
    source = register_ok(client, path)
    response = client.get(f"/v1/sources/{source['id']}/media")
    assert response.status_code == 200
    assert response.headers["content-type"] == content_type
    assert response.content == path.read_bytes()


def test_media_unknown_source_is_404(client: TestClient) -> None:
    response = client.get("/v1/sources/nope/media")
    assert response.status_code == 404
    assert error_of(response)["code"] == "SOURCE_NOT_FOUND"
    # The id is checked before the Range header.
    assert client.get("/v1/sources/nope/media", headers={"Range": "bytes=0-9"}).status_code == 404


def test_media_needs_the_session_token(anonymous: TestClient) -> None:
    assert anonymous.get("/v1/sources/nope/media").status_code == 401


def test_byte_range_parser_accepts_only_ascii_digits() -> None:
    assert parse_byte_range("bytes=0-9", 100) == ByteRange(0, 9)
    assert parse_byte_range(" bytes=95- ", 100) == ByteRange(95, 99)
    assert parse_byte_range("bytes=-10", 100) == ByteRange(90, 99)
    for header in ("bytes=١-٩", "bytes=0-9x", "bytes=-5-"):
        with pytest.raises(RangeNotSatisfiableError):
            parse_byte_range(header, 100)
    with pytest.raises(RangeNotSatisfiableError):
        parse_byte_range("bytes=0-0", 0)
    with pytest.raises(RangeNotSatisfiableError):
        parse_byte_range("bytes=-5", 0)


def test_media_of_a_deleted_file_is_source_unavailable(client: TestClient, tmp_path: Path) -> None:
    url, path, _ = media_source(client, tmp_path)
    path.unlink()
    for headers in ({}, {"Range": "bytes=0-9"}):
        response = client.get(url, headers=headers)
        assert response.status_code == 400
        error = error_of(response)
        assert error["code"] == "SOURCE_UNAVAILABLE"
        assert error["details"] == {}
        assert str(path) not in response.text
        assert path.name not in response.text
        assert str(tmp_path) not in response.text


def test_media_of_a_changed_file_is_source_changed(client: TestClient, tmp_path: Path) -> None:
    url, path, _ = media_source(client, tmp_path)
    original = path.stat()

    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns + 5_000_000_000))
    response = client.get(url)
    assert response.status_code == 400
    assert error_of(response)["code"] == "SOURCE_CHANGED"
    assert str(path) not in response.text

    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert client.get(url).status_code == 200

    with path.open("ab") as handle:
        handle.write(b"x")
    response = client.get(url, headers={"Range": "bytes=0-9"})
    assert response.status_code == 400
    assert error_of(response)["code"] == "SOURCE_CHANGED"


def test_media_of_a_directory_at_the_stored_path_is_source_unavailable(
    client: TestClient, tmp_path: Path
) -> None:
    url, path, _ = media_source(client, tmp_path)
    path.unlink()
    path.mkdir()
    response = client.get(url)
    assert response.status_code == 400
    assert error_of(response)["code"] == "SOURCE_UNAVAILABLE"


def test_media_reads_leave_the_original_untouched(client: TestClient, tmp_path: Path) -> None:
    url, path, data = media_source(client, tmp_path)
    before = (sha256(path), path.stat().st_mtime_ns, path.stat().st_size)

    for header in (None, "bytes=0-99", "bytes=-100", "bytes=50-", "bytes=0-9,20-29", "bytes=-0"):
        client.get(url, headers={} if header is None else {"Range": header})
    assert client.get(url).content == data

    assert (sha256(path), path.stat().st_mtime_ns, path.stat().st_size) == before
    # Nothing was left open: the file can be replaced right away (Windows would refuse otherwise).
    path.unlink()
    path.write_bytes(data)
    os.utime(path, ns=(path.stat().st_atime_ns, before[1]))
    assert client.get(url).content == data


def test_media_has_no_path_parameter_and_ignores_extra_input(
    client: TestClient, tmp_path: Path
) -> None:
    url, _, data = media_source(client, tmp_path)
    other = copy_fixture("no_audio.mp4", tmp_path / "other")
    response = client.get(url, params={"path": str(other)})
    assert response.status_code == 200
    assert response.content == data
    assert client.get("/v1/sources/..%2F..%2Fetc/media").status_code == 404
