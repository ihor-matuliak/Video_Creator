"""Offline tests for the dev-only selection probe (apps/backend/scripts/analysis_probe.py).

Every case either stops before the transport or runs through httpx2.MockTransport; nothing here
touches the network.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx2
import pytest

from clipforge.adapters.openrouter_chat import (
    PROMPT_VERSION,
    SELECT_SYSTEM_PROMPT,
    build_chat_body,
    request_bytes,
)
from clipforge.domain.analysis_windows import Window
from clipforge.domain.search_profile import DEFAULT_PROFILE
from clipforge.services.analysis_plan import UNIT_LEVEL_SEGMENT, user_payload
from clipforge.storage import TranscriptRevision

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
FAKE_KEY = "sk-or-v1-test-key-0123456789abcdef"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
ENV = {"OPENROUTER_API_KEY": FAKE_KEY}
MODEL = "vendor/candidate-selector"
BASE_ARGS = [
    "--model",
    MODEL,
    "--prompt-cap",
    "0.25",
    "--completion-cap",
    "2",
    "--context-tokens",
    "128000",
    "--max-tokens",
    "4096",
]
GENERATION_ID = "gen-test-1"
USAGE: dict[str, object] = {"prompt_tokens": 1500, "completion_tokens": 180, "cost": 0.00042}

Handler = Callable[[httpx2.Request], httpx2.Response]


def _load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "analysis_probe", SCRIPTS_DIR / "analysis_probe.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["analysis_probe"] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


class _Body(httpx2.SyncByteStream):
    """A body that arrives in pieces, as on the network: httpx2 does not preload it."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __iter__(self) -> Iterator[bytes]:
        for start in range(0, len(self._data), 8192):
            yield self._data[start : start + 8192]


def wire(status: int, data: bytes = b"") -> httpx2.Response:
    return httpx2.Response(status, stream=_Body(data))


def chat_answer(
    content: str,
    *,
    finish_reason: str = "stop",
    usage: object = USAGE,
    generation_id: object = GENERATION_ID,
) -> httpx2.Response:
    body = {
        "id": generation_id,
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": usage,
    }
    return wire(200, json.dumps(body).encode("utf-8"))


def candidate(
    start: str = "seg-004", end: str = "seg-005", *, categories: list[str] | None = None
) -> dict[str, object]:
    return {
        "title": "Pan falls on the table",
        "categories": categories or ["funny"],
        "start_id": start,
        "end_id": end,
        "evidence_ids": [start],
        "reason": "The pan falls and the reaction makes the moment clear.",
        "scores": {
            "clarity": 4,
            "hook": 3,
            "completeness": 3,
            "emotion": 3,
            "novelty": 2,
            "relevance": 3,
        },
        "visual_review_required": False,
    }


def selection(*candidates: dict[str, object]) -> str:
    return json.dumps({"candidates": list(candidates)}, ensure_ascii=False)


def _ok(_request: httpx2.Request) -> httpx2.Response:
    return chat_answer(selection(candidate()))


@dataclass(frozen=True)
class _Run:
    code: int
    seen: list[httpx2.Request]
    out_dir: Path
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def _send(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    argv: list[str] | None = None,
    env: Mapping[str, str] = ENV,
    reply: Handler = _ok,
) -> _Run:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return reply(request)

    out_dir = tmp_path / "out"
    code = probe.main(
        BASE_ARGS if argv is None else argv,
        env=env,
        transport=httpx2.MockTransport(handler),
        out_dir=out_dir,
    )
    captured = capsys.readouterr()
    return _Run(code, seen, out_dir, captured.out, captured.err)


def _summary(out_dir: Path) -> dict[str, Any]:
    (path,) = out_dir.glob("*.summary.json")
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _assert_no_key_on_disk(out_dir: Path) -> None:
    for path in out_dir.iterdir():
        assert FAKE_KEY not in path.read_text(encoding="utf-8")


def _args_with(option: str, value: str) -> list[str]:
    args = list(BASE_ARGS)
    args[args.index(option) + 1] = value
    return args


