"""Smoke test of the built sidecar executable (M0 plan item 7, ADR 013).

Run after build_sidecar.py::

    uv run python apps/backend/packaging/smoke_sidecar.py

Launches the exe the way Electron main will (``--port 0 --config-stdin``, test mode,
data dir in a temp folder with a Cyrillic name), reads the ready line, checks
/v1/health with and without the Bearer token, closes stdin and expects exit code 0.
Prints PASS with the bundle size and build time, or FAIL with the reason (exit code 1).
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import IO

import httpx2

NAME = "clipforge-sidecar"
REPO_ROOT = Path(__file__).resolve().parents[3]
DIST_DIR = REPO_ROOT / "build" / "sidecar"  # same layout as build_sidecar.py
BUNDLE_DIR = DIST_DIR / NAME
EXE = BUNDLE_DIR / f"{NAME}.exe"
BUILD_INFO = DIST_DIR / "build-info.json"

TOKEN = "smoke-" + "0123456789abcdef" * 2  # 38 printable ASCII characters, no secret value
READY_LINE = re.compile(rb'^\{"event":"ready","port":([1-9][0-9]*)\}\n$')
READY_TIMEOUT = 60.0  # a cold onedir start can be slow under antivirus
EXIT_TIMEOUT = 30.0


class SmokeError(Exception):
    """A smoke check failed."""


def clean_env() -> dict[str, str]:
    """Environment without anything that could make a source checkout leak into the exe."""
    blocked = ("CLIPFORGE_", "PYTHON")
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(blocked)}


def read_first_line(process: subprocess.Popen[bytes]) -> bytes:
    stdout = process.stdout
    if stdout is None:
        raise SmokeError("stdout pipe is not available")
    lines: queue.Queue[bytes] = queue.Queue()
    threading.Thread(target=lambda: lines.put(stdout.readline()), daemon=True).start()
    try:
        return lines.get(timeout=READY_TIMEOUT)
    except queue.Empty:
        raise SmokeError(f"no ready line within {READY_TIMEOUT:.0f} s") from None


def drain(stream: IO[bytes], sink: list[bytes]) -> threading.Thread:
    """Collect a pipe in the background so a chatty child can never block on it."""

    def collect() -> None:
        sink.append(stream.read())

    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    return thread


def check_health(port: int) -> None:
    url = f"http://127.0.0.1:{port}/v1/health"
    with httpx2.Client(trust_env=False, timeout=10.0) as http:
        authorized = http.get(url, headers={"Authorization": f"Bearer {TOKEN}"})
        anonymous = http.get(url)
    if authorized.status_code != 200:
        raise SmokeError(f"/v1/health with Bearer returned {authorized.status_code}")
    body = authorized.json()
    if body.get("mode") != "test" or body.get("status") != "ok":
        raise SmokeError(f"unexpected /v1/health body: {body}")
    if anonymous.status_code != 401:
        raise SmokeError(f"/v1/health without Bearer returned {anonymous.status_code}, not 401")


def run_sidecar(data_dir: Path, run_dir: Path) -> float:
    """Run the full launch sequence; returns seconds from spawn to the ready line."""
    config = {"mode": "test", "data_dir": str(data_dir), "session_token": TOKEN}
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(EXE), "--port", "0", "--config-stdin"],
        cwd=run_dir,  # not the repository: the bundle must not need the source tree
        env=clean_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stderr_chunks: list[bytes] = []
    stderr_reader: threading.Thread | None = None
    try:
        if process.stdin is None or process.stderr is None:
            raise SmokeError("stdin/stderr pipes are not available")
        stderr_reader = drain(process.stderr, stderr_chunks)
        started = time.monotonic()
        process.stdin.write(json.dumps(config, ensure_ascii=False).encode("utf-8") + b"\n")
        process.stdin.flush()

        raw = read_first_line(process)
        ready_seconds = time.monotonic() - started
        matched = READY_LINE.match(raw)
        if matched is None:
            raise SmokeError(f"first stdout line is not the ready line: {raw!r}")
        check_health(int(matched.group(1)))
        if process.poll() is not None:
            raise SmokeError(f"sidecar exited early with code {process.returncode}")

        process.stdin.close()  # EOF, as when Electron main dies
        try:
            code = process.wait(timeout=EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise SmokeError(
                f"sidecar did not exit within {EXIT_TIMEOUT:.0f} s of stdin EOF"
            ) from None
        if code != 0:
            raise SmokeError(f"sidecar exit code after stdin EOF is {code}, expected 0")
        return ready_seconds
    except SmokeError:
        if process.poll() is None:
            process.kill()  # our own child only
            process.wait(timeout=EXIT_TIMEOUT)
        if stderr_reader is not None:
            stderr_reader.join(timeout=5.0)
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
        if stderr_text:
            print(f"sidecar stderr:\n{stderr_text}", file=sys.stderr)
        raise
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=EXIT_TIMEOUT)
        if stderr_reader is not None:
            stderr_reader.join(timeout=5.0)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def folder_size(folder: Path) -> int:
    return sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())


def main() -> int:
    if not EXE.is_file():
        print(f"FAIL: {EXE} not found; run apps/backend/packaging/build_sidecar.py first")
        return 1
    try:
        with tempfile.TemporaryDirectory(prefix="ClipForge_тест_") as temp:
            data_dir = Path(temp) / "дані проєкту"
            data_dir.mkdir()
            ready_seconds = run_sidecar(data_dir, Path(temp))
    except SmokeError as exc:
        print(f"FAIL: {exc}")
        return 1

    size_mib = folder_size(BUNDLE_DIR) / (1024 * 1024)
    try:
        build_seconds = json.loads(BUILD_INFO.read_text(encoding="utf-8"))["build_seconds"]
        build_text = f"{build_seconds} s"
    except (OSError, ValueError, KeyError):
        build_text = "unknown (build-info.json missing)"
    print(
        f"PASS exe folder: {size_mib:.1f} MiB, build time: {build_text}, "
        f"time to ready line: {ready_seconds:.1f} s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
