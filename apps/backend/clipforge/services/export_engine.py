"""Exact-cut export of one clip (ADR 018, docs/05 sections 2, 3 and 8, FR-036..FR-041).

``export_clip`` renders ``[start_ms, end_ms)`` of the original source with the ADR 018
recipe into a task-UUID temp file inside the output directory, verifies that file,
and only then renames it to the final name. The rename never replaces anything: a
taken name moves on to the next collision suffix. A failed, cancelled or invalid run
removes its own temp file and leaves no final file.

Times: ``S`` and ``E`` are seconds from the container zero, ``origin + ms / 1000``,
computed exactly and printed by this module as decimals; the filtergraph is made of
these numbers only. ``-ss`` is relative to the container start time, so the pre-seek
position is ``max(0, start_ms - 3000)`` from the canonical zero (checked on sources
whose first frame is at 10 s). Pre-seek is used only for mov/mp4/matroska; any other
format is decoded from its start. When the verification of a pre-seek render finds no
video or a first frame later than the tolerance, the clip is rendered once more
without pre-seek and the manifest says ``full_decode``. A render that fails before the
first frame, or writes none, is checked with the same video chain into a null output:
zero frames in the ``-progress`` count (after the full-decode repeat) means
``NO_VIDEO_FRAMES``, not ``FFMPEG_FAILED`` or ``OUTPUT_INVALID``.

Verification (FR-041): ffprobe of the temp file (streams, size, codec, pixel format,
frame timing against the requested interval) and a full decode of every stream of the
file (``-map 0``, so all audio tracks of audio_mode "all" are decoded). Observed
first/last video PTS are reported in canonical source milliseconds, rounded down, so
``start_ms <= first <= last < end_ms`` holds for the integers as well.

Source identity (FR-038, docs/05 section 8): size and mtime are read before the work
and compared after every render and right before the rename; any change aborts with
``SOURCE_CHANGED``. A quick fingerprint can be required before the start.

FFmpeg runs only through ``run_process`` (owned job, no shell, no console window).

``canonical_window``, ``video_chain``, ``audio_chain`` and the temp/identity/process helpers
(``create_temp``, ``remove_quietly``, ``source_state``, ``ensure_unchanged``, ``check_fingerprint``,
``raise_if_cancelled``, ``run_ffmpeg``, ``render_failure``) are public because the preview proxy
(``preview_engine``) builds on the same recipe and plumbing instead of repeating them.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import ClassVar, Final, Literal

from clipforge.adapters.ffprobe import (
    HDR_UNSUPPORTED,
    SourceProbe,
    SourceUnavailableError,
    probe_source,
)
from clipforge.adapters.fingerprint import quick_fingerprint
from clipforge.adapters.media_tools import (
    MediaTools,
    MediaToolsUnavailableError,
    run_short,
    stderr_tail,
)
from clipforge.adapters.process import FfmpegProgress, ProcessResult, run_process
from clipforge.domain.interval import validate_interval
from clipforge.domain.naming import export_basename, first_free_name

ExportStage = Literal["render", "verify"]
SeekMode = Literal["preseek", "full_decode"]
ExportProgressCallback = Callable[[ExportStage, FfmpegProgress], None]

PRESEEK_MARGIN_MS: Final = 3000
# Container families whose index makes a pre-seek safe (ADR 018); names as ffprobe prints them.
PRESEEK_FORMATS: Final = frozenset({"mov", "mp4", "matroska"})
BOUNDARY_TOLERANCE_MS: Final = 50
AUDIO_FRAME_SLACK_MS: Final = 50
OUTPUT_PROBE_TIMEOUT_S: Final = 120.0
TEMP_PREFIX: Final = ".clipforge-"
TEMP_SUFFIX: Final = ".part"

_FRAME_SLACK: Final = Fraction(1, 1000)
_NO_SPACE_TEXT: Final = "No space left on device"
_DISK_FULL_FREE_BYTES: Final = 1 << 20
_REMOVE_ATTEMPTS: Final = 10
_REMOVE_PAUSE_S: Final = 0.1
_AUDIO_BITRATE: Final = "192k"


class ExportError(Exception):
    """Base of export errors. ``message`` is Ukrainian and contains no paths.

    ``detail`` is a bounded FFmpeg/FFprobe stderr tail for logs only.
    """

    code: ClassVar[str] = "EXPORT_FAILED"

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class ExportCancelledError(ExportError):
    code = "EXPORT_CANCELLED"


class SourceChangedError(ExportError):
    """The source differs from what the export started with."""

    code = "SOURCE_CHANGED"


class HdrUnsupportedError(ExportError):
    code = HDR_UNSUPPORTED


class InvalidAudioStreamError(ExportError):
    """The requested audio stream is not an audio stream of the source."""

    code = "INVALID_AUDIO_STREAM"


class OutputDirUnavailableError(ExportError):
    code = "OUTPUT_DIR_UNAVAILABLE"


class OutputNotWritableError(ExportError):
    code = "OUTPUT_NOT_WRITABLE"


class DiskFullError(ExportError):
    code = "DISK_FULL"


class RenderFailedError(ExportError):
    """FFmpeg exited with an error."""

    code = "FFMPEG_FAILED"


class NoVideoFramesError(ExportError):
    """No video frame of the source has its presentation time inside the interval."""

    code = "NO_VIDEO_FRAMES"


class OutputInvalidError(ExportError):
    """The rendered file failed verification; it was removed."""

    code = "OUTPUT_INVALID"


@dataclass(frozen=True, slots=True)
class ExportRequest:
    task_id: str
    """UUID of the export task; part of the temp file name."""
    source: Path
    """Absolute path of the original file."""
    output_dir: Path
    """Absolute path of an existing directory."""
    start_ms: int
    end_ms: int
    clip_id: str
    revision: int
    audio_stream_index: int | None
    """Global source stream index of the audio to export; ``None`` for a video-only clip."""
    expected_fingerprint: str | None = None
    """``quick_fingerprint`` of the registered source, checked before the render."""
    audio_stream_indexes: tuple[int, ...] = ()
    """Global source stream indexes of every audio stream to export (audio_mode ``all``),
    mapped in this order, each through its own chain so its start offset survives.
    Mutually exclusive with ``audio_stream_index``; no duplicates."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Manifest data of a verified export."""

    path: Path
    seek_mode: SeekMode
    ffmpeg_version: str
    ffprobe_version: str
    profile: dict[str, str | int]
    first_video_pts_ms: int
    """PTS of the first video frame in canonical source time, rounded down."""
    last_video_pts_ms: int
    """PTS of the last video frame in canonical source time, rounded down."""
    observed_duration_ms: int
    """End of the last video frame minus the clip start, in the output file."""
    video_frame_count: int


