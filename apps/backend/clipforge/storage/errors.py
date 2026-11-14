"""Storage-layer errors (ADR 017).

Messages are Ukrainian and never contain file paths or stored values, so they are safe to log.
The API layer maps these to its own error codes (for example ``StaleRevision`` -> 409).
"""

from __future__ import annotations

from pathlib import Path


class StorageError(Exception):
    """Base class for storage failures."""


class WorkspaceLocked(StorageError):
    """Another process already holds the workspace lock of this data directory."""

    def __init__(self) -> None:
        super().__init__("Робочу теку вже використовує інший екземпляр ClipForge")


class MigrationError(StorageError):
    """The database schema could not be brought to the current version.

    ``backup_path`` is the copy taken before the attempt (``None`` when no backup was needed).
    The schema stays at its previous version: the upgrade is one transaction.
    """

    def __init__(self, message: str, *, backup_path: Path | None = None) -> None:
        super().__init__(message)
        self.backup_path = backup_path


class RecordNotFound(StorageError):
    def __init__(self, entity: str, record_id: str) -> None:
        super().__init__(f"Запис не знайдено: {entity}")
        self.entity = entity
        self.record_id = record_id


class StaleRevision(StorageError):
    """The clip was changed since the caller read it; nothing was written."""

    def __init__(self, clip_id: str, expected_revision: int, current_revision: int) -> None:
        super().__init__("Кліп змінено в іншому місці: ревізія застаріла")
        self.clip_id = clip_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision
