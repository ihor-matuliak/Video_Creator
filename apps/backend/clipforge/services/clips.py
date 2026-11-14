"""Clips (FR-030..FR-032): manual creation, listing, editing and review.

Every change goes through one ``Database.write_session()`` (``BEGIN IMMEDIATE``): the clip is read
and written under the write lock, and the repository's compare-and-set on ``revision`` rejects a
stale write (NFR-004, T-015).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from clipforge.domain.categories import CategoryError, validate_categories
from clipforge.domain.interval import Interval, IntervalError, validate_interval
from clipforge.services.errors import (
    ErrorKind,
    ServiceError,
    conflict,
    invalid,
    invalid_cursor,
    not_found,
)
from clipforge.services.text import clean_text
from clipforge.storage import (
    Clip,
    ClipRepository,
    Database,
    Page,
    RecordNotFound,
    Source,
    SourceRepository,
    StaleRevision,
)

CLIP_TITLE_MAX: Final = 160
REVIEW_STATUSES: Final = ("suggested", "approved", "rejected")
MANUAL_EVIDENCE: Final = "manual"


@dataclass(frozen=True, slots=True)
class ClipEdit:
    """User-editable fields; ``None`` means "leave as it is"."""

    title: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    categories: Sequence[str] | None = None


class ClipService:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create_manual(
        self,
        *,
        source_id: str,
        title: str,
        start_ms: int,
        end_ms: int,
        categories: Sequence[str],
    ) -> Clip:
        with self._database.write_session() as session:
            source = _require_source(SourceRepository(session), source_id)
            clean_title = _title(title)
            interval = _interval(start_ms, end_ms, source)
            checked = _categories(categories)
            return ClipRepository(session).add(
                source_id=source_id,
                title=clean_title,
                start_ms=interval.start_ms,
                end_ms=interval.end_ms,
                categories=checked,
                evidence_type=MANUAL_EVIDENCE,
            )

    def get(self, clip_id: str) -> Clip:
        with self._database.session() as session:
            return _require_clip(ClipRepository(session), clip_id)

    def list_page(
        self,
        *,
        source_id: str | None,
        review_status: str | None,
        category: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page[Clip]:
        if review_status is not None and review_status not in REVIEW_STATUSES:
            raise invalid(
                "INVALID_REVIEW_STATUS", "Невідомий статус перегляду", field="review_status"
            )
        if category is not None:
            _categories([category])
        with self._database.session() as session:
            try:
                return ClipRepository(session).list_page(
                    source_id=source_id,
                    review_status=review_status,
                    category=category,
                    limit=limit,
                    after=cursor,
                )
            except ValueError:
                raise invalid_cursor() from None

    def edit(self, clip_id: str, *, expected_revision: int, edit: ClipEdit) -> Clip:
        """Apply ``edit`` if the clip is still at ``expected_revision``.

        Changing the bounds of a clip whose export is ``verified`` marks that export ``stale``
        (FR-032); the revision is not bumped a second time for it.
        """
        if (
            edit.title is None
            and edit.start_ms is None
            and edit.end_ms is None
            and edit.categories is None
        ):
            raise invalid("NO_CHANGES", "Немає змін для збереження", field="body")
        with self._database.write_session() as session:
            clips = ClipRepository(session)
            current = _require_clip(clips, clip_id)
            if current.revision != expected_revision:
                raise _stale(current.revision)
            source = _require_source(SourceRepository(session), current.source_id)

            changes: dict[str, object] = {}
            if edit.title is not None:
                changes["title"] = _title(edit.title)
            start_ms = current.start_ms if edit.start_ms is None else edit.start_ms
            end_ms = current.end_ms if edit.end_ms is None else edit.end_ms
            bounds_changed = (start_ms, end_ms) != (current.start_ms, current.end_ms)
            if edit.start_ms is not None or edit.end_ms is not None:
                interval = _interval(start_ms, end_ms, source)
                changes["start_ms"], changes["end_ms"] = interval.start_ms, interval.end_ms
            if edit.categories is not None:
                changes["categories"] = list(_categories(edit.categories))

            was_verified = current.export_state == "verified"
            updated = _update(clips, clip_id, expected_revision, changes)
            if bounds_changed and was_verified:
                updated = clips.set_export_state(clip_id, "stale")
            return updated

    def review(self, clip_id: str, *, expected_revision: int, status: str) -> Clip:
        if status not in REVIEW_STATUSES:
            raise invalid("INVALID_REVIEW_STATUS", "Невідомий статус перегляду", field="status")
        with self._database.write_session() as session:
            return _update(
                ClipRepository(session), clip_id, expected_revision, {"review_status": status}
            )


def _update(
    clips: ClipRepository, clip_id: str, expected_revision: int, changes: dict[str, object]
) -> Clip:
    try:
        return clips.update(clip_id, expected_revision=expected_revision, **changes)
    except StaleRevision as stale:
        raise _stale(stale.current_revision) from None
    except RecordNotFound:
        raise _clip_not_found() from None


def _require_clip(clips: ClipRepository, clip_id: str) -> Clip:
    clip = clips.get(clip_id)
    if clip is None:
        raise _clip_not_found()
    return clip


def _require_source(sources: SourceRepository, source_id: str) -> Source:
    source = sources.get(source_id)
    if source is None:
        raise not_found("SOURCE_NOT_FOUND", "Джерело не знайдено")
    return source


def _clip_not_found() -> ServiceError:
    return not_found("CLIP_NOT_FOUND", "Кліп не знайдено")


def _stale(current_revision: int) -> ServiceError:
    return conflict(
        "STALE_REVISION",
        "Кліп змінено в іншому місці. Оновіть дані та повторіть",
        details={"current_revision": current_revision},
    )


def _title(value: str) -> str:
    return clean_text(
        value,
        field="title",
        code="INVALID_CLIP_TITLE",
        label="Назва кліпу",
        max_length=CLIP_TITLE_MAX,
    )


def _interval(start_ms: int, end_ms: int, source: Source) -> Interval:
    try:
        return validate_interval(start_ms, end_ms, source.duration_ms)
    except IntervalError as error:
        raise ServiceError(
            ErrorKind.INVALID,
            "INVALID_INTERVAL",
            error.message,
            details={"field": error.field, "reason": error.code},
        ) from None


def _categories(values: Sequence[str]) -> tuple[str, ...]:
    try:
        return validate_categories(values)
    except CategoryError as error:
        raise ServiceError(
            ErrorKind.INVALID,
            "INVALID_CATEGORIES",
            error.message,
            details={"field": "categories", "reason": error.code},
        ) from None
