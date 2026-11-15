"""Preview proxy of a whole source (ADR 021, docs/05 section 2, FR-029).

``build_preview`` renders the original into ``<output_dir>/<task_id>.mp4`` through a temp file
(``.clipforge-<task_id>.part``, the engine's exact name), verifies that temp file with FFprobe and
only then replaces it into the final name. A failed, cancelled or invalid run removes its own temp
file and leaves no final file. The original is only read; its size and mtime are compared with the
registered ones before the work and with the values seen at the start of the work before the proxy
is published (plus the quick fingerprint before the work, when the caller has one).

Recipe: the ADR 018 filter chains over the whole source, so the proxy timeline starts at canonical
zero (``trim`` + ``setpts`` from the origin, ``-copyts``) and a late audio start is filled with
silence (``aresample=async=1:first_pts=0``); both chains come from ``export_engine`` and are not
written a second time here. The proxy is H.264 (libx264, ``-bf 0``, one keyframe per second), at
most 720 lines high and never upscaled, AAC stereo (``-ac 2``, whatever the source had), MP4 with
``+faststart``. Frame times are passed through (``-fps_mode passthrough``, ``-enc_time_base:v
demux``), so VFR stays VFR and the duration is the duration of the source. A proxy is never used
for the final export (FR-038). An HDR source has no proxy (HDR_UNSUPPORTED, docs/05 section 2):
there is no tone mapping, and a washed-out picture must not pass for the original.

Verification (ADR 021): FFprobe reads the temp file and decodes its first frames, then codecs,
size, stream set, origin and duration are checked against the source. There is no full decode
pass, unlike the export: the proxy is a review aid and the decode of hours of video would cost
as much as rendering it.

FFmpeg runs only through ``run_process`` (owned job, no shell, no console window).
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from clipforge.adapters.ffprobe import HDR_UNSUPPORTED, ProbeError, SourceProbe, probe_source
from clipforge.adapters.media_tools import MediaTools
from clipforge.adapters.process import FfmpegProgress
from clipforge.services.export_engine import (
    BOUNDARY_TOLERANCE_MS,
    ExportCancelledError,
    ExportStage,
    HdrUnsupportedError,
    InvalidAudioStreamError,
    OutputDirUnavailableError,
    OutputInvalidError,
    OutputNotWritableError,
    SourceChangedError,
    audio_chain,
    canonical_window,
    check_fingerprint,
    create_temp,
    ensure_unchanged,
    raise_if_cancelled,
    remove_quietly,
    render_failure,
    run_ffmpeg,
    source_state,
    video_chain,
)

PROFILE_VERSION: Final = 1
"""Part of the identity of a preview: change it with the recipe, and old previews are not reused."""
MAX_HEIGHT: Final = 720
DURATION_TOLERANCE_MS: Final = 100
"""docs/05 section 3: max(one frame interval, 50 ms); 100 ms covers one frame down to 10 fps."""

PreviewProgressCallback = Callable[[FfmpegProgress], None]


@dataclass(frozen=True, slots=True)
class PreviewRequest:
    task_id: str
    """Canonical UUID of the preview; the final file is ``<task_id>.mp4``."""
    source: Path
    """Absolute path of the original file."""
    output_dir: Path
    """Absolute path of an existing directory (the app's preview cache)."""
    audio_stream_index: int | None
    """Global source stream index of the audio to keep; ``None`` for a video-only proxy."""
    expected_size: int
    expected_mtime_ns: int
    """Size and ``st_mtime_ns`` of the registered source."""
    expected_fingerprint: str | None = None
    """``quick_fingerprint`` of the registered source, checked before the work."""


@dataclass(frozen=True, slots=True)
class PreviewResult:
    path: Path
    size: int
    duration_ms: int
    """Duration of the verified proxy (video end minus its first frame)."""


def build_preview(
    tools: MediaTools,
    request: PreviewRequest,
    *,
    cancel: threading.Event | None = None,
    on_progress: PreviewProgressCallback | None = None,
) -> PreviewResult:
    """Render, verify and publish one preview. Blocking; run it in a worker thread.

    Raises ``ExportError`` subclasses (``code`` tells what failed), the probe errors of
    ``clipforge.adapters.ffprobe`` and ``MediaToolsUnavailableError``. ``ValueError`` means a
    malformed request (relative paths, a task id that is not a canonical UUID).
    """
    _validate_request(request)
    source, directory = request.source, request.output_dir
    if not directory.is_dir():
        raise OutputDirUnavailableError("Тека прев'ю недоступна")

    before = source_state(source)
    if (before.size, before.mtime_ns) != (request.expected_size, request.expected_mtime_ns):
        raise SourceChangedError("Файл джерела змінився після реєстрації")
    if request.expected_fingerprint is not None:
        check_fingerprint(source, request.expected_fingerprint)
    probe = probe_source(tools.ffprobe, source)
    if probe.unsupported_reason == HDR_UNSUPPORTED:
        raise HdrUnsupportedError("HDR-відео поки не підтримується для прев'ю")
    audio = request.audio_stream_index
    if audio is not None and audio not in {stream.index for stream in probe.audio_streams}:
        raise InvalidAudioStreamError("Вибрана аудіодоріжка відсутня у файлі")
    ensure_unchanged(source, before)
    raise_if_cancelled(cancel)

    temp = create_temp(directory, request.task_id)
    final = directory / f"{request.task_id.lower()}.mp4"
    published = False
    try:
        duration_ms = _render_and_verify(tools, request, probe, temp, cancel, on_progress)
        ensure_unchanged(source, before)
        raise_if_cancelled(cancel)
        size = temp.stat().st_size
        _publish(temp, final)
        published = True
    finally:
        if not published:
            remove_quietly(temp)
    return PreviewResult(path=final, size=size, duration_ms=duration_ms)


def _validate_request(request: PreviewRequest) -> None:
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


def _publish(temp: Path, final: Path) -> None:
    try:
        os.replace(temp, final)
    except OSError:
        raise OutputNotWritableError("Не вдалося зберегти прев'ю") from None


# --- render ----------------------------------------------------------------------------------


def _render_and_verify(
    tools: MediaTools,
    request: PreviewRequest,
    probe: SourceProbe,
    temp: Path,
    cancel: threading.Event | None,
    on_progress: PreviewProgressCallback | None,
) -> int:
    def forward(_stage: ExportStage, progress: FfmpegProgress) -> None:
        if on_progress is not None:
            on_progress(progress)

    argv = _render_argv(tools, request, probe, temp)
    result, _frames = run_ffmpeg(argv, "render", cancel, forward)
    if result.cancelled:
        raise ExportCancelledError("Створення прев'ю скасовано")
    if result.exit_code != 0:
        raise render_failure(result, temp.parent)
    raise_if_cancelled(cancel)
    return _verify(tools, request, probe, temp)


def _render_argv(
    tools: MediaTools, request: PreviewRequest, probe: SourceProbe, temp: Path
) -> list[str]:
    start_text, end_text = canonical_window(probe, 0, probe.duration_ms)
    graph = video_chain(
        probe.video_stream_index, start_text, end_text, scale_height=_scale_height(probe)
    )
    audio = request.audio_stream_index
    if audio is not None:
        graph += ";" + audio_chain(audio, 0, start_text, end_text)
    argv = [
        str(tools.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-y",
        "-copyts",
        "-i",
        f"file:{request.source}",
        "-filter_complex",
        graph,
        "-map",
        "[v]",
    ]
    if audio is not None:
        argv += ["-map", "[a0]"]
    argv += [
        "-map_chapters",
        "-1",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-bf",
        "0",
        "-force_key_frames",
        "expr:gte(t,n_forced*1)",
        "-fps_mode",
        "passthrough",
        "-enc_time_base:v",
        "demux",
    ]
    if audio is not None:
        argv += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    argv += ["-movflags", "+faststart", "-f", "mp4", f"file:{temp}"]
    return argv


def _scale_height(probe: SourceProbe) -> int | None:
    """``MAX_HEIGHT`` when the upright picture is taller than that, else no scaling (no upscale).
    FFmpeg applies the rotation before the filter graph, so a quarter turn swaps the sides."""
    upright_height = probe.width if probe.rotation in (90, 270) else probe.height
    return MAX_HEIGHT if upright_height > MAX_HEIGHT else None


# --- verification ----------------------------------------------------------------------------


def _verify(tools: MediaTools, request: PreviewRequest, source: SourceProbe, temp: Path) -> int:
    """FFprobe the temp file against the source; returns the proxy duration in milliseconds."""
    try:
        proxy = probe_source(tools.ffprobe, temp)
    except ProbeError as error:
        raise OutputInvalidError(
            "Створене прев'ю не вдалося прочитати", detail=error.detail
        ) from None

    if proxy.video_codec != "h264":
        raise OutputInvalidError("Відео прев'ю має неочікуваний кодек")
    if proxy.hdr or proxy.rotation != 0:
        raise OutputInvalidError("Відео прев'ю має неочікуваний формат кольору або поворот")
    if proxy.height > MAX_HEIGHT or proxy.width % 2 or proxy.height % 2:
        raise OutputInvalidError("Відео прев'ю має неочікуваний розмір")

    audios = proxy.audio_streams
    if request.audio_stream_index is None:
        if audios:
            raise OutputInvalidError("Прев'ю не повинно мати звуку")
    else:
        if len(audios) != 1 or audios[0].codec != "aac" or audios[0].channels != 2:
            raise OutputInvalidError("Звук прев'ю не відповідає вибраній доріжці")
        offset = audios[0].start_offset_ms
        if offset is not None and abs(offset) > BOUNDARY_TOLERANCE_MS:
            raise OutputInvalidError("Звук прев'ю починається не від нуля таймлайну")

    if abs(proxy.origin_ms) > BOUNDARY_TOLERANCE_MS:
        raise OutputInvalidError("Таймлайн прев'ю починається не від нуля")
    if abs(proxy.duration_ms - source.duration_ms) > DURATION_TOLERANCE_MS:
        raise OutputInvalidError("Тривалість прев'ю не відповідає тривалості джерела")
    return proxy.duration_ms
