"""Dev-only live probe of the OpenRouter speech-to-text endpoint.

Sends one short WAV file once to POST /api/v1/audio/transcriptions and records the
real response contract under test-results/live/. This is a paid call: it makes a
single attempt and never retries. A timeout after the request was sent is reported
as ``unknown_outcome`` because the provider may still have processed (and billed) it.

The API key is read only from the OPENROUTER_API_KEY environment variable. It is
never accepted on the command line and never printed or written to disk.

The output directory is created and write-tested before the request is sent. If it cannot be
written, the probe refuses and sends nothing. If the answer arrives but the result files cannot
be written, the redacted summary is printed to stdout and the probe exits 5.

Exit codes: 0 ok, 1 error response or invalid body, 2 refused before sending,
3 unknown_outcome (sent, no usable answer), 4 not_sent (transport failure before send),
5 save_failed (answer received, result files not written).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

ENDPOINT = "https://openrouter.ai/api/v1/audio/transcriptions"
API_KEY_ENV = "OPENROUTER_API_KEY"
TIMEOUT_S = 90.0
MAX_AUDIO_SECONDS = 60.0
MAX_AUDIO_BYTES = 20 * 1024 * 1024
ERROR_MESSAGE_LIMIT = 500
BASE64_PREVIEW_CHARS = 24

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SCRIPT = SCRIPT_DIR / "speech_sample.txt"
DEFAULT_OUT_DIR = SCRIPT_DIR.parents[2] / "test-results" / "live"

EXIT_OK = 0
EXIT_ERROR_RESPONSE = 1
EXIT_REFUSED = 2
EXIT_UNKNOWN_OUTCOME = 3
EXIT_NOT_SENT = 4
EXIT_SAVE_FAILED = 5

_TOKEN_RE = re.compile(r"\d+(?:[.:]\d+)*|[^\W\d_]+(?:'[^\W\d_]+)*")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


class ProbeRefusedError(Exception):
    """A precondition failed; nothing was sent."""


@dataclass(frozen=True)
class AudioInfo:
    path: Path
    size_bytes: int
    duration_s: float
    sample_rate: int
    channels: int
    sha256: str
    data: bytes


# --------------------------------------------------------------------------- audio


def inspect_audio(path: Path) -> AudioInfo:
    """Validate the WAV against the probe limits and return its bytes and facts."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ProbeRefusedError(f"cannot read audio file: {exc.strerror or exc}") from exc
    if not path.is_file():
        raise ProbeRefusedError("audio path is not a file")
    if size > MAX_AUDIO_BYTES:
        raise ProbeRefusedError(
            f"audio is {size} bytes; the probe limit is {MAX_AUDIO_BYTES} bytes (20 MiB)"
        )
    try:
        with wave.open(str(path), "rb") as reader:
            frames = reader.getnframes()
            rate = reader.getframerate()
            channels = reader.getnchannels()
    except (wave.Error, EOFError) as exc:
        raise ProbeRefusedError(f"audio is not a readable PCM WAV file: {exc}") from exc
    if rate <= 0 or frames <= 0:
        raise ProbeRefusedError("audio WAV is empty")
    duration = frames / rate
    if duration > MAX_AUDIO_SECONDS:
        raise ProbeRefusedError(
            f"audio is {duration:.2f} s; the probe limit is {MAX_AUDIO_SECONDS:.0f} s"
        )
    data = path.read_bytes()
    return AudioInfo(
        path=path,
        size_bytes=len(data),
        duration_s=duration,
        sample_rate=rate,
        channels=channels,
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


# ------------------------------------------------------------------------- request


def build_request_body(model: str, audio_b64: str) -> dict[str, Any]:
    return {
        "model": model,
        "input_audio": {"data": audio_b64, "format": "wav"},
        "language": "en",
        "temperature": 0,
        "response_format": "verbose_json",
        "timestamp_granularities": ["segment", "word"],
    }


def describe_request(body: Mapping[str, Any]) -> dict[str, Any]:
    """The request as it would be sent, with the audio payload and the key abbreviated."""
    shown = dict(body)
    audio = dict(body["input_audio"])
    data = str(audio["data"])
    audio["data"] = f"{data[:BASE64_PREVIEW_CHARS]}...<{len(data)} base64 chars total>"
    shown["input_audio"] = audio
    return {
        "method": "POST",
        "url": ENDPOINT,
        "headers": {
            "Authorization": f"Bearer <{API_KEY_ENV}>",
            "Content-Type": "application/json",
        },
        "body": shown,
    }


# --------------------------------------------------------------------- WER / text


def normalize_words(text: str) -> list[str]:
    """Lowercase tokens; punctuation and thousands separators are dropped, % becomes percent."""
    folded = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    folded = folded.replace("%", " percent ")
    folded = _THOUSANDS_RE.sub("", folded)
    return _TOKEN_RE.findall(folded)


def _edit_distance(ref: Sequence[str], hyp: Sequence[str]) -> int:
    previous = list(range(len(hyp) + 1))
    for i, ref_word in enumerate(ref, start=1):
        current = [i]
        for j, hyp_word in enumerate(hyp, start=1):
            cost = 0 if ref_word == hyp_word else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


def word_error_rate(reference: str, hypothesis: str) -> dict[str, Any]:
    ref = normalize_words(reference)
    hyp = normalize_words(hypothesis)
    distance = _edit_distance(ref, hyp)
    return {
        "value": distance / len(ref) if ref else None,
        "ref_words": len(ref),
        "hyp_words": len(hyp),
        "edit_distance": distance,
        "normalization": (
            "NFKC, casefold, thousands separators removed, % -> percent, "
            "punctuation dropped; numbers are compared as written"
        ),
    }


# ------------------------------------------------------------------ summary extract


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _timed(entry: Mapping[str, Any]) -> tuple[float, float] | None:
    start = _number(entry.get("start"))
    end = _number(entry.get("end"))
    if start is None or end is None or end < start:
        return None
    return start, end


def _collect_words(body: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], str]:
    top = body.get("words")
    if isinstance(top, list):
        words: list[Mapping[str, Any]] = [w for w in top if isinstance(w, dict)]
        if words:
            return words, "top_level"
    segments = body.get("segments")
    if isinstance(segments, list):
        nested: list[Mapping[str, Any]] = []
        for segment in segments:
            if isinstance(segment, dict) and isinstance(segment.get("words"), list):
                nested.extend(w for w in segment["words"] if isinstance(w, dict))
        if nested:
            return nested, "in_segments"
    return [], "none"


