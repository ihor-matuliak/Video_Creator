"""Aggregation of analysis runs into clips (ADR 041 §6, §7, §9, §15; acceptance T-035..T-038).

Every test runs on its own temporary workspace. The source is 60 seconds long and its transcript
has ten segments of five seconds each: ``s0`` covers 0..5 s, ``s1`` 5..10 s, and so on up to ``s9``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.domain.selection_output import RawCandidate, Scores, UnitRef, validate_candidates
from clipforge.services.analysis_aggregate import (
    aggregate_run,
    read_window_result,
    window_result_json,
)
from clipforge.storage import (
    AnalysisRun,
    AnalysisRunRepository,
    Clip,
    ClipRepository,
    Database,
    NewSegment,
    TranscriptRepository,
    open_database,
)

from .test_storage_support import add_project, add_source

SOURCE_MS = 60_000
CORE = (0, 9)
SELECTED = ("funny", "action", "fails")
HIGH = Scores(clarity=4, hook=4, completeness=4, emotion=4, novelty=4, relevance=4)  # rank 100
MID = Scores(clarity=3, hook=3, completeness=3, emotion=3, novelty=3, relevance=3)  # rank 75
LOW = Scores(clarity=1, hook=1, completeness=1, emotion=1, novelty=1, relevance=1)  # rank 25
UNITS = tuple(
    UnitRef(id=f"s{i}", ordinal=i, start_ms=i * 5_000, end_ms=(i + 1) * 5_000) for i in range(10)
)
UNITS_BY_ID = {unit.id: unit for unit in UNITS}
# Padding adds at most 3 s before and 4 s after the evidence, so with 5 s segments no evidence
# entry can lie inside a recommended interval without lying inside the evidence interval too.
# The merge test therefore uses a 1 s grid.
FINE_UNITS = tuple(
    UnitRef(id=f"w{i}", ordinal=i, start_ms=i * 1_000, end_ms=(i + 1) * 1_000) for i in range(60)
)
FINE_UNITS_BY_ID = {unit.id: unit for unit in FINE_UNITS}
FINE_CORE = (0, 59)
# Two segments with the same words at different times (T-037).
TEXTS = {"s1": "Ось так", "s7": "Ось так"}
COVERAGE = [{"start_ms": 0, "end_ms": SOURCE_MS, "state": "succeeded"}]
PRICE = {"prompt_usd_per_mtok": "0.5", "completion_usd_per_mtok": "1.5", "max_tokens": 4096}
PROMPT_VERSION = "sha256:0123456789abcdef"


def profile(
    *,
    categories: Sequence[str] = SELECTED,
    min_ms: int = 5_000,
    max_ms: int = 60_000,
    max_candidates: int = 10,
) -> dict[str, Any]:
    return {
        "categories": list(categories),
        "custom_instruction": "",
        "min_duration_ms": min_ms,
        "max_duration_ms": max_ms,
        "max_candidates": max_candidates,
        "allow_partial": False,
    }


def raw(
    title: str,
    categories: Sequence[str],
    start_id: str,
    end_id: str,
    evidence: Sequence[str],
    *,
    scores: Scores = HIGH,
) -> RawCandidate:
    return RawCandidate(
        title=title,
        categories=tuple(categories),
        start_id=start_id,
        end_id=end_id,
        evidence_ids=tuple(evidence),
        reason=f"Причина: {title}",
        scores=scores,
        visual_review_required=False,
    )


def window_json(
    candidates: Sequence[RawCandidate],
    *,
    core: tuple[int, int] = CORE,
    selected: tuple[str, ...] = SELECTED,
    units: Sequence[UnitRef] = UNITS,
) -> list[dict[str, Any]]:
    result = validate_candidates(candidates, units, core, selected)
    assert result.errors == ()
    return window_result_json(result, candidates, {unit.id: unit for unit in units})


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_database(tmp_path / "data")
    try:
        yield database
    finally:
        database.close()


@dataclass(frozen=True, slots=True)
class Workspace:
    source_id: str
    transcript_id: str
    revision: int


def add_transcript(session: Session, source_id: str) -> tuple[str, int]:
    revision = TranscriptRepository(session).add_revision(
        source_id=source_id,
        origin="import",
        timing_level="segment",
        language="uk",
        duration_ms=SOURCE_MS,
        coverage=COVERAGE,
        metadata={"format": "normalized_json", "offset_ms": 0},
        segments=[
            NewSegment(
                id=unit.id,
                ordinal=unit.ordinal,
                start_ms=unit.start_ms,
                end_ms=unit.end_ms,
                text=TEXTS.get(unit.id, f"репліка {unit.ordinal}"),
            )
            for unit in UNITS
        ],
    )
    return revision.id, revision.revision


@pytest.fixture
def workspace(db: Database) -> Workspace:
    with db.write_session() as session:
        project_id = add_project(session).id
        source_id = add_source(session, project_id, duration_ms=SOURCE_MS).id
        transcript_id, revision = add_transcript(session, source_id)
    return Workspace(source_id=source_id, transcript_id=transcript_id, revision=revision)


def add_run(
    session: Session,
    ws: Workspace,
    *,
    status: str = "running",
    transcript_id: str | None = None,
    revision: int | None = None,
    run_profile: dict[str, Any] | None = None,
) -> AnalysisRun:
    return AnalysisRunRepository(session).add_run(
        source_id=ws.source_id,
        transcript_id=transcript_id if transcript_id is not None else ws.transcript_id,
        transcript_revision=revision if revision is not None else ws.revision,
        profile_json=run_profile if run_profile is not None else profile(),
        model="test/selector",
        provider="openrouter",
        prompt_version=PROMPT_VERSION,
        schema_version=1,
        unit_level="segment",
        price_snapshot_json=PRICE,
        status=status,
    )


def add_window(
    session: Session,
    run_id: str,
    entries: list[dict[str, Any]] | None,
    *,
    state: str = "succeeded",
    core: tuple[int, int] = CORE,
) -> None:
    runs = AnalysisRunRepository(session)
    window = runs.add_window(
        run_id=run_id,
        idx=0,
        first=0,
        core_first=core[0],
        core_last=core[1],
        last=core[1],
        state="pending",
    )
    runs.update_window(window.id, state=state, result_json=entries)


def add_clip(
    session: Session,
    ws: Workspace,
    *,
    title: str,
    start_ms: int,
    end_ms: int,
    review_status: str = "suggested",
    run_id: str | None = None,
    evidence: Sequence[str] = (),
    categories: Sequence[str] = ("funny",),
) -> Clip:
    return ClipRepository(session).add(
        source_id=ws.source_id,
        title=title,
        start_ms=start_ms,
        end_ms=end_ms,
        categories=categories,
        evidence_type="transcript" if run_id is not None else "manual",
        evidence_ids=list(evidence),
        review_status=review_status,
        analysis_run_id=run_id,
    )


def clip_by_id(db: Database, clip_id: str) -> Clip:
    with db.session() as session:
        clip = ClipRepository(session).get(clip_id)
    assert clip is not None
    return clip


def clips_of(db: Database, source_id: str) -> list[Clip]:
    with db.session() as session:
        return list(session.scalars(select(Clip).where(Clip.source_id == source_id)))


def run_of(db: Database, run_id: str) -> AnalysisRun:
    with db.session() as session:
        run = AnalysisRunRepository(session).get_run(run_id)
    assert run is not None
    return run


def diagnostics_of(db: Database, run_id: str) -> dict[str, Any]:
    diagnostics = run_of(db, run_id).diagnostics_json
    assert diagnostics is not None
    return diagnostics


def clip_state(clip: Clip) -> tuple[object, ...]:
    """Every stored field that a change of the clip would alter."""
    return (
        clip.revision,
        clip.title,
        clip.start_ms,
        clip.end_ms,
        clip.review_status,
        clip.analysis_run_id,
        tuple(clip.evidence_ids),
        tuple(clip.categories),
        clip.updated_at,
    )


def test_multi_category_same_event_is_one_clip(db: Database, workspace: Workspace) -> None:
    """T-036: one event labelled with two categories is one clip carrying both labels."""
    funny = raw("Смішний момент", ("funny",), "s2", "s5", ["s2", "s3", "s4", "s5"])
    action = raw("Та сама подія, дія", ("action",), "s2", "s5", ["s3", "s4"])
    with db.write_session() as session:
        run = add_run(session, workspace)
        add_window(session, run.id, window_json([funny, action]))
        run_id = run.id
    with db.write_session() as session:
        clip_ids = aggregate_run(session, run_id)

    assert len(clip_ids) == 1
    clip = clip_by_id(db, clip_ids[0])
    assert clip.categories == ["funny", "action"]
    assert clip.visual_review_required is True
    assert clip.evidence_ids == ["s2", "s3", "s4", "s5"]
    assert (clip.start_ms, clip.end_ms) == (7_000, 34_000)
    assert (clip.recommended_start_ms, clip.recommended_end_ms) == (7_000, 34_000)
    assert (clip.evidence_start_ms, clip.evidence_end_ms) == (10_000, 30_000)
    assert clip.analysis_run_id == run_id
    assert clip.review_status == "suggested"
    assert clip.evidence_type == "transcript"
    assert clip.rank == 100
    assert clip.scores == {
        "clarity": 4,
        "hook": 4,
        "completeness": 4,
        "emotion": 4,
        "novelty": 4,
        "relevance": 4,
    }
    assert diagnostics_of(db, run_id)["suppressed"] == [
        {
            "window_idx": 0,
            "index": 1,
            "reason": "duplicate_in_run",
            "rank": 100,
            "recommended_start_ms": 7_000,
            "recommended_end_ms": 34_000,
            "duplicate_of": {"kind": "candidate", "window_idx": 0, "index": 0},
            "candidate": {
                "title": "Та сама подія, дія",
                "reason": "Причина: Та сама подія, дія",
                "categories": ["action"],
                "start_id": "s2",
                "end_id": "s5",
                "evidence_start_ms": 10_000,
                "evidence_end_ms": 30_000,
                "evidence": [["s3", 15_000, 20_000], ["s4", 20_000, 25_000]],
                "scores": {
                    "clarity": 4,
                    "hook": 4,
                    "completeness": 4,
                    "emotion": 4,
                    "novelty": 4,
                    "relevance": 4,
                },
                "visual_review_required": True,
            },
        }
    ]


def test_merged_duplicates_widen_the_evidence_interval(db: Database, workspace: Workspace) -> None:
    """ADR 041 §17: two in-run duplicates merge into the kept clip. Their evidence lies outside
    the kept candidate's own interval, so the stored evidence interval widens to cover it."""
    main = raw("Головний", ("funny",), "w20", "w24", ["w20", "w21", "w22", "w23", "w24"])
    before = raw("Дубль раніше", ("funny",), "w19", "w22", ["w19", "w21", "w22"], scores=MID)
    after = raw(
        "Дубль пізніше",
        ("funny",),
        "w22",
        "w25",
        ["w22", "w23", "w24", "w25"],
        scores=MID,
    )
    with db.write_session() as session:
        run = add_run(session, workspace)
        candidates = [main, before, after]
        add_window(
            session,
            run.id,
            window_json(candidates, units=FINE_UNITS, core=FINE_CORE),
            core=FINE_CORE,
        )
        run_id = run.id
    with db.write_session() as session:
        clip_ids = aggregate_run(session, run_id)

    assert len(clip_ids) == 1
    clip = clip_by_id(db, clip_ids[0])
    # Own evidence [20 s, 25 s) plus at most 3 s before and 4 s after.
    assert (clip.start_ms, clip.end_ms) == (17_000, 29_000)
    assert sorted(clip.evidence_ids) == ["w19", "w20", "w21", "w22", "w23", "w24", "w25"]
    assert clip.evidence_start_ms is not None
    assert clip.evidence_end_ms is not None
    assert (clip.evidence_start_ms, clip.evidence_end_ms) == (19_000, 26_000)
    for unit_id in clip.evidence_ids:
        unit = FINE_UNITS_BY_ID[unit_id]
        assert clip.evidence_start_ms <= unit.start_ms
        assert unit.end_ms <= clip.evidence_end_ms
    assert [
        (entry["reason"], entry["duplicate_of"])
        for entry in diagnostics_of(db, run_id)["suppressed"]
    ] == [
        ("duplicate_in_run", {"kind": "candidate", "window_idx": 0, "index": 0}),
        ("duplicate_in_run", {"kind": "candidate", "window_idx": 0, "index": 0}),
    ]


