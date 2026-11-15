"""POST /v1/projects, GET /v1/projects, PATCH /v1/projects/{id} (FR-001)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from clipforge.api.pagination import DEFAULT_PAGE_SIZE, Cursor, Limit
from clipforge.services.runtime import Runtime
from clipforge.storage import Project, ProjectSummary

SEARCH_MAX_LENGTH = 200
Search = Annotated[str | None, Query(max_length=SEARCH_MAX_LENGTH)]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str


class ProjectPatch(BaseModel):
    """Editable fields only; ``null`` or an absent field leaves the value as it is."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str | None = None
    archived: bool | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    created_at: str
    updated_at: str
    archived_at: str | None


class ProjectUpdateOut(ProjectOut):
    """PATCH response: the POST shape plus the ``archived`` flag."""

    archived: bool


class ProjectListItem(ProjectOut):
    """GET list item: the POST shape plus the figures of the project list (docs/03 section 4)."""

    source_count: int
    clip_count: int
    last_activity_at: str


class ProjectPage(BaseModel):
    items: list[ProjectListItem]
    next_cursor: str | None


def project_update_out(project: Project) -> ProjectUpdateOut:
    base = ProjectOut.model_validate(project)
    return ProjectUpdateOut(**base.model_dump(), archived=project.archived_at is not None)


def project_list_item(summary: ProjectSummary) -> ProjectListItem:
    base = ProjectOut.model_validate(summary.project)
    return ProjectListItem(
        **base.model_dump(),
        source_count=summary.source_count,
        clip_count=summary.clip_count,
        last_activity_at=summary.last_activity_at,
    )


def build_router(runtime: Runtime) -> APIRouter:
    router = APIRouter()

    # Plain `def` routes run in FastAPI's thread pool: SQLite and FFprobe calls block.
    @router.post("/projects", status_code=201, response_model=ProjectOut)
    def create_project(body: ProjectCreate) -> ProjectOut:
        return ProjectOut.model_validate(runtime.projects.create(body.name))

    @router.get("/projects", response_model=ProjectPage)
    def list_projects(
        limit: Limit = DEFAULT_PAGE_SIZE,
        cursor: Cursor = None,
        include_archived: bool = False,
        q: Search = None,
    ) -> ProjectPage:
        page = runtime.projects.list_page(
            limit=limit, cursor=cursor, include_archived=include_archived, query=q
        )
        return ProjectPage(
            items=[project_list_item(summary) for summary in page.items],
            next_cursor=page.next_cursor,
        )

    # An absent body counts as an empty one: it changes nothing and is answered NO_CHANGES.
    @router.patch("/projects/{project_id}", response_model=ProjectUpdateOut)
    def update_project(project_id: str, body: ProjectPatch | None = None) -> ProjectUpdateOut:
        project = runtime.projects.update(
            project_id,
            name=None if body is None else body.name,
            archived=None if body is None else body.archived,
        )
        return project_update_out(project)

    return router
