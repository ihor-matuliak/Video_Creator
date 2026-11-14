"""Projects (FR-001): create, list, rename and archive."""

from __future__ import annotations

from clipforge.services.errors import ServiceError, invalid, invalid_cursor, not_found
from clipforge.services.text import clean_text
from clipforge.storage import Database, Page, Project, ProjectRepository

PROJECT_NAME_MAX = 120


class ProjectService:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, name: str) -> Project:
        """``name`` is trimmed and must then have 1-120 characters."""
        clean = _name(name)
        with self._database.write_session() as session:
            return ProjectRepository(session).add(clean)

    def list_page(
        self, *, limit: int, cursor: str | None, include_archived: bool = False
    ) -> Page[Project]:
        with self._database.session() as session:
            try:
                return ProjectRepository(session).list_page(
                    limit=limit, after=cursor, include_archived=include_archived
                )
            except ValueError:
                raise invalid_cursor() from None

    def update(self, project_id: str, *, name: str | None, archived: bool | None) -> Project:
        """Rename and/or archive the project. ``None`` leaves a field as it is.

        A request that would change nothing is rejected (NO_CHANGES), so a repeated request does
        not rewrite ``updated_at``. Every check runs before the first write.
        """
        with self._database.write_session() as session:
            repo = ProjectRepository(session)
            project = repo.get(project_id)
            if project is None:
                raise not_found("PROJECT_NOT_FOUND", "Проєкт не знайдено")

            new_name: str | None = None
            if name is not None:
                clean = _name(name)
                if clean != project.name:
                    new_name = clean
            new_archived: bool | None = None
            if archived is not None and archived != (project.archived_at is not None):
                new_archived = archived
            if new_name is None and new_archived is None:
                raise _no_changes()

            if new_name is not None:
                repo.rename(project_id, new_name)
            if new_archived is True:
                repo.archive(project_id)
            elif new_archived is False:
                repo.unarchive(project_id)
            return project


def _name(value: str) -> str:
    return clean_text(
        value,
        field="name",
        code="INVALID_PROJECT_NAME",
        label="Назва проєкту",
        max_length=PROJECT_NAME_MAX,
    )


def _no_changes() -> ServiceError:
    return invalid("NO_CHANGES", "Немає змін для збереження", field="body")