def _extract_error(body: Mapping[str, Any]) -> dict[str, Any] | None:
    error = body.get("error")
    if error is None:
        return None
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        return {
            "code": code if isinstance(code, int | str) and not isinstance(code, bool) else None,
            "message": message[:ERROR_MESSAGE_LIMIT] if isinstance(message, str) else None,
        }
    if isinstance(error, str):
        return {"code": None, "message": error[:ERROR_MESSAGE_LIMIT]}
    return {"code": None, "message": None}


def extract_summary(body: object, reference_text: str) -> dict[str, Any]:
    """Reduce a provider response body to the facts the probe records.

    Pure function over already-parsed JSON; tolerant of missing and mistyped fields.
    """
    payload: Mapping[str, Any] = body if isinstance(body, dict) else {}
    error = _extract_error(payload)

    text = payload.get("text")
    text_value = text if isinstance(text, str) else ""
    language = payload.get("language")

    segments_raw = payload.get("segments")
    segments: list[Mapping[str, Any]] = []
    if isinstance(segments_raw, list):
        segments = [s for s in segments_raw if isinstance(s, dict)]
    timed_segments = [s for s in segments if _timed(s) is not None]

    words, words_location = _collect_words(payload)
    timed_words = [t for t in (_timed(w) for w in words) if t is not None]

    if timed_words:
        timing_level = "word"
    elif timed_segments:
        timing_level = "segment"
    else:
        timing_level = "none"

    usage = payload.get("usage")
    return {
        "timing_level": timing_level,
        "language": language if isinstance(language, str) else None,
        "duration_s": _number(payload.get("duration")),
        "text_chars": len(text_value),
        "text_empty": not text_value.strip(),
        "segment_count": len(segments),
        "timed_segment_count": len(timed_segments),
        "word_count": len(words),
        "timed_word_count": len(timed_words),
        "words_location": words_location,
        "first_word_start_s": timed_words[0][0] if timed_words else None,
        "last_word_end_s": timed_words[-1][1] if timed_words else None,
        "usage": usage if isinstance(usage, dict) else None,
        "error": error,
        "wer": None if error is not None else word_error_rate(reference_text, text_value),
    }


# ------------------------------------------------------------------------- helpers


def model_slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-.") or "model"


def redact(text: str, key: str) -> str:
    return text.replace(key, "[REDACTED]") if key else text


def _utc_stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _eprint(message: str) -> None:
    print(message, file=sys.stderr)


