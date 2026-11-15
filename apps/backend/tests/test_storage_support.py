"""Helpers shared by the test_storage_* modules (not a test module itself)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from itertools import count

from sqlalchemy.orm import Session

from clipforge.storage import (
    Clip,
    ClipRepository,
    Project,
    ProjectRepository,
    Source,
    SourceRepository,
)
from clipforge.storage.util import utc_now

SHA256 = "a" * 64


def add_project(session: Session, name: str = "Проєкт") -> Project:
    return ProjectRepository(session).add(name)


def add_source(
    session: Session,
    project_id: str,
    *,
    duration_ms: int = 60_000,
    now: Callable[[], str] = utc_now,
) -> Source:
    return SourceRepository(session, now=now).add(
        project_id=project_id,
        path="C:\\Відео\\запис.mkv",
        display_name="запис.mkv",
        size=1_000_000,
        mtime=1_700_000_000_000_000_000,
        quick_fingerprint="quick-1",
        hash_status="pending",
        duration_ms=duration_ms,
        video_stream_index=0,
        audio_streams=[{"index": 1, "codec": "aac", "default": True}],
        canonical_origin={"origin_ms": 0, "time_base": "1/1000"},
        probe_json={"format": {"duration": "60.000000"}},
        availability="available",
    )


def add_clip(
    session: Session,
    source_id: str,
    *,
    start_ms: int = 1_000,
    end_ms: int = 5_000,
    title: str = "Кліп",
    categories: Sequence[str] = ("funny",),
    review_status: str = "suggested",
    now: Callable[[], str] = utc_now,
) -> Clip:
    return ClipRepository(session, now=now).add(
        source_id=source_id,
        title=title,
        start_ms=start_ms,
        end_ms=end_ms,
        categories=categories,
        evidence_type="manual",
        review_status=review_status,
    )


def counting_clock() -> Callable[[], str]:
    """Strictly increasing ISO timestamps, one per call."""
    ticks = count(1)
    return lambda: f"2026-11-13T10:00:{next(ticks):02d}.000000Z"
