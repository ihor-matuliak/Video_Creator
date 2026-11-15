"""POST/GET /v1/sources, GET /v1/sources/{id}, POST /v1/sources/{id}/relink, GET .../media."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from clipforge.api.media import media_response
from clipforge.api.pagination import DEFAULT_PAGE_SIZE, Cursor
from clipforge.services.runtime import Runtime
from clipforge.storage import Source

SOURCE_MAX_PAGE_SIZE = 100
SourceLimit = Annotated[int, Query(ge=1, le=SOURCE_MAX_PAGE_SIZE)]
ProjectId = Annotated[str, Query(min_length=1, max_length=200)]


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    project_id: str
    path: str


class SourceRelink(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str


class AudioStreamOut(BaseModel):
    index: int
    """Global stream index in the source: the stable track ID (FR-005)."""
    codec: str
    channels: int | None
    language: str | None
    title: str | None
    default: bool
    start_offset_ms: int | None


class SourceOut(BaseModel):
    id: str
    project_id: str
    path: str
    display_name: str
    size: int
    mtime_ns: int
    quick_fingerprint: str
    sha256: str | None
    hash_status: str
    duration_ms: int
    video_stream_index: int
    audio_streams: list[AudioStreamOut]
    canonical_origin: dict[str, Any]
    probe: dict[str, Any]
    availability: str
    created_at: str
    hash_job_id: str | None
    """The SHA-256 job queued by this registration; ``null`` in every other response."""


class SourcePage(BaseModel):
    items: list[SourceOut]
    next_cursor: str | None


def source_out(source: Source, *, hash_job_id: str | None = None) -> SourceOut:
    return SourceOut(
        id=source.id,
        project_id=source.project_id,
        path=source.path,
        display_name=source.display_name,
        size=source.size,
        mtime_ns=source.mtime,
        quick_fingerprint=source.quick_fingerprint,
        sha256=source.sha256,
        hash_status=source.hash_status,
        duration_ms=source.duration_ms,
        video_stream_index=source.video_stream_index,
        audio_streams=[AudioStreamOut(**stream) for stream in source.audio_streams],
        canonical_origin=source.canonical_origin,
        probe=source.probe_json,
        availability=source.availability,
        created_at=source.created_at,
        hash_job_id=hash_job_id,
    )


def build_router(runtime: Runtime) -> APIRouter:
    router = APIRouter()

    # Plain `def` routes run in FastAPI's thread pool: probing and hashing block.
    @router.post("/sources", status_code=201, response_model=SourceOut)
    def create_source(body: SourceCreate) -> SourceOut:
        registered = runtime.sources.register(body.project_id, body.path)
        return source_out(registered.source, hash_job_id=registered.hash_job_id)

    @router.get("/sources", response_model=SourcePage)
    def list_sources(
        project_id: ProjectId,
        limit: SourceLimit = DEFAULT_PAGE_SIZE,
        cursor: Cursor = None,
    ) -> SourcePage:
        page = runtime.sources.list_page(project_id, limit=limit, cursor=cursor)
        return SourcePage(
            items=[source_out(source) for source in page.items], next_cursor=page.next_cursor
        )

    @router.get("/sources/{source_id}", response_model=SourceOut)
    def get_source(source_id: str) -> SourceOut:
        return source_out(runtime.sources.get(source_id))

    @router.post("/sources/{source_id}/relink", response_model=SourceOut)
    def relink_source(source_id: str, body: SourceRelink) -> SourceOut:
        return source_out(runtime.sources.relink(source_id, body.path))

    # Plain `def`: opening the file and reading chunks block, so they run in the thread pool.
    @router.get("/sources/{source_id}/media")
    def get_source_media(
        source_id: str, range_header: Annotated[str | None, Header(alias="Range")] = None
    ) -> Response:
        return media_response(runtime.sources.open_media(source_id), range_header)

    return router
