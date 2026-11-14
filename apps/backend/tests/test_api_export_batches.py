"""POST/GET /v1/export-batches, POST /v1/jobs/{id}/cancel and the export job (FR-036..FR-044,
docs/04 sections 4 and 5, ADR 020).

Renders are real FFmpeg runs on the generated fixtures. ``RenderProbe`` replaces the
``export_clip`` name inside ``clipforge.services.exports`` by a wrapper that still calls the real
engine: it records the requests and can hold a render, so the states between "queued" and
"verified" can be observed without sleeping.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import func, select

from clipforge.adapters.media_tools import MediaTools, run_short
from clipforge.adapters.process import FfmpegProgress
from clipforge.domain.catalog import CATALOG_COLUMNS
from clipforge.services import exports as exports_module
from clipforge.services.export_engine import (
    ExportError,
    ExportProgressCallback,
    ExportRequest,
    ExportResult,
    ExportStage,
    RenderFailedError,
    export_clip,
)
from clipforge.services.runtime import Runtime
from clipforge.storage import ClipRepository, ExportBatch, ExportRepository, Job, JobRepository

from .support import TOKEN, RuntimeFactory, client_for
from .test_api_clips import create as create_clip
from .test_api_clips import review as review_clip
from .test_api_sources import copy_fixture, error_of, new_project, register, sha256, wait_job
from .test_storage_support import add_clip, add_project, add_source

PROFILE: dict[str, Any] = {
    "video_codec": "h264",
    "quality": "high",
    "audio_mode": "selected",
    "audio_stream_index": 1,
    "write_srt": False,
}
NO_AUDIO: dict[str, Any] = {"audio_mode": "none", "audio_stream_index": None}
PROFILE_FIELDS = tuple(PROFILE)
ALL_BATCH_STATUSES = ("queued", "running", "succeeded", "partial_failed", "failed", "cancelled")
WAIT_S = 60.0


# --- helpers ---------------------------------------------------------------------------------


class RenderProbe:
    """Stands in for ``export_clip`` in the export service and delegates to the real engine."""

    def __init__(self) -> None:
        self.requests: list[ExportRequest] = []
        self.entered = threading.Event()
        """Set when the first render has been asked for."""
        self.rendering = threading.Event()
        """Set when FFmpeg reports progress for ``watch_clip``."""
        self.release = threading.Event()
        self.hold = False
        """While true a render waits (before FFmpeg starts) for ``release`` or for its cancel."""
        self.watch_clip: str | None = None
        self.hold_stage: str | None = None
        """FFmpeg's progress reader waits in this engine stage, after the service saw the report."""
        self.stage_held = threading.Event()
        self.stage_release = threading.Event()
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(
        self,
        tools: MediaTools,
        request: ExportRequest,
        *,
        cancel: threading.Event | None = None,
        on_progress: ExportProgressCallback | None = None,
    ) -> ExportResult:
        with self._lock:
            self.requests.append(request)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.entered.set()
            self._wait_if_held(cancel)

            def watch(stage: Any, progress: Any) -> None:
                if request.clip_id == self.watch_clip:
                    self.rendering.set()
                if on_progress is not None:
                    on_progress(stage, progress)
                if stage == self.hold_stage:
                    self.stage_held.set()
                    self.stage_release.wait(WAIT_S)

            return export_clip(tools, request, cancel=cancel, on_progress=watch)
        finally:
            with self._lock:
                self.active -= 1

    def _wait_if_held(self, cancel: threading.Event | None) -> None:
        deadline = time.monotonic() + WAIT_S
        while self.hold and not self.release.is_set():
            if cancel is not None and cancel.is_set():
                return
            if time.monotonic() > deadline:
                raise RuntimeError("the test never released the render")
            time.sleep(0.01)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Iterator[RenderProbe]:
    wrapper = RenderProbe()
    monkeypatch.setattr(exports_module, "export_clip", wrapper)
    yield wrapper
    wrapper.stage_release.set()  # never leave a reader thread waiting after a failed test


@pytest.fixture(scope="module")
def heavy_source(media_tools: MediaTools, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 12 s 720p video-only file: a render of most of it takes long enough to be cancelled."""
    path = tmp_path_factory.mktemp("heavy") / "heavy.mp4"
    argv = [
        str(media_tools.ffmpeg), "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=12",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path),
    ]  # fmt: skip
    completed = run_short(argv, timeout_s=180)
    assert completed.returncode == 0, completed.stderr
    return path


def ffprobe(tools: MediaTools, path: Path) -> dict[str, Any]:
    argv = [str(tools.ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json"]
    completed = run_short([*argv, "-i", f"file:{path}"], timeout_s=60)
    assert completed.returncode == 0, completed.stderr
    info: dict[str, Any] = json.loads(completed.stdout)
    return info


def stream_count(tools: MediaTools, path: Path, kind: str) -> int:
    streams: list[dict[str, Any]] = ffprobe(tools, path)["streams"]
    return sum(1 for stream in streams if stream["codec_type"] == kind)


def make_source(
    client: TestClient,
    directory: Path,
    name: str = "color_gop.mp4",
    *,
    project_id: str | None = None,
    copy_as: str | None = None,
) -> dict[str, Any]:
    path = copy_fixture(name, directory / "media", copy_as)
    response = register(client, project_id or new_project(client), path)
    assert response.status_code == 201, response.text
    source: dict[str, Any] = response.json()
    return source


def approved_clip(
    client: TestClient,
    source: dict[str, Any],
    start_ms: int = 1000,
    end_ms: int = 3000,
    **fields: Any,
) -> dict[str, Any]:
    clip = create_clip(client, source["id"], start_ms=start_ms, end_ms=end_ms, **fields)
    response = review_clip(client, clip, "approved")
    assert response.status_code == 200, response.text
    approved: dict[str, Any] = response.json()
    return approved


def start_batch(
    client: TestClient,
    project_id: str,
    clip_ids: list[str],
    output_dir: Path | str,
    **profile_changes: Any,
) -> Response:
    return client.post(
        "/v1/export-batches",
        json={
            "project_id": project_id,
            "clip_ids": clip_ids,
            "output_dir": str(output_dir),
            "profile": {**PROFILE, **profile_changes},
        },
    )


def begin(
    client: TestClient,
    project_id: str,
    clip_ids: list[str],
    output_dir: Path,
    **profile_changes: Any,
) -> dict[str, Any]:
    response = start_batch(client, project_id, clip_ids, output_dir, **profile_changes)
    assert response.status_code == 202, response.text
    created: dict[str, Any] = response.json()
    return created


def get_batch(client: TestClient, batch_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/export-batches/{batch_id}")
    assert response.status_code == 200, response.text
    batch: dict[str, Any] = response.json()
    return batch


def finish(client: TestClient, created: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Wait for the job of a created batch; returns ``(job, batch)``."""
    job = wait_job(client, created["job_id"])
    return job, get_batch(client, created["id"])


def export_state(client: TestClient, clip: dict[str, Any]) -> str:
    state: str = client.get(f"/v1/clips/{clip['id']}").json()["export_state"]
    return state


def new_out(directory: Path, name: str = "out") -> Path:
    out = directory / name
    out.mkdir(parents=True, exist_ok=True)
    return out


def names(directory: Path) -> list[str]:
    return sorted(os.listdir(directory))


def batch_rows(runtime: Runtime) -> int:
    with runtime.database.session() as session:
        return int(session.scalar(select(func.count()).select_from(ExportBatch)) or 0)


def mutate_source(path: Path, how: str) -> None:
    state = path.stat()
    if how == "appended":
        with path.open("ab") as handle:
            handle.write(b"\0" * 16)
    elif how == "touched":
        os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns + 5_000_000_000))
    elif how == "rewritten_in_place":
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        path.write_bytes(bytes(data))
        os.utime(path, ns=(state.st_atime_ns, state.st_mtime_ns))
    elif how == "removed":
        path.unlink()
    else:
        raise AssertionError(how)


