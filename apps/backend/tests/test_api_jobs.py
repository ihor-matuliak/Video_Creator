"""GET /v1/jobs/{id}, the SHA-256 job lifecycle and start-up job recovery (ADR 017)."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from clipforge.services.jobs import add_source_hash_job
from clipforge.services.runtime import Runtime
from clipforge.storage import Job, JobRepository, SourceRepository

from .support import RuntimeFactory, client_for, fixture_file
from .test_api_sources import copy_fixture, register_ok, sha256, wait_job
from .test_storage_support import add_project, add_source

JOB_STATES = {"queued", "running", "cancelling", "succeeded", "failed", "cancelled", "interrupted"}


def seed_hash_job(runtime: Runtime, **source_fields: Any) -> tuple[str, str]:
    """A pending source row (with ``source_fields`` overriding the stored file facts) plus its
    queued hash job; returns ``(source_id, job_id)`` without starting the job."""
    with runtime.database.write_session() as session:
        source = add_source(session, add_project(session).id)
        for name, value in source_fields.items():
            setattr(source, name, value)
        session.flush()
        job_id = add_source_hash_job(JobRepository(session), source.id)
    return source.id, job_id


def test_unknown_job_is_404(client: TestClient) -> None:
    response = client.get("/v1/jobs/nope")
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "JOB_NOT_FOUND",
            "message": "Завдання не знайдено",
            "details": {},
            "retryable": False,
        }
    }


def test_job_envelope_of_a_registration(client: TestClient, tmp_path: Path) -> None:
    source = register_ok(client, copy_fixture("color_gop.mp4", tmp_path))
    first = client.get(f"/v1/jobs/{source['hash_job_id']}").json()
    assert set(first) == {"id", "type", "state", "progress", "error"}
    assert first["id"] == source["hash_job_id"]
    assert first["type"] == "source_hash"
    assert first["state"] in JOB_STATES
    assert first["error"] is None

    final = wait_job(client, source["hash_job_id"])
    assert final["state"] == "succeeded"
    assert final["progress"] == {"stage": "done"}
    assert final["error"] is None


def test_hash_job_of_a_vanished_file_fails_with_a_fixed_message(
    client: TestClient, runtime: Runtime
) -> None:
    source_id, job_id = seed_hash_job(runtime)  # the stored path does not exist
    runtime.runner.submit_source_hash(job_id, source_id)

    job = wait_job(client, job_id)
    assert job["state"] == "failed"
    assert job["error"] == {
        "code": "SOURCE_UNAVAILABLE",
        "message": "Файл джерела недоступний",
    }
    assert job["progress"] == {"stage": "failed"}
    assert "Відео" not in str(job)
    assert "запис.mkv" not in str(job)
    with runtime.database.session() as session:
        source = SourceRepository(session).get(source_id)
        assert source is not None
        assert (source.hash_status, source.sha256) == ("failed", None)


def test_hash_job_of_a_file_changed_since_registration_fails(
    client: TestClient, runtime: Runtime, tmp_path: Path
) -> None:
    path = copy_fixture("color_gop.mp4", tmp_path)
    stat = path.stat()
    source_id, job_id = seed_hash_job(
        runtime, path=str(path), size=stat.st_size + 1, mtime=stat.st_mtime_ns
    )
    runtime.runner.submit_source_hash(job_id, source_id)

    job = wait_job(client, job_id)
    assert job["state"] == "failed"
    assert job["error"]["code"] == "SOURCE_CHANGED"
    with runtime.database.session() as session:
        source = SourceRepository(session).get(source_id)
        assert source is not None
        assert (source.hash_status, source.sha256) == ("failed", None)


def test_restart_interrupts_unfinished_jobs_and_rehashes_pending_sources(
    make_runtime: RuntimeFactory, tmp_path: Path
) -> None:
    data_dir = tmp_path / "workspace"
    first = make_runtime(data_dir=data_dir)
    path = copy_fixture("color_gop.mp4", tmp_path / "media")
    stat = path.stat()
    with first.database.write_session() as session:
        project_id = add_project(session).id
        pending = add_source(session, project_id)
        pending.path, pending.size, pending.mtime = str(path), stat.st_size, stat.st_mtime_ns
        jobs = JobRepository(session)
        stale_running = jobs.add(type="source_hash", state="running")
        stale_queued = jobs.add(type="source_hash", state="queued")
        finished = jobs.add(type="source_hash", state="succeeded")
        session.flush()
    first.close()  # the "crash": nothing finished these jobs

    second = make_runtime(data_dir=data_dir)
    client = client_for(second)
    assert client.get(f"/v1/jobs/{stale_running.id}").json()["state"] == "interrupted"
    assert client.get(f"/v1/jobs/{stale_queued.id}").json()["state"] == "interrupted"
    assert client.get(f"/v1/jobs/{finished.id}").json()["state"] == "succeeded"

    deadline = time.monotonic() + 30
    state: tuple[str, str | None] = ("pending", None)
    while state[0] == "pending" and time.monotonic() < deadline:
        with second.database.session() as session:
            source = SourceRepository(session).get(pending.id)
            assert source is not None
            state = (source.hash_status, source.sha256)
        time.sleep(0.05)
    assert state == ("computed", sha256(path))


def seed_failed_source(runtime: Runtime, path: Path, *, size: int, mtime_ns: int) -> str:
    """A source whose hash failed earlier, stored with the given file facts."""
    with runtime.database.write_session() as session:
        source = add_source(session, add_project(session).id)
        source.path, source.size, source.mtime = str(path), size, mtime_ns
        SourceRepository(session).set_hash(source.id, sha256=None, hash_status="failed")
        return source.id


def hash_jobs_of(runtime: Runtime, source_id: str) -> list[Job]:
    with runtime.database.session() as session:
        jobs = session.scalars(select(Job).where(Job.type == "source_hash"))
        return [job for job in jobs if (job.checkpoint_json or {}).get("source_id") == source_id]


def test_restart_retries_a_failed_hash_when_the_file_is_available_again(
    make_runtime: RuntimeFactory, tmp_path: Path
) -> None:
    data_dir = tmp_path / "workspace"
    first = make_runtime(data_dir=data_dir)
    path = copy_fixture("color_gop.mp4", tmp_path / "media")
    first_client = client_for(first)
    registered = register_ok(first_client, path)
    source_id = registered["id"]
    assert wait_job(first_client, registered["hash_job_id"])["state"] == "succeeded"
    with first.database.write_session() as session:  # the hash failed back then
        SourceRepository(session).set_hash(source_id, sha256=None, hash_status="failed")
    earlier_job_ids = {job.id for job in hash_jobs_of(first, source_id)}
    first.close()

    second = make_runtime(data_dir=data_dir)
    client = client_for(second)
    deadline = time.monotonic() + 30
    status = "failed"
    while status != "computed" and time.monotonic() < deadline:
        with second.database.session() as session:
            source = SourceRepository(session).get(source_id)
            assert source is not None
            status = source.hash_status
        time.sleep(0.05)
    assert status == "computed"

    new_jobs = [job for job in hash_jobs_of(second, source_id) if job.id not in earlier_job_ids]
    assert len(new_jobs) == 1
    assert wait_job(client, new_jobs[0].id)["state"] == "succeeded"
    stored = client.get(f"/v1/sources/{source_id}").json()
    assert stored["sha256"] == sha256(path)

    copy = tmp_path / "нова тека" / "копія.mp4"
    copy.parent.mkdir()
    shutil.copy2(path, copy)
    path.unlink()
    response = client.post(f"/v1/sources/{source_id}/relink", json={"path": str(copy)})
    assert response.status_code == 200, response.text
    assert Path(response.json()["path"]) == copy.resolve()


def test_restart_keeps_a_failed_hash_when_the_file_is_missing_or_changed(
    make_runtime: RuntimeFactory, tmp_path: Path
) -> None:
    data_dir = tmp_path / "workspace"
    first = make_runtime(data_dir=data_dir)
    changed_path = copy_fixture("color_gop.mp4", tmp_path / "media")
    stat = changed_path.stat()
    missing_id = seed_failed_source(
        first, tmp_path / "media" / "немає.mp4", size=stat.st_size, mtime_ns=stat.st_mtime_ns
    )
    resized_id = seed_failed_source(
        first, changed_path, size=stat.st_size + 1, mtime_ns=stat.st_mtime_ns
    )
    touched_id = seed_failed_source(
        first, changed_path, size=stat.st_size, mtime_ns=stat.st_mtime_ns + 1
    )
    first.close()

    second = make_runtime(data_dir=data_dir)
    client = client_for(second)
    for source_id in (missing_id, resized_id, touched_id):
        assert hash_jobs_of(second, source_id) == []
        with second.database.session() as session:
            source = SourceRepository(session).get(source_id)
            assert source is not None
            assert (source.hash_status, source.sha256) == ("failed", None)
        response = client.post(f"/v1/sources/{source_id}/relink", json={"path": str(changed_path)})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SOURCE_HASH_UNAVAILABLE"


def test_closing_the_runtime_releases_the_workspace(
    make_runtime: RuntimeFactory, tmp_path: Path
) -> None:
    data_dir = tmp_path / "workspace"
    runtime = make_runtime(data_dir=data_dir)
    client = client_for(runtime)
    source = register_ok(client, fixture_file("color_gop.mp4"))
    wait_job(client, source["hash_job_id"])
    runtime.close()
    runtime.close()  # idempotent
    make_runtime(data_dir=data_dir).close()  # would raise WorkspaceLocked if still held
