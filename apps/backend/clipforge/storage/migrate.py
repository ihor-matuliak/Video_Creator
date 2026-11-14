"""Programmatic Alembic upgrade with a pre-upgrade backup (ADR 017, docs/04 section 7).

Rules:

* a missing or empty database is created by the migrations, no backup (nothing to protect);
* an existing database below ``head`` is copied to ``<data_dir>/backups`` first;
* the upgrade is one transaction (see ``migrations/env.py``): on failure the previous schema
  stays and ``MigrationError`` is raised, the sidecar must not continue;
* a database with tables but no version, or a version this build does not know (written by a
  newer build), is never touched.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import Engine, inspect

from .errors import MigrationError
from .util import utc_stamp

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_VERSION_TABLE = "alembic_version"


def upgrade_to_head(
    engine: Engine, *, db_path: Path, backups_dir: Path, migrations_dir: Path = MIGRATIONS_DIR
) -> Path | None:
    """Bring the schema to head. Returns the backup path when one was taken."""
    config = _alembic_config(migrations_dir)
    try:
        script = ScriptDirectory.from_config(config)
        heads = script.get_heads()
    except CommandError:
        raise MigrationError("Не вдалося прочитати скрипти міграцій") from None
    if len(heads) != 1:
        raise MigrationError("Скрипти міграцій мають рівно одну голову")
    head = heads[0]

    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
        tables = set(inspect(connection).get_table_names()) - {_VERSION_TABLE}

    if current is None and tables:
        raise MigrationError("База даних не має версії схеми; автоматичне оновлення неможливе")
    if current == head:
        return None
    if current is not None:
        try:
            script.get_revision(current)
        except CommandError:
            raise MigrationError("Базу даних створила новіша версія ClipForge") from None

    backup_path = _backup(db_path, backups_dir, current) if current is not None else None
    try:
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
        with engine.connect() as connection:
            reached = MigrationContext.configure(connection).get_current_revision()
    except Exception:
        raise MigrationError(
            "Не вдалося оновити схему бази даних; попередню схему збережено",
            backup_path=backup_path,
        ) from None
    if reached != head:
        raise MigrationError("Схема бази даних не досягла поточної версії", backup_path=backup_path)
    return backup_path


def _alembic_config(migrations_dir: Path) -> Config:
    config = Config()
    # Config values are %-interpolated; the path is data, not a template.
    config.set_main_option("script_location", str(migrations_dir).replace("%", "%%"))
    return config


def _backup(db_path: Path, backups_dir: Path, revision: str) -> Path:
    """Consistent copy through SQLite's online backup API (includes data still in the WAL)."""
    backups_dir.mkdir(parents=True, exist_ok=True)
    safe_revision = re.sub(r"[^A-Za-z0-9_-]", "_", revision)
    target = backups_dir / f"clipforge-{utc_stamp()}-from-{safe_revision}.sqlite3"
    if target.exists():
        raise MigrationError("Резервна копія з такою назвою вже існує")
    try:
        with closing(sqlite3.connect(db_path)) as source, closing(sqlite3.connect(target)) as copy:
            source.backup(copy)
            verdict = copy.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error:
        raise MigrationError("Не вдалося створити резервну копію бази даних") from None
    if verdict != ("ok",):
        raise MigrationError("Резервна копія бази даних не пройшла перевірку цілісності")
    return target
