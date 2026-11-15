"""Preview proxies on real FFmpeg and copies of the generated fixtures (ADR 021, FR-029).

POST /v1/sources/{id}/previews, GET /v1/previews/{id}, GET /v1/previews/{id}/media and the
``preview`` job (cancel, restart recovery). Fixtures come from ``npm run fixtures``; every test
registers a private copy and checks that the original's bytes and mtime stay as they were.
"""

from __future__ import annotations

import itertools
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from clipforge.adapters.media_tools import MediaTools
from clipforge.adapters.process import ProcessResult, run_process
from clipforge.services import export_engine
from clipforge.services import previews as previews_module
from clipforge.services.export_engine import OutputInvalidError
from clipforge.services.preview_engine import PreviewProgressCallback, PreviewRequest
from clipforge.services.preview_engine import build_preview as real_build_preview
from clipforge.services.runtime import Runtime
from clipforge.storage import JobRepository, PreviewRepository

from .support import RuntimeFactory, client_for
from .test_api_sources import (
    copy_fixture,
    error_of,
    new_project,
    register,
    sha256,
    wait_job,
)
from .test_export_engine import (
    audio_samples,
    container_duration,
    ffprobe_json,
    frequency,
    make_media,
    rms,
    stream_types,
    video_pts,
)
from .test_storage_support import add_project, add_source

WAIT_S = 60.0
ONE_FRAME_S = 1 / 30
VIDEO_AND_AAC = [("video", "h264"), ("audio", "aac")]
VIDEO_ONLY = [("video", "h264")]


# --- helpers ---------------------------------------------------------------------------------


class RenderProbe:
    """Stands in for ``build_preview`` in the preview service and delegates to the real engine."""

    def __init__(self) -> None:
        self.requests: list[PreviewRequest] = []
        self.entered = threading.Event()
        """Set when the first render has been asked for."""
        self.rendering = threading.Event()
        """Set when FFmpeg reports progress."""
        self.release = threading.Event()
        self.hold = False
        """While true a render waits (before the engine starts) for ``release`` or its cancel."""

    def __call__(
        self,
        tools: MediaTools,
        request: PreviewRequest,
        *,
        cancel: threading.Event | None = None,
        on_progress: PreviewProgressCallback | None = None,
    ) -> Any:
        self.requests.append(request)
        self.entered.set()
        deadline = time.monotonic() + WAIT_S
        while self.hold and not self.release.is_set():
            if cancel is not None and cancel.is_set():
                break
            if time.monotonic() > deadline:
                raise RuntimeError("the test never released the render")
            time.sleep(0.01)

        def watch(progress: Any) -> None:
            self.rendering.set()
            if on_progress is not None:
                on_progress(progress)

        return real_build_preview(tools, request, cancel=cancel, on_progress=watch)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Iterator[RenderProbe]:
    wrapper = RenderProbe()
    monkeypatch.setattr(previews_module, "build_preview", wrapper)
    yield wrapper
    wrapper.release.set()  # never leave a render waiting after a failed test


