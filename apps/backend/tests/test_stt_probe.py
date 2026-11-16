"""Offline tests for the dev-only STT probe (apps/backend/scripts/stt_probe.py).

Every case uses synthetic dicts or httpx2.MockTransport; nothing here touches the network.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import tempfile
import wave
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx2
import pytest

from clipforge.adapters.media_tools import MediaTools

from .support import fixture_file

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
FAKE_KEY = "sk-or-v1-test-key-0123456789abcdef"
MODEL = "microsoft/mai-transcribe-2"
REFERENCE = "Flight 417 landed at 6:45, 25 minutes early."


def _load_probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stt_probe", SCRIPTS_DIR / "stt_probe.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["stt_probe"] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def _write_wav(path: Path, seconds: float, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


def _word(text: str, start: float, end: float) -> dict[str, Any]:
    return {"word": text, "start": start, "end": end}


# ------------------------------------------------------------------ extract_summary


def test_summary_word_level() -> None:
    body = {
        "text": "Flight 417 landed at 6:45, 25 minutes early.",
        "language": "en",
        "duration": 5.25,
        "segments": [
            {"id": 0, "start": 0.4, "end": 2.0, "text": "Flight 417 landed"},
            {"id": 1, "start": 2.0, "end": 5.1, "text": "at 6:45, 25 minutes early."},
        ],
        "words": [
            _word("Flight", 0.4, 0.8),
            _word("417", 0.8, 1.5),
            _word("landed", 1.5, 2.0),
            _word("early.", 4.6, 5.1),
        ],
        "usage": {"seconds": 5.25, "cost": 0.0002},
    }

    summary = probe.extract_summary(body, REFERENCE)

    assert summary["timing_level"] == "word"
    assert summary["language"] == "en"
    assert summary["duration_s"] == 5.25
    assert summary["segment_count"] == 2
    assert summary["timed_segment_count"] == 2
    assert summary["word_count"] == 4
    assert summary["timed_word_count"] == 4
    assert summary["words_location"] == "top_level"
    assert summary["first_word_start_s"] == 0.4
    assert summary["last_word_end_s"] == 5.1
    assert summary["usage"] == {"seconds": 5.25, "cost": 0.0002}
    assert summary["text_empty"] is False
    assert summary["error"] is None
    assert summary["wer"]["value"] == 0.0
    assert summary["wer"]["ref_words"] == summary["wer"]["hyp_words"]


def test_summary_words_nested_in_segments() -> None:
    body = {
        "text": "hello there",
        "segments": [
            {"start": 0.0, "end": 1.0, "words": [_word("hello", 0.1, 0.5)]},
            {"start": 1.0, "end": 2.0, "words": [_word("there", 1.2, 1.9)]},
        ],
    }

    summary = probe.extract_summary(body, "hello there")

    assert summary["timing_level"] == "word"
    assert summary["words_location"] == "in_segments"
    assert summary["first_word_start_s"] == 0.1
    assert summary["last_word_end_s"] == 1.9


def test_summary_segment_only() -> None:
    body = {
        "text": "Flight 417 landed 25 minutes early.",
        "language": "en",
        "duration": "5.0",
        "segments": [{"id": 0, "start": 0.0, "end": 5.0, "text": "Flight 417 landed"}],
        "words": [],
    }

    summary = probe.extract_summary(body, REFERENCE)

    assert summary["timing_level"] == "segment"
    assert summary["segment_count"] == 1
    assert summary["word_count"] == 0
    assert summary["words_location"] == "none"
    assert summary["first_word_start_s"] is None
    assert summary["last_word_end_s"] is None
    assert summary["duration_s"] is None  # a string duration is a contract deviation, not a number
    assert summary["usage"] is None
    # reference has 8 words, the hypothesis misses "at" and "6:45"
    assert summary["wer"]["ref_words"] == 8
    assert summary["wer"]["edit_distance"] == 2
    assert summary["wer"]["value"] == pytest.approx(0.25)


def test_summary_empty_text() -> None:
    summary = probe.extract_summary({"text": "", "language": "en", "duration": 0.0}, REFERENCE)

    assert summary["text_empty"] is True
    assert summary["text_chars"] == 0
    assert summary["timing_level"] == "none"
    assert summary["segment_count"] == 0
    assert summary["word_count"] == 0
    assert summary["error"] is None
    assert summary["wer"]["value"] == 1.0
    assert summary["wer"]["hyp_words"] == 0


def test_summary_error_body() -> None:
    body = {"error": {"code": 402, "message": "Insufficient credits"}}

    summary = probe.extract_summary(body, REFERENCE)

    assert summary["error"] == {"code": 402, "message": "Insufficient credits"}
    assert summary["timing_level"] == "none"
    assert summary["wer"] is None
    assert summary["usage"] is None


def test_summary_untimed_words_are_not_word_level() -> None:
    body = {"text": "a b", "words": [{"word": "a"}, {"word": "b", "start": 2.0, "end": 1.0}]}

    summary = probe.extract_summary(body, "a b")

    assert summary["word_count"] == 2
    assert summary["timed_word_count"] == 0
    assert summary["timing_level"] == "none"


def test_summary_non_object_body() -> None:
    summary = probe.extract_summary(["unexpected"], REFERENCE)

    assert summary["timing_level"] == "none"
    assert summary["text_empty"] is True


def test_wer_normalization() -> None:
    result = probe.word_error_rate(
        "It cost 1.2 million dollars, or 1,250 each; 12% more.",
        "it cost 1.2 Million dollars or 1250 each 12 percent more",
    )

    assert result["value"] == 0.0


# --------------------------------------------------------------------------- limits


def test_inspect_audio_refuses_over_60_seconds(tmp_path: Path) -> None:
    wav = _write_wav(tmp_path / "long.wav", seconds=61, rate=1000)

    with pytest.raises(probe.ProbeRefusedError, match="60 s"):
        probe.inspect_audio(wav)


def test_inspect_audio_accepts_60_seconds(tmp_path: Path) -> None:
    wav = _write_wav(tmp_path / "edge.wav", seconds=60, rate=1000)

    info = probe.inspect_audio(wav)

    assert info.duration_s == 60.0
    assert info.channels == 1


def test_inspect_audio_refuses_over_20_mib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wav = _write_wav(tmp_path / "small.wav", seconds=1, rate=1000)
    assert probe.MAX_AUDIO_BYTES == 20 * 1024 * 1024
    monkeypatch.setattr(probe, "MAX_AUDIO_BYTES", wav.stat().st_size - 1)

    with pytest.raises(probe.ProbeRefusedError, match="bytes"):
        probe.inspect_audio(wav)


def test_inspect_audio_refuses_non_wav(tmp_path: Path) -> None:
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wav file")

    with pytest.raises(probe.ProbeRefusedError, match="WAV"):
        probe.inspect_audio(junk)


# ------------------------------------------------------------------------------ CLI


def test_dry_run_prints_truncated_request_without_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)

    code = probe.main(["--audio", str(wav), "--model", MODEL, "--dry-run"], env={})

    captured = capsys.readouterr()
    assert code == 0
    printed = json.loads(captured.out)
    assert printed["method"] == "POST"
    assert printed["url"] == "https://openrouter.ai/api/v1/audio/transcriptions"
    body = printed["body"]
    assert list(body) == [
        "model",
        "input_audio",
        "temperature",
        "response_format",
        "timestamp_granularities",
    ]
    assert body["model"] == MODEL
    assert body["input_audio"]["format"] == "wav"
    assert "base64 chars total" in body["input_audio"]["data"]
    assert len(body["input_audio"]["data"]) < 100
    assert "language" not in body  # no language by default: the provider detects it
    assert "provider" not in body
    assert body["temperature"] == 0
    assert body["response_format"] == "verbose_json"
    assert body["timestamp_granularities"] == ["segment", "word"]


def test_dry_run_never_prints_the_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)

    code = probe.main(
        ["--audio", str(wav), "--model", MODEL, "--dry-run"],
        env={"OPENROUTER_API_KEY": FAKE_KEY},
    )

    captured = capsys.readouterr()
    assert code == 0
    assert FAKE_KEY not in captured.out + captured.err


def test_missing_key_refuses_before_any_request(tmp_path: Path) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={})

    code = probe.main(
        ["--audio", str(wav), "--model", MODEL, "--out-dir", str(tmp_path / "out")],
        env={},
        transport=httpx2.MockTransport(handler),
    )

    assert code == probe.EXIT_REFUSED
    assert calls == []
    assert not (tmp_path / "out").exists()


def _run_with_handler(
    tmp_path: Path, handler: Any, capsys: pytest.CaptureFixture[str]
) -> tuple[int, Path, str]:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)
    out_dir = tmp_path / "out"
    code = probe.main(
        ["--audio", str(wav), "--model", MODEL, "--out-dir", str(out_dir)],
        env={"OPENROUTER_API_KEY": FAKE_KEY},
        transport=httpx2.MockTransport(handler),
    )
    captured = capsys.readouterr()
    return code, out_dir, captured.out + captured.err


def _assert_no_key_on_disk(out_dir: Path) -> None:
    for path in out_dir.iterdir():
        assert FAKE_KEY not in path.read_text(encoding="utf-8")


def test_success_writes_response_and_summary_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx2.Request] = []
    response_body = {
        "text": "Flight 417 landed.",
        "language": "en",
        "duration": 2.0,
        "words": [_word("Flight", 0.2, 0.5), _word("landed.", 1.0, 1.6)],
        "usage": {"cost": 0.001},
        "echo": FAKE_KEY,  # even a misbehaving provider must not get the key onto disk
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=response_body, headers={"X-Generation-Id": "gen-123"})

    code, out_dir, output = _run_with_handler(tmp_path, handler, capsys)

    assert code == 0
    assert len(seen) == 1
    assert seen[0].headers["Authorization"] == f"Bearer {FAKE_KEY}"
    sent = json.loads(seen[0].content)
    assert sent["model"] == MODEL
    assert sent["input_audio"]["format"] == "wav"

    responses = list(out_dir.glob("microsoft-mai-transcribe-2-*.response.json"))
    summaries = list(out_dir.glob("microsoft-mai-transcribe-2-*.summary.json"))
    assert len(responses) == 1
    assert len(summaries) == 1
    saved = json.loads(responses[0].read_text(encoding="utf-8"))
    assert saved["text"] == "Flight 417 landed."
    summary = json.loads(summaries[0].read_text(encoding="utf-8"))
    assert summary["outcome"] == "ok"
    assert summary["status"] == 200
    assert summary["generation_id"] == "gen-123"
    assert summary["timing_level"] == "word"
    assert summary["response_file"] == responses[0].name
    assert summary["latency_s"] >= 0
    assert FAKE_KEY not in output
    _assert_no_key_on_disk(out_dir)


def test_read_timeout_is_unknown_outcome_and_not_retried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        raise httpx2.ReadTimeout("timed out", request=request)

    code, out_dir, output = _run_with_handler(tmp_path, handler, capsys)

    assert code == probe.EXIT_UNKNOWN_OUTCOME
    assert len(calls) == 1
    assert list(out_dir.glob("*.response.json")) == []
    (summary_path,) = out_dir.glob("*.summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["outcome"] == "unknown_outcome"
    assert summary["status"] is None
    assert summary["response_file"] is None
    assert summary["error"]["type"] == "ReadTimeout"
    assert "unknown_outcome" in output
    assert FAKE_KEY not in output
    _assert_no_key_on_disk(out_dir)


def test_connect_timeout_is_not_sent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        raise httpx2.ConnectTimeout("no route", request=request)

    code, out_dir, _ = _run_with_handler(tmp_path, handler, capsys)

    assert code == probe.EXIT_NOT_SENT
    assert len(calls) == 1
    (summary_path,) = out_dir.glob("*.summary.json")
    assert json.loads(summary_path.read_text(encoding="utf-8"))["outcome"] == "not_sent"


def test_error_response_is_recorded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(
            402,
            json={"error": {"code": 402, "message": "Insufficient credits"}},
            headers={"X-Generation-Id": "gen-err"},
        )

    code, out_dir, _ = _run_with_handler(tmp_path, handler, capsys)

    assert code == probe.EXIT_ERROR_RESPONSE
    assert len(calls) == 1
    (response_path,) = out_dir.glob("*.response.json")
    assert json.loads(response_path.read_text(encoding="utf-8"))["error"]["code"] == 402
    (summary_path,) = out_dir.glob("*.summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["outcome"] == "error_response"
    assert summary["status"] == 402
    assert summary["generation_id"] == "gen-err"
    assert summary["error"] == {"code": 402, "message": "Insufficient credits"}
    assert summary["timing_level"] == "none"


# ------------------------------------------------------- output directory and saving


def test_out_dir_is_created_and_write_tested_before_the_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)
    out_dir = tmp_path / "nested" / "out"
    dir_seen_by_transport: list[bool] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        dir_seen_by_transport.append(out_dir.is_dir())
        return httpx2.Response(200, json={"text": "ok"})

    code = probe.main(
        ["--audio", str(wav), "--model", MODEL, "--out-dir", str(out_dir)],
        env={"OPENROUTER_API_KEY": FAKE_KEY},
        transport=httpx2.MockTransport(handler),
    )

    captured = capsys.readouterr()
    assert code == probe.EXIT_OK
    assert dir_seen_by_transport == [True]
    assert FAKE_KEY not in captured.out + captured.err
    assert not [p for p in out_dir.iterdir() if p.name.startswith(".write-test-")]


@pytest.mark.parametrize("layout", ["out_dir_is_a_file", "parent_is_a_file"])
def test_invalid_out_dir_refuses_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], layout: str
) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("not a directory", encoding="utf-8")
    out_dir = blocker if layout == "out_dir_is_a_file" else blocker / "out"
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={})

    code = probe.main(
        ["--audio", str(wav), "--model", MODEL, "--out-dir", str(out_dir)],
        env={"OPENROUTER_API_KEY": FAKE_KEY},
        transport=httpx2.MockTransport(handler),
    )

    captured = capsys.readouterr()
    assert code == probe.EXIT_REFUSED
    assert code != 0
    assert calls == []
    assert "refused: output directory" in captured.err
    assert FAKE_KEY not in captured.out + captured.err
    assert blocker.read_text(encoding="utf-8") == "not a directory"


def test_unwritable_out_dir_refuses_before_any_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)
    calls: list[httpx2.Request] = []

    def refuse_writes(*args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    # chmod cannot make a Windows directory read-only, so the failing write is simulated.
    monkeypatch.setattr(probe.tempfile, "NamedTemporaryFile", refuse_writes)

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={})

    code = probe.main(
        ["--audio", str(wav), "--model", MODEL, "--out-dir", str(tmp_path / "out")],
        env={"OPENROUTER_API_KEY": FAKE_KEY},
        transport=httpx2.MockTransport(handler),
    )

    captured = capsys.readouterr()
    assert code == probe.EXIT_REFUSED
    assert calls == []
    assert "Permission denied" in captured.err
    assert FAKE_KEY not in captured.out + captured.err


def test_write_failure_after_response_prints_redacted_summary_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)
    out_dir = tmp_path / "out"
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        # The output directory is replaced by a file while the paid request is in flight.
        out_dir.rmdir()
        out_dir.write_text("not a directory", encoding="utf-8")
        return httpx2.Response(
            200,
            json={
                "text": "Flight 417 landed.",
                "words": [_word("Flight", 0.2, 0.5)],
                "echo": FAKE_KEY,  # the summary printed on failure must not carry the key either
            },
            headers={"X-Generation-Id": "gen-save"},
        )

    code = probe.main(
        ["--audio", str(wav), "--model", MODEL, "--out-dir", str(out_dir)],
        env={"OPENROUTER_API_KEY": FAKE_KEY},
        transport=httpx2.MockTransport(handler),
    )

    captured = capsys.readouterr()
    assert len(calls) == 1
    assert code == probe.EXIT_SAVE_FAILED
    assert code != 0
    assert "save_failed" in captured.err
    assert FAKE_KEY not in captured.out + captured.err
    printed = json.loads(captured.out)
    assert printed["outcome"] == "ok"
    assert printed["status"] == 200
    assert printed["generation_id"] == "gen-save"
    assert printed["response_file"] is None
    assert printed["save_error"]["type"] in {"FileNotFoundError", "NotADirectoryError"}
    assert printed["timing_level"] == "word"


def test_summary_write_failure_after_response_still_prints_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)
    out_dir = tmp_path / "out"
    real_write_json = probe._write_json

    def fail_summary_only(path: Path, value: object) -> None:
        if path.name.endswith(".summary.json"):
            raise OSError(28, "No space left on device")
        real_write_json(path, value)

    monkeypatch.setattr(probe, "_write_json", fail_summary_only)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"text": "Flight 417 landed.", "echo": FAKE_KEY},
            headers={"X-Generation-Id": "gen-summary"},
        )

    code = probe.main(
        ["--audio", str(wav), "--model", MODEL, "--out-dir", str(out_dir)],
        env={"OPENROUTER_API_KEY": FAKE_KEY},
        transport=httpx2.MockTransport(handler),
    )

    captured = capsys.readouterr()
    assert code == probe.EXIT_SAVE_FAILED
    assert "save_failed" in captured.err
    assert FAKE_KEY not in captured.out + captured.err
    (response_path,) = out_dir.glob("*.response.json")
    assert not list(out_dir.glob("*.summary.json"))
    printed = json.loads(captured.out)
    assert printed["response_file"] == response_path.name
    assert printed["save_error"]["type"] == "OSError"
    assert printed["generation_id"] == "gen-summary"
    _assert_no_key_on_disk(out_dir)


# ------------------------------------------------------- language and MAI transcribe style

GEMINI = "google/gemini-3.5-transcribe"
ENV = {"OPENROUTER_API_KEY": FAKE_KEY}
BODY_KEYS = ["model", "input_audio", "temperature", "response_format", "timestamp_granularities"]


def _dry_run_body(argv: list[str], capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    code = probe.main([*argv, "--dry-run"], env={})
    assert code == 0
    printed: dict[str, Any] = json.loads(capsys.readouterr().out)
    body: dict[str, Any] = printed["body"]
    return body


def test_language_is_sent_only_when_given(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)

    default = _dry_run_body(["--audio", str(wav), "--model", MODEL], capsys)
    spanish = _dry_run_body(["--audio", str(wav), "--model", MODEL, "--language", "es"], capsys)

    assert list(default) == BODY_KEYS
    assert list(spanish) == [*BODY_KEYS[:2], "language", *BODY_KEYS[2:]]
    assert spanish["language"] == "es"
    assert "provider" not in spanish


def test_sent_request_has_no_language_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"text": "ok"})

    code, out_dir, _ = _run_with_handler(tmp_path, handler, capsys)

    assert code == 0
    sent = json.loads(seen[0].content)
    assert "language" not in sent
    assert "provider" not in sent
    (summary_path,) = out_dir.glob("*.summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["request"] == {"language": None, "mai_style": None, "window": None}
    assert summary["audio"]["file"] == "s.wav"


@pytest.mark.parametrize("language", ["", "eng", "EN", "e1", "uk-UA", " en"])
def test_language_must_be_a_two_letter_iso_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], language: str
) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={})

    code = probe.main(
        ["--audio", str(wav), "--model", MODEL, "--language", language],
        env=ENV,
        transport=httpx2.MockTransport(handler),
    )

    captured = capsys.readouterr()
    assert code == probe.EXIT_REFUSED
    assert calls == []
    assert "--language" in captured.err
    assert captured.out == ""


def test_mai_style_is_forwarded_for_mai_models(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)
    out_dir = tmp_path / "out"
    seen: list[httpx2.Request] = []
    expected_provider = {
        "options": {
            "azure": {"enhancedMode": {"modelOptions": {"transcribeStyle": "no-such-style"}}}
        }
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"text": "ok"})

    argv = ["--audio", str(wav), "--model", MODEL, "--mai-style", "no-such-style"]
    dry = _dry_run_body(argv, capsys)
    code = probe.main(
        [*argv, "--language", "es", "--out-dir", str(out_dir)],
        env=ENV,
        transport=httpx2.MockTransport(handler),
    )

    capsys.readouterr()
    assert dry["provider"] == expected_provider
    assert "language" not in dry
    assert code == 0
    sent = json.loads(seen[0].content)
    assert sent["provider"] == expected_provider
    assert sent["language"] == "es"
    (summary_path,) = out_dir.glob("*.summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["request"] == {"language": "es", "mai_style": "no-such-style", "window": None}


@pytest.mark.parametrize("model", [GEMINI, "microsoft/phi-4"])
def test_mai_style_is_refused_for_other_models(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model: str
) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)
    out_dir = tmp_path / "out"
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={})

    code = probe.main(
        ["--audio", str(wav), "--model", model, "--mai-style", "x", "--out-dir", str(out_dir)],
        env=ENV,
        transport=httpx2.MockTransport(handler),
    )

    captured = capsys.readouterr()
    assert code == probe.EXIT_REFUSED
    assert calls == []
    assert not out_dir.exists()
    assert "--mai-style" in captured.err
    assert captured.out == ""
    assert FAKE_KEY not in captured.err


def test_gemini_request_without_mai_style_has_no_provider_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)

    body = _dry_run_body(["--audio", str(wav), "--model", GEMINI, "--language", "en"], capsys)

    assert "provider" not in body
    assert body["language"] == "en"


# -------------------------------------------------------- audio window of a video (--source)


def _file_state(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime_ns


def _parse(*extra: str) -> Any:
    return probe.build_parser().parse_args(["--model", MODEL, *extra])


def test_source_window_is_cut_to_a_16k_mono_wav_of_the_requested_length(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    media_tools: MediaTools,
) -> None:
    source = fixture_file("color_gop.mp4")
    before = _file_state(source)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    out_dir = tmp_path / "out"
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"text": "ok"})

    code = probe.main(
        [
            "--source",
            str(source),
            "--start-ms",
            "2000",
            "--duration-ms",
            "3000",
            "--model",
            MODEL,
            "--out-dir",
            str(out_dir),
        ],
        env=ENV,
        transport=httpx2.MockTransport(handler),
        media_tools=media_tools,
    )

    captured = capsys.readouterr()
    assert code == 0
    assert len(seen) == 1
    raw = base64.b64decode(json.loads(seen[0].content)["input_audio"]["data"])
    wav_path = tmp_path / "sent.wav"
    wav_path.write_bytes(raw)
    with wave.open(str(wav_path), "rb") as reader:
        assert reader.getframerate() == 16000
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getnframes() == 3000 * 16
    assert len(raw) == 44 + 2 * 3000 * 16
    (summary_path,) = out_dir.glob("*.summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    window = summary["request"]["window"]
    assert window.pop("seek_mode") in {"preseek", "full_decode"}
    assert window == {
        "source_file": "color_gop.mp4",
        "start_ms": 2000,
        "end_ms": 5000,
        "duration_ms": 3000,
        "audio_stream_index": 1,
    }
    assert summary["request"]["language"] is None
    assert summary["request"]["mai_style"] is None
    assert summary["audio"]["file"] is None
    assert summary["audio"]["duration_s"] == 3.0
    assert summary["audio"]["sample_rate"] == 16000
    assert summary["audio"]["channels"] == 1
    assert summary["audio"]["bytes"] == len(raw)
    assert list(scratch.iterdir()) == []  # the temporary WAV is gone
    assert _file_state(source) == before  # the video was only read
    assert FAKE_KEY not in captured.out + captured.err


def test_source_dry_run_needs_no_key_and_reports_the_window_audio_size(
    capsys: pytest.CaptureFixture[str], media_tools: MediaTools
) -> None:
    source = fixture_file("color_gop.mp4")
    before = _file_state(source)

    code = probe.main(
        [
            "--source",
            str(source),
            "--start-ms",
            "1000",
            "--duration-ms",
            "2500",
            "--model",
            MODEL,
            "--language",
            "es",
            "--dry-run",
        ],
        env={},
        media_tools=media_tools,
    )

    body = json.loads(capsys.readouterr().out)["body"]
    wav_bytes = 44 + 2 * 2500 * 16
    assert code == 0
    assert body["language"] == "es"
    assert f"<{-(-wav_bytes // 3) * 4} base64 chars total>" in body["input_audio"]["data"]
    assert _file_state(source) == before


@pytest.mark.parametrize(
    ("name", "start_ms", "duration_ms"),
    [
        ("color_gop.mp4", 11000, 3000),  # runs past the end of the 12 s video
        ("no_audio.mp4", 0, 1000),
        ("does-not-exist.mp4", 0, 1000),
    ],
)
def test_unusable_source_window_is_refused_before_any_request(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    media_tools: MediaTools,
    name: str,
    start_ms: int,
    duration_ms: int,
) -> None:
    source = tmp_path / name if name == "does-not-exist.mp4" else fixture_file(name)
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={})

    code = probe.main(
        [
            "--source",
            str(source),
            "--start-ms",
            str(start_ms),
            "--duration-ms",
            str(duration_ms),
            "--model",
            MODEL,
            "--out-dir",
            str(tmp_path / "out"),
        ],
        env=ENV,
        transport=httpx2.MockTransport(handler),
        media_tools=media_tools,
    )

    captured = capsys.readouterr()
    assert code == probe.EXIT_REFUSED
    assert calls == []
    assert "refused:" in captured.err
    assert captured.out == ""
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("duration", ["60001", "120000", "0", "-5"])
def test_window_longer_than_60_seconds_is_refused_before_any_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], duration: str
) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={})

    code = probe.main(
        [
            "--source",
            str(tmp_path / "never-opened.mp4"),
            "--start-ms",
            "0",
            "--duration-ms",
            duration,
            "--model",
            MODEL,
            "--out-dir",
            str(tmp_path / "out"),
        ],
        env=ENV,
        transport=httpx2.MockTransport(handler),
    )

    captured = capsys.readouterr()
    assert code == probe.EXIT_REFUSED
    assert calls == []
    assert captured.err.startswith("refused:")
    assert "60000" in captured.err or "--duration-ms must be positive" in captured.err
    assert not (tmp_path / "out").exists()


def test_window_limit_is_60000_ms_inclusive() -> None:
    source = ["--source", "video.mp4", "--start-ms", "5"]

    probe.validate_options(_parse(*source, "--duration-ms", "60000"))
    with pytest.raises(probe.ProbeRefusedError, match="60000"):
        probe.validate_options(_parse(*source, "--duration-ms", "60001"))


def test_audio_and_source_are_mutually_exclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)

    with pytest.raises(SystemExit) as raised:
        probe.main(
            [
                "--audio",
                str(wav),
                "--source",
                str(tmp_path / "v.mp4"),
                "--start-ms",
                "0",
                "--duration-ms",
                "1000",
                "--model",
                MODEL,
                "--dry-run",
            ],
            env={},
        )

    assert raised.value.code == probe.EXIT_REFUSED
    captured = capsys.readouterr()
    assert "not allowed with argument" in captured.err
    assert captured.out == ""


def test_one_of_audio_and_source_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        probe.main(["--model", MODEL, "--dry-run"], env={})

    assert raised.value.code == probe.EXIT_REFUSED
    assert "required" in capsys.readouterr().err


@pytest.mark.parametrize(
    "window_args",
    [[], ["--start-ms", "0"], ["--duration-ms", "1000"]],
    ids=["neither", "only-start", "only-duration"],
)
def test_source_needs_both_start_and_duration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], window_args: list[str]
) -> None:
    code = probe.main(
        ["--source", str(tmp_path / "v.mp4"), "--model", MODEL, *window_args, "--dry-run"],
        env={},
    )

    captured = capsys.readouterr()
    assert code == probe.EXIT_REFUSED
    assert "--start-ms" in captured.err
    assert captured.out == ""


def test_window_options_are_refused_with_audio(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wav = _write_wav(tmp_path / "s.wav", seconds=2)

    code = probe.main(
        ["--audio", str(wav), "--model", MODEL, "--start-ms", "0", "--dry-run"], env={}
    )

    captured = capsys.readouterr()
    assert code == probe.EXIT_REFUSED
    assert "only with --source" in captured.err
    assert captured.out == ""
