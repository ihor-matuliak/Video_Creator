"""Alembic environment for programmatic upgrades only.

``clipforge.storage.migrate`` passes an open SQLAlchemy connection through
``config.attributes["connection"]``. There is no ini file, no offline mode and no logging setup
(the sidecar's stdout is a protocol channel).

``transactional_ddl=True`` makes the whole upgrade one real transaction even on SQLite, so a
failing revision leaves the previous schema and the previous ``alembic_version`` intact.
"""

from alembic import context
from sqlalchemy.engine import Connection


def run_migrations() -> None:
    connection = context.config.attributes.get("connection")
    if not isinstance(connection, Connection):
        raise RuntimeError("Міграції запускаються лише програмно з відкритим з'єднанням")
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