# ------------------------------------------------------------------ synthetic request


def test_synthetic_transcript_mixes_english_and_spanish() -> None:
    units = probe.synthetic_units()
    texts = [unit.text for unit in units]

    assert len({unit.id for unit in units}) == len(units)
    assert [unit.ordinal for unit in units] == list(range(len(units)))
    assert all(earlier.end_ms <= later.start_ms for earlier, later in itertools.pairwise(units))
    assert max(unit.end_ms for unit in units) <= probe.SYNTHETIC_DURATION_MS
    assert any(char in "¿¡áéíóúñ" for text in texts for char in text)
    assert any(" the " in text.lower() for text in texts)


def test_request_is_built_only_with_the_service_functions() -> None:
    profile = probe.build_profile(probe.build_parser().parse_args(BASE_ARGS))
    request = probe.build_request(profile, "strings")
    units = probe.synthetic_units()
    revision = TranscriptRevision(
        language=probe.SYNTHETIC_LANGUAGE, duration_ms=probe.SYNTHETIC_DURATION_MS
    )
    window = Window(index=0, first=0, core_first=0, core_last=len(units) - 1, last=len(units) - 1)
    expected = build_chat_body(
        profile,
        system_prompt=SELECT_SYSTEM_PROMPT,
        user_payload=user_payload(revision, DEFAULT_PROFILE, UNIT_LEVEL_SEGMENT, units, window),
    )

    assert request.body == expected
    assert request.request_size_bytes == request_bytes(expected)
    assert request.body["provider"] == {
        "require_parameters": True,
        "max_price": {"prompt": "0.25", "completion": "2"},
    }
    assert request.core_ordinals == (0, len(units) - 1)
    assert len(request.units) == len(units)


def test_numbers_form_puts_json_numbers_in_max_price() -> None:
    profile = probe.build_profile(probe.build_parser().parse_args(BASE_ARGS))

    request = probe.build_request(profile, "numbers")

    assert request.body["provider"] == {
        "require_parameters": True,
        "max_price": {"prompt": 0.25, "completion": 2.0},
    }


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--prompt-cap", "abc"),
        ("--prompt-cap", "0"),
        ("--prompt-cap", "1e3"),
        ("--prompt-cap", "-1"),
        ("--prompt-cap", "1001"),
        ("--completion-cap", "NaN"),
        ("--completion-cap", ""),
        ("--context-tokens", "0"),
        ("--max-tokens", "0"),
        ("--model", " vendor/candidate-selector"),
    ],
)
def test_bad_options_are_refused_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], option: str, value: str
) -> None:
    run = _send(tmp_path, capsys, argv=_args_with(option, value))

    assert run.code == probe.EXIT_REFUSED
    assert run.seen == []
    assert not run.out_dir.exists()
    assert run.stderr.startswith("refused:")
    assert FAKE_KEY not in run.output


def test_context_too_small_for_a_selection_window_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 40 000 - 2 * 4 096 is already below the 32 768-byte minimum, whatever the envelope.
    run = _send(tmp_path, capsys, argv=_args_with("--context-tokens", "40000"))

    assert run.code == probe.EXIT_REFUSED
    assert run.seen == []
    assert "ANALYSIS_MODEL_NOT_SUPPORTED" in run.stderr


