"""The preview proxy engine and the filter recipe it shares with the export (ADR 018, ADR 021)."""

from __future__ import annotations

import os
import threading
import uuid
from fractions import Fraction
from pathlib import Path

import pytest

from clipforge.adapters.ffprobe import SourceProbe, probe_source
from clipforge.adapters.fingerprint import quick_fingerprint
from clipforge.adapters.media_tools import MediaTools
from clipforge.services import preview_engine
from clipforge.services.export_engine import (
    ExportCancelledError,
    InvalidAudioStreamError,
    SourceChangedError,
    audio_chain,
    canonical_window,
    video_chain,
)
from clipforge.services.preview_engine import PreviewRequest, build_preview

from .test_api_sources import copy_fixture


def make_probe(*, width: int, height: int, rotation: int = 0, origin_pts: int = 0) -> SourceProbe:
    return SourceProbe(
        format_name="matroska,webm",
        duration_ms=1000,
        origin_pts=origin_pts,
        time_base=Fraction(1, 1000),
        video_stream_index=0,
        video_codec="h264",
        width=width,
        height=height,
        rotation=rotation,
        hdr=False,
        unsupported_reason=None,
        audio_streams=(),
    )


def make_request(
    original: Path, cache: Path, audio: int | None = None, **changes: object
) -> PreviewRequest:
    """A request for ``original``; ``changes`` replace request fields by name."""
    state = original.stat()
    values: dict[str, object] = {
        "task_id": str(uuid.uuid4()),
        "source": original,
        "output_dir": cache,
        "audio_stream_index": audio,
        "expected_size": state.st_size,
        "expected_mtime_ns": state.st_mtime_ns,
        "expected_fingerprint": quick_fingerprint(original),
    }
    values.update(changes)
    return PreviewRequest(**values)  # type: ignore[arg-type]


def listing(directory: Path) -> list[str]:
    return sorted(os.listdir(directory))


# --- the shared recipe -----------------------------------------------------------------------


def test_the_chains_are_the_adr_018_text_and_the_scale_sits_between_setpts_and_pad() -> None:
    assert video_chain(0, "1.000000", "3.000000") == (
        "[0:0]trim=start=1.000000:end=3.000000,setpts=PTS-1.000000/TB,"
        "pad=width=ceil(iw/2)*2:height=ceil(ih/2)*2[v]"
    )
    assert video_chain(2, "1.000000", "3.000000", scale_height=720) == (
        "[0:2]trim=start=1.000000:end=3.000000,setpts=PTS-1.000000/TB,scale=w=-2:h=720,"
        "pad=width=ceil(iw/2)*2:height=ceil(ih/2)*2[v]"
    )
    assert audio_chain(1, 0, "1.000000", "3.000000") == (
        "[0:1]atrim=start=1.000000:end=3.000000,asetpts=PTS-1.000000/TB,"
        "aresample=async=1:first_pts=0[a0]"
    )
    assert audio_chain(4, 2, "0.500000", "9.250000").endswith("[a2]")


def test_the_window_is_counted_from_the_canonical_origin() -> None:
    probe = make_probe(width=320, height=180, origin_pts=10_000)  # the first frame is at 10 s
    assert canonical_window(probe, 0, 1000) == ("10.000000", "11.000000")
    assert canonical_window(probe, 500, 1501) == ("10.500000", "11.501000")


@pytest.mark.parametrize(
    ("width", "height", "rotation", "expected"),
    [
        (1920, 1080, 0, 720),
        (1280, 720, 0, None),  # exactly 720 is not scaled
        (320, 180, 0, None),  # never upscaled
        (1080, 1920, 0, 720),  # a portrait video is taller than 720 as well
        (1080, 1920, 90, 720),  # a quarter turn makes it 1920 x 1080 upright
        (1280, 720, 90, 720),  # ... and this one 720 x 1280, which is taller than 720
        (720, 1280, 90, None),  # ... while this one becomes 1280 x 720
    ],
)
def test_the_scale_follows_the_upright_height(
    width: int, height: int, rotation: int, expected: int | None
) -> None:
    probe = make_probe(width=width, height=height, rotation=rotation)
    assert preview_engine._scale_height(probe) == expected


# --- the engine ------------------------------------------------------------------------------


def test_build_preview_writes_only_the_final_file(media_tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture("color_gop.mp4", tmp_path / "in")
    out = tmp_path / "cache"
    out.mkdir()
    request = make_request(source, out, 1)
    seen: list[list[str]] = []

    result = build_preview(
        media_tools, request, on_progress=lambda progress: seen.append(listing(out))
    )

    assert result.path == out / f"{request.task_id}.mp4"
    assert result.size == result.path.stat().st_size
    assert abs(result.duration_ms - 12000) <= 100
    assert listing(out) == [f"{request.task_id}.mp4"]
    assert seen
    assert seen[0] == [f".clipforge-{request.task_id}.part"]  # the engine's exact temp name
    assert probe_source(media_tools.ffprobe, result.path).audio_streams[0].channels == 2


def test_a_changed_source_stops_the_work_before_it_starts(
    media_tools: MediaTools, tmp_path: Path
) -> None:
    source = copy_fixture("color_gop.mp4", tmp_path / "in")
    out = tmp_path / "cache"
    out.mkdir()
    state = source.stat()

    for changes in (
        {"expected_size": state.st_size + 1},
        {"expected_mtime_ns": state.st_mtime_ns + 1},
        {"expected_fingerprint": "qf1:" + "0" * 64},
    ):
        with pytest.raises(SourceChangedError):
            build_preview(media_tools, make_request(source, out, 1, **changes))
        assert listing(out) == []


def test_an_audio_stream_the_source_lacks_is_refused_before_any_file_is_made(
    media_tools: MediaTools, tmp_path: Path
) -> None:
    source = copy_fixture("color_gop.mp4", tmp_path / "in")
    out = tmp_path / "cache"
    out.mkdir()
    for audio in (0, 2, 7):  # 0 is the video stream
        with pytest.raises(InvalidAudioStreamError):
            build_preview(media_tools, make_request(source, out, audio))
    assert listing(out) == []


def test_a_cancel_before_the_start_leaves_nothing(media_tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture("color_gop.mp4", tmp_path / "in")
    out = tmp_path / "cache"
    out.mkdir()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ExportCancelledError):
        build_preview(media_tools, make_request(source, out, 1), cancel=cancel)
    assert listing(out) == []


def test_malformed_requests_are_value_errors(media_tools: MediaTools, tmp_path: Path) -> None:
    source = copy_fixture("color_gop.mp4", tmp_path / "in")
    out = tmp_path / "cache"
    out.mkdir()
    for changes in (
        {"task_id": "not-a-uuid"},
        {"task_id": "{" + str(uuid.uuid4()) + "}"},
        {"source": Path("relative.mp4")},
        {"output_dir": Path("relative")},
    ):
        with pytest.raises(ValueError):  # noqa: PT011 - the message is not the contract
            build_preview(media_tools, make_request(source, out, 1, **changes))
    assert listing(out) == []
