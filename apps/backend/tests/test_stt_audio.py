"""STT audio window on real FFmpeg and copies of the generated fixtures (docs/05 sections 1, 2, 4).

Fixtures come from ``npm run fixtures`` (test-results/fixtures); every test works on a copy in
``tmp_path`` and never touches the shared files. The extracted WAV files are decoded again here,
so the checks look at signals (silence, tone, frequency) and at sample counts, not only at the
container metadata.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import shutil
import struct
import threading
import time
import uuid
from array import array
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from clipforge.adapters import stt_audio
from clipforge.adapters.ffprobe import probe_source
from clipforge.adapters.media_tools import MediaTools, MediaToolsUnavailableError, run_short
from clipforge.adapters.process import FfmpegProgress, ProcessResult, run_process
from clipforge.adapters.stt_audio import (
    NoAudioStreamError,
    SttAudioRequest,
    SttAudioResult,
    extract_stt_audio,
)
from clipforge.domain.chunking import MAX_UPLOAD_MS, estimate_payload
from clipforge.domain.interval import IntervalError
from clipforge.services import export_engine
from clipforge.services.export_engine import (
    DiskFullError,
    ExportCancelledError,
    ExportError,
    InvalidAudioStreamError,
    OutputDirUnavailableError,
    OutputInvalidError,
    RenderFailedError,
    SourceChangedError,
)

from .support import fixture_file

RATE = 16000
Samples = array[int]


@pytest.fixture
def tools(media_tools: MediaTools) -> MediaTools:
    return media_tools


# --- helpers ----------------------------------------------------------------------------------


def copy_fixture(tmp_path: Path, name: str, as_name: str | None = None) -> Path:
    sources = tmp_path / "sources"
    sources.mkdir(exist_ok=True)
    target = sources / (as_name or name)
    shutil.copyfile(fixture_file(name), target)
    return target


def output_dir(tmp_path: Path, name: str = "audio") -> Path:
    target = tmp_path / name
    target.mkdir(exist_ok=True)
    return target


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def leftovers(out: Path) -> list[str]:
    return sorted(os.listdir(out))


def make_request(
    source: Path, out: Path, index: int, start_ms: int, end_ms: int, task_id: str | None = None
) -> SttAudioRequest:
    return SttAudioRequest(
        task_id=str(uuid.uuid4()) if task_id is None else task_id,
        source=source,
        output_dir=out,
        audio_stream_index=index,
        upload_start_ms=start_ms,
        upload_end_ms=end_ms,
    )


def run(
    tools: MediaTools,
    source: Path,
    out: Path,
    index: int,
    start_ms: int,
    end_ms: int,
    *,
    task_id: str | None = None,
) -> SttAudioResult:
    return extract_stt_audio(tools, make_request(source, out, index, start_ms, end_ms, task_id))


def ffmpeg_output(tools: MediaTools, *args: str) -> bytes:
    argv = [str(tools.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    completed = run_short(argv, timeout_s=120)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    return completed.stdout


def make_media(tools: MediaTools, target: Path, *args: str) -> Path:
    ffmpeg_output(tools, "-y", *args, str(target))
    return target


def pcm(tools: MediaTools, path: Path) -> Samples:
    """Decoded samples of the file as they are stored (the profile is checked separately)."""
    data = ffmpeg_output(tools, "-i", str(path), "-map", "0:a:0", "-f", "s16le", "-")
    samples = array("h")
    samples.frombytes(data)
    return samples


def rms(samples: Sequence[int], start_s: float, end_s: float) -> float:
    window = samples[int(start_s * RATE) : int(end_s * RATE)]
    assert len(window) > 0
    return math.sqrt(sum(sample * sample for sample in window) / len(window)) / 32768


def frequency(samples: Sequence[int], start_s: float, end_s: float) -> float:
    window = samples[int(start_s * RATE) : int(end_s * RATE)]
    crossings = sum(1 for a, b in itertools.pairwise(window) if (a < 0) != (b < 0))
    return crossings / 2 / (len(window) / RATE)


def first_loud_ms(samples: Sequence[int], level: float = 0.02) -> float:
    """Time of the first sample louder than ``level`` (full scale 1.0), in milliseconds."""
    limit = level * 32768
    for position, sample in enumerate(samples):
        if abs(sample) > limit:
            return position * 1000 / RATE
    raise AssertionError("the file is silent")


def ffprobe_json(tools: MediaTools, path: Path) -> dict[str, object]:
    argv = [
        str(tools.ffprobe), "-v", "error", "-show_format", "-show_streams",
        "-of", "json", "-i", f"file:{path}",
    ]  # fmt: skip
    completed = run_short(argv, timeout_s=60)
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    document: dict[str, object] = json.loads(completed.stdout)
    return document


def audio_stream(tools: MediaTools, path: Path) -> dict[str, object]:
    streams = ffprobe_json(tools, path)["streams"]
    assert isinstance(streams, list)
    assert len(streams) == 1
    stream: dict[str, object] = streams[0]
    return stream


def container_format(tools: MediaTools, path: Path) -> str:
    container = ffprobe_json(tools, path)["format"]
    assert isinstance(container, dict)
    return str(container["format_name"])


def container_duration_ms(tools: MediaTools, path: Path) -> float:
    container = ffprobe_json(tools, path)["format"]
    assert isinstance(container, dict)
    return float(container["duration"]) * 1000


def source_duration_ms(tools: MediaTools, source: Path) -> int:
    return probe_source(tools.ffprobe, source).duration_ms


def spy_renders(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the argv of every render and delegate to the real runner."""
    commands: list[list[str]] = []
    real: Callable[..., ProcessResult] = run_process

    def spy(args: Sequence[str], **kwargs: object) -> ProcessResult:
        if "-filter_complex" in args:
            commands.append(list(args))
        return real(args, **kwargs)

    monkeypatch.setattr(export_engine, "run_process", spy)
    return commands


