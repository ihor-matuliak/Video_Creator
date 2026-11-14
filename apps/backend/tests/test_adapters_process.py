"""Owned child process runner: job tree, stop sequence, progress and stderr tail."""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path

import pytest

from clipforge.adapters.process import (
    TERMINATED_EXIT_CODE,
    FfmpegProgress,
    ProgressParser,
    run_process,
)

# The base interpreter, not the venv launcher: the child under test is then the
# real process (as FFmpeg would be), without the launcher's own job in between.
PY: str = getattr(sys, "_base_executable", sys.executable)
WAIT_S = 30.0

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_OpenProcess = _kernel32.OpenProcess
_OpenProcess.restype = wintypes.HANDLE
_OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_WaitForSingleObject = _kernel32.WaitForSingleObject
_WaitForSingleObject.restype = wintypes.DWORD
_WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_CloseHandle = _kernel32.CloseHandle
_CloseHandle.restype = wintypes.BOOL
_CloseHandle.argtypes = (wintypes.HANDLE,)
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_OBJECT_0 = 0

# Writes its own pid, starts a grandchild that writes its pid, then sleeps
# without reading stdin, so only the job can end it.
TREE_CHILD = """
import os, subprocess, sys, time

def write_pid(path):
    with open(path + ".tmp", "w") as f:
        f.write(str(os.getpid()))
    os.replace(path + ".tmp", path)

GRANDCHILD = (
    "import os, sys, time\\n"
    "with open(sys.argv[1] + '.tmp', 'w') as f: f.write(str(os.getpid()))\\n"
    "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\\n"
    "time.sleep(120)\\n"
)
subprocess.Popen([sys.executable, "-c", GRANDCHILD, sys.argv[2]])
write_pid(sys.argv[1])
if sys.argv[3] == "stay":
    time.sleep(120)
deadline = time.monotonic() + 30
while not os.path.exists(sys.argv[2]) and time.monotonic() < deadline:
    time.sleep(0.02)
"""

PROGRESS_CHILD = """
import sys
sys.stdout.write("not a progress line\\n")
for frame, us, state in ((1, 40000, "continue"), (2, 80000, "end")):
    sys.stdout.write(
        f"frame={frame}\\nfps=25.00\\nout_time_us={us}\\nout_time_ms={us}\\n"
        f"out_time=00:00:00.0{us}\\nspeed=N/A\\nprogress={state}\\n"
    )
    sys.stdout.flush()
"""

QUIT_CHILD = """
import sys
sys.stdout.write("frame=0\\nprogress=continue\\n")
sys.stdout.flush()
if sys.stdin.buffer.read(1) == b"q":
    sys.stderr.write("got q\\n")
    sys.exit(7)
sys.exit(3)
"""

LOOPING_PROGRESS_CHILD = """
import sys, time
while True:
    sys.stdout.write("frame=1\\nprogress=continue\\n")
    sys.stdout.flush()
    time.sleep(0.05)
"""


def open_process(pid: int) -> int:
    handle = _OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    assert handle, f"process {pid} is not running"
    return int(handle)


def has_exited(handle: int, timeout_s: float) -> bool:
    return int(_WaitForSingleObject(handle, int(timeout_s * 1000))) == _WAIT_OBJECT_0


def pid_is_running(pid: int) -> bool:
    handle = _OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        return not has_exited(int(handle), 0)
    finally:
        _CloseHandle(handle)


def wait_for_pid(path: Path) -> int:
    deadline = time.monotonic() + WAIT_S
    while not path.exists():
        if time.monotonic() >= deadline:
            pytest.fail(f"{path.name} was not written in time")
        time.sleep(0.02)
    return int(path.read_text())


def test_parser_splits_blocks_and_ignores_other_lines() -> None:
    parser = ProgressParser()
    lines = [
        "garbage without separator",
        "frame=12",
        "fps=25.00",
        "out_time_us=480000",
        "out_time_ms=480000",
        "out_time=00:00:00.480000",
        "speed=1.02x",
        "progress=continue",
        "frame=N/A",
        "out_time_ms=-23220",
        "progress=end",
    ]
    blocks = [block for line in lines if (block := parser.feed_line(line)) is not None]

    assert [(b.frame, b.out_time_us, b.ended) for b in blocks] == [
        (12, 480000, False),
        (None, -23220, True),
    ]
    assert blocks[0].fields["speed"] == "1.02x"
    assert "garbage without separator" not in blocks[0].fields
    assert dict(blocks[1].fields) == {"frame": "N/A", "out_time_ms": "-23220", "progress": "end"}