def test_evidence_interval_without_merge_is_the_candidates_own(
    db: Database, workspace: Workspace
) -> None:
    """ADR 041 §17: without an in-run merge the evidence interval is the candidate's own."""
    candidate = raw("Без злиття", ("funny",), "s2", "s4", ["s2", "s3", "s4"])
    with db.write_session() as session:
        run = add_run(session, workspace)
        add_window(session, run.id, window_json([candidate]))
        run_id = run.id
    with db.write_session() as session:
        clip_ids = aggregate_run(session, run_id)

    assert len(clip_ids) == 1
    clip = clip_by_id(db, clip_ids[0])
    assert (clip.evidence_start_ms, clip.evidence_end_ms) == (10_000, 25_000)
    assert diagnostics_of(db, run_id)["suppressed"] == []


def test_same_words_at_different_times_are_two_clips(db: Database, workspace: Workspace) -> None:
    """T-037: the same words said at two different times are two clips, not one."""
    early = raw("Перша згадка", ("funny",), "s1", "s1", ["s1"])
    late = raw("Пізніша згадка", ("funny",), "s7", "s7", ["s7"])
    with db.write_session() as session:
        run = add_run(session, workspace)
        add_window(session, run.id, window_json([early, late]))
        run_id = run.id
    with db.write_session() as session:
        clip_ids = aggregate_run(session, run_id)

    assert len(clip_ids) == 2
    clips = [clip_by_id(db, clip_id) for clip_id in clip_ids]
    assert [clip.evidence_ids for clip in clips] == [["s1"], ["s7"]]
    assert [(clip.start_ms, clip.end_ms) for clip in clips] == [
        (2_000, 14_000),
        (32_000, 44_000),
    ]
    assert diagnostics_of(db, run_id)["suppressed"] == []


def test_reviewed_clips_and_their_duplicates_are_suppressed(
    db: Database, workspace: Workspace
) -> None:
    """T-038: approved, rejected and manual clips stay as they are; duplicates of them are
    suppressed as ``already_reviewed``."""
    with db.write_session() as session:
        old = add_run(session, workspace, status="completed")
        approved = add_clip(
            session,
            workspace,
            title="Схвалено",
            start_ms=10_000,
            end_ms=30_000,
            review_status="approved",
            run_id=old.id,
            evidence=["s2", "s3", "s4", "s5"],
        )
        rejected = add_clip(
            session,
            workspace,
            title="Відхилено",
            start_ms=40_000,
            end_ms=50_000,
            review_status="rejected",
            run_id=old.id,
            evidence=["s8", "s9"],
        )
        manual = add_clip(
            session, workspace, title="Вручну", start_ms=0, end_ms=6_000, categories=()
        )
        reviewed_ids = [approved.id, rejected.id, manual.id]
        before = {clip_id: clip_state(clip_by_id_in(session, clip_id)) for clip_id in reviewed_ids}
        new = add_run(session, workspace)
        add_window(
            session,
            new.id,
            window_json(
                [
                    raw("Схвалений епізод", ("funny",), "s2", "s4", ["s2", "s3", "s4"]),
                    raw("Відхилений епізод", ("funny",), "s8", "s9", ["s8", "s9"]),
                    raw("Ручний момент", ("funny",), "s0", "s0", ["s0"]),
                    raw("Новий епізод", ("funny",), "s6", "s6", ["s6"]),
                ]
            ),
        )
        run_id = new.id
    with db.write_session() as session:
        clip_ids = aggregate_run(session, run_id)

    assert len(clip_ids) == 1
    assert clip_by_id(db, clip_ids[0]).evidence_ids == ["s6"]
    for clip_id in reviewed_ids:
        assert clip_state(clip_by_id(db, clip_id)) == before[clip_id]
    assert len(clips_of(db, workspace.source_id)) == 4

    suppressed = {
        entry["candidate"]["title"]: (entry["reason"], entry["duplicate_of"])
        for entry in diagnostics_of(db, run_id)["suppressed"]
    }
    assert suppressed == {
        "Схвалений епізод": ("already_reviewed", {"kind": "clip", "id": approved.id}),
        "Відхилений епізод": ("already_reviewed", {"kind": "clip", "id": rejected.id}),
        "Ручний момент": ("already_reviewed", {"kind": "clip", "id": manual.id}),
    }


