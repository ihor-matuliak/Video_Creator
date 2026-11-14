"""GET /v1/jobs/{id}, POST /v1/jobs/{id}/cancel."""

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

    return router