@pytest.fixture(scope="module")
def heavy_source(media_tools: MediaTools, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 30 s 1080p video-only file: its proxy takes long enough to be cancelled."""
    return make_media(
        media_tools,
        tmp_path_factory.mktemp("heavy") / "heavy.mp4",
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    )  # fmt: skip


def cache_dir(runtime: Runtime) -> Path:
    return runtime.database.data_dir / "cache" / "previews"


def cache_names(runtime: Runtime) -> list[str]:
    directory = cache_dir(runtime)
    return sorted(os.listdir(directory)) if directory.is_dir() else []


def make_source(
    client: TestClient,
    directory: Path,
    name: str = "color_gop.mp4",
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Register a private copy of a fixture; returns the source JSON (its ``path`` is the copy)."""
    path = copy_fixture(name, directory / "media")
    response = register(client, project_id or new_project(client), path)
    assert response.status_code == 201, response.text
    source: dict[str, Any] = response.json()
    return source


def post_preview(client: TestClient, source_id: str, audio: object) -> Response:
    return client.post(f"/v1/sources/{source_id}/previews", json={"audio_stream_index": audio})


def begin(client: TestClient, source: dict[str, Any], audio: int | None) -> dict[str, Any]:
    response = post_preview(client, source["id"], audio)
    assert response.status_code == 202, response.text
    created: dict[str, Any] = response.json()
    return created


def finish(client: TestClient, created: dict[str, Any]) -> dict[str, Any]:
    """Wait for the job of a created preview; returns the job."""
    return wait_job(client, created["job_id"])


def make_ready(client: TestClient, source: dict[str, Any], audio: int | None) -> dict[str, Any]:
    created = begin(client, source, audio)
    job = finish(client, created)
    assert job["state"] == "succeeded", job
    return created


def get_preview(client: TestClient, preview_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/previews/{preview_id}")
    assert response.status_code == 200, response.text
    preview: dict[str, Any] = response.json()
    return preview


def download(client: TestClient, preview_id: str, directory: Path) -> Path:
    response = client.get(f"/v1/previews/{preview_id}/media")
    assert response.status_code == 200, response.text
    target = directory / f"{preview_id}.mp4"
    target.write_bytes(response.content)
    return target


def fingerprint(path: Path) -> tuple[str, int, int]:
    state = path.stat()
    return sha256(path), state.st_mtime_ns, state.st_size


def keyframe_times(tools: MediaTools, path: Path) -> list[float]:
    document = ffprobe_json(
        tools, path, "-select_streams", "v:0", "-show_entries", "packet=pts_time,flags"
    )
    packets = document["packets"]
    assert isinstance(packets, list)
    return sorted(float(p["pts_time"]) for p in packets if "K" in str(p["flags"]))


def video_stream(tools: MediaTools, path: Path) -> dict[str, Any]:
    streams: list[dict[str, Any]] = ffprobe_json(tools, path, "-show_streams")["streams"]  # type: ignore[assignment]
    return next(stream for stream in streams if stream["codec_type"] == "video")


def audio_streams(tools: MediaTools, path: Path) -> list[dict[str, Any]]:
    streams: list[dict[str, Any]] = ffprobe_json(tools, path, "-show_streams")["streams"]  # type: ignore[assignment]
    return [stream for stream in streams if stream["codec_type"] == "audio"]


def assert_proxy_of(
    tools: MediaTools, proxy: Path, source: dict[str, Any], types: list[tuple[str, str]]
) -> None:
    """Common promises of every proxy: codecs, size, one keyframe a second, ``+faststart``,
    a timeline from zero and the duration of the source."""
    assert stream_types(tools, proxy) == types
    video = video_stream(tools, proxy)
    assert int(video["height"]) <= 720
    assert int(video["width"]) % 2 == 0
    assert int(video["height"]) % 2 == 0
    for stream in audio_streams(tools, proxy):
        assert int(stream["channels"]) == 2
    assert abs(container_duration(tools, proxy) - source["duration_ms"] / 1000) <= ONE_FRAME_S
    assert video_pts(tools, proxy)[0] == 0
    keys = keyframe_times(tools, proxy)
    assert keys[0] == 0
    assert all(b - a <= 1.0 + 0.07 for a, b in itertools.pairwise(keys))
    data = proxy.read_bytes()
    assert 0 <= data.index(b"moov") < data.index(b"mdat")


# --- the proxy itself ------------------------------------------------------------------------


def test_audio_offset_proxy_is_h264_aac_with_a_silent_start_of_1_5_seconds(
    client: TestClient, runtime: Runtime, media_tools: MediaTools, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path, "audio_offset.mkv")
    original = Path(source["path"])
    before = fingerprint(original)
    assert [stream["codec"] for stream in source["audio_streams"]] == ["pcm_s16le"]

    response = post_preview(client, source["id"], 1)
    assert response.status_code == 202
    created = response.json()
    assert set(created) == {"id", "job_id", "status"}
    assert created["status"] == "queued"
    job = finish(client, created)

    assert job["state"] == "succeeded", job
    assert job["type"] == "preview"
    assert job["error"] is None
    assert job["progress"] == {"stage": "done", "percent": 100}
    assert get_preview(client, created["id"]) == {
        "id": created["id"],
        "source_id": source["id"],
        "audio_stream_index": 1,
        "status": "ready",
        "job_id": created["job_id"],
        "error": None,
    }
    proxy = download(client, created["id"], tmp_path)
    assert_proxy_of(media_tools, proxy, source, VIDEO_AND_AAC)
    video = video_stream(media_tools, proxy)
    assert (int(video["width"]), int(video["height"])) == (320, 180)  # never upscaled

    # The 1000 Hz audio starts 1.5 s into the source: the proxy is silent until then.
    samples = audio_samples(media_tools, proxy)
    assert rms(samples, 8000, 0.1, 1.4) < 0.001
    assert rms(samples, 8000, 1.7, 11.5) > 0.02
    assert abs(frequency(samples, 8000, 1.7, 11.5) - 1000) <= 20

    assert cache_names(runtime) == [f"{created['id']}.mp4"]
    assert (cache_dir(runtime) / f"{created['id']}.mp4").read_bytes() == proxy.read_bytes()
    assert fingerprint(original) == before


@pytest.mark.parametrize(("stream", "expected_hz"), [(1, 220), (2, 880)])
def test_two_tracks_proxy_keeps_the_selected_audio_stream(
    client: TestClient, media_tools: MediaTools, tmp_path: Path, stream: int, expected_hz: int
) -> None:
    source = make_source(client, tmp_path, "two_tracks.mkv")
    assert [a["index"] for a in source["audio_streams"]] == [1, 2]
    before = fingerprint(Path(source["path"]))

    created = make_ready(client, source, stream)

    proxy = download(client, created["id"], tmp_path)
    assert_proxy_of(media_tools, proxy, source, VIDEO_AND_AAC)
    samples = audio_samples(media_tools, proxy)
    assert abs(frequency(samples, 8000, 0.3, 11.5) - expected_hz) <= 15
    assert rms(samples, 8000, 0.3, 11.5) > 0.02
    assert fingerprint(Path(source["path"])) == before


@pytest.mark.parametrize(
    ("name", "audio"),
    [("no_audio.mp4", None), ("two_tracks.mkv", None)],
    ids=["source-without-audio", "audio-not-wanted"],
)
def test_a_proxy_without_audio_has_only_the_video_stream(
    client: TestClient, media_tools: MediaTools, tmp_path: Path, name: str, audio: int | None
) -> None:
    source = make_source(client, tmp_path, name)
    created = make_ready(client, source, audio)
    proxy = download(client, created["id"], tmp_path)
    assert_proxy_of(media_tools, proxy, source, VIDEO_ONLY)
    assert get_preview(client, created["id"])["audio_stream_index"] is None


def test_variable_frame_rate_is_kept(
    client: TestClient, media_tools: MediaTools, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path, "vfr.mkv")
    created = make_ready(client, source, None)
    proxy = download(client, created["id"], tmp_path)
    assert_proxy_of(media_tools, proxy, source, VIDEO_ONLY)
    pts = video_pts(media_tools, proxy)
    intervals = {round(float(b - a), 3) for a, b in itertools.pairwise(pts)}
    assert any(abs(value - 0.033) <= 0.003 for value in intervals), intervals
    assert any(abs(value - 0.067) <= 0.003 for value in intervals), intervals
    assert len(pts) == 180  # no frame was dropped or duplicated


def test_a_source_taller_than_720_lines_is_scaled_down_with_its_aspect(
    client: TestClient, media_tools: MediaTools, tmp_path: Path
) -> None:
    (tmp_path / "media").mkdir()
    big = make_media(
        media_tools, tmp_path / "media" / "big.mp4",
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
    )  # fmt: skip
    response = register(client, new_project(client), big)
    assert response.status_code == 201, response.text
    source = response.json()
    created = make_ready(client, source, 1)
    proxy = download(client, created["id"], tmp_path)
    assert_proxy_of(media_tools, proxy, source, VIDEO_AND_AAC)
    video = video_stream(media_tools, proxy)
    assert (int(video["width"]), int(video["height"])) == (1280, 720)


def test_progress_while_rendering_names_the_stage_and_a_percent(
    client: TestClient, probe: RenderProbe, heavy_source: Path
) -> None:
    response = register(client, new_project(client), heavy_source)
    assert response.status_code == 201, response.text
    created = begin(client, response.json(), None)
    assert probe.rendering.wait(WAIT_S)
    deadline = time.monotonic() + WAIT_S
    job = client.get(f"/v1/jobs/{created['job_id']}").json()
    while job["state"] == "running" and job["progress"].get("percent", 0) == 0:
        assert time.monotonic() < deadline
        time.sleep(0.05)
        job = client.get(f"/v1/jobs/{created['job_id']}").json()
    if job["state"] == "running":
        assert job["progress"]["stage"] == "render"
        assert 0 < job["progress"]["percent"] <= 100
    assert client.post(f"/v1/jobs/{created['job_id']}/cancel", json={}).status_code == 202
    assert finish(client, created)["state"] in ("cancelled", "succeeded")


# --- reuse -----------------------------------------------------------------------------------


def test_a_repeated_post_returns_the_same_ready_preview_with_200(
    client: TestClient, runtime: Runtime, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path, "two_tracks.mkv")
    first = make_ready(client, source, 1)

    again = post_preview(client, source["id"], 1)

    assert again.status_code == 200
    assert again.json() == {"id": first["id"], "job_id": first["job_id"], "status": "ready"}
    other = begin(client, source, 2)  # another audio stream is another preview
    assert other["id"] != first["id"]
    assert finish(client, other)["state"] == "succeeded"
    assert post_preview(client, source["id"], 2).json()["id"] == other["id"]
    assert cache_names(runtime) == sorted([f"{first['id']}.mp4", f"{other['id']}.mp4"])


def test_a_post_while_the_same_preview_is_rendering_returns_it_without_a_second_job(
    client: TestClient, probe: RenderProbe, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path)
    probe.hold = True
    first = begin(client, source, 1)
    assert probe.entered.wait(WAIT_S)

    again = post_preview(client, source["id"], 1)

    assert again.status_code == 202
    assert again.json()["id"] == first["id"]
    assert again.json()["job_id"] == first["job_id"]
    assert again.json()["status"] == "running"
    probe.release.set()
    assert finish(client, first)["state"] == "succeeded"
    assert len(probe.requests) == 1


def test_a_missing_or_resized_proxy_file_is_rendered_again(
    client: TestClient, runtime: Runtime, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path)
    first = make_ready(client, source, 1)
    proxy = cache_dir(runtime) / f"{first['id']}.mp4"

    with proxy.open("ab") as handle:
        handle.write(b"x")
    assert client.get(f"/v1/previews/{first['id']}/media").status_code == 409
    second = post_preview(client, source["id"], 1)
    assert second.status_code == 202
    assert second.json()["id"] != first["id"]
    assert finish(client, second.json())["state"] == "succeeded"

    proxy_2 = cache_dir(runtime) / f"{second.json()['id']}.mp4"
    proxy_2.unlink()
    third = post_preview(client, source["id"], 1)
    assert third.status_code == 202
    assert third.json()["id"] not in (first["id"], second.json()["id"])
    assert finish(client, third.json())["state"] == "succeeded"


def test_a_source_that_changed_after_registration_is_refused_even_with_a_ready_preview(
    client: TestClient, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path)
    path = Path(source["path"])
    first = make_ready(client, source, 1)
    state = path.stat()

    os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns + 5_000_000_000))
    changed = post_preview(client, source["id"], 1)
    assert changed.status_code == 400
    assert error_of(changed)["code"] == "SOURCE_CHANGED"
    assert str(path) not in changed.text

    os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns))
    restored = post_preview(client, source["id"], 1)
    assert restored.status_code == 200
    assert restored.json()["id"] == first["id"]

    path.unlink()
    gone = post_preview(client, source["id"], 1)
    assert gone.status_code == 400
    assert error_of(gone)["code"] == "SOURCE_UNAVAILABLE"
    assert path.name not in gone.text


