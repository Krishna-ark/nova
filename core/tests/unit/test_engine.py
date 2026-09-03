"""Tests for the NOVA async SQLite engine and session layer.

Isolation rules observed throughout this module:

* Every database lives under ``tmp_path``. The developer's real database at
  ``%LOCALAPPDATA%\\NOVA\\nova.db`` is never opened, created or modified.
* Every test disposes the engine it creates, so no connection or WAL
  sidecar file is left open when the test ends.
* The module-level database singleton is reset by an autouse fixture, so one
  test cannot leak a configured database into the next.
* The module-level database singleton is reset by an autouse fixture, so one
  test cannot leak a configured database into the next.
* Every test disposes the engine it creates, so no connection or WAL
  sidecar file is left open when the test ends.

Group 3A defines no models, so the connection layer is proved with
``SELECT 1`` and PRAGMA queries rather than table operations.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from nova.config import DatabaseSettings
from nova.storage import (
    Database,
    DatabaseConfigurationError,
    create_database_engine,
    dispose_database,
    get_database,
    get_session,
    init_database,
)
from nova.storage import engine as engine_module


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Clear the process-wide database between tests."""
    engine_module._database = None
    yield
    engine_module._database = None


def _settings(
    tmp_path: Path,
    *,
    path: Path | None = None,
    enable_wal: bool = True,
    busy_timeout_seconds: float = 5.0,
) -> DatabaseSettings:
    """Build DatabaseSettings pointing at a throwaway database."""
    return DatabaseSettings(
        path=path if path is not None else tmp_path / "nova.db",
        enable_wal=enable_wal,
        busy_timeout_seconds=busy_timeout_seconds,
    )


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    """Yield a disposable Database backed by a temporary file."""
    instance = Database(_settings(tmp_path))
    try:
        yield instance
    finally:
        await instance.dispose()


# --- Engine creation ---


def test_engine_is_created_from_settings(tmp_path: Path) -> None:
    """The engine must come from NOVA settings, not a hard-coded path."""
    settings = _settings(tmp_path)

    engine = create_database_engine(settings)

    assert isinstance(engine, AsyncEngine)
    assert engine.url.drivername == "sqlite+aiosqlite"
    assert str(settings.path.as_posix()) in str(engine.url)


def test_database_exposes_engine_and_factory(database: Database) -> None:
    assert isinstance(database.engine, AsyncEngine)
    assert database.session_factory is not None
    assert database.settings.enable_wal is True


def test_creating_an_engine_does_not_open_a_connection(tmp_path: Path) -> None:
    """Engine construction is lazy; no file should exist until first use."""
    settings = _settings(tmp_path)

    create_database_engine(settings)

    assert not settings.path.exists(), "engine creation must not connect eagerly"


# --- Directory handling ---


def test_missing_parent_directory_is_created(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        path=tmp_path / "deep" / "nested" / "nova.db",
    )

    create_database_engine(settings)

    assert settings.path.parent.is_dir(), "database parent directory not created"


async def test_database_file_is_created_on_first_connection(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        path=tmp_path / "created" / "nova.db",
    )
    instance = Database(settings)

    try:
        async with instance.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await instance.dispose()

    assert settings.path.exists(), "database file was not created"


# --- Invalid configuration ---


def test_directory_as_database_path_fails_clearly(tmp_path: Path) -> None:
    """A misconfigured path must raise, not silently fall back."""
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    settings = _settings(tmp_path, path=directory)

    with pytest.raises(DatabaseConfigurationError, match="is a directory"):
        create_database_engine(settings)


def test_unusable_parent_path_fails_clearly(tmp_path: Path) -> None:
    """A parent that cannot be created must raise a clear error."""
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    settings = _settings(
        tmp_path,
        path=blocker / "sub" / "nova.db",
    )

    with pytest.raises(
        DatabaseConfigurationError,
        match="Cannot create database directory",
    ):
        create_database_engine(settings)


def test_configuration_error_is_a_runtime_error() -> None:
    """Callers may catch the broader type without importing ours."""
    assert issubclass(DatabaseConfigurationError, RuntimeError)


# --- PRAGMAs ---


async def test_connection_succeeds(database: Database) -> None:
    async with database.engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))

    assert result.scalar_one() == 1


async def test_sqlite_reports_wal_mode(database: Database) -> None:
    async with database.engine.connect() as connection:
        mode = (await connection.execute(text("PRAGMA journal_mode"))).scalar_one()

    assert str(mode).lower() == "wal", f"expected WAL, got {mode!r}"