@dataclass(frozen=True, slots=True)
class _Plan:
    ffmpeg: Path
    source: Path
    video_index: int
    audio_indexes: tuple[int, ...]
    start_ms: int
    end_ms: int
    start_text: str
    end_text: str
    seek_text: str
    width: int
    height: int
    preseek_allowed: bool

    @property
    def duration(self) -> Fraction:
        return Fraction(self.end_ms - self.start_ms, 1000)


@dataclass(frozen=True, slots=True)
class SourceState:
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _Observed:
    first_out: Fraction
    last_out: Fraction
    video_end_out: Fraction
    frame_count: int


class _RetryWithoutPreseek(Exception):
    """A pre-seek render produced no usable first frame (ADR 018)."""


def export_clip(
    tools: MediaTools,
    request: ExportRequest,
    *,
    cancel: threading.Event | None = None,
    on_progress: ExportProgressCallback | None = None,
) -> ExportResult:
    """Render, verify and finalize one clip. Blocking; run it in a worker thread.

    Raises ``ExportError`` subclasses, the probe errors of ``clipforge.adapters.ffprobe``,
    ``IntervalError`` and ``MediaToolsUnavailableError``. ``ValueError`` means a malformed
    request (relative paths, task id that is not a UUID, invalid clip ID or revision).
    """
    _validate_request(request)
    source, output_dir = request.source, request.output_dir
    if not output_dir.is_dir():
        raise OutputDirUnavailableError("Тека експорту недоступна")
    basename = export_basename(
        source_stem=source.stem,
        clip_id=request.clip_id,
        revision=request.revision,
        start_ms=request.start_ms,
        end_ms=request.end_ms,
    )

    before = source_state(source)
    if request.expected_fingerprint is not None:
        check_fingerprint(source, request.expected_fingerprint)
    probe = probe_source(tools.ffprobe, source)
    if probe.unsupported_reason == HDR_UNSUPPORTED:
        raise HdrUnsupportedError("HDR-відео поки не підтримується для експорту")
    interval = validate_interval(request.start_ms, request.end_ms, probe.duration_ms)
    _check_audio_streams(probe, _requested_audio(request))
    plan = _make_plan(tools, request, probe, interval.start_ms, interval.end_ms)
    ensure_unchanged(source, before)
    raise_if_cancelled(cancel)

    temp = create_temp(output_dir, request.task_id)
    finalized = False
    try:
        modes: tuple[SeekMode, ...] = (
            ("preseek", "full_decode") if plan.preseek_allowed else ("full_decode",)
        )
        for mode in modes:
            try:
                observed = _render_and_verify(tools, plan, temp, mode, before, cancel, on_progress)
            except _RetryWithoutPreseek:
                continue
            seek_mode = mode
            break
        else:  # pragma: no cover - the last mode never asks for a retry
            raise OutputInvalidError("Не вдалося отримати коректний кліп")
        ensure_unchanged(source, before)
        raise_if_cancelled(cancel)
        final = _finalize(temp, output_dir, basename)
        finalized = True
    finally:
        if not finalized:
            remove_quietly(temp)

    start = Fraction(request.start_ms, 1000)
    return ExportResult(
        path=final,
        seek_mode=seek_mode,
        ffmpeg_version=tools.ffmpeg_version,
        ffprobe_version=tools.ffprobe_version,
        profile=_profile(bool(plan.audio_indexes)),
        first_video_pts_ms=math.floor((start + observed.first_out) * 1000),
        last_video_pts_ms=math.floor((start + observed.last_out) * 1000),
        observed_duration_ms=math.floor(observed.video_end_out * 1000 + Fraction(1, 2)),
        video_frame_count=observed.frame_count,
    )