# --- media -----------------------------------------------------------------------------------


def test_media_serves_the_proxy_with_the_range_semantics_of_a_source(
    client: TestClient, runtime: Runtime, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path)
    created = make_ready(client, source, 1)
    url = f"/v1/previews/{created['id']}/media"
    data = (cache_dir(runtime) / f"{created['id']}.mp4").read_bytes()

    whole = client.get(url)
    assert whole.status_code == 200
    assert whole.content == data
    assert whole.headers["content-length"] == str(len(data))
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["content-type"] == "video/mp4"
    assert "content-range" not in whole.headers

    part = client.get(url, headers={"Range": "bytes=10-99"})
    assert part.status_code == 206
    assert part.content == data[10:100]
    assert part.headers["content-range"] == f"bytes 10-99/{len(data)}"
    assert part.headers["content-type"] == "video/mp4"

    tail = client.get(url, headers={"Range": "bytes=-50"})
    assert tail.status_code == 206
    assert tail.content == data[-50:]
    assert client.get(url, headers={"Range": f"bytes={len(data) - 1}-"}).content == data[-1:]

    for header in (f"bytes={len(data)}-", "bytes=0-9,20-29", "bytes=5-2", "items=0-9", ""):
        response = client.get(url, headers={"Range": header})
        assert response.status_code == 416, header
        assert response.headers["content-range"] == f"bytes */{len(data)}"
        assert error_of(response)["code"] == "RANGE_NOT_SATISFIABLE"


