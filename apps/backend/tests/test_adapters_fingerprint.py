"""Quick fingerprint and streaming SHA-256 (docs/01 FR-004)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from clipforge.adapters.fingerprint import (
    QUICK_SAMPLE_BYTES,
    HashCancelledError,
    quick_fingerprint,
    sha256_file,
)

from .support import BACKEND_DIR

FIXTURES = BACKEND_DIR.parents[1] / "test-results" / "fixtures"


def fixture(name: str) -> Path:
    path = FIXTURES / name
    if not path.is_file():
        pytest.fail(f"Fixture {name} is missing: run `npm run fixtures` first")
    return path


def pattern(size: int) -> bytes:
    return (bytes(range(251)) * (size // 251 + 1))[:size]


@pytest.mark.parametrize("name", ["color_gop.mp4", "two_tracks.mkv", "Київ запис.mp4"])
def test_sha256_equals_hashlib_on_fixtures(name: str) -> None:
    path = fixture(name)
    assert sha256_file(path) == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("size", [0, 1, 4095, 4096, 4097, 3 * 4096 + 123])
def test_sha256_equals_hashlib_for_chunk_boundaries(tmp_path: Path, size: int) -> None:
    data = pattern(size)
    path = tmp_path / "data.bin"
    path.write_bytes(data)
    assert sha256_file(path, chunk_bytes=4096) == hashlib.sha256(data).hexdigest()


def test_sha256_does_not_change_the_file(tmp_path: Path) -> None:
    path = tmp_path / "source.bin"
    path.write_bytes(pattern(10000))
    before = path.stat()
    sha256_file(path, chunk_bytes=1024)
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_sha256_cancel_stops_and_releases_the_file(tmp_path: Path) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(pattern(10 * 1024))
    checks = 0

    def should_cancel() -> bool:
        nonlocal checks
        checks += 1
        return checks == 3

    with pytest.raises(HashCancelledError):
        sha256_file(path, should_cancel=should_cancel, chunk_bytes=1024)
    assert checks == 3
    path.unlink()  # Windows refuses this while a handle is still open


def test_sha256_cancel_before_first_chunk(tmp_path: Path) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(b"x")
    with pytest.raises(HashCancelledError):
        sha256_file(path, should_cancel=lambda: True)


def test_sha256_rejects_non_positive_chunk(tmp_path: Path) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(b"x")
    with pytest.raises(ValueError, match="chunk_bytes"):
        sha256_file(path, chunk_bytes=0)


def test_missing_file_raises_os_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sha256_file(tmp_path / "missing.bin")
    with pytest.raises(FileNotFoundError):
        quick_fingerprint(tmp_path / "missing.bin")


def test_quick_fingerprint_format_and_copy_equality(tmp_path: Path) -> None:
    original = fixture("color_gop.mp4")
    copy = tmp_path / "copy.mp4"
    shutil.copyfile(original, copy)
    value = quick_fingerprint(original)
    prefix, digest = value.split(":")
    assert prefix == "qf1"
    assert len(digest) == 64
    assert int(digest, 16) >= 0
    assert quick_fingerprint(copy) == value
    assert quick_fingerprint(fixture("Київ запис.mp4")) == value


def test_quick_fingerprint_detects_small_file_change(tmp_path: Path) -> None:
    path = tmp_path / "small.bin"
    data = bytearray(pattern(5000))
    path.write_bytes(bytes(data))
    before = quick_fingerprint(path)
    data[2500] ^= 0xFF
    path.write_bytes(bytes(data))
    assert quick_fingerprint(path) != before


@pytest.mark.parametrize("where", ["start", "middle", "end", "size"])
def test_quick_fingerprint_detects_changes_in_samples(tmp_path: Path, where: str) -> None:
    size = 3 * QUICK_SAMPLE_BYTES + 4097
    data = bytearray(pattern(size))
    path = tmp_path / "large.bin"
    path.write_bytes(bytes(data))
    before = quick_fingerprint(path)
    if where == "size":
        data += b"\0"
    else:
        position = {"start": 10, "middle": size // 2, "end": size - 10}[where]
        data[position] ^= 0xFF
    path.write_bytes(bytes(data))
    assert quick_fingerprint(path) != before


def test_quick_fingerprint_is_not_proof_of_identity(tmp_path: Path) -> None:
    size = 3 * QUICK_SAMPLE_BYTES + 4097
    data = bytearray(pattern(size))
    path = tmp_path / "large.bin"
    path.write_bytes(bytes(data))
    quick_before, full_before = quick_fingerprint(path), sha256_file(path)
    data[QUICK_SAMPLE_BYTES + 100] ^= 0xFF  # outside every sample
    path.write_bytes(bytes(data))
    assert quick_fingerprint(path) == quick_before
    assert sha256_file(path) != full_before
