"""Aggregation of a finished analysis run into clips (ADR 041 §6, §7, §9, §15).

``aggregate_run`` runs in the caller's transaction once every window of the run has succeeded.
For each validated candidate it computes the rank, applies the quality threshold and the
recommended interval (§6), drops duplicates against the clips of the source and against the run
itself, orders the rest for diversity and applies the cap (§7). The kept candidates become
``suggested`` clips, every rejected and suppressed candidate goes to the run's diagnostics in full,
and the run becomes ``completed``. Existing clips are only read: no clip is changed here.

A window's result is a list of entries (``window_result_json``). An accepted entry carries its
evidence as ``[id, start_ms, end_ms]`` triples, so the dedupe can compare candidates without the
transcript. A dropped entry keeps the model's candidate as it was written, with the drop reason.
``read_window_result`` reads that list back.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.domain.search_profile import parse_search_profile
from clipforge.domain.selection_dedupe import (
    EvidenceEntry,
    Existing,
    Item,
    Suppressed,
    dedupe_and_rank,
)
from clipforge.domain.selection_output import (
    CATEGORY_NOT_SELECTED,
    NOT_OWNED,
    DropReason,
    RawCandidate,
    Scores,
    UnitRef,
    ValidationResult,
)
from clipforge.domain.selection_scoring import (
    Recommended,
    Rejected,
    compute_rank,
    passes_threshold,
    recommend_interval,
)
from clipforge.storage import (
    AnalysisRun,
    AnalysisRunRepository,
    Clip,
    ClipRepository,
    RecordNotFound,
    Source,
)

LOW_QUALITY: Final = "LOW_QUALITY"
RejectReason = Literal["LOW_QUALITY", "OUT_OF_SOURCE", "LONGER_THAN_MAX", "SHORTER_THAN_MIN"]


@dataclass(frozen=True, slots=True)
class AcceptedEntry:
    """An accepted candidate of a window: the fields of ``GroundedCandidate`` as stored."""

    index: int
    title: str
    reason: str
    categories: tuple[str, ...]
    start_id: str
    end_id: str
    evidence_start_ms: int
    evidence_end_ms: int
    evidence: tuple[EvidenceEntry, ...]
    scores: Scores
    visual_review_required: bool


@dataclass(frozen=True, slots=True)
class DroppedEntry:
    """A candidate the domain dropped softly, with the model's data exactly as it was written."""

    index: int
    reason: DropReason
    candidate: RawCandidate