def test_media_is_409_until_the_preview_is_ready(
    client: TestClient, probe: RenderProbe, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path)
    probe.hold = True
    created = begin(client, source, 1)
    assert probe.entered.wait(WAIT_S)
    url = f"/v1/previews/{created['id']}/media"

    for headers in ({}, {"Range": "bytes=0-9"}):
        waiting = client.get(url, headers=headers)
        assert waiting.status_code == 409
        error = error_of(waiting)
        assert error["code"] == "PREVIEW_NOT_READY"
        assert error["retryable"] is True
        assert error["details"] == {}
    assert get_preview(client, created["id"])["status"] == "running"

    probe.release.set()
    assert finish(client, created)["state"] == "succeeded"
    assert client.get(url).status_code == 200


def test_media_of_a_cancelled_failed_or_missing_preview(
    client: TestClient, runtime: Runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path)

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise OutputInvalidError("broken")

    monkeypatch.setattr(previews_module, "build_preview", failing)
    created = begin(client, source, 1)
    assert finish(client, created)["state"] == "failed"
    failed = client.get(f"/v1/previews/{created['id']}/media")
    assert failed.status_code == 409
    assert error_of(failed)["code"] == "PREVIEW_NOT_READY"
    assert error_of(failed)["retryable"] is False

    monkeypatch.setattr(previews_module, "build_preview", real_build_preview)
    ready = make_ready(client, source, 1)
    (cache_dir(runtime) / f"{ready['id']}.mp4").unlink()
    gone = client.get(f"/v1/previews/{ready['id']}/media")
    assert gone.status_code == 409
    assert error_of(gone)["code"] == "PREVIEW_NOT_READY"
    assert error_of(gone)["retryable"] is False