# --- a successful batch ----------------------------------------------------------------------


def test_approved_clip_is_exported_verified_and_published(
    client: TestClient, tmp_path: Path, media_tools: MediaTools
) -> None:
    source = make_source(client, tmp_path)
    original = sha256(Path(source["path"]))
    clip = approved_clip(client, source)
    out = new_out(tmp_path)

    response = start_batch(client, source["project_id"], [clip["id"]], out)
    assert response.status_code == 202
    created = response.json()
    assert set(created) == {"id", "job_id", "status"}
    assert created["status"] == "queued"

    job, batch = finish(client, created)
    assert job["state"] == "succeeded"
    assert job["type"] == "export"
    assert job["error"] is None
    assert job["progress"] == {"stage": "done", "total": 1, "completed": 1, "failed": 0}
    assert batch["id"] == created["id"]
    assert batch["job_id"] == created["job_id"]
    assert batch["project_id"] == source["project_id"]
    assert batch["status"] == "succeeded"
    assert batch["profile"] == PROFILE
    assert Path(batch["output_dir"]) == out.resolve()

    (item,) = batch["items"]
    assert item["state"] == "verified"
    assert item["clip_id"] == clip["id"]
    assert item["clip_revision"] == clip["revision"]
    assert item["error"] is None
    final = Path(item["final_path"])
    assert final.parent == out.resolve()
    assert final.is_file()
    info = ffprobe(media_tools, final)
    assert stream_count(media_tools, final, "video") == 1
    assert stream_count(media_tools, final, "audio") == 1
    assert abs(float(info["format"]["duration"]) - 2.0) < 0.08

    assert names(out) == sorted(
        [final.name, Path(batch["manifest_path"]).name, Path(batch["catalog_path"]).name]
    )
    assert export_state(client, clip) == "verified"
    assert client.get(f"/v1/clips/{clip['id']}").json()["revision"] == clip["revision"]
    assert sha256(Path(source["path"])) == original


def test_manifest_records_tools_profile_interval_and_observed_timestamps(
    client: TestClient, tmp_path: Path, media_tools: MediaTools
) -> None:
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source, title="Мій момент")
    created = begin(client, source["project_id"], [clip["id"]], new_out(tmp_path))
    _, batch = finish(client, created)

    text = Path(batch["manifest_path"]).read_text(encoding="utf-8")
    manifest = json.loads(text)
    assert manifest["schema_version"] == 1
    assert manifest["batch_id"] == created["id"]
    assert manifest["status"] == "succeeded"
    assert manifest["requested_profile"] == PROFILE
    assert manifest["tools"] == {
        "ffmpeg_version": media_tools.ffmpeg_version,
        "ffprobe_version": media_tools.ffprobe_version,
    }
    (entry,) = manifest["items"]
    assert entry["seek_mode"] in ("preseek", "full_decode")
    assert entry["ffmpeg_version"] == media_tools.ffmpeg_version
    assert entry["ffprobe_version"] == media_tools.ffprobe_version
    assert entry["interval"] == {"start_ms": 1000, "end_ms": 3000, "duration_ms": 2000}
    assert entry["encoding_profile"]["video_codec"] == "libx264"
    assert entry["encoding_profile"]["crf"] == 18
    assert entry["audio_stream_indexes"] == [1]
    assert 1000 <= entry["first_video_pts_ms"] <= entry["last_video_pts_ms"] < 3000
    assert entry["observed_duration_ms"] > 0
    assert entry["video_frame_count"] > 0
    assert entry["state"] == "verified"
    assert entry["path"] == batch["items"][0]["final_path"]
    assert entry["title"] == "Мій момент"
    assert entry["clip_id"] == clip["id"]
    assert entry["revision"] == clip["revision"]
    assert entry["source_id"] == source["id"]
    assert entry["review_status"] == "approved"
    assert batch["items"][0]["observed"]["seek_mode"] == entry["seek_mode"]
    assert TOKEN not in text
    assert "Bearer" not in text
    assert "api_key" not in text.lower()


def test_catalog_is_the_fr_042_csv_and_neutralizes_formulas(
    client: TestClient, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source, title='=HYPERLINK("http://x")', categories=["fails"])
    created = begin(client, source["project_id"], [clip["id"]], new_out(tmp_path))
    _, batch = finish(client, created)

    raw = Path(batch["catalog_path"]).read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    assert rows[0] == list(CATALOG_COLUMNS)
    assert rows[1] == [
        source["id"],
        clip["id"],
        str(clip["revision"]),
        '\'=HYPERLINK("http://x")',
        "fails",
        "1000",
        "3000",
        "2000",
        "approved",
        batch["items"][0]["final_path"],
        "verified",
    ]
    assert len(rows) == 2


def test_audio_mode_all_keeps_every_audio_stream(
    client: TestClient, tmp_path: Path, media_tools: MediaTools
) -> None:
    source = make_source(client, tmp_path, "two_tracks.mkv")
    assert [stream["index"] for stream in source["audio_streams"]] == [1, 2]
    clip = approved_clip(client, source)
    created = begin(
        client,
        source["project_id"],
        [clip["id"]],
        new_out(tmp_path),
        audio_mode="all",
        audio_stream_index=None,
    )
    _, batch = finish(client, created)

    assert batch["status"] == "succeeded"
    assert batch["profile"]["audio_mode"] == "all"
    (item,) = batch["items"]
    assert stream_count(media_tools, Path(item["final_path"]), "audio") == 2
    assert item["observed"]["audio_stream_indexes"] == [1, 2]
    assert batch["items"][0]["state"] == "verified"


def test_audio_mode_none_exports_video_only(
    client: TestClient, tmp_path: Path, media_tools: MediaTools
) -> None:
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source)
    created = begin(client, source["project_id"], [clip["id"]], new_out(tmp_path), **NO_AUDIO)
    _, batch = finish(client, created)

    assert batch["status"] == "succeeded"
    final = Path(batch["items"][0]["final_path"])
    assert stream_count(media_tools, final, "audio") == 0
    assert batch["items"][0]["observed"]["audio_stream_indexes"] == []