# --- profile and duration ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "index", "start_ms", "end_ms"),
    [
        ("color_gop.mp4", 1, 2000, 7000),
        ("color_gop.mp4", 1, 1234, 6789),
        ("audio_offset.mkv", 1, 0, 5000),
        ("two_tracks.mkv", 2, 3217, 9001),
    ],
)
def test_output_is_pcm_wav_mono_16k_with_the_window_duration(
    tools: MediaTools, tmp_path: Path, name: str, index: int, start_ms: int, end_ms: int
) -> None:
    source = copy_fixture(tmp_path, name)
    out = output_dir(tmp_path)
    task_id = str(uuid.uuid4())
    original = sha256(fixture_file(name))

    result = run(tools, source, out, index, start_ms, end_ms, task_id=task_id)

    assert result.path == out / f"{task_id}.wav"
    assert leftovers(out) == [f"{task_id}.wav"]
    assert result.size_bytes == result.path.stat().st_size
    assert result.size_bytes == estimate_payload(end_ms - start_ms).audio_bytes
    assert (result.upload_start_ms, result.upload_end_ms) == (start_ms, end_ms)
    assert result.ffmpeg_version == tools.ffmpeg_version
    assert result.ffprobe_version == tools.ffprobe_version
    stream = audio_stream(tools, result.path)
    assert stream["codec_name"] == "pcm_s16le"
    assert container_format(tools, result.path) == "wav"
    assert stream["channels"] == 1
    assert stream["sample_rate"] == "16000"
    assert stream["sample_fmt"] == "s16"
    assert abs(container_duration_ms(tools, result.path) - (end_ms - start_ms)) <= 1
    assert len(pcm(tools, result.path)) == 16 * (end_ms - start_ms)
    assert sha256(source) == original


def test_a_full_upload_window_is_exactly_the_estimated_wav(
    tools: MediaTools, tmp_path: Path
) -> None:
    """310 s: ``44 + 310 * 16000 * 2`` bytes, no LIST chunk, as ``domain.chunking`` estimates."""
    source = make_media(
        tools, tmp_path / "long.mkv",
        "-f", "lavfi", "-i", "color=c=black:s=64x64:r=5:d=320",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=320",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-c:a", "aac",
    )  # fmt: skip
    assert source_duration_ms(tools, source) >= 315_000
    result = run(tools, source, output_dir(tmp_path), 1, 5000, 5000 + MAX_UPLOAD_MS)

    data_bytes = 310 * RATE * 2
    assert result.size_bytes == 44 + data_bytes == 9_920_044
    assert result.size_bytes == estimate_payload(MAX_UPLOAD_MS).audio_bytes
    header = result.path.read_bytes()[:44]
    assert header[0:4] == b"RIFF"
    assert struct.unpack("<I", header[4:8])[0] == 36 + data_bytes
    assert header[8:16] == b"WAVEfmt "
    assert header[36:40] == b"data"  # right after the fmt chunk: nothing between them
    assert struct.unpack("<I", header[40:44])[0] == data_bytes