def _prepare_out_dir(out_dir: Path) -> None:
    """Create the output directory and prove that a file can be written into it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=out_dir, prefix=".write-test-") as handle:
        handle.write(b"write test\n")
        handle.flush()


def _report_save_failure(summary: dict[str, Any], key: str, error: OSError) -> int:
    """The provider answered but the result could not be saved: print the summary and fail."""
    summary["save_error"] = {
        "type": type(error).__name__,
        "message": redact(str(error), key)[:ERROR_MESSAGE_LIMIT],
    }
    _eprint(
        "save_failed: the provider answered, but the result files could not be written. "
        "The redacted summary is printed to stdout. Do not rerun: that is a second paid call."
    )
    print(redact(json.dumps(summary, indent=2, ensure_ascii=False), key))
    return EXIT_SAVE_FAILED


# ---------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stt_probe",
        description=(
            "Send one short WAV to OpenRouter /audio/transcriptions and record the response. "
            f"The key is read from {API_KEY_ENV} only."
        ),
    )
    parser.add_argument("--audio", type=Path, required=True, help="PCM WAV, <= 60 s and <= 20 MiB")
    parser.add_argument("--model", required=True, help="OpenRouter model id")
    parser.add_argument(
        "--script", type=Path, default=DEFAULT_SCRIPT, help="reference text for the WER"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=DEFAULT_OUT_DIR, help="where the result files go"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the request (audio truncated) and exit; no network, no key needed",
    )
    return parser


def _send_once(
    body: Mapping[str, Any], key: str, transport: httpx2.BaseTransport | None
) -> tuple[str, httpx2.Response | None, float, Exception | None]:
    """One attempt. Returns (outcome, response, latency_s, error)."""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    started = time.perf_counter()
    try:
        with httpx2.Client(timeout=httpx2.Timeout(TIMEOUT_S), transport=transport) as client:
            response = client.post(ENDPOINT, headers=headers, json=body)
    except (httpx2.ConnectTimeout, httpx2.ConnectError, httpx2.PoolTimeout) as exc:
        return "not_sent", None, time.perf_counter() - started, exc
    except httpx2.HTTPError as exc:
        # Read/write timeouts and broken connections happen after (part of) the request
        # left the machine, so the provider may have processed and billed it.
        return "unknown_outcome", None, time.perf_counter() - started, exc
    return "received", response, time.perf_counter() - started, None


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx2.BaseTransport | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    environment = os.environ if env is None else env

    try:
        audio = inspect_audio(args.audio)
        reference_text = args.script.read_text(encoding="utf-8")
    except ProbeRefusedError as exc:
        _eprint(f"refused: {exc}")
        return EXIT_REFUSED
    except (OSError, UnicodeDecodeError) as exc:
        _eprint(f"refused: cannot read script text: {exc}")
        return EXIT_REFUSED

    audio_b64 = base64.b64encode(audio.data).decode("ascii")
    body = build_request_body(args.model, audio_b64)

    if args.dry_run:
        print(json.dumps(describe_request(body), indent=2))
        return EXIT_OK

    key = environment.get(API_KEY_ENV, "").strip()
    if not key:
        _eprint(f"refused: {API_KEY_ENV} is not set")
        return EXIT_REFUSED

    # Prove the output directory before spending a paid call.
    try:
        _prepare_out_dir(args.out_dir)
    except OSError as exc:
        _eprint(f"refused: output directory {args.out_dir} cannot be created or written: {exc}")
        return EXIT_REFUSED

    started_at = datetime.now(UTC)
    stamp = _utc_stamp(started_at)
    outcome, response, latency_s, error = _send_once(body, key, transport)

    summary: dict[str, Any] = {
        "model": args.model,
        "started_utc": started_at.isoformat(timespec="seconds"),
        "endpoint": ENDPOINT,
        "audio": {
            "file": audio.path.name,
            "bytes": audio.size_bytes,
            "duration_s": round(audio.duration_s, 3),
            "sample_rate": audio.sample_rate,
            "channels": audio.channels,
            "sha256": audio.sha256,
            "base64_chars": len(audio_b64),
        },
        "latency_s": round(latency_s, 3),
        "status": None,
        "generation_id": None,
        "response_file": None,
    }

    out_dir: Path = args.out_dir
    base =out_dir / f"{model_slug(args.model)}-{stamp}"
    response_path = Path(f"{base}.response.json")
    summary_path = Path(f"{base}.summary.json")
    exit_code: int

    if response is None:
        summary["outcome"] = outcome
        summary["error"] = {
            "type": type(error).__name__,
            "message": redact(str(error), key)[:ERROR_MESSAGE_LIMIT],
        }
        exit_code = EXIT_UNKNOWN_OUTCOME if outcome == "unknown_outcome" else EXIT_NOT_SENT
        if outcome == "unknown_outcome":
            _eprint(
                "unknown_outcome: the request was sent but no answer arrived. The provider may "
                "have processed and billed it. Not retried; check the OpenRouter activity log."
            )
        else:
            _eprint("not_sent: the connection failed before the request was sent.")
    else:
        raw_text = redact(response.content.decode("utf-8", errors="replace"), key)
        try:
            parsed: object = json.loads(raw_text)
            body_is_json = True
        except ValueError:
            parsed = None
            body_is_json = False
        details = extract_summary(parsed, reference_text)
        is_success = response.is_success and isinstance(parsed, dict) and details["error"] is None
        if is_success:
            outcome = "ok"
        elif response.is_success and not isinstance(parsed, dict):
            outcome = "invalid_response"
        else:
            outcome = "error_response"
        summary.update(
            outcome=outcome,
            status=response.status_code,
            generation_id=response.headers.get("X-Generation-Id"),
            body_is_json=body_is_json,
        )
        summary.update(details)
        exit_code = EXIT_OK if outcome == "ok" else EXIT_ERROR_RESPONSE
        try:
            if body_is_json:
                _write_json(response_path, parsed)
            else:
                response_path.write_text(raw_text, encoding="utf-8")
        except OSError as exc:
            return _report_save_failure(summary, key, exc)
        summary["response_file"] = response_path.name

    try:
        _write_json(summary_path, summary)
    except OSError as exc:
        return _report_save_failure(summary, key, exc)
    print(json.dumps(summary, indent=2))
    if summary["response_file"] is not None:
        print(f"response: {response_path}")
    print(f"summary:  {summary_path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