def test_duplicate_of_older_suggested_clip_is_suppressed_and_unchanged(
    db: Database, workspace: Workspace
) -> None:
    with db.write_session() as session:
        old = add_run(session, workspace, status="completed")
        older = add_clip(
            session,
            workspace,
            title="Старий",
            start_ms=10_000,
            end_ms=30_000,
            run_id=old.id,
            evidence=["s2", "s3", "s4", "s5"],
        )
        older_id = older.id
        before = clip_state(older)
        new = add_run(session, workspace)
        add_window(
            session, new.id, window_json([raw("Повтор", ("funny",), "s3", "s4", ["s3", "s4"])])
        )
        run_id = new.id
    with db.write_session() as session:
        clip_ids = aggregate_run(session, run_id)

    assert clip_ids == ()
    assert clip_state(clip_by_id(db, older_id)) == before
    assert [
        (entry["reason"], entry["duplicate_of"])
        for entry in diagnostics_of(db, run_id)["suppressed"]
    ] == [("duplicate_of_suggested", {"kind": "clip", "id": older_id})]


def test_same_transcript_disjoint_evidence_is_not_a_duplicate(
    db: Database, workspace: Workspace
) -> None:
    """Time overlap alone is not enough when both clips cite evidence of the same transcript."""
    with db.write_session() as session:
        old = add_run(session, workspace, status="completed")
        add_clip(
            session,
            workspace,
            title="Старий",
            start_ms=10_000,
            end_ms=30_000,
            run_id=old.id,
            evidence=["s2"],
        )
        new = add_run(session, workspace)
        add_window(
            session, new.id, window_json([raw("Інше слово", ("funny",), "s4", "s4", ["s4"])])
        )
        run_id = new.id
    with db.write_session() as session:
        clip_ids = aggregate_run(session, run_id)

    assert len(clip_ids) == 1
    assert diagnostics_of(db, run_id)["suppressed"] == []


