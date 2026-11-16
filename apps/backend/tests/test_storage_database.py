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
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, text

from clipforge.storage import (
    Base,
    Database,
    MigrationError,
    ProjectRepository,
    SettingsRepository,
    WorkspaceLocked,
    open_database,
)
from clipforge.storage.database import BACKUP_DIRNAME, BUSY_TIMEOUT_MS, DB_FILENAME
from clipforge.storage.migrate import MIGRATIONS_DIR

from .support import head_revision
from .test_storage_support import add_project, add_source

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


def table_columns(path: Path, table: str) -> list[tuple[str, int, int]]:
    """``(name, notnull, pk)`` of each column."""
    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return [(row[1], row[3], row[5]) for row in rows]


def test_migration_0005_adds_app_settings_and_its_downgrade_drops_it(tmp_path: Path) -> None:
    only_0004 = make_migrations(tmp_path / "only-0004")
    for name in (
        "r0001_initial.py",
        "r0002_previews.py",
        "r0003_transcripts.py",
        "r0004_spend_micro_usd.py",
    ):
        shutil.copyfile(MIGRATIONS_DIR / "versions" / name, only_0004 / "versions" / name)
    data_dir = tmp_path / "data"
    db_path = data_dir / DB_FILENAME
    with (
        open_database(data_dir, migrations_dir=only_0004) as old,
        old.write_session() as session,
    ):
        project_id = ProjectRepository(session).add("Старий проєкт").id
    assert "app_settings" not in table_names(db_path)

    with open_database(data_dir) as upgraded:
        assert stored_revision(db_path) == head_revision()
        assert table_columns(db_path, "app_settings") == [("key", 1, 1), ("value_json", 1, 0)]
        backup = next((data_dir / BACKUP_DIRNAME).iterdir())
        assert backup.name.endswith("-from-0004.sqlite3")
        assert "app_settings" not in table_names(backup)
        with upgraded.engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []

        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
        with upgraded.engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0004")
            connection.commit()
        assert stored_revision(db_path) == "0004"
        assert "app_settings" not in table_names(db_path)
        with upgraded.session() as session:
            project = ProjectRepository(session).get(project_id)
            assert project is not None
            assert project.name == "Старий проєкт"


