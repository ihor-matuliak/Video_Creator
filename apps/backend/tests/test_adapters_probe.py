"""Media tools and source probe on real FFmpeg/FFprobe and the generated fixtures.

Fixtures come from ``npm run fixtures`` (test-results/fixtures). Extra files are
generated with ffmpeg into tmp_path; the shared fixtures are never modified.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from clipforge.adapters.ffprobe import (
    HDR_UNSUPPORTED,
    ProbeTimeoutError,
    SourceNoVideoError,
    SourceProbe,
    SourceUnavailableError,
    SourceUnreadableError,
    probe_source,
)
from clipforge.adapters.media_tools import (
    ENV_FFMPEG_PATH,
    ENV_FFPROBE_PATH,
    MediaTools,
    MediaToolsUnavailableError,
    read_tool_version,
    resolve_media_tools,
    run_short,
)

from .support import BACKEND_DIR

FIXTURES = BACKEND_DIR.parents[1] / "test-results" / "fixtures"


@pytest.fixture(scope="module")
def tools() -> MediaTools:
    return resolve_media_tools("test", os.environ)


@pytest.fixture(scope="module")
def fixture_info() -> dict[str, object]:
    info: dict[str, object] = json.loads(fixture("fixture-info.json").read_text("utf-8"))
    return info


def fixture(name: str) -> Path:
    path = FIXTURES / name
    if not path.is_file():
        pytest.fail(f"Fixture {name} is missing: run `npm run fixtures` first")
    return path


def ffmpeg(tools: MediaTools, *args: str) -> None:
    argv = [str(tools.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args]
    completed = run_short(argv, timeout_s=120)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


def probe(tools: MediaTools, path: Path) -> SourceProbe:
    return probe_source(tools.ffprobe, path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- media tools -------------------------------------------------------------------


def test_tools_are_absolute_and_report_versions(tools: MediaTools) -> None:
    assert tools.ffmpeg.is_absolute()
    assert tools.ffprobe.is_absolute()
    assert tools.ffmpeg_version
    assert tools.ffprobe_version
    first_line = run_short([str(tools.ffprobe), "-version"], timeout_s=15).stdout.decode()
    assert first_line.startswith(f"ffprobe version {tools.ffprobe_version} ")


def test_test_mode_env_paths_take_precedence(tools: MediaTools) -> None:
    env = {ENV_FFMPEG_PATH: str(tools.ffmpeg), ENV_FFPROBE_PATH: str(tools.ffprobe)}
    resolved = resolve_media_tools("test", env, which=lambda name: None)
    assert (resolved.ffmpeg, resolved.ffprobe) == (tools.ffmpeg, tools.ffprobe)


def test_test_mode_env_path_must_exist(tools: MediaTools, tmp_path: Path) -> None:
    env = {ENV_FFMPEG_PATH: str(tools.ffmpeg), ENV_FFPROBE_PATH: str(tmp_path / "ffprobe.exe")}
    with pytest.raises(MediaToolsUnavailableError) as error:
        resolve_media_tools("test", env, which=lambda name: str(tools.ffprobe))
    assert error.value.code == "MEDIA_TOOLS_UNAVAILABLE"
    assert error.value.message == "ffprobe не знайдено"


def test_test_mode_env_path_must_be_absolute(tools: MediaTools) -> None:
    env = {ENV_FFPROBE_PATH: "ffprobe.exe"}
    with pytest.raises(MediaToolsUnavailableError):
        resolve_media_tools("test", env, which=lambda name: str(tools.ffmpeg))


def test_production_ignores_env_paths_and_uses_which(tools: MediaTools, tmp_path: Path) -> None:
    found = {"ffmpeg": str(tools.ffmpeg), "ffprobe": str(tools.ffprobe)}
    env = {ENV_FFPROBE_PATH: str(tmp_path / "missing.exe")}
    resolved = resolve_media_tools("production", env, which=found.get)
    assert resolved.ffprobe == tools.ffprobe
    assert resolved.ffprobe_version == tools.ffprobe_version


def test_missing_tools_are_unavailable() -> None:
    with pytest.raises(MediaToolsUnavailableError):
        resolve_media_tools("production", {}, which=lambda name: None)


def test_non_tool_executable_has_no_version(tmp_path: Path) -> None:
    not_a_tool = tmp_path / "ffprobe.exe"
    not_a_tool.write_bytes(b"not an executable")
    with pytest.raises(MediaToolsUnavailableError):
        read_tool_version(not_a_tool)


# --- probe of the shared fixtures ---------------------------------------------------


def test_color_gop(tools: MediaTools, fixture_info: dict[str, object]) -> None:
    path = fixture("color_gop.mp4")
    before = sha256(path)
    result = probe(tools, path)
    assert result.duration_ms == fixture_info["source_duration_ms"] == 12000
    assert result.origin_pts == 0
    assert result.origin_ms == 0
    assert result.video_stream_index == 0
    assert result.video_codec == "h264"
    assert (result.width, result.height) == (320, 180)
    assert result.rotation == 0
    assert result.hdr is False
    assert result.unsupported_reason is None
    assert "mp4" in result.format_name
    assert len(result.audio_streams) == 1
    audio = result.audio_streams[0]
    assert (audio.index, audio.codec, audio.channels) == (1, "aac", 1)
    assert audio.start_offset_ms == 0
    assert audio.default is True
    assert audio.language is None  # "und" is not a language
    assert sha256(path) == before


def test_audio_offset(tools: MediaTools, fixture_info: dict[str, object]) -> None:
    result = probe(tools, fixture("audio_offset.mkv"))
    assert result.origin_ms == 0
    assert result.duration_ms == 12000
    assert [a.index for a in result.audio_streams] == [1]
    assert result.audio_streams[0].start_offset_ms == fixture_info["audio_offset_ms"] == 1500


def test_two_tracks(tools: MediaTools) -> None:
    result = probe(tools, fixture("two_tracks.mkv"))
    assert result.duration_ms == 12000
    assert [a.index for a in result.audio_streams] == [1, 2]
    assert [a.language for a in result.audio_streams] == ["ukr", "eng"]
    assert [a.default for a in result.audio_streams] == [True, False]
    assert [a.codec for a in result.audio_streams] == ["pcm_s16le", "pcm_s16le"]
    assert [a.start_offset_ms for a in result.audio_streams] == [0, 0]


def test_no_audio(tools: MediaTools) -> None:
    result = probe(tools, fixture("no_audio.mp4"))
    assert result.audio_streams == ()
    assert result.duration_ms == 12000
    assert result.video_stream_index == 0


def test_vfr_duration_from_packets(tools: MediaTools) -> None:
    result = probe(tools, fixture("vfr.mkv"))
    assert result.origin_ms == 0
    assert 8900 <= result.duration_ms <= 9000
    assert result.audio_streams == ()


def test_cyrillic_fixture(tools: MediaTools) -> None:
    path = fixture("Київ запис.mp4")
    assert probe(tools, path) == probe(tools, fixture("color_gop.mp4"))


@pytest.mark.parametrize(
    "name",
    ["it's & %PATH% ;, [1] (copy).mp4", "-i.mp4", "file;name ^ `x`.mp4", "Ünïcødé 日本.mp4"],
)
def test_unusual_file_names(tools: MediaTools, tmp_path: Path, name: str) -> None:
    target = tmp_path / name
    shutil.copyfile(fixture("color_gop.mp4"), target)
    result = probe(tools, target)
    assert result.duration_ms == 12000
    assert [a.index for a in result.audio_streams] == [1]


# --- generated sources -------------------------------------------------------------


@pytest.mark.parametrize("container", ["mkv", "mp4"])
def test_nonzero_start_keeps_duration_and_reports_origin(
    tools: MediaTools, tmp_path: Path, container: str
) -> None:
    target = tmp_path / f"shifted.{container}"
    ffmpeg(
        tools,
        "-i",
        str(fixture("color_gop.mp4")),
        "-c",
        "copy",
        "-output_ts_offset",
        "10",
        str(target),
    )
    result = probe(tools, target)
    assert result.origin_ms == 10000
    assert result.origin_seconds == 10
    assert result.duration_ms == 12000
    assert len(result.audio_streams) == 1
    offset = result.audio_streams[0].start_offset_ms
    assert offset is not None
    assert abs(offset) < 50  # only AAC priming, measured from the video origin


def test_rotation_is_clockwise(tools: MediaTools, tmp_path: Path) -> None:
    target = tmp_path / "rotated.mp4"
    ffmpeg(
        tools,
        "-display_rotation",
        "90",
        "-i",
        str(fixture("no_audio.mp4")),
        "-c",
        "copy",
        str(target),
    )
    assert probe(tools, target).rotation == 270  # 90 counter-clockwise


def test_hdr_is_flagged_unsupported(tools: MediaTools, tmp_path: Path) -> None:
    target = tmp_path / "hdr.mp4"
    # FFmpeg 8 takes colour tags from frame properties, so set them with setparams.
    ffmpeg(
        tools,
        "-f",
        "lavfi",
        "-i",
        "color=c=gray:s=64x64:r=30:d=0.5",
        "-vf",
        "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(target),
    )
    result = probe(tools, target)
    assert result.hdr is True
    assert result.unsupported_reason == HDR_UNSUPPORTED


def test_audio_only_file_has_no_video(tools: MediaTools, tmp_path: Path) -> None:
    target = tmp_path / "audio.wav"
    ffmpeg(
        tools, "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1", str(target)
    )
    with pytest.raises(SourceNoVideoError) as error:
        probe(tools, target)
    assert error.value.code == "SOURCE_NO_VIDEO"


# --- errors --------------------------------------------------------------------------


def test_missing_file(tools: MediaTools, tmp_path: Path) -> None:
    with pytest.raises(SourceUnavailableError) as error:
        probe(tools, tmp_path / "missing.mp4")
    assert error.value.code == "SOURCE_UNAVAILABLE"
    assert str(tmp_path) not in error.value.message


def test_directory_is_not_a_source(tools: MediaTools, tmp_path: Path) -> None:
    with pytest.raises(SourceUnavailableError):
        probe(tools, tmp_path)


def test_relative_path_is_rejected(tools: MediaTools) -> None:
    with pytest.raises(ValueError, match="absolute"):
        probe_source(tools.ffprobe, Path("color_gop.mp4"))


def test_random_bytes_are_unreadable(tools: MediaTools, tmp_path: Path) -> None:
    target = tmp_path / "random.mp4"
    target.write_bytes(bytes((i * 151 + 7) % 256 for i in range(65536)))
    with pytest.raises(SourceUnreadableError) as error:
        probe(tools, target)
    assert error.value.code == "SOURCE_UNREADABLE"
    assert str(tmp_path) not in error.value.message


def test_truncated_mp4_is_unreadable(tools: MediaTools, tmp_path: Path) -> None:
    data = fixture("color_gop.mp4").read_bytes()
    assert data.index(b"moov") > len(data) // 2  # index sits at the end of the file
    target = tmp_path / "truncated.mp4"
    target.write_bytes(data[: len(data) // 2])
    with pytest.raises(SourceUnreadableError):
        probe(tools, target)


def test_undecodable_video_fails_decode_check(tools: MediaTools, tmp_path: Path) -> None:
    data = bytearray(fixture("no_audio.mp4").read_bytes())
    payload_start = data.index(b"mdat") + 4
    payload_end = data.index(b"moov") - 4
    data[payload_start:payload_end] = bytes(payload_end - payload_start)
    target = tmp_path / "zeroed.mp4"
    target.write_bytes(bytes(data))
    with pytest.raises(SourceUnreadableError) as error:
        probe(tools, target)
    assert error.value.message == "Не вдалося декодувати відео"


def test_timeout_is_typed(tools: MediaTools) -> None:
    with pytest.raises(ProbeTimeoutError) as error:
        probe_source(tools.ffprobe, fixture("color_gop.mp4"), timeout_s=0.000001)
    assert error.value.code == "PROBE_TIMEOUT"