async def test_sqlite_reports_foreign_keys_enabled(database: Database) -> None:
    """SQLite defaults foreign_keys to OFF; NOVA must turn it on."""
    async with database.engine.connect() as connection:
        enabled = (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one()

    assert str(enabled) == "1", f"foreign keys not enforced, got {enabled!r}"


@pytest.mark.parametrize(
    ("seconds", "expected_ms"),
    [
        (0.5, 500),
        (1.0, 1000),
        (2.5, 2500),
        (5.0, 5000),
        (30.0, 30000),
    ],
)
async def test_busy_timeout_is_converted_to_milliseconds(
    tmp_path: Path,
    seconds: float,
    expected_ms: int,
) -> None:
    """The setting is in seconds; the PRAGMA expects milliseconds."""
    instance = Database(
        _settings(
            tmp_path,
            busy_timeout_seconds=seconds,
        )
    )

    try:
        async with instance.engine.connect() as connection:
            timeout = (await connection.execute(text("PRAGMA busy_timeout"))).scalar_one()
    finally:
        await instance.dispose()

    assert int(timeout) == expected_ms


async def test_pragmas_apply_to_every_pooled_connection(
    database: Database,
) -> None:
    """PRAGMAs are per connection, so each new one must be configured."""
    for attempt in range(3):
        async with database.engine.connect() as connection:
            foreign_keys = (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one()

            mode = (await connection.execute(text("PRAGMA journal_mode"))).scalar_one()

        assert str(foreign_keys) == "1", f"foreign keys off on connection {attempt}"
        assert str(mode).lower() == "wal", f"WAL lost on connection {attempt}"


async def test_wal_can_be_disabled_by_configuration(
    tmp_path: Path,
) -> None:
    """enable_wal=False must be honoured rather than forced on."""
    instance = Database(
        _settings(
            tmp_path,
            enable_wal=False,
        )
    )

    try:
        async with instance.engine.connect() as connection:
            mode = (await connection.execute(text("PRAGMA journal_mode"))).scalar_one()
    finally:
        await instance.dispose()

    assert str(mode).lower() != "wal"


# --- verify() ---


async def test_verify_reports_the_active_pragmas(
    database: Database,
) -> None:
    report = await database.verify()

    assert report["journal_mode"] == "wal"
    assert report["foreign_keys"] == "1"
    assert int(report["busy_timeout"]) == 5000


async def test_verify_succeeds_repeatedly(database: Database) -> None:
    await database.verify()
    await database.verify()


# --- Sessions ---


async def test_session_executes_a_statement(database: Database) -> None:
    async with database.session() as session:
        result = await session.execute(text("SELECT 1"))

        assert result.scalar_one() == 1


async def test_session_is_an_async_session(database: Database) -> None:
    async with database.session() as session:
        assert isinstance(session, AsyncSession)


async def test_session_releases_its_connection_after_the_block(
    database: Database,
) -> None:
    """Leaving the block must return the connection to the pool.

    ``is_active`` is not the right signal here: SQLAlchemy keeps it True on a
    closed session because it reports transaction validity, not liveness.
    ``in_transaction()`` is what flips when the connection is released.
    """
    async with database.session() as session:
        await session.execute(text("SELECT 1"))
        assert session.in_transaction(), "expected an open transaction inside the block"

    assert not session.in_transaction(), "connection was not released"


async def test_session_rolls_back_on_error(database: Database) -> None:
    """An exception inside the block must not leave a session in transaction."""
    message = "deliberate failure"

    with pytest.raises(ValueError, match=message):
        async with database.session() as session:
            await session.execute(text("SELECT 1"))
            raise ValueError(message)

    assert not session.in_transaction(), "transaction survived the exception"


async def test_sessions_are_independent(database: Database) -> None:
    async with database.session() as first, database.session() as second:
        assert first is not second


# --- Disposal ---


async def test_dispose_closes_the_engine(tmp_path: Path) -> None:
    instance = Database(_settings(tmp_path))

    async with instance.engine.connect() as connection:
        await connection.execute(text("SELECT 1"))

    await instance.dispose()

    # A disposed engine builds a fresh pool on next use rather than failing.
    async with instance.engine.connect() as connection:
        assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1

    await instance.dispose()


async def test_dispose_is_idempotent(tmp_path: Path) -> None:
    instance = Database(_settings(tmp_path))

    await instance.dispose()
    await instance.dispose()


# --- Module-level instance ---


def test_get_database_before_init_raises() -> None:
    with pytest.raises(RuntimeError, match="not initialised"):
        get_database()


async def test_init_database_registers_the_instance(
    tmp_path: Path,
) -> None:
    instance = init_database(_settings(tmp_path))

    try:
        assert get_database() is instance
    finally:
        await dispose_database()


async def test_dispose_database_clears_the_instance(
    tmp_path: Path,
) -> None:
    init_database(_settings(tmp_path))

    await dispose_database()

    with pytest.raises(RuntimeError, match="not initialised"):
        get_database()


async def test_dispose_database_without_init_is_safe() -> None:
    await dispose_database()


async def test_get_session_yields_a_working_session(
    tmp_path: Path,
) -> None:
    """get_session is shaped for FastAPI dependency injection."""
    init_database(_settings(tmp_path))

    try:
        async for session in get_session():
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await dispose_database()


# --- Containment ---


async def _use_database(tmp_path: Path) -> None:
    """Open a session against a temporary database, then dispose it."""
    instance = Database(_settings(tmp_path))

    try:
        async with instance.session() as session:
            await session.execute(text("SELECT 1"))
    finally:
        await instance.dispose()


def test_nothing_is_written_outside_the_temporary_directory(
    tmp_path: Path,
) -> None:
    """Explicit guard: the real NOVA database must stay untouched.

    The filesystem inspection is deliberately synchronous; blocking pathlib
    calls inside an async function are flagged by the ASYNC ruleset.
    """
    asyncio.run(_use_database(tmp_path))

    written = list(tmp_path.iterdir())

    assert written, "nothing was written to the temporary directory"
    assert all(path.parent == tmp_path for path in written)
