"""Migration 0007 and the analysis tables (ADR 041 §8): runs, windows and attempts, and the evidence
interval and analysis run of a clip. Upgrades and downgrades run on real migration files."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clipforge.storage import (
    AnalysisRun,
    AnalysisRunRepository,
    Base,
    ClipRepository,
    Database,
    ExportRepository,
    NewSegment,
    RecordNotFound,
    TranscriptRepository,
    open_database,
)
from clipforge.storage.database import BACKUP_DIRNAME, DB_FILENAME
from clipforge.storage.migrate import MIGRATIONS_DIR
from clipforge.storage.models import (
    ANALYSIS_ATTEMPT_KINDS,
    ANALYSIS_ATTEMPT_STATES,
    ANALYSIS_RUN_STATUSES,
    ANALYSIS_WINDOW_STATES,
)
from clipforge.storage.util import new_id

from .support import head_revision
from .test_storage_support import add_project, add_source, counting_clock

COVERAGE = [{"start_ms": 0, "end_ms": 60_000, "state": "succeeded"}]
PROFILE = {
    "categories": ["funny"],
    "custom_instruction": "",
    "min_duration_ms": 5_000,
    "max_duration_ms": 60_000,
    "max_candidates": 10,
    "allow_partial": False,
}
PRICE = {"prompt_usd_per_mtok": "0.5", "completion_usd_per_mtok": "1.5", "max_tokens": 4096}
PROMPT_VERSION = "sha256:0123456789abcdef"
STAMP = "2026-11-15T10:00:00.000000Z"
MAX_RAW_OUTPUT = 1_048_576
ANALYSIS_TABLES = frozenset({"analysis_runs", "analysis_windows", "analysis_attempts"})
CLIP_COLUMNS_0006 = frozenset(
    {
        "id",
        "source_id",
        "analysis_run_id",
        "revision",
        "title",
        "start_ms",
        "end_ms",
        "recommended_start_ms",
        "recommended_end_ms",
        "categories",
        "evidence_type",
        "evidence_ids",
        "reason",
        "scores",
        "rank",
        "visual_review_required",
        "review_status",
        "export_state",
        "created_at",
        "updated_at",
    }
)
CLIP_ID = "clip-0006"
# A clip as revision 0006 stores it: the evidence columns do not exist yet.
_CLIP_INSERT_0006 = (
    "INSERT INTO clips (id, source_id, analysis_run_id, revision, title, start_ms, end_ms, "
    "recommended_start_ms, recommended_end_ms, categories, evidence_type, evidence_ids, reason, "
    "scores, rank, visual_review_required, review_status, export_state, created_at, updated_at) "
    "VALUES (:id, :source_id, NULL, 1, :title, 1000, 5000, NULL, NULL, :categories, 'manual', "
    ":evidence_ids, NULL, NULL, NULL, 0, 'approved', 'verified', :stamp, :stamp)"
)
# Raw rows for the CHECK and FK tests: no repository, so the value reaches the database as given.
_RUN_SQL = (
    "INSERT INTO analysis_runs (id, source_id, transcript_id, transcript_revision, profile_json, "
    "model, provider, prompt_version, schema_version, unit_level, price_snapshot_json, status, "
    "error_code, diagnostics_json, created_at, updated_at) VALUES (:id, :source_id, "
    ":transcript_id, :transcript_revision, :profile_json, 'm', 'p', :prompt_version, 1, "
    "'segment', :price_snapshot_json, :status, NULL, NULL, :stamp, :stamp)"
)
_WINDOW_SQL = (
    "INSERT INTO analysis_windows (id, run_id, idx, first, core_first, core_last, last, state, "
    "attempts, repair_used, error_code, result_json) VALUES (:id, :run_id, :idx, :first, "
    ":core_first, :core_last, :last, :state, :attempts, :repair_used, NULL, NULL)"
)
_ATTEMPT_SQL = (
    "INSERT INTO analysis_attempts (id, window_id, kind, request_id, spend_entry_id, state, "
    "http_status, error_code, raw_output, usage_json, created_at) VALUES (:id, :window_id, :kind, "
    "NULL, NULL, :state, NULL, NULL, :raw_output, NULL, :created_at)"
)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_database(tmp_path / "data")
    try:
        yield database
    finally:
        database.close()


@dataclass(frozen=True, slots=True)
class Seed:
    project_id: str
    source_id: str
    transcript_id: str
    run_id: str
    window_id: str


@pytest.fixture
def seeded(db: Database) -> Seed:
    return seed(db)


def add_transcript(session: Session, source_id: str) -> str:
    """An imported revision with two segments; the analysis runs refer to it."""
    return (
        TranscriptRepository(session)
        .add_revision(
            source_id=source_id,
            origin="import",
            timing_level="segment",
            language="uk",
            duration_ms=60_000,
            coverage=COVERAGE,
            metadata={"format": "normalized_json", "offset_ms": 0},
            segments=[
                NewSegment(id="s1", ordinal=0, start_ms=0, end_ms=5_000, text="перша репліка"),
                NewSegment(id="s2", ordinal=1, start_ms=5_000, end_ms=9_000, text="друга репліка"),
            ],
        )
        .id
    )


def add_run(
    runs: AnalysisRunRepository, source_id: str, transcript_id: str, status: str = "queued"
) -> AnalysisRun:
    return runs.add_run(
        source_id=source_id,
        transcript_id=transcript_id,
        transcript_revision=1,
        profile_json=PROFILE,
        model="test/selector",
        provider="openrouter",
        prompt_version=PROMPT_VERSION,
        schema_version=1,
        unit_level="segment",
        price_snapshot_json=PRICE,
        status=status,
    )


def seed(db: Database) -> Seed:
    """A project with a source, a transcript, one run with one window, and one export batch."""
    with db.write_session() as session:
        project_id = add_project(session).id
        source_id = add_source(session, project_id).id
        transcript_id = add_transcript(session, source_id)
        run = add_run(AnalysisRunRepository(session), source_id, transcript_id, "completed")
        window = AnalysisRunRepository(session).add_window(
            run_id=run.id, idx=0, first=0, core_first=0, core_last=1, last=1, state="succeeded"
        )
        return Seed(
            project_id=project_id,
            source_id=source_id,
            transcript_id=transcript_id,
            run_id=run.id,
            window_id=window.id,
        )


def run_row(seeded: Seed, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": new_id(),
        "source_id": seeded.source_id,
        "transcript_id": seeded.transcript_id,
        "transcript_revision": 1,
        "profile_json": '{"schema_version":1,"value":{}}',
        "prompt_version": PROMPT_VERSION,
        "price_snapshot_json": '{"schema_version":1,"value":{}}',
        "status": "queued",
        "stamp": STAMP,
    }
    row.update(overrides)
    return row


def window_row(seeded: Seed, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": new_id(),
        "run_id": seeded.run_id,
        "idx": 1,
        "first": 0,
        "core_first": 0,
        "core_last": 1,
        "last": 1,
        "state": "pending",
        "attempts": 0,
        "repair_used": 0,
    }
    row.update(overrides)
    return row


def attempt_row(seeded: Seed, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": new_id(),
        "window_id": seeded.window_id,
        "kind": "first",
        "state": "succeeded",
        "raw_output": None,
        "created_at": STAMP,
    }
    row.update(overrides)
    return row


def insert_raw(db: Database, sql: str, row: Mapping[str, object]) -> None:
    with db.engine.begin() as connection:
        connection.execute(text(sql), dict(row))


def table_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {name for (name,) in rows}


def stored_revision(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        (version,) = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert isinstance(version, str)
    return version


def clip_columns(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {row[1] for row in connection.execute("PRAGMA table_info(clips)").fetchall()}


def clip_parents(path: Path) -> set[str]:
    """The tables the clips foreign keys refer to."""
    with closing(sqlite3.connect(path)) as connection:
        return {row[2] for row in connection.execute("PRAGMA foreign_key_list(clips)").fetchall()}


def export_item_parents(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("PRAGMA foreign_key_list(export_items)").fetchall()
        return {row[2] for row in rows}


def clip_rows(path: Path) -> list[tuple[object, ...]]:
    """The clip columns that exist in every revision from 0001 on, as stored."""
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT id, title, start_ms, end_ms, categories, evidence_type, evidence_ids, "
            "review_status, export_state, visual_review_required, created_at, updated_at "
            "FROM clips ORDER BY id"
        ).fetchall()


def foreign_key_violations(path: Path) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("PRAGMA foreign_key_check").fetchall()


def migrations_through_0006(root: Path) -> Path:
    """A throw-away migrations directory: the real env.py and the revisions 0001 to 0006."""
    (root / "versions").mkdir(parents=True)
    shutil.copyfile(MIGRATIONS_DIR / "env.py", root / "env.py")
    for source in sorted((MIGRATIONS_DIR / "versions").glob("r000[1-6]_*.py")):
        shutil.copyfile(source, root / "versions" / source.name)
    return root


def alembic_config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
    config.attributes["connection"] = connection
    return config


def run_downgrade(db: Database, revision: str) -> None:
    with db.engine.connect() as connection:
        command.downgrade(alembic_config(connection), revision)
        connection.commit()


def run_upgrade(db: Database, revision: str) -> None:
    with db.engine.connect() as connection:
        command.upgrade(alembic_config(connection), revision)
        connection.commit()


# --- migration -----------------------------------------------------------------------------------


def test_upgrading_from_0006_keeps_the_clips_and_their_export_items(tmp_path: Path) -> None:
    before = migrations_through_0006(tmp_path / "through-0006")
    data_dir = tmp_path / "data"
    db_path = data_dir / DB_FILENAME
    with open_database(data_dir, migrations_dir=before) as old, old.write_session() as session:
        project_id = add_project(session, "Старий проєкт").id
        source_id = add_source(session, project_id).id
        # ClipRepository would write the evidence columns, which revision 0006 does not have.
        session.execute(
            text(_CLIP_INSERT_0006),
            {
                "id": CLIP_ID,
                "source_id": source_id,
                "title": "Старий кліп",
                "categories": '{"schema_version":1,"value":["funny"]}',
                "evidence_ids": '{"schema_version":1,"value":[]}',
                "stamp": STAMP,
            },
        )
        batch_id = (
            ExportRepository(session)
            .add_batch(
                project_id=project_id, output_dir="C:\\out", profile_json={}, status="queued"
            )
            .id
        )
        ExportRepository(session).add_item(
            batch_id=batch_id,
            clip_id=CLIP_ID,
            clip_revision=1,
            snapshot_json={"title": "Старий кліп"},
            state="verified",
        )
    rows_before = clip_rows(db_path)
    assert ANALYSIS_TABLES.isdisjoint(table_names(db_path))

    with open_database(data_dir) as upgraded:
        assert stored_revision(db_path) == head_revision()
        with upgraded.session() as session:
            clip = ClipRepository(session).get(CLIP_ID)
            assert clip is not None
            assert (clip.title, clip.categories, clip.review_status, clip.export_state) == (
                "Старий кліп",
                ["funny"],
                "approved",
                "verified",
            )
            assert (clip.analysis_run_id, clip.evidence_start_ms, clip.evidence_end_ms) == (
                None,
                None,
                None,
            )
            items = ExportRepository(session).list_items(batch_id)
            assert [item.clip_id for item in items] == [CLIP_ID]
        with upgraded.engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []

    assert clip_rows(db_path) == rows_before
    assert "clips" in export_item_parents(db_path)
    assert foreign_key_violations(db_path) == []
    backup = next((data_dir / BACKUP_DIRNAME).iterdir())
    assert backup.name.endswith("-from-0006.sqlite3")
    assert "analysis_runs" not in table_names(backup)


def test_downgrade_to_0006_restores_its_schema_and_upgrading_again_keeps_the_data(
    db: Database,
) -> None:
    ids = seed(db)
    with db.write_session() as session:
        clip_id = (
            ClipRepository(session)
            .add(
                source_id=ids.source_id,
                title="Кліп",
                start_ms=1_000,
                end_ms=5_000,
                categories=["funny"],
                evidence_type="transcript",
                evidence_ids=["s1"],
                analysis_run_id=ids.run_id,
                evidence_start_ms=0,
                evidence_end_ms=4_000,
            )
            .id
        )
        batch_id = (
            ExportRepository(session)
            .add_batch(
                project_id=ids.project_id, output_dir="C:\\out", profile_json={}, status="queued"
            )
            .id
        )
        ExportRepository(session).add_item(
            batch_id=batch_id, clip_id=clip_id, clip_revision=1, snapshot_json={}, state="queued"
        )
    db_path = db.data_dir / DB_FILENAME
    rows_before = clip_rows(db_path)

    run_downgrade(db, "0006")

    assert stored_revision(db_path) == "0006"
    assert ANALYSIS_TABLES.isdisjoint(table_names(db_path))
    assert clip_columns(db_path) == CLIP_COLUMNS_0006
    assert clip_parents(db_path) == {"sources"}
    assert clip_rows(db_path) == rows_before
    assert "clips" in export_item_parents(db_path)
    assert foreign_key_violations(db_path) == []

    run_upgrade(db, "head")

    assert stored_revision(db_path) == head_revision()
    assert table_names(db_path) >= ANALYSIS_TABLES
    assert clip_parents(db_path) == {"sources", "analysis_runs"}
    assert clip_rows(db_path) == rows_before
    with db.session() as session:
        clip = ClipRepository(session).get(clip_id)
        assert clip is not None
        # The analysis run went with the downgrade, so the link is gone; the evidence is too.
        assert (clip.analysis_run_id, clip.evidence_start_ms, clip.evidence_end_ms) == (
            None,
            None,
            None,
        )
    with db.engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


# --- constraints ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"status": "done"}, "ck_analysis_runs_status"),
        ({"source_id": "missing"}, "FOREIGN KEY"),
        ({"transcript_id": "missing"}, "FOREIGN KEY"),
    ],
)
def test_run_constraints(
    db: Database, seeded: Seed, overrides: dict[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        insert_raw(db, _RUN_SQL, run_row(seeded, **overrides))


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"first": 2}, "ck_analysis_windows_span"),
        ({"core_first": 2}, "ck_analysis_windows_span"),
        ({"core_last": 2}, "ck_analysis_windows_span"),
        ({"state": "done"}, "ck_analysis_windows_state"),
        ({"attempts": -1}, "ck_analysis_windows_attempts"),
        ({"repair_used": 2}, "ck_analysis_windows_repair_used"),
        ({"run_id": "missing"}, "FOREIGN KEY"),
    ],
)
def test_window_constraints(
    db: Database, seeded: Seed, overrides: dict[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        insert_raw(db, _WINDOW_SQL, window_row(seeded, **overrides))


def test_a_window_index_is_unique_within_its_run_only(db: Database, seeded: Seed) -> None:
    with (
        pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: analysis_windows\.run_id"),
        db.write_session() as session,
    ):
        AnalysisRunRepository(session).add_window(
            run_id=seeded.run_id, idx=0, first=0, core_first=0, core_last=1, last=1
        )
    with db.write_session() as session:
        runs = AnalysisRunRepository(session)
        other_run_id = add_run(runs, seeded.source_id, seeded.transcript_id).id
        runs.add_window(run_id=other_run_id, idx=0, first=0, core_first=0, core_last=1, last=1)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"kind": "retry"}, "ck_analysis_attempts_kind"),
        ({"state": "done"}, "ck_analysis_attempts_state"),
        ({"raw_output": "a" * (MAX_RAW_OUTPUT + 1)}, "ck_analysis_attempts_raw_output"),
        ({"window_id": "missing"}, "FOREIGN KEY"),
    ],
)
def test_attempt_constraints(
    db: Database, seeded: Seed, overrides: dict[str, object], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        insert_raw(db, _ATTEMPT_SQL, attempt_row(seeded, **overrides))


def test_raw_output_is_capped_in_utf8_bytes_not_characters(db: Database, seeded: Seed) -> None:
    """600 000 Cyrillic characters are 1.2 MB of UTF-8 and over the cap; 524 288 fit exactly."""
    with pytest.raises(IntegrityError, match="ck_analysis_attempts_raw_output"):
        insert_raw(db, _ATTEMPT_SQL, attempt_row(seeded, raw_output="а" * 600_000))
    insert_raw(db, _ATTEMPT_SQL, attempt_row(seeded, raw_output="а" * 524_288))


def test_the_boundary_values_are_accepted(db: Database, seeded: Seed) -> None:
    """A window of one unit, a raw output of exactly 1 048 576 ASCII bytes, no raw output."""
    insert_raw(
        db,
        _WINDOW_SQL,
        window_row(seeded, first=4, core_first=4, core_last=4, last=4, attempts=0),
    )
    insert_raw(db, _ATTEMPT_SQL, attempt_row(seeded, raw_output="a" * MAX_RAW_OUTPUT))
    insert_raw(db, _ATTEMPT_SQL, attempt_row(seeded, raw_output=None))


def test_every_status_state_and_kind_of_the_vocabularies_is_accepted(
    db: Database, seeded: Seed
) -> None:
    for status in ANALYSIS_RUN_STATUSES:
        insert_raw(db, _RUN_SQL, run_row(seeded, status=status))
    for idx, state in enumerate(ANALYSIS_WINDOW_STATES, start=1):
        insert_raw(db, _WINDOW_SQL, window_row(seeded, idx=idx, state=state))
    for kind in ANALYSIS_ATTEMPT_KINDS:
        for state in ANALYSIS_ATTEMPT_STATES:
            insert_raw(db, _ATTEMPT_SQL, attempt_row(seeded, kind=kind, state=state))
    with db.session() as session:
        runs = AnalysisRunRepository(session)
        assert len(runs.list_runs_for_source(seeded.source_id)) == 1 + len(ANALYSIS_RUN_STATUSES)
        assert len(runs.list_windows(seeded.run_id)) == 1 + len(ANALYSIS_WINDOW_STATES)
        attempts = len(ANALYSIS_ATTEMPT_KINDS) * len(ANALYSIS_ATTEMPT_STATES)
        assert len(runs.list_attempts(seeded.window_id)) == attempts


def test_the_vocabularies_are_the_ones_of_adr_041() -> None:
    assert ANALYSIS_RUN_STATUSES == (
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    )
    assert ANALYSIS_WINDOW_STATES == (
        "pending",
        "running",
        "succeeded",
        "failed",
        "unknown_outcome",
        "cancelled",
    )
    assert ANALYSIS_ATTEMPT_KINDS == ("first", "repair")


@pytest.mark.parametrize(("start", "end"), [(-1, 5), (5, 5), (6, 5), (None, 5), (0, None)])
def test_clip_evidence_is_both_null_or_a_valid_interval(
    db: Database, seeded: Seed, start: int | None, end: int | None
) -> None:
    with (
        pytest.raises(IntegrityError, match="ck_clips_evidence_interval"),
        db.session() as session,
    ):
        ClipRepository(session).add(
            source_id=seeded.source_id,
            title="Кліп",
            start_ms=1,
            end_ms=2,
            categories=["funny"],
            evidence_type="transcript",
            evidence_start_ms=start,
            evidence_end_ms=end,
        )


def test_a_clip_needs_an_existing_analysis_run(db: Database, seeded: Seed) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), db.session() as session:
        ClipRepository(session).add(
            source_id=seeded.source_id,
            title="Кліп",
            start_ms=1,
            end_ms=2,
            categories=["funny"],
            evidence_type="transcript",
            analysis_run_id="missing",
        )


# --- repository ----------------------------------------------------------------------------------


def test_a_run_and_its_windows_round_trip(db: Database) -> None:
    with db.write_session() as session:
        project_id = add_project(session).id
        source_id = add_source(session, project_id).id
        transcript_id = add_transcript(session, source_id)
        runs = AnalysisRunRepository(session)
        run_id = add_run(runs, source_id, transcript_id).id
        # Added out of order: the list follows the index, not the insertion.
        later = runs.add_window(run_id=run_id, idx=1, first=3, core_first=3, core_last=5, last=6)
        first = runs.add_window(run_id=run_id, idx=0, first=0, core_first=0, core_last=2, last=3)
        later_id, first_id = later.id, first.id
    with db.session() as session:
        stored = AnalysisRunRepository(session).get_run(run_id)
        assert stored is not None
        assert (stored.source_id, stored.transcript_id, stored.transcript_revision) == (
            source_id,
            transcript_id,
            1,
        )
        assert (stored.status, stored.error_code, stored.diagnostics_json) == ("queued", None, None)
        assert stored.profile_json == PROFILE
        assert stored.price_snapshot_json == PRICE
        assert (stored.model, stored.provider, stored.prompt_version) == (
            "test/selector",
            "openrouter",
            PROMPT_VERSION,
        )
        assert (stored.schema_version, stored.unit_level) == (1, "segment")
        windows = AnalysisRunRepository(session).list_windows(run_id)
        assert [(w.id, w.idx, w.first, w.core_first, w.core_last, w.last) for w in windows] == [
            (first_id, 0, 0, 0, 2, 3),
            (later_id, 1, 3, 3, 5, 6),
        ]
        assert [(w.state, w.attempts, w.repair_used, w.result_json) for w in windows] == [
            ("pending", 0, False, None),
            ("pending", 0, False, None),
        ]
        assert AnalysisRunRepository(session).list_windows("missing") == []
        assert AnalysisRunRepository(session).get_run("missing") is None


def test_windows_and_runs_change_only_the_fields_that_are_allowed(db: Database) -> None:
    candidates = [{"start_id": "s1", "end_id": "s2", "title": "Момент"}]
    clock = counting_clock()  # the run is created and updated from one clock: created < updated
    with db.write_session() as session:
        project_id = add_project(session).id
        source_id = add_source(session, project_id).id
        transcript_id = add_transcript(session, source_id)
        runs = AnalysisRunRepository(session, now=clock)
        run_id = add_run(runs, source_id, transcript_id).id
        window_id = runs.add_window(
            run_id=run_id, idx=0, first=0, core_first=0, core_last=2, last=3
        ).id
    with db.write_session() as session:
        runs = AnalysisRunRepository(session, now=clock)
        runs.update_window(
            window_id,
            state="succeeded",
            attempts=2,
            repair_used=True,
            error_code=None,
            result_json=candidates,
        )
        runs.update_run(run_id, status="completed", diagnostics_json={"over_cap": 1})
    with db.session() as session:
        runs = AnalysisRunRepository(session)
        (window,) = runs.list_windows(run_id)
        assert (window.state, window.attempts, window.repair_used) == ("succeeded", 2, True)
        assert window.result_json == candidates
        assert (window.core_first, window.core_last) == (0, 2)
        stored = runs.get_run(run_id)
        assert stored is not None
        assert (stored.status, stored.diagnostics_json) == ("completed", {"over_cap": 1})
        assert stored.updated_at > stored.created_at


def test_attempts_round_trip_and_update(db: Database) -> None:
    ids = seed(db)
    with db.write_session() as session:
        attempts = AnalysisRunRepository(session, now=counting_clock())
        first = attempts.add_attempt(
            window_id=ids.window_id,
            kind="first",
            state="failed",
            request_id="req-1",
            spend_entry_id=None,
            http_status=200,
            error_code="ANALYSIS_INVALID_OUTPUT",
            raw_output='{"candidates": [',
            usage_json={"prompt_tokens": 120},
        )
        attempts.add_attempt(
            window_id=ids.window_id,
            kind="repair",
            state="succeeded",
            request_id="req-2",
            http_status=200,
            raw_output='{"candidates": []}',
            usage_json={"prompt_tokens": 180},
        )
        first_id = first.id
    with db.write_session() as session:
        AnalysisRunRepository(session).update_attempt(
            first_id, state="unknown_outcome", error_code="ANALYSIS_UNKNOWN_OUTCOME"
        )
    with db.session() as session:
        listed = AnalysisRunRepository(session).list_attempts(ids.window_id)
        assert [(a.kind, a.state, a.request_id, a.http_status) for a in listed] == [
            ("first", "unknown_outcome", "req-1", 200),
            ("repair", "succeeded", "req-2", 200),
        ]
        assert listed[0].error_code == "ANALYSIS_UNKNOWN_OUTCOME"
        assert listed[0].raw_output == '{"candidates": ['
        assert listed[1].usage_json == {"prompt_tokens": 180}
        assert AnalysisRunRepository(session).list_attempts("missing") == []


def test_attempts_that_share_a_created_at_stay_in_insertion_order_whatever_their_ids(
    db: Database,
) -> None:
    # Ids are random: the attempt inserted first has the id that sorts last, yet comes first.
    ids = seed(db)
    insert_raw(db, _ATTEMPT_SQL, attempt_row(ids, id="zz-first", state="failed", created_at=STAMP))
    insert_raw(
        db,
        _ATTEMPT_SQL,
        attempt_row(ids, id="aa-second", kind="repair", created_at=STAMP),
    )
    with db.session() as session:
        listed = AnalysisRunRepository(session).list_attempts(ids.window_id)
    assert [attempt.id for attempt in listed] == ["zz-first", "aa-second"]


def test_updates_refuse_fields_that_are_not_mutable_and_unknown_rows(db: Database) -> None:
    ids = seed(db)
    with db.write_session() as session:
        attempt_id = (
            AnalysisRunRepository(session).add_attempt(
                window_id=ids.window_id, kind="first", state="running"
            )
        ).id
    with (
        pytest.raises(ValueError, match="source_id"),
        db.write_session() as session,
    ):
        AnalysisRunRepository(session).update_run(ids.run_id, source_id="other")
    with (
        pytest.raises(ValueError, match="run_id"),
        db.write_session() as session,
    ):
        AnalysisRunRepository(session).update_window(ids.window_id, run_id="other")
    with (
        pytest.raises(ValueError, match="kind"),
        db.write_session() as session,
    ):
        AnalysisRunRepository(session).update_attempt(attempt_id, kind="repair")
    with pytest.raises(RecordNotFound), db.write_session() as session:
        AnalysisRunRepository(session).update_run("missing", status="failed")
    with pytest.raises(RecordNotFound), db.write_session() as session:
        AnalysisRunRepository(session).update_window("missing", state="failed")
    with pytest.raises(RecordNotFound), db.write_session() as session:
        AnalysisRunRepository(session).update_attempt("missing", state="failed")


def test_runs_of_a_source_are_listed_newest_first(db: Database) -> None:
    clock = counting_clock()
    with db.write_session() as session:
        project_id = add_project(session).id
        source_id = add_source(session, project_id).id
        other_source_id = add_source(session, project_id).id
        transcript_id = add_transcript(session, source_id)
        other_transcript_id = add_transcript(session, other_source_id)
        runs = AnalysisRunRepository(session, now=clock)
        older_id = add_run(runs, source_id, transcript_id).id
        newer_id = add_run(runs, source_id, transcript_id, status="running").id
        foreign_id = add_run(runs, other_source_id, other_transcript_id).id
    with db.session() as session:
        runs = AnalysisRunRepository(session)
        assert [run.id for run in runs.list_runs_for_source(source_id)] == [newer_id, older_id]
        assert [run.id for run in runs.list_runs_for_source(other_source_id)] == [foreign_id]
        assert runs.list_runs_for_source("missing") == []


def test_a_clip_keeps_its_evidence_and_its_analysis_run(db: Database) -> None:
    ids = seed(db)
    with db.write_session() as session:
        clip_id = (
            ClipRepository(session)
            .add(
                source_id=ids.source_id,
                title="Смішний момент",
                start_ms=1_000,
                end_ms=4_000,
                categories=["funny"],
                evidence_type="transcript",
                evidence_ids=["s1"],
                analysis_run_id=ids.run_id,
                evidence_start_ms=0,
                evidence_end_ms=3_000,
                recommended_start_ms=500,
                recommended_end_ms=4_500,
            )
            .id
        )
    with db.session() as session:
        stored = ClipRepository(session).get(clip_id)
        assert stored is not None
        assert (stored.analysis_run_id, stored.evidence_start_ms, stored.evidence_end_ms) == (
            ids.run_id,
            0,
            3_000,
        )
        assert (stored.recommended_start_ms, stored.recommended_end_ms) == (500, 4_500)
