"""The ``previews`` table (migration 0002) and ``PreviewRepository`` (ADR 021)."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clipforge.storage import (
    Database,
    JobRepository,
    PreviewRepository,
    ProjectRepository,
    RecordNotFound,
    open_database,
)
from clipforge.storage.database import BACKUP_DIRNAME, DB_FILENAME
from clipforge.storage.migrate import MIGRATIONS_DIR
from clipforge.storage.repositories import Clock
from clipforge.storage.util import utc_now

from .test_storage_support import add_project, add_source, counting_clock


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_database(tmp_path / "data")
    try:
        yield database
    finally:
        database.close()


def add_preview(
    session: Session,
    source_id: str,
    *,
    audio: int | None = 1,
    status: str = "queued",
    clock: Clock = utc_now,
) -> str:
    """A preview with its own queued job; returns the preview id. Pass a counting clock when the
    order of creation matters: the real clock ticks only every ~15 ms on Windows."""
    job = JobRepository(session).add(type="preview")
    previews = PreviewRepository(session, now=clock)
    preview = previews.add(
        source_id=source_id,
        audio_stream_index=audio,
        profile_version=1,
        job_id=job.id,
        source_size=1_000_000,
        source_mtime=1_700_000_000_000_000_000,
    )
    if status != "queued":
        previews.set_state(preview.id, status)
    return preview.id


def test_new_preview_is_queued_and_keeps_the_source_snapshot(db: Database) -> None:
    with db.write_session() as session:
        source_id = add_source(session, add_project(session).id).id
        preview_id = add_preview(session, source_id, audio=None)
    with db.session() as session:
        preview = PreviewRepository(session).get(preview_id)
        assert preview is not None
        assert preview.status == "queued"
        assert preview.audio_stream_index is None
        assert (preview.source_size, preview.source_mtime) == (1_000_000, 1_700_000_000_000_000_000)
        assert (preview.file_size, preview.error_code) == (None, None)
        assert preview.created_at == preview.updated_at


def test_set_state_records_the_file_size_and_the_error_code(db: Database) -> None:
    with db.write_session() as session:
        source_id = add_source(session, add_project(session).id).id
        ready_id = add_preview(session, source_id)
        failed_id = add_preview(session, source_id, audio=2)
        previews = PreviewRepository(session)
        previews.set_state(ready_id, "ready", file_size=12345)
        previews.set_state(failed_id, "failed", error_code="OUTPUT_INVALID")
    with db.session() as session:
        previews = PreviewRepository(session)
        ready, failed = previews.get(ready_id), previews.get(failed_id)
        assert ready is not None
        assert failed is not None
        assert (ready.status, ready.file_size, ready.error_code) == ("ready", 12345, None)
        assert (failed.status, failed.file_size, failed.error_code) == (
            "failed",
            None,
            "OUTPUT_INVALID",
        )
    with pytest.raises(RecordNotFound), db.write_session() as session:
        PreviewRepository(session).set_state("missing", "ready")


def test_find_ready_and_find_open_follow_the_key_and_the_null_audio_index(db: Database) -> None:
    with db.write_session() as session:
        source_id = add_source(session, add_project(session).id).id
        other_source_id = add_source(session, add_project(session, "Інший").id).id
        silent = add_preview(session, source_id, audio=None, status="ready")
        track_1 = add_preview(session, source_id, audio=1, status="ready")
        running = add_preview(session, source_id, audio=2, status="running")
        add_preview(session, source_id, audio=2, status="cancelled")
        add_preview(session, other_source_id, audio=1, status="ready")
    with db.session() as session:
        previews = PreviewRepository(session)
        found = previews.find_ready(source_id, None, 1)
        assert found is not None
        assert found.id == silent
        found = previews.find_ready(source_id, 1, 1)
        assert found is not None
        assert found.id == track_1
        assert previews.find_ready(source_id, 2, 1) is None
        assert previews.find_ready(source_id, 1, 2) is None  # another profile version
        found = previews.find_open(source_id, 2, 1)
        assert found is not None
        assert found.id == running
        assert previews.find_open(source_id, 1, 1) is None


def test_find_ready_returns_the_newest_ready_preview(db: Database) -> None:
    with db.write_session() as session:
        source_id = add_source(session, add_project(session).id).id
        clock = counting_clock()
        add_preview(session, source_id, status="ready", clock=clock)
        newest = add_preview(session, source_id, status="ready", clock=clock)
    with db.session() as session:
        found = PreviewRepository(session).find_ready(source_id, 1, 1)
        assert found is not None
        assert found.id == newest


def test_list_with_status_is_ordered_by_creation(db: Database) -> None:
    with db.write_session() as session:
        source_id = add_source(session, add_project(session).id).id
        clock = counting_clock()
        first = add_preview(session, source_id, audio=1, status="running", clock=clock)
        add_preview(session, source_id, audio=2, status="ready", clock=clock)
        second = add_preview(session, source_id, audio=3, status="queued", clock=clock)
    with db.session() as session:
        listed = PreviewRepository(session).list_with_status(("queued", "running"))
        assert [preview.id for preview in listed] == [first, second]


def test_status_and_numbers_are_checked_by_the_database(db: Database) -> None:
    with db.write_session() as session:
        source_id = add_source(session, add_project(session).id).id
        preview_id = add_preview(session, source_id)

    def raw_update(sql: str) -> None:
        with db.engine.begin() as connection:
            connection.exec_driver_sql(sql.format(id=preview_id))

    for sql, constraint in (
        ("UPDATE previews SET status = 'done' WHERE id = '{id}'", "ck_previews_status"),
        (
            "UPDATE previews SET profile_version = 0 WHERE id = '{id}'",
            "ck_previews_profile_version",
        ),
        (
            "UPDATE previews SET audio_stream_index = -1 WHERE id = '{id}'",
            "ck_previews_audio_stream_index",
        ),
        ("UPDATE previews SET source_size = -1 WHERE id = '{id}'", "ck_previews_source_size"),
        ("UPDATE previews SET file_size = -1 WHERE id = '{id}'", "ck_previews_file_size"),
    ):
        with pytest.raises(IntegrityError, match=constraint):
            raw_update(sql)


def test_a_preview_needs_an_existing_source_and_job(db: Database) -> None:
    with db.write_session() as session:
        source_id = add_source(session, add_project(session).id).id
        job_id = JobRepository(session).add(type="preview").id
    for source, job in (("no-source", job_id), (source_id, "no-job")):
        with pytest.raises(IntegrityError, match="FOREIGN KEY"), db.write_session() as session:
            PreviewRepository(session).add(
                source_id=source,
                audio_stream_index=None,
                profile_version=1,
                job_id=job,
                source_size=1,
                source_mtime=1,
            )


def test_a_database_at_revision_0001_is_upgraded_with_a_backup_and_keeps_its_data(
    tmp_path: Path,
) -> None:
    first_only = tmp_path / "only-0001"
    (first_only / "versions").mkdir(parents=True)
    shutil.copyfile(MIGRATIONS_DIR / "env.py", first_only / "env.py")
    shutil.copyfile(
        MIGRATIONS_DIR / "versions" / "r0001_initial.py", first_only / "versions" / "r0001.py"
    )
    data_dir = tmp_path / "data"
    with open_database(data_dir, migrations_dir=first_only) as old:
        with old.write_session() as session:
            project_id = ProjectRepository(session).add("Старий проєкт").id
        assert "previews" not in table_names(data_dir / DB_FILENAME)

    with open_database(data_dir) as upgraded:
        assert "previews" in table_names(data_dir / DB_FILENAME)
        with upgraded.session() as session:
            project = ProjectRepository(session).get(project_id)
            assert project is not None
            assert project.name == "Старий проєкт"
            assert PreviewRepository(session).list_with_status(("queued",)) == []
    backup = next((data_dir / BACKUP_DIRNAME).iterdir())
    assert backup.name.endswith("-from-0001.sqlite3")


def table_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {name for (name,) in rows}
