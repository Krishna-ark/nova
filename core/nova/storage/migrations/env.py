"""Alembic environment for NOVA.

Two things this file is careful about:

* **Metadata has one source.** ``target_metadata`` is
  ``nova.storage.models.Base.metadata``. A second, handwritten schema
  definition would drift from the ORM within a release.
* **The database URL is never hard-coded.** It is resolved in a fixed order
  so tests always run against a temporary database and can never reach the
  developer's real ``nova.db``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import TYPE_CHECKING

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from nova.storage.models import Base

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    """Return the database URL for this migration run."""
    injected = config.attributes.get("sqlalchemy.url")
    if injected:
        return str(injected)

    from_command_line = context.get_x_argument(as_dictionary=True).get("db_url")
    if from_command_line:
        return str(from_command_line)

    configured = config.get_main_option("sqlalchemy.url", default=None)
    if configured:
        return configured

    from nova.config import get_settings

    try:
        return get_settings().database.url
    except Exception as exc:
        message = (
            "No database URL available for migrations. Provide one with "
            "'alembic -x db_url=sqlite+aiosqlite:///path/to.db ...', or ensure "
            f"NOVA configuration loads correctly. Underlying error: {exc}"
        )
        raise RuntimeError(message) from exc


def _configure(connection: Connection) -> None:
    """Configure Alembic for an online migration."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
    )


def run_migrations_offline() -> None:
    """Emit SQL without connecting to a database."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    """Run migrations on an established synchronous connection."""
    _configure(connection)

    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    """Create an async engine and run migrations through it."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _resolve_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    connection = config.attributes.get("connection")

    if connection is not None:
        _run_migrations(connection)
        return

    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()