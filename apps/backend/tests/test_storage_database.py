"""open_database: pragmas, workspace lock, backup before upgrade, migration failures."""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, text

from clipforge.storage import (
    Base,
    Database,
    MigrationError,
    ProjectRepository,
    WorkspaceLocked,
    open_database,
)
from clipforge.storage.database import BACKUP_DIRNAME, BUSY_TIMEOUT_MS, DB_FILENAME
from clipforge.storage.migrate import MIGRATIONS_DIR

_CHILD_OPEN = """
import sys
from pathlib import Path

from clipforge.storage import WorkspaceLocked, open_database

try:
    open_database(Path(sys.argv[1])).close()
except WorkspaceLocked:
    print("locked")
else:
    print("opened")
"""

_REVISION_TEMPLATE = '''"""Test revision."""

from alembic import op
import sqlalchemy as sa

revision = "{revision}"
down_revision = {down!r}
branch_labels = None
depends_on = None


def upgrade() -> None:
{body}
'''


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = open_database(tmp_path / "data")
    try:
        yield database
    finally:
        database.close()


def make_migrations(root: Path) -> Path:
    """A throw-away migrations directory that reuses the real env.py."""
    (root / "versions").mkdir(parents=True)
    shutil.copyfile(MIGRATIONS_DIR / "env.py", root / "env.py")
    return root


def write_revision(root: Path, revision: str, down: str | None, body: str) -> None:
    source = _REVISION_TEMPLATE.format(revision=revision, down=down, body=body)
    (root / "versions" / f"r{revision}.py").write_text(source, encoding="utf-8")


def table_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {name for (name,) in rows}