def test_unknown_ids_are_404_and_the_id_is_checked_before_the_range(
    client: TestClient, tmp_path: Path
) -> None:
    for response in (
        client.get("/v1/previews/nope"),
        client.get("/v1/previews/nope/media"),
        client.get("/v1/previews/nope/media", headers={"Range": "bytes=0-9"}),
        client.get("/v1/previews/..%2F..%2Fetc/media"),
    ):
        assert response.status_code == 404
    assert error_of(client.get("/v1/previews/nope"))["code"] == "PREVIEW_NOT_FOUND"
    unknown_source = post_preview(client, "nope", None)
    assert unknown_source.status_code == 404
    assert error_of(unknown_source)["code"] == "SOURCE_NOT_FOUND"


def test_preview_routes_need_the_session_token(anonymous: TestClient) -> None:
    assert (
        anonymous.post("/v1/sources/x/previews", json={"audio_stream_index": None}).status_code
        == 401
    )
    assert anonymous.get("/v1/previews/x").status_code == 401
    assert anonymous.get("/v1/previews/x/media").status_code == 401


# --- request validation ----------------------------------------------------------------------


def test_an_audio_stream_the_source_does_not_have_is_422_and_nothing_is_created(
    client: TestClient, runtime: Runtime, tmp_path: Path
) -> None:
    with_audio = make_source(client, tmp_path, "two_tracks.mkv")
    without_audio = make_source(client, tmp_path, "no_audio.mp4")

    for source, audio in ((with_audio, 0), (with_audio, 3), (with_audio, 99), (without_audio, 1)):
        response = post_preview(client, source["id"], audio)
        assert response.status_code == 422, (source["id"], audio)
        error = error_of(response)
        assert error["code"] == "INVALID_AUDIO_STREAM"
        assert error["details"] == {"field": "audio_stream_index"}
    with runtime.database.session() as session:
        assert PreviewRepository(session).list_with_status(("queued", "running", "ready")) == []
    assert cache_names(runtime) == []