def test_nothing_but_audio_is_written(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "two_tracks.mkv")
    result = run(tools, source, output_dir(tmp_path), 1, 1000, 4000)
    document = ffprobe_json(tools, result.path)
    streams = document["streams"]
    assert isinstance(streams, list)
    assert [stream["codec_type"] for stream in streams] == ["audio"]
    assert document.get("chapters", []) == []


def test_progress_and_temp_name(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    task_id = str(uuid.uuid4())
    blocks: list[FfmpegProgress] = []
    names: list[list[str]] = []

    def watch(progress: FfmpegProgress) -> None:
        blocks.append(progress)
        names.append(leftovers(out))

    result = extract_stt_audio(
        tools, make_request(source, out, 1, 1000, 6000, task_id), on_progress=watch
    )

    assert blocks
    assert blocks[-1].ended is True
    assert blocks[-1].out_time_us is not None
    assert names[0] == [f".clipforge-{task_id}.part"]
    assert leftovers(out) == [result.path.name]


# --- timeline: late audio, canonical zero -----------------------------------------------------


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "tone_at_ms"),
    [(0, 5000, 1500), (500, 3500, 1000), (1500, 4000, 0)],
    ids=["window-0-5000", "window-500-3500", "window-starts-with-the-audio"],
)
def test_late_audio_start_is_silence_then_tone(
    tools: MediaTools, tmp_path: Path, start_ms: int, end_ms: int, tone_at_ms: int
) -> None:
    # audio_offset.mkv: a 1000 Hz tone that starts 1500 ms after the first video frame.
    source = copy_fixture(tmp_path, "audio_offset.mkv")
    result = run(tools, source, output_dir(tmp_path), 1, start_ms, end_ms)
    samples = pcm(tools, result.path)
    seconds = (end_ms - start_ms) / 1000
    tone_at = tone_at_ms / 1000

    if tone_at_ms > 0:
        assert max(abs(sample) for sample in samples[: int((tone_at - 0.002) * RATE)]) <= 4
        assert rms(samples, 0.0, tone_at - 0.01) < 0.0005
    assert abs(first_loud_ms(samples) - tone_at_ms) <= 2
    assert rms(samples, tone_at + 0.05, seconds - 0.05) > 0.05
    assert abs(frequency(samples, tone_at + 0.1, seconds - 0.1) - 1000) <= 20


def test_window_that_ends_before_the_audio_starts_is_silent(
    tools: MediaTools, tmp_path: Path
) -> None:
    source = copy_fixture(tmp_path, "audio_offset.mkv")
    result = run(tools, source, output_dir(tmp_path), 1, 0, 1000)
    samples = pcm(tools, result.path)
    assert len(samples) == RATE
    assert max(abs(sample) for sample in samples) == 0


def test_audio_before_canonical_zero_is_dropped(tools: MediaTools, tmp_path: Path) -> None:
    """Audio runs from container time 0, the first video frame is at 1 s: that is canonical zero."""
    source = make_media(
        tools, tmp_path / "early-audio.mkv",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=5",
        "-itsoffset", "1", "-f", "lavfi", "-i", "color=c=black:s=160x90:r=30:d=5",
        "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]",
        "-map", "2:v", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-c:a", "pcm_s16le",
    )  # fmt: skip
    info = probe_source(tools.ffprobe, source)
    assert info.origin_ms == 1000
    assert info.audio_streams[0].start_offset_ms == -1000
    result = run(tools, source, output_dir(tmp_path), 1, 0, 3000)
    samples = pcm(tools, result.path)
    assert len(samples) == 3 * RATE
    # The 440 Hz first second lies before canonical zero; the window starts on the 1000 Hz part.
    assert abs(frequency(samples, 0.0, 0.5) - 1000) <= 20
    assert abs(frequency(samples, 0.0, 3.0) - 1000) <= 10
    assert rms(samples, 0.0, 3.0) > 0.05