# --- request and plan -----------------------------------------------------------------


def _validate_request(request: ExportRequest) -> None:
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
    if request.audio_stream_index is not None and request.audio_stream_indexes:
        raise ValueError("audio_stream_index and audio_stream_indexes are mutually exclusive")
    if len(set(request.audio_stream_indexes)) != len(request.audio_stream_indexes):
        raise ValueError("audio_stream_indexes must not contain duplicates")


def _requested_audio(request: ExportRequest) -> tuple[int, ...]:
    """Audio streams to export, in output order; empty for a video-only clip."""
    if request.audio_stream_indexes:
        return request.audio_stream_indexes
    if request.audio_stream_index is not None:
        return (request.audio_stream_index,)
    return ()


def _check_audio_streams(probe: SourceProbe, indexes: tuple[int, ...]) -> None:
    available = {stream.index for stream in probe.audio_streams}
    if any(index not in available for index in indexes):
        raise InvalidAudioStreamError("Вибрана аудіодоріжка відсутня у файлі")


def _make_plan(
    tools: MediaTools, request: ExportRequest, probe: SourceProbe, start_ms: int, end_ms: int
) -> _Plan:
    width, height = (
        (probe.height, probe.width) if probe.rotation in (90, 270) else (probe.width, probe.height)
    )
    start_text, end_text = canonical_window(probe, start_ms, end_ms)
    return _Plan(
        ffmpeg=tools.ffmpeg,
        source=request.source,
        video_index=probe.video_stream_index,
        audio_indexes=_requested_audio(request),
        start_ms=start_ms,
        end_ms=end_ms,
        start_text=start_text,
        end_text=end_text,
        seek_text=_decimal(Fraction(max(0, start_ms - PRESEEK_MARGIN_MS), 1000), 3),
        width=width + width % 2,
        height=height + height % 2,
        preseek_allowed=_preseek_allowed(probe.format_name),
    )


def _preseek_allowed(format_name: str) -> bool:
    return any(part.strip() in PRESEEK_FORMATS for part in format_name.split(","))