def test_duplicate_clip_ids_are_exported_once_in_request_order(
    client: TestClient, tmp_path: Path, probe: RenderProbe
) -> None:
    source = make_source(client, tmp_path)
    first = approved_clip(client, source, 500, 1500)
    second = approved_clip(client, source, 2000, 3000)
    out = new_out(tmp_path)
    ids = [second["id"], first["id"], second["id"], first["id"]]

    created = begin(client, source["project_id"], ids, out)
    _, batch = finish(client, created)

    assert [item["clip_id"] for item in batch["items"]] == [second["id"], first["id"]]
    assert [request.clip_id for request in probe.requests] == [second["id"], first["id"]]
    assert batch["status"] == "succeeded"
    assert len([name for name in names(out) if name.endswith(".mp4")]) == 2


def test_batches_render_one_at_a_time(
    client: TestClient, tmp_path: Path, probe: RenderProbe
) -> None:
    source = make_source(client, tmp_path)
    clip_a = approved_clip(client, source, 500, 1500)
    clip_b = approved_clip(client, source, 2000, 3000)
    out = new_out(tmp_path)
    first = begin(client, source["project_id"], [clip_a["id"], clip_b["id"]], out)
    second = begin(client, source["project_id"], [clip_b["id"]], out)

    assert finish(client, first)[1]["status"] == "succeeded"
    assert finish(client, second)[1]["status"] == "succeeded"
    assert len(probe.requests) == 3
    assert probe.max_active == 1


# --- states while rendering ------------------------------------------------------------------


def test_clip_and_batch_states_follow_the_render(
    client: TestClient, tmp_path: Path, probe: RenderProbe
) -> None:
    source = make_source(client, tmp_path)
    clip_a = approved_clip(client, source, 500, 1500)
    clip_b = approved_clip(client, source, 2000, 3000)
    assert export_state(client, clip_a) == "none"
    probe.hold = True

    created = begin(client, source["project_id"], [clip_a["id"], clip_b["id"]], new_out(tmp_path))
    assert probe.entered.wait(WAIT_S)

    assert export_state(client, clip_a) == "running"
    assert export_state(client, clip_b) == "queued"
    batch = get_batch(client, created["id"])
    assert batch["status"] == "running"
    assert [item["state"] for item in batch["items"]] == ["running", "queued"]
    assert batch["manifest_path"] is None
    assert batch["catalog_path"] is None
    job = client.get(f"/v1/jobs/{created['job_id']}").json()
    assert job["state"] == "running"
    assert job["progress"] == {"stage": "render", "total": 2, "completed": 0, "failed": 0}

    probe.release.set()
    final_job, final_batch = finish(client, created)
    assert final_job["state"] == "succeeded"
    assert [item["state"] for item in final_batch["items"]] == ["verified", "verified"]
    assert export_state(client, clip_a) == export_state(client, clip_b) == "verified"


