"""Offline tests for the dev-only STT probe (apps/backend/scripts/stt_probe.py).

Every case uses synthetic dicts or httpx2.MockTransport; nothing here touches the network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import wave
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx2
import pytest

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
        "language",
        "temperature",
        "response_format",
        "timestamp_granularities",
    ]
    assert body["model"] == MODEL
    assert body["input_audio"]["format"] == "wav"
    assert "base64 chars total" in body["input_audio"]["data"]
    assert len(body["input_audio"]["data"]) < 100
    assert body["language"] == "en"
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