def test_other_transcript_evidence_is_not_compared(db: Database, workspace: Workspace) -> None:
    """ADR 041 §15: ids of another transcript revision are not comparable, so time decides."""
    with db.write_session() as session:
        other_id, other_revision = add_transcript(session, workspace.source_id)
        old = add_run(
            session,
            workspace,
            status="completed",
            transcript_id=other_id,
            revision=other_revision,
        )
        add_clip(
            session,
            workspace,
            title="Старий з іншого тексту",
            start_ms=10_000,
            end_ms=30_000,
            run_id=old.id,
            evidence=["s2"],
        )
        new = add_run(session, workspace)
        add_window(
            session, new.id, window_json([raw("Інше слово", ("funny",), "s4", "s4", ["s4"])])
        )
        run_id = new.id
    with db.write_session() as session:
        clip_ids = aggregate_run(session, run_id)

    assert clip_ids == ()
    assert [entry["reason"] for entry in diagnostics_of(db, run_id)["suppressed"]] == [
        "duplicate_of_suggested"
    ]


def test_no_qualifying_candidate_completes_run_without_clips(
    db: Database, workspace: Workspace
) -> None:
    """T-035: nothing passes the threshold, so the run completes with no clips."""
    weak = raw("Слабкий", ("funny",), "s2", "s3", ["s2", "s3"], scores=LOW)
    # Rank 81 passes the rank threshold, but completeness 1 is below 2.
    incomplete = raw(
        "Без завершеності", ("funny",), "s6", "s7", ["s6", "s7"], scores=Scores(4, 4, 1, 4, 4, 4)
    )
    with db.write_session() as session:
        run = add_run(session, workspace)
        add_window(session, run.id, window_json([weak, incomplete]))
        run_id = run.id
    with db.write_session() as session:
        clip_ids = aggregate_run(session, run_id)

    assert clip_ids == ()
    assert clips_of(db, workspace.source_id) == []
    stored = run_of(db, run_id)
    assert stored.status == "completed"
    assert stored.error_code is None
    diagnostics = diagnostics_of(db, run_id)
    assert [(entry["index"], entry["reason"]) for entry in diagnostics["rejected"]] == [
        (0, "LOW_QUALITY"),
        (1, "LOW_QUALITY"),
    ]
    assert diagnostics["dropped"] == []
    assert diagnostics["suppressed"] == []


