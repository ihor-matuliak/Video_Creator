"""GET /v1/system/checks: readiness of FFmpeg, FFprobe and the data directory (ADR 023).

FFmpeg and FFprobe are reported from the ``MediaTools`` found at start-up; nothing is started per
request. The data directory is checked by creating and deleting a uniquely named file in its own
``tmp`` subdirectory. The response and the log never carry a path or the text of an exception.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Final

from fastapi import APIRouter
from pydantic import BaseModel

from clipforge.services.runtime import Runtime

CHECK_DIRNAME: Final = "tmp"
CHECK_FILE_PREFIX: Final = ".clipforge-check-"
CHECK_FILE_SUFFIX: Final = ".tmp"

_LOGGER = logging.getLogger(__name__)


class ToolCheck(BaseModel):
    ok: bool
    version: str | None


class DataDirCheck(BaseModel):
    ok: bool
    free_bytes: int | None


class SystemChecks(BaseModel):
    ffmpeg: ToolCheck
    ffprobe: ToolCheck
    data_dir: DataDirCheck


def build_router(runtime: Runtime) -> APIRouter:
    router = APIRouter()

    # Plain `def`: the route touches the disk, so it runs in FastAPI's thread pool.
    @router.get("/system/checks", response_model=SystemChecks)
    def system_checks() -> SystemChecks:
        tools = runtime.media_tools
        data_dir = runtime.database.data_dir
        return SystemChecks(
            ffmpeg=ToolCheck(ok=tools is not None, version=tools.ffmpeg_version if tools else None),
            ffprobe=ToolCheck(
                ok=tools is not None, version=tools.ffprobe_version if tools else None
            ),
            data_dir=DataDirCheck(ok=_can_write(data_dir), free_bytes=_free_bytes(data_dir)),
        )

    return router


def _can_write(data_dir: Path) -> bool:
    """Create and delete an own file in ``<data_dir>/tmp``; any failure means ``False``."""
    directory = data_dir / CHECK_DIRNAME
    target = directory / f"{CHECK_FILE_PREFIX}{uuid.uuid4().hex}{CHECK_FILE_SUFFIX}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _write_probe_file(target)
        target.unlink()
    except OSError:
        # Fixed text only: an OSError message carries the path. The name is unique, so a file
        # that is left under it is this check's own.
        _LOGGER.warning("The data directory is not writable")
        _remove_quietly(target)
        return False
    return True


def _write_probe_file(target: Path) -> None:
    """Create ``target`` exclusively; an existing file is never overwritten."""
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(b"clipforge")


def _remove_quietly(target: Path) -> None:
    try:
        target.unlink(missing_ok=True)
    except OSError:
        _LOGGER.warning("A data directory check file could not be removed")


def _free_bytes(data_dir: Path) -> int | None:
    try:
        return shutil.disk_usage(data_dir).free
    except OSError:
        _LOGGER.warning("The free space of the data directory is unknown")
        return None