def test_render_uses_the_snapshot_and_a_changed_clip_becomes_stale(
    client: TestClient, tmp_path: Path, probe: RenderProbe
) -> None:
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source, 1000, 3000, title="Старе ім'я")
    probe.hold = True
    created = begin(client, source["project_id"], [clip["id"]], new_out(tmp_path))
    assert probe.entered.wait(WAIT_S)

    changed = client.patch(
        f"/v1/clips/{clip['id']}",
        json={
            "expected_revision": clip["revision"],
            "title": "Нове ім'я",
            "start_ms": 4000,
            "end_ms": 6000,
        },
    )
    assert changed.status_code == 200
    probe.release.set()
    job, batch = finish(client, created)

    assert job["state"] == "succeeded"
    (request,) = probe.requests
    assert (request.start_ms, request.end_ms, request.revision) == (1000, 3000, clip["revision"])
    (item,) = batch["items"]
    assert item["state"] == "verified"
    assert item["clip_revision"] == clip["revision"]
    assert f"_r{clip['revision']}_00h00m01s000-00h00m03s000" in Path(item["final_path"]).name
    current = client.get(f"/v1/clips/{clip['id']}").json()
    assert current["export_state"] == "stale"
    assert current["revision"] == clip["revision"] + 1  # the export itself never bumps it
    manifest = json.loads(Path(batch["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["items"][0]["title"] == "Старе ім'я"
    assert manifest["items"][0]["interval"]["start_ms"] == 1000


def test_review_and_title_change_during_the_render_keep_the_export_verified(
    client: TestClient, tmp_path: Path, probe: RenderProbe
) -> None:
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source)
    probe.hold = True
    created = begin(client, source["project_id"], [clip["id"]], new_out(tmp_path))
    assert probe.entered.wait(WAIT_S)

    rejected = review_clip(client, clip, "rejected")
    assert rejected.status_code == 200
    renamed = client.patch(
        f"/v1/clips/{clip['id']}",
        json={"expected_revision": rejected.json()["revision"], "title": "Нова назва"},
    )
    assert renamed.status_code == 200
    probe.release.set()
    job, batch = finish(client, created)

    # Only the bounds make a render stale (FR-032); the revision moved twice and that is not enough.
    assert job["state"] == "succeeded"
    assert batch["items"][0]["state"] == "verified"
    assert batch["items"][0]["clip_revision"] == clip["revision"]
    assert export_state(client, clip) == "verified"
    current = client.get(f"/v1/clips/{clip['id']}").json()
    assert current["revision"] == clip["revision"] + 2
    assert (current["start_ms"], current["end_ms"]) == (clip["start_ms"], clip["end_ms"])


def test_bounds_changed_while_the_item_is_queued_render_the_snapshot_and_end_stale(
    client: TestClient, tmp_path: Path, probe: RenderProbe
) -> None:
    source = make_source(client, tmp_path)
    clip_a = approved_clip(client, source, 500, 1500)
    clip_b = approved_clip(client, source, 2000, 3000)
    probe.hold = True
    created = begin(client, source["project_id"], [clip_a["id"], clip_b["id"]], new_out(tmp_path))
    assert probe.entered.wait(WAIT_S)
    # The first item is held before FFmpeg; the second has not been started: it reads its
    # snapshot only after the first one finished.
    assert export_state(client, clip_b) == "queued"

    changed = client.patch(
        f"/v1/clips/{clip_b['id']}",
        json={"expected_revision": clip_b["revision"], "start_ms": 4000, "end_ms": 5000},
    )
    assert changed.status_code == 200
    assert export_state(client, clip_b) == "queued"
    probe.release.set()
    job, batch = finish(client, created)

    assert job["state"] == "succeeded"
    assert [item["state"] for item in batch["items"]] == ["verified", "verified"]
    assert [request.clip_id for request in probe.requests] == [clip_a["id"], clip_b["id"]]
    second = probe.requests[1]
    assert (second.start_ms, second.end_ms, second.revision) == (2000, 3000, clip_b["revision"])
    item = batch["items"][1]
    assert item["clip_revision"] == clip_b["revision"]
    assert item["observed"]["interval"] == {"start_ms": 2000, "end_ms": 3000}
    assert 2000 <= item["observed"]["first_video_pts_ms"] <= item["observed"]["last_video_pts_ms"]
    assert item["observed"]["last_video_pts_ms"] < 3000
    assert f"_r{clip_b['revision']}_00h00m02s000-00h00m03s000" in Path(item["final_path"]).name
    manifest = json.loads(Path(batch["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["items"][1]["interval"] == {
        "start_ms": 2000,
        "end_ms": 3000,
        "duration_ms": 1000,
    }
    assert export_state(client, clip_a) == "verified"
    assert export_state(client, clip_b) == "stale"
    current = client.get(f"/v1/clips/{clip_b['id']}").json()
    assert (current["start_ms"], current["end_ms"]) == (4000, 5000)
    assert current["revision"] == clip_b["revision"] + 1


# --- progress of the render ------------------------------------------------------------------


def ffmpeg_progress(out_time_us: int | None, *, ended: bool = False) -> FfmpegProgress:
    return FfmpegProgress(fields={}, out_time_us=out_time_us, frame=None, ended=ended)


def record_progress_writes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every progress the export service stores for a job, in order."""
    writes: list[dict[str, Any]] = []
    original = JobRepository.set_progress

    def record(self: JobRepository, job_id: str, progress_json: Mapping[str, Any]) -> Job:
        writes.append(dict(progress_json))
        return original(self, job_id, progress_json)

    monkeypatch.setattr(JobRepository, "set_progress", record)
    return writes


def reports_of(writes: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """``(stage, item_percent)`` of the writes that came from a render's progress."""
    return [(write["stage"], write["item_percent"]) for write in writes if "item_percent" in write]


def fail_after_progress(
    monkeypatch: pytest.MonkeyPatch, reports: list[tuple[ExportStage, FfmpegProgress]]
) -> None:
    """``export_clip`` that reports ``reports`` as FFmpeg would and then fails."""

    def render(*_args: Any, on_progress: ExportProgressCallback | None = None, **_kw: Any) -> Any:
        assert on_progress is not None
        for stage, progress in reports:
            on_progress(stage, progress)
        raise RenderFailedError("failed")

    monkeypatch.setattr(exports_module, "export_clip", render)


def test_job_progress_carries_the_engine_stage_and_the_item_percent(
    client: TestClient, tmp_path: Path, probe: RenderProbe, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes = record_progress_writes(monkeypatch)
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source)
    probe.hold_stage = "verify"

    created = begin(client, source["project_id"], [clip["id"]], new_out(tmp_path))
    assert probe.stage_held.wait(WAIT_S), "the render never reached the verification"

    held = client.get(f"/v1/jobs/{created['job_id']}").json()
    assert held["state"] == "running"
    progress = held["progress"]
    assert progress["stage"] == "verify"
    assert set(progress) == {"stage", "total", "completed", "failed", "item_percent"}
    assert (progress["total"], progress["completed"], progress["failed"]) == (1, 0, 0)
    assert isinstance(progress["item_percent"], int)
    assert 0 <= progress["item_percent"] <= 100

    probe.stage_release.set()
    job, batch = finish(client, created)
    assert batch["status"] == "succeeded"
    assert job["progress"] == {"stage": "done", "total": 1, "completed": 1, "failed": 0}
    reports = reports_of(writes)
    assert {stage for stage, _ in reports} == {"render", "verify"}
    assert all(0 <= percent <= 100 for _, percent in reports)
    # Every write keeps the counts of the batch, whatever the stage.
    assert all(
        {"stage", "total", "completed", "failed"} <= set(write) and write["total"] == 1
        for write in writes
    )


def test_progress_is_written_at_most_once_per_interval_and_at_every_stage_change(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes = record_progress_writes(monkeypatch)
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source, 1000, 3000)  # 2 s: 2 000 000 us
    steps: list[tuple[ExportStage, FfmpegProgress]] = [
        ("render", ffmpeg_progress(step * 5_000)) for step in range(300)
    ]
    steps += [("verify", ffmpeg_progress(1_000_000)), ("verify", ffmpeg_progress(1_500_000))]
    fail_after_progress(monkeypatch, steps)

    created = begin(client, source["project_id"], [clip["id"]], new_out(tmp_path))
    job, batch = finish(client, created)

    assert batch["status"] == "failed"
    assert job["progress"] == {"stage": "failed", "total": 1, "completed": 1, "failed": 1}
    # 302 reports inside half a second: the first of "render" and the first of "verify" only.
    assert reports_of(writes) == [("render", 0), ("verify", 50)]


def test_item_percent_is_out_time_against_the_clip_duration_clamped_to_0_100(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exports_module, "PROGRESS_INTERVAL_S", 0.0)
    writes = record_progress_writes(monkeypatch)
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source, 1000, 3000)  # 2 s: 2 000 000 us
    fail_after_progress(
        monkeypatch,
        [
            ("render", ffmpeg_progress(0)),
            ("render", ffmpeg_progress(500_000)),
            ("render", ffmpeg_progress(None)),  # "N/A" at the start
            ("render", ffmpeg_progress(-7_000)),
            ("render", ffmpeg_progress(1_999_999)),
            ("render", ffmpeg_progress(9_000_000)),  # past the end
            ("render", ffmpeg_progress(1_000_000, ended=True)),
            ("verify", ffmpeg_progress(1_000_000)),
        ],
    )

    created = begin(client, source["project_id"], [clip["id"]], new_out(tmp_path))
    finish(client, created)

    assert reports_of(writes) == [
        ("render", 0),
        ("render", 25),
        ("render", 0),
        ("render", 0),
        ("render", 99),
        ("render", 100),
        ("render", 100),
        ("verify", 50),
    ]


# --- refused requests ------------------------------------------------------------------------


def test_unapproved_clips_are_409_and_nothing_is_created_or_started(
    client: TestClient, runtime: Runtime, tmp_path: Path, probe: RenderProbe
) -> None:
    source = make_source(client, tmp_path)
    approved = approved_clip(client, source, 500, 1500)
    suggested = create_clip(client, source["id"], start_ms=2000, end_ms=3000)
    rejected = review_clip(
        client, create_clip(client, source["id"], start_ms=4000, end_ms=5000), "rejected"
    ).json()
    out = new_out(tmp_path)

    response = start_batch(
        client, source["project_id"], [approved["id"], suggested["id"], rejected["id"]], out
    )

    assert response.status_code == 409
    error = error_of(response)
    assert error["code"] == "CLIP_NOT_APPROVED"
    assert error["details"] == {"clip_ids": [suggested["id"], rejected["id"]]}
    assert error["message"] == "Експортувати можна лише підтверджені кліпи"
    assert probe.requests == []
    assert batch_rows(runtime) == 0
    assert names(out) == []
    assert export_state(client, approved) == "none"


def test_clip_of_another_project_is_422(
    client: TestClient, runtime: Runtime, tmp_path: Path, probe: RenderProbe
) -> None:
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source)
    other_project = new_project(client, "Інший проєкт")

    response = start_batch(client, other_project, [clip["id"]], new_out(tmp_path))

    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "CLIP_PROJECT_MISMATCH"
    assert error["details"]["clip_ids"] == [clip["id"]]
    assert probe.requests == []
    assert batch_rows(runtime) == 0


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"video_codec": "h265"}, "UNSUPPORTED_VIDEO_CODEC"),
        ({"video_codec": "copy"}, "UNSUPPORTED_VIDEO_CODEC"),
        ({"quality": "low"}, "UNSUPPORTED_QUALITY"),
        ({"audio_mode": "mix"}, "UNSUPPORTED_AUDIO_MODE"),
        ({"audio_mode": "selected", "audio_stream_index": None}, "INVALID_AUDIO_STREAM"),
        ({"audio_mode": "none", "audio_stream_index": 1}, "INVALID_AUDIO_STREAM"),
        ({"audio_mode": "all", "audio_stream_index": 1}, "INVALID_AUDIO_STREAM"),
        ({"audio_stream_index": -1}, "INVALID_AUDIO_STREAM"),
        ({"audio_stream_index": 0}, "INVALID_AUDIO_STREAM"),  # the video stream
        ({"audio_stream_index": 2}, "INVALID_AUDIO_STREAM"),  # color_gop.mp4 has only stream 1
        ({"audio_stream_index": 99}, "INVALID_AUDIO_STREAM"),
        ({"write_srt": True}, "SRT_REQUIRES_TRANSCRIPT"),
    ],
)
def test_unsupported_profile_values_are_422_with_no_fallback(
    client: TestClient,
    runtime: Runtime,
    tmp_path: Path,
    probe: RenderProbe,
    changes: dict[str, Any],
    code: str,
) -> None:
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source)
    out = new_out(tmp_path)

    response = start_batch(client, source["project_id"], [clip["id"]], out, **changes)

    assert response.status_code == 422, response.text
    assert error_of(response)["code"] == code
    assert probe.requests == []
    assert batch_rows(runtime) == 0
    assert names(out) == []