def test_run_without_windows_completes_with_no_clips(db: Database, workspace: Workspace) -> None:
    """An empty transcript has no windows: no requests were made and nothing is suggested."""
    with db.write_session() as session:
        run_id = add_run(session, workspace).id
    with db.write_session() as session:
        clip_ids = aggregate_run(session, run_id)

    assert clip_ids == ()
    assert run_of(db, run_id).status == "completed"
    assert diagnostics_of(db, run_id) == {"dropped": [], "rejected": [], "suppressed": []}


def test_rejections_and_cap_are_recorded_in_diagnostics(db: Database, workspace: Workspace) -> None:
    """LOW_QUALITY, SHORTER_THAN_MIN, LONGER_THAN_MAX and over_cap are written in full."""
    run_profile = profile(
        categories=("funny", "fails"), min_ms=12_000, max_ms=20_000, max_candidates=1
    )
    candidates = [
        raw("Слабкий", ("funny",), "s2", "s3", ["s2", "s3"], scores=LOW),
        raw("Короткий", ("funny",), "s0", "s0", ["s0"]),
        raw("Довгий", ("funny",), "s0", "s5", ["s0", "s1", "s2", "s3", "s4", "s5"]),
        raw("Перший", ("funny",), "s6", "s7", ["s6", "s7"]),
        raw("Другий", ("fails",), "s2", "s3", ["s2", "s3"]),
    ]
    with db.write_session() as session:
        run = add_run(session, workspace, run_profile=run_profile)
        add_window(session, run.id, window_json(candidates))
        run_id = run.id
    with db.write_session() as session:
        clip_ids = aggregate_run(session, run_id)

    assert [clip_by_id(db, clip_id).title for clip_id in clip_ids] == ["Другий"]
    diagnostics = diagnostics_of(db, run_id)
    assert [(entry["index"], entry["reason"]) for entry in diagnostics["rejected"]] == [
        (0, "LOW_QUALITY"),
        (1, "SHORTER_THAN_MIN"),
        (2, "LONGER_THAN_MAX"),
    ]
    assert diagnostics["rejected"][0]["candidate"]["scores"] == {
        "clarity": 1,
        "hook": 1,
        "completeness": 1,
        "emotion": 1,
        "novelty": 1,
        "relevance": 1,
    }
    assert diagnostics["rejected"][1]["candidate"]["title"] == "Короткий"
    assert diagnostics["rejected"][1]["candidate"]["evidence"] == [["s0", 0, 5_000]]
    [over] = diagnostics["suppressed"]
    assert over["reason"] == "over_cap"
    assert over["duplicate_of"] is None
    assert over["candidate"] == {
        "title": "Перший",
        "reason": "Причина: Перший",
        "categories": ["funny"],
        "start_id": "s6",
        "end_id": "s7",
        "evidence_start_ms": 30_000,
        "evidence_end_ms": 40_000,
        "evidence": [["s6", 30_000, 35_000], ["s7", 35_000, 40_000]],
        "scores": {
            "clarity": 4,
            "hook": 4,
            "completeness": 4,
            "emotion": 4,
            "novelty": 4,
            "relevance": 4,
        },
        "visual_review_required": False,
    }
    assert (over["recommended_start_ms"], over["recommended_end_ms"]) == (27_000, 44_000)


