"""Typed source probe (docs/05 sections 1-3, docs/01 FR-003 and FR-005).

Canonical zero is the presentation time of the first decoded frame of the main video
stream. It is kept exactly as ``origin_pts`` in the video stream time base;
``duration_ms`` and audio start offsets are measured from it.

Three short ffprobe calls, each with a timeout:

1. ``-show_format -show_streams``: container and stream metadata;
2. decode of the first packets of the video stream: the decode check and the origin;
3. only when the container gives no video stream duration (Matroska): the packets
   after the last keyframe, whose end is the end of the video stream.

The source is passed as one ``file:`` argument, so a file name is never parsed as an
option or as another FFmpeg protocol. The file is only read.
"""

from __future__ import annotations

import json
import math
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import ClassVar, Final

from clipforge.adapters.media_tools import MediaToolsUnavailableError, run_short, stderr_tail

DEFAULT_TIMEOUT_S: Final = 60.0
DECODE_CHECK_PACKETS: Final = 60
HDR_UNSUPPORTED: Final = "HDR_UNSUPPORTED"
_HDR_TRANSFERS: Final = frozenset({"smpte2084", "arib-std-b67"})
_HDR_SIDE_DATA: Final = frozenset({"DOVI configuration record"})
_UNDETERMINED_LANGUAGE: Final = "und"


class ProbeError(Exception):
    """Base of probe errors. ``message`` is Ukrainian and contains no paths.

    ``detail`` is a bounded ffprobe stderr tail for logs only.
    """

    code: ClassVar[str] = "SOURCE_UNREADABLE"

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class SourceUnavailableError(ProbeError):
    """Path is missing, is not a regular file or cannot be opened for reading."""

    code = "SOURCE_UNAVAILABLE"


class SourceUnreadableError(ProbeError):
    """FFprobe cannot read the file, or its video cannot be decoded."""

    code = "SOURCE_UNREADABLE"


class SourceNoVideoError(ProbeError):
    """The file has no video stream."""

    code = "SOURCE_NO_VIDEO"


class ProbeTimeoutError(ProbeError):
    """An ffprobe call exceeded its timeout; the child was killed."""

    code = "PROBE_TIMEOUT"


@dataclass(frozen=True, slots=True)
class AudioStream:
    index: int
    """Global stream index in the source (FR-005 stable ID)."""
    codec: str
    channels: int | None
    language: str | None
    """Language tag as stored; ``und`` and empty values become ``None``."""
    title: str | None
    default: bool
    start_offset_ms: int | None
    """Stream start minus canonical origin; ``None`` when the container gives no start."""


@dataclass(frozen=True, slots=True)
class SourceProbe:
    format_name: str
    duration_ms: int
    """End of the video stream minus canonical origin."""
    origin_pts: int
    """PTS of the first decoded video frame, in ``time_base``."""
    time_base: Fraction
    """Time base of the video stream."""
    video_stream_index: int
    video_codec: str
    width: int
    height: int
    rotation: int
    """Clockwise degrees to show the picture upright, normalised to [0, 360)."""
    hdr: bool
    unsupported_reason: str | None
    """``HDR_UNSUPPORTED`` for HDR sources (docs/05 section 2), otherwise ``None``."""
    audio_streams: tuple[AudioStream, ...]

    @property
    def origin_seconds(self) -> Fraction:
        """Container time of canonical zero, exact."""
        return self.origin_pts * self.time_base

    @property
    def origin_ms(self) -> int:
        """Container time of canonical zero, rounded to milliseconds."""
        return _round_ms(self.origin_seconds)