def _decimal(value: Fraction, digits: int) -> str:
    """Exact decimal text of ``value`` rounded half up to ``digits`` places."""
    scale = 10**digits
    scaled = math.floor(value * scale + Fraction(1, 2))
    sign = "-" if scaled < 0 else ""
    whole, fraction = divmod(abs(scaled), scale)
    return f"{sign}{whole}.{fraction:0{digits}d}"


def _profile(with_audio: bool) -> dict[str, str | int]:
    profile: dict[str, str | int] = {
        "recipe": "ADR 018",
        "container": "mp4",
        "video_codec": "libx264",
        "crf": 18,
        "preset": "medium",
        "pix_fmt": "yuv420p",
        "b_frames": 0,
    }
    if with_audio:
        profile["audio_codec"] = "aac"
        profile["audio_bitrate"] = _AUDIO_BITRATE
    else:
        profile["audio_codec"] = "none"
    return profile


# --- source identity --------------------------------------------------------------------


def source_state(source: Path) -> SourceState:
    try:
        stat = source.stat()
    except OSError:
        raise SourceUnavailableError("Файл джерела недоступний") from None
    return SourceState(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def ensure_unchanged(source: Path, before: SourceState) -> None:
    try:
        now = source_state(source)
    except SourceUnavailableError:
        raise SourceChangedError("Файл джерела змінився під час експорту") from None
    if now != before:
        raise SourceChangedError("Файл джерела змінився під час експорту")


def check_fingerprint(source: Path, expected: str) -> None:
    try:
        actual = quick_fingerprint(source)
    except OSError:
        raise SourceUnavailableError("Файл джерела недоступний") from None
    if actual != expected:
        raise SourceChangedError("Файл джерела відрізняється від зареєстрованого")


def raise_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ExportCancelledError("Експорт скасовано")


# --- temp and final files ---------------------------------------------------------------


def create_temp(output_dir: Path, task_id: str) -> Path:
    """Create the empty temp file exclusively; its name is unique to the task."""
    temp = output_dir / f"{TEMP_PREFIX}{task_id.lower()}{TEMP_SUFFIX}"
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
    except FileExistsError:
        raise ValueError("task_id was already used in this output directory") from None
    except OSError:
        raise OutputNotWritableError("Немає доступу на запис у теку експорту") from None
    os.close(descriptor)
    return temp


def remove_quietly(temp: Path) -> None:
    """Delete the task's own temp file; the FFmpeg processes have already exited."""
    for _ in range(_REMOVE_ATTEMPTS):
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            time.sleep(_REMOVE_PAUSE_S)  # a scanner may hold the file for a moment
        else:
            return


def _finalize(temp: Path, output_dir: Path, basename: str) -> Path:
    """Rename the temp file to the first free name; ``os.rename`` never replaces."""
    taken = set(os.listdir(output_dir))
    while True:
        name = first_free_name(basename, taken)
        target = output_dir / name
        try:
            os.rename(temp, target)
        except FileExistsError:
            taken.add(name)  # created by someone else since the listing
            continue
        except OSError:
            raise OutputNotWritableError("Не вдалося створити файл у теці експорту") from None
        return target


# --- render ---------------------------------------------------------------------------


def _render_and_verify(
    tools: MediaTools,
    plan: _Plan,
    temp: Path,
    mode: SeekMode,
    before: SourceState,
    cancel: threading.Event | None,
    on_progress: ExportProgressCallback | None,
) -> _Observed:
    argv = _render_argv(plan, temp, mode)
    result, frames = run_ffmpeg(argv, "render", cancel, on_progress)
    if result.cancelled:
        raise ExportCancelledError("Експорт скасовано")
    if result.exit_code != 0:
        if not frames:
            _raise_if_no_frames(plan, mode, cancel)
        raise render_failure(result, temp.parent)
    ensure_unchanged(plan.source, before)
    return _verify(tools, plan, temp, mode, cancel, on_progress)


def canonical_window(probe: SourceProbe, start_ms: int, end_ms: int) -> tuple[str, str]:
    """``S`` and ``E`` of the ADR 018 filters: the container seconds of ``start_ms`` and ``end_ms``
    (canonical origin plus the time), as exact decimal text. Shared with the preview proxy."""
    origin = probe.origin_seconds
    return (
        _decimal(origin + Fraction(start_ms, 1000), 6),
        _decimal(origin + Fraction(end_ms, 1000), 6),
    )


def video_chain(
    video_index: int, start_text: str, end_text: str, *, scale_height: int | None = None
) -> str:
    """The ADR 018 video chain ``[0:V]trim,setpts[,scale],pad[v]``. ``scale_height`` (even) scales
    to that height keeping the aspect, only for the preview proxy; the export never scales."""
    scale = f"scale=w=-2:h={scale_height}," if scale_height is not None else ""
    return (
        f"[0:{video_index}]trim=start={start_text}:end={end_text},"
        f"setpts=PTS-{start_text}/TB,"
        f"{scale}"
        "pad=width=ceil(iw/2)*2:height=ceil(ih/2)*2[v]"
    )


def audio_chain(audio_index: int, position: int, start_text: str, end_text: str) -> str:
    """The ADR 018 audio chain ``[0:A]atrim,asetpts,aresample[a<position>]``: ``first_pts=0`` pads
    the stream's own late start with silence."""
    return (
        f"[0:{audio_index}]atrim=start={start_text}:end={end_text},"
        f"asetpts=PTS-{start_text}/TB,aresample=async=1:first_pts=0[a{position}]"
    )


def _video_chain(plan: _Plan) -> str:
    return video_chain(plan.video_index, plan.start_text, plan.end_text)


def _raise_if_no_frames(plan: _Plan, mode: SeekMode, cancel: threading.Event | None) -> None:
    """Turn "the video chain yields no frame" into the right outcome; otherwise return."""
    if not _interval_has_no_frames(plan, mode, cancel):
        return
    # A pre-seek that lands past the interval is not proof: decode from the start.
    if mode == "preseek":
        raise _RetryWithoutPreseek
    raise NoVideoFramesError("У вибраному інтервалі немає жодного кадру відео")


def _interval_has_no_frames(plan: _Plan, mode: SeekMode, cancel: threading.Event | None) -> bool:
    """True when the render's own video chain, run to a null output, yields zero frames.

    The answer is the frame count of ``-progress``, never FFmpeg's message text. Anything
    else (a failed run, no count reported) is "not proven", so the render error stays.
    """
    argv = [
        str(plan.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
    ]
    if mode == "preseek":
        argv += ["-ss", plan.seek_text]
    argv += [
        "-copyts",
        "-i",
        f"file:{plan.source}",
        "-filter_complex",
        _video_chain(plan),
        "-map",
        "[v]",
        "-fps_mode",
        "passthrough",
        "-f",
        "null",
        "-",
    ]
    result, frames = run_ffmpeg(argv, "render", cancel, None)
    if result.cancelled:
        raise ExportCancelledError("Експорт скасовано")
    return result.exit_code == 0 and frames == 0


def _render_argv(plan: _Plan, temp: Path, mode: SeekMode) -> list[str]:
    graph = _video_chain(plan)
    for position, audio_index in enumerate(plan.audio_indexes):
        # One chain per stream: first_pts=0 pads each stream's own late start with silence.
        graph += ";" + audio_chain(audio_index, position, plan.start_text, plan.end_text)
    argv = [
        str(plan.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-y",
    ]
    if mode == "preseek":
        argv += ["-ss", plan.seek_text]
    argv += ["-copyts", "-i", f"file:{plan.source}", "-filter_complex", graph, "-map", "[v]"]
    for position in range(len(plan.audio_indexes)):
        argv += ["-map", f"[a{position}]"]
    argv += [
        "-map_chapters",
        "-1",
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-preset",
        "medium",
        "-pix_fmt",
        "yuv420p",
        "-bf",
        "0",
        "-fps_mode",
        "passthrough",
        "-enc_time_base:v",
        "demux",
    ]
    if plan.audio_indexes:
        argv += ["-c:a", "aac", "-b:a", _AUDIO_BITRATE]
    argv += ["-f", "mp4", f"file:{temp}"]
    return argv


def run_ffmpeg(
    argv: list[str],
    stage: ExportStage,
    cancel: threading.Event | None,
    on_progress: ExportProgressCallback | None,
) -> tuple[ProcessResult, int | None]:
    """Run FFmpeg as an owned process; also returns the last reported frame count."""
    last_frame: list[int | None] = [None]
    callback_errors: list[Exception] = []

    def forward(progress: FfmpegProgress) -> None:
        if progress.frame is not None:
            last_frame[0] = progress.frame
        if on_progress is None:
            return
        try:
            on_progress(stage, progress)
        except Exception as exc:
            callback_errors.append(exc)
            raise

    try:
        result = run_process(argv, on_progress=forward, cancel=cancel)
    except OSError:
        if callback_errors:
            raise
        raise MediaToolsUnavailableError("Не вдалося запустити ffmpeg") from None
    return result, last_frame[0]


def render_failure(result: ProcessResult, output_dir: Path) -> ExportError:
    tail = result.stderr_tail[-2000:]
    if _NO_SPACE_TEXT in tail or _free_bytes(output_dir) < _DISK_FULL_FREE_BYTES:
        return DiskFullError("Недостатньо місця на диску для експорту", detail=tail)
    return RenderFailedError("FFmpeg не зміг створити кліп", detail=tail)


def _free_bytes(directory: Path) -> int:
    try:
        return shutil.disk_usage(directory).free
    except OSError:
        return _DISK_FULL_FREE_BYTES  # unknown: do not claim a full disk


# --- verification ---------------------------------------------------------------------


def _verify(
    tools: MediaTools,
    plan: _Plan,
    temp: Path,
    mode: SeekMode,
    cancel: threading.Event | None,
    on_progress: ExportProgressCallback | None,
) -> _Observed:
    raise_if_cancelled(cancel)
    info = _ffprobe_json(tools, ["-show_format", "-show_streams"], temp)
    streams = [_obj(item) for item in _list(info.get("streams"))]
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    packets = _video_packets(tools, temp, videos[0]) if videos else None
    if packets is None:
        if mode == "preseek":
            raise _RetryWithoutPreseek
        _raise_if_no_frames(plan, mode, cancel)  # a video-only render may exit 0 without frames
        raise OutputInvalidError("У кліпі немає жодного відеокадру")

    _check_streams(plan, streams, videos)
    observed = _check_timing(plan, packets, mode, streams, _obj(info.get("format")))
    _decode_check(tools, temp, observed.frame_count, cancel, on_progress)
    return observed


def _check_streams(
    plan: _Plan, streams: list[dict[str, object]], videos: list[dict[str, object]]
) -> None:
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(streams) != len(videos) + len(audios) or len(videos) != 1:
        raise OutputInvalidError("Кліп має неочікуваний набір доріжок")
    video = videos[0]
    if (
        video.get("codec_name") != "h264"
        or video.get("pix_fmt") != "yuv420p"
        or (_int(video.get("width")), _int(video.get("height"))) != (plan.width, plan.height)
    ):
        raise OutputInvalidError("Відео кліпу має неочікуваний формат або розмір")
    if len(audios) != len(plan.audio_indexes) or any(
        audio.get("codec_name") != "aac" for audio in audios
    ):
        raise OutputInvalidError("Аудіо кліпу не відповідає вибраній доріжці")


def _check_timing(
    plan: _Plan,
    packets: _Packets,
    mode: SeekMode,
    streams: list[dict[str, object]],
    container: dict[str, object],
) -> _Observed:
    pts = sorted(packets.pts)
    first, last = pts[0], pts[-1]
    first_gap = pts[1] - pts[0] if len(pts) > 1 else Fraction(0)
    last_gap = pts[-1] - pts[-2] if len(pts) > 1 else Fraction(0)
    video_end = last + packets.last_duration
    boundary = Fraction(BOUNDARY_TOLERANCE_MS, 1000)
    duration = plan.duration

    if first < -_FRAME_SLACK or last >= duration + _FRAME_SLACK:
        raise OutputInvalidError("Кадри кліпу виходять за межі вибраного інтервалу")
    if first > max(boundary, first_gap):
        if mode == "preseek":
            raise _RetryWithoutPreseek
        raise OutputInvalidError("Перший кадр кліпу починається пізніше за вибраний початок")
    tolerance = max(boundary, first_gap, last_gap, packets.last_duration)
    if abs(video_end - duration) > tolerance:
        raise OutputInvalidError("Тривалість кліпу не відповідає вибраному інтервалу")

    if plan.audio_indexes:
        # Only an upper bound: the source audio may legitimately end before the video does.
        slack = tolerance + Fraction(AUDIO_FRAME_SLACK_MS, 1000)
        for audio_duration in _audio_durations(streams, container):
            if audio_duration is None or audio_duration > duration + slack:
                raise OutputInvalidError("Тривалість аудіо кліпу не відповідає вибраному інтервалу")
    return _Observed(first_out=first, last_out=last, video_end_out=video_end, frame_count=len(pts))


def _audio_durations(
    streams: list[dict[str, object]], container: dict[str, object]
) -> list[Fraction | None]:
    """Duration of every audio stream of the output, in stream order."""
    return [
        _fraction(stream.get("duration")) or _fraction(container.get("duration"))
        for stream in streams
        if stream.get("codec_type") == "audio"
    ]


def _decode_check(
    tools: MediaTools,
    temp: Path,
    expected_frames: int,
    cancel: threading.Event | None,
    on_progress: ExportProgressCallback | None,
) -> None:
    """Decode every stream of the file: no decoder message, one frame per video packet.

    ``-map 0`` is needed: without it FFmpeg picks one video and one audio stream and the
    extra audio tracks of audio_mode "all" would never be decoded.
    """
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
        "-fps_mode",
        "passthrough",
        "-f",
        "null",
        "-",
    ]
    result, frames = run_ffmpeg(argv, "verify", cancel, on_progress)
    if result.cancelled:
        raise ExportCancelledError("Експорт скасовано")
    tail = result.stderr_tail[-2000:]
    if result.exit_code != 0 or tail.strip() or frames != expected_frames:
        raise OutputInvalidError("Кліп не пройшов перевірку декодуванням", detail=tail)


@dataclass(frozen=True, slots=True)
class _Packets:
    pts: list[Fraction]
    """Presentation times in seconds, output container time."""
    last_duration: Fraction


def _video_packets(tools: MediaTools, temp: Path, video: dict[str, object]) -> _Packets | None:
    """Video packet times of the temp file, or ``None`` when it has no video frames."""
    time_base = _fraction(video.get("time_base"))
    if time_base is None or time_base <= 0:
        raise OutputInvalidError("Кліп має некоректну часову базу відео")
    options = ["-select_streams", "v:0", "-show_entries", "packet=pts,duration"]
    entries = [_obj(item) for item in _list(_ffprobe_json(tools, options, temp).get("packets"))]
    if not entries:
        return None
    pts: list[Fraction] = []
    for entry in entries:
        value = _int(entry.get("pts"))
        if value is None:
            raise OutputInvalidError("Кліп містить відеокадри без часу показу")
        pts.append(value * time_base)
    last_duration = (_int(entries[-1].get("duration")) or 0) * time_base
    return _Packets(pts=pts, last_duration=max(last_duration, Fraction(0)))


def _ffprobe_json(tools: MediaTools, options: list[str], path: Path) -> dict[str, object]:
    argv = [
        str(tools.ffprobe),
        "-v",
        "error",
        "-hide_banner",
        *options,
        "-of",
        "json",
        "-i",
        f"file:{path}",
    ]
    try:
        completed = run_short(argv, timeout_s=OUTPUT_PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise OutputInvalidError("Перевірка кліпу перевищила ліміт часу") from None
    except OSError:
        raise MediaToolsUnavailableError("Не вдалося запустити ffprobe") from None
    detail = stderr_tail(completed.stderr)
    if completed.returncode != 0:
        raise OutputInvalidError("Створений кліп не вдалося прочитати", detail=detail)
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
    """Exact value of an ffprobe number: int, ``"12.000000"`` or ``"1/1000"``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | str):
        try:
            return Fraction(value)
        except (ValueError, ZeroDivisionError):
            return None
    return None
