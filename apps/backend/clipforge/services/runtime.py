"""The services of one running sidecar (ADR 017).

``Runtime.start`` opens the workspace database (workspace lock, backup and migration happen
there; failures propagate as ``StorageError``) and runs start-up job recovery. ``close`` stops the
job workers first and releases the workspace lock last.
"""

from __future__ import annotations

from pathlib import Path

from clipforge.adapters.media_tools import MediaTools
from clipforge.services.clips import ClipService
from clipforge.services.exports import EXPORT_JOB, ExportService
from clipforge.services.jobs import JobRunner, JobService
from clipforge.services.projects import ProjectService
from clipforge.services.sources import SourceService
from clipforge.storage import Database, open_database


class Runtime:
    def __init__(self, database: Database, media_tools: MediaTools | None) -> None:
        self.database = database
        self.media_tools = media_tools
        self.runner = JobRunner(database)
        self.projects = ProjectService(database)
        self.sources = SourceService(database, media_tools, self.runner)
        self.clips = ClipService(database)
        self.exports = ExportService(database, media_tools)
        self.jobs = JobService(database, {EXPORT_JOB: self.exports.cancel})

    @classmethod
    def start(cls, data_dir: Path, *, media_tools: MediaTools | None) -> Runtime:
        """``media_tools`` is ``None`` when FFmpeg was not found: media routes then answer 503."""
        database = open_database(data_dir)
        runtime = cls(database, media_tools)
        try:
            runtime.runner.recover()
            runtime.exports.recover()
        except BaseException:
            runtime.close()
            raise
        return runtime

    def close(self) -> None:
        try:
            self.exports.shutdown()
        finally:
            try:
                self.runner.shutdown()
            finally:
                self.database.close()
