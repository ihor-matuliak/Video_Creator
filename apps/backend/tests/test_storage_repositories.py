"""Repositories: CRUD, pagination, revision-checked clip updates, jobs."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

from clipforge.storage import (
    ClipRepository,
    Database,
    ExportRepository,
    JobRepository,
    ProjectRepository,
    RecordNotFound,
    SourceRepository,
    StaleRevision,
    open_database,
)

from .test_storage_support import SHA256, add_clip, add_project, add_source, counting_clock


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_database(tmp_path / "дані")
    try:
        yield database
    finally:
        database.close()


def seed_clip(db: Database) -> str:
    with db.session() as session:
        source_id = add_source(session, add_project(session).id).id
        return add_clip(session, source_id).id


# --- projects ---------------------------------------------------------------------------------


def test_project_archive_hides_it_but_keeps_the_data(db: Database) -> None:
    with db.session() as session:
        repo = ProjectRepository(session)
        keep = repo.add("Лишається")
        gone = repo.add("Архівний")
        repo.archive(gone.id)
        assert gone.archived_at is not None
        assert [p.id for p in repo.list_page().items] == [keep.id]
        listed = repo.list_page(include_archived=True).items
        assert {p.id for p in listed} == {keep.id, gone.id}
        assert repo.get(gone.id) is not None


def test_project_unarchive_brings_it_back_and_keeps_the_name(db: Database) -> None:
    with db.session() as session:
        repo = ProjectRepository(session, now=counting_clock())
        project = repo.add("Повернеться")
        repo.archive(project.id)
        archived_updated_at = project.updated_at
        repo.unarchive(project.id)
        assert project.archived_at is None
        assert project.name == "Повернеться"
        assert project.updated_at > archived_updated_at
        assert [p.id for p in repo.list_page().items] == [project.id]


def test_missing_project_cannot_be_unarchived(db: Database) -> None:
    with db.session() as session, pytest.raises(RecordNotFound):
        ProjectRepository(session).unarchive("nope")


def test_project_rename_trims_and_updates_the_timestamp(db: Database) -> None:
    with db.session() as session:
        repo = ProjectRepository(session, now=counting_clock())
        project = repo.add("Стара")
        created = project.updated_at
        repo.rename(project.id, "  Нова назва ")
        assert project.name == "Нова назва"
        assert project.updated_at > created


def test_missing_project_raises_record_not_found(db: Database) -> None:
    with db.session() as session, pytest.raises(RecordNotFound):
        ProjectRepository(session).rename("nope", "x")


def test_pagination_walks_every_project_once(db: Database) -> None:
    with db.session() as session:
        repo = ProjectRepository(session, now=counting_clock())
        created = [repo.add(f"Проєкт {n}").id for n in range(5)]
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    with db.session() as session:
        repo = ProjectRepository(session)
        while True:
            page = repo.list_page(limit=2, after=cursor)
            seen.extend(p.id for p in page.items)
            pages += 1
            cursor = page.next_cursor
            if cursor is None:
                break
    assert seen == created
    assert pages == 3


def test_pagination_is_stable_when_timestamps_are_equal(db: Database) -> None:
    with db.session() as session:
        repo = ProjectRepository(session, now=lambda: "2026-11-13T10:00:00.000000Z")
        created = {repo.add(f"Проєкт {n}").id for n in range(5)}
    seen: list[str] = []
    cursor: str | None = None
    with db.session() as session:
        while True:
            page = ProjectRepository(session).list_page(limit=2, after=cursor)
            seen.extend(p.id for p in page.items)
            cursor = page.next_cursor
            if cursor is None:
                break
    assert len(seen) == 5
    assert set(seen) == created


def test_pagination_rejects_bad_arguments(db: Database) -> None:
    with db.session() as session:
        repo = ProjectRepository(session)
        with pytest.raises(ValueError, match="limit"):
            repo.list_page(limit=0)
        with pytest.raises(ValueError, match="курсор"):
            repo.list_page(after="unknown")


# --- project search and summary ---------------------------------------------------------------


def names_found(db: Database, query: str | None, **kwargs: Any) -> list[str]:
    with db.session() as session:
        page = ProjectRepository(session).list_page(query=query, **kwargs)
    return [project.name for project in page.items]


def at(stamp: str) -> Callable[[], str]:
    """A clock stuck at ``2026-11-13T<stamp>.000000Z``."""
    return lambda: f"2026-11-13T{stamp}.000000Z"


def test_search_folds_case_beyond_ascii(db: Database) -> None:
    with db.session() as session:
        repo = ProjectRepository(session, now=counting_clock())
        for name in ("Київ запис", "Одеса", "Straße", "ІГОР"):
            repo.add(name)
    assert names_found(db, "київ") == ["Київ запис"]
    assert names_found(db, "КИЇВ") == ["Київ запис"]
    assert names_found(db, "ЗАПИС") == ["Київ запис"]
    assert names_found(db, "игор") == []
    assert names_found(db, "ігор") == ["ІГОР"]
    # casefold, not lower(): "ß" folds to "ss" in both directions.
    assert names_found(db, "STRASSE") == ["Straße"]
    assert names_found(db, "straße") == ["Straße"]


def test_search_is_a_trimmed_substring_and_empty_does_not_filter(db: Database) -> None:
    with db.session() as session:
        repo = ProjectRepository(session, now=counting_clock())
        for name in ("Перший запис", "Другий", "100% готово", "a_b"):
            repo.add(name)
    assert names_found(db, "  ий зап  ") == ["Перший запис"]
    everything = ["Перший запис", "Другий", "100% готово", "a_b"]
    assert names_found(db, None) == everything
    assert names_found(db, "") == everything
    assert names_found(db, " \t ") == everything
    assert names_found(db, "немає такого") == []
    # LIKE wildcards are plain text.
    assert names_found(db, "%") == ["100% готово"]
    assert names_found(db, "_") == ["a_b"]


def test_search_works_with_archived_and_cursor(db: Database) -> None:
    with db.session() as session:
        repo = ProjectRepository(session, now=counting_clock())
        ids = [repo.add(name).id for name in ("Київ 1", "Львів", "Київ 2", "Київ 3", "Київ 4")]
        repo.archive(ids[2])
    assert names_found(db, "київ") == ["Київ 1", "Київ 3", "Київ 4"]
    assert names_found(db, "київ", include_archived=True) == [
        "Київ 1",
        "Київ 2",
        "Київ 3",
        "Київ 4",
    ]
    # The cursor may be a project that the search does not show (Львів).
    assert names_found(db, "київ", after=ids[1], include_archived=True) == [
        "Київ 2",
        "Київ 3",
        "Київ 4",
    ]
    with db.session() as session:
        repo = ProjectRepository(session)
        first = repo.list_page(query="київ", limit=2, include_archived=True)
        assert [p.name for p in first.items] == ["Київ 1", "Київ 2"]
        assert first.next_cursor == ids[2]
        rest = repo.list_page(query="київ", limit=2, after=first.next_cursor, include_archived=True)
        assert [p.name for p in rest.items] == ["Київ 3", "Київ 4"]
        assert rest.next_cursor is None
        with pytest.raises(ValueError, match="курсор"):
            repo.list_page(query="київ", after="unknown")


def test_summarize_counts_sources_and_clips_per_project(db: Database) -> None:
    with db.session() as session:
        repo = ProjectRepository(session, now=at("10:00:00"))
        busy, one_source, empty = (repo.add(name) for name in ("Багато", "Одне", "Порожній"))
        first = add_source(session, busy.id, now=at("10:01:00"))
        second = add_source(session, busy.id, now=at("10:02:00"))
        add_clip(session, first.id, now=at("10:03:00"))
        add_clip(session, first.id, start_ms=6_000, end_ms=9_000, now=at("10:04:00"))
        add_clip(session, second.id, now=at("10:05:00"))
        lone = add_source(session, one_source.id, now=at("10:06:00"))
        # An unrelated clip of another project must not be counted for `busy`.
        add_clip(session, lone.id, now=at("10:07:00"))
        summaries = {
            s.project.id: s for s in ProjectRepository(session).summarize([busy, one_source, empty])
        }
    assert (summaries[busy.id].source_count, summaries[busy.id].clip_count) == (2, 3)
    assert (summaries[one_source.id].source_count, summaries[one_source.id].clip_count) == (1, 1)
    assert (summaries[empty.id].source_count, summaries[empty.id].clip_count) == (0, 0)
    assert summaries[busy.id].last_activity_at == "2026-11-13T10:05:00.000000Z"
    assert summaries[one_source.id].last_activity_at == "2026-11-13T10:07:00.000000Z"
    # No activity: the project's own timestamp.
    assert summaries[empty.id].last_activity_at == "2026-11-13T10:00:00.000000Z"


def test_summarize_keeps_the_order_of_its_argument(db: Database) -> None:
    with db.session() as session:
        repo = ProjectRepository(session, now=counting_clock())
        projects = [repo.add(f"Проєкт {n}") for n in range(4)]
        add_source(session, projects[1].id)
        reordered = [projects[3], projects[1], projects[0]]
        assert [s.project.id for s in repo.summarize(reordered)] == [p.id for p in reordered]
        assert repo.summarize([]) == []


def test_last_activity_is_the_latest_of_every_kind_of_activity(db: Database) -> None:
    with db.session() as session:
        project = ProjectRepository(session, now=at("10:00:00")).add("Проєкт")
        repo = ProjectRepository(session)

        def last() -> str:
            return repo.summarize([project])[0].last_activity_at

        assert last() == "2026-11-13T10:00:00.000000Z"
        source = add_source(session, project.id, now=at("10:01:00"))
        assert last() == "2026-11-13T10:01:00.000000Z"
        clip = add_clip(session, source.id, now=at("10:02:00"))
        assert last() == "2026-11-13T10:02:00.000000Z"
        ClipRepository(session, now=at("10:03:00")).update(clip.id, expected_revision=1, title="Н")
        assert last() == "2026-11-13T10:03:00.000000Z"
        ExportRepository(session, now=at("10:04:00")).add_batch(
            project_id=project.id, output_dir="C:\\Експорт", profile_json={}, status="queued"
        )
        assert last() == "2026-11-13T10:04:00.000000Z"
        ProjectRepository(session, now=at("10:05:00")).rename(project.id, "Нова назва")
        assert last() == "2026-11-13T10:05:00.000000Z"
        # An older event never lowers the value.
        add_source(session, project.id, now=at("09:00:00"))
        assert last() == "2026-11-13T10:05:00.000000Z"


def test_summarize_uses_two_queries_for_any_number_of_projects(db: Database) -> None:
    with db.session() as session:
        repo = ProjectRepository(session, now=counting_clock())
        projects = [repo.add(f"Проєкт {n}") for n in range(8)]
        for project in projects:
            add_clip(session, add_source(session, project.id).id)

    statements: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", record)
    try:
        with db.session() as session:
            summaries = ProjectRepository(session).summarize(projects)
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    assert [(s.source_count, s.clip_count) for s in summaries] == [(1, 1)] * 8
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 2


# --- sources ----------------------------------------------------------------------------------


def test_source_round_trip(db: Database) -> None:
    with db.session() as session:
        project_id = add_project(session).id
        source = add_source(session, project_id)
        source_id = source.id
    with db.session() as session:
        repo = SourceRepository(session)
        loaded = repo.get(source_id)
        assert loaded is not None
        assert loaded.display_name == "запис.mkv"
        assert loaded.mtime == 1_700_000_000_000_000_000
        assert loaded.audio_streams == [{"index": 1, "codec": "aac", "default": True}]
        assert loaded.canonical_origin == {"origin_ms": 0, "time_base": "1/1000"}
        assert loaded.sha256 is None
        repo.set_hash(source_id, sha256=SHA256, hash_status="done")
        repo.set_availability(source_id, "missing")
        assert [s.id for s in repo.list_for_project(project_id)] == [source_id]
    with db.session() as session:
        reloaded = SourceRepository(session).get(source_id)
        assert reloaded is not None
        assert (reloaded.sha256, reloaded.hash_status, reloaded.availability) == (
            SHA256,
            "done",
            "missing",
        )


# --- clips ------------------------------------------------------------------------------------


def test_new_clip_starts_at_revision_1_suggested_and_not_exported(db: Database) -> None:
    with db.session() as session:
        source_id = add_source(session, add_project(session).id).id
        clip = ClipRepository(session).add(
            source_id=source_id,
            title="Смішний момент",
            start_ms=1_000,
            end_ms=9_500,
            categories=["funny", "unexpected"],
            evidence_type="transcript",
            evidence_ids=["w1", "w9"],
            reason="Стрімер сміється",
            scores={"clarity": 3, "hook": 4},
            rank=72,
            visual_review_required=True,
            recommended_start_ms=800,
            recommended_end_ms=9_700,
        )
        clip_id = clip.id
    with db.session() as session:
        loaded = ClipRepository(session).get(clip_id)
        assert loaded is not None
        assert (loaded.revision, loaded.review_status, loaded.export_state) == (
            1,
            "suggested",
            "none",
        )
        assert loaded.categories == ["funny", "unexpected"]
        assert loaded.evidence_ids == ["w1", "w9"]
        assert loaded.scores == {"clarity": 3, "hook": 4}
        assert loaded.visual_review_required is True
        assert (loaded.recommended_start_ms, loaded.recommended_end_ms) == (800, 9_700)
        assert loaded.analysis_run_id is None


def test_update_bumps_the_revision_and_returns_the_fresh_clip(db: Database) -> None:
    clip_id = seed_clip(db)
    with db.write_session() as session:
        repo = ClipRepository(session, now=lambda: "2030-01-01T00:00:00.000000Z")
        before = repo.get(clip_id)
        assert before is not None
        updated = repo.update(clip_id, expected_revision=1, title="Нова назва", end_ms=7_000)
        assert updated is before  # same identity-mapped object, refreshed
        assert (updated.revision, updated.title, updated.end_ms) == (2, "Нова назва", 7_000)
        assert updated.updated_at == "2030-01-01T00:00:00.000000Z"
        assert updated.start_ms == 1_000


def test_review_change_is_an_edit_and_bumps_the_revision(db: Database) -> None:
    clip_id = seed_clip(db)
    with db.write_session() as session:
        repo = ClipRepository(session)
        approved = repo.update(clip_id, expected_revision=1, review_status="approved")
        assert (approved.revision, approved.review_status) == (2, "approved")
        back = repo.update(clip_id, expected_revision=2, review_status="suggested")
        assert (back.revision, back.review_status) == (3, "suggested")


def test_stale_revision_is_rejected_and_the_newer_data_is_kept(db: Database) -> None:
    clip_id = seed_clip(db)
    # Two editors read revision 1; the first one saves.
    with db.write_session() as session:
        ClipRepository(session).update(clip_id, expected_revision=1, title="Правка першого")

    with pytest.raises(StaleRevision) as stale, db.write_session() as session:
        ClipRepository(session).update(
            clip_id, expected_revision=1, title="Правка другого", start_ms=2_000
        )
    assert stale.value.clip_id == clip_id
    assert stale.value.expected_revision == 1
    assert stale.value.current_revision == 2

    with db.session() as session:
        clip = ClipRepository(session).get(clip_id)
        assert clip is not None
        assert clip.revision == 2
        assert clip.title == "Правка першого"
        assert clip.start_ms == 1_000  # nothing from the rejected update leaked in


def test_stale_review_is_rejected_too(db: Database) -> None:
    clip_id = seed_clip(db)
    with db.write_session() as session:
        ClipRepository(session).update(clip_id, expected_revision=1, title="Зміна")
    with pytest.raises(StaleRevision), db.write_session() as session:
        ClipRepository(session).update(clip_id, expected_revision=1, review_status="approved")
    with db.session() as session:
        clip = ClipRepository(session).get(clip_id)
        assert clip is not None
        assert (clip.revision, clip.review_status) == (2, "suggested")


def test_update_of_an_unknown_clip_is_not_found_not_stale(db: Database) -> None:
    with pytest.raises(RecordNotFound), db.write_session() as session:
        ClipRepository(session).update("nope", expected_revision=1, title="x")


def test_update_accepts_only_editable_fields(db: Database) -> None:
    clip_id = seed_clip(db)
    with db.write_session() as session:
        repo = ClipRepository(session)
        for forbidden in ("revision", "evidence_ids", "export_state", "source_id", "id"):
            with pytest.raises(ValueError, match="Непідтримувані"):
                repo.update(clip_id, expected_revision=1, **{forbidden: 5})
        with pytest.raises(ValueError, match="Немає змін"):
            repo.update(clip_id, expected_revision=1)
    with db.session() as session:
        clip = ClipRepository(session).get(clip_id)
        assert clip is not None
        assert clip.revision == 1


def test_update_to_an_invalid_interval_changes_nothing(db: Database) -> None:
    clip_id = seed_clip(db)
    with pytest.raises(IntegrityError, match="ck_clips_interval"), db.write_session() as session:
        ClipRepository(session).update(clip_id, expected_revision=1, title="Нова", end_ms=500)
    with db.session() as session:
        clip = ClipRepository(session).get(clip_id)
        assert clip is not None
        assert (clip.revision, clip.title, clip.end_ms) == (1, "Кліп", 5_000)


def test_concurrent_updates_with_the_same_revision_have_exactly_one_winner(db: Database) -> None:
    clip_id = seed_clip(db)
    workers = 8
    barrier = threading.Barrier(workers)

    def attempt(index: int) -> tuple[str, int]:
        barrier.wait()
        try:
            with db.write_session() as session:
                ClipRepository(session).update(
                    clip_id, expected_revision=1, title=f"Правка {index}"
                )
        except StaleRevision as stale:
            return "stale", stale.current_revision
        return "saved", index

    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(attempt, range(workers)))

    winners = [index for kind, index in outcomes if kind == "saved"]
    assert len(winners) == 1
    assert [rev for kind, rev in outcomes if kind == "stale"] == [2] * (workers - 1)
    with db.session() as session:
        clip = ClipRepository(session).get(clip_id)
        assert clip is not None
        assert (clip.revision, clip.title) == (2, f"Правка {winners[0]}")


def test_export_state_change_does_not_make_a_pending_edit_stale(db: Database) -> None:
    clip_id = seed_clip(db)
    with db.write_session() as session:
        clip = ClipRepository(session).set_export_state(clip_id, "running")
        assert (clip.revision, clip.export_state) == (1, "running")
    with db.write_session() as session:
        edited = ClipRepository(session).update(clip_id, expected_revision=1, title="Після")
        assert (edited.revision, edited.export_state) == (2, "running")
    with pytest.raises(RecordNotFound), db.write_session() as session:
        ClipRepository(session).set_export_state("nope", "failed")


def test_clip_list_filters_and_pagination(db: Database) -> None:
    with db.session() as session:
        project_id = add_project(session).id
        first = add_source(session, project_id).id
        second = add_source(session, project_id).id
        funny = add_clip(session, first, categories=["funny"])
        mixed = add_clip(session, first, categories=["funny", "action"], review_status="approved")
        other = add_clip(session, second, categories=["fails"])

    def ids(
        *,
        source_id: str | None = None,
        review_status: str | None = None,
        category: str | None = None,
    ) -> list[str]:
        with db.session() as session:
            page = ClipRepository(session).list_page(
                source_id=source_id, review_status=review_status, category=category
            )
            return [c.id for c in page.items]

    assert set(ids()) == {funny.id, mixed.id, other.id}
    assert set(ids(source_id=first)) == {funny.id, mixed.id}
    assert ids(review_status="approved") == [mixed.id]
    assert set(ids(category="funny")) == {funny.id, mixed.id}
    assert ids(category="action") == [mixed.id]
    assert ids(category="game_details") == []
    assert ids(category="funny", source_id=second) == []
    assert ids(category="x' OR '1'='1") == []  # bound parameter, never SQL text

    with db.session() as session:
        page = ClipRepository(session).list_page(limit=2)
        assert len(page.items) == 2
        assert page.next_cursor == page.items[-1].id
        rest = ClipRepository(session).list_page(limit=2, after=page.next_cursor)
        assert len(rest.items) == 1
        assert rest.next_cursor is None


# --- export -----------------------------------------------------------------------------------


def test_export_item_snapshot_is_fixed_but_progress_fields_change(db: Database) -> None:
    with db.session() as session:
        project_id = add_project(session).id
        source_id = add_source(session, project_id).id
        clip_id = add_clip(session, source_id).id
        repo = ExportRepository(session)
        batch = repo.add_batch(
            project_id=project_id,
            output_dir="C:\\Експорт",
            profile_json={"video_codec": "h264", "quality": "high"},
            status="queued",
        )
        item = repo.add_item(
            batch_id=batch.id,
            clip_id=clip_id,
            clip_revision=1,
            snapshot_json={"start_ms": 1_000, "end_ms": 5_000, "title": "Кліп"},
            state="queued",
        )
        item_id, batch_id = item.id, batch.id

    with db.session() as session:
        repo = ExportRepository(session)
        for fixed in ("snapshot_json", "clip_revision", "clip_id", "batch_id", "id"):
            with pytest.raises(ValueError, match="Непідтримувані"):
                repo.update_item(item_id, **{fixed: "x"})
        done = repo.update_item(
            item_id,
            state="verified",
            temp_path="C:\\Експорт\\.tmp",
            final_path="C:\\Експорт\\кліп.mp4",
            observed_json={"duration_ms": 4_000},
        )
        assert done.error_code is None
        repo.set_batch_status(batch_id, "succeeded")
        with pytest.raises(RecordNotFound):
            repo.update_item("nope", state="failed")
        with pytest.raises(RecordNotFound):
            repo.set_batch_status("nope", "failed")

    with db.session() as session:
        repo = ExportRepository(session)
        loaded = repo.get_batch(batch_id)
        assert loaded is not None
        assert loaded.status == "succeeded"
        assert loaded.profile_json == {"video_codec": "h264", "quality": "high"}
        (stored,) = repo.list_items(batch_id)
        assert stored.snapshot_json == {"start_ms": 1_000, "end_ms": 5_000, "title": "Кліп"}
        assert stored.observed_json == {"duration_ms": 4_000}
        assert stored.final_path == "C:\\Експорт\\кліп.mp4"


# --- jobs -------------------------------------------------------------------------------------


def test_job_state_progress_and_checkpoint(db: Database) -> None:
    with db.session() as session:
        repo = JobRepository(session, now=counting_clock())
        job = repo.add(type="export", checkpoint_json={"done": []})
        assert (job.state, job.progress_json) == ("queued", None)
        created = job.updated_at
        repo.set_state(job.id, "running")
        repo.set_progress(job.id, {"percent": 40})
        repo.set_checkpoint(job.id, {"done": ["clip-1"]})
        job_id = job.id
        assert job.updated_at > created
    with db.session() as session:
        loaded = JobRepository(session).get(job_id)
        assert loaded is not None
        assert loaded.state == "running"
        assert loaded.progress_json == {"percent": 40}
        assert loaded.checkpoint_json == {"done": ["clip-1"]}
        with pytest.raises(RecordNotFound):
            JobRepository(session).set_state("nope", "failed")