@pytest.mark.parametrize(
    "payload",
    [{}, {"audio_stream_index": "1"}, {"audio_stream_index": True}, {"audio_stream_index": 1.5},
     {"audio_stream_index": 1, "force": True}],
)  # fmt: skip
def test_a_malformed_body_is_a_schema_error(
    client: TestClient, tmp_path: Path, payload: object
) -> None:
    source = make_source(client, tmp_path)
    response = client.post(f"/v1/sources/{source['id']}/previews", json=payload)
    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"


def test_without_ffmpeg_creating_a_preview_is_503(
    make_runtime: RuntimeFactory, tmp_path: Path
) -> None:
    runtime = make_runtime(with_tools=False)
    client = client_for(runtime)
    response = post_preview(client, "any", None)
    assert response.status_code == 503
    assert error_of(response)["code"] == "MEDIA_TOOLS_UNAVAILABLE"
    assert client.get("/v1/previews/any").status_code == 404  # reading still works


# --- failures --------------------------------------------------------------------------------


def test_an_invalid_proxy_fails_the_job_and_leaves_nothing_in_the_cache(
    client: TestClient, runtime: Runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path)
    real: Callable[..., ProcessResult] = run_process

    def run_then_truncate(args: Sequence[str], **kwargs: object) -> ProcessResult:
        result = real(args, **kwargs)
        if "-filter_complex" in args:
            target = Path(args[-1].removeprefix("file:"))
            target.write_bytes(target.read_bytes()[:100])
        return result

    monkeypatch.setattr(export_engine, "run_process", run_then_truncate)
    created = begin(client, source, 1)
    job = finish(client, created)

    expected = {"code": "OUTPUT_INVALID", "message": "Створене прев'ю не пройшло перевірку"}
    assert job["state"] == "failed"
    assert job["error"] == expected
    assert job["progress"]["stage"] == "failed"
    preview = get_preview(client, created["id"])
    assert (preview["status"], preview["error"]) == ("failed", expected)
    assert cache_names(runtime) == []
    assert str(tmp_path) not in client.get(f"/v1/previews/{created['id']}").text


def test_a_full_disk_has_its_own_code(
    client: TestClient, runtime: Runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path)

    def fake(args: Sequence[str], **kwargs: object) -> ProcessResult:
        return ProcessResult(
            exit_code=1,
            cancelled=False,
            timed_out=False,
            stderr_tail="av_interleaved_write_frame(): No space left on device",
        )

    monkeypatch.setattr(export_engine, "run_process", fake)
    created = begin(client, source, None)
    job = finish(client, created)
    assert job["state"] == "failed"
    assert job["error"] == {
        "code": "DISK_FULL",
        "message": "Недостатньо місця на диску для прев'ю",
    }
    assert get_preview(client, created["id"])["error"] == job["error"]
    assert cache_names(runtime) == []


def test_an_hdr_source_has_no_preview(
    client: TestClient, runtime: Runtime, media_tools: MediaTools, tmp_path: Path
) -> None:
    (tmp_path / "media").mkdir()
    hdr = make_media(
        media_tools, tmp_path / "media" / "hdr.mp4",
        "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=30:d=1",
        "-vf", "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
    )  # fmt: skip
    response = register(client, new_project(client), hdr)
    assert response.status_code == 201, response.text
    created = begin(client, response.json(), None)
    job = finish(client, created)
    assert job["state"] == "failed"
    assert job["error"]["code"] == "HDR_UNSUPPORTED"
    assert cache_names(runtime) == []