# --- end of the source ------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["audio_offset.mkv", "color_gop.mp4"])
def test_window_ending_at_the_source_end(tools: MediaTools, tmp_path: Path, name: str) -> None:
    source = copy_fixture(tmp_path, name)
    end_ms = source_duration_ms(tools, source)
    assert end_ms == 12000
    result = run(tools, source, output_dir(tmp_path), 1, end_ms - 5000, end_ms)
    samples = pcm(tools, result.path)
    assert len(samples) == 5 * RATE
    assert abs(container_duration_ms(tools, result.path) - 5000) <= 1
    assert rms(samples, 4.8, 4.95) > 0.05  # the signal runs to the end of the window


def test_whole_source_is_one_window(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "audio_offset.mkv")
    end_ms = source_duration_ms(tools, source)
    result = run(tools, source, output_dir(tmp_path), 1, 0, end_ms)
    samples = pcm(tools, result.path)
    assert len(samples) == 16 * end_ms
    assert abs(first_loud_ms(samples) - 1500) <= 2
    assert rms(samples, end_ms / 1000 - 0.2, end_ms / 1000 - 0.05) > 0.05


def test_audio_that_ends_before_the_video_is_padded_with_silence(
    tools: MediaTools, tmp_path: Path
) -> None:
    source = make_media(
        tools, tmp_path / "short-audio.mkv",
        "-f", "lavfi", "-i", "color=c=black:s=160x90:r=30:d=6",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-c:a", "pcm_s16le",
    )  # fmt: skip
    end_ms = source_duration_ms(tools, source)
    assert end_ms >= 5900
    result = run(tools, source, output_dir(tmp_path), 1, 2000, end_ms)
    samples = pcm(tools, result.path)
    assert len(samples) == 16 * (end_ms - 2000)
    assert rms(samples, 0.1, 1.9) > 0.05  # source 2.1-3.9 s
    assert abs(frequency(samples, 0.1, 1.9) - 440) <= 10
    assert rms(samples, 2.1, (end_ms - 2000) / 1000) == 0  # the end is silence, not stretched


# --- stream choice ----------------------------------------------------------------------------


@pytest.mark.parametrize(("stream", "expected_hz"), [(2, 880), (1, 220)])
def test_two_tracks_extracts_the_selected_stream(
    tools: MediaTools, tmp_path: Path, stream: int, expected_hz: int
) -> None:
    source = copy_fixture(tmp_path, "two_tracks.mkv")
    result = run(tools, source, output_dir(tmp_path), stream, 2000, 7000)
    samples = pcm(tools, result.path)
    assert abs(frequency(samples, 0.3, 4.7) - expected_hz) <= 15
    assert rms(samples, 0.3, 4.7) > 0.05


def test_source_without_audio_raises_a_typed_error(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "no_audio.mp4")
    out = output_dir(tmp_path)
    original = sha256(source)
    with pytest.raises(NoAudioStreamError) as error:
        run(tools, source, out, 1, 1000, 4000)
    assert error.value.code == "SOURCE_NO_AUDIO"
    assert isinstance(error.value, ExportError)
    assert str(tmp_path) not in error.value.message
    assert leftovers(out) == []
    assert sha256(source) == original


@pytest.mark.parametrize("index", [0, 3, 99, -1], ids=["video", "missing", "far", "negative"])
def test_bad_stream_index_raises_a_typed_error(
    tools: MediaTools, tmp_path: Path, index: int
) -> None:
    source = copy_fixture(tmp_path, "two_tracks.mkv")  # video 0, audio 1 and 2
    out = output_dir(tmp_path)
    with pytest.raises(InvalidAudioStreamError) as error:
        run(tools, source, out, index, 1000, 4000)
    assert error.value.code == "INVALID_AUDIO_STREAM"
    assert not isinstance(error.value, NoAudioStreamError)
    assert str(tmp_path) not in error.value.message
    assert leftovers(out) == []


# --- profile: downmix -------------------------------------------------------------------------


def test_stereo_downmix_keeps_the_signal(tools: MediaTools, tmp_path: Path) -> None:
    """Identical left and right channels at 44.1 kHz: a standard downmix keeps the level."""
    source = make_media(
        tools, tmp_path / "stereo.mkv",
        "-f", "lavfi", "-i", "color=c=black:s=160x90:r=30:d=6",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=6",
        "-filter_complex", "[1:a]pan=stereo|c0=c0|c1=c0[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-c:a", "pcm_s16le",
    )  # fmt: skip
    result = run(tools, source, output_dir(tmp_path), 1, 500, 5500)
    samples = pcm(tools, result.path)
    assert len(samples) == 5 * RATE
    assert 0.07 < rms(samples, 0.2, 4.8) < 0.11  # one channel of the sine: 0.125 / sqrt(2)
    assert abs(frequency(samples, 0.2, 4.8) - 440) <= 10


