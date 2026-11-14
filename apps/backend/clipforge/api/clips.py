"""POST/GET /v1/clips, GET/PATCH /v1/clips/{id}, POST /v1/clips/{id}/review."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from clipforge.api.pagination import DEFAULT_PAGE_SIZE, Cursor, Limit
from clipforge.services.clips import ClipEdit
from clipforge.services.runtime import Runtime
from clipforge.storage import Clip

Revision = Annotated[int, Field(ge=1)]


class ClipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_id: str
    title: str
    start_ms: int
    end_ms: int
    categories: list[str]
    evidence_type: Literal["manual"]


class ClipPatch(BaseModel):
    """Editable fields only; ``null`` or an absent field leaves the value as it is."""

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: Revision
    title: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    categories: list[str] | None = None


class ClipReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: Revision
    status: Literal["approved", "rejected", "suggested"]


class ClipOut(BaseModel):
    id: str
    source_id: str
    analysis_run_id: str | None
    revision: int
    title: str
    start_ms: int
    end_ms: int
    recommended_start_ms: int | None
    recommended_end_ms: int | None
    categories: list[str]
    evidence_type: str
    evidence_ids: list[str]
    reason: str | None
    scores: dict[str, int] | None
    rank: int | None
    visual_review_required: bool
    review_status: str
    export_state: str
    created_at: str
    updated_at: str


class ClipPage(BaseModel):
    items: list[ClipOut]
    next_cursor: str | None


def clip_out(clip: Clip) -> ClipOut:
    fields: dict[str, Any] = {name: getattr(clip, name) for name in ClipOut.model_fields}
    return ClipOut(**fields)


def build_router(runtime: Runtime) -> APIRouter:
    router = APIRouter()

    # Plain `def` routes run in FastAPI's thread pool: SQLite calls block.
    @router.post("/clips", status_code=201, response_model=ClipOut)
    def create_clip(body: ClipCreate) -> ClipOut:
        return clip_out(
            runtime.clips.create_manual(
                source_id=body.source_id,
                title=body.title,
                start_ms=body.start_ms,
                end_ms=body.end_ms,
                categories=body.categories,
            )
        )

    @router.get("/clips", response_model=ClipPage)
    def list_clips(
        source_id: str | None = None,
        review_status: str | None = None,
        category: str | None = None,
        limit: Limit = DEFAULT_PAGE_SIZE,
        cursor: Cursor = None,
    ) -> ClipPage:
        page = runtime.clips.list_page(
            source_id=source_id,
            review_status=review_status,
            category=category,
            limit=limit,
            cursor=cursor,
        )
        return ClipPage(items=[clip_out(clip) for clip in page.items], next_cursor=page.next_cursor)

    @router.get("/clips/{clip_id}", response_model=ClipOut)
    def get_clip(clip_id: str) -> ClipOut:
        return clip_out(runtime.clips.get(clip_id))

    @router.patch("/clips/{clip_id}", response_model=ClipOut)
    def patch_clip(clip_id: str, body: ClipPatch) -> ClipOut:
        edit = ClipEdit(
            title=body.title,
            start_ms=body.start_ms,
            end_ms=body.end_ms,
            categories=body.categories,
        )
        return clip_out(
            runtime.clips.edit(clip_id, expected_revision=body.expected_revision, edit=edit)
        )

    @router.post("/clips/{clip_id}/review", response_model=ClipOut)
    def review_clip(clip_id: str, body: ClipReview) -> ClipOut:
        return clip_out(
            runtime.clips.review(
                clip_id, expected_revision=body.expected_revision, status=body.status
            )
        )

    return router