def test_a_source_that_changes_while_the_preview_waits_fails_the_job(
    client: TestClient, runtime: Runtime, probe: RenderProbe, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path)
    path = Path(source["path"])
    probe.hold = True
    created = begin(client, source, 1)
    assert probe.entered.wait(WAIT_S)
    state = path.stat()
    os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns + 5_000_000_000))
    probe.release.set()

    job = finish(client, created)

    assert job["state"] == "failed"
    assert job["error"]["code"] == "SOURCE_CHANGED"
    assert cache_names(runtime) == []


def test_an_unexpected_error_is_a_generic_failure_without_its_text(
    client: TestClient,
    runtime: Runtime,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    source = make_source(client, tmp_path)

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"secret {tmp_path}")

    monkeypatch.setattr(previews_module, "build_preview", explode)
    with caplog.at_level(logging.DEBUG):
        created = begin(client, source, 1)
        job = finish(client, created)

    assert job["state"] == "failed"
    assert job["error"] == {"code": "PREVIEW_FAILED", "message": "Не вдалося створити прев'ю"}
    assert get_preview(client, created["id"])["error"] == job["error"]
    assert "secret" not in caplog.text
    assert str(tmp_path) not in caplog.text


# --- cancel ----------------------------------------------------------------------------------


def test_cancel_stops_the_render_and_leaves_no_file(
    client: TestClient, runtime: Runtime, probe: RenderProbe, heavy_source: Path
) -> None:
    response = register(client, new_project(client), heavy_source)
    assert response.status_code == 201, response.text
    source = response.json()
    before = fingerprint(heavy_source)
    created = begin(client, source, None)
    assert probe.rendering.wait(WAIT_S), "FFmpeg never reported progress"
    assert cache_names(runtime) == [f".clipforge-{created['id']}.part"]  # the engine's temp file

    cancelled = client.post(f"/v1/jobs/{created['job_id']}/cancel", json={})

    assert cancelled.status_code == 202
    assert cancelled.json()["state"] == "cancelling"
    job = finish(client, created)
    assert job["state"] == "cancelled"
    assert job["error"] is None
    assert get_preview(client, created["id"])["status"] == "cancelled"
    assert get_preview(client, created["id"])["error"] is None
    assert cache_names(runtime) == []  # neither the temp file nor a final file
    assert fingerprint(heavy_source) == before
    # Cancelling again changes nothing, and a cancelled preview is not reused.
    assert client.post(f"/v1/jobs/{created['job_id']}/cancel", json={}).status_code == 202
    probe.hold = False
    again = begin(client, source, None)
    assert again["id"] != created["id"]
    assert client.post(f"/v1/jobs/{again['job_id']}/cancel", json={}).status_code == 202
    assert finish(client, again)["state"] in ("cancelled", "succeeded")


def test_cancelling_a_queued_preview_cancels_it_without_rendering(
    client: TestClient, runtime: Runtime, probe: RenderProbe, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path, "two_tracks.mkv")
    probe.hold = True
    first = begin(client, source, 1)
    assert probe.entered.wait(WAIT_S)
    second = begin(client, source, 2)
    assert get_preview(client, second["id"])["status"] == "queued"

    cancelled = client.post(f"/v1/jobs/{second['job_id']}/cancel", json={})

    assert cancelled.status_code == 202
    assert cancelled.json()["state"] == "cancelled"
    assert get_preview(client, second["id"])["status"] == "cancelled"
    probe.release.set()
    assert finish(client, first)["state"] == "succeeded"
    # The worker has passed the cancelled job; a cancelled preview is not reused, so the same
    # stream gets a new preview.
    third = begin(client, source, 2)
    assert third["id"] != second["id"]
    assert finish(client, third)["state"] == "succeeded"
    assert client.get(f"/v1/jobs/{second['job_id']}").json()["state"] == "cancelled"
    assert [request.task_id for request in probe.requests] == [first["id"], third["id"]]
    assert cache_names(runtime) == sorted([f"{first['id']}.mp4", f"{third['id']}.mp4"])