def test_surround_centre_channel_is_audible_in_the_mono_mix(
    tools: MediaTools, tmp_path: Path
) -> None:
    source = make_media(
        tools, tmp_path / "surround.mkv",
        "-f", "lavfi", "-i", "color=c=black:s=160x90:r=30:d=6",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6",
        "-filter_complex", "[1:a]pan=5.1|FC=c0[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-c:a", "pcm_s16le",
    )  # fmt: skip
    assert probe_source(tools.ffprobe, source).audio_streams[0].channels == 6
    result = run(tools, source, output_dir(tmp_path), 1, 500, 5500)
    samples = pcm(tools, result.path)
    # FFmpeg's standard 5.1 -> mono matrix gives the centre about 0.29 of its level (0.0884 here).
    assert 0.02 < rms(samples, 0.2, 4.8) < 0.035
    assert abs(frequency(samples, 0.2, 4.8) - 440) <= 10


# --- seek policy (ADR 018) --------------------------------------------------------------------


def shifted(tools: MediaTools, tmp_path: Path, base: str, name: str, *args: str) -> Path:
    return make_media(
        tools, tmp_path / "sources" / name,
        "-i", str(copy_fixture(tmp_path, base)), "-c", "copy", "-output_ts_offset", "10", *args,
    )  # fmt: skip