_CHUNK_INSERT = (
    "INSERT INTO transcript_chunks (id, run_id, core_start_ms, core_end_ms, upload_start_ms, "
    "upload_end_ms, state, attempts, request_id, response_path, error_code, "
    "reserved_cost, actual_cost) VALUES (?, 'run-1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_COSTS_USD = "SELECT id, state, reserved_cost, actual_cost FROM transcript_chunks ORDER BY id"
_COSTS_MICRO = (
    "SELECT id, state, reserved_micro_usd, actual_micro_usd FROM transcript_chunks ORDER BY id"
)


def chunk_costs(path: Path, query: str) -> list[tuple[object, ...]]:
    """``(id, state, reserved, actual)`` of every chunk, in the units the query selects."""
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(query).fetchall()


def test_migration_0006_converts_chunk_costs_and_keeps_the_words_that_name_a_chunk(
    tmp_path: Path,
) -> None:
    only_0005 = make_migrations(tmp_path / "only-0005")
    for name in (
        "r0001_initial.py",
        "r0002_previews.py",
        "r0003_transcripts.py",
        "r0004_spend_micro_usd.py",
        "r0005_app_settings.py",
    ):
        shutil.copyfile(MIGRATIONS_DIR / "versions" / name, only_0005 / "versions" / name)
    data_dir = tmp_path / "data"
    db_path = data_dir / DB_FILENAME
    with (
        open_database(data_dir, migrations_dir=only_0005) as old,
        old.write_session() as session,
    ):
        source_id = add_source(session, add_project(session).id).id
    assert table_columns(db_path, "transcript_chunks")[-2:] == [
        ("reserved_cost", 1, 0),
        ("actual_cost", 0, 0),
    ]
    with closing(sqlite3.connect(db_path)) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute(
            "INSERT INTO transcript_runs (id, source_id, model, provider, options_hash, status, "
            "created_at) VALUES "
            "('run-1', ?, 'm', 'p', 'h', 'running', '2026-11-15T10:00:00.000000Z')",
            (source_id,),
        )
        raw.executemany(
            _CHUNK_INSERT,
            [
                (
                    "c1",
                    0,
                    300_000,
                    0,
                    305_000,
                    "succeeded",
                    2,
                    "req-1",
                    "c1.json",
                    None,
                    0.0305,
                    2e-05,
                ),
                (
                    "c2",
                    300_000,
                    600_000,
                    295_000,
                    605_000,
                    "failed",
                    3,
                    "req-2",
                    None,
                    "E",
                    1e-07,
                    None,
                ),
                (
                    "c3",
                    600_000,
                    620_000,
                    595_000,
                    620_000,
                    "cancelled",
                    0,
                    None,
                    None,
                    None,
                    0.0,
                    None,
                ),
            ],
        )
        raw.execute(
            "INSERT INTO transcript_revisions (id, source_id, revision, origin, run_id, parent_id, "
            "timing_level, language, duration_ms, word_count, segment_count, coverage, "
            "metadata_json, created_at) VALUES ('rev-1', ?, 1, 'stt', 'run-1', NULL, 'word', "
            "'en', 60000, 1, 0, '[]', '{}', '2026-11-15T10:00:00.000000Z')",
            (source_id,),
        )
        raw.execute(
            "INSERT INTO transcript_words (revision_id, id, ordinal, start_ms, end_ms, text, "
            "chunk_id) VALUES ('rev-1', 'w1', 0, 0, 500, 'hello', 'c1')"
        )
        raw.commit()

    with open_database(data_dir) as upgraded:
        assert stored_revision(db_path) == head_revision()
        assert table_columns(db_path, "transcript_chunks")[-2:] == [
            ("reserved_micro_usd", 1, 0),
            ("actual_micro_usd", 0, 0),
        ]
        # Rounded UP to a micro-USD; a missing cost stays missing.
        expected_up = [
            ("c1", "succeeded", 30_500, 20),
            ("c2", "failed", 1, None),
            ("c3", "cancelled", 0, None),
        ]
        assert chunk_costs(db_path, _COSTS_MICRO) == expected_up
        backup = next((data_dir / BACKUP_DIRNAME).iterdir())
        assert backup.name.endswith("-from-0005.sqlite3")
        assert table_columns(backup, "transcript_chunks")[-2][0] == "reserved_cost"
        with upgraded.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, Base.metadata) == []
            word = connection.exec_driver_sql(
                "SELECT chunk_id FROM transcript_words WHERE revision_id = 'rev-1'"
            ).all()
            assert word == [("c1",)]
        with upgraded.engine.begin() as connection:
            index = connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'transcript_chunks' AND name NOT LIKE 'sqlite_%'"
            ).all()
            assert index == [("ix_transcript_chunks_run_id",)]
            # The new state is accepted, the old table's costs are gone from the model's view.
            connection.exec_driver_sql(
                "INSERT INTO transcript_chunks (id, run_id, core_start_ms, core_end_ms, "
                "upload_start_ms, upload_end_ms, state, attempts, reserved_micro_usd) "
                "VALUES ('c4', 'run-1', 0, 1000, 0, 1000, 'superseded', 1, 5)"
            )

        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS_DIR).replace("%", "%%"))
        with upgraded.engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0005")
            connection.commit()
        assert stored_revision(db_path) == "0005"
        # The superseded parent is dropped, the costs are USD again, the word still names its chunk.
        assert chunk_costs(db_path, _COSTS_USD) == [
            ("c1", "succeeded", 0.0305, 2e-05),
            ("c2", "failed", 1e-06, None),
            ("c3", "cancelled", 0.0, None),
        ]
        with upgraded.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
            assert connection.exec_driver_sql("SELECT chunk_id FROM transcript_words").all() == [
                ("c1",)
            ]

        with upgraded.engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
        assert stored_revision(db_path) == head_revision()
        assert chunk_costs(db_path, _COSTS_MICRO) == [
            ("c1", "succeeded", 30_500, 20),
            ("c2", "failed", 1, None),
            ("c3", "cancelled", 0, None),
        ]