def stored_revision(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        (version,) = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert isinstance(version, str)
    return version


def run_child_open(data_dir: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-I", "-c", _CHILD_OPEN, str(data_dir)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_database_is_in_wal_mode(db: Database) -> None:
    with db.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
    # journal_mode is stored in the file header, so a foreign connection sees it too.
    with closing(sqlite3.connect(db.data_dir / DB_FILENAME)) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone() == ("wal",)


def test_every_connection_enforces_foreign_keys_and_busy_timeout(db: Database) -> None:
    first = db.engine.connect()
    second = db.engine.connect()  # a second pooled connection, not a reused one
    try:
        for connection in (first, second):
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            assert timeout == BUSY_TIMEOUT_MS
    finally:
        second.close()
        first.close()


def test_write_session_takes_the_write_lock_when_it_begins(db: Database) -> None:
    db_path = db.data_dir / DB_FILENAME
    with db.write_session() as session:
        session.execute(text("SELECT 1"))
        with closing(sqlite3.connect(db_path, timeout=0)) as other:
            other.isolation_level = None
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
    with db.session() as session:
        session.execute(text("SELECT 1"))
        with closing(sqlite3.connect(db_path, timeout=0)) as other:
            other.isolation_level = None
            other.execute("BEGIN IMMEDIATE")  # a deferred reader does not block a writer
            other.execute("ROLLBACK")


def test_second_open_of_the_same_data_dir_fails_while_the_first_holds_the_lock(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    first = open_database(data_dir)
    try:
        with pytest.raises(WorkspaceLocked):
            open_database(data_dir)
        # The failed attempt must not have released or damaged the first holder.
        with first.session() as session:
            ProjectRepository(session).add("Усе ще працює")
    finally:
        first.close()
    open_database(data_dir).close()


def test_second_process_cannot_open_a_locked_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "дані з пробілом"
    with open_database(data_dir):
        assert run_child_open(data_dir) == "locked"
    assert run_child_open(data_dir) == "opened"


def test_cyrillic_data_dir_path(tmp_path: Path) -> None:
    data_dir = tmp_path / "Мої відео" / "дані_ClipForge"
    with open_database(data_dir) as database:
        with database.write_session() as session:
            project_id = ProjectRepository(session).add("Проєкт «Стрім»").id
        assert (data_dir / DB_FILENAME).is_file()
        assert (data_dir / "workspace.lock").is_file()
    with open_database(data_dir) as database, database.session() as session:
        project = ProjectRepository(session).get(project_id)
        assert project is not None
        assert project.name == "Проєкт «Стрім»"


def test_fresh_database_and_reopen_at_head_take_no_backup(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    with open_database(data_dir):
        pass
    with open_database(data_dir):
        pass
    assert not (data_dir / BACKUP_DIRNAME).exists()


def test_backup_is_created_before_an_upgrade_of_an_existing_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "дані"
    migrations = make_migrations(tmp_path / "міграції")
    write_revision(
        migrations,
        "0001",
        None,
        "    op.create_table('notes', sa.Column('id', sa.Integer(), primary_key=True),\n"
        "                    sa.Column('body', sa.Text()))\n"
        "    op.execute(\"INSERT INTO notes (id, body) VALUES (1, 'до оновлення')\")",
    )
    with open_database(data_dir, migrations_dir=migrations):
        pass
    assert not (data_dir / BACKUP_DIRNAME).exists()

    write_revision(
        migrations, "0002", "0001", "    op.create_table('extra', sa.Column('id', sa.Integer()))"
    )
    with open_database(data_dir, migrations_dir=migrations):
        pass

    db_path = data_dir / DB_FILENAME
    assert stored_revision(db_path) == "0002"
    assert "extra" in table_names(db_path)
    backups = list((data_dir / BACKUP_DIRNAME).iterdir())
    assert len(backups) == 1
    assert re.fullmatch(r"clipforge-\d{8}T\d{12}Z-from-0001\.sqlite3", backups[0].name)
    # The copy is a complete database at the OLD version.
    assert stored_revision(backups[0]) == "0001"
    assert "extra" not in table_names(backups[0])
    with closing(sqlite3.connect(backups[0])) as raw:
        assert raw.execute("SELECT body FROM notes").fetchall() == [("до оновлення",)]
    assert not list((data_dir / BACKUP_DIRNAME).glob("*-wal"))


def test_failed_migration_keeps_the_previous_schema_and_the_backup(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    migrations = make_migrations(tmp_path / "migrations")
    write_revision(
        migrations, "0001", None, "    op.create_table('a', sa.Column('id', sa.Integer()))"
    )
    with open_database(data_dir, migrations_dir=migrations):
        pass
    write_revision(
        migrations, "0002", "0001", "    op.create_table('b', sa.Column('id', sa.Integer()))"
    )
    write_revision(
        migrations,
        "0003",
        "0002",
        "    op.create_table('c', sa.Column('id', sa.Integer()))\n    raise RuntimeError('boom')",
    )

    with pytest.raises(MigrationError) as failure:
        open_database(data_dir, migrations_dir=migrations)

    db_path = data_dir / DB_FILENAME
    # One transaction: neither 0002 nor 0003 was applied, not even partially.
    assert stored_revision(db_path) == "0001"
    assert {"a"} <= table_names(db_path)
    assert not ({"b", "c"} & table_names(db_path))
    assert failure.value.backup_path is not None
    assert failure.value.backup_path.is_file()
    assert stored_revision(failure.value.backup_path) == "0001"
    assert "boom" not in str(failure.value)
    # The lock was released: the same data dir can be opened again once the script is fixed.
    (migrations / "versions" / "r0003.py").unlink()
    with open_database(data_dir, migrations_dir=migrations):
        pass
    assert stored_revision(db_path) == "0002"


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_delete_mode_database(db_path: Path, *statements: str) -> str:
    """A foreign database in journal_mode=delete; returns the SHA-256 of its bytes."""
    with closing(sqlite3.connect(db_path)) as raw:
        raw.isolation_level = None
        assert raw.execute("PRAGMA journal_mode=delete").fetchone() == ("delete",)
        for statement in statements:
            raw.execute(statement)
    return file_digest(db_path)


def assert_left_as_found(db_path: Path, digest: str) -> None:
    """Refusing a database must not even switch it to WAL (ADR 019)."""
    assert file_digest(db_path) == digest
    assert sorted(path.name for path in db_path.parent.glob(f"{DB_FILENAME}-*")) == []
    with closing(sqlite3.connect(db_path)) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    assert file_digest(db_path) == digest
    assert not (db_path.parent / BACKUP_DIRNAME).exists()


def test_database_without_a_version_is_never_touched(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / DB_FILENAME
    digest = make_delete_mode_database(db_path, "CREATE TABLE legacy (id INTEGER PRIMARY KEY)")

    with pytest.raises(MigrationError):
        open_database(data_dir)

    assert table_names(db_path) == {"legacy"}
    assert_left_as_found(db_path, digest)
    with pytest.raises(MigrationError):  # still not locked out by the first failure
        open_database(data_dir)
    assert_left_as_found(db_path, digest)


def test_database_written_by_a_newer_build_is_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / DB_FILENAME
    digest = make_delete_mode_database(
        db_path,
        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)",
        "INSERT INTO alembic_version VALUES ('9999')",
    )

    with pytest.raises(MigrationError):
        open_database(data_dir)

    assert stored_revision(db_path) == "9999"
    assert_left_as_found(db_path, digest)
    with pytest.raises(MigrationError):
        open_database(data_dir)
    assert_left_as_found(db_path, digest)


def test_accepted_database_in_delete_mode_is_switched_to_wal(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    with open_database(data_dir):
        pass
    db_path = data_dir / DB_FILENAME
    with closing(sqlite3.connect(db_path)) as raw:
        assert raw.execute("PRAGMA journal_mode=delete").fetchone() == ("delete",)

    with open_database(data_dir) as database, database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


def test_migrated_schema_matches_the_models(db: Database) -> None:
    with db.engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_model_check_constraints_are_identical_to_the_migration(db: Database) -> None:
    def normalize(sql: str) -> str:
        return re.sub(r"\s+", " ", sql)

    with db.engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
        ).all()
    ddl = {name: normalize(sql) for name, sql in rows}

    checked = 0
    for table in Base.metadata.sorted_tables:
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint):
                expected = (
                    f"CONSTRAINT {constraint.name} CHECK ({normalize(str(constraint.sqltext))})"
                )
                assert expected in ddl[table.name]
                checked += 1
    assert checked >= 10
