"""GET /v1/jobs/{id}, POST /v1/jobs/{id}/cancel, POST /v1/jobs/{id}/resume."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from clipforge.services.jobs import JobView
from clipforge.services.runtime import Runtime


class JobError(BaseModel):
    code: str
    message: str


class JobOut(BaseModel):
    id: str
    type: str
    state: str
    progress: dict[str, Any]
    error: JobError | None


class JobCancel(BaseModel):
    """The body is ``{}``; it carries nothing, so any field is refused."""

    model_config = ConfigDict(extra="forbid", strict=True)


class JobResume(BaseModel):
    """``retry_unknown`` is the user's explicit decision to send again the requests whose outcome
    is unknown (they may already have been paid for); without it they are not sent. ``budget_usd``
    replaces the budget ceiling of a ``transcription`` run (the service checks its value)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    retry_unknown: bool = False
    budget_usd: str | None = None


def job_out(job: JobView) -> JobOut:
    return JobOut(
        id=job.id,
        type=job.type,
        state=job.state,
        progress=job.progress,
        error=JobError(**job.error) if job.error is not None else None,
    )


def build_router(runtime: Runtime) -> APIRouter:
    router = APIRouter()

    @router.get("/jobs/{job_id}", response_model=JobOut)
    def get_job(job_id: str) -> JobOut:
        return job_out(runtime.jobs.get(job_id))

    @router.post("/jobs/{job_id}/cancel", status_code=202, response_model=JobOut)
    def cancel_job(job_id: str, body: JobCancel | None = None) -> JobOut:
        return job_out(runtime.jobs.cancel(job_id))

    @router.post("/jobs/{job_id}/resume", status_code=202, response_model=JobOut)
    def resume_job(job_id: str, body: JobResume | None = None) -> JobOut:
        retry_unknown = body.retry_unknown if body is not None else False
        budget_usd = body.budget_usd if body is not None else None
        return job_out(
            runtime.jobs.resume(job_id, retry_unknown=retry_unknown, budget_usd=budget_usd)
        )

    return router
