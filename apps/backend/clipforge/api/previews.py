"""POST /v1/sources/{id}/previews, GET /v1/previews/{id}, GET /v1/previews/{id}/media (ADR 021)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from clipforge.api.media import media_response
from clipforge.services.previews import PreviewView
from clipforge.services.runtime import Runtime


class PreviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    audio_stream_index: int | None
    """Global source stream index of the audio to keep; ``null`` for a proxy without sound."""


class PreviewCreated(BaseModel):
    id: str
    job_id: str
    status: str


class PreviewError(BaseModel):
    code: str
    message: str


class PreviewOut(BaseModel):
    id: str
    source_id: str
    audio_stream_index: int | None
    status: str
    job_id: str
    error: PreviewError | None


def preview_out(preview: PreviewView) -> PreviewOut:
    return PreviewOut(
        id=preview.id,
        source_id=preview.source_id,
        audio_stream_index=preview.audio_stream_index,
        status=preview.status,
        job_id=preview.job_id,
        error=PreviewError(**preview.error) if preview.error is not None else None,
    )


def build_router(runtime: Runtime) -> APIRouter:
    router = APIRouter()

    # Plain `def` routes run in FastAPI's thread pool: they touch the database and the disk.
    @router.post("/sources/{source_id}/previews", status_code=202, response_model=PreviewCreated)
    def create_preview(source_id: str, body: PreviewCreate, response: Response) -> PreviewCreated:
        created = runtime.previews.create(source_id, body.audio_stream_index)
        if created.status == "ready":
            response.status_code = 200  # an existing, still valid preview: nothing was started
        return PreviewCreated(id=created.id, job_id=created.job_id, status=created.status)

    @router.get("/previews/{preview_id}", response_model=PreviewOut)
    def get_preview(preview_id: str) -> PreviewOut:
        return preview_out(runtime.previews.get(preview_id))

    @router.get("/previews/{preview_id}/media")
    def get_preview_media(
        preview_id: str, range_header: Annotated[str | None, Header(alias="Range")] = None
    ) -> Response:
        return media_response(runtime.previews.open_media(preview_id), range_header)

    return router
