"""POST /v1/export-batches, GET /v1/export-batches/{id}."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from clipforge.services.exports import ExportBatchView, ExportItemView, ExportProfile
from clipforge.services.runtime import Runtime


class ExportProfileIn(BaseModel):
    """Values are checked by the service, which answers 422 with a code for each unsupported one."""

    model_config = ConfigDict(extra="forbid", strict=True)

    video_codec: str
    quality: str
    audio_mode: str
    audio_stream_index: int | None
    write_srt: bool


class ExportBatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    project_id: str
    clip_ids: list[str]
    output_dir: str
    profile: ExportProfileIn


class ExportBatchCreated(BaseModel):
    id: str
    job_id: str
    status: str


class ExportItemError(BaseModel):
    code: str
    message: str


class ExportItemOut(BaseModel):
    id: str
    clip_id: str
    clip_revision: int
    state: str
    final_path: str | None
    observed: dict[str, Any] | None
    error: ExportItemError | None


class ExportBatchOut(BaseModel):
    id: str
    project_id: str
    job_id: str | None
    status: str
    output_dir: str
    profile: dict[str, Any]
    items: list[ExportItemOut]
    manifest_path: str | None
    catalog_path: str | None
    created_at: str


def _item_out(item: ExportItemView) -> ExportItemOut:
    return ExportItemOut(
        id=item.id,
        clip_id=item.clip_id,
        clip_revision=item.clip_revision,
        state=item.state,
        final_path=item.final_path,
        observed=item.observed,
        error=ExportItemError(**item.error) if item.error is not None else None,
    )


def _batch_out(batch: ExportBatchView) -> ExportBatchOut:
    return ExportBatchOut(
        id=batch.id,
        project_id=batch.project_id,
        job_id=batch.job_id,
        status=batch.status,
        output_dir=batch.output_dir,
        profile=batch.profile,
        items=[_item_out(item) for item in batch.items],
        manifest_path=batch.manifest_path,
        catalog_path=batch.catalog_path,
        created_at=batch.created_at,
    )


def build_router(runtime: Runtime) -> APIRouter:
    router = APIRouter()

    # Plain `def` routes run in FastAPI's thread pool: SQLite calls block.
    @router.post("/export-batches", status_code=202, response_model=ExportBatchCreated)
    def create_export_batch(body: ExportBatchCreate) -> ExportBatchCreated:
        created = runtime.exports.create_batch(
            project_id=body.project_id,
            clip_ids=body.clip_ids,
            output_dir=body.output_dir,
            profile=ExportProfile(
                video_codec=body.profile.video_codec,
                quality=body.profile.quality,
                audio_mode=body.profile.audio_mode,
                audio_stream_index=body.profile.audio_stream_index,
                write_srt=body.profile.write_srt,
            ),
        )
        return ExportBatchCreated(id=created.id, job_id=created.job_id, status=created.status)

    @router.get("/export-batches/{batch_id}", response_model=ExportBatchOut)
    def get_export_batch(batch_id: str) -> ExportBatchOut:
        return _batch_out(runtime.exports.get_batch(batch_id))

    return router