def test_progress_callback_receives_blocks_from_child_stdout() -> None:
    received: list[FfmpegProgress] = []

    result = run_process([PY, "-c", PROGRESS_CHILD], on_progress=received.append)

    assert result.exit_code == 0
    assert not result.cancelled
    assert not result.timed_out
    assert [(p.frame, p.out_time_us, p.ended) for p in received] == [
        (1, 40000, False),
        (2, 80000, True),
    ]


def test_arguments_with_spaces_quotes_and_cyrillic_arrive_intact() -> None:
    args = [
        "with space",
        'double"quote',
        "single'quote",
        "trailing backslash\\",
        'backslash before quote\\"x',
        "",
        "Кирилиця і пробіли.mp4",
        r"C:\Шлях з пробілом\файл (1) [cut].mkv",
        "a&b|c^d<e>f %PATH% !x! $(y)",
        "tab\tinside",
    ]
    echo = "import json, sys; sys.stderr.write(json.dumps(sys.argv[1:]))"

    result = run_process([PY, "-c", echo, *args])

    assert result.exit_code == 0
    assert json.loads(result.stderr_tail) == args


def test_stderr_keeps_only_a_bounded_tail() -> None:
    noisy = (
        "import sys; w = sys.stderr.buffer.write; "
        "w(b'START'); w(b'x' * 1_000_000); w(b'END'); sys.stderr.flush()"
    )

    result = run_process([PY, "-c", noisy], stderr_tail_bytes=4096)

    assert result.exit_code == 0
    assert len(result.stderr_tail.encode("utf-8")) <= 4096
    assert result.stderr_tail.endswith("xxxEND")
    assert "START" not in result.stderr_tail


def test_timeout_stops_the_child_and_reports_timed_out() -> None:
    started = time.monotonic()

    result = run_process(
        [PY, "-c", "import time; time.sleep(120)"], timeout_s=0.5, stop_grace_s=0.2
    )

    assert result.timed_out
    assert not result.cancelled
    assert result.exit_code == TERMINATED_EXIT_CODE
    assert time.monotonic() - started < 15


def test_cancel_sends_q_first_and_lets_the_child_exit_gracefully() -> None:
    cancel = threading.Event()

    result = run_process(
        [PY, "-c", QUIT_CHILD],
        on_progress=lambda _progress: cancel.set(),
        cancel=cancel,
        stop_grace_s=WAIT_S,
    )

    assert result.cancelled
    assert not result.timed_out
    assert result.exit_code == 7
    assert "got q" in result.stderr_tail


def test_cancel_ends_child_and_grandchild_but_not_an_unrelated_process(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    grandchild_pid = tmp_path / "grandchild.pid"
    cancel = threading.Event()
    unrelated = subprocess.Popen([PY, "-c", "import time; time.sleep(120)"])
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                run_process,
                [PY, "-c", TREE_CHILD, str(child_pid), str(grandchild_pid), "stay"],
                cancel=cancel,
                stop_grace_s=0.5,
            )
            handles: list[int] = []
            try:
                handles = [open_process(wait_for_pid(p)) for p in (child_pid, grandchild_pid)]
                assert not any(has_exited(h, 0) for h in handles)
                cancel.set()
                result = future.result(timeout=WAIT_S)
                # Already gone when run_process returns: no grace for stragglers.
                assert all(has_exited(h, 0) for h in handles)
            finally:
                cancel.set()
                for handle in handles:
                    _CloseHandle(handle)

        assert result.cancelled
        assert not result.timed_out
        assert result.exit_code == TERMINATED_EXIT_CODE
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=WAIT_S)


def test_descendants_left_behind_end_with_the_run(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    grandchild_pid = tmp_path / "grandchild.pid"

    result = run_process(
        [PY, "-c", TREE_CHILD, str(child_pid), str(grandchild_pid), "leave"], timeout_s=WAIT_S
    )

    assert result.exit_code == 0
    assert not result.cancelled
    assert not result.timed_out
    assert not pid_is_running(int(grandchild_pid.read_text()))


def test_failing_progress_callback_stops_the_child_and_is_reraised() -> None:
    def explode(_progress: FfmpegProgress) -> None:
        raise RuntimeError("callback failed")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="callback failed"):
        run_process(
            [PY, "-c", LOOPING_PROGRESS_CHILD],
            on_progress=explode,
            timeout_s=WAIT_S,
            stop_grace_s=0.2,
        )
    assert time.monotonic() - started < 15


@pytest.mark.parametrize("command", ["python -c pass", b"python -c pass"])
def test_rejects_a_command_string(command: object) -> None:
    with pytest.raises(TypeError, match="command string"):
        run_process(command)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("args", "message"),
    [([], "must not be empty"), ([PY, "-c", "pass\x00"], "NUL")],
)
def test_rejects_invalid_arguments(args: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        run_process(args)
