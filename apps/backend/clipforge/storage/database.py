"""Opening the workspace database (ADR 017, docs/04 sections 3 and 7, NFR-004).

``open_database`` takes the workspace lock first, so a second sidecar on the same data directory
fails before it touches the database. Then it opens SQLite with ``foreign_keys=ON`` and a busy
timeout, upgrades the schema (with a backup of an existing database) and only then switches the
database to WAL: a database without a schema version or from a newer build is refused untouched.

Transactions: sqlite3's implicit BEGIN is switched off and SQLAlchemy emits BEGIN itself, which
makes DDL transactional and gives every ``Session`` a real transaction.

* ``Database.session()`` - deferred BEGIN, for reads and for transactions whose first statement
  is the write.
* ``Database.write_session()`` - BEGIN IMMEDIATE: writers queue on the busy timeout instead of
  failing with SQLITE_BUSY_SNAPSHOT when they read before they write.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Final

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.engine import URL
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from .errors import StorageError
from .migrate import MIGRATIONS_DIR, upgrade_to_head
from .workspace import WorkspaceLock

DB_FILENAME: Final = "clipforge.sqlite3"
LOCK_FILENAME: Final = "workspace.lock"
BACKUP_DIRNAME: Final = "backups"
BUSY_TIMEOUT_MS: Final = 5000

_BEGIN_OPTION: Final = "clipforge_begin"
_BEGIN_STATEMENTS: Final = {"DEFERRED": "BEGIN", "IMMEDIATE": "BEGIN IMMEDIATE"}


class Database:
    """An open workspace database. Call ``close`` (or use ``with``) to release the lock."""

    def __init__(self, *, data_dir: Path, engine: Engine, lock: WorkspaceLock) -> None:
        self.data_dir = data_dir
        self.engine = engine
        self._lock = lock
        self._read_factory = sessionmaker(engine, expire_on_commit=False)
        self._write_factory = sessionmaker(
            engine.execution_options(**{_BEGIN_OPTION: "IMMEDIATE"}), expire_on_commit=False
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Commit on normal exit, roll back on any exception."""
        with self._read_factory() as session:
            yield from _transaction(session)

    @contextmanager
    def write_session(self) -> Iterator[Session]:
        """Like ``session`` but takes the write lock when the transaction starts."""
        with self._write_factory() as session:
            yield from _transaction(session)

    def close(self) -> None:
        """Idempotent. Disposes the connection pool, then releases the workspace lock."""
        try:
            self.engine.dispose()
        finally:
            self._lock.release()

    def __enter__(self) -> Database:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def open_database(data_dir: Path, *, migrations_dir: Path = MIGRATIONS_DIR) -> Database:
    """Lock the workspace, open ``<data_dir>/clipforge.sqlite3`` and upgrade it to head.

    Raises ``WorkspaceLocked`` when another process holds the workspace and ``MigrationError``
    when the schema cannot be upgraded. ``migrations_dir`` exists for the migration tests.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = WorkspaceLock(data_dir / LOCK_FILENAME)
    lock.acquire()
    engine: Engine | None = None
    try:
        db_path = data_dir / DB_FILENAME
        engine = _create_engine(db_path)
        upgrade_to_head(
            engine,
            db_path=db_path,
            backups_dir=data_dir / BACKUP_DIRNAME,
            migrations_dir=migrations_dir,
        )
        # Only after `upgrade_to_head` accepted the database: a database without a version or
        # from a newer build is refused above and stays byte-for-byte as it was found.
        _enable_wal(engine)
        _require_wal(engine)
    except BaseException:
        if engine is not None:
            engine.dispose()
        lock.release()
        raise
    return Database(data_dir=data_dir, engine=engine, lock=lock)


def _create_engine(db_path: Path) -> Engine:
    # URL.create keeps the path verbatim (Cyrillic, spaces, '?' and '%' need no escaping).
    engine = create_engine(URL.create("sqlite+pysqlite", database=str(db_path)))
    event.listen(engine, "connect", _on_connect)
    event.listen(engine, "begin", _on_begin)
    return engine


def _on_connect(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        raise StorageError("Очікувалося з'єднання sqlite3")
    # Autocommit for the pragmas below; BEGIN is emitted by `_on_begin`.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        # No journal_mode here: that pragma rewrites the header of a foreign database, so it
        # runs in `_enable_wal` only after the schema version has been accepted.
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


def _on_begin(connection: Connection) -> None:
    mode = connection.get_execution_options().get(_BEGIN_OPTION, "DEFERRED")
    connection.exec_driver_sql(_BEGIN_STATEMENTS[mode])


def _enable_wal(engine: Engine) -> None:
    # The mode lives in the file header, so every later connection is in WAL too. A raw cursor,
    # because the pragma is refused inside the transaction SQLAlchemy would begin.
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()
    finally:
        raw.close()


def _require_wal(engine: Engine) -> None:
    with engine.connect() as connection:
        mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
    if str(mode).lower() != "wal":
        raise StorageError("SQLite не перейшов у режим WAL; тека даних не підтримується")


def _transaction(session: Session) -> Iterator[Session]:
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
