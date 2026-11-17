"""Planning of an AI selection run (ADR 041 §3, §4, §8, §15).

``plan_run`` reads one transcript revision and computes what the run will send: the units of the
analysis level, the windows of ``domain.analysis_windows``, the request payload of every window and
the reservation of its first request. ``add_run`` stores the run as ``queued`` with its ``pending``
windows. Neither sends a request, writes a job or spends anything; the caller owns the transaction.

Windows are planned over positions in the unit sequence. The stored windows hold the ordinals of
those units instead, because ordinals may have gaps (ADR 041 §15).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from json.encoder import encode_basestring
from typing import Final

from sqlalchemy.orm import Session

from clipforge.adapters.openrouter_chat import (
    PROMPT_VERSION,
    REPAIR_SYSTEM_PROMPT,
    SCHEMA_VERSION,
    SELECT_SYSTEM_PROMPT,
    AnalysisProfile,
    build_chat_body,
    request_bytes,
    reservation_micro_usd,
)
from clipforge.domain.analysis_windows import MIN_MAX_WINDOW_BYTES, Unit, Window, plan_windows
from clipforge.domain.search_profile import SearchProfile, profile_to_json
from clipforge.services.errors import invalid, not_found
from clipforge.storage import (
    AnalysisRunRepository,
    TranscriptRepository,
    TranscriptRevision,
    TranscriptSegment,
    TranscriptWord,
)
from clipforge.storage.repositories import MAX_PAGE_SIZE

PROVIDER: Final = "openrouter"
UNIT_LEVEL_SEGMENT: Final = "segment"
UNIT_LEVEL_WORD: Final = "word"
RUN_QUEUED: Final = "queued"
MODEL_NOT_SUPPORTED: Final = "ANALYSIS_MODEL_NOT_SUPPORTED"
# ADR 041 §16: a window is at most 96 KiB and fits the context beside the repair request.
WINDOW_BYTES_CAP: Final = 98_304
MAX_VALIDATION_ERRORS: Final = 50
# Bytes of the JSON string form of a line, without quotes (ADR 041 §18).
MAX_VALIDATION_ERROR_BYTES: Final = 256
# Bytes of the JSON string form of the previous answer, without quotes (ADR 041 §4, §18).
MAX_PREVIOUS_OUTPUT_BYTES: Final = 65_536
# The envelope gives each of the 50 error slots a longest line (ADR 041 §16, §18).
_ENVELOPE_ERROR: Final = "x" * MAX_VALIDATION_ERROR_BYTES
# A lone surrogate (a JSON escape in the model answer or an id) has no UTF-8 form.
_LONE_SURROGATE: Final = re.compile("[\ud800-\udfff]")


@dataclass(frozen=True, slots=True)
class AnalysisPlan:
    """What a run of one transcript revision will send.

    ``windows`` are positions in ``units``. ``first_reservations[i]`` is the reservation of the
    first request of ``windows[i]`` in micro-USD; ``estimate_micro_usd`` is their sum.
    """

    unit_level: str
    units: tuple[Unit, ...]
    windows: tuple[Window, ...]
    first_reservations: tuple[int, ...]
    estimate_micro_usd: int


def window_bytes_limit(
    revision: TranscriptRevision,
    search_profile: SearchProfile,
    analysis_profile: AnalysisProfile,
) -> int:
    """The largest window in bytes the model takes (ADR 041 §16).

    min(98304, context_tokens − 2 × max_tokens − envelope_bytes). The first ``max_tokens`` is
    the answer, the second is the previous answer a repair request carries. The envelope is the
    larger request with no units (see ``_envelope_bytes``), so the instruction counts here.

    Raises ``ANALYSIS_MODEL_NOT_SUPPORTED`` (422) when that is below 32768 bytes.
    """
    limit = min(
        WINDOW_BYTES_CAP,
        analysis_profile.context_tokens
        - 2 * analysis_profile.max_tokens
        - _envelope_bytes(revision, search_profile, analysis_profile),
    )
    if limit < MIN_MAX_WINDOW_BYTES:
        raise invalid(
            MODEL_NOT_SUPPORTED,
            "Модель аналізу не вміщує запит відбору моментів. Оберіть іншу модель або коротшу "
            "інструкцію.",
            field="model",
        )
    return limit


def load_units(session: Session, transcript_id: str) -> tuple[str, tuple[Unit, ...]]:
    """The analysis units of a revision: its segments by ordinal when it has any, else its words.

    Raises ``TRANSCRIPT_NOT_FOUND`` (404) for an unknown id.
    """
    transcripts = TranscriptRepository(session)
    _require_revision(transcripts, transcript_id)
    segments = _read_all(
        lambda offset, limit: transcripts.list_segments(transcript_id, offset=offset, limit=limit)
    )
    if segments:
        return UNIT_LEVEL_SEGMENT, tuple(_unit(segment) for segment in segments)
    words = _read_all(
        lambda offset, limit: transcripts.list_words(transcript_id, offset=offset, limit=limit)
    )
    return UNIT_LEVEL_WORD, tuple(_unit(word) for word in words)


def user_payload(
    revision: TranscriptRevision,
    search_profile: SearchProfile,
    unit_level: str,
    units: Sequence[Unit],
    window: Window,
) -> dict[str, object]:
    """The user message of the first request of ``window`` as an object (ADR 041 §4).

    It holds the language and duration of the source, the profile (without ``allow_partial``), the
    unit level and the units ``window.first..window.last``. No file name or path. The custom
    instruction is a plain string value: it is data, never part of the system prompt.
    """
    return _request_object(
        revision,
        search_profile,
        unit_level,
        [_unit_payload(unit) for unit in units[window.first : window.last + 1]],
    )


def repair_payload(
    base: Mapping[str, object],
    errors: Sequence[str],
    previous_output: str,
) -> dict[str, object]:
    """The repair user object: ``base`` plus the first 50 validation errors and the previous answer.

    Each error line is cut so that its JSON string form, without quotes, has at most 256 UTF-8
    bytes, on a character boundary (ADR 041 §18). The answer is cut to at most 65536 bytes of its
    JSON form the same way (ADR 041 §4). A lone surrogate in either is replaced by U+FFFD first.
    """
    return {
        **base,
        "validation_errors": [
            _json_cut(error, MAX_VALIDATION_ERROR_BYTES) for error in errors[:MAX_VALIDATION_ERRORS]
        ],
        "previous_output": _json_cut(previous_output, MAX_PREVIOUS_OUTPUT_BYTES),
    }


def repair_body(
    analysis_profile: AnalysisProfile,
    base: Mapping[str, object],
    errors: Sequence[str],
    previous_output: str,
) -> dict[str, object]:
    """The REPAIR chat body of a window: ``base`` with the errors and the previous answer.

    ``base`` is the user object of the window (``user_payload``). The answer is cut, on a character
    boundary, so that its JSON form, without quotes, has at most ``min(65536, context_tokens −
    max_tokens − request_bytes(the same body with an empty answer))`` bytes (ADR 041 §18). Then
    ``request_bytes(body) + max_tokens <= context_tokens``. A short answer is kept unchanged.

    Raises ``ValueError`` when the body does not fit the context even without the answer.
    """
    without_answer = _repair_request(analysis_profile, base, errors, "")
    room = min(
        MAX_PREVIOUS_OUTPUT_BYTES,
        analysis_profile.context_tokens
        - analysis_profile.max_tokens
        - request_bytes(without_answer),
    )
    if room < 0:
        raise ValueError("Repair-запит без попередньої відповіді не вміщується в контекст моделі")
    return _repair_request(analysis_profile, base, errors, _json_cut(previous_output, room))


def _repair_request(
    analysis_profile: AnalysisProfile,
    base: Mapping[str, object],
    errors: Sequence[str],
    previous_output: str,
) -> dict[str, object]:
    return build_chat_body(
        analysis_profile,
        system_prompt=REPAIR_SYSTEM_PROMPT,
        user_payload=repair_payload(base, errors, previous_output),
    )


def plan_run(
    session: Session,
    transcript_id: str,
    search_profile: SearchProfile,
    analysis_profile: AnalysisProfile,
) -> AnalysisPlan:
    """Plan the windows of ``transcript_id`` for ``analysis_profile`` and price them.

    An empty transcript has no windows and costs nothing. Raises ``ANALYSIS_MODEL_NOT_SUPPORTED``
    for a model that cannot take the smallest window, and ``TRANSCRIPT_NOT_FOUND`` for an unknown
    transcript.
    """
    revision = _require_revision(TranscriptRepository(session), transcript_id)
    max_window_bytes = window_bytes_limit(revision, search_profile, analysis_profile)
    unit_level, units = load_units(session, transcript_id)
    windows = plan_windows(units, max_window_bytes)
    first_reservations = tuple(
        reservation_micro_usd(
            build_chat_body(
                analysis_profile,
                system_prompt=SELECT_SYSTEM_PROMPT,
                user_payload=user_payload(revision, search_profile, unit_level, units, window),
            ),
            analysis_profile,
        )
        for window in windows
    )
    return AnalysisPlan(
        unit_level=unit_level,
        units=units,
        windows=windows,
        first_reservations=first_reservations,
        estimate_micro_usd=sum(first_reservations),
    )


def add_run(
    session: Session,
    plan: AnalysisPlan,
    *,
    source_id: str,
    revision: TranscriptRevision,
    search_profile: SearchProfile,
    analysis_profile: AnalysisProfile,
) -> str:
    """Store the run as ``queued`` with its ``pending`` windows and return the run id.

    The windows get the ordinals of their first, core and last units, not their positions.
    """
    runs = AnalysisRunRepository(session)
    run = runs.add_run(
        source_id=source_id,
        transcript_id=revision.id,
        transcript_revision=revision.revision,
        profile_json=profile_to_json(search_profile),
        model=analysis_profile.model,
        provider=PROVIDER,
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        unit_level=plan.unit_level,
        price_snapshot_json=price_snapshot(analysis_profile),
        status=RUN_QUEUED,
    )
    for window in plan.windows:
        runs.add_window(
            run_id=run.id,
            idx=window.index,
            first=plan.units[window.first].ordinal,
            core_first=plan.units[window.core_first].ordinal,
            core_last=plan.units[window.core_last].ordinal,
            last=plan.units[window.last].ordinal,
        )
    return run.id


def _require_revision(transcripts: TranscriptRepository, transcript_id: str) -> TranscriptRevision:
    revision = transcripts.get(transcript_id)
    if revision is None:
        raise not_found("TRANSCRIPT_NOT_FOUND", "Транскрипт не знайдено")
    return revision


def _read_all[T](read: Callable[[int, int], list[T]]) -> list[T]:
    """Every item of a windowed repository read ``read(offset, limit)``."""
    items: list[T] = []
    while True:
        batch = read(len(items), MAX_PAGE_SIZE)
        items += batch
        if len(batch) < MAX_PAGE_SIZE:
            return items


def _unit(item: TranscriptSegment | TranscriptWord) -> Unit:
    return Unit(
        id=item.id,
        ordinal=item.ordinal,
        start_ms=item.start_ms,
        end_ms=item.end_ms,
        text=item.text,
    )


def _unit_payload(unit: Unit) -> dict[str, object]:
    """The same keys as ``domain.analysis_windows.unit_json_bytes`` counts."""
    return {
        "id": unit.id,
        "ordinal": unit.ordinal,
        "start_ms": unit.start_ms,
        "end_ms": unit.end_ms,
        "text": unit.text,
    }


def _request_object(
    revision: TranscriptRevision,
    search_profile: SearchProfile,
    unit_level: str,
    units: list[dict[str, object]],
) -> dict[str, object]:
    """The user message as an object, holding the given unit objects (ADR 041 §4)."""
    return {
        "source_context": {"language": revision.language, "duration_ms": revision.duration_ms},
        "profile": {
            "categories": list(search_profile.categories),
            "custom_instruction": search_profile.custom_instruction,
            "min_duration_ms": search_profile.min_duration_ms,
            "max_duration_ms": search_profile.max_duration_ms,
            "max_candidates": search_profile.max_candidates,
        },
        "timing_level": unit_level,
        "units": units,
    }


def _envelope_bytes(
    revision: TranscriptRevision,
    search_profile: SearchProfile,
    analysis_profile: AnalysisProfile,
) -> int:
    """Request bytes without units: the larger of SELECT and REPAIR (ADR 041 §16).

    REPAIR carries 50 error lines of the maximum length and an empty previous answer. The level is
    ``segment``, the longer of the two names, so the envelope holds for either level.
    """
    base = _request_object(revision, search_profile, UNIT_LEVEL_SEGMENT, [])
    select = build_chat_body(
        analysis_profile, system_prompt=SELECT_SYSTEM_PROMPT, user_payload=base
    )
    repair = build_chat_body(
        analysis_profile,
        system_prompt=REPAIR_SYSTEM_PROMPT,
        user_payload=repair_payload(base, [_ENVELOPE_ERROR] * MAX_VALIDATION_ERRORS, ""),
    )
    return max(request_bytes(select), request_bytes(repair))


def storable_text(text: str) -> str:
    """``text`` with each lone surrogate replaced by U+FFFD, so that it has a UTF-8 form."""
    return _LONE_SURROGATE.sub("�", text)


def price_snapshot(profile: AnalysisProfile) -> dict[str, object]:
    """The caps as the strings sent to the provider, so the reservation can be recomputed."""
    return {
        "prompt_usd_per_mtok": str(profile.prompt_usd_per_mtok),
        "completion_usd_per_mtok": str(profile.completion_usd_per_mtok),
        "max_tokens": profile.max_tokens,
        "price_basis": profile.price_basis,
    }


def _json_cut(text: str, limit: int) -> str:
    """The longest prefix of ``text`` whose JSON string form, without quotes, has at most ``limit``
    UTF-8 bytes (ADR 041 §18). A lone surrogate is replaced by U+FFFD first.

    Every character takes at least one byte of its JSON form, so at most ``limit`` leading
    characters can fit. The cut falls between characters: never inside one, never inside an escape.
    """
    text = storable_text(text)
    head = text[: max(limit, 0)]
    used = 0
    for index, char in enumerate(head):
        used += _json_bytes(char)
        if used > limit:
            return head[:index]
    return head


def _json_bytes(char: str) -> int:
    """UTF-8 bytes of one character inside a JSON string, as ``json`` escapes it (ensure_ascii off).

    ``encode_basestring`` returns the escaped string with its two quotes, which are one byte each.
    """
    return len(encode_basestring(char).encode("utf-8")) - 2
