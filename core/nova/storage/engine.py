"""Async SQLite engine and session management for NOVA.

SQLite connection PRAGMAs are configured for every DB-API connection.
No database connection is opened during module import.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nova.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nova.config.settings import DatabaseSettings

logger = get_logger(__name__)

_MILLISECONDS_PER_SECOND = 1000


class DatabaseConfigurationError(RuntimeError):
    """Raised when the database cannot be configured as requested."""


def _register_pragmas(engine: AsyncEngine, settings: DatabaseSettings) -> None:
    """Apply required SQLite PRAGMAs to every new connection."""

    busy_timeout_ms = int(settings.busy_timeout_seconds * _MILLISECONDS_PER_SECOND)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(
        dbapi_connection: Any,
        _record: Any,
    ) -> None:
        cursor = dbapi_connection.cursor()

        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")

            if settings.enable_wal:
                cursor.execute("PRAGMA journal_mode=WAL")
                row = cursor.fetchone()
                mode = str(row[0]).lower() if row else "unknown"

                if mode != "wal":
                    message = (
                        f"SQLite refused WAL journal mode for {settings.path} "
                        f"(reported {mode!r}). WAL is required by NOVA."
                    )
                    raise DatabaseConfigurationError(message)
        finally:
            cursor.close()


def create_database_engine(settings: DatabaseSettings) -> AsyncEngine:
    """Create an async SQLite engine from NOVA database settings."""

    path = settings.path

    if path.exists() and path.is_dir():
        message = (
            f"Configured database path {path} is a directory, not a file. "
            "Set NOVA_DATABASE__PATH to a file path."
        )
        raise DatabaseConfigurationError(message)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        message = f"Cannot create database directory {path.parent}: {exc}"
        raise DatabaseConfigurationError(message) from exc

    engine = create_async_engine(
        settings.url,
        echo=False,
    )

    _register_pragmas(engine, settings)

    return engine


class Database:
    """Own an async engine and its session factory."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings
        self._engine = create_database_engine(settings)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        """Return the underlying async engine."""
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Return the session factory."""
        return self._session_factory

    @property
    def settings(self) -> DatabaseSettings:
        """Return the settings used to create this database."""
        return self._settings

    async def verify(self) -> dict[str, str]:
        """Verify required SQLite PRAGMAs."""

        async with self._engine.connect() as connection:
            journal_mode = str(
                (await connection.execute(text("PRAGMA journal_mode"))).scalar_one()
            ).lower()

            foreign_keys = str((await connection.execute(text("PRAGMA foreign_keys"))).scalar_one())

            busy_timeout = str((await connection.execute(text("PRAGMA busy_timeout"))).scalar_one())

        if self._settings.enable_wal and journal_mode != "wal":
            raise DatabaseConfigurationError(
                f"Expected WAL journal mode, database reports {journal_mode!r}."
            )

        if foreign_keys != "1":
            raise DatabaseConfigurationError(
                f"Expected foreign_keys=1, database reports {foreign_keys!r}."
            )

        logger.info(
            "database_verified",
            journal_mode=journal_mode,
            foreign_keys=foreign_keys,
            busy_timeout_ms=busy_timeout,
        )

        return {
            "journal_mode": journal_mode,
            "foreign_keys": foreign_keys,
            "busy_timeout": busy_timeout,
        }

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield an async session and roll back when the block fails."""

        async with self._session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        """Dispose all pooled database connections."""

        await self._engine.dispose()

        logger.info(
            "database_disposed",
            path=str(self._settings.path),
        )


_database: Database | None = None


def init_database(settings: DatabaseSettings) -> Database:
    """Create and register the process-wide database instance."""

    global _database

    _database = Database(settings)

    return _database


def get_database() -> Database:
    """Return the process-wide database instance."""

    if _database is None:
        raise RuntimeError("Database is not initialised. Call init_database(settings) first.")

    return _database


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a session suitable for future FastAPI dependency injection."""

    async with get_database().session() as session:
        yield session


async def dispose_database() -> None:
    """Dispose and clear the process-wide database instance."""

    global _database

    if _database is not None:
        await _database.dispose()
        _database = None