def test_selected_audio_stream_must_exist_in_every_source_of_the_batch(
    client: TestClient, runtime: Runtime, tmp_path: Path, probe: RenderProbe
) -> None:
    two_tracks = make_source(client, tmp_path, "two_tracks.mkv")
    one_track = make_source(
        client, tmp_path, "color_gop.mp4", project_id=two_tracks["project_id"], copy_as="one.mp4"
    )
    clip_a = approved_clip(client, two_tracks)
    clip_b = approved_clip(client, one_track)

    response = start_batch(
        client,
        two_tracks["project_id"],
        [clip_a["id"], clip_b["id"]],
        new_out(tmp_path),
        audio_stream_index=2,
    )

    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "INVALID_AUDIO_STREAM"
    assert error["details"]["source_id"] == one_track["id"]
    assert probe.requests == []
    assert batch_rows(runtime) == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda profile: profile.pop("write_srt"),
        lambda profile: profile.pop("audio_stream_index"),
        lambda profile: profile.update(write_srt="false"),
        lambda profile: profile.update(audio_stream_index="1"),
        lambda profile: profile.update(bitrate=1),
    ],
    ids=["no write_srt", "no audio_stream_index", "srt as text", "index as text", "extra field"],
)
def test_malformed_profile_is_a_schema_error(
    client: TestClient, tmp_path: Path, probe: RenderProbe, mutate: Any
) -> None:
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source)
    profile = dict(PROFILE)
    mutate(profile)

    response = client.post(
        "/v1/export-batches",
        json={
            "project_id": source["project_id"],
            "clip_ids": [clip["id"]],
            "output_dir": str(new_out(tmp_path)),
            "profile": profile,
        },
    )

    assert response.status_code == 422
    assert error_of(response)["code"] == "VALIDATION_ERROR"
    assert probe.requests == []


def test_request_references_and_output_directory_are_checked(
    client: TestClient, runtime: Runtime, tmp_path: Path, probe: RenderProbe
) -> None:
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source)
    project_id = source["project_id"]
    out = new_out(tmp_path)
    not_a_directory = tmp_path / "file.txt"
    not_a_directory.write_text("x", encoding="utf-8")

    ids = [clip["id"]]
    unavailable = "OUTPUT_DIR_UNAVAILABLE"
    cases: list[tuple[Response, int, str]] = [
        (start_batch(client, "no-such-project", ids, out), 404, "PROJECT_NOT_FOUND"),
        (start_batch(client, project_id, ["no-such-clip"], out), 404, "CLIP_NOT_FOUND"),
        (start_batch(client, project_id, [], out), 422, "EXPORT_NO_CLIPS"),
        (start_batch(client, project_id, ids, tmp_path / "missing"), 422, unavailable),
        (start_batch(client, project_id, ids, not_a_directory), 422, unavailable),
        (start_batch(client, project_id, ids, "relative\\dir"), 422, "INVALID_OUTPUT_DIR"),
        (start_batch(client, project_id, ids, ""), 422, "INVALID_OUTPUT_DIR"),
    ]
    for response, status, code in cases:
        assert response.status_code == status, response.text
        assert error_of(response)["code"] == code
    assert probe.requests == []
    assert batch_rows(runtime) == 0


def test_unknown_batch_is_404(client: TestClient) -> None:
    response = client.get("/v1/export-batches/nope")
    assert response.status_code == 404
    assert error_of(response)["code"] == "EXPORT_BATCH_NOT_FOUND"


def test_export_routes_need_the_session_token(anonymous: TestClient) -> None:
    assert anonymous.post("/v1/export-batches", json={}).status_code == 401
    assert anonymous.get("/v1/export-batches/x").status_code == 401
    assert anonymous.post("/v1/jobs/x/cancel", json={}).status_code == 401


def test_without_ffmpeg_exporting_is_503(make_runtime: RuntimeFactory, tmp_path: Path) -> None:
    runtime: Runtime = make_runtime(with_tools=False)
    client = client_for(runtime)
    with runtime.database.write_session() as session:
        project = add_project(session)
        clip = add_clip(session, add_source(session, project.id).id, review_status="approved")

    response = start_batch(client, project.id, [clip.id], new_out(tmp_path))

    assert response.status_code == 503
    assert error_of(response)["code"] == "MEDIA_TOOLS_UNAVAILABLE"
    assert batch_rows(runtime) == 0