def test_nonzero_origin_matroska_uses_preseek_and_keeps_the_timeline(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = shifted(tools, tmp_path, "audio_offset.mkv", "shifted.mkv")
    info = probe_source(tools.ffprobe, source)
    assert info.origin_ms == 10000
    assert info.audio_streams[0].start_offset_ms == 1500
    commands = spy_renders(monkeypatch)

    early = run(tools, source, output_dir(tmp_path, "a"), 1, 500, 3500)
    deep = run(tools, source, output_dir(tmp_path, "b"), 1, 6000, 9000)

    assert early.seek_mode == deep.seek_mode == "preseek"
    assert ["-ss" in command for command in commands] == [True, True]
    assert commands[1][commands[1].index("-ss") + 1] == "3.000"
    samples = pcm(tools, early.path)
    assert abs(first_loud_ms(samples) - 1000) <= 2  # 1500 ms into the source, 500 ms window start
    assert abs(frequency(samples, 1.2, 2.9) - 1000) <= 20
    deep_samples = pcm(tools, deep.path)
    assert rms(deep_samples, 0.0, 3.0) > 0.05
    assert abs(frequency(deep_samples, 0.1, 2.9) - 1000) <= 20


def test_preseek_and_full_decode_give_the_same_samples(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copy_fixture(tmp_path, "two_tracks.mkv")
    fast = run(tools, source, output_dir(tmp_path, "fast"), 2, 5000, 9000)
    monkeypatch.setattr(stt_audio, "PRESEEK_FORMATS", frozenset())
    slow = run(tools, source, output_dir(tmp_path, "slow"), 2, 5000, 9000)
    assert (fast.seek_mode, slow.seek_mode) == ("preseek", "full_decode")
    assert pcm(tools, fast.path) == pcm(tools, slow.path)


def test_mpegts_with_nonzero_start_is_decoded_from_the_start(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = shifted(tools, tmp_path, "color_gop.mp4", "shifted.ts", "-f", "mpegts")
    assert probe_source(tools.ffprobe, source).origin_ms >= 10000
    commands = spy_renders(monkeypatch)
    result = run(tools, source, output_dir(tmp_path), 1, 4120, 7880)
    assert result.seek_mode == "full_decode"
    assert "-ss" not in commands[0]
    samples = pcm(tools, result.path)
    assert len(samples) == 16 * 3760
    assert rms(samples, 0.1, 3.6) > 0.05
    assert abs(frequency(samples, 0.1, 3.6) - 440) <= 10


# --- cancel -----------------------------------------------------------------------------------


def test_cancel_before_start(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ExportCancelledError) as error:
        extract_stt_audio(tools, make_request(source, out, 1, 1000, 4000), cancel=cancel)
    assert error.value.code == "EXPORT_CANCELLED"
    assert leftovers(out) == []


def test_cancel_during_render_stops_the_process_and_leaves_no_files(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    original = sha256(source)
    real: Callable[..., ProcessResult] = run_process

    def paced(args: Sequence[str], **kwargs: object) -> ProcessResult:
        argv = list(args)
        if "-filter_complex" in argv:
            argv.insert(argv.index("-i"), "-re")  # read at real time: the 12 s render takes 12 s
        return real(argv, **kwargs)

    monkeypatch.setattr(export_engine, "run_process", paced)
    cancel = threading.Event()
    blocks: list[FfmpegProgress] = []

    def stop(progress: FfmpegProgress) -> None:
        blocks.append(progress)
        cancel.set()

    started = time.monotonic()
    with pytest.raises(ExportCancelledError):
        extract_stt_audio(
            tools, make_request(source, out, 1, 0, 12000), cancel=cancel, on_progress=stop
        )
    elapsed = time.monotonic() - started

    assert blocks
    assert blocks[0].out_time_us is not None
    assert blocks[0].out_time_us < 11_000_000  # it was running, and far from finished
    assert elapsed < 10
    assert leftovers(out) == []  # the temp file is deletable: no process holds it
    assert sha256(source) == original


# --- failures and invalid output --------------------------------------------------------------


def corrupt_render(monkeypatch: pytest.MonkeyPatch, damage: Callable[[Path], None]) -> None:
    real: Callable[..., ProcessResult] = run_process

    def run_then_damage(args: Sequence[str], **kwargs: object) -> ProcessResult:
        result = real(args, **kwargs)
        if "-filter_complex" in args:
            damage(Path(args[-1].removeprefix("file:")))
        return result

    monkeypatch.setattr(export_engine, "run_process", run_then_damage)


def test_invalid_outputs_leave_neither_final_nor_temp_file(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    wrong_profile = make_media(
        tools, tmp_path / "wrong-profile.wav",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=3",
        "-ac", "2", "-c:a", "pcm_s16le", "-f", "wav",
    )  # fmt: skip
    wrong_length = make_media(
        tools, tmp_path / "wrong-length.wav",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=3.5",
        "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav",
    )  # fmt: skip
    wrong_sample_format = make_media(
        tools, tmp_path / "wrong-sample-format.wav",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=3",
        "-ac", "1", "-c:a", "pcm_s24le", "-f", "wav",
    )  # fmt: skip
    wrong_container = make_media(
        tools, tmp_path / "wrong-container.raw",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=3",
        "-ac", "1", "-c:a", "flac", "-f", "flac",
    )  # fmt: skip

    def truncate(path: Path) -> None:
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 3])

    def garbage(path: Path) -> None:
        path.write_bytes(b"not audio at all" * 100)

    def stereo_44k(path: Path) -> None:
        path.write_bytes(wrong_profile.read_bytes())

    def half_second_too_long(path: Path) -> None:
        path.write_bytes(wrong_length.read_bytes())

    def pcm_24_bit(path: Path) -> None:
        path.write_bytes(wrong_sample_format.read_bytes())

    def flac_container(path: Path) -> None:
        path.write_bytes(wrong_container.read_bytes())

    for damage in (
        truncate, garbage, stereo_44k, half_second_too_long, pcm_24_bit, flac_container
    ):  # fmt: skip
        out = output_dir(tmp_path, f"out-{damage.__name__}")
        with monkeypatch.context() as patch:
            corrupt_render(patch, damage)
            with pytest.raises(OutputInvalidError) as error:
                run(tools, source, out, 1, 1000, 4000)
        assert error.value.code == "OUTPUT_INVALID", damage.__name__
        assert str(tmp_path) not in error.value.message
        assert leftovers(out) == [], damage.__name__


def fake_render(monkeypatch: pytest.MonkeyPatch, stderr: str) -> None:
    def fake(args: Sequence[str], **kwargs: object) -> ProcessResult:
        return ProcessResult(exit_code=1, cancelled=False, timed_out=False, stderr_tail=stderr)

    monkeypatch.setattr(export_engine, "run_process", fake)


def test_disk_full_has_its_own_code(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    fake_render(monkeypatch, "av_interleaved_write_frame(): No space left on device")
    with pytest.raises(DiskFullError) as error:
        run(tools, source, out, 1, 1000, 4000)
    assert error.value.code == "DISK_FULL"
    assert leftovers(out) == []


def test_ffmpeg_failure_has_its_own_code(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    fake_render(monkeypatch, "Error while opening encoder")
    with pytest.raises(RenderFailedError) as error:
        run(tools, source, out, 1, 1000, 4000)
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
        run(broken, source, out, 1, 1000, 4000)
    assert leftovers(out) == []


@pytest.mark.parametrize("change", ["mtime", "size"])
def test_source_change_during_render_aborts(tools: MediaTools, tmp_path: Path, change: str) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    changed = threading.Event()

    def touch(progress: FfmpegProgress) -> None:
        if changed.is_set():
            return
        changed.set()
        if change == "mtime":
            stat = source.stat()
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        else:
            with source.open("ab") as handle:
                handle.write(b"\0")

    with pytest.raises(SourceChangedError) as error:
        extract_stt_audio(tools, make_request(source, out, 1, 1000, 4000), on_progress=touch)
    assert error.value.code == "SOURCE_CHANGED"
    assert changed.is_set()
    assert leftovers(out) == []


# --- safety of arguments and paths ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["Київ запис.mp4", "it's & %PATH% ;, [1] (copy).mp4", "-i.mp4", "file;name ^ `x`.mp4"],
)
def test_unusual_source_names_and_output_directory(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4", name)
    out = output_dir(tmp_path, "Вихідне аудіо з пробілами")
    commands = spy_renders(monkeypatch)

    result = run(tools, source, out, 1, 1000, 3000)

    assert leftovers(out) == [result.path.name]
    assert len(pcm(tools, result.path)) == 2 * RATE
    argv = commands[0]
    assert all(isinstance(arg, str) for arg in argv)
    assert f"file:{source}" in argv  # one argument: never parsed as an option
    assert argv[argv.index("-filter_complex") + 1].count(name) == 0
    assert not any(name in arg for arg in argv if arg not in (f"file:{source}",))


def test_render_command_is_the_stt_recipe(
    tools: MediaTools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    commands = spy_renders(monkeypatch)
    run(tools, source, output_dir(tmp_path), 1, 4120, 7880)
    argv = commands[0]
    assert argv[0] == str(tools.ffmpeg)
    graph = argv[argv.index("-filter_complex") + 1]
    assert graph.startswith("[0:1]atrim=start=4.120000:end=7.880000,asetpts=PTS-4.120000/TB,")
    assert "aresample=async=1:first_pts=0[a0];" in graph
    assert "aformat=sample_fmts=s16:sample_rates=16000:channel_layouts=mono" in graph
    assert f"apad=whole_len={16 * 3760},atrim=end_sample={16 * 3760}[a]" in graph
    for forbidden in ("afftdn", "arnndn", "atempo", "asetrate", "silenceremove", "rubberband"):
        assert forbidden not in graph
    assert argv[argv.index("-map") + 1] == "[a]"
    assert "-ss" in argv
    assert argv[argv.index("-ss") + 1] == "1.120"
    assert "-copyts" in argv


# --- request validation -----------------------------------------------------------------------


def test_window_must_lie_inside_the_source(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    for start_ms, end_ms in [(-1, 1000), (1000, 1000), (4000, 3000), (1000, 12001)]:
        with pytest.raises(IntervalError):
            run(tools, source, out, 1, start_ms, end_ms)
    assert leftovers(out) == []
    assert len(pcm(tools, run(tools, source, out, 1, 11000, 12000).path)) == RATE


def test_output_directory_must_exist(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    with pytest.raises(OutputDirUnavailableError) as error:
        run(tools, source, tmp_path / "missing", 1, 1000, 4000)
    assert error.value.code == "OUTPUT_DIR_UNAVAILABLE"
    with pytest.raises(OutputDirUnavailableError):
        run(tools, source, source, 1, 1000, 4000)  # a file, not a directory


def test_malformed_requests(tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture(tmp_path, "color_gop.mp4")
    out = output_dir(tmp_path)
    with pytest.raises(ValueError, match="source"):
        run(tools, Path("color_gop.mp4"), out, 1, 1000, 4000)
    with pytest.raises(ValueError, match="output_dir"):
        run(tools, source, Path("audio"), 1, 1000, 4000)
    for bad in ("not-a-uuid", "", "{" + str(uuid.uuid4()) + "}"):
        with pytest.raises(ValueError, match="task_id"):
            run(tools, source, out, 1, 1000, 4000, task_id=bad)
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
        run(tools, source, out, 1, 1000, 4000, task_id=task_id)
    assert leftover.read_bytes() == b"not ours to touch"