def test_max_price_form_is_checked_by_the_parser(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return chat_answer(selection(candidate()))

    with pytest.raises(SystemExit) as raised:
        probe.main(
            [*BASE_ARGS, "--max-price-as", "yaml"],
            env=ENV,
            transport=httpx2.MockTransport(handler),
            out_dir=tmp_path / "out",
        )

    capsys.readouterr()
    assert raised.value.code == probe.EXIT_REFUSED
    assert seen == []


# ------------------------------------------------------------------- key and output


def test_missing_key_is_refused_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _send(tmp_path, capsys, env={})

    assert run.code == probe.EXIT_REFUSED
    assert run.seen == []
    assert not run.out_dir.exists()
    assert "OPENROUTER_API_KEY is not set" in run.stderr


def test_unusable_key_is_refused_without_echoing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad_key = "sk-or v1 with spaces"

    run = _send(tmp_path, capsys, env={"OPENROUTER_API_KEY": bad_key})

    assert run.code == probe.EXIT_REFUSED
    assert run.seen == []
    assert "not a usable API key" in run.stderr
    assert bad_key not in run.output


def test_unwritable_out_dir_is_refused_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_writes(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    # chmod cannot make a Windows directory read-only, so the failing write is simulated.
    monkeypatch.setattr(probe.tempfile, "NamedTemporaryFile", refuse_writes)

    run = _send(tmp_path, capsys)

    assert run.code == probe.EXIT_REFUSED
    assert run.seen == []
    assert "Permission denied" in run.stderr
    assert FAKE_KEY not in run.output


# ------------------------------------------------------------------------ one call


def test_success_is_one_post_and_the_summary_records_the_answer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    content = selection(candidate())

    run = _send(
        tmp_path,
        capsys,
        reply=lambda _request: chat_answer(content, generation_id="gen-ok-1"),
    )

    assert run.code == probe.EXIT_OK
    assert len(run.seen) == 1
    sent_request = run.seen[0]
    assert sent_request.method == "POST"
    assert str(sent_request.url) == ENDPOINT
    assert sent_request.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert FAKE_KEY not in sent_request.content.decode("utf-8")
    sent = json.loads(sent_request.content)
    expected_body = probe.build_request(
        probe.build_profile(probe.build_parser().parse_args(BASE_ARGS)), "strings"
    ).body
    assert sent == expected_body
    assert "temperature" not in sent

    summary = _summary(run.out_dir)
    assert summary["outcome"] == "ok"
    assert summary["status"] == 200
    assert summary["error_kind"] is None
    assert summary["finish_reason"] == "stop"
    assert summary["cost_usd"] == "0.00042"
    assert summary["generation_id"] == "gen-ok-1"
    assert summary["usage"] == {"prompt_tokens": 1500, "completion_tokens": 180}
    assert summary["request"]["request_bytes"] == request_bytes(expected_body)
    assert summary["request"]["prompt_version"] == PROMPT_VERSION
    assert summary["request"]["max_price_as"] == "strings"
    assert summary["request"]["max_price"] == {"prompt": "0.25", "completion": "2"}
    assert summary["selection"]["accepted"] is True
    assert summary["selection"]["candidate_count"] == 1
    assert summary["selection"]["accepted_count"] == 1
    assert summary["endpoint"] == ENDPOINT
    assert len(list(run.out_dir.glob("vendor-candidate-selector-*.summary.json"))) == 1
    assert FAKE_KEY not in run.output
    _assert_no_key_on_disk(run.out_dir)


def test_numbers_form_is_sent_and_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _send(tmp_path, capsys, argv=[*BASE_ARGS, "--max-price-as", "numbers"])

    assert run.code == probe.EXIT_OK
    sent = json.loads(run.seen[0].content)
    assert sent["provider"]["max_price"] == {"prompt": 0.25, "completion": 2.0}
    summary = _summary(run.out_dir)
    assert summary["request"]["max_price_as"] == "numbers"
    assert summary["request"]["max_price"] == {"prompt": 0.25, "completion": 2.0}


def test_answer_without_usage_or_cost_records_nulls(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _send(
        tmp_path,
        capsys,
        reply=lambda _request: chat_answer(selection(candidate()), usage=None),
    )

    assert run.code == probe.EXIT_OK
    summary = _summary(run.out_dir)
    assert summary["usage"] is None
    assert summary["cost_usd"] is None


def test_selection_with_an_unknown_unit_is_exit_1_and_not_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    content = selection(candidate(start="seg-999"))

    run = _send(tmp_path, capsys, reply=lambda _request: chat_answer(content))

    assert run.code == probe.EXIT_ERROR_RESPONSE
    assert len(run.seen) == 1
    summary = _summary(run.out_dir)
    assert summary["outcome"] == "invalid_selection"
    assert summary["status"] == 200
    assert summary["selection"]["accepted"] is False
    assert any("unknown unit id" in error for error in summary["selection"]["validate_errors"])


def test_answer_that_is_not_selection_json_is_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _send(
        tmp_path,
        capsys,
        reply=lambda _request: chat_answer("Here are the clips: 00:01 to 00:20."),
    )

    assert run.code == probe.EXIT_ERROR_RESPONSE
    assert len(run.seen) == 1
    summary = _summary(run.out_dir)
    assert summary["outcome"] == "invalid_selection"
    assert summary["selection"]["accepted"] is False
    assert summary["selection"]["parse_errors"]


def test_key_echoed_by_the_provider_is_redacted_everywhere(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    content = selection(candidate(start=FAKE_KEY))

    run = _send(
        tmp_path,
        capsys,
        reply=lambda _request: chat_answer(content, generation_id=FAKE_KEY),
    )

    assert run.code == probe.EXIT_ERROR_RESPONSE
    assert FAKE_KEY not in run.output
    _assert_no_key_on_disk(run.out_dir)
    summary = _summary(run.out_dir)
    assert summary["generation_id"] == "[REDACTED]"
    assert any("[REDACTED]" in error for error in summary["selection"]["validate_errors"])


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, "invalid_request"),
        (401, "auth"),
        (402, "funds"),
        (404, "model_unavailable"),
        (429, "rate_limited"),
        (503, "upstream"),
    ],
)
def test_error_response_is_exit_1_and_is_not_retried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], status: int, kind: str
) -> None:
    run = _send(tmp_path, capsys, reply=lambda _request: wire(status))

    assert run.code == probe.EXIT_ERROR_RESPONSE
    assert len(run.seen) == 1
    assert FAKE_KEY not in run.output
    summary = _summary(run.out_dir)
    assert summary["outcome"] == "error_response"
    assert summary["status"] == status
    assert summary["error_kind"] == kind
    assert summary["selection"] is None