# --- failures of single items ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("how", "code", "message", "renders"),
    [
        ("appended", "SOURCE_CHANGED", "Файл джерела змінився після реєстрації", 0),
        ("touched", "SOURCE_CHANGED", "Файл джерела змінився після реєстрації", 0),
        ("rewritten_in_place", "SOURCE_CHANGED", "Файл джерела змінився після реєстрації", 1),
        ("removed", "SOURCE_UNAVAILABLE", "Файл джерела недоступний", 0),
    ],
)
def test_source_changed_after_registration_fails_the_item(
    client: TestClient,
    tmp_path: Path,
    probe: RenderProbe,
    how: str,
    code: str,
    message: str,
    renders: int,
) -> None:
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source)
    out = new_out(tmp_path)
    mutate_source(Path(source["path"]), how)

    created = begin(client, source["project_id"], [clip["id"]], out)
    job, batch = finish(client, created)

    assert len(probe.requests) == renders
    assert batch["status"] == "failed"
    (item,) = batch["items"]
    assert item["state"] == "failed"
    assert item["final_path"] is None
    assert item["error"] == {"code": code, "message": message}
    assert job["state"] == "failed"
    assert job["error"] == {
        "code": "EXPORT_FAILED",
        "message": "Жоден кліп не вдалося експортувати",
    }
    assert export_state(client, clip) == "failed"
    # Neither a final file nor a temp file remains; only the manifest and the catalog do.
    assert names(out) == sorted(
        [Path(batch["manifest_path"]).name, Path(batch["catalog_path"]).name]
    )
    assert str(tmp_path) not in json.dumps(item["error"]) + json.dumps(job["error"])
    manifest = json.loads(Path(batch["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["items"][0]["error_code"] == code
    assert manifest["items"][0]["path"] is None


class _NoVideoFrames(ExportError):
    code = "NO_VIDEO_FRAMES"


def test_engine_error_without_video_frames_has_its_own_message(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> ExportResult:
        raise _NoVideoFrames("no frames", detail="C:/secret/path.mp4")

    monkeypatch.setattr(exports_module, "export_clip", refuse)
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source)

    created = begin(client, source["project_id"], [clip["id"]], new_out(tmp_path))
    job, batch = finish(client, created)

    assert batch["status"] == "failed"
    assert job["progress"] == {"stage": "failed", "total": 1, "completed": 1, "failed": 1}
    assert batch["items"][0]["error"] == {
        "code": "NO_VIDEO_FRAMES",
        "message": "У вибраному інтервалі немає жодного кадру відео",
    }
    assert export_state(client, clip) == "failed"


def test_failed_item_is_logged_with_a_redacted_bounded_stderr_tail(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    temp_names: list[str] = []

    def render(_tools: MediaTools, request: ExportRequest, **_kwargs: Any) -> ExportResult:
        temp = request.output_dir / f".clipforge-{request.task_id}.part"
        temp_names.append(temp.name)
        lines = [
            "HEAD-MARKER " + "x" * 6000,  # cut away by the 4 KB limit
            f"Error opening input file {request.source}: Permission denied",
            f"Input #0, from 'file:{request.source.as_posix()}':",
            f"Output #0, mp4, to 'file:{temp}':",
            f"Could not write {request.output_dir}\\clip.mp4 ({request.source.name})",
            "TAIL-MARKER",
        ]
        raise RenderFailedError("FFmpeg не зміг створити кліп", detail="\n".join(lines))

    monkeypatch.setattr(exports_module, "export_clip", render)
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source)

    with caplog.at_level(logging.WARNING, logger=exports_module.__name__):
        created = begin(client, source["project_id"], [clip["id"]], new_out(tmp_path))
        job, batch = finish(client, created)

    (item,) = batch["items"]
    records = [
        record
        for record in caplog.records
        if record.name == exports_module.__name__ and record.levelno == logging.WARNING
    ]
    (record,) = records
    message = record.getMessage()
    assert f"batch={created['id']} item={item['id']} code=FFMPEG_FAILED " in message
    tail = message.split("detail=", 1)[1]
    assert tail.endswith("TAIL-MARKER")
    assert "HEAD-MARKER" not in tail
    assert len(tail.encode("utf-8")) <= 4096
    assert "\n" not in message
    assert "<source>" in tail
    assert "<output>" in tail
    assert "<temp>" in tail
    source_path, out = Path(source["path"]), Path(batch["output_dir"])
    for secret in (
        str(source_path),
        source_path.as_posix(),
        source_path.name,
        str(out),
        out.as_posix(),
        temp_names[0],
    ):
        assert secret.lower() not in message.lower()
    # The tail is for the log only: the API and the manifest get the code and a fixed message.
    assert item["error"] == {"code": "FFMPEG_FAILED", "message": "FFmpeg не зміг створити кліп"}
    manifest = Path(batch["manifest_path"]).read_text(encoding="utf-8")
    for stored in (json.dumps(item), json.dumps(job), manifest):
        assert "TAIL-MARKER" not in stored
        assert "Permission denied" not in stored


@pytest.mark.parametrize("bad_first", [False, True])
def test_one_failed_item_does_not_stop_the_others(
    client: TestClient, tmp_path: Path, bad_first: bool
) -> None:
    good = make_source(client, tmp_path, copy_as="good.mp4")
    bad = make_source(client, tmp_path, project_id=good["project_id"], copy_as="bad.mp4")
    clip_good = approved_clip(client, good)
    clip_bad = approved_clip(client, bad)
    mutate_source(Path(bad["path"]), "appended")
    ordered = [clip_bad, clip_good] if bad_first else [clip_good, clip_bad]

    created = begin(client, good["project_id"], [clip["id"] for clip in ordered], new_out(tmp_path))
    job, batch = finish(client, created)

    expected = ["failed", "verified"] if bad_first else ["verified", "failed"]
    assert [item["state"] for item in batch["items"]] == expected
    assert batch["status"] == "partial_failed"
    assert job["state"] == "succeeded"
    # Every finished item counts, the failed one too; the failures are counted on their own.
    assert job["progress"] == {"stage": "done", "total": 2, "completed": 2, "failed": 1}
    assert export_state(client, clip_good) == "verified"
    assert export_state(client, clip_bad) == "failed"
    rows = list(
        csv.reader(
            io.StringIO(Path(batch["catalog_path"]).read_bytes().decode("utf-8-sig")),
            delimiter=";",
        )
    )
    by_clip = {row[1]: row for row in rows[1:]}
    good_item = next(item for item in batch["items"] if item["clip_id"] == clip_good["id"])
    assert by_clip[clip_good["id"]][-2:] == [good_item["final_path"], "verified"]
    assert by_clip[clip_bad["id"]][-2:] == ["", "failed"]
    manifest = json.loads(Path(batch["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "partial_failed"


def test_failing_catalog_write_fails_the_job_but_keeps_the_verified_items(
    client: TestClient, tmp_path: Path, probe: RenderProbe
) -> None:
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source)
    out = new_out(tmp_path)
    probe.hold = True
    created = begin(client, source["project_id"], [clip["id"]], out)
    assert probe.entered.wait(WAIT_S)
    (out / f"manifest_{created['id']}.json").mkdir()  # the manifest cannot be written there
    probe.release.set()

    job, batch = finish(client, created)

    assert job["state"] == "failed"
    assert job["error"] == {
        "code": "CATALOG_WRITE_FAILED",
        "message": "Не вдалося записати маніфест і каталог експорту",
    }
    assert batch["status"] == "succeeded"
    assert batch["items"][0]["state"] == "verified"
    assert Path(batch["items"][0]["final_path"]).is_file()
    assert batch["manifest_path"] is None
    assert Path(batch["catalog_path"]).is_file()
    assert [name for name in names(out) if name.endswith(".part")] == []


# --- cancel ----------------------------------------------------------------------------------


def test_cancel_keeps_finished_items_and_leaves_nothing_of_the_cancelled_one(
    client: TestClient,
    tmp_path: Path,
    probe: RenderProbe,
    heavy_source: Path,
    media_tools: MediaTools,
) -> None:
    project_id = new_project(client)
    response = register(client, project_id, heavy_source)
    assert response.status_code == 201, response.text
    source = response.json()
    short = approved_clip(client, source, 0, 1000)
    long = approved_clip(client, source, 1000, 11000)
    never = approved_clip(client, source, 0, 500)
    out = new_out(tmp_path)
    probe.watch_clip = long["id"]

    created = begin(client, project_id, [short["id"], long["id"], never["id"]], out, **NO_AUDIO)
    assert probe.rendering.wait(WAIT_S), "the second render never reported progress"
    cancelled = client.post(f"/v1/jobs/{created['job_id']}/cancel", json={})

    assert cancelled.status_code == 202
    assert cancelled.json()["state"] == "cancelling"
    job, batch = finish(client, created)
    assert job["state"] == "cancelled"
    assert job["error"] is None
    # The cancelled items are finished too: nothing is left open, and nobody failed.
    assert job["progress"] == {"stage": "cancelled", "total": 3, "completed": 3, "failed": 0}
    assert batch["status"] == "cancelled"
    assert [item["state"] for item in batch["items"]] == ["verified", "cancelled", "cancelled"]
    assert [item["final_path"] is None for item in batch["items"]] == [False, True, True]
    assert [item["error"] for item in batch["items"]] == [None, None, None]
    assert [export_state(client, clip) for clip in (short, long, never)] == [
        "verified",
        "cancelled",
        "cancelled",
    ]
    assert [request.clip_id for request in probe.requests] == [short["id"], long["id"]]

    kept = Path(batch["items"][0]["final_path"])
    assert abs(float(ffprobe(media_tools, kept)["format"]["duration"]) - 1.0) < 0.08
    # The first output, the manifest and the catalog; no temp file, nothing for the others.
    assert names(out) == sorted(
        [kept.name, Path(batch["manifest_path"]).name, Path(batch["catalog_path"]).name]
    )
    manifest = json.loads(Path(batch["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"
    assert [entry["state"] for entry in manifest["items"]] == ["verified", "cancelled", "cancelled"]
    assert [entry["path"] is None for entry in manifest["items"]] == [False, True, True]


def test_cancel_is_idempotent_while_the_job_is_stopping(
    client: TestClient, tmp_path: Path, probe: RenderProbe
) -> None:
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source)
    out = new_out(tmp_path)
    probe.hold = True
    created = begin(client, source["project_id"], [clip["id"]], out)
    assert probe.entered.wait(WAIT_S)

    first = client.post(f"/v1/jobs/{created['job_id']}/cancel", json={})
    second = client.post(f"/v1/jobs/{created['job_id']}/cancel")  # a body is optional

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json()["state"] == second.json()["state"] == "cancelling"
    job, batch = finish(client, created)
    assert job["state"] == "cancelled"
    assert batch["status"] == "cancelled"
    assert batch["items"][0]["state"] == "cancelled"
    assert export_state(client, clip) == "cancelled"
    assert client.post(f"/v1/jobs/{created['job_id']}/cancel", json={}).status_code == 202
    assert names(out) == sorted(
        [Path(batch["manifest_path"]).name, Path(batch["catalog_path"]).name]
    )


def test_cancelling_a_queued_batch_cancels_it_without_rendering(
    client: TestClient, tmp_path: Path, probe: RenderProbe
) -> None:
    source = make_source(client, tmp_path)
    clip_a = approved_clip(client, source, 500, 1500)
    clip_b = approved_clip(client, source, 2000, 3000)
    clip_c = approved_clip(client, source, 3500, 4500)
    out = new_out(tmp_path)
    probe.hold = True
    first = begin(client, source["project_id"], [clip_a["id"]], out)
    assert probe.entered.wait(WAIT_S)
    second = begin(client, source["project_id"], [clip_b["id"]], out)
    assert get_batch(client, second["id"])["status"] == "queued"

    cancelled = client.post(f"/v1/jobs/{second['job_id']}/cancel", json={})

    assert cancelled.status_code == 202
    assert cancelled.json()["state"] == "cancelled"
    queued = get_batch(client, second["id"])
    assert queued["status"] == "cancelled"
    assert [item["state"] for item in queued["items"]] == ["cancelled"]
    assert (queued["manifest_path"], queued["catalog_path"]) == (None, None)
    assert export_state(client, clip_b) == "cancelled"

    probe.release.set()
    assert finish(client, first)[1]["status"] == "succeeded"
    third = begin(client, source["project_id"], [clip_c["id"]], out)
    assert finish(client, third)[1]["status"] == "succeeded"  # the worker passed the second job
    assert [request.clip_id for request in probe.requests] == [clip_a["id"], clip_c["id"]]
    assert client.get(f"/v1/jobs/{second['job_id']}").json()["state"] == "cancelled"


def test_cancel_is_refused_for_finished_and_foreign_jobs_and_unknown_ids(
    client: TestClient, runtime: Runtime, tmp_path: Path
) -> None:
    source = make_source(client, tmp_path)
    clip = approved_clip(client, source)
    created = begin(client, source["project_id"], [clip["id"]], new_out(tmp_path))
    finish(client, created)
    with runtime.database.write_session() as session:
        hash_job_id = JobRepository(session).add(type="source_hash", state="queued").id

    finished = client.post(f"/v1/jobs/{created['job_id']}/cancel", json={})
    foreign = client.post(f"/v1/jobs/{hash_job_id}/cancel", json={})
    unknown = client.post("/v1/jobs/nope/cancel", json={})
    bad_body = client.post(f"/v1/jobs/{created['job_id']}/cancel", json={"force": True})

    assert finished.status_code == 409
    assert error_of(finished) | {"details": {}} == {
        "code": "JOB_NOT_CANCELLABLE",
        "message": "Завдання вже завершено",
        "details": {},
        "retryable": False,
    }
    assert foreign.status_code == 409
    assert error_of(foreign)["code"] == "JOB_NOT_CANCELLABLE"
    assert error_of(foreign)["message"] == "Це завдання не можна скасувати"
    assert client.get(f"/v1/jobs/{hash_job_id}").json()["state"] == "queued"
    assert unknown.status_code == 404
    assert error_of(unknown)["code"] == "JOB_NOT_FOUND"
    assert bad_body.status_code == 422
    assert error_of(bad_body)["code"] == "VALIDATION_ERROR"


# --- restart ---------------------------------------------------------------------------------


def test_restart_settles_a_batch_left_open_by_a_dead_process(
    make_runtime: RuntimeFactory, tmp_path: Path
) -> None:
    data_dir = tmp_path / "workspace"
    out = new_out(tmp_path)
    first = make_runtime(data_dir=data_dir)
    kept = out / "kept.mp4"
    kept.write_bytes(b"finished export")
    precious = out / "precious.mp4"
    precious.write_bytes(b"not ours")
    with first.database.write_session() as session:
        project = add_project(session)
        source = add_source(session, project.id)
        clips = [add_clip(session, source.id, review_status="approved") for _ in range(4)]
        exports = ExportRepository(session)
        batch = exports.add_batch(
            project_id=project.id, output_dir=str(out), profile_json=PROFILE, status="running"
        )
        items = [
            exports.add_item(
                batch_id=batch.id,
                clip_id=clip.id,
                clip_revision=1,
                snapshot_json={"position": position},
                state=state,
            )
            for position, (clip, state) in enumerate(
                zip(clips, ["verified", "running", "queued", "running"], strict=True)
            )
        ]
        exports.update_item(items[0].id, final_path=str(kept))
        own_temp = out / f".clipforge-{items[1].id}.part"
        own_temp.write_bytes(b"half a render")
        exports.update_item(items[1].id, temp_path=str(own_temp))
        exports.update_item(items[3].id, temp_path=str(precious))  # not the engine's name
        for clip, state in zip(clips, ["verified", "running", "queued", "running"], strict=True):
            ClipRepository(session).set_export_state(clip.id, state)
        job = JobRepository(session).add(
            type="export", state="running", checkpoint_json={"batch_id": batch.id}
        )
    first.close()  # the "crash": nothing finished this batch

    second = make_runtime(data_dir=data_dir)
    client = client_for(second)
    result = get_batch(client, batch.id)

    assert result["status"] == "partial_failed"
    assert [item["state"] for item in result["items"]] == ["verified", "failed", "failed", "failed"]
    assert [item["error"] for item in result["items"]] == [
        None,
        *[{"code": "INTERRUPTED", "message": "Експорт перервано перезапуском застосунку"}] * 3,
    ]
    assert result["job_id"] == job.id
    assert client.get(f"/v1/jobs/{job.id}").json()["state"] == "interrupted"
    assert [export_state(client, {"id": clip.id}) for clip in clips] == [
        "verified",
        "failed",
        "failed",
        "failed",
    ]
    assert not own_temp.exists()
    assert precious.read_bytes() == b"not ours"
    assert kept.read_bytes() == b"finished export"

    # FR-042: the batch the restart closed has its manifest and catalog, linked from its job.
    manifest_path, catalog_path = Path(result["manifest_path"]), Path(result["catalog_path"])
    assert manifest_path == out / f"manifest_{batch.id}.json"
    assert catalog_path == out / f"catalog_{batch.id}.csv"
    assert names(out) == sorted([kept.name, precious.name, manifest_path.name, catalog_path.name])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["batch_id"] == batch.id
    assert manifest["status"] == "partial_failed"
    assert [(entry["state"], entry["error_code"]) for entry in manifest["items"]] == [
        ("verified", None),
        ("failed", "INTERRUPTED"),
        ("failed", "INTERRUPTED"),
        ("failed", "INTERRUPTED"),
    ]
    assert [entry["path"] for entry in manifest["items"]] == [str(kept), None, None, None]
    rows = list(
        csv.reader(io.StringIO(catalog_path.read_bytes().decode("utf-8-sig")), delimiter=";")
    )
    assert rows[0] == list(CATALOG_COLUMNS)
    assert [row[-1] for row in rows[1:]] == ["verified", "failed", "failed", "failed"]
    assert client.get(f"/v1/jobs/{job.id}").json()["state"] == "interrupted"


def test_restart_closes_a_batch_whose_output_directory_is_gone_without_failing(
    make_runtime: RuntimeFactory, tmp_path: Path
) -> None:
    data_dir = tmp_path / "workspace"
    out = new_out(tmp_path, "gone")
    first = make_runtime(data_dir=data_dir)
    with first.database.write_session() as session:
        project = add_project(session)
        clip = add_clip(session, add_source(session, project.id).id, review_status="approved")
        exports = ExportRepository(session)
        batch = exports.add_batch(
            project_id=project.id, output_dir=str(out), profile_json=PROFILE, status="running"
        )
        exports.add_item(
            batch_id=batch.id,
            clip_id=clip.id,
            clip_revision=1,
            snapshot_json={"position": 0},
            state="running",
        )
        job = JobRepository(session).add(
            type="export", state="running", checkpoint_json={"batch_id": batch.id}
        )
    first.close()
    shutil.rmtree(out)

    second = make_runtime(data_dir=data_dir)  # the start-up must not fail
    result = get_batch(client_for(second), batch.id)

    assert result["status"] == "failed"
    assert result["items"][0]["error"]["code"] == "INTERRUPTED"
    assert result["job_id"] == job.id
    assert (result["manifest_path"], result["catalog_path"]) == (None, None)
    assert not out.exists()


def test_closing_the_runtime_during_a_render_stops_it_and_the_next_start_recovers(
    make_runtime: RuntimeFactory, probe: RenderProbe, heavy_source: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "workspace"
    first = make_runtime(data_dir=data_dir)
    client = client_for(first)
    project_id = new_project(client)
    response = register(client, project_id, heavy_source)
    assert response.status_code == 201, response.text
    clip = approved_clip(client, response.json(), 0, 11000)
    out = new_out(tmp_path)
    probe.watch_clip = clip["id"]
    created = begin(client, project_id, [clip["id"]], out, **NO_AUDIO)
    assert probe.rendering.wait(WAIT_S)

    first.close()

    assert names(out) == []  # the render was stopped and its temp file removed
    second = make_runtime(data_dir=data_dir)
    client = client_for(second)
    batch = get_batch(client, created["id"])
    assert batch["status"] == "failed"
    assert batch["items"][0]["state"] == "failed"
    assert batch["items"][0]["error"]["code"] == "INTERRUPTED"
    assert client.get(f"/v1/jobs/{created['job_id']}").json()["state"] == "interrupted"
    assert export_state(client, clip) == "failed"
    # The temp file is gone for good; the restart only added the files of the closed batch.
    assert names(out) == sorted(
        [Path(batch["manifest_path"]).name, Path(batch["catalog_path"]).name]
    )
    manifest = json.loads(Path(batch["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["items"][0]["error_code"] == "INTERRUPTED"
