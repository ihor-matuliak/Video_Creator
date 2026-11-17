"""POST /v1/analysis-runs, POST /v1/analysis-runs/estimate, GET /v1/analysis-runs and
GET /v1/analysis-runs/{id} (ADR 041 §10, §20).

A run is started as an ``analysis`` job; ``POST /v1/jobs/{id}/cancel`` and ``/resume`` control it.
The search profile is checked by the service, which names the bad field in ``details.field`` as
``profile.<field>``. ``model`` and ``budget_usd`` default to the settings. The API key is never part
of a request or a response: the sidecar holds it in memory (``POST /v1/runtime/credentials``).
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from clipforge.services.analysis_jobs import (
    AnalysisEstimate,
    AnalysisRunListItem,
    AnalysisRunView,
    WindowCounts,
)
from clipforge.services.runtime import Runtime

SourceIdQuery = Annotated[str, Query(min_length=1, max_length=200)]


class AnalysisRunRequest(BaseModel):
    """Values are checked by the service, which answers 422 with a code for each bad one.
    ``profile`` and ``budget_usd`` are any JSON value here: their shape is the service's rule, so
    a bad one names its field (``budget_usd: true`` is INVALID_RUN_BUDGET, not a schema error)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: str
    transcript_id: str
    profile: object
    model: str | None = None
    budget_usd: object | None = None


class AnalysisRunCreated(BaseModel):
    id: str
    job_id: str
    status: str


class AnalysisEstimateOut(BaseModel):
    model: str
    unit_level: str
    unit_count: int
    window_count: int
    estimate_micro_usd: int
    prompt_usd_per_mtok: str
    completion_usd_per_mtok: str
    max_tokens: int
    price_basis: str
    budget_usd: str | None
    within_budget: bool | None


class WindowCountsOut(BaseModel):
    total: int
    succeeded: int
    failed: int
    unknown_outcome: int
    pending: int


class AnalysisRunOut(BaseModel):
    id: str
    source_id: str
    transcript_id: str
    transcript_revision: int
    model: str
    provider: str
    prompt_version: str
    schema_version: int
    unit_level: str
    status: str
    error_code: str | None
    created_at: str
    updated_at: str
    job_id: str | None
    committed_micro_usd: int
    windows: WindowCountsOut
    clip_ids: list[str]
    stale: bool
    diagnostics_summary: dict[str, dict[str, int]]


class AnalysisRunItemOut(BaseModel):
    id: str
    source_id: str
    transcript_id: str
    transcript_revision: int
    model: str
    status: str
    error_code: str | None
    job_id: str | None
    committed_micro_usd: int
    created_at: str


class AnalysisRunList(BaseModel):
    items: list[AnalysisRunItemOut]


def _estimate_out(estimate: AnalysisEstimate) -> AnalysisEstimateOut:
    return AnalysisEstimateOut(
        model=estimate.model,
        unit_level=estimate.unit_level,
        unit_count=estimate.unit_count,
        window_count=estimate.window_count,
        estimate_micro_usd=estimate.estimate_micro_usd,
        prompt_usd_per_mtok=estimate.prompt_usd_per_mtok,
        completion_usd_per_mtok=estimate.completion_usd_per_mtok,
        max_tokens=estimate.max_tokens,
        price_basis=estimate.price_basis,
        budget_usd=estimate.budget_usd,
        within_budget=estimate.within_budget,
    )


def _budget(value: object) -> str | int | float | None:
    """``budget_usd`` goes to the service as the client sent it. The service's annotation is
    narrower than what JSON can carry: a bool, a list or an object is refused there (422
    INVALID_RUN_BUDGET), so the type is widened here only for mypy."""
    return cast("str | int | float | None", value)


def _windows_out(counts: WindowCounts) -> WindowCountsOut:
    return WindowCountsOut(
        total=counts.total,
        succeeded=counts.succeeded,
        failed=counts.failed,
        unknown_outcome=counts.unknown_outcome,
        pending=counts.pending,
    )


def _run_out(run: AnalysisRunView) -> AnalysisRunOut:
    return AnalysisRunOut(
        id=run.id,
        source_id=run.source_id,
        transcript_id=run.transcript_id,
        transcript_revision=run.transcript_revision,
        model=run.model,
        provider=run.provider,
        prompt_version=run.prompt_version,
        schema_version=run.schema_version,
        unit_level=run.unit_level,
        status=run.status,
        error_code=run.error_code,
        created_at=run.created_at,
        updated_at=run.updated_at,
        job_id=run.job_id,
        committed_micro_usd=run.committed_micro_usd,
        windows=_windows_out(run.windows),
        clip_ids=list(run.clip_ids),
        stale=run.stale,
        diagnostics_summary={
            section: dict(reasons) for section, reasons in run.diagnostics_summary.items()
        },
    )


def _item_out(item: AnalysisRunListItem) -> AnalysisRunItemOut:
    return AnalysisRunItemOut(
        id=item.id,
        source_id=item.source_id,
        transcript_id=item.transcript_id,
        transcript_revision=item.transcript_revision,
        model=item.model,
        status=item.status,
        error_code=item.error_code,
        job_id=item.job_id,
        committed_micro_usd=item.committed_micro_usd,
        created_at=item.created_at,
    )


def build_router(runtime: Runtime) -> APIRouter:
    router = APIRouter()

    # Plain `def` routes run in FastAPI's thread pool: SQLite calls block.
    @router.post("/analysis-runs", status_code=202, response_model=AnalysisRunCreated)
    def create_analysis_run(body: AnalysisRunRequest) -> AnalysisRunCreated:
        created = runtime.analysis_jobs.create(
            source_id=body.source_id,
            transcript_id=body.transcript_id,
            profile=body.profile,
            model=body.model,
            budget_usd=_budget(body.budget_usd),
        )
        return AnalysisRunCreated(id=created.id, job_id=created.job_id, status=created.status)

    @router.post("/analysis-runs/estimate", response_model=AnalysisEstimateOut)
    def estimate_analysis_run(body: AnalysisRunRequest) -> AnalysisEstimateOut:
        return _estimate_out(
            runtime.analysis_jobs.estimate(
                source_id=body.source_id,
                transcript_id=body.transcript_id,
                profile=body.profile,
                model=body.model,
                budget_usd=_budget(body.budget_usd),
            )
        )

    @router.get("/analysis-runs", response_model=AnalysisRunList)
    def list_analysis_runs(source_id: SourceIdQuery) -> AnalysisRunList:
        return AnalysisRunList(
            items=[_item_out(item) for item in runtime.analysis_jobs.list_runs(source_id)]
        )

    @router.get("/analysis-runs/{run_id}", response_model=AnalysisRunOut)
    def get_analysis_run(run_id: str) -> AnalysisRunOut:
        return _run_out(runtime.analysis_jobs.get_run(run_id))

    return router