def test_a_failed_0006_rebuild_leaves_the_old_table_and_its_words_untouched(
    tmp_path: Path,
) -> None:
    """The table is dropped and created again inside the upgrade transaction: when a row cannot
    be put back, nothing is kept of the attempt."""
    only_0005 = make_migrations(tmp_path / "only-0005")
    for name in (
        "r0001_initial.py",
        "r0002_previews.py",
        "r0003_transcripts.py",
        "r0004_spend_micro_usd.py",
        "r0005_app_settings.py",
    ):
        shutil.copyfile(MIGRATIONS_DIR / "versions" / name, only_0005 / "versions" / name)
    data_dir = tmp_path / "data"
    db_path = data_dir / DB_FILENAME
    with (
        open_database(data_dir, migrations_dir=only_0005) as old,
        old.write_session() as session,
    ):
        source_id = add_source(session, add_project(session).id).id
    with closing(sqlite3.connect(db_path)) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute(
            "INSERT INTO transcript_runs (id, source_id, model, provider, options_hash, status, "
            "created_at) VALUES "
            "('run-1', ?, 'm', 'p', 'h', 'running', '2026-11-15T10:00:00.000000Z')",
            (source_id,),
        )
        # A state the new table's CHECK refuses: the row cannot be put back, the rebuild fails.
        raw.execute("PRAGMA ignore_check_constraints=ON")
        raw.execute(_CHUNK_INSERT, ("c1", 0, 1000, 0, 1000, "odd", 0, None, None, None, 0.0, None))
        raw.execute("PRAGMA ignore_check_constraints=OFF")
        raw.execute(
            "INSERT INTO transcript_revisions (id, source_id, revision, origin, run_id, parent_id, "
            "timing_level, language, duration_ms, word_count, segment_count, coverage, "
            "metadata_json, created_at) VALUES ('rev-1', ?, 1, 'stt', 'run-1', NULL, 'word', "
            "'en', 60000, 1, 0, '[]', '{}', '2026-11-15T10:00:00.000000Z')",
            (source_id,),
        )
        raw.execute(
            "INSERT INTO transcript_words (revision_id, id, ordinal, start_ms, end_ms, text, "
            "chunk_id) VALUES ('rev-1', 'w1', 0, 0, 500, 'hello', 'c1')"
        )
        raw.commit()

    with pytest.raises(MigrationError):
        open_database(data_dir)

    assert stored_revision(db_path) == "0005"
    assert chunk_costs(db_path, _COSTS_USD) == [("c1", "odd", 0.0, None)]


def test_settings_repository_stores_replaces_and_deletes_values(db: Database) -> None:
    with db.write_session() as session:
        settings = SettingsRepository(session)
        assert settings.get_many(["stt_language", "run_budget_usd"]) == {}
        settings.set("stt_language", "en")
        settings.set("run_budget_usd", "12.50")
        settings.set("stt_language", "uk")  # replaces
        settings.set("other", "x")
    with db.session() as session:
        settings = SettingsRepository(session)
        assert settings.get_many(["stt_language", "run_budget_usd", "missing"]) == {
            "stt_language": "uk",
            "run_budget_usd": "12.50",
        }
    with db.write_session() as session:
        settings = SettingsRepository(session)
        settings.delete("stt_language")
        settings.delete("never_set")  # nothing happens
    with db.session() as session:
        assert SettingsRepository(session).get_many(["stt_language", "run_budget_usd"]) == {
            "run_budget_usd": "12.50"
        }
    with closing(sqlite3.connect(db.data_dir / DB_FILENAME)) as raw:
        stored = raw.execute("SELECT value_json FROM app_settings WHERE key = 'run_budget_usd'")
        assert stored.fetchall() == [('{"schema_version":1,"value":"12.50"}',)]


def test_settings_repository_refuses_null_because_a_reset_is_a_delete(db: Database) -> None:
    with db.write_session() as session, pytest.raises(ValueError, match="null"):
        SettingsRepository(session).set("stt_language", None)


def put_raw_setting(db: Database, key: str, stored: str) -> None:
    with closing(sqlite3.connect(db.data_dir / DB_FILENAME)) as raw:
        raw.execute(
            "INSERT OR REPLACE INTO app_settings (key, value_json) VALUES (?, ?)", (key, stored)
        )
        raw.commit()


def raw_settings(db: Database) -> dict[str, str]:
    with closing(sqlite3.connect(db.data_dir / DB_FILENAME)) as raw:
        return dict(raw.execute("SELECT key, value_json FROM app_settings").fetchall())


def test_settings_repository_skips_a_damaged_row_and_can_replace_or_delete_it(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    put_raw_setting(db, "bad_text", "not json SECRET-TEXT")
    put_raw_setting(db, "bad_version", '{"schema_version":2,"value":"SECRET-TEXT"}')
    put_raw_setting(db, "bad_shape", '"SECRET-TEXT"')
    put_raw_setting(db, "good", '{"schema_version":1,"value":"ok"}')
    keys = ["bad_text", "bad_version", "bad_shape", "good", "missing"]

    with db.session() as session:
        assert SettingsRepository(session).get_many(keys) == {"good": "ok"}  # no exception

    # Only the key names are logged, never the stored text.
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 3
    for key in ("bad_text", "bad_version", "bad_shape"):
        assert sum(key in message for message in messages) == 1
    assert "SECRET-TEXT" not in caplog.text
    with db.write_session() as session:
        settings = SettingsRepository(session)
        settings.set("bad_text", "fixed")  # replaces a row that cannot be read
        settings.delete("bad_version")  # removes one
    with db.session() as session:
        assert SettingsRepository(session).get_many(keys) == {"good": "ok", "bad_text": "fixed"}
    assert raw_settings(db) == {
        "bad_text": '{"schema_version":1,"value":"fixed"}',
        "bad_shape": '"SECRET-TEXT"',  # untouched: nobody asked for it
        "good": '{"schema_version":1,"value":"ok"}',
    }
