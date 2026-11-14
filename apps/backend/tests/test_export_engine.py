"""Exact-cut export on real FFmpeg and copies of the generated fixtures (T-003, T-005..T-010).

Fixtures come from ``npm run fixtures`` (test-results/fixtures); every test works on a
copy in ``tmp_path`` and never touches the shared files. Pictures and sound of the
exported clips are decoded again here, so the checks look at the content of the first
and last frames and at signals, not only at durations.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import shutil
import threading
import uuid
from array import array
from collections.abc import Callable, Sequence
from fractions import Fraction
from pathlib import Path

import pytest

from clipforge.adapters.ffprobe import SourceProbe, probe_source
from clipforge.adapters.fingerprint import quick_fingerprint
from clipforge.adapters.media_tools import (
    MediaTools,
    MediaToolsUnavailableError,
    resolve_media_tools,
    run_short,
)
from clipforge.adapters.process import FfmpegProgress, ProcessResult, run_process
from clipforge.domain.interval import IntervalError
from clipforge.domain.naming import export_basename
from clipforge.services import export_engine
from clipforge.services.export_engine import (
    DiskFullError,
    ExportCancelledError,
    ExportRequest,
    ExportResult,
    ExportStage,
    HdrUnsupportedError,
    InvalidAudioStreamError,
    NoVideoFramesError,
    OutputDirUnavailableError,
    OutputInvalidError,
    RenderFailedError,
    SourceChangedError,
    export_clip,
)

from .support import BACKEND_DIR

FIXTURES = BACKEND_DIR.parents[1] / "test-results" / "fixtures"
CLIP_ID = "clip-1"
Colors = list[str]


@pytest.fixture(scope="module")
def tools() -> MediaTools:
    return resolve_media_tools("test", os.environ)


# --- helpers --------------------------------------------------------------------------


def copy_fixture(tmp_path: Path, name: str, as_name: str | None = None) -> Path:
    source = FIXTURES / name
    if not source.is_file():
        pytest.fail(f"Fixture {name} is missing: run `npm run fixtures` first")
    sources = tmp_path / "sources"
    sources.mkdir(exist_ok=True)
    target = sources / (as_name or name)
    shutil.copyfile(source, target)
    return target


def output_dir(tmp_path: Path, name: str = "out") -> Path:
    target = tmp_path / name
    target.mkdir(exist_ok=True)
    return target


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_request(
    source: Path,
    out: Path,
    start_ms: int,
    end_ms: int,
    audio: int | None,
    *,
    task_id: str | None = None,
    expected_fingerprint: str | None = None,
    audio_indexes: tuple[int, ...] = (),
) -> ExportRequest:
    return ExportRequest(
        task_id=str(uuid.uuid4()) if task_id is None else task_id,
        source=source,
        output_dir=out,
        start_ms=start_ms,
        end_ms=end_ms,
        clip_id=CLIP_ID,
        revision=1,
        audio_stream_index=audio,
        expected_fingerprint=expected_fingerprint,
        audio_stream_indexes=audio_indexes,
    )


def run_export(
    tools: MediaTools,
    source: Path,
    out: Path,
    start_ms: int,
    end_ms: int,
    audio: int | None,
    *,
    task_id: str | None = None,
    expected_fingerprint: str | None = None,
    audio_indexes: tuple[int, ...] = (),
) -> ExportResult:
    request = make_request(
        source,
        out,
        start_ms,
        end_ms,
        audio,
        task_id=task_id,
        expected_fingerprint=expected_fingerprint,
        audio_indexes=audio_indexes,
    )
    return export_clip(tools, request)


def ffmpeg_output(tools: MediaTools, *args: str) -> bytes:
    argv = [str(tools.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    completed = run_short(argv, timeout_s=120)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed.stdout


def ffmpeg_output_stderr(tools: MediaTools, *args: str) -> str:
    """Decoder messages of an ffmpeg run; the exit code is not checked."""
    argv = [str(tools.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    return run_short(argv, timeout_s=120).stderr.decode("utf-8", errors="replace")


def make_media(tools: MediaTools, target: Path, *args: str) -> Path:
    ffmpeg_output(tools, "-y", *args, str(target))
    return target


def classify(red: int, green: int, blue: int) -> str:
    if green > 100 and red < 60 and blue < 60:
        return "green"
    if red > 180 and green < 70 and blue < 70:
        return "red"
    if blue > 180 and red < 70 and green < 70:
        return "blue"
    return f"other({red},{green},{blue})"


def frame_colors(tools: MediaTools, path: Path) -> Colors:
    """Dominant colour of every decoded video frame, in decoding order."""
    data = ffmpeg_output(
        tools,
        "-i", str(path), "-map", "0:v:0",
        "-vf", "scale=2:2:flags=area", "-fps_mode", "passthrough",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    )  # fmt: skip
    assert len(data) % 12 == 0
    colors: Colors = []
    for offset in range(0, len(data), 12):
        pixels = data[offset : offset + 12]
        red, green, blue = (sum(pixels[channel::3]) // 4 for channel in range(3))
        colors.append(classify(red, green, blue))
    return colors


def audio_samples(tools: MediaTools, path: Path, rate: int = 8000, track: int = 0) -> array[int]:
    data = ffmpeg_output(
        tools,
        "-i", str(path), "-map", f"0:a:{track}",
        "-ac", "1", "-ar", str(rate), "-f", "s16le", "-",
    )  # fmt: skip
    samples = array("h")
    samples.frombytes(data)
    return samples


def rms(samples: Sequence[int], rate: int, start_s: float, end_s: float) -> float:
    window = samples[int(start_s * rate) : int(end_s * rate)]
    assert len(window) > 0
    return math.sqrt(sum(sample * sample for sample in window) / len(window)) / 32768


def frequency(samples: Sequence[int], rate: int, start_s: float, end_s: float) -> float:
    window = samples[int(start_s * rate) : int(end_s * rate)]
    crossings = sum(1 for a, b in itertools.pairwise(window) if (a < 0) != (b < 0))
    return crossings / 2 / (len(window) / rate)


def ffprobe_json(tools: MediaTools, path: Path, *options: str) -> dict[str, object]:
    argv = [str(tools.ffprobe), "-v", "error", *options, "-of", "json", "-i", f"file:{path}"]
    completed = run_short(argv, timeout_s=60)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    document: dict[str, object] = json.loads(completed.stdout)
    return document


def stream_types(tools: MediaTools, path: Path) -> list[tuple[str, str]]:
    document = ffprobe_json(tools, path, "-show_streams")
    streams = document["streams"]
    assert isinstance(streams, list)
    return [(str(s["codec_type"]), str(s["codec_name"])) for s in streams]


def video_pts(tools: MediaTools, path: Path) -> list[Fraction]:
    document = ffprobe_json(
        tools, path, "-select_streams", "v:0", "-show_entries", "packet=pts_time"
    )
    packets = document["packets"]
    assert isinstance(packets, list)
    return sorted(Fraction(str(packet["pts_time"])) for packet in packets)


def container_duration(tools: MediaTools, path: Path) -> float:
    document = ffprobe_json(tools, path, "-show_entries", "format=duration")
    container = document["format"]
    assert isinstance(container, dict)
    return float(container["duration"])


def probe(tools: MediaTools, path: Path) -> SourceProbe:
    return probe_source(tools.ffprobe, path)


def leftovers(out: Path) -> list[str]:
    return sorted(os.listdir(out))


def expected_frames(start_ms: int, end_ms: int, fps: int = 30) -> int:
    """Frames of a CFR source at ``n / fps`` with ``start <= n / fps < end``."""
    first = -(-start_ms * fps // 1000)
    after_last = -(-end_ms * fps // 1000)
    return after_last - first


# --- T-003: first and last frame content -------------------------------------------------


def test_color_gop_non_keyframe_start_is_exact(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    source_hash = sha256(source)

    result = run_export(
        tools, source, out, 4120, 7880, 1, expected_fingerprint=quick_fingerprint(source)
    )

    assert result.path.parent == out
    assert leftovers(out) == [result.path.name]
    assert result.seek_mode == "preseek"
    colors = frame_colors(tools, result.path)
    assert colors[0] == "green"  # the keyframe before the start is red
    assert set(colors) == {"green"}
    assert len(colors) == result.video_frame_count == expected_frames(4120, 7880) == 113
    assert abs(container_duration(tools, result.path) - 3.76) <= 0.08
    assert 4120 <= result.first_video_pts_ms <= 4120 + 50
    assert 7880 - 50 <= result.last_video_pts_ms < 7880
    assert stream_types(tools, result.path) == [("video", "h264"), ("audio", "aac")]
    assert sha256(source) == source_hash


def test_scene_boundaries_are_half_open(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    result = run_export(tools, source, output_dir(tmp_path), 4000, 8000, 1)
    colors = frame_colors(tools, result.path)
    assert colors == ["green"] * 120  # frame 8.000 s (blue) is outside [4000, 8000)
    assert (result.first_video_pts_ms, result.last_video_pts_ms) == (4000, 7966)


def test_manifest_data(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    result = run_export(tools, source, output_dir(tmp_path), 1000, 4000, 1)
    assert result.ffmpeg_version == tools.ffmpeg_version
    assert result.ffprobe_version == tools.ffprobe_version
    assert result.profile == {
        "recipe": "ADR 018",
        "container": "mp4",
        "video_codec": "libx264",
        "crf": 18,
        "preset": "medium",
        "pix_fmt": "yuv420p",
        "b_frames": 0,
        "audio_codec": "aac",
        "audio_bitrate": "192k",
    }
    assert 2950 <= result.observed_duration_ms <= 3050
    assert result.video_frame_count == 90


# --- T-006, T-007: audio selection ---------------------------------------------------------


def test_no_audio_source_exports_video_only(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "no_audio.mp4")
    result = run_export(tools, source, output_dir(tmp_path), 1000, 4000, None)
    assert stream_types(tools, result.path) == [("video", "h264")]
    assert result.profile["audio_codec"] == "none"
    assert "audio_bitrate" not in result.profile


def test_audio_stream_must_exist(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "no_audio.mp4")
    out = output_dir(tmp_path)
    with pytest.raises(InvalidAudioStreamError) as error:
        run_export(tools, source, out, 1000, 4000, 1)
    assert error.value.code == "INVALID_AUDIO_STREAM"
    assert leftovers(out) == []


def test_video_stream_index_is_not_an_audio_stream(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    with pytest.raises(InvalidAudioStreamError):
        run_export(tools, source, output_dir(tmp_path), 1000, 4000, 0)


@pytest.mark.parametrize(
    "indexes",
    [(1, 0), (0,), (1, 5), (3,)],
    ids=["video-after-audio", "only-video", "one-missing", "all-missing"],
)
def test_audio_stream_indexes_must_be_audio_streams(
    tools: MediaTools, tmp_path: Path, indexes: tuple[int, ...]
) -> None:
    source = copy_fixture(tmp_path, "two_tracks.mkv")
    out = output_dir(tmp_path)
    with pytest.raises(InvalidAudioStreamError) as error:
        run_export(tools, source, out, 1000, 4000, None, audio_indexes=indexes)
    assert error.value.code == "INVALID_AUDIO_STREAM"
    assert leftovers(out) == []


@pytest.mark.parametrize(("stream", "expected_hz"), [(1, 220), (2, 880)])
def test_two_tracks_exports_the_selected_track(
    tools: MediaTools, tmp_path: Path, stream: int, expected_hz: int
) -> None:
    source = copy_fixture(tmp_path, "two_tracks.mkv")
    result = run_export(tools, source, output_dir(tmp_path), 2000, 5000, stream)
    assert stream_types(tools, result.path) == [("video", "h264"), ("audio", "aac")]
    samples = audio_samples(tools, result.path)
    assert abs(frequency(samples, 8000, 0.3, 2.7) - expected_hz) <= 15
    assert rms(samples, 8000, 0.3, 2.7) > 0.02


@pytest.mark.parametrize(("indexes", "expected_hz"), [((1, 2), (220, 880)), ((2, 1), (880, 220))])
def test_two_tracks_exports_every_track_in_the_given_order(
    tools: MediaTools, tmp_path: Path, indexes: tuple[int, ...], expected_hz: tuple[int, int]
) -> None:
    source = copy_fixture(tmp_path, "two_tracks.mkv")
    out = output_dir(tmp_path)
    result = run_export(tools, source, out, 2000, 5000, None, audio_indexes=indexes)
    assert stream_types(tools, result.path) == [
        ("video", "h264"),
        ("audio", "aac"),
        ("audio", "aac"),
    ]
    assert result.profile["audio_codec"] == "aac"
    assert leftovers(out) == [result.path.name]
    for track, hz in enumerate(expected_hz):
        samples = audio_samples(tools, result.path, track=track)
        assert abs(frequency(samples, 8000, 0.3, 2.7) - hz) <= 15, track
        assert rms(samples, 8000, 0.3, 2.7) > 0.02, track


# --- T-008, T-009, T-010: timing ------------------------------------------------------------


def test_audio_offset_is_kept(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "audio_offset.mkv")
    result = run_export(tools, source, output_dir(tmp_path), 500, 3500, 1)
    samples = audio_samples(tools, result.path)
    # The 1000 Hz audio starts at 1.5 s in the source, i.e. 1.0 s into this clip.
    assert rms(samples, 8000, 0.1, 0.85) < 0.001
    assert rms(samples, 8000, 1.1, 2.8) > 0.02
    assert abs(frequency(samples, 8000, 1.2, 2.8) - 1000) <= 20
    assert abs(container_duration(tools, result.path) - 3.0) <= 0.08


def test_audio_offset_is_kept_when_all_audio_streams_are_exported(
    tools: MediaTools, tmp_path: Path
) -> None:
    source = copy_fixture(tmp_path, "audio_offset.mkv")
    assert [stream.index for stream in probe(tools, source).audio_streams] == [1]
    result = run_export(tools, source, output_dir(tmp_path), 500, 3500, None, audio_indexes=(1,))
    assert stream_types(tools, result.path) == [("video", "h264"), ("audio", "aac")]
    samples = audio_samples(tools, result.path)
    assert rms(samples, 8000, 0.1, 0.85) < 0.001
    assert rms(samples, 8000, 1.1, 2.8) > 0.02
    assert abs(frequency(samples, 8000, 1.2, 2.8) - 1000) <= 20
    assert abs(container_duration(tools, result.path) - 3.0) <= 0.08


def test_every_audio_stream_keeps_its_own_start_offset(tools: MediaTools, tmp_path: Path) -> None:
    source = make_media(
        tools, tmp_path / "offsets.mkv",
        "-f", "lavfi", "-i", "color=c=green:s=160x90:r=30:d=6",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
        "-itsoffset", "1.5",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=4.5",
        "-map", "0:v", "-map", "1:a", "-map", "2:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-c:a", "aac",
    )  # fmt: skip
    assert [stream.index for stream in probe(tools, source).audio_streams] == [1, 2]
    result = run_export(tools, source, output_dir(tmp_path), 500, 3500, None, audio_indexes=(2, 1))
    late = audio_samples(tools, result.path, track=0)  # source stream 2: starts 1.0 s into the clip
    early = audio_samples(tools, result.path, track=1)  # source stream 1: sounds from the start
    assert rms(late, 8000, 0.1, 0.85) < 0.001
    assert rms(late, 8000, 1.1, 2.8) > 0.02
    assert abs(frequency(late, 8000, 1.2, 2.8) - 1000) <= 20
    assert rms(early, 8000, 0.1, 0.85) > 0.02
    assert abs(frequency(early, 8000, 0.1, 2.8) - 440) <= 15


def test_variable_frame_rate_keeps_pts(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "vfr.mkv")
    result = run_export(tools, source, output_dir(tmp_path), 2000, 5000, None)
    pts = video_pts(tools, result.path)
    intervals = {round(float(b - a), 3) for a, b in itertools.pairwise(pts)}
    assert any(abs(value - 0.033) <= 0.003 for value in intervals), intervals
    assert any(abs(value - 0.067) <= 0.003 for value in intervals), intervals
    assert abs(container_duration(tools, result.path) - 3.0) <= 0.08
    assert 2000 <= result.first_video_pts_ms <= 2050
    assert result.last_video_pts_ms < 5000


@pytest.fixture
def mpegts_source(tools: MediaTools, tmp_path: Path) -> Path:
    base = copy_fixture(tmp_path, "color_gop.mp4")
    return make_media(
        tools, tmp_path / "sources" / "shifted.ts",
        "-i", str(base), "-c", "copy", "-output_ts_offset", "10", "-f", "mpegts",
    )  # fmt: skip


def test_mpegts_with_nonzero_start_is_decoded_from_the_start(
    tools: MediaTools, tmp_path: Path, mpegts_source: Path
) -> None:
    assert probe(tools, mpegts_source).origin_ms >= 10000
    result = run_export(tools, mpegts_source, output_dir(tmp_path), 4120, 7880, 1)
    assert result.seek_mode == "full_decode"
    colors = frame_colors(tools, result.path)
    assert colors[0] == "green"
    assert colors == ["green"] * 113
    assert abs(container_duration(tools, result.path) - 3.76) <= 0.08


@pytest.mark.parametrize("container", ["mkv", "mp4"])
def test_nonzero_origin_with_preseek(tools: MediaTools, tmp_path: Path, container: str) -> None:
    base = copy_fixture(tmp_path, "color_gop.mp4")
    source = make_media(
        tools, tmp_path / "sources" / f"shifted.{container}",
        "-i", str(base), "-c", "copy", "-output_ts_offset", "10",
    )  # fmt: skip
    assert probe(tools, source).origin_ms == 10000
    result = run_export(tools, source, output_dir(tmp_path), 4120, 7880, 1)
    assert result.seek_mode == "preseek"  # -ss is relative to the container start
    assert frame_colors(tools, result.path) == ["green"] * 113
    assert 4120 <= result.first_video_pts_ms <= 4170


def test_preseek_that_finds_nothing_is_repeated_without_it(
    tools: MediaTools, tmp_path: Path, mpegts_source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_engine, "PRESEEK_FORMATS", frozenset({"mpegts"}))
    commands: list[list[str]] = []
    real: Callable[..., ProcessResult] = run_process

    def spy(args: Sequence[str], **kwargs: object) -> ProcessResult:
        if "-filter_complex" in args:
            commands.append(list(args))
        return real(args, **kwargs)

    monkeypatch.setattr(export_engine, "run_process", spy)
    out = output_dir(tmp_path)
    result = run_export(tools, mpegts_source, out, 4120, 7880, 1)
    assert [("-ss" in command) for command in commands] == [True, False]
    assert result.seek_mode == "full_decode"
    assert frame_colors(tools, result.path) == ["green"] * 113
    assert leftovers(out) == [result.path.name]


# --- geometry --------------------------------------------------------------------------------


def test_odd_dimensions_are_padded_not_cropped(tools: MediaTools, tmp_path: Path) -> None:
    source = make_media(
        tools, tmp_path / "odd.mkv",
        "-f", "lavfi", "-i", "color=c=green:s=321x181:r=30:d=2",
        "-c:v", "libx264", "-pix_fmt", "yuv444p", "-preset", "ultrafast",
    )  # fmt: skip
    assert (probe(tools, source).width, probe(tools, source).height) == (321, 181)
    result = run_export(tools, source, output_dir(tmp_path), 500, 1500, None)
    info = probe(tools, result.path)
    assert (info.width, info.height) == (322, 182)
    frame = ffmpeg_output(
        tools, "-i", str(result.path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"
    )
    assert len(frame) == 322 * 182 * 3

    def pixel(x: int, y: int) -> tuple[int, int, int]:
        offset = (y * 322 + x) * 3
        return frame[offset], frame[offset + 1], frame[offset + 2]

    assert classify(*pixel(319, 179)) == "green"  # last full content pixels are kept
    assert classify(*pixel(0, 0)) == "green"
    assert sum(pixel(321, 181)) < 60  # only the padding is black


def test_rotation_is_applied_once(tools: MediaTools, tmp_path: Path) -> None:
    source = make_media(
        tools, tmp_path / "rotated.mp4",
        "-display_rotation", "90", "-i", str(copy_fixture(tmp_path, "no_audio.mp4")),
        "-c", "copy",
    )  # fmt: skip
    assert probe(tools, source).rotation == 270
    result = run_export(tools, source, output_dir(tmp_path), 4120, 7880, None)
    info = probe(tools, result.path)
    assert (info.width, info.height, info.rotation) == (180, 320, 0)
    assert set(frame_colors(tools, result.path)) == {"green"}


def test_hdr_source_is_unsupported(tools: MediaTools, tmp_path: Path) -> None:
    source = make_media(
        tools, tmp_path / "hdr.mp4",
        "-f", "lavfi", "-i", "color=c=gray:s=64x64:r=30:d=1",
        "-vf", "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
    )  # fmt: skip
    out = output_dir(tmp_path)
    with pytest.raises(HdrUnsupportedError) as error:
        run_export(tools, source, out, 0, 500, None)
    assert error.value.code == "HDR_UNSUPPORTED"
    assert leftovers(out) == []


# --- file names and directories --------------------------------------------------------------


def test_cyrillic_name_and_directory_with_spaces(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4", "Київ запис.mp4")
    out = output_dir(tmp_path, "Вихідні кліпи з пробілами")
    result = run_export(tools, source, out, 1000, 3000, 1)
    assert result.path.parent == out
    assert result.path.name.startswith(f"Київ запис_{CLIP_ID}_r1_00h00m01s000-00h00m03s000")
    assert leftovers(out) == [result.path.name]
    assert probe(tools, result.path).duration_ms > 1900


@pytest.mark.parametrize(
    "name", ["it's & %PATH% ;, [1] (copy).mp4", "-i.mp4", "file;name ^ `x`.mp4", "Ünïcødé 日本.mp4"]
)
def test_unusual_source_names(tools: MediaTools, tmp_path: Path, name: str) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4", name)
    out = output_dir(tmp_path)
    result = run_export(tools, source, out, 1000, 2500, 1)
    assert leftovers(out) == [result.path.name]
    assert len(frame_colors(tools, result.path)) == 45


def test_collision_gets_a_suffix_and_keeps_the_existing_file(
    tools: MediaTools, tmp_path: Path
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    basename = export_basename(
        source_stem=source.stem, clip_id=CLIP_ID, revision=1, start_ms=1000, end_ms=2500
    )
    existing = out / f"{basename}.mp4"
    existing.write_bytes(b"somebody else's clip")

    second = run_export(tools, source, out, 1000, 2500, 1)
    third = run_export(tools, source, out, 1000, 2500, 1)

    assert second.path.name == f"{basename} (2).mp4"
    assert third.path.name == f"{basename} (3).mp4"
    assert existing.read_bytes() == b"somebody else's clip"
    assert len(leftovers(out)) == 3


def test_output_next_to_the_source_leaves_the_source_alone(
    tools: MediaTools, tmp_path: Path
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    before = sha256(source)
    result = run_export(tools, source, source.parent, 1000, 2500, 1)
    assert result.path != source
    assert sha256(source) == before
    assert leftovers(source.parent) == sorted([source.name, result.path.name])


def test_temp_file_has_the_task_uuid_and_lives_in_the_output_dir(
    tools: MediaTools, tmp_path: Path
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    task_id = str(uuid.uuid4())
    seen: dict[ExportStage, list[str]] = {}

    def watch(stage: ExportStage, progress: FfmpegProgress) -> None:
        seen.setdefault(stage, leftovers(out))

    result = export_clip(
        tools, make_request(source, out, 1000, 4000, 1, task_id=task_id), on_progress=watch
    )

    temp_name = f".clipforge-{task_id}.part"
    assert seen["render"] == [temp_name]
    assert seen["verify"] == [temp_name]
    assert leftovers(out) == [result.path.name]


def test_progress_goes_through_with_the_stage(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    blocks: list[tuple[ExportStage, FfmpegProgress]] = []
    export_clip(
        tools,
        make_request(source, output_dir(tmp_path), 1000, 4000, 1),
        on_progress=lambda stage, progress: blocks.append((stage, progress)),
    )
    for stage in ("render", "verify"):
        stage_blocks = [progress for name, progress in blocks if name == stage]
        assert stage_blocks, stage
        assert stage_blocks[-1].ended is True
    names = [name for name, _ in blocks]
    assert names == sorted(names)  # every render block comes before the verify blocks
    render_blocks = [progress for name, progress in blocks if name == "render"]
    assert render_blocks[-1].frame == 90


# --- source identity ---------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["mtime", "size"])
def test_source_change_during_render_aborts(tools: MediaTools, tmp_path: Path, change: str) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    changed = threading.Event()

    def touch(stage: ExportStage, progress: FfmpegProgress) -> None:
        if stage != "render" or changed.is_set():
            return
        changed.set()
        if change == "mtime":
            stat = source.stat()
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        else:
            with source.open("ab") as handle:
                handle.write(b"\0")

    with pytest.raises(SourceChangedError) as error:
        export_clip(tools, make_request(source, out, 1000, 4000, 1), on_progress=touch)
    assert error.value.code == "SOURCE_CHANGED"
    assert changed.is_set()
    assert leftovers(out) == []


def test_fingerprint_mismatch_stops_before_any_work(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    other = copy_fixture(tmp_path, "no_audio.mp4")
    out = output_dir(tmp_path)
    with pytest.raises(SourceChangedError):
        run_export(tools, source, out, 1000, 4000, 1, expected_fingerprint=quick_fingerprint(other))
    assert leftovers(out) == []


def test_source_hash_is_unchanged_by_every_kind_of_export(
    tools: MediaTools, tmp_path: Path
) -> None:
    names = ["color_gop.mp4", "audio_offset.mkv", "two_tracks.mkv", "vfr.mkv"]
    for name in names:
        source = copy_fixture(tmp_path, name)
        original = sha256(FIXTURES / name)
        audio = None if name == "vfr.mkv" else 1
        run_export(tools, source, output_dir(tmp_path, name), 1000, 3000, audio)
        assert sha256(source) == original, name


# --- cancel and invalid output --------------------------------------------------------------------


def test_cancel_before_start(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ExportCancelledError) as error:
        export_clip(tools, make_request(source, out, 1000, 4000, 1), cancel=cancel)
    assert error.value.code == "EXPORT_CANCELLED"
    assert leftovers(out) == []


def test_cancel_during_render_leaves_no_files(tools: MediaTools, tmp_path: Path) -> None:
    source = make_media(
        tools, tmp_path / "heavy.mp4",
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    )  # fmt: skip
    out = output_dir(tmp_path)
    cancel = threading.Event()
    stages: list[ExportStage] = []

    def stop(stage: ExportStage, progress: FfmpegProgress) -> None:
        stages.append(stage)
        cancel.set()

    with pytest.raises(ExportCancelledError):
        export_clip(
            tools, make_request(source, out, 0, 30000, None), cancel=cancel, on_progress=stop
        )
    assert stages
    assert set(stages) == {"render"}
    assert leftovers(out) == []


def corrupt_render(monkeypatch: pytest.MonkeyPatch, damage: Callable[[Path], None]) -> None:
    real: Callable[..., ProcessResult] = run_process

    def run_then_damage(args: Sequence[str], **kwargs: object) -> ProcessResult:
        result = real(args, **kwargs)
        if "-filter_complex" in args:
            damage(Path(args[-1].removeprefix("file:")))
        return result

    monkeypatch.setattr(export_engine, "run_process", run_then_damage)


def truncate(path: Path) -> None:
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])  # the moov atom is at the end


def zero_media_data(path: Path) -> None:
    data = bytearray(path.read_bytes())
    start = data.index(b"mdat") + 4
    end = data.index(b"moov") - 4
    data[start:end] = bytes(end - start)
    path.write_bytes(bytes(data))


@pytest.mark.parametrize("damage", [truncate, zero_media_data], ids=["truncated", "zeroed"])
def test_invalid_output_leaves_neither_final_nor_temp_file(
    tools: MediaTools,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: Callable[[Path], None],
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    corrupt_render(monkeypatch, damage)
    with pytest.raises(OutputInvalidError) as error:
        run_export(tools, source, out, 1000, 4000, 1)
    assert error.value.code == "OUTPUT_INVALID"
    assert str(tmp_path) not in error.value.message
    assert leftovers(out) == []


@pytest.mark.parametrize("audio", [1, None], ids=["with-audio", "video-only"])
def test_interval_between_two_frames_has_its_own_code(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audio: int | None
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")  # 30 fps: frames at 4.000 s and 4.033 s
    out = output_dir(tmp_path)
    renders: list[bool] = []
    real: Callable[..., ProcessResult] = run_process

    def spy(args: Sequence[str], **kwargs: object) -> ProcessResult:
        if "-filter_complex" in args and "libx264" in args:
            renders.append("-ss" in args)
        return real(args, **kwargs)

    monkeypatch.setattr(export_engine, "run_process", spy)
    with pytest.raises(NoVideoFramesError) as error:
        run_export(tools, source, out, 4001, 4010, audio)
    assert error.value.code == "NO_VIDEO_FRAMES"
    assert not isinstance(error.value, RenderFailedError)
    assert renders == [True, False]  # the pre-seek result is confirmed by a full decode
    assert leftovers(out) == []


def test_interval_between_two_frames_of_a_full_decode_source(
    tools: MediaTools, tmp_path: Path, mpegts_source: Path
) -> None:
    out = output_dir(tmp_path)
    with pytest.raises(NoVideoFramesError) as error:
        run_export(tools, mpegts_source, out, 4001, 4010, 1)
    assert error.value.code == "NO_VIDEO_FRAMES"
    assert leftovers(out) == []


def test_corrupt_second_audio_track_fails_the_decode_check(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """audio_mode "all": the decode check must read every track, not only the first one."""
    source = copy_fixture(tmp_path, "two_tracks.mkv")
    out = output_dir(tmp_path)
    damaged: list[int] = []

    def damage_second_track(path: Path) -> None:
        # Only the later packets: ffmpeg decodes the first ones while probing the input, so
        # damage there would be reported even by a check that never decodes the track.
        document = ffprobe_json(
            tools, path, "-select_streams", "a:1", "-show_entries", "packet=pos,size"
        )
        packets = document["packets"]
        assert isinstance(packets, list)
        data = bytearray(path.read_bytes())
        for packet in packets[len(packets) // 2 :]:
            position, size = int(packet["pos"]), int(packet["size"])
            data[position : position + size] = bytes((i * 37 + 11) % 256 for i in range(size))
            damaged.append(position)
        path.write_bytes(bytes(data))
        track = ffmpeg_output_stderr(tools, "-i", str(path), "-map", "0:a:1", "-f", "null", "-")
        assert track.strip(), "the second track must really be damaged"
        first = ffmpeg_output_stderr(tools, "-i", str(path), "-map", "0:a:0", "-f", "null", "-")
        assert not first.strip(), "the first track must be intact"

    corrupt_render(monkeypatch, damage_second_track)
    with pytest.raises(OutputInvalidError) as error:
        run_export(tools, source, out, 1000, 4000, None, audio_indexes=(1, 2))
    assert error.value.code == "OUTPUT_INVALID"
    assert damaged
    assert leftovers(out) == []


def test_decode_check_maps_every_stream_of_the_output(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copy_fixture(tmp_path, "two_tracks.mkv")
    out = output_dir(tmp_path)
    checks: list[list[str]] = []
    real: Callable[..., ProcessResult] = run_process

    def spy(args: Sequence[str], **kwargs: object) -> ProcessResult:
        if "-filter_complex" not in args and "null" in args:
            checks.append(list(args))
        return real(args, **kwargs)

    monkeypatch.setattr(export_engine, "run_process", spy)
    result = run_export(tools, source, out, 2000, 5000, None, audio_indexes=(1, 2))
    assert len(checks) == 1
    argv = checks[0]
    assert argv[argv.index("-map") + 1] == "0"  # video and both audio tracks
    assert argv.count("-map") == 1
    assert stream_types(tools, result.path) == [
        ("video", "h264"),
        ("audio", "aac"),
        ("audio", "aac"),
    ]
    for track in (0, 1):
        assert not ffmpeg_output_stderr(
            tools, "-i", str(result.path), "-map", f"0:a:{track}", "-f", "null", "-"
        ).strip()


def fake_render(monkeypatch: pytest.MonkeyPatch, stderr: str, exit_code: int = 1) -> None:
    def fake(args: Sequence[str], **kwargs: object) -> ProcessResult:
        return ProcessResult(
            exit_code=exit_code, cancelled=False, timed_out=False, stderr_tail=stderr
        )

    monkeypatch.setattr(export_engine, "run_process", fake)


def test_disk_full_has_its_own_code(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    fake_render(monkeypatch, "av_interleaved_write_frame(): No space left on device")
    with pytest.raises(DiskFullError) as error:
        run_export(tools, source, out, 1000, 4000, 1)
    assert error.value.code == "DISK_FULL"
    assert leftovers(out) == []


def test_ffmpeg_failure_has_its_own_code(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    fake_render(monkeypatch, "Error while opening encoder")
    with pytest.raises(RenderFailedError) as error:
        run_export(tools, source, out, 1000, 4000, 1)
    assert error.value.code == "FFMPEG_FAILED"
    assert "encoder" in error.value.detail
    assert leftovers(out) == []


def test_missing_ffmpeg_is_reported_as_unavailable(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    broken = MediaTools(
        ffmpeg=tmp_path / "missing-ffmpeg.exe",
        ffprobe=tools.ffprobe,
        ffmpeg_version=tools.ffmpeg_version,
        ffprobe_version=tools.ffprobe_version,
    )
    with pytest.raises(MediaToolsUnavailableError):
        run_export(broken, source, out, 1000, 4000, 1)
    assert leftovers(out) == []


# --- request validation ---------------------------------------------------------------------------


def test_interval_beyond_the_source_is_rejected(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    with pytest.raises(IntervalError):
        run_export(tools, source, out, 1000, 12001, 1)
    assert run_export(tools, source, out, 11000, 12000, 1).video_frame_count == 30
    assert len(leftovers(out)) == 1


def test_output_directory_must_exist(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    with pytest.raises(OutputDirUnavailableError) as error:
        run_export(tools, source, tmp_path / "missing", 1000, 4000, 1)
    assert error.value.code == "OUTPUT_DIR_UNAVAILABLE"
    with pytest.raises(OutputDirUnavailableError):
        run_export(tools, source, source, 1000, 4000, 1)  # a file, not a directory


def test_malformed_requests(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    with pytest.raises(ValueError, match="source"):
        run_export(tools, Path("color_gop.mp4"), out, 1000, 4000, 1)
    with pytest.raises(ValueError, match="output_dir"):
        run_export(tools, source, Path("out"), 1000, 4000, 1)
    for bad in ("not-a-uuid", "", "{" + str(uuid.uuid4()) + "}"):
        with pytest.raises(ValueError, match="task_id"):
            run_export(tools, source, out, 1000, 4000, 1, task_id=bad)
    assert leftovers(out) == []


def test_single_and_all_audio_selection_are_mutually_exclusive(
    tools: MediaTools, tmp_path: Path
) -> None:
    source = copy_fixture(tmp_path, "two_tracks.mkv")
    out = output_dir(tmp_path)
    with pytest.raises(ValueError, match="mutually exclusive"):
        run_export(tools, source, out, 1000, 4000, 1, audio_indexes=(1, 2))
    with pytest.raises(ValueError, match="mutually exclusive"):
        run_export(tools, source, out, 1000, 4000, 1, audio_indexes=(1,))
    assert leftovers(out) == []


def test_audio_stream_indexes_must_be_unique(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "two_tracks.mkv")
    out = output_dir(tmp_path)
    with pytest.raises(ValueError, match="duplicates"):
        run_export(tools, source, out, 1000, 4000, None, audio_indexes=(1, 2, 1))
    assert leftovers(out) == []


def test_task_id_cannot_be_reused_while_its_temp_file_exists(
    tools: MediaTools, tmp_path: Path
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    task_id = str(uuid.uuid4())
    leftover = out / f".clipforge-{task_id}.part"
    leftover.write_bytes(b"not ours to touch")
    with pytest.raises(ValueError, match="task_id"):
        run_export(tools, source, out, 1000, 4000, 1, task_id=task_id)
    assert leftover.read_bytes() == b"not ours to touch"
