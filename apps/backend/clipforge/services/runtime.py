"""The services of one running sidecar (ADR 017).

``Runtime.start`` opens the workspace database (workspace lock, backup and migration happen
there; failures propagate as ``StorageError``) and runs start-up job recovery. ``close`` stops the
job workers first and releases the workspace lock last.

``stt_client_factory`` says how the API key becomes an STT client (production:
``openrouter_stt_factory(config)``; without one every STT request is refused) and ``extract_audio``
how the audio of a chunk is cut (default: FFmpeg). Both exist so that tests run without the network
and without FFmpeg; the key is read from ``credentials`` only when a request is sent.

``key_checker`` says how the key is checked (``POST /v1/runtime/credentials/check``; production:
``openrouter_key_checker(config)``; without one every check is refused and nothing is sent).

``chat_client_factory`` says how the key becomes an AI selection client (production:
``openrouter_chat_factory(config)``; without one every analysis request is refused). ``mode`` is the
mode of the process: it decides the models AI selection accepts and whether a key is required
(ADR 041 §1–§2). It defaults to production, the stricter of the two.
"""

from __future__ import annotations

from pathlib import Path

from clipforge.adapters.media_tools import MediaTools
from clipforge.config import Mode
from clipforge.services.analysis_jobs import (
    ANALYSIS_JOB,
    AnalysisJobService,
    ChatClientFactory,
    KeyedChatClient,
    unconfigured_chat_factory,
)
from clipforge.services.analysis_runs import AnalysisRunService
from clipforge.services.clips import ClipService
from clipforge.services.credentials import (
    CredentialStore,
    KeyChecker,
    KeyCheckService,
    unconfigured_key_checker,
)
from clipforge.services.exports import EXPORT_JOB, ExportService
from clipforge.services.jobs import JobRunner, JobService
from clipforge.services.previews import PREVIEW_JOB, PreviewService
from clipforge.services.projects import ProjectService
from clipforge.services.settings import SettingsService
from clipforge.services.sources import SourceService
from clipforge.services.spend import SpendLedger
from clipforge.services.transcription_jobs import (
    TRANSCRIPTION_JOB,
    KeyedSttClient,
    SttClientFactory,
    TranscriptionJobService,
    missing_tools_extractor,
    unconfigured_stt_factory,
)
from clipforge.services.transcription_runs import (
    AudioExtractor,
    TranscriptionRunService,
    ffmpeg_extractor,
)
from clipforge.services.transcripts import TranscriptService
from clipforge.storage import Database, open_database


class Runtime:
    def __init__(
        self,
        database: Database,
        media_tools: MediaTools | None,
        *,
        stt_client_factory: SttClientFactory | None = None,
        extract_audio: AudioExtractor | None = None,
        key_checker: KeyChecker | None = None,
        chat_client_factory: ChatClientFactory | None = None,
        mode: Mode = "production",
    ) -> None:
        self.database = database
        self.media_tools = media_tools
        # The API key: memory only, gone when this runtime closes.
        self.credentials = CredentialStore()
        self.key_checks = KeyCheckService(self.credentials, key_checker or unconfigured_key_checker)
        self.settings = SettingsService(database, self.credentials)
        # One ledger for every paid request of this process (FR-049).
        self.spend = SpendLedger(database)
        self.runner = JobRunner(database)
        self.projects = ProjectService(database)
        self.sources = SourceService(database, media_tools, self.runner)
        self.clips = ClipService(database)
        self.transcripts = TranscriptService(database)
        self.exports = ExportService(database, media_tools)
        self.previews = PreviewService(database, media_tools)
        if extract_audio is None:
            extract_audio = (
                ffmpeg_extractor(media_tools)
                if media_tools is not None
                else missing_tools_extractor
            )
        self.transcription_runs = TranscriptionRunService(
            database,
            self.spend,
            client=KeyedSttClient(self.credentials, stt_client_factory or unconfigured_stt_factory),
            extract_audio=extract_audio,
        )
        self.transcription_jobs = TranscriptionJobService(
            database,
            self.transcription_runs,
            self.spend,
            self.settings,
            self.credentials,
            media_tools,
        )
        self.analysis_runs = AnalysisRunService(
            database,
            self.spend,
            client=KeyedChatClient(
                self.credentials, chat_client_factory or unconfigured_chat_factory, mode=mode
            ),
        )
        self.analysis_jobs = AnalysisJobService(
            database, self.analysis_runs, self.settings, self.credentials, mode=mode
        )
        self.jobs = JobService(
            database,
            {
                EXPORT_JOB: self.exports.cancel,
                PREVIEW_JOB: self.previews.cancel,
                TRANSCRIPTION_JOB: self.transcription_jobs.cancel,
                ANALYSIS_JOB: self.analysis_jobs.cancel,
            },
            {
                TRANSCRIPTION_JOB: self.transcription_jobs.resume,
                ANALYSIS_JOB: self.analysis_jobs.resume,
            },
        )

    @classmethod
    def start(
        cls,
        data_dir: Path,
        *,
        media_tools: MediaTools | None,
        stt_client_factory: SttClientFactory | None = None,
        extract_audio: AudioExtractor | None = None,
        key_checker: KeyChecker | None = None,
        chat_client_factory: ChatClientFactory | None = None,
        mode: Mode = "production",
    ) -> Runtime:
        """``media_tools`` is ``None`` when FFmpeg was not found: media routes then answer 503."""
        database = open_database(data_dir)
        runtime = cls(
            database,
            media_tools,
            stt_client_factory=stt_client_factory,
            extract_audio=extract_audio,
            key_checker=key_checker,
            chat_client_factory=chat_client_factory,
            mode=mode,
        )
        try:
            runtime.runner.recover()
            runtime.transcription_jobs.recover()
            runtime.analysis_jobs.recover()
            runtime.exports.recover()
            runtime.previews.recover()
        except BaseException:
            runtime.close()
            raise
        return runtime

    def close(self) -> None:
        try:
            # First: nothing may be sent once the key is forgotten below.
            try:
                self.analysis_jobs.shutdown()
            finally:
                self.transcription_jobs.shutdown()
        finally:
            self.credentials.clear_openrouter_key()
            try:
                self.previews.shutdown()
            finally:
                try:
                    self.exports.shutdown()
                finally:
                    try:
                        self.runner.shutdown()
                    finally:
                        self.database.close()