def test_cancel_of_a_finished_preview_job_is_409(client: TestClient, tmp_path: Path) -> None:
    created = make_ready(client, make_source(client, tmp_path), 1)
    response = client.post(f"/v1/jobs/{created['job_id']}/cancel", json={})
    assert response.status_code == 409
    assert error_of(response)["code"] == "JOB_NOT_CANCELLABLE"
    assert get_preview(client, created["id"])["status"] == "ready"


# --- restart ---------------------------------------------------------------------------------


def test_restart_marks_unfinished_previews_interrupted_and_removes_only_the_engines_temp(
    make_runtime: RuntimeFactory, tmp_path: Path
) -> None:
    data_dir = tmp_path / "workspace"
    first = make_runtime(data_dir=data_dir)
    cache = cache_dir(first)
    cache.mkdir(parents=True)
    ids: dict[str, str] = {}
    jobs: dict[str, str] = {}
    with first.database.write_session() as session:
        source_id = add_source(session, add_project(session).id).id
        job_states = {"queued": "queued", "running": "running", "ready": "succeeded"}
        for audio, status in enumerate(("queued", "running", "ready")):
            job = JobRepository(session).add(type="preview", state=job_states[status])
            preview = PreviewRepository(session).add(
                source_id=source_id,
                audio_stream_index=audio,
                profile_version=1,
                job_id=job.id,
                source_size=1,
                source_mtime=1,
            )
            PreviewRepository(session).set_state(
                preview.id,
                status,
                file_size=len(b"a finished proxy") if status == "ready" else None,
            )
            ids[status], jobs[status] = preview.id, job.id
    own_temps = [cache / f".clipforge-{ids[status]}.part" for status in ("queued", "running")]
    for path in own_temps:
        path.write_bytes(b"half a render")
    stranger = cache / f".clipforge-{uuid.uuid4()}.part"  # a temp of nobody we know
    stranger.write_bytes(b"not ours")
    precious = cache / "precious.mp4"
    precious.write_bytes(b"not ours either")
    finished = cache / f"{ids['ready']}.mp4"
    finished.write_bytes(b"a finished proxy")
    first.close()  # the "crash": nothing finished the first two

    second = make_runtime(data_dir=data_dir)
    client = client_for(second)

    interrupted = {
        "code": "INTERRUPTED",
        "message": "Створення прев'ю перервано перезапуском застосунку",
    }
    for status in ("queued", "running"):
        view = get_preview(client, ids[status])
        assert (view["status"], view["error"]) == ("interrupted", interrupted)
        assert client.get(f"/v1/jobs/{jobs[status]}").json()["state"] == "interrupted"
        assert client.get(f"/v1/previews/{ids[status]}/media").status_code == 409
    ready = get_preview(client, ids["ready"])
    assert (ready["status"], ready["error"]) == ("ready", None)
    assert client.get(f"/v1/previews/{ids['ready']}/media").content == b"a finished proxy"
    assert not any(path.exists() for path in own_temps)
    assert stranger.read_bytes() == b"not ours"
    assert precious.read_bytes() == b"not ours either"
    assert finished.read_bytes() == b"a finished proxy"


def test_closing_the_runtime_during_a_render_stops_it_and_the_next_start_recovers(
    make_runtime: RuntimeFactory, probe: RenderProbe, heavy_source: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "workspace"
    first = make_runtime(data_dir=data_dir)
    client = client_for(first)
    response = register(client, new_project(client), heavy_source)
    assert response.status_code == 201, response.text
    created = begin(client, response.json(), None)
    assert probe.rendering.wait(WAIT_S)

    first.close()

    assert cache_names(first) == []  # the render was stopped and its temp file removed
    second = make_runtime(data_dir=data_dir)
    client = client_for(second)
    preview = get_preview(client, created["id"])
    assert preview["status"] == "interrupted"
    assert preview["error"]["code"] == "INTERRUPTED"
    assert client.get(f"/v1/jobs/{created['job_id']}").json()["state"] == "interrupted"
    assert cache_names(second) == []