def probe_source(
    ffprobe: Path, source: Path, *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> SourceProbe:
    """Probe and decode-check ``source``. Raises ``ProbeError`` subclasses."""
    if not source.is_absolute():
        raise ValueError("source must be an absolute path")
    _check_readable_file(source)

    info = _run_json(ffprobe, ["-show_format", "-show_streams"], source, timeout_s)
    streams = [_obj(item) for item in _list(info.get("streams"))]
    container = _obj(info.get("format"))

    video = _main_video_stream(streams)
    if video is None:
        raise SourceNoVideoError("У файлі немає відеодоріжки")
    video_index = _int(video.get("index"))
    time_base = _positive_fraction(video.get("time_base"))
    width = _int(video.get("width"))
    height = _int(video.get("height"))
    if video_index is None or time_base is None or not width or not height:
        raise SourceUnreadableError("Не вдалося прочитати параметри відеодоріжки")

    origin_pts = _first_frame_pts(ffprobe, source, video_index, timeout_s)
    origin = origin_pts * time_base
    end_pts = _video_end_pts(ffprobe, source, video, video_index, container, timeout_s)
    duration_ms = _round_ms((end_pts - origin_pts) * time_base)
    if duration_ms <= 0:
        raise SourceUnreadableError("Не вдалося визначити тривалість відео")

    hdr = _is_hdr(video)
    return SourceProbe(
        format_name=_str(container.get("format_name")) or "",
        duration_ms=duration_ms,
        origin_pts=origin_pts,
        time_base=time_base,
        video_stream_index=video_index,
        video_codec=_str(video.get("codec_name")) or "",
        width=width,
        height=height,
        rotation=_rotation(video),
        hdr=hdr,
        unsupported_reason=HDR_UNSUPPORTED if hdr else None,
        audio_streams=tuple(
            audio
            for audio in (_audio_stream(stream, origin) for stream in streams)
            if audio is not None
        ),
    )


def _check_readable_file(source: Path) -> None:
    try:
        mode = source.stat().st_mode
    except OSError:
        raise SourceUnavailableError("Файл джерела недоступний") from None
    if not stat.S_ISREG(mode):
        raise SourceUnavailableError("Шлях джерела не вказує на файл")
    try:
        with source.open("rb"):
            pass
    except OSError:
        raise SourceUnavailableError("Немає доступу до файлу джерела") from None


def _run_json(
    ffprobe: Path, options: Sequence[str], source: Path, timeout_s: float
) -> dict[str, object]:
    argv = [
        str(ffprobe),
        "-v",
        "error",
        "-hide_banner",
        *options,
        "-of",
        "json",
        "-i",
        f"file:{source}",
    ]
    try:
        completed = run_short(argv, timeout_s=timeout_s)
    except subprocess.TimeoutExpired:
        raise ProbeTimeoutError("Перевірка файлу перевищила ліміт часу") from None
    except OSError:
        raise MediaToolsUnavailableError("Не вдалося запустити ffprobe") from None
    detail = stderr_tail(completed.stderr)
    if completed.returncode != 0:
        raise SourceUnreadableError(
            "Файл пошкоджений або має непідтримуваний формат", detail=detail
        )
    try:
        document: object = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        document = None
    if not isinstance(document, dict):
        raise SourceUnreadableError("FFprobe повернув некоректну відповідь", detail=detail)
    return _obj(document)


def _main_video_stream(streams: list[dict[str, object]]) -> dict[str, object] | None:
    for stream in streams:
        attached_pic = _int(_obj(stream.get("disposition")).get("attached_pic"))
        if stream.get("codec_type") == "video" and attached_pic != 1:
            return stream
    return None


def _first_frame_pts(ffprobe: Path, source: Path, video_index: int, timeout_s: float) -> int:
    """Decode the first packets: no frame means the video cannot be played."""
    options = [
        "-select_streams",
        str(video_index),
        "-read_intervals",
        f"%+#{DECODE_CHECK_PACKETS}",
        "-show_entries",
        "frame=pts,best_effort_timestamp",
    ]
    frames = _list(_run_json(ffprobe, options, source, timeout_s).get("frames"))
    timestamps: list[int] = []
    for item in frames:
        frame = _obj(item)
        timestamp = _int(frame.get("best_effort_timestamp"))
        if timestamp is None:
            timestamp = _int(frame.get("pts"))
        if timestamp is not None:
            timestamps.append(timestamp)
    if not timestamps:
        raise SourceUnreadableError("Не вдалося декодувати відео")
    return min(timestamps)


def _video_end_pts(
    ffprobe: Path,
    source: Path,
    video: dict[str, object],
    video_index: int,
    container: dict[str, object],
    timeout_s: float,
) -> int:
    """End of the video stream in its time base."""
    start_pts = _int(video.get("start_pts"))
    duration_ts = _int(video.get("duration_ts"))
    if start_pts is not None and duration_ts is not None and duration_ts > 0:
        return start_pts + duration_ts

    # No stream duration (Matroska): read packets from the last keyframe to the end.
    # The seek target only has to be at or after the last keyframe, so the container
    # end estimate may overshoot.
    options = ["-select_streams", str(video_index)]
    container_start = _fraction(container.get("start_time")) or Fraction(0)
    container_duration = _fraction(container.get("duration"))
    if container_duration is not None and container_start + container_duration > 0:
        target = float(container_start + container_duration)
        options += ["-read_intervals", f"{target:.6f}%"]
    options += ["-show_entries", "packet=pts,duration,flags"]
    packets = _list(_run_json(ffprobe, options, source, timeout_s).get("packets"))
    ends: list[int] = []
    for item in packets:
        packet = _obj(item)
        pts = _int(packet.get("pts"))
        if pts is None or "D" in (_str(packet.get("flags")) or ""):
            continue
        ends.append(pts + max(_int(packet.get("duration")) or 0, 0))
    if not ends:
        raise SourceUnreadableError("Не вдалося визначити тривалість відео")
    return max(ends)


def _audio_stream(stream: dict[str, object], origin: Fraction) -> AudioStream | None:
    if stream.get("codec_type") != "audio":
        return None
    index = _int(stream.get("index"))
    if index is None:
        return None
    tags = {key.lower(): value for key, value in _obj(stream.get("tags")).items()}
    language = (_str(tags.get("language")) or "").strip()
    title = (_str(tags.get("title")) or "").strip()
    start_pts = _int(stream.get("start_pts"))
    time_base = _positive_fraction(stream.get("time_base"))
    offset_ms = (
        _round_ms(start_pts * time_base - origin)
        if start_pts is not None and time_base is not None
        else None
    )
    return AudioStream(
        index=index,
        codec=_str(stream.get("codec_name")) or "",
        channels=_int(stream.get("channels")),
        language=language if language and language != _UNDETERMINED_LANGUAGE else None,
        title=title or None,
        default=_int(_obj(stream.get("disposition")).get("default")) == 1,
        start_offset_ms=offset_ms,
    )


def _rotation(video: dict[str, object]) -> int:
    """FFprobe reports the display matrix rotation counter-clockwise."""
    for item in _list(video.get("side_data_list")):
        rotation = _fraction(_obj(item).get("rotation"))
        if rotation is not None:
            return (-round(rotation)) % 360
    return 0


def _is_hdr(video: dict[str, object]) -> bool:
    if _str(video.get("color_transfer")) in _HDR_TRANSFERS:
        return True
    return any(
        _str(_obj(item).get("side_data_type")) in _HDR_SIDE_DATA
        for item in _list(video.get("side_data_list"))
    )


def _round_ms(seconds: Fraction) -> int:
    """Seconds to integer milliseconds, half rounded up."""
    return math.floor(seconds * 1000 + Fraction(1, 2))


def _obj(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


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
    """Exact value of an ffprobe number: int, float, ``"12.000000"`` or ``"1/1000"``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | str):
        try:
            return Fraction(value)
        except (ValueError, ZeroDivisionError):
            return None
    if isinstance(value, float) and math.isfinite(value):
        return Fraction(value)
    return None


def _positive_fraction(value: object) -> Fraction | None:
    result = _fraction(value)
    return result if result is not None and result > 0 else None
