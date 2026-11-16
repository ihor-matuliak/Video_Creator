"""Audio window for speech-to-text (docs/05 sections 1, 2 and 4, ADR 018).

``extract_stt_audio`` renders one global audio stream of the original source, for the upload
window ``[upload_start_ms, upload_end_ms)`` of the canonical timeline, into the app-owned file
``<output_dir>/<task_id>.wav`` through a task-UUID temp file (``.clipforge-<task_id>.part``) that
is verified and only then renamed. A failed, cancelled or invalid run removes its own temp file
and leaves no final file. The original is only read; its size and mtime are compared before the
work and after the render.

Timeline: canonical zero is the first displayed video frame (``probe_source``). The window is
turned into container seconds by ``canonical_window`` and cut with the ADR 018 audio chain
(``atrim``, ``asetpts``, ``aresample=async=1:first_pts=0``), so audio that starts later than
the window start gets leading silence and audio before canonical zero is dropped. The output
timeline therefore begins exactly at ``upload_start_ms``: a provider time ``t`` seconds maps to
``upload_start_ms + round(t * 1000)`` with no further offset (docs/05 section 4).

STT profile (docs/05 section 2): PCM WAV (``pcm_s16le``), mono, 16000 Hz, 16-bit; WAV is the
container the live OpenRouter probe confirmed for both STT models. The downmix is FFmpeg's standard
channel matrix (no channel is phase-inverted). There is no denoise, tempo change, pitch shift or
silence removal. After the audio chain the stream is converted to the profile and padded with
silence, then cut, to exactly ``16 * (end - start)`` samples, so the file lasts as long as the
window even when the source audio ends before the video does. That tail is silence, never a
stretched signal. The file is exactly ``44 + 2 * samples`` bytes (RIFF header, ``fmt `` and
``data`` chunk headers; ``domain.chunking.estimate_payload`` relies on it): ``-fflags +bitexact``
keeps FFmpeg from adding a ``LIST``/``ISFT`` chunk naming its version, and ``-map_metadata -1``
keeps tags of the source out.

Seek: like the export, ``-ss`` before ``-i`` (``max(0, start - 3000)`` ms, relative to the container
start) is used only for mov/mp4/matroska; any other format is decoded from its start. Without it a
window deep inside a long source would read everything before it.

Verification: FFprobe of the temp file (WAV, one pcm_s16le mono 16000 Hz stream, duration equal to
the window within 1 ms) and a full decode whose reported length must match too, with no decoder
message.

FFmpeg runs only through ``run_process`` (owned job, no shell, no console window); the source and
the temp file are single ``file:`` arguments, and the filtergraph holds only numbers made here.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Final

from clipforge.adapters.ffprobe import SourceProbe, probe_source
from clipforge.adapters.media_tools import (
    MediaTools,
    MediaToolsUnavailableError,
    run_short,
    stderr_tail,
)
from clipforge.adapters.process import FfmpegProgress
from clipforge.domain.interval import validate_interval
from clipforge.services.export_engine import (
    OUTPUT_PROBE_TIMEOUT_S,
    PRESEEK_FORMATS,
    PRESEEK_MARGIN_MS,
    DiskFullError,
    ExportCancelledError,
    ExportError,
    ExportStage,
    InvalidAudioStreamError,
    OutputDirUnavailableError,
    OutputInvalidError,
    OutputNotWritableError,
    RenderFailedError,
    SeekMode,
    SourceChangedError,
    SourceState,
    audio_chain,
    canonical_window,
    create_temp,
    ensure_unchanged,
    remove_quietly,
    render_failure,
    run_ffmpeg,
    source_state,
)

SAMPLE_RATE: Final = 16000
CHANNELS: Final = 1
SAMPLES_PER_MS: Final = SAMPLE_RATE // 1000
DURATION_TOLERANCE_MS: Final = 1

SttAudioProgressCallback = Callable[[FfmpegProgress], None]

_SAMPLE_FORMAT: Final = "s16"
_CODEC: Final = "pcm_s16le"
_FORMAT: Final = "wav"
_MICROSECONDS_PER_MS: Final = 1000


class NoAudioStreamError(ExportError):
    """The source has no audio stream, so there is nothing to transcribe."""

    code = "SOURCE_NO_AUDIO"


@dataclass(frozen=True, slots=True)
class SttAudioRequest:
    task_id: str
    """Canonical UUID of the task: part of the temp name, and the final file is ``<id>.wav``."""
    source: Path
    """Absolute path of the original file."""
    output_dir: Path
    """Absolute path of an existing app-owned directory."""
    audio_stream_index: int
    """Global source stream index of the audio stream to extract."""
    upload_start_ms: int
    upload_end_ms: int
    """The window ``[upload_start_ms, upload_end_ms)`` in canonical source milliseconds."""


@dataclass(frozen=True, slots=True)
class SttAudioResult:
    """Data of a verified audio file."""

    path: Path
    size_bytes: int
    upload_start_ms: int
    upload_end_ms: int
    seek_mode: SeekMode
    ffmpeg_version: str
    ffprobe_version: str


@dataclass(frozen=True, slots=True)
class _Plan:
    request: SttAudioRequest
    window_ms: int
    sample_count: int
    start_text: str
    end_text: str
    seek_text: str | None
    """``-ss`` value, or ``None`` when the source is decoded from its start."""


def extract_stt_audio(
    tools: MediaTools,
    request: SttAudioRequest,
    *,
    cancel: threading.Event | None = None,
    on_progress: SttAudioProgressCallback | None = None,
) -> SttAudioResult:
    """Render, verify and publish one audio window. Blocking; run it in a worker thread.

    ``on_progress`` receives the blocks of the render only (``out_time_us`` counts from the window
    start). Raises ``ExportError`` subclasses (``code`` tells what failed; ``NoAudioStreamError``
    and ``InvalidAudioStreamError`` for the audio choice), the probe errors of
    ``clipforge.adapters.ffprobe``, ``IntervalError`` and ``MediaToolsUnavailableError``.
    ``ValueError`` means a malformed request (relative paths, a task id that is not a canonical
    UUID, or one whose temp file already exists).
    """
    _validate_request(request)
    source, output_dir = request.source, request.output_dir
    if not output_dir.is_dir():
        raise OutputDirUnavailableError("Тека аудіо недоступна")

    before = source_state(source)
    probe = probe_source(tools.ffprobe, source)
    interval = validate_interval(request.upload_start_ms, request.upload_end_ms, probe.duration_ms)
    _check_audio_stream(probe, request.audio_stream_index)
    plan = _make_plan(request, probe, interval.start_ms, interval.end_ms)
    _ensure_unchanged(source, before)
    _raise_if_cancelled(cancel)

    temp = create_temp(output_dir, request.task_id)
    final = output_dir / f"{request.task_id.lower()}.wav"
    published = False
    try:
        _render(tools, plan, temp, cancel, on_progress)
        _ensure_unchanged(source, before)
        _verify(tools, plan, temp, cancel)
        _raise_if_cancelled(cancel)
        size = temp.stat().st_size
        _publish(temp, final)
        published = True
    finally:
        if not published:
            remove_quietly(temp)
    return SttAudioResult(
        path=final,
        size_bytes=size,
        upload_start_ms=interval.start_ms,
        upload_end_ms=interval.end_ms,
        seek_mode="preseek" if plan.seek_text is not None else "full_decode",
        ffmpeg_version=tools.ffmpeg_version,
        ffprobe_version=tools.ffprobe_version,
    )


# --- request and plan -----------------------------------------------------------------


def _validate_request(request: SttAudioRequest) -> None:
    if not request.source.is_absolute():
        raise ValueError("source must be an absolute path")
    if not request.output_dir.is_absolute():
        raise ValueError("output_dir must be an absolute path")
    try:
        parsed = uuid.UUID(request.task_id)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("task_id must be a UUID") from None
    if str(parsed) != request.task_id.lower():
        raise ValueError("task_id must be a canonical UUID")


def _check_audio_stream(probe: SourceProbe, index: int) -> None:
    if not probe.audio_streams:
        raise NoAudioStreamError("У файлі немає аудіодоріжки")
    if index not in {stream.index for stream in probe.audio_streams}:
        raise InvalidAudioStreamError("Вибрана аудіодоріжка відсутня у файлі")


def _make_plan(request: SttAudioRequest, probe: SourceProbe, start_ms: int, end_ms: int) -> _Plan:
    start_text, end_text = canonical_window(probe, start_ms, end_ms)
    return _Plan(
        request=request,
        window_ms=end_ms - start_ms,
        sample_count=(end_ms - start_ms) * SAMPLES_PER_MS,
        start_text=start_text,
        end_text=end_text,
        seek_text=_seek_text(start_ms) if _preseek_allowed(probe.format_name) else None,
    )


def _preseek_allowed(format_name: str) -> bool:
    return any(part.strip() in PRESEEK_FORMATS for part in format_name.split(","))


def _seek_text(start_ms: int) -> str:
    """Pre-seek position, relative to the container start (ADR 018), as exact decimal seconds."""
    seek_ms = max(0, start_ms - PRESEEK_MARGIN_MS)
    return f"{seek_ms // 1000}.{seek_ms % 1000:03d}"


def _ensure_unchanged(source: Path, before: SourceState) -> None:
    try:
        ensure_unchanged(source, before)
    except SourceChangedError:
        raise SourceChangedError("Файл джерела змінився під час підготовки аудіо") from None


def _raise_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ExportCancelledError("Підготовку аудіо скасовано")


def _publish(temp: Path, final: Path) -> None:
    try:
        os.replace(temp, final)
    except OSError:
        raise OutputNotWritableError("Не вдалося зберегти аудіо для розпізнавання") from None


# --- render ---------------------------------------------------------------------------


def _render(
    tools: MediaTools,
    plan: _Plan,
    temp: Path,
    cancel: threading.Event | None,
    on_progress: SttAudioProgressCallback | None,
) -> None:
    def forward(_stage: ExportStage, progress: FfmpegProgress) -> None:
        if on_progress is not None:
            on_progress(progress)

    result, _frames = run_ffmpeg(_render_argv(tools, plan, temp), "render", cancel, forward)
    if result.cancelled:
        raise ExportCancelledError("Підготовку аудіо скасовано")
    if result.exit_code != 0:
        failure = render_failure(result, temp.parent)
        if isinstance(failure, DiskFullError):
            raise DiskFullError(
                "Недостатньо місця на диску для аудіо", detail=failure.detail
            ) from None
        raise RenderFailedError("FFmpeg не зміг підготувати аудіо", detail=failure.detail)


def _filter_graph(plan: _Plan) -> str:
    """The ADR 018 audio chain, then the STT profile and the exact length.

    ``aformat`` makes FFmpeg insert the standard resampler and channel mixer; ``apad`` fills a
    source that ends early and ``atrim=end_sample`` removes a sample of resampler rounding, both
    at the output rate where a millisecond is a whole number of samples.
    """
    count = plan.sample_count
    return (
        audio_chain(plan.request.audio_stream_index, 0, plan.start_text, plan.end_text)
        + f";[a0]aformat=sample_fmts={_SAMPLE_FORMAT}:sample_rates={SAMPLE_RATE}"
        f":channel_layouts=mono,apad=whole_len={count},atrim=end_sample={count}[a]"
    )


def _render_argv(tools: MediaTools, plan: _Plan, temp: Path) -> list[str]:
    argv = [
        str(tools.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-y",
    ]
    if plan.seek_text is not None:
        argv += ["-ss", plan.seek_text]
    argv += [
        "-copyts",
        "-i",
        f"file:{plan.request.source}",
        "-filter_complex",
        _filter_graph(plan),
        "-map",
        "[a]",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-fflags",
        "+bitexact",
        "-c:a",
        _CODEC,
        "-f",
        _FORMAT,
        f"file:{temp}",
    ]
    return argv


# --- verification ---------------------------------------------------------------------


def _verify(tools: MediaTools, plan: _Plan, temp: Path, cancel: threading.Event | None) -> None:
    _raise_if_cancelled(cancel)
    info = _ffprobe_json(tools, temp)
    streams = [_obj(item) for item in _list(info.get("streams"))]
    if len(streams) != 1:
        raise OutputInvalidError("Аудіофайл має неочікуваний набір доріжок")
    stream = streams[0]
    actual = (
        stream.get("codec_type"),
        stream.get("codec_name"),
        _int(stream.get("channels")),
        _int(stream.get("sample_rate")),
        stream.get("sample_fmt"),
    )
    container = _obj(info.get("format"))
    if actual != ("audio", _CODEC, CHANNELS, SAMPLE_RATE, _SAMPLE_FORMAT) or (
        container.get("format_name") != _FORMAT
    ):
        raise OutputInvalidError("Аудіо для розпізнавання має неочікуваний формат")
    duration = _fraction(stream.get("duration"))
    if duration is None:
        duration = _fraction(container.get("duration"))
    if duration is None or _off_by_more_than_tolerance(duration * 1000, plan.window_ms):
        raise OutputInvalidError("Тривалість аудіо не відповідає вікну розпізнавання")
    _decode_check(tools, plan, temp, cancel)


def _decode_check(
    tools: MediaTools, plan: _Plan, temp: Path, cancel: threading.Event | None
) -> None:
    """Decode the whole file: no decoder message, and the decoded length is the window."""
    argv = [
        str(tools.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-i",
        f"file:{temp}",
        "-map",
        "0",
        "-f",
        "null",
        "-",
    ]
    decoded_us: list[int | None] = [None]

    def watch(_stage: ExportStage, progress: FfmpegProgress) -> None:
        if progress.out_time_us is not None:
            decoded_us[0] = progress.out_time_us

    result, _frames = run_ffmpeg(argv, "verify", cancel, watch)
    if result.cancelled:
        raise ExportCancelledError("Підготовку аудіо скасовано")
    tail = result.stderr_tail[-2000:]
    decoded = decoded_us[0]
    if (
        result.exit_code != 0
        or tail.strip()
        or decoded is None
        or _off_by_more_than_tolerance(Fraction(decoded, _MICROSECONDS_PER_MS), plan.window_ms)
    ):
        raise OutputInvalidError("Аудіо не пройшло перевірку декодуванням", detail=tail)


def _off_by_more_than_tolerance(actual_ms: Fraction, expected_ms: int) -> bool:
    return abs(actual_ms - expected_ms) > DURATION_TOLERANCE_MS


def _ffprobe_json(tools: MediaTools, path: Path) -> dict[str, object]:
    argv = [
        str(tools.ffprobe),
        "-v",
        "error",
        "-hide_banner",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        "-i",
        f"file:{path}",
    ]
    try:
        completed = run_short(argv, timeout_s=OUTPUT_PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise OutputInvalidError("Перевірка аудіо перевищила ліміт часу") from None
    except OSError:
        raise MediaToolsUnavailableError("Не вдалося запустити ffprobe") from None
    detail = stderr_tail(completed.stderr)
    if completed.returncode != 0:
        raise OutputInvalidError("Створене аудіо не вдалося прочитати", detail=detail)
    try:
        document: object = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        document = None
    if not isinstance(document, dict):
        raise OutputInvalidError("FFprobe повернув некоректну відповідь", detail=detail)
    return _obj(document)


def _obj(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _fraction(value: object) -> Fraction | None:
    """Exact value of an ffprobe number: int or ``"5.000000"``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | str):
        try:
            return Fraction(value)
        except (ValueError, ZeroDivisionError):
            return None
    return None