def test_200_without_a_chat_shape_is_invalid_response_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _send(tmp_path, capsys, reply=lambda _request: wire(200, b'{"not": "a chat"}'))

    assert run.code == probe.EXIT_ERROR_RESPONSE
    assert len(run.seen) == 1
    summary = _summary(run.out_dir)
    assert summary["outcome"] == "invalid_response"
    assert summary["status"] == 200
    assert summary["error_kind"] == "invalid_response"


def test_read_timeout_after_send_is_unknown_outcome_and_not_retried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    run = _send(tmp_path, capsys, reply=timeout)

    assert run.code == probe.EXIT_UNKNOWN_OUTCOME
    assert len(run.seen) == 1
    assert "unknown_outcome" in run.stderr
    assert FAKE_KEY not in run.output
    summary = _summary(run.out_dir)
    assert summary["outcome"] == "unknown_outcome"
    assert summary["error_kind"] == "unknown_outcome"
    assert summary["status"] is None
    assert summary["selection"] is None


def test_connect_error_is_not_sent_exit_4(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def refused(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    run = _send(tmp_path, capsys, reply=refused)

    assert run.code == probe.EXIT_NOT_SENT
    summary = _summary(run.out_dir)
    assert summary["outcome"] == "not_sent"
    assert summary["error_kind"] == "not_sent"
    assert summary["status"] is None


def test_summary_write_failure_prints_the_redacted_summary_and_exits_5(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse_write(path: Path, text: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(probe, "_write_summary", refuse_write)

    run = _send(
        tmp_path,
        capsys,
        reply=lambda _request: chat_answer(selection(candidate()), generation_id=FAKE_KEY),
    )

    assert run.code == probe.EXIT_SAVE_FAILED
    assert len(run.seen) == 1
    assert "save_failed" in run.stderr
    assert FAKE_KEY not in run.output
    printed = json.loads(run.stdout)
    assert printed["outcome"] == "ok"
    assert printed["generation_id"] == "[REDACTED]"
    assert printed["save_error"]["type"] == "OSError"
