"""POST /v1/transcription-runs, POST /v1/transcription-runs/estimate, GET /v1/transcription-runs
and GET /v1/transcription-runs/{id} (docs/04 section 4, ADR 032 and 037).

A run is started as a ``transcription`` job; ``POST /v1/jobs/{id}/cancel`` and ``/resume`` control
it. ``model``, ``language`` and ``budget_usd`` default to the settings. The API key is never part of
a request or a response: the sidecar holds it in memory (``POST /v1/runtime/credentials``).
``force`` confirms a second, paid run over a transcript that already exists (409
``TRANSCRIPT_ALREADY_EXISTS`` without it).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from clipforge.services.runtime import Runtime
from clipforge.services.transcription_jobs import (
    ExistingTranscript,
    RunChunkView,
    RunEstimate,
    RunListItem,
    RunView,
)

SourceIdQuery = Annotated[str, Query(min_length=1, max_length=200)]


class TranscriptionRunCreate(BaseModel):
    """Values are checked by the service, which answers 422 with a code for each bad one."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: str
    audio_stream_index: int
    model: str | None = None
    language: str | None = None
    budget_usd: str | None = None
    force: bool = False


class TranscriptionEstimateRequest(BaseModel):
    """The values of ``TranscriptionRunCreate`` that decide the price and the transcript."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: str
    audio_stream_index: int
    model: str | None = None
    language: str | None = None
    budget_usd: str | None = None


class ExistingTranscriptOut(BaseModel):
    transcript_id: str
    run_id: str


class TranscriptionEstimateOut(BaseModel):
    source_id: str
    audio_stream_index: int
    model: str
    language: str | None
    duration_ms: int
    chunk_count: int
    billed_seconds: int
    usd_per_second_upper: str
    estimate_micro_usd: int
    price_basis: str
    budget_usd: str | None
    within_budget: bool | None
    existing: ExistingTranscriptOut | None


class TranscriptionRunCreated(BaseModel):
    id: str
    job_id: str
    status: str


class RunChunkOut(BaseModel):
    id: str
    core_start_ms: int
    core_end_ms: int
    state: str
    attempts: int
    error_code: str | None


class TranscriptionRunOut(BaseModel):
    id: str
    source_id: str
    job_id: str | None
    model: str
    status: str
    revision_id: str | None
    error_code: str | None
    committed_micro_usd: int
    chunks: list[RunChunkOut]


class TranscriptionRunItemOut(BaseModel):
    id: str
    source_id: str
    job_id: str | None
    model: str
    status: str
    revision_id: str | None
    error_code: str | None
    committed_micro_usd: int
    created_at: str


class TranscriptionRunList(BaseModel):
    items: list[TranscriptionRunItemOut]


def _existing_out(existing: ExistingTranscript | None) -> ExistingTranscriptOut | None:
    if existing is None:
        return None
    return ExistingTranscriptOut(transcript_id=existing.transcript_id, run_id=existing.run_id)


def _estimate_out(estimate: RunEstimate) -> TranscriptionEstimateOut:
    return TranscriptionEstimateOut(
        source_id=estimate.source_id,
        audio_stream_index=estimate.audio_stream_index,
        model=estimate.model,
        language=estimate.language,
        duration_ms=estimate.duration_ms,
        chunk_count=estimate.chunk_count,
        billed_seconds=estimate.billed_seconds,
        usd_per_second_upper=estimate.usd_per_second_upper,
        estimate_micro_usd=estimate.estimate_micro_usd,
        price_basis=estimate.price_basis,
        budget_usd=estimate.budget_usd,
        within_budget=estimate.within_budget,
        existing=_existing_out(estimate.existing),
    )


def _item_out(item: RunListItem) -> TranscriptionRunItemOut:
    return TranscriptionRunItemOut(
        id=item.id,
        source_id=item.source_id,
        job_id=item.job_id,
        model=item.model,
        status=item.status,
        revision_id=item.revision_id,
        error_code=item.error_code,
        committed_micro_usd=item.committed_micro_usd,
        created_at=item.created_at,
    )


def _chunk_out(chunk: RunChunkView) -> RunChunkOut:
    return RunChunkOut(
        id=chunk.id,
        core_start_ms=chunk.core_start_ms,
        core_end_ms=chunk.core_end_ms,
        state=chunk.state,
        attempts=chunk.attempts,
        error_code=chunk.error_code,
    )


def _run_out(run: RunView) -> TranscriptionRunOut:
    return TranscriptionRunOut(
        id=run.id,
        source_id=run.source_id,
        job_id=run.job_id,
        model=run.model,
        status=run.status,
        revision_id=run.revision_id,
        error_code=run.error_code,
        committed_micro_usd=run.committed_micro_usd,
        chunks=[_chunk_out(chunk) for chunk in run.chunks],
    )


def build_router(runtime: Runtime) -> APIRouter:
    router = APIRouter()

    # Plain `def` routes run in FastAPI's thread pool: SQLite calls block.
    @router.post("/transcription-runs", status_code=202, response_model=TranscriptionRunCreated)
    def create_transcription_run(body: TranscriptionRunCreate) -> TranscriptionRunCreated:
        created = runtime.transcription_jobs.create(
            source_id=body.source_id,
            audio_stream_index=body.audio_stream_index,
            model=body.model,
            language=body.language,
            budget_usd=body.budget_usd,
            force=body.force,
        )
        return TranscriptionRunCreated(id=created.id, job_id=created.job_id, status=created.status)

    @router.post("/transcription-runs/estimate", response_model=TranscriptionEstimateOut)
    def estimate_transcription_run(body: TranscriptionEstimateRequest) -> TranscriptionEstimateOut:
        return _estimate_out(
            runtime.transcription_jobs.estimate(
                source_id=body.source_id,
                audio_stream_index=body.audio_stream_index,
                model=body.model,
                language=body.language,
                budget_usd=body.budget_usd,
            )
        )

    @router.get("/transcription-runs", response_model=TranscriptionRunList)
    def list_transcription_runs(source_id: SourceIdQuery) -> TranscriptionRunList:
        return TranscriptionRunList(
            items=[_item_out(item) for item in runtime.transcription_jobs.list_runs(source_id)]
        )

    @router.get("/transcription-runs/{run_id}", response_model=TranscriptionRunOut)
    def get_transcription_run(run_id: str) -> TranscriptionRunOut:
        return _run_out(runtime.transcription_jobs.get_run(run_id))

    return router
