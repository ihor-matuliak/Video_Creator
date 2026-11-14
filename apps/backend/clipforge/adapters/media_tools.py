"""FFmpeg/FFprobe location, versions and short tool calls (ADR 017, docs/05 section 3).

Paths are resolved once at startup: in test mode from ``CLIPFORGE_FFMPEG_PATH`` /
``CLIPFORGE_FFPROBE_PATH`` when set, otherwise through ``shutil.which``. The result is
always an absolute path. Versions go into run records and export manifests.

``run_short`` is the only way this layer starts a short-lived tool: an argument list,
``shell=False``, closed stdin, a timeout and no console window. On timeout
``subprocess.run`` kills only its own child process.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from clipforge.config import Mode

ENV_FFMPEG_PATH: Final = "CLIPFORGE_FFMPEG_PATH"
ENV_FFPROBE_PATH: Final = "CLIPFORGE_FFPROBE_PATH"
VERSION_TIMEOUT_S: Final = 15.0
STDERR_TAIL_CHARS: Final = 2000

if sys.platform == "win32":
    _CREATION_FLAGS: Final[int] = subprocess.CREATE_NO_WINDOW
else:
    _CREATION_FLAGS: Final[int] = 0

_VERSION_RE: Final = re.compile(r"^(?:ffmpeg|ffprobe) version (\S+)")


class MediaToolsUnavailableError(Exception):
    """FFmpeg or FFprobe cannot be used. The message is Ukrainian and contains no paths."""

    code: Final = "MEDIA_TOOLS_UNAVAILABLE"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True, slots=True)
class MediaTools:
    ffmpeg: Path
    ffprobe: Path
    ffmpeg_version: str
    ffprobe_version: str


def run_short(argv: Sequence[str], *, timeout_s: float) -> subprocess.CompletedProcess[bytes]:
    """Run a short tool call and capture its output.

    Raises ``subprocess.TimeoutExpired`` (the child is already killed) and ``OSError``
    when the executable cannot be started.
    """
    return subprocess.run(  # noqa: S603 - argument list, shell=False, no user text in a shell
        list(argv),
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout_s,
        check=False,
        creationflags=_CREATION_FLAGS,
    )


def stderr_tail(stderr: bytes) -> str:
    """Bounded, decoded stderr for diagnostics (never the only source of state)."""
    return stderr.decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:]


def resolve_media_tools(
    mode: Mode,
    env: Mapping[str, str],
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> MediaTools:
    """Find ffmpeg and ffprobe and read their versions."""
    ffmpeg = _resolve_one("ffmpeg", ENV_FFMPEG_PATH, mode, env, which)
    ffprobe = _resolve_one("ffprobe", ENV_FFPROBE_PATH, mode, env, which)
    return MediaTools(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        ffmpeg_version=read_tool_version(ffmpeg),
        ffprobe_version=read_tool_version(ffprobe),
    )


def read_tool_version(executable: Path) -> str:
    """Version token from the first line of ``-version``, e.g. ``8.1-full_build-www.gyan.dev``."""
    argv = [str(executable), "-hide_banner", "-version"]
    try:
        completed = run_short(argv, timeout_s=VERSION_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        raise MediaToolsUnavailableError(
            f"Не вдалося запустити {executable.stem} для перевірки версії"
        ) from None
    first_line = completed.stdout.decode("utf-8", errors="replace").splitlines()[:1]
    match = _VERSION_RE.match(first_line[0]) if first_line else None
    if completed.returncode != 0 or match is None:
        raise MediaToolsUnavailableError(f"{executable.stem} не повідомив свою версію")
    return match.group(1)


def _resolve_one(
    name: str,
    env_name: str,
    mode: Mode,
    env: Mapping[str, str],
    which: Callable[[str], str | None],
) -> Path:
    if mode == "test" and env.get(env_name):
        candidate = Path(env[env_name])
        if not candidate.is_absolute():
            raise MediaToolsUnavailableError(f"{env_name} має бути абсолютним шляхом")
    else:
        found = which(name)
        if found is None:
            raise MediaToolsUnavailableError(f"{name} не знайдено")
        candidate = Path(found).resolve()
    if not candidate.is_file():
        raise MediaToolsUnavailableError(f"{name} не знайдено")
    return candidate
