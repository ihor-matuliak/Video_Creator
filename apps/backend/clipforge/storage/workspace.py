"""Workspace lock: one sidecar per data directory (ADR 017, docs/04 section 7).

An exclusive Windows byte-range lock (``msvcrt.locking``) on ``<data_dir>/workspace.lock`` is
held for the whole life of the process. The operating system drops it when the process dies, so
a crashed sidecar never leaves a stale lock. The descriptor is not inheritable, so FFmpeg child
processes do not keep the workspace locked after a crash.
"""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

from .errors import StorageError, WorkspaceLocked

if sys.platform == "win32":
    import msvcrt

    _OPEN_FLAGS = os.O_RDWR | os.O_CREAT | os.O_BINARY

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - Windows 11 is the only supported platform (ADR 001)
    _OPEN_FLAGS = os.O_RDWR | os.O_CREAT

    def _try_lock(fd: int) -> None:
        raise StorageError("Блокування робочої теки реалізоване лише для Windows")

    def _unlock(fd: int) -> None:
        raise StorageError("Блокування робочої теки реалізоване лише для Windows")


class WorkspaceLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        """Take the lock or raise ``WorkspaceLocked`` at once (no waiting)."""
        if self._fd is not None:
            raise StorageError("Блокування вже утримується цим екземпляром")
        fd = os.open(self._path, _OPEN_FLAGS)
        try:
            _try_lock(fd)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EDEADLK):
                raise WorkspaceLocked from None
            raise
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        """Idempotent."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)
