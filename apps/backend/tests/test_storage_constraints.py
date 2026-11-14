"""Database-level rules: CHECK constraints, foreign keys, uniqueness, versioned JSON."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from clipforge.storage import (
    ClipRepository,
    Database,
    ExportRepository,
    JobRepository,
    ProjectRepository,
    SourceRepository,
    StorageError,
    open_database,
)

from .test_storage_support import SHA256, add_clip, add_project, add_source


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_database(tmp_path / "data")
    try:
        yield database
    finally:
        database.close()


def test_source_duration_must_be_positive(db: Database) -> None:
    with db.session() as session:
        project_id = add_project(session).id
        assert add_source(session, project_id, duration_ms=1).duration_ms == 1
    for duration_ms in (0, -1):
        with (
            pytest.raises(IntegrityError, match="ck_sources_duration"),
            db.session() as session,
        ):
            add_source(session, project_id, duration_ms=duration_ms)


@pytest.mark.parametrize(("start_ms", "end_ms"), [(-1, 5), (5, 5), (6, 5), (0, 0)])
def test_clip_needs_0_le_start_lt_end(db: Database, start_ms: int, end_ms: int) -> None:
    with db.session() as session:
        source_id = add_source(session, add_project(session).id).id
    with pytest.raises(IntegrityError, match="ck_clips_interval"), db.session() as session:
        add_clip(session, source_id, start_ms=start_ms, end_ms=end_ms)


def test_clip_interval_accepts_the_smallest_valid_clip(db: Database) -> None:
    with db.session() as session:
        source_id = add_source(session, add_project(session).id).id
        clip = add_clip(session, source_id, start_ms=0, end_ms=1)
        assert (clip.start_ms, clip.end_ms) == (0, 1)


@pytest.mark.parametrize(("start", "end"), [(0, None), (None, 5), (-1, 5), (5, 5), (9, 5)])
def test_recommended_interval_is_both_null_or_a_valid_interval(
    db: Database, start: int | None, end: int | None
) -> None:
    with db.session() as session:
        source_id = add_source(session, add_project(session).id).id
    with (
        pytest.raises(IntegrityError, match="ck_clips_recommended_interval"),
        db.session() as session,
    ):
        ClipRepository(session).add(
            source_id=source_id,
            title="Кліп",
            start_ms=1,
            end_ms=2,
            categories=["funny"],
            evidence_type="transcript",
            recommended_start_ms=start,
            recommended_end_ms=end,
        )


def test_enumerated_columns_reject_unknown_values(db: Database) -> None:
    with db.session() as session:
        source_id = add_source(session, add_project(session).id).id
        clip_id = add_clip(session, source_id).id

    with pytest.raises(IntegrityError, match="ck_clips_review_status"), db.session() as session:
        add_clip(session, source_id, review_status="maybe")
    with pytest.raises(IntegrityError, match="ck_clips_export_state"), db.session() as session:
        ClipRepository(session).set_export_state(clip_id, "exported")
    with pytest.raises(IntegrityError, match="ck_jobs_state"), db.session() as session:
        JobRepository(session).add(type="export", state="paused")


def test_documented_enumerations_are_accepted(db: Database) -> None:
    with db.session() as session:
        source_id = add_source(session, add_project(session).id).id
        for status in ("suggested", "approved", "rejected"):
            add_clip(session, source_id, review_status=status)
        clip_id = add_clip(session, source_id).id
        for state in ("none", "queued", "running", "verified", "failed", "cancelled", "stale"):
            ClipRepository(session).set_export_state(clip_id, state)
        for state in (
            "queued",
            "running",
            "cancelling",
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
        ):
            JobRepository(session).add(type="export", state=state)


@pytest.mark.parametrize("name", ["", "я" * 121])
def test_project_name_length_is_limited(db: Database, name: str) -> None:
    with pytest.raises(IntegrityError, match="ck_projects_name_length"), db.session() as session:
        ProjectRepository(session).add(name)


def test_project_name_is_trimmed_and_120_characters_fit(db: Database) -> None:
    with db.session() as session:
        assert ProjectRepository(session).add("  Назва  ").name == "Назва"
        assert len(ProjectRepository(session).add("я" * 120).name) == 120


def test_whitespace_only_project_name_is_rejected(db: Database) -> None:
    with pytest.raises(IntegrityError, match="ck_projects_name_length"), db.session() as session:
        ProjectRepository(session).add("   ")


def test_source_sha256_must_be_64_characters(db: Database) -> None:
    with db.session() as session:
        source = add_source(session, add_project(session).id)
        SourceRepository(session).set_hash(source.id, sha256=SHA256, hash_status="done")
        source_id = source.id
    with pytest.raises(IntegrityError, match="ck_sources_sha256_length"), db.session() as session:
        SourceRepository(session).set_hash(source_id, sha256="abc", hash_status="done")


def test_foreign_keys_are_enforced(db: Database) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), db.session() as session:
        add_source(session, "no-such-project")
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), db.session() as session:
        add_clip(session, "no-such-source")
    with db.session() as session:
        project_id = add_project(session).id
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), db.session() as session:
        ExportRepository(session).add_item(
            batch_id="no-such-batch",
            clip_id="no-such-clip",
            clip_revision=1,
            snapshot_json={},
            state="queued",
        )
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), db.session() as session:
        ExportRepository(session).add_batch(
            project_id="no-such-project", output_dir="C:\\out", profile_json={}, status="queued"
        )
    with db.session() as session:  # the valid parent is accepted
        ExportRepository(session).add_batch(
            project_id=project_id, output_dir="C:\\out", profile_json={}, status="queued"
        )


def test_parent_rows_with_children_cannot_be_deleted(db: Database) -> None:
    with db.session() as session:
        project_id = add_project(session).id
        add_source(session, project_id)
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), db.session() as session:
        session.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_id})


def test_clip_cannot_appear_twice_in_one_batch(db: Database) -> None:
    with db.session() as session:
        project_id = add_project(session).id
        source_id = add_source(session, project_id).id
        clip = add_clip(session, source_id)
        batch = ExportRepository(session).add_batch(
            project_id=project_id, output_dir="C:\\out", profile_json={}, status="queued"
        )
        ExportRepository(session).add_item(
            batch_id=batch.id, clip_id=clip.id, clip_revision=1, snapshot_json={}, state="queued"
        )
        batch_id, clip_id = batch.id, clip.id
    with (
        pytest.raises(
            IntegrityError,
            match=r"UNIQUE constraint failed: export_items.batch_id, export_items.clip_id",
        ),
        db.session() as session,
    ):
        ExportRepository(session).add_item(
            batch_id=batch_id, clip_id=clip_id, clip_revision=1, snapshot_json={}, state="queued"
        )


def test_json_columns_are_stored_with_a_schema_version(db: Database) -> None:
    with db.session() as session:
        source_id = add_source(session, add_project(session).id).id
        clip_id = add_clip(session, source_id, categories=["funny", "game_details"]).id

    with db.engine.connect() as connection:
        raw = connection.execute(
            text("SELECT categories FROM clips WHERE id = :id"), {"id": clip_id}
        ).scalar_one()
    assert raw == '{"schema_version":1,"value":["funny","game_details"]}'

    with db.session() as session:
        clip = ClipRepository(session).get(clip_id)
        assert clip is not None
        assert clip.categories == ["funny", "game_details"]
        assert clip.scores is None


def test_json_is_stored_as_utf8_not_as_escapes(db: Database) -> None:
    with db.session() as session:
        source = add_source(session, add_project(session).id)
        clip = add_clip(session, source.id, title="Кліп")
        clip_id = clip.id
        ClipRepository(session).update(clip_id, expected_revision=1, categories=["смішне"])
    with db.engine.connect() as connection:
        raw = connection.execute(
            text("SELECT categories FROM clips WHERE id = :id"), {"id": clip_id}
        ).scalar_one()
    assert "смішне" in raw


def test_unknown_json_schema_version_is_refused_on_read(db: Database) -> None:
    with db.session() as session:
        source_id = add_source(session, add_project(session).id).id
        clip_id = add_clip(session, source_id).id
    with db.session() as session:
        session.execute(
            text("UPDATE clips SET categories = :value WHERE id = :id"),
            {"value": '{"schema_version":2,"value":["funny"]}', "id": clip_id},
        )
    with pytest.raises(StorageError), db.session() as session:
        ClipRepository(session).get(clip_id)