@dataclass(frozen=True, slots=True)
class WindowResult:
    accepted: tuple[AcceptedEntry, ...]
    dropped: tuple[DroppedEntry, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    """An accepted candidate with a recommended interval, ready for dedupe."""

    window_idx: int
    accepted: AcceptedEntry
    rank: int
    recommended: Recommended

    @property
    def key(self) -> str:
        return f"w{self.window_idx}c{self.accepted.index}"


def window_result_json(
    result: ValidationResult,
    raw: Sequence[RawCandidate],
    units_by_id: Mapping[str, UnitRef],
) -> list[dict[str, Any]]:
    """The JSON value of one window's validated candidates, for ``analysis_windows.result_json``.

    ``raw`` is the list that was validated, in the same order, and ``units_by_id`` holds the units
    of that request. A result with hard errors is refused: such a window is repaired or failed,
    and nothing of it is kept. Accepted entries come first, then dropped ones, each in input order.
    """
    if result.errors:
        raise ValueError("Вікно з помилками перевірки не зберігається")
    if any(not 0 <= index < len(raw) for index, _ in result.dropped):
        raise ValueError("Відкинутий кандидат має індекс поза списком")
    dropped_indexes = {index for index, _ in result.dropped}
    accepted_indexes = [index for index in range(len(raw)) if index not in dropped_indexes]
    if len(accepted_indexes) != len(result.accepted):
        raise ValueError("Список кандидатів не відповідає результату перевірки")
    entries: list[dict[str, Any]] = []
    for index, grounded in zip(accepted_indexes, result.accepted, strict=True):
        accepted = AcceptedEntry(
            index=index,
            title=grounded.title,
            reason=grounded.reason,
            categories=grounded.categories,
            start_id=grounded.start.id,
            end_id=grounded.end.id,
            evidence_start_ms=grounded.start.start_ms,
            evidence_end_ms=grounded.end.end_ms,
            evidence=tuple(
                _evidence_entry(unit_id, units_by_id) for unit_id in grounded.evidence_ids
            ),
            scores=grounded.scores,
            visual_review_required=grounded.visual_review_required,
        )
        entries.append(
            {"status": "accepted", "index": index, "candidate": _candidate_json(accepted)}
        )
    for index, reason in result.dropped:
        entries.append(
            {
                "status": "dropped",
                "index": index,
                "reason": reason,
                "candidate": _raw_json(raw[index]),
            }
        )
    return entries


def read_window_result(value: object) -> WindowResult:
    """Read back the list that ``window_result_json`` wrote, as it is stored in ``result_json``.

    Raises ``ValueError`` when the value does not have the documented shape.
    """
    if not isinstance(value, list):
        raise ValueError("Результат вікна має бути масивом")
    accepted: list[AcceptedEntry] = []
    dropped: list[DroppedEntry] = []
    for position, item in enumerate(value):
        where = f"елемент {position}"
        entry = _as_mapping(item, where, "entry")
        index = _as_int(entry.get("index"), where, "index")
        candidate = _as_mapping(entry.get("candidate"), where, "candidate")
        status = _as_text(entry.get("status"), where, "status")
        if status == "accepted":
            accepted.append(_accepted(index, candidate, where))
        elif status == "dropped":
            reason = _drop_reason(entry.get("reason"), where)
            dropped.append(DroppedEntry(index, reason, _raw(candidate, where)))
        else:
            raise _bad(where, "status")
    return WindowResult(accepted=tuple(accepted), dropped=tuple(dropped))


def aggregate_run(session: Session, run_id: str) -> tuple[str, ...]:
    """Turn the validated candidates of a run into suggested clips and return their ids.

    Call it in the caller's transaction once every window of the run has succeeded (ADR 041 §9).
    Nothing is committed here. A run that is not found, has a window that is not ``succeeded``,
    or is already ``completed`` is refused before anything is written. Otherwise the run becomes
    ``completed`` with its diagnostics, and the clips are returned in the order they were kept.
    """
    runs = AnalysisRunRepository(session)
    run = runs.get_run(run_id)
    if run is None:
        raise RecordNotFound("analysis_run", run_id)
    if run.status == "completed":
        raise ValueError("Запуск аналізу вже завершено: повторна агрегація заборонена")
    source = session.get(Source, run.source_id)
    if source is None:
        raise RecordNotFound("source", run.source_id)
    profile = parse_search_profile(run.profile_json)
    windows = runs.list_windows(run_id)
    if any(window.state != "succeeded" for window in windows):
        raise ValueError("Не всі вікна запуску завершено успішно")

    dropped_out: list[dict[str, Any]] = []
    rejected_out: list[dict[str, Any]] = []
    candidates: list[_Candidate] = []
    for window in windows:
        if window.result_json is None:
            raise ValueError("Успішне вікно не має результату")
        result = read_window_result(window.result_json)
        for dropped in result.dropped:
            dropped_out.append(
                {
                    "window_idx": window.idx,
                    "index": dropped.index,
                    "reason": dropped.reason,
                    "candidate": _raw_json(dropped.candidate),
                }
            )
        for accepted in result.accepted:
            rank = compute_rank(_scores_map(accepted.scores))
            if not passes_threshold(rank, accepted.scores.completeness):
                rejected_out.append(_rejected_json(window.idx, accepted, rank, LOW_QUALITY))
                continue
            placed = recommend_interval(
                accepted.evidence_start_ms,
                accepted.evidence_end_ms,
                duration_ms=source.duration_ms,
                min_ms=profile.min_duration_ms,
                max_ms=profile.max_duration_ms,
            )
            if isinstance(placed, Rejected):
                rejected_out.append(_rejected_json(window.idx, accepted, rank, placed.code))
                continue
            candidates.append(_Candidate(window.idx, accepted, rank, placed))

    by_key = {candidate.key: candidate for candidate in candidates}
    items = [
        Item(
            key=candidate.key,
            start_ms=candidate.recommended.start_ms,
            end_ms=candidate.recommended.end_ms,
            evidence=candidate.accepted.evidence,
            categories=candidate.accepted.categories,
            rank=candidate.rank,
            visual_review_required=candidate.accepted.visual_review_required,
        )
        for candidate in candidates
    ]
    selection = dedupe_and_rank(
        items, _existing_clips(session, run), max_candidates=profile.max_candidates
    )

    clips = ClipRepository(session)
    clip_ids: list[str] = []
    for item in selection.kept:
        candidate = by_key[item.key]
        evidence_start_ms, evidence_end_ms = _evidence_span(candidate.accepted, item.evidence)
        clip = clips.add(
            source_id=run.source_id,
            title=candidate.accepted.title,
            start_ms=item.start_ms,
            end_ms=item.end_ms,
            categories=item.categories,
            evidence_type="transcript",
            evidence_ids=[entry[0] for entry in item.evidence],
            reason=candidate.accepted.reason,
            scores=_scores_map(candidate.accepted.scores),
            rank=item.rank,
            visual_review_required=item.visual_review_required,
            recommended_start_ms=item.start_ms,
            recommended_end_ms=item.end_ms,
            evidence_start_ms=evidence_start_ms,
            evidence_end_ms=evidence_end_ms,
            analysis_run_id=run.id,
            review_status="suggested",
        )
        clip_ids.append(clip.id)

    suppressed_out = [_suppressed_json(entry, by_key) for entry in selection.suppressed]
    runs.update_run(
        run.id,
        status="completed",
        diagnostics_json={
            "dropped": dropped_out,
            "rejected": rejected_out,
            "suppressed": suppressed_out,
        },
    )
    return tuple(clip_ids)


def _existing_clips(session: Session, run: AnalysisRun) -> list[Existing]:
    """The clips of the run's source, as dedupe sees them (ADR 041 §7, §15)."""
    runs = AnalysisRunRepository(session)
    clips = session.scalars(
        select(Clip).where(Clip.source_id == run.source_id).order_by(Clip.id)
    ).all()
    existing: list[Existing] = []
    for clip in clips:
        # A manual clip, and an approved or rejected one, is reviewed; any other is a suggestion.
        reviewed = clip.analysis_run_id is None or clip.review_status != "suggested"
        existing.append(
            Existing(
                clip_id=clip.id,
                start_ms=clip.start_ms,
                end_ms=clip.end_ms,
                evidence_ids=_comparable_evidence(runs, clip, run),
                kind="reviewed" if reviewed else "suggested",
            )
        )
    return existing


def _comparable_evidence(
    runs: AnalysisRunRepository, clip: Clip, run: AnalysisRun
) -> frozenset[str] | None:
    """The evidence ids of a clip when they name the units of this run, else ``None`` (§15).

    Ids are scoped to one transcript revision and one unit level, so ids of another revision or
    of the other level are never compared with this run's ids.
    """
    if clip.analysis_run_id is None:
        return None
    clip_run = runs.get_run(clip.analysis_run_id)
    if (
        clip_run is None
        or clip_run.transcript_id != run.transcript_id
        or clip_run.unit_level != run.unit_level
    ):
        return None
    return frozenset(clip.evidence_ids)


def _evidence_span(accepted: AcceptedEntry, evidence: Sequence[EvidenceEntry]) -> tuple[int, int]:
    """The evidence interval of a kept clip (ADR 041 §17).

    It is the candidate's own interval widened to cover every evidence entry of the kept item,
    including the entries merged from in-run duplicates, so each stored evidence id lies inside it.
    """
    starts = [accepted.evidence_start_ms, *(start for _, start, _ in evidence)]
    ends = [accepted.evidence_end_ms, *(end for _, _, end in evidence)]
    return min(starts), max(ends)


def _evidence_entry(unit_id: str, units_by_id: Mapping[str, UnitRef]) -> EvidenceEntry:
    unit = units_by_id.get(unit_id)
    if unit is None:
        raise ValueError("Доказ кандидата не належить до одиниць запиту")
    return (unit.id, unit.start_ms, unit.end_ms)


def _scores_map(scores: Scores) -> dict[str, int]:
    return {
        "clarity": scores.clarity,
        "hook": scores.hook,
        "completeness": scores.completeness,
        "emotion": scores.emotion,
        "novelty": scores.novelty,
        "relevance": scores.relevance,
    }


def _candidate_json(entry: AcceptedEntry) -> dict[str, Any]:
    return {
        "title": entry.title,
        "reason": entry.reason,
        "categories": list(entry.categories),
        "start_id": entry.start_id,
        "end_id": entry.end_id,
        "evidence_start_ms": entry.evidence_start_ms,
        "evidence_end_ms": entry.evidence_end_ms,
        "evidence": [list(item) for item in entry.evidence],
        "scores": _scores_map(entry.scores),
        "visual_review_required": entry.visual_review_required,
    }


def _raw_json(candidate: RawCandidate) -> dict[str, Any]:
    return {
        "title": candidate.title,
        "reason": candidate.reason,
        "categories": list(candidate.categories),
        "start_id": candidate.start_id,
        "end_id": candidate.end_id,
        "evidence_ids": list(candidate.evidence_ids),
        "scores": _scores_map(candidate.scores),
        "visual_review_required": candidate.visual_review_required,
    }


def _rejected_json(
    window_idx: int, accepted: AcceptedEntry, rank: int, reason: RejectReason
) -> dict[str, Any]:
    return {
        "window_idx": window_idx,
        "index": accepted.index,
        "reason": reason,
        "rank": rank,
        "candidate": _candidate_json(accepted),
    }


def _suppressed_json(entry: Suppressed, by_key: Mapping[str, _Candidate]) -> dict[str, Any]:
    candidate = by_key[entry.key]
    return {
        "window_idx": candidate.window_idx,
        "index": candidate.accepted.index,
        "reason": entry.kind,
        "rank": candidate.rank,
        "recommended_start_ms": candidate.recommended.start_ms,
        "recommended_end_ms": candidate.recommended.end_ms,
        "duplicate_of": _duplicate_of(entry, by_key),
        "candidate": _candidate_json(candidate.accepted),
    }


def _duplicate_of(entry: Suppressed, by_key: Mapping[str, _Candidate]) -> dict[str, Any] | None:
    if entry.duplicate_of is None:
        return None
    if entry.kind == "duplicate_in_run":
        target = by_key[entry.duplicate_of]
        return {
            "kind": "candidate",
            "window_idx": target.window_idx,
            "index": target.accepted.index,
        }
    return {"kind": "clip", "id": entry.duplicate_of}


def _bad(where: str, key: str) -> ValueError:
    return ValueError(f"Некоректне поле {key} ({where}) у результаті вікна")


def _as_int(value: object, where: str, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(where, key)
    return value


def _as_text(value: object, where: str, key: str) -> str:
    if not isinstance(value, str):
        raise _bad(where, key)
    return value


def _as_bool(value: object, where: str, key: str) -> bool:
    if not isinstance(value, bool):
        raise _bad(where, key)
    return value


def _as_list(value: object, where: str, key: str) -> list[object]:
    if not isinstance(value, list):
        raise _bad(where, key)
    return value


def _as_mapping(value: object, where: str, key: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise _bad(where, key)
    return value


def _as_texts(value: object, where: str, key: str) -> tuple[str, ...]:
    return tuple(_as_text(item, where, key) for item in _as_list(value, where, key))


def _as_scores(value: object, where: str) -> Scores:
    scores = _as_mapping(value, where, "scores")
    return Scores(
        clarity=_as_int(scores.get("clarity"), where, "scores"),
        hook=_as_int(scores.get("hook"), where, "scores"),
        completeness=_as_int(scores.get("completeness"), where, "scores"),
        emotion=_as_int(scores.get("emotion"), where, "scores"),
        novelty=_as_int(scores.get("novelty"), where, "scores"),
        relevance=_as_int(scores.get("relevance"), where, "scores"),
    )


def _as_evidence(value: object, where: str) -> tuple[EvidenceEntry, ...]:
    entries: list[EvidenceEntry] = []
    for item in _as_list(value, where, "evidence"):
        triple = _as_list(item, where, "evidence")
        if len(triple) != 3:
            raise _bad(where, "evidence")
        ident, start, end = triple
        entries.append(
            (
                _as_text(ident, where, "evidence"),
                _as_int(start, where, "evidence"),
                _as_int(end, where, "evidence"),
            )
        )
    return tuple(entries)


def _drop_reason(value: object, where: str) -> DropReason:
    if value == CATEGORY_NOT_SELECTED:
        return CATEGORY_NOT_SELECTED
    if value == NOT_OWNED:
        return NOT_OWNED
    raise _bad(where, "reason")


def _raw(candidate: Mapping[str, object], where: str) -> RawCandidate:
    return RawCandidate(
        title=_as_text(candidate.get("title"), where, "title"),
        categories=_as_texts(candidate.get("categories"), where, "categories"),
        start_id=_as_text(candidate.get("start_id"), where, "start_id"),
        end_id=_as_text(candidate.get("end_id"), where, "end_id"),
        evidence_ids=_as_texts(candidate.get("evidence_ids"), where, "evidence_ids"),
        reason=_as_text(candidate.get("reason"), where, "reason"),
        scores=_as_scores(candidate.get("scores"), where),
        visual_review_required=_as_bool(
            candidate.get("visual_review_required"), where, "visual_review_required"
        ),
    )


def _accepted(index: int, candidate: Mapping[str, object], where: str) -> AcceptedEntry:
    return AcceptedEntry(
        index=index,
        title=_as_text(candidate.get("title"), where, "title"),
        reason=_as_text(candidate.get("reason"), where, "reason"),
        categories=_as_texts(candidate.get("categories"), where, "categories"),
        start_id=_as_text(candidate.get("start_id"), where, "start_id"),
        end_id=_as_text(candidate.get("end_id"), where, "end_id"),
        evidence_start_ms=_as_int(candidate.get("evidence_start_ms"), where, "evidence_start_ms"),
        evidence_end_ms=_as_int(candidate.get("evidence_end_ms"), where, "evidence_end_ms"),
        evidence=_as_evidence(candidate.get("evidence"), where),
        scores=_as_scores(candidate.get("scores"), where),
        visual_review_required=_as_bool(
            candidate.get("visual_review_required"), where, "visual_review_required"
        ),
    )
