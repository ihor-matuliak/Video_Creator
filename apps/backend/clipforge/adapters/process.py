"""Owned child processes for FFmpeg/FFprobe (docs/05_MEDIA_TRANSCRIPTION.md section 3).

A child is started from an argument list with ``shell=False`` and without a
console window. It is created suspended, assigned to a fresh Windows Job Object
with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and only then resumed, so every
process it spawns belongs to the same job. Stopping a run therefore ends exactly
this task's process tree; nothing is killed by image name.

Stop sequence for cancel and timeout: write ``q`` to the child's stdin (FFmpeg's
graceful quit, so callers must not pass ``-nostdin``), wait ``stop_grace_s``,
then terminate the job. When the run ends, any process left in the job is
terminated as well. The runner returns only after the processes it terminated
have exited (their handles are closed), so the caller may delete temp files.

stdout is parsed as FFmpeg ``-progress pipe:1`` output; other stdout lines are
drained and discarded. stderr is kept only as a bounded tail.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from types import MappingProxyType
from typing import IO, Any, Final, Literal

if sys.platform != "win32":  # pragma: no cover - the product targets Windows only
    raise ImportError("clipforge.adapters.process requires Windows (Job Objects)")

DEFAULT_STOP_GRACE_S: Final = 5.0
DEFAULT_STDERR_TAIL_BYTES: Final = 64 * 1024
# Exit code given to processes ended through the job (STATUS_CONTROL_C_EXIT).
TERMINATED_EXIT_CODE: Final = 0xC000013A

_POLL_S: Final = 0.05
_KILL_WAIT_S: Final = 10.0
_READER_JOIN_S: Final = 5.0
_CHUNK_BYTES: Final = 64 * 1024
_MAX_STDOUT_LINE_BYTES: Final = 64 * 1024
_INT_RE: Final = re.compile(r"-?\d+")


class ProcessRunnerError(RuntimeError):
    """The child could not be placed under job control. Nothing keeps running."""


@dataclass(frozen=True, slots=True)
class FfmpegProgress:
    """One ``-progress`` block, closed by its ``progress=continue|end`` line.

    ``out_time_us`` is microseconds. FFmpeg writes microseconds under both
    ``out_time_us`` and the misnamed ``out_time_ms`` key; ``N/A`` gives ``None``.
    """

    fields: Mapping[str, str]
    out_time_us: int | None
    frame: int | None
    ended: bool


ProgressCallback = Callable[[FfmpegProgress], None]
_Stop = Literal["cancel", "timeout", "callback"]


@dataclass(frozen=True, slots=True)
class ProcessResult:
    exit_code: int
    cancelled: bool
    timed_out: bool
    stderr_tail: str


class ProgressParser:
    """Incremental parser of ``key=value`` lines; lines without ``=`` are ignored."""

    def __init__(self) -> None:
        self._fields: dict[str, str] = {}

    def feed_line(self, line: str) -> FfmpegProgress | None:
        key, separator, value = line.strip().partition("=")
        key = key.strip()
        if not separator or not key:
            return None
        value = value.strip()
        self._fields[key] = value
        if key != "progress":
            return None
        block, self._fields = self._fields, {}
        out_time_us = _int_field(block, "out_time_us")
        if out_time_us is None:
            out_time_us = _int_field(block, "out_time_ms")
        return FfmpegProgress(
            fields=MappingProxyType(block),
            out_time_us=out_time_us,
            frame=_int_field(block, "frame"),
            ended=value == "end",
        )


def run_process(
    args: Sequence[str],
    *,
    on_progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    timeout_s: float | None = None,
    stop_grace_s: float = DEFAULT_STOP_GRACE_S,
    stderr_tail_bytes: int = DEFAULT_STDERR_TAIL_BYTES,
) -> ProcessResult:
    """Run ``args`` as an owned process tree and wait for it to finish.

    ``on_progress`` is called on a reader thread. If it raises, the run is
    stopped like a cancel and the exception is re-raised here. A missing
    executable raises the ``OSError`` from process creation unchanged.
    """
    argv = _argv(args)
    if timeout_s is not None and timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    if stop_grace_s < 0:
        raise ValueError("stop_grace_s must not be negative")
    if stderr_tail_bytes <= 0:
        raise ValueError("stderr_tail_bytes must be positive")

    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    tail = _Tail(stderr_tail_bytes)
    pump = _ProgressPump(on_progress)
    job = _Job()
    try:
        process = subprocess.Popen(  # noqa: S603 - argument list, shell=False, nothing interpolated
            argv,
            bufsize=0,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW | _CREATE_SUSPENDED,
        )
    except BaseException:
        job.close()
        raise

    readers: list[threading.Thread] = []
    assigned = False
    stop: _Stop | None = None
    try:
        try:
            job.assign(process.pid)
            assigned = True
            _resume_threads(process.pid)
        except OSError as exc:
            raise ProcessRunnerError("Не вдалося запустити процес під контролем задачі") from exc
        # stdout and stderr are PIPE, so both streams exist.
        readers = [
            threading.Thread(target=pump.drain, args=(process.stdout,), daemon=True),
            threading.Thread(target=tail.drain, args=(process.stderr,), daemon=True),
        ]
        for reader in readers:
            reader.start()
        stop = _wait_for_exit_or_stop(process, cancel, deadline, pump)
        if stop is not None:
            _send_quit(process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=stop_grace_s)
            # Still running after the grace period: the job is terminated below.
    finally:
        try:
            _end_tree(process, job, assigned=assigned)
        finally:
            for reader in readers:
                reader.join(_READER_JOIN_S)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            job.close()

    if pump.error is not None:
        raise pump.error
    return ProcessResult(
        exit_code=int(process.returncode),
        cancelled=stop == "cancel",
        timed_out=stop == "timeout",
        stderr_tail=tail.text(),
    )


def _wait_for_exit_or_stop(
    process: subprocess.Popen[bytes],
    cancel: threading.Event | None,
    deadline: float | None,
    pump: _ProgressPump,
) -> _Stop | None:
    while True:
        try:
            process.wait(timeout=_POLL_S)
        except subprocess.TimeoutExpired:
            pass
        else:
            return None
        if pump.failed.is_set():
            return "callback"
        if cancel is not None and cancel.is_set():
            return "cancel"
        if deadline is not None and time.monotonic() >= deadline:
            return "timeout"


def _send_quit(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is None:
        return
    with contextlib.suppress(OSError):  # the child has exited or closed its stdin
        process.stdin.write(b"q")
        process.stdin.flush()


def _end_tree(process: subprocess.Popen[bytes], job: _Job, *, assigned: bool) -> None:
    if assigned:
        job.terminate_and_wait(TERMINATED_EXIT_CODE, _KILL_WAIT_S)
    elif process.poll() is None:
        process.kill()  # never joined the job: it is still our direct, suspended child
    process.wait(timeout=_KILL_WAIT_S)


def _argv(args: Sequence[str]) -> list[str]:
    if isinstance(args, str | bytes):
        raise TypeError("args must be a list of arguments, not a command string")
    argv = list(args)
    if not argv:
        raise ValueError("args must not be empty")
    for arg in argv:
        if not isinstance(arg, str):
            raise TypeError("every argument must be a str")
        if "\x00" in arg:
            raise ValueError("arguments must not contain NUL characters")
    return argv


def _int_field(fields: Mapping[str, str], key: str) -> int | None:
    value = fields.get(key)
    if value is None or _INT_RE.fullmatch(value) is None:
        return None
    return int(value)


def _read_chunks(stream: IO[bytes]) -> Iterator[bytes]:
    fd = stream.fileno()
    while True:
        try:
            chunk = os.read(fd, _CHUNK_BYTES)
        except OSError:
            return
        if not chunk:
            return
        yield chunk


class _ProgressPump:
    def __init__(self, callback: ProgressCallback | None) -> None:
        self._callback = callback
        self._parser = ProgressParser()
        self.failed = threading.Event()
        self.error: Exception | None = None

    def drain(self, stream: IO[bytes]) -> None:
        pending = b""
        for chunk in _read_chunks(stream):
            *lines, pending = (pending + chunk).split(b"\n")
            for line in lines:
                self._line(line)
            if len(pending) > _MAX_STDOUT_LINE_BYTES:
                pending = b""
        if pending:
            self._line(pending)

    def _line(self, raw: bytes) -> None:
        progress = self._parser.feed_line(raw.decode("utf-8", "replace"))
        if progress is None or self._callback is None or self.failed.is_set():
            return
        try:
            self._callback(progress)
        except Exception as exc:
            self.error = exc
            self.failed.set()


class _Tail:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()
        self._trimmed = False
        self._lock = threading.Lock()

    def drain(self, stream: IO[bytes]) -> None:
        for chunk in _read_chunks(stream):
            with self._lock:
                self._data += chunk
                excess = len(self._data) - self._limit
                if excess > 0:
                    del self._data[:excess]
                    self._trimmed = True

    def text(self) -> str:
        with self._lock:
            data = bytes(self._data)
            trimmed = self._trimmed
        start = 0
        while trimmed and start < min(3, len(data)) and data[start] & 0xC0 == 0x80:
            start += 1  # do not start the tail in the middle of a UTF-8 sequence
        return data[start:].decode("utf-8", "replace")


# --- Win32 Job Object plumbing -------------------------------------------------

_CREATE_SUSPENDED: Final = 0x00000004
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
_JOB_BASIC_ACCOUNTING_INFORMATION: Final = 1
_JOB_BASIC_PROCESS_ID_LIST: Final = 3
_JOB_EXTENDED_LIMIT_INFORMATION: Final = 9
_PROCESS_TERMINATE: Final = 0x0001
_PROCESS_SET_QUOTA: Final = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
_SYNCHRONIZE: Final = 0x00100000
_ERROR_MORE_DATA: Final = 234
_MAX_LISTED_PROCESSES: Final = 256
_TH32CS_SNAPTHREAD: Final = 0x00000004
_THREAD_SUSPEND_RESUME: Final = 0x0002
_RESUME_FAILED: Final = 0xFFFFFFFF
_INVALID_HANDLE_VALUE: Final = wintypes.HANDLE(-1).value


class _IoCounters(ctypes.Structure):
    _fields_ = (
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    )


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = (
        ("TotalUserTime", wintypes.LARGE_INTEGER),
        ("TotalKernelTime", wintypes.LARGE_INTEGER),
        ("ThisPeriodTotalUserTime", wintypes.LARGE_INTEGER),
        ("ThisPeriodTotalKernelTime", wintypes.LARGE_INTEGER),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    )


class _ProcessIdList(ctypes.Structure):
    """JOBOBJECT_BASIC_PROCESS_ID_LIST with a fixed capacity."""

    _fields_ = (
        ("NumberOfAssignedProcesses", wintypes.DWORD),
        ("NumberOfProcessIdsInList", wintypes.DWORD),
        ("ProcessIdList", ctypes.c_size_t * _MAX_LISTED_PROCESSES),
    )


class _ThreadEntry32(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    )


# A private WinDLL instance, so argtypes set here never clash with other code.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _bind(name: str, restype: Any, *argtypes: Any) -> Callable[..., Any]:
    function = getattr(_kernel32, name)
    function.restype = restype
    function.argtypes = argtypes
    return function  # type: ignore[no-any-return]


_CreateJobObjectW = _bind("CreateJobObjectW", wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCWSTR)
_SetInformationJobObject = _bind(
    "SetInformationJobObject",
    wintypes.BOOL,
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
)
_QueryInformationJobObject = _bind(
    "QueryInformationJobObject",
    wintypes.BOOL,
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPDWORD,
)
_AssignProcessToJobObject = _bind(
    "AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE
)
_TerminateJobObject = _bind("TerminateJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
_IsProcessInJob = _bind(
    "IsProcessInJob", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE, wintypes.PBOOL
)
_WaitForSingleObject = _bind("WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
_OpenProcess = _bind("OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_CloseHandle = _bind("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
_CreateToolhelp32Snapshot = _bind(
    "CreateToolhelp32Snapshot", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD
)
_Thread32First = _bind(
    "Thread32First", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)
)
_Thread32Next = _bind(
    "Thread32Next", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)
)
_OpenThread = _bind("OpenThread", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_ResumeThread = _bind("ResumeThread", wintypes.DWORD, wintypes.HANDLE)


def _last_error(function: str) -> OSError:
    error = ctypes.WinError(ctypes.get_last_error())
    error.strerror = f"{function}: {error.strerror}"
    return error


class _Job:
    """A fresh Job Object; closing its handle kills every process still in it."""

    def __init__(self) -> None:
        handle = _CreateJobObjectW(None, None)
        if not handle:
            raise ProcessRunnerError("Не вдалося створити Job Object") from _last_error(
                "CreateJobObjectW"
            )
        self._handle: int | None = int(handle)
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _SetInformationJobObject(
            self._handle, _JOB_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            error = _last_error("SetInformationJobObject")
            self.close()
            raise ProcessRunnerError("Не вдалося налаштувати Job Object") from error

    def assign(self, pid: int) -> None:
        process = _OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            raise _last_error("OpenProcess")
        try:
            if not _AssignProcessToJobObject(self._handle, process):
                raise _last_error("AssignProcessToJobObject")
        finally:
            _CloseHandle(process)

    def terminate_and_wait(self, exit_code: int, timeout_s: float) -> None:
        """Terminate every process in the job and wait (bounded) until each has exited.

        The job's active-process counter drops before a terminated process has
        closed its handles, so the members are opened first and waited on.
        """
        deadline = time.monotonic() + timeout_s
        members = self._member_handles()
        try:
            # Best effort: if this fails, closing the job handle still kills the job.
            _TerminateJobObject(self._handle, exit_code)
            for member in members:
                _WaitForSingleObject(member, _remaining_ms(deadline))
        finally:
            for member in members:
                _CloseHandle(member)
        while self._active_processes() > 0 and time.monotonic() < deadline:
            time.sleep(0.01)

    def _member_handles(self) -> list[int]:
        ids = _ProcessIdList()
        if (
            not _QueryInformationJobObject(
                self._handle,
                _JOB_BASIC_PROCESS_ID_LIST,
                ctypes.byref(ids),
                ctypes.sizeof(ids),
                None,
            )
            and ctypes.get_last_error() != _ERROR_MORE_DATA
        ):
            return []
        handles: list[int] = []
        for pid in ids.ProcessIdList[: ids.NumberOfProcessIdsInList]:
            handle = _OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                continue  # already gone
            in_job = wintypes.BOOL()
            if _IsProcessInJob(handle, self._handle, ctypes.byref(in_job)) and in_job.value:
                handles.append(int(handle))
            else:
                _CloseHandle(handle)  # the pid was reused by a process outside this job
        return handles

    def _active_processes(self) -> int:
        info = _BasicAccountingInformation()
        if not _QueryInformationJobObject(
            self._handle,
            _JOB_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            raise _last_error("QueryInformationJobObject")
        return int(info.ActiveProcesses)

    def close(self) -> None:
        if self._handle is not None:
            _CloseHandle(self._handle)
            self._handle = None


def _remaining_ms(deadline: float) -> int:
    return max(0, int((deadline - time.monotonic()) * 1000))


def _resume_threads(pid: int) -> None:
    """Resume the threads of a process created with CREATE_SUSPENDED."""
    snapshot = _CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        raise _last_error("CreateToolhelp32Snapshot")
    resumed = 0
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        found = _Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == pid:
                thread = _OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if not thread:
                    raise _last_error("OpenThread")
                try:
                    if _ResumeThread(thread) == _RESUME_FAILED:
                        raise _last_error("ResumeThread")
                finally:
                    _CloseHandle(thread)
                resumed += 1
            found = _Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        _CloseHandle(snapshot)
    if resumed == 0:
        raise OSError(f"no thread of process {pid} to resume")
