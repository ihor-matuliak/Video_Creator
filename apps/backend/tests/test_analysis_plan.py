"""Planning of an analysis run (ADR 041 §3, §4, §8, §15): units by level, windows stored as
ordinals, the request payload, the window limit, the estimate and the repair request.

Transcripts are imported straight into a temporary workspace database. Nothing is sent: the
planner only builds bodies and prices their first requests.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from clipforge.adapters.openrouter_chat import (
    PROMPT_VERSION,
    REPAIR_SYSTEM_PROMPT,
    SCHEMA_VERSION,
    SELECT_SYSTEM_PROMPT,
    AnalysisProfile,
    analysis_profiles,
    build_chat_body,
    request_bytes,
    reservation_micro_usd,
)
from clipforge.domain.analysis_windows import Unit
from clipforge.domain.search_profile import SearchProfile, parse_search_profile, profile_to_json
from clipforge.services.analysis_plan import (
    add_run,
    load_units,
    plan_run,
    repair_body,
    repair_payload,
    user_payload,
    window_bytes_limit,
)
from clipforge.services.errors import ErrorKind, ServiceError
from clipforge.storage import (
    AnalysisRunRepository,
    Database,
    NewSegment,
    NewWord,
    TranscriptRepository,
    TranscriptRevision,
    open_database,
)

from .test_storage_support import add_project, add_source

TEST_PROFILE: AnalysisProfile = analysis_profiles("test")["test/selector"]
PROFILE_JSON = {
    "categories": ["funny", "fails"],
    "custom_instruction": "",
    "min_duration_ms": 15_000,
    "max_duration_ms": 90_000,
    "max_candidates": 20,
    "allow_partial": False,
}
PROFILE: SearchProfile = parse_search_profile(PROFILE_JSON)
COVERAGE = [{"start_ms": 0, "end_ms": 2_000_000, "state": "succeeded"}]
METADATA = {"format": "normalized_json", "offset_ms": 0}


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_database(tmp_path / "data")
    try:
        yield database
    finally:
        database.close()


def _source_id(session: Session) -> str:
    return add_source(session, add_project(session).id, duration_ms=2_000_000).id


def _revision(
    session: Session,
    source_id: str,
    *,
    words: Sequence[NewWord] = (),
    segments: Sequence[NewSegment] = (),
    timing_level: str = "segment",
) -> TranscriptRevision:
    return TranscriptRepository(session).add_revision(
        source_id=source_id,
        origin="import",
        timing_level=timing_level,
        language="en",
        duration_ms=2_000_000,
        coverage=COVERAGE,
        metadata=METADATA,
        words=words,
        segments=segments,
    )


def _ten_segments() -> list[NewSegment]:
    """Ordinals 5, 15, ..., 95 (gaps between them), 100 s each: three windows with the test model.

    Cores are [0..3], [4..7] and [8..9] by position; the context adds nothing outside the cores.
    """
    return [
        NewSegment(
            id=f"s{ordinal}",
            ordinal=ordinal,
            start_ms=index * 100_000,
            end_ms=(index + 1) * 100_000,
            text=f"репліка {ordinal}",
        )
        for index, ordinal in enumerate(range(5, 100, 10))
    ]


def _model(context_tokens: int) -> AnalysisProfile:
    return AnalysisProfile(
        model="test/small",
        prompt_usd_per_mtok=Decimal("1"),
        completion_usd_per_mtok=Decimal("2"),
        context_tokens=context_tokens,
        max_tokens=4096,
        price_basis="test",
    )


def _contents(body: dict[str, object]) -> tuple[str, str]:
    """The system and the user message of a request body."""
    messages = body["messages"]
    assert isinstance(messages, list)
    system, user = messages
    assert isinstance(system, dict)
    assert isinstance(user, dict)
    system_text, user_text = system["content"], user["content"]
    assert isinstance(system_text, str)
    assert isinstance(user_text, str)
    return system_text, user_text


# --- units by level ---


def test_segments_are_the_units_when_a_revision_has_any_and_words_otherwise(db: Database) -> None:
    with db.write_session() as session:
        source_id = _source_id(session)
        with_both = _revision(
            session,
            source_id,
            words=[
                NewWord(id="w1", ordinal=0, start_ms=0, end_ms=500, text="раз"),
                NewWord(id="w2", ordinal=1, start_ms=500, end_ms=1_000, text="два"),
            ],
            segments=[
                NewSegment(
                    id="s1",
                    ordinal=0,
                    start_ms=0,
                    end_ms=1_000,
                    text="раз два",
                    word_ids=["w1", "w2"],
                )
            ],
            timing_level="word",
        )
        words_only = _revision(
            session,
            source_id,
            words=[
                NewWord(id="w1", ordinal=0, start_ms=0, end_ms=500, text="раз"),
                NewWord(id="w2", ordinal=1, start_ms=500, end_ms=1_000, text="два"),
            ],
            timing_level="word",
        )
        with_both_id, words_only_id = with_both.id, words_only.id

    with db.session() as session:
        assert load_units(session, with_both_id) == (
            "segment",
            (Unit(id="s1", ordinal=0, start_ms=0, end_ms=1_000, text="раз два"),),
        )
        assert load_units(session, words_only_id) == (
            "word",
            (
                Unit(id="w1", ordinal=0, start_ms=0, end_ms=500, text="раз"),
                Unit(id="w2", ordinal=1, start_ms=500, end_ms=1_000, text="два"),
            ),
        )


def test_an_unknown_transcript_is_not_found(db: Database) -> None:
    with db.session() as session, pytest.raises(ServiceError) as raised:
        load_units(session, "немає-такої-ревізії")
    assert raised.value.code == "TRANSCRIPT_NOT_FOUND"
    assert raised.value.kind is ErrorKind.NOT_FOUND


# --- windows and ordinals ---


def test_windows_are_planned_over_positions_and_stored_as_ordinals(db: Database) -> None:
    with db.write_session() as session:
        source_id = _source_id(session)
        revision = _revision(session, source_id, segments=_ten_segments())
        plan = plan_run(session, revision.id, PROFILE, TEST_PROFILE)
        planned = [(w.index, w.first, w.core_first, w.core_last, w.last) for w in plan.windows]
        run_id = add_run(
            session,
            plan,
            source_id=source_id,
            revision=revision,
            search_profile=PROFILE,
            analysis_profile=TEST_PROFILE,
        )

    # Positions 0..9 of the plan; the stored values are the ordinals 5..95 of those units.
    assert planned == [(0, 0, 0, 3, 3), (1, 4, 4, 7, 7), (2, 8, 8, 9, 9)]
    with db.session() as session:
        windows = AnalysisRunRepository(session).list_windows(run_id)
        stored = [
            (w.idx, w.first, w.core_first, w.core_last, w.last, w.state, w.attempts)
            for w in windows
        ]
    assert stored == [
        (0, 5, 5, 35, 35, "pending", 0),
        (1, 45, 45, 75, 75, "pending", 0),
        (2, 85, 85, 95, 95, "pending", 0),
    ]
    assert all(not w.repair_used for w in windows)


def test_an_empty_transcript_has_no_windows_and_no_estimate(db: Database) -> None:
    with db.write_session() as session:
        source_id = _source_id(session)
        revision = _revision(session, source_id)
        plan = plan_run(session, revision.id, PROFILE, TEST_PROFILE)
        assert plan.units == ()
        assert plan.windows == ()
        assert plan.first_reservations == ()
        assert plan.estimate_micro_usd == 0
        run_id = add_run(
            session,
            plan,
            source_id=source_id,
            revision=revision,
            search_profile=PROFILE,
            analysis_profile=TEST_PROFILE,
        )

    with db.session() as session:
        assert AnalysisRunRepository(session).list_windows(run_id) == []
        run = AnalysisRunRepository(session).get_run(run_id)
        assert run is not None
        assert run.status == "queued"


def test_add_run_stores_a_queued_run_with_its_profile_and_price_snapshot(db: Database) -> None:
    with db.write_session() as session:
        source_id = _source_id(session)
        revision = _revision(
            session,
            source_id,
            segments=[NewSegment(id="s7", ordinal=7, start_ms=0, end_ms=1_000, text="Привіт")],
        )
        plan = plan_run(session, revision.id, PROFILE, TEST_PROFILE)
        run_id = add_run(
            session,
            plan,
            source_id=source_id,
            revision=revision,
            search_profile=PROFILE,
            analysis_profile=TEST_PROFILE,
        )

    with db.session() as session:
        run = AnalysisRunRepository(session).get_run(run_id)
        assert run is not None
        assert run.source_id == source_id
        assert run.transcript_id == revision.id
        assert run.transcript_revision == 1
        assert run.status == "queued"
        assert run.provider == "openrouter"
        assert run.model == "test/selector"
        assert run.prompt_version == PROMPT_VERSION
        assert run.schema_version == SCHEMA_VERSION
        assert run.unit_level == "segment"
        assert run.profile_json == profile_to_json(PROFILE)
        assert run.price_snapshot_json == {
            "prompt_usd_per_mtok": "1",
            "completion_usd_per_mtok": "2",
            "max_tokens": 4096,
            "price_basis": "test",
        }
        windows = AnalysisRunRepository(session).list_windows(run_id)
        assert [(w.idx, w.first, w.core_first, w.core_last, w.last) for w in windows] == [
            (0, 7, 7, 7, 7)
        ]


# --- request payload ---


def test_the_request_has_no_file_name_or_path_and_the_instruction_is_data(db: Database) -> None:
    instruction = 'Шукай жарти, але не згадуй "ім\'я" \\ і {"ignore": true}\nдругий рядок'
    profile = parse_search_profile({**PROFILE_JSON, "custom_instruction": instruction})
    with db.write_session() as session:
        source = add_source(session, add_project(session).id, duration_ms=2_000_000)
        path, display_name = source.path, source.display_name
        revision = _revision(
            session,
            source.id,
            segments=[NewSegment(id="s1", ordinal=0, start_ms=0, end_ms=1_000, text="Тестова")],
        )
        plan = plan_run(session, revision.id, profile, TEST_PROFILE)

    payload = user_payload(revision, profile, plan.unit_level, plan.units, plan.windows[0])
    assert set(payload) == {"source_context", "profile", "timing_level", "units"}
    assert payload["source_context"] == {"language": "en", "duration_ms": 2_000_000}
    assert set(_as_dict(payload["profile"])) == {
        "categories",
        "custom_instruction",
        "min_duration_ms",
        "max_duration_ms",
        "max_candidates",
    }

    body = build_chat_body(TEST_PROFILE, system_prompt=SELECT_SYSTEM_PROMPT, user_payload=payload)
    system_text, user_text = _contents(body)
    assert system_text == SELECT_SYSTEM_PROMPT
    assert instruction not in system_text
    assert json.loads(user_text)["profile"]["custom_instruction"] == instruction
    for leaked in (path, display_name, "запис", "mkv", "Відео", "C:"):
        assert leaked not in system_text
        assert leaked not in user_text


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


# --- estimate ---


def test_the_estimate_is_the_sum_of_the_first_request_reservations(db: Database) -> None:
    with db.write_session() as session:
        revision = _revision(session, _source_id(session), segments=_ten_segments())
        plan = plan_run(session, revision.id, PROFILE, TEST_PROFILE)

    assert len(plan.windows) == 3
    expected: list[int] = []
    for window in plan.windows:
        payload = user_payload(revision, PROFILE, plan.unit_level, plan.units, window)
        body = build_chat_body(
            TEST_PROFILE, system_prompt=SELECT_SYSTEM_PROMPT, user_payload=payload
        )
        expected.append(reservation_micro_usd(body, TEST_PROFILE))
    assert plan.first_reservations == tuple(expected)
    assert plan.estimate_micro_usd == sum(expected)
    assert plan.estimate_micro_usd > 0


# --- window limit (ADR 041 §16) ---

CYRILLIC_INSTRUCTION = "я" * 2_000  # 4 000 UTF-8 bytes, counted as they are
# Without the instruction this context leaves 36 168 bytes, with it 32 168 (below 32 768).
TIGHT_CONTEXT = 60_000


def _revision_of(db: Database) -> TranscriptRevision:
    with db.write_session() as session:
        revision = _revision(session, _source_id(session), segments=_ten_segments())
    return revision


def _with_instruction(text: str) -> SearchProfile:
    return parse_search_profile({**PROFILE_JSON, "custom_instruction": text})


def test_the_window_limit_is_capped_at_98304_bytes(db: Database) -> None:
    revision = _revision_of(db)

    assert window_bytes_limit(revision, PROFILE, TEST_PROFILE) == 98_304
    assert window_bytes_limit(revision, PROFILE, _model(200_000)) == 98_304


def test_the_window_limit_falls_by_the_context_and_by_twice_max_tokens(db: Database) -> None:
    revision = _revision_of(db)
    base = window_bytes_limit(revision, PROFILE, _model(90_000))

    assert window_bytes_limit(revision, PROFILE, _model(91_000)) == base + 1_000
    assert window_bytes_limit(revision, PROFILE, replace(_model(90_000), max_tokens=2_048)) == (
        base + 2 * (4_096 - 2_048)
    )


def test_a_model_whose_limit_is_below_32768_bytes_is_rejected(db: Database) -> None:
    revision = _revision_of(db)
    # The envelope is what the limit lacks below the context: 90 000 − 2 × 4 096 − limit.
    envelope = 90_000 - 2 * 4_096 - window_bytes_limit(revision, PROFILE, _model(90_000))
    smallest_context = 32_768 + 2 * 4_096 + envelope

    assert window_bytes_limit(revision, PROFILE, _model(smallest_context)) == 32_768
    with pytest.raises(ServiceError) as raised:
        window_bytes_limit(revision, PROFILE, _model(smallest_context - 1))
    assert raised.value.code == "ANALYSIS_MODEL_NOT_SUPPORTED"
    assert raised.value.kind is ErrorKind.INVALID
    assert raised.value.details == {"field": "model"}


def test_a_long_cyrillic_instruction_lowers_the_limit_by_its_bytes(db: Database) -> None:
    revision = _revision_of(db)
    model = _model(90_000)
    plain = window_bytes_limit(revision, PROFILE, model)

    assert plain < 98_304  # the cap would hide the instruction
    cyrillic = _with_instruction(CYRILLIC_INSTRUCTION)
    assert plain - window_bytes_limit(revision, cyrillic, model) == 4_000


def test_an_instruction_that_alone_drops_the_limit_below_32768_is_a_model_error(
    db: Database,
) -> None:
    revision = _revision_of(db)
    model = _model(TIGHT_CONTEXT)
    assert window_bytes_limit(revision, PROFILE, model) >= 32_768

    with pytest.raises(ServiceError) as raised:
        window_bytes_limit(revision, _with_instruction(CYRILLIC_INSTRUCTION), model)
    assert raised.value.code == "ANALYSIS_MODEL_NOT_SUPPORTED"
    assert raised.value.kind is ErrorKind.INVALID
    assert raised.value.details == {"field": "model"}


def test_plan_run_refuses_a_model_below_the_window_minimum(db: Database) -> None:
    with db.write_session() as session:
        revision = _revision(session, _source_id(session), segments=_ten_segments())
        with pytest.raises(ServiceError) as raised:
            plan_run(session, revision.id, PROFILE, _model(31_808))
    assert raised.value.code == "ANALYSIS_MODEL_NOT_SUPPORTED"


# --- repair request ---


def test_the_repair_request_keeps_50_errors_and_65536_bytes_of_the_answer() -> None:
    base: dict[str, object] = {"timing_level": "segment", "units": []}
    errors = [f"error {n}" for n in range(60)]

    payload = repair_payload(base, errors, "a" * 70_000)

    assert payload["validation_errors"] == errors[:50]
    assert payload["previous_output"] == "a" * 65_536
    assert payload["timing_level"] == "segment"
    assert payload["units"] == []


def test_the_answer_is_cut_on_a_character_boundary() -> None:
    # 1 + 80 000 bytes. The 32 768th letter would end at byte 65 537, so it is dropped whole.
    cut = repair_payload({}, [], "a" + "я" * 40_000)["previous_output"]
    assert cut == "a" + "я" * 32_767
    assert len(cut.encode("utf-8")) == 65_535

    assert repair_payload({}, [], "коротко")["previous_output"] == "коротко"
    assert repair_payload({}, [], "x\ud800y")["previous_output"] == "x�y"


def test_an_error_line_is_cut_to_256_bytes_on_a_character_boundary() -> None:
    lines = repair_payload(
        {},
        ["я" * 300, "€" * 100, "a" * 255 + "я", "x\ud800y"],
        "",
    )["validation_errors"]

    assert isinstance(lines, list)
    # 300 letters of 2 bytes keep 128 letters; 100 euro signs of 3 bytes keep 85 (255 bytes).
    # The letter that would end at byte 257 is dropped whole, and a lone surrogate becomes U+FFFD.
    assert lines == ["я" * 128, "€" * 85, "a" * 255, "x�y"]
    assert all(len(line.encode("utf-8")) <= 256 for line in lines)


def test_every_repair_request_of_a_planned_window_fits_the_test_context(db: Database) -> None:
    # Five-second units of about 1 KB: the cores fill up by bytes, and the context by its bytes too.
    segments = [
        NewSegment(
            id=f"s{n}",
            ordinal=n,
            start_ms=n * 5_000,
            end_ms=(n + 1) * 5_000,
            text="я" * 500,
        )
        for n in range(150)
    ]
    with db.write_session() as session:
        revision = _revision(session, _source_id(session), segments=segments)
        plan = plan_run(session, revision.id, PROFILE, TEST_PROFILE)
    assert len(plan.windows) > 1
    # 60 errors of 300 letters: repair_payload keeps 50 and cuts each to 256 bytes.
    errors = ["я" * 150] * 60
    fullest = 0

    for window in plan.windows:
        base = user_payload(revision, PROFILE, plan.unit_level, plan.units, window)
        repair = build_chat_body(
            TEST_PROFILE,
            system_prompt=REPAIR_SYSTEM_PROMPT,
            user_payload=repair_payload(base, errors, ""),
        )
        # The previous answer is the second max_tokens of the budget (ADR 041 §16).
        assert request_bytes(repair) + 2 * TEST_PROFILE.max_tokens <= TEST_PROFILE.context_tokens
        fullest = max(fullest, request_bytes(repair))

    # The largest window uses most of the room the limit leaves, so the check is not vacuous.
    assert fullest + 2 * TEST_PROFILE.max_tokens > TEST_PROFILE.context_tokens - 10_000


# --- repair request in JSON form (ADR 041 §18) ---

# Five kinds of worst case: each takes 2 or 6 bytes per character in its JSON form.
WORST_ERRORS = ['"' * 300, "\\" * 300, "\x01" * 300, "\t" * 300, "я" * 300]


def _json_form_bytes(text: str) -> int:
    """UTF-8 bytes of ``text`` between the quotes of a compact JSON string."""
    escaped = json.dumps(text, ensure_ascii=False, separators=(",", ":"))[1:-1]
    return len(escaped.encode("utf-8"))


def _error_lines(errors: Sequence[str]) -> list[str]:
    lines = repair_payload({}, errors, "")["validation_errors"]
    assert isinstance(lines, list)
    texts: list[str] = []
    for line in lines:
        assert isinstance(line, str)
        texts.append(line)
    return texts


def _user_object(body: dict[str, object]) -> dict[str, object]:
    payload = json.loads(_contents(body)[1])
    assert isinstance(payload, dict)
    return payload


def test_an_error_of_quotes_backslashes_and_control_characters_is_cut_to_256_json_bytes() -> None:
    lines = _error_lines(WORST_ERRORS)

    assert len(lines) == len(WORST_ERRORS)
    for error, line in zip(WORST_ERRORS, lines, strict=True):
        assert error.startswith(line)
        assert _json_form_bytes(line) <= 256
        # The next character would not fit, so the cut is as long as the limit allows.
        assert _json_form_bytes(error[: len(line) + 1]) > 256
    # 128 quotes or backslashes take 256 bytes, 42 control characters take 252 (43 take 258).
    assert lines[0] == '"' * 128
    assert lines[1] == "\\" * 128
    assert lines[2] == "\x01" * 42
    assert lines[4] == "я" * 128


def test_no_error_cut_splits_a_character_or_an_escape() -> None:
    # Characters of 1 to 4 UTF-8 bytes and escapes of 2 and 6 bytes, at every offset of the cut.
    for pad in range(8):
        for error in ("\x01" * pad + '\U0001f600я"€' * 100, "a" * pad + "\n\\\U0001f600" * 100):
            line = _error_lines([error])[0]
            assert line.encode("utf-8").decode("utf-8") == line
            assert error.startswith(line)
            assert _json_form_bytes(line) <= 256
            assert _json_form_bytes(error[: len(line) + 1]) > 256


def test_the_largest_window_with_a_long_instruction_and_a_long_answer_fits_the_context(
    db: Database,
) -> None:
    profile = _with_instruction("я" * 2_000)
    segments = [
        NewSegment(
            id=f"s{n}",
            ordinal=n,
            start_ms=n * 5_000,
            end_ms=(n + 1) * 5_000,
            text="я" * 500,
        )
        for n in range(150)
    ]
    with db.write_session() as session:
        revision = _revision(session, _source_id(session), segments=segments)
        plan = plan_run(session, revision.id, profile, TEST_PROFILE)
    assert len(plan.windows) > 1
    largest = max(range(len(plan.windows)), key=lambda index: plan.first_reservations[index])
    base = user_payload(revision, profile, plan.unit_level, plan.units, plan.windows[largest])
    errors = WORST_ERRORS * 10  # 50 errors of the worst kinds
    assert len(errors) == 50

    body = repair_body(TEST_PROFILE, base, errors, '"' * 300_000)

    context, max_tokens = TEST_PROFILE.context_tokens, TEST_PROFILE.max_tokens
    assert request_bytes(body) + max_tokens <= context
    # The answer takes what the context leaves, up to one character of 2 bytes of quotes.
    assert request_bytes(body) + max_tokens >= context - 1
    # The window uses most of the room the limit leaves, so the check is not vacuous.
    without_answer = repair_body(TEST_PROFILE, base, errors, "")
    assert request_bytes(without_answer) + 2 * max_tokens > context - 10_000


def test_a_short_previous_output_reaches_the_repair_request_unchanged() -> None:
    answer = 'Коротко: "так", \\ і\nрядок'

    body = repair_body(TEST_PROFILE, {"timing_level": "segment", "units": []}, ["помилка"], answer)

    payload = _user_object(body)
    assert payload["previous_output"] == answer
    assert payload["validation_errors"] == ["помилка"]
    assert payload["timing_level"] == "segment"
    assert payload["units"] == []


def test_the_answer_is_cut_to_what_a_small_context_leaves_on_a_character_boundary() -> None:
    small = replace(TEST_PROFILE, context_tokens=16_000)

    body = repair_body(small, {"timing_level": "segment", "units": []}, [], "\U0001f600" * 20_000)

    answer = _user_object(body)["previous_output"]
    assert isinstance(answer, str)
    assert 0 < len(answer) < 20_000
    assert set(answer) == {"\U0001f600"}
    # Each character is 4 bytes, so at most 3 bytes of the room are left over.
    assert (
        small.context_tokens - 3 <= request_bytes(body) + small.max_tokens <= small.context_tokens
    )


def test_a_repair_body_that_does_not_fit_without_its_answer_is_refused() -> None:
    base = {"timing_level": "segment", "units": [{"text": "я" * 20_000}]}

    with pytest.raises(ValueError, match="не вміщується"):
        repair_body(replace(TEST_PROFILE, context_tokens=20_000), base, [], "x")