def test_window_result_round_trips_through_json_and_storage(
    db: Database, workspace: Workspace
) -> None:
    candidates = [
        raw("Прийнято", ("funny", "action"), "s1", "s2", ["s1", "s2"]),
        raw("Інша категорія", ("unexpected",), "s1", "s1", ["s1"]),
        raw("Поза ядром", ("funny",), "s6", "s7", ["s6", "s7"]),
        raw("Поза ядром і категорія", ("fails",), "s8", "s8", ["s8"]),
    ]
    result = validate_candidates(candidates, UNITS, (0, 4), ("funny", "action"))
    entries = window_result_json(result, candidates, UNITS_BY_ID)

    loaded = json.loads(json.dumps(entries, ensure_ascii=False))
    assert loaded == entries
    parsed = read_window_result(loaded)
    assert parsed == read_window_result(entries)

    [accepted] = parsed.accepted
    assert accepted.index == 0
    assert accepted.categories == ("funny", "action")
    assert accepted.visual_review_required is True
    assert accepted.evidence == (("s1", 5_000, 10_000), ("s2", 10_000, 15_000))
    assert (accepted.evidence_start_ms, accepted.evidence_end_ms) == (5_000, 15_000)
    assert accepted.scores == HIGH
    # ADR 041 §22 (4): one dropped entry per candidate; a candidate outside the core and outside
    # the categories is recorded once, as NOT_OWNED.
    assert [(entry.index, entry.reason) for entry in parsed.dropped] == [
        (1, "CATEGORY_NOT_SELECTED"),
        (2, "NOT_OWNED"),
        (3, "NOT_OWNED"),
    ]
    assert parsed.dropped[0].candidate == candidates[1]
    assert parsed.dropped[2].candidate == candidates[3]

    with db.write_session() as session:
        run_id = add_run(session, workspace).id
        add_window(session, run_id, entries)
    with db.session() as session:
        [window] = AnalysisRunRepository(session).list_windows(run_id)
        stored = window.result_json
    assert stored is not None
    assert read_window_result(stored) == parsed


def test_window_with_hard_errors_is_not_stored() -> None:
    candidates = [raw("Чужий кінець", ("funny",), "s1", "s404", ["s1"])]
    result = validate_candidates(candidates, UNITS, CORE, SELECTED)
    assert result.errors
    with pytest.raises(ValueError, match="помилками"):
        window_result_json(result, candidates, UNITS_BY_ID)


def test_unfinished_window_or_completed_run_is_refused(db: Database, workspace: Workspace) -> None:
    with db.write_session() as session:
        run_id = add_run(session, workspace).id
        add_window(session, run_id, None, state="failed")
    with db.write_session() as session, pytest.raises(ValueError, match="успішно"):
        aggregate_run(session, run_id)
    assert clips_of(db, workspace.source_id) == []

    with db.write_session() as session:
        done_id = add_run(session, workspace, status="completed").id
    with db.write_session() as session, pytest.raises(ValueError, match="вже завершено"):
        aggregate_run(session, done_id)


def clip_by_id_in(session: Session, clip_id: str) -> Clip:
    clip = ClipRepository(session).get(clip_id)
    assert clip is not None
    return clip
