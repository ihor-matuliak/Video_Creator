"""Migration 0003, the transcript models and their repositories: runs, chunks, immutable
revisions with words and segments, the spend ledger."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import Executable, delete, insert, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clipforge.storage import (
    Base,
    Database,
    NewSegment,
    NewWord,
    ProjectRepository,
    RecordNotFound,
    SpendRepository,
    TranscriptChunk,
    TranscriptRepository,
    TranscriptRevision,
    TranscriptRunRepository,
    TranscriptSegment,
    TranscriptWord,
    open_database,
)
from clipforge.storage.database import BACKUP_DIRNAME, DB_FILENAME
from clipforge.storage.migrate import MIGRATIONS_DIR
from clipforge.storage.util import utc_now

from .support import head_revision
from .test_storage_support import add_project, add_source

COVERAGE = [{"start_ms": 0, "end_ms": 60_000, "state": "succeeded"}]
IMMUTABLE = "TRANSCRIPT_IMMUTABLE"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_database(tmp_path / "data")
    try:
        yield database
    finally:
        database.close()


def seed_source(session: Session, *, name: str = "Проєкт") -> str:
    return add_source(session, add_project(session, name).id).id


def add_revision(
    session: Session,
    source_id: str,
    *,
    words: int = 0,
    segments: int = 0,
    origin: str = "import",
    timing_level: str | None = None,
    run_id: str | None = None,
    parent_id: str | None = None,
) -> TranscriptRevision:
    """A revision with ``words`` words ``w1..`` and ``segments`` segments ``s1..``; each item
    lasts 1 s and starts at its ordinal in seconds."""
    return TranscriptRepository(session).add_revision(
        source_id=source_id,
        origin=origin,
        timing_level=timing_level or ("word" if words else "segment"),
        language="uk",
        duration_ms=60_000,
        coverage=COVERAGE,
        metadata={"format": "normalized_json", "offset_ms": 0},
        words=[
            NewWord(
                id=f"w{n}",
                ordinal=n - 1,
                start_ms=(n - 1) * 1000,
                end_ms=n * 1000,
                text=f"слово{n}",
            )
            for n in range(1, words + 1)
        ],
        segments=[
            NewSegment(
                id=f"s{n}",
                ordinal=n - 1,
                start_ms=(n - 1) * 1000,
                end_ms=n * 1000,
                text=f"репліка {n}",
                word_ids=[f"w{n}"] if n <= words else [],
            )
            for n in range(1, segments + 1)
        ],
        run_id=run_id,
        parent_id=parent_id,
    )


def execute(db: Database, statement: Executable) -> None:
    """Run ``statement`` straight on the engine: no repository, no session, no validation."""
    with db.engine.begin() as connection:
        connection.execute(statement)


def insert_row(db: Database, model: type[Base], **columns: object) -> None:
    execute(db, insert(model).values(**columns))


def table_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {name for (name,) in rows}


def trigger_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        return {name for (name,) in rows}


# --- migration -----------------------------------------------------------------------------------


def test_a_database_at_revision_0002_is_upgraded_with_a_backup_and_keeps_its_data(
    tmp_path: Path,
) -> None:
    """Upgrades through every later migration too, so it ends at the current head."""
    up_to_previews = tmp_path / "only-0002"
    (up_to_previews / "versions").mkdir(parents=True)
    shutil.copyfile(MIGRATIONS_DIR / "env.py", up_to_previews / "env.py")
    for name in ("r0001_initial.py", "r0002_previews.py"):
        shutil.copyfile(MIGRATIONS_DIR / "versions" / name, up_to_previews / "versions" / name)
    data_dir = tmp_path / "data"
    db_path = data_dir / DB_FILENAME
    with open_database(data_dir, migrations_dir=up_to_previews) as old:
        with old.write_session() as session:
            project_id = ProjectRepository(session).add("Старий проєкт").id
        assert "transcript_revisions" not in table_names(db_path)

    with open_database(data_dir) as upgraded:
        assert {
            "transcript_runs",
            "transcript_chunks",
            "transcript_revisions",
            "transcript_words",
            "transcript_segments",
            "spend_entries",
        } <= table_names(db_path)
        with upgraded.session() as session:
            project = ProjectRepository(session).get(project_id)
            assert project is not None
            assert project.name == "Старий проєкт"
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            head_revision(),
        )
    backup = next((data_dir / BACKUP_DIRNAME).iterdir())
    assert backup.name.endswith("-from-0002.sqlite3")
    assert "transcript_revisions" not in table_names(backup)


def test_the_immutability_triggers_exist(db: Database) -> None:
    assert trigger_names(db.data_dir / DB_FILENAME) == {
        f"tr_{table}_no_{action}"
        for table in ("transcript_revisions", "transcript_words", "transcript_segments")
        for action in ("update", "delete")
    }


# --- revisions -----------------------------------------------------------------------------------


def test_a_revision_is_stored_with_its_words_and_segments(db: Database) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
        revision_id = add_revision(session, source_id, words=3, segments=2).id
    with db.session() as session:
        transcripts = TranscriptRepository(session)
        revision = transcripts.get(revision_id)
        assert revision is not None
        assert (revision.source_id, revision.revision, revision.origin) == (source_id, 1, "import")
        assert (revision.run_id, revision.parent_id) == (None, None)
        assert (revision.timing_level, revision.language, revision.duration_ms) == (
            "word",
            "uk",
            60_000,
        )
        assert (revision.word_count, revision.segment_count) == (3, 2)
        assert revision.coverage == COVERAGE
        assert revision.metadata_json == {"format": "normalized_json", "offset_ms": 0}
        words = transcripts.list_words(revision_id)
        assert [(w.id, w.ordinal, w.start_ms, w.end_ms, w.text) for w in words] == [
            ("w1", 0, 0, 1000, "слово1"),
            ("w2", 1, 1000, 2000, "слово2"),
            ("w3", 2, 2000, 3000, "слово3"),
        ]
        assert [(w.chunk_id, w.speaker_local, w.confidence) for w in words] == [
            (None, None, None)
        ] * 3
        segments = transcripts.list_segments(revision_id)
        assert [(s.id, s.word_ids, s.timing_level) for s in segments] == [
            ("s1", ["w1"], "word"),
            ("s2", ["w2"], "word"),
        ]


def test_revision_numbers_count_per_source(db: Database) -> None:
    with db.write_session() as session:
        first = seed_source(session, name="Перший")
        second = seed_source(session, name="Другий")
        numbers = [
            add_revision(session, first).revision,
            add_revision(session, second).revision,
            add_revision(session, first).revision,
            add_revision(session, first).revision,
        ]
    assert numbers == [1, 1, 2, 3]


def test_the_same_revision_number_cannot_be_stored_twice_for_a_source(db: Database) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
        add_revision(session, source_id)
    with pytest.raises(IntegrityError, match=r"uq_transcript_revisions_source_revision|UNIQUE"):
        insert_revision(db, source_id=source_id, revision=1)


def test_a_revision_keeps_its_words_while_a_later_one_is_added(db: Database) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
        first_id = add_revision(session, source_id, words=2, segments=1).id
    with db.write_session() as session:
        second_id = add_revision(session, source_id, words=4, segments=3).id
    with db.session() as session:
        transcripts = TranscriptRepository(session)
        assert [w.id for w in transcripts.list_words(first_id)] == ["w1", "w2"]
        assert [s.id for s in transcripts.list_segments(first_id)] == ["s1"]
        assert [w.id for w in transcripts.list_words(second_id)] == ["w1", "w2", "w3", "w4"]


def test_a_failed_add_revision_writes_nothing(db: Database) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
    with (
        pytest.raises(IntegrityError, match="ck_transcript_words_interval"),
        db.write_session() as session,
    ):
        TranscriptRepository(session).add_revision(
            source_id=source_id,
            origin="import",
            timing_level="word",
            language="uk",
            duration_ms=60_000,
            coverage=COVERAGE,
            metadata={},
            words=[NewWord(id="w1", ordinal=0, start_ms=5, end_ms=5, text="а")],
        )
    with db.session() as session:
        assert TranscriptRepository(session).list_page_for_source(source_id).items == []


def test_revisions_of_a_source_are_paged_in_revision_order(db: Database) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
        other_id = seed_source(session, name="Інший")
        ids = [add_revision(session, source_id).id for _ in range(5)]
        foreign = add_revision(session, other_id).id
    with db.session() as session:
        transcripts = TranscriptRepository(session)
        first = transcripts.list_page_for_source(source_id, limit=2)
        assert [r.id for r in first.items] == ids[:2]
        assert first.next_cursor == ids[1]
        second = transcripts.list_page_for_source(source_id, limit=2, after=first.next_cursor)
        assert [r.id for r in second.items] == ids[2:4]
        last = transcripts.list_page_for_source(source_id, limit=2, after=second.next_cursor)
        assert [r.id for r in last.items] == ids[4:]
        assert last.next_cursor is None
        assert [r.revision for r in transcripts.list_page_for_source(source_id).items] == [
            1,
            2,
            3,
            4,
            5,
        ]
        with pytest.raises(ValueError, match="курсор"):
            transcripts.list_page_for_source(source_id, after=foreign)
        with pytest.raises(ValueError, match="курсор"):
            transcripts.list_page_for_source(source_id, after="missing")
        with pytest.raises(ValueError, match="limit"):
            transcripts.list_page_for_source(source_id, limit=0)


def test_words_and_segments_are_windowed_by_offset_and_limit(db: Database) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
        revision_id = add_revision(session, source_id, words=7, segments=3).id
    with db.session() as session:
        transcripts = TranscriptRepository(session)
        assert [w.id for w in transcripts.list_words(revision_id, offset=0, limit=3)] == [
            "w1",
            "w2",
            "w3",
        ]
        assert [w.id for w in transcripts.list_words(revision_id, offset=3, limit=3)] == [
            "w4",
            "w5",
            "w6",
        ]
        assert [w.id for w in transcripts.list_words(revision_id, offset=6, limit=3)] == ["w7"]
        assert transcripts.list_words(revision_id, offset=7, limit=3) == []
        assert [s.id for s in transcripts.list_segments(revision_id, offset=1, limit=5)] == [
            "s2",
            "s3",
        ]
        assert transcripts.list_words("missing") == []
        for offset, limit in ((-1, 5), (0, 0), (0, 1001)):
            with pytest.raises(ValueError, match=r"offset|limit"):
                transcripts.list_words(revision_id, offset=offset, limit=limit)
            with pytest.raises(ValueError, match=r"offset|limit"):
                transcripts.list_segments(revision_id, offset=offset, limit=limit)


def test_windows_follow_ordinals_not_ordinal_values(db: Database) -> None:
    """Ordinals only have to grow; the offset counts items."""
    with db.write_session() as session:
        source_id = seed_source(session)
        revision_id = (
            TranscriptRepository(session)
            .add_revision(
                source_id=source_id,
                origin="import",
                timing_level="word",
                language="uk",
                duration_ms=60_000,
                coverage=COVERAGE,
                metadata={},
                words=[
                    NewWord(
                        id=f"w{n}", ordinal=ordinal, start_ms=n * 10, end_ms=n * 10 + 5, text="а"
                    )
                    for n, ordinal in ((1, 0), (2, 5), (3, 40))
                ],
            )
            .id
        )
    with db.session() as session:
        words = TranscriptRepository(session).list_words(revision_id, offset=1, limit=1)
        assert [w.id for w in words] == ["w2"]


# --- immutability --------------------------------------------------------------------------------


def test_a_stored_revision_with_its_words_and_segments_cannot_be_updated(db: Database) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
        revision_id = add_revision(session, source_id, words=2, segments=1).id
    revisions = update(TranscriptRevision).where(TranscriptRevision.id == revision_id)
    words = update(TranscriptWord).where(TranscriptWord.revision_id == revision_id)
    segments = update(TranscriptSegment).where(TranscriptSegment.revision_id == revision_id)
    for statement in (
        revisions.values(language="en"),
        revisions.values(word_count=0),
        words.values(text="інше"),
        words.values(start_ms=1),
        segments.values(text="інша"),
        segments.values(word_ids=[]),
    ):
        with pytest.raises(IntegrityError, match=IMMUTABLE):
            execute(db, statement)


def test_a_stored_revision_with_its_words_and_segments_cannot_be_deleted(db: Database) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
        revision_id = add_revision(session, source_id, words=2, segments=1).id
    for statement in (
        delete(TranscriptWord).where(TranscriptWord.revision_id == revision_id),
        delete(TranscriptSegment).where(TranscriptSegment.revision_id == revision_id),
        delete(TranscriptRevision).where(TranscriptRevision.id == revision_id),
    ):
        with pytest.raises(IntegrityError, match=IMMUTABLE):
            execute(db, statement)


def change_language(db: Database, revision_id: str) -> None:
    with db.write_session() as session:
        revision = TranscriptRepository(session).get(revision_id)
        assert revision is not None
        revision.language = "en"
        session.flush()


def delete_the_word(db: Database, revision_id: str) -> None:
    with db.write_session() as session:
        (word,) = TranscriptRepository(session).list_words(revision_id)
        session.delete(word)
        session.flush()


def test_changing_a_loaded_revision_through_the_orm_is_refused(db: Database) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
        revision_id = add_revision(session, source_id, words=1).id
    with pytest.raises(IntegrityError, match=IMMUTABLE):
        change_language(db, revision_id)
    with pytest.raises(IntegrityError, match=IMMUTABLE):
        delete_the_word(db, revision_id)
    with db.session() as session:
        transcripts = TranscriptRepository(session)
        revision = transcripts.get(revision_id)
        assert revision is not None
        assert revision.language == "uk"
        assert len(transcripts.list_words(revision_id)) == 1


# --- constraints ---------------------------------------------------------------------------------


def insert_revision(db: Database, **overrides: object) -> None:
    """Insert a revision row directly; pass ``source_id`` and the columns to override."""
    columns: dict[str, object] = {
        "id": "r-new",
        "revision": 9,
        "origin": "import",
        "run_id": None,
        "parent_id": None,
        "timing_level": "segment",
        "language": "uk",
        "duration_ms": 1000,
        "word_count": 0,
        "segment_count": 0,
        "coverage": [],
        "metadata_json": {},
        "created_at": utc_now(),
    }
    columns.update(overrides)
    insert_row(db, TranscriptRevision, **columns)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"revision": 0}, "ck_transcript_revisions_revision"),
        ({"origin": "ocr"}, "ck_transcript_revisions_origin"),
        ({"timing_level": "phrase"}, "ck_transcript_revisions_timing_level"),
        ({"language": "u"}, "ck_transcript_revisions_language"),
        ({"language": "u" * 33}, "ck_transcript_revisions_language"),
        ({"duration_ms": 0}, "ck_transcript_revisions_duration"),
        ({"word_count": -1}, "ck_transcript_revisions_counts"),
        ({"segment_count": -1}, "ck_transcript_revisions_counts"),
        ({"timing_level": "segment", "word_count": 1}, "ck_transcript_revisions_word_level"),
        ({"origin": "stt"}, "ck_transcript_revisions_provenance"),
        ({"origin": "correction"}, "ck_transcript_revisions_provenance"),
        ({"origin": "import", "run_id": "r"}, "FOREIGN KEY|ck_transcript_revisions_provenance"),
        ({"origin": "import", "parent_id": "r"}, "FOREIGN KEY|ck_transcript_revisions_provenance"),
        ({"source_id": "missing"}, "FOREIGN KEY"),
        ({"origin": "stt", "run_id": "missing"}, "FOREIGN KEY"),
        ({"origin": "correction", "parent_id": "missing"}, "FOREIGN KEY"),
    ],
)
def test_revision_constraints(db: Database, overrides: dict[str, object], constraint: str) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
    with pytest.raises(IntegrityError, match=constraint):
        insert_revision(db, **{"source_id": source_id, **overrides})


def test_an_import_with_a_run_or_a_parent_violates_the_provenance_check(db: Database) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
        run_id = (
            TranscriptRunRepository(session)
            .add_run(
                source_id=source_id, model="m", provider="p", options_hash="h", status="running"
            )
            .id
        )
        parent_id = add_revision(session, source_id).id
    with pytest.raises(IntegrityError, match="ck_transcript_revisions_provenance"):
        insert_revision(db, source_id=source_id, run_id=run_id)
    with pytest.raises(IntegrityError, match="ck_transcript_revisions_provenance"):
        insert_revision(db, source_id=source_id, parent_id=parent_id)


def test_stt_and_correction_revisions_name_their_run_and_parent(db: Database) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
        run_id = (
            TranscriptRunRepository(session)
            .add_run(
                source_id=source_id, model="m", provider="p", options_hash="h", status="succeeded"
            )
            .id
        )
        stt = add_revision(session, source_id, words=2, origin="stt", run_id=run_id)
        correction = add_revision(
            session, source_id, words=2, origin="correction", parent_id=stt.id
        )
        stt_id, correction_id = stt.id, correction.id
    with db.session() as session:
        transcripts = TranscriptRepository(session)
        stored_stt = transcripts.get(stt_id)
        stored_correction = transcripts.get(correction_id)
        assert stored_stt is not None
        assert stored_correction is not None
        assert (
            stored_stt.origin,
            stored_stt.run_id,
            stored_stt.parent_id,
            stored_stt.revision,
        ) == (
            "stt",
            run_id,
            None,
            1,
        )
        assert (
            stored_correction.origin,
            stored_correction.run_id,
            stored_correction.parent_id,
            stored_correction.revision,
        ) == (
            "correction",
            None,
            stt_id,
            2,
        )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"ordinal": -1}, "ck_transcript_words_ordinal"),
        ({"start_ms": -1}, "ck_transcript_words_interval"),
        ({"start_ms": 500, "end_ms": 500}, "ck_transcript_words_interval"),
        ({"start_ms": 600, "end_ms": 500}, "ck_transcript_words_interval"),
        ({"text": ""}, "ck_transcript_words_text"),
        ({"text": "а" * 2001}, "ck_transcript_words_text"),
        ({"ordinal": 0}, "UNIQUE"),
        ({"id": "w1"}, "UNIQUE"),
        ({"revision_id": "missing"}, "FOREIGN KEY"),
        ({"chunk_id": "missing"}, "FOREIGN KEY"),
    ],
)
def test_word_constraints(db: Database, overrides: dict[str, object], constraint: str) -> None:
    with db.write_session() as session:
        revision_id = add_revision(session, seed_source(session), words=1).id
    columns: dict[str, object] = {
        "revision_id": revision_id,
        "id": "w2",
        "ordinal": 1,
        "start_ms": 100,
        "end_ms": 200,
        "text": "слово",
        "chunk_id": None,
        **overrides,
    }
    with pytest.raises(IntegrityError, match=constraint):
        insert_row(db, TranscriptWord, **columns)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"ordinal": -1}, "ck_transcript_segments_ordinal"),
        ({"start_ms": 300, "end_ms": 300}, "ck_transcript_segments_interval"),
        ({"text": ""}, "ck_transcript_segments_text"),
        ({"timing_level": "phrase"}, "ck_transcript_segments_timing_level"),
        ({"ordinal": 0}, "UNIQUE"),
        ({"id": "s1"}, "UNIQUE"),
        ({"revision_id": "missing"}, "FOREIGN KEY"),
    ],
)
def test_segment_constraints(db: Database, overrides: dict[str, object], constraint: str) -> None:
    with db.write_session() as session:
        revision_id = add_revision(session, seed_source(session), segments=1).id
    columns: dict[str, object] = {
        "revision_id": revision_id,
        "id": "s2",
        "ordinal": 1,
        "start_ms": 100,
        "end_ms": 200,
        "text": "репліка",
        "word_ids": [],
        "timing_level": "segment",
        **overrides,
    }
    with pytest.raises(IntegrityError, match=constraint):
        insert_row(db, TranscriptSegment, **columns)


def test_the_same_word_id_may_exist_in_two_revisions_but_not_twice_in_one(db: Database) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
        add_revision(session, source_id, words=1)
        add_revision(session, source_id, words=1)  # "w1" again, in revision 2
    with pytest.raises(IntegrityError), db.write_session() as session:
        TranscriptRepository(session).add_revision(
            source_id=source_id,
            origin="import",
            timing_level="word",
            language="uk",
            duration_ms=60_000,
            coverage=COVERAGE,
            metadata={},
            words=[
                NewWord(id="w1", ordinal=0, start_ms=0, end_ms=10, text="а"),
                NewWord(id="w1", ordinal=1, start_ms=10, end_ms=20, text="б"),
            ],
        )


# --- runs, chunks, spend -------------------------------------------------------------------------


def test_runs_and_chunks_round_trip(db: Database) -> None:
    usage = {"seconds": 12.5, "requests": 2}
    with db.write_session() as session:
        source_id = seed_source(session)
        runs = TranscriptRunRepository(session)
        run = runs.add_run(
            source_id=source_id,
            model="microsoft/mai-transcribe-2",
            provider="openrouter",
            options_hash="abc123",
            status="running",
        )
        full = runs.add_run(
            source_id=source_id,
            model="m",
            provider="p",
            options_hash="h",
            status="succeeded",
            coverage=COVERAGE,
            usage=usage,
        )
        second = runs.add_chunk(
            run_id=run.id,
            core_start_ms=300_000,
            core_end_ms=600_000,
            upload_start_ms=295_000,
            upload_end_ms=605_000,
            state="running",
            reserved_micro_usd=20_000,
        )
        first = runs.add_chunk(
            run_id=run.id,
            core_start_ms=0,
            core_end_ms=300_000,
            upload_start_ms=0,
            upload_end_ms=305_000,
        )
        run_id, full_id, first_id, second_id = run.id, full.id, first.id, second.id
    with db.session() as session:
        runs = TranscriptRunRepository(session)
        stored_run = runs.get_run(run_id)
        stored_full = runs.get_run(full_id)
        assert stored_run is not None
        assert stored_full is not None
        assert (
            stored_run.source_id,
            stored_run.model,
            stored_run.provider,
            stored_run.options_hash,
            stored_run.status,
        ) == (source_id, "microsoft/mai-transcribe-2", "openrouter", "abc123", "running")
        assert (stored_run.coverage, stored_run.usage) == (None, None)
        assert (stored_full.coverage, stored_full.usage) == (COVERAGE, usage)
        chunks = runs.list_chunks(run_id)
        assert [chunk.id for chunk in chunks] == [first_id, second_id]
        assert [
            (c.state, c.attempts, c.reserved_micro_usd, c.actual_micro_usd) for c in chunks
        ] == [
            ("pending", 0, 0, None),
            ("running", 0, 20_000, None),
        ]
        assert (chunks[0].request_id, chunks[0].response_path, chunks[0].error_code) == (
            None,
            None,
            None,
        )
        assert runs.get_run("missing") is None
        assert runs.list_chunks("missing") == []


def test_a_run_needs_an_existing_source(db: Database) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), db.write_session() as session:
        TranscriptRunRepository(session).add_run(
            source_id="missing", model="m", provider="p", options_hash="h", status="queued"
        )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"core_start_ms": -1, "upload_start_ms": -1}, "chunks_core|chunks_upload"),
        ({"core_start_ms": 5000, "core_end_ms": 5000, "upload_end_ms": 5000}, "chunks_core"),
        ({"upload_start_ms": -1}, "ck_transcript_chunks_upload"),
        ({"upload_start_ms": 4000}, "ck_transcript_chunks_upload"),
        ({"upload_end_ms": 7000}, "ck_transcript_chunks_upload"),
        ({"state": "done"}, "ck_transcript_chunks_state"),
        ({"attempts": -1}, "ck_transcript_chunks_attempts"),
        ({"reserved_micro_usd": -1}, "ck_transcript_chunks_costs"),
        ({"actual_micro_usd": -1}, "ck_transcript_chunks_costs"),
        ({"run_id": "missing"}, "FOREIGN KEY"),
    ],
)
def test_chunk_constraints(db: Database, overrides: dict[str, object], constraint: str) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
        run_id = (
            TranscriptRunRepository(session)
            .add_run(
                source_id=source_id, model="m", provider="p", options_hash="h", status="running"
            )
            .id
        )
    columns: dict[str, object] = {
        "id": "c1",
        "run_id": run_id,
        "core_start_ms": 3000,
        "core_end_ms": 8000,
        "upload_start_ms": 1000,
        "upload_end_ms": 9000,
        "state": "pending",
        "attempts": 0,
        "reserved_micro_usd": 0,
        "actual_micro_usd": None,
        **overrides,
    }
    with pytest.raises(IntegrityError, match=constraint):
        insert_row(db, TranscriptChunk, **columns)


def test_every_chunk_state_is_accepted(db: Database) -> None:
    """The six states of FR-018 and ``superseded``, for a chunk that was split (docs/05 s. 4)."""
    with db.write_session() as session:
        run_id = (
            TranscriptRunRepository(session)
            .add_run(
                source_id=seed_source(session),
                model="m",
                provider="p",
                options_hash="h",
                status="running",
            )
            .id
        )
        runs = TranscriptRunRepository(session)
        for state in (
            "pending",
            "running",
            "succeeded",
            "failed",
            "cancelled",
            "unknown_outcome",
            "superseded",
        ):
            runs.add_chunk(
                run_id=run_id,
                core_start_ms=0,
                core_end_ms=1000,
                upload_start_ms=0,
                upload_end_ms=1000,
                state=state,
            )
        assert len(runs.list_chunks(run_id)) == 7


def test_run_and_chunk_updates_change_only_the_fields_that_are_allowed(db: Database) -> None:
    coverage = [{"start_ms": 0, "end_ms": 1000, "state": "failed"}]
    with db.write_session() as session:
        runs = TranscriptRunRepository(session)
        run = runs.add_run(
            source_id=seed_source(session),
            model="m",
            provider="p",
            options_hash="h",
            status="queued",
        )
        chunk = runs.add_chunk(
            run_id=run.id,
            core_start_ms=0,
            core_end_ms=1000,
            upload_start_ms=0,
            upload_end_ms=1000,
        )
        run_id, chunk_id = run.id, chunk.id
    with db.write_session() as session:
        runs = TranscriptRunRepository(session)
        runs.update_run(run_id, status="partial", coverage=coverage, usage={"revision_id": None})
        runs.update_chunk(
            chunk_id,
            state="running",
            attempts=2,
            request_id="req-2",
            response_path="p.json",
            error_code="E",
            reserved_micro_usd=7,
            actual_micro_usd=5,
        )
    with db.session() as session:
        runs = TranscriptRunRepository(session)
        stored_run, stored_chunk = runs.get_run(run_id), runs.get_chunk(chunk_id)
        assert stored_run is not None
        assert stored_chunk is not None
        assert (stored_run.status, stored_run.coverage, stored_run.usage) == (
            "partial",
            coverage,
            {"revision_id": None},
        )
        assert (
            stored_chunk.state,
            stored_chunk.attempts,
            stored_chunk.request_id,
            stored_chunk.response_path,
            stored_chunk.error_code,
            stored_chunk.reserved_micro_usd,
            stored_chunk.actual_micro_usd,
        ) == ("running", 2, "req-2", "p.json", "E", 7, 5)
        assert runs.get_chunk("missing") is None
    with db.write_session() as session:
        runs = TranscriptRunRepository(session)
        with pytest.raises(ValueError, match="source_id"):
            runs.update_run(run_id, source_id="other")
        with pytest.raises(ValueError, match="core_start_ms"):
            runs.update_chunk(chunk_id, core_start_ms=5)
        with pytest.raises(RecordNotFound):
            runs.update_run("missing", status="failed")
        with pytest.raises(RecordNotFound):
            runs.update_chunk("missing", state="failed")


def test_imported_words_may_name_the_chunk_they_came_from(db: Database) -> None:
    with db.write_session() as session:
        source_id = seed_source(session)
        run_id = (
            TranscriptRunRepository(session)
            .add_run(
                source_id=source_id, model="m", provider="p", options_hash="h", status="running"
            )
            .id
        )
        chunk_id = (
            TranscriptRunRepository(session)
            .add_chunk(
                run_id=run_id,
                core_start_ms=0,
                core_end_ms=1000,
                upload_start_ms=0,
                upload_end_ms=1000,
            )
            .id
        )
        revision = TranscriptRepository(session).add_revision(
            source_id=source_id,
            origin="stt",
            run_id=run_id,
            timing_level="word",
            language="uk",
            duration_ms=60_000,
            coverage=COVERAGE,
            metadata={},
            words=[
                NewWord(
                    id="w1",
                    ordinal=0,
                    start_ms=0,
                    end_ms=500,
                    text="а",
                    chunk_id=chunk_id,
                    speaker_local="A",
                    confidence=0.8,
                )
            ],
        )
        revision_id = revision.id
    with db.session() as session:
        (word,) = TranscriptRepository(session).list_words(revision_id)
        assert (word.chunk_id, word.speaker_local, word.confidence) == (chunk_id, "A", 0.8)


def test_spend_entries_round_trip_and_stay_unique_per_request(db: Database) -> None:
    snapshot = {"usd_per_minute": "0.003", "captured_at": "2026-11-15"}
    with db.write_session() as session:
        spend = SpendRepository(session)
        spend.add(
            run_id="run-1",
            request_id="req-2",
            state="reserved",
            reserved_micro_usd=50_000,
            model="microsoft/mai-transcribe-2",
            price_snapshot=snapshot,
        )
        spend.add(
            run_id="run-1",
            request_id="req-1",
            state="completed",
            reserved_micro_usd=50_000,
            actual_micro_usd=31_000,
            model="microsoft/mai-transcribe-2",
            price_snapshot=snapshot,
        )
        spend.add(
            run_id="run-2",
            request_id="req-1",  # the same request id in another run is another request
            state="reserved",
            reserved_micro_usd=0,
            model="m",
            price_snapshot={},
        )
    with db.session() as session:
        entries = SpendRepository(session).list_for_run("run-1")
        assert [
            (e.request_id, e.state, e.reserved_micro_usd, e.actual_micro_usd) for e in entries
        ] == [
            ("req-1", "completed", 50_000, 31_000),
            ("req-2", "reserved", 50_000, None),
        ]
        assert all(e.price_snapshot == snapshot for e in entries)
        assert SpendRepository(session).list_for_run("missing") == []
    with pytest.raises(IntegrityError, match="UNIQUE"), db.write_session() as session:
        SpendRepository(session).add(
            run_id="run-1",
            request_id="req-1",
            state="reserved",
            reserved_micro_usd=10_000,
            model="m",
            price_snapshot={},
        )


@pytest.mark.parametrize(
    ("reserved", "actual", "constraint"),
    [(-1, None, "ck_spend_entries_reserved"), (0, -1, "ck_spend_entries_actual")],
)
def test_spend_amounts_cannot_be_negative(
    db: Database, reserved: int, actual: int | None, constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint), db.write_session() as session:
        SpendRepository(session).add(
            run_id="run-1",
            request_id="req-1",
            state="reserved",
            reserved_micro_usd=reserved,
            actual_micro_usd=actual,
            model="m",
            price_snapshot={},
        )


def test_a_spend_state_outside_the_vocabulary_is_refused(db: Database) -> None:
    with pytest.raises(IntegrityError, match="ck_spend_entries_state"), db.write_session() as s:
        SpendRepository(s).add(
            run_id="run-1",
            request_id="req-1",
            state="settled",
            reserved_micro_usd=1,
            model="m",
            price_snapshot={},
        )
