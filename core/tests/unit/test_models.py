"""Tests for the NOVA SQLAlchemy models.

Isolation rules observed throughout this module:

* Every schema is created against a database under ``tmp_path``. The real
  database at ``%LOCALAPPDATA%\\NOVA\\nova.db`` is never opened or created.
* Each test gets a fresh engine and disposes it, so no connection or WAL
  sidecar survives the test.
* Group 3B adds no migrations, so the schema is built with
  ``Base.metadata.create_all`` through ``run_sync`` on the async engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from nova.config import DatabaseSettings
from nova.storage import Database
from nova.storage.models import (
    AuditDecision,
    AuditEvent,
    AuditResult,
    Base,
    Device,
    DevicePlatform,
    DeviceStatus,
    Preference,
    new_device_id,
    utcnow,
)

EXPECTED_TABLES = {"preferences", "devices", "audit_events"}


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    """Yield a Database with the full schema created, then dispose it."""
    instance = Database(DatabaseSettings(path=tmp_path / "nova.db"))
    async with instance.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield instance
    finally:
        await instance.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    """Yield a session against the prepared schema."""
    async with database.session() as active_session:
        yield active_session


def _device(**overrides: object) -> Device:
    """Build a Device with sensible defaults for tests."""
    values: dict[str, object] = {
        "name": "test-laptop",
        "platform": DevicePlatform.WINDOWS,
    }
    values.update(overrides)
    return Device(**values)


def _audit(**overrides: object) -> AuditEvent:
    """Build an AuditEvent with sensible defaults for tests."""
    values: dict[str, object] = {
        "actor": "user",
        "action": "tool.invoke",
        "decision": AuditDecision.ALLOW,
        "hash": "a" * 64,
    }
    values.update(overrides)
    return AuditEvent(**values)


# --- Metadata ---


def test_metadata_imports_and_registers_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_no_unexpected_tables_are_defined() -> None:
    """Guards against a stray model being added without review."""
    unexpected = set(Base.metadata.tables) - EXPECTED_TABLES

    assert unexpected == set(), f"unexpected tables defined: {unexpected}"


def test_table_names_are_correct() -> None:
    assert Preference.__tablename__ == "preferences"
    assert Device.__tablename__ == "devices"
    assert AuditEvent.__tablename__ == "audit_events"


def test_preference_table_is_not_named_settings() -> None:
    """`Settings` is the Pydantic config model; the table must not collide."""
    assert "settings" not in Base.metadata.tables


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("preferences", {"key", "value", "description", "created_at", "updated_at"}),
        (
            "devices",
            {
                "id",
                "name",
                "platform",
                "public_key",
                "status",
                "os_version",
                "agent_version",
                "last_seen_at",
                "authorized_at",
                "revoked_at",
                "created_at",
                "updated_at",
            },
        ),
        (
            "audit_events",
            {
                "id",
                "occurred_at",
                "actor",
                "action",
                "tool_name",
                "device_id",
                "decision",
                "result",
                "args_hash",
                "detail",
                "prev_hash",
                "hash",
            },
        ),
    ],
)
def test_required_columns_exist(table: str, columns: set[str]) -> None:
    actual = {column.name for column in Base.metadata.tables[table].columns}

    assert actual == columns, f"{table} columns differ: {actual ^ columns}"


def test_audit_hash_chain_fields_exist() -> None:
    """prev_hash and hash must exist now so no migration is needed later."""
    audit_columns = Base.metadata.tables["audit_events"].columns

    assert "prev_hash" in audit_columns
    assert "hash" in audit_columns
    assert audit_columns["prev_hash"].nullable is True, "first row has no predecessor"
    assert audit_columns["hash"].nullable is False


def test_audit_events_have_no_updated_at() -> None:
    """Audit rows are append-only; an update column would invite mutation."""
    assert "updated_at" not in Base.metadata.tables["audit_events"].columns


# --- Constraints declared in metadata ---


def test_primary_keys_are_declared() -> None:
    tables = Base.metadata.tables

    assert [c.name for c in tables["preferences"].primary_key] == ["key"]
    assert [c.name for c in tables["devices"].primary_key] == ["id"]
    assert [c.name for c in tables["audit_events"].primary_key] == ["id"]


@pytest.mark.parametrize(
    ("table", "column", "nullable"),
    [
        ("preferences", "value", False),
        ("preferences", "description", True),
        ("devices", "name", False),
        ("devices", "platform", False),
        ("devices", "public_key", True),
        ("devices", "last_seen_at", True),
        ("audit_events", "actor", False),
        ("audit_events", "action", False),
        ("audit_events", "decision", False),
        ("audit_events", "result", True),
        ("audit_events", "device_id", True),
    ],
)
def test_nullability_is_correct(table: str, column: str, nullable: bool) -> None:
    assert Base.metadata.tables[table].columns[column].nullable is nullable


def test_unique_constraints_are_declared() -> None:
    devices = Base.metadata.tables["devices"].columns
    audit = Base.metadata.tables["audit_events"].columns

    assert devices["name"].unique is True, "device names must not collide"
    assert devices["public_key"].unique is True, "a key must identify one device"
    assert audit["hash"].unique is True, "each audit hash must be unique"


def test_foreign_key_is_declared_with_restrict() -> None:
    """Deleting a device must not silently erase its audit history."""
    foreign_keys = list(Base.metadata.tables["audit_events"].c.device_id.foreign_keys)

    assert len(foreign_keys) == 1
    assert foreign_keys[0].column.table.name == "devices"
    assert foreign_keys[0].ondelete == "RESTRICT"


def test_constraint_naming_convention_is_applied() -> None:
    """Anonymous SQLite constraints cannot be altered by Alembic later."""
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"
    assert Base.metadata.tables["devices"].primary_key.name == "pk_devices"


# --- Schema creation ---


async def test_schema_can_be_created(database: Database) -> None:
    async with database.engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))

    assert tables >= EXPECTED_TABLES


async def test_no_unexpected_tables_are_created(database: Database) -> None:
    async with database.engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))

    extra = {name for name in tables if not name.startswith("sqlite_")} - EXPECTED_TABLES
    assert extra == set(), f"unexpected tables created: {extra}"


async def test_create_all_is_idempotent(database: Database) -> None:
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


# --- Preferences ---


async def test_preference_insert_and_select(session: AsyncSession) -> None:
    session.add(Preference(key="response_language", value='"auto"'))
    await session.commit()

    stored = await session.get(Preference, "response_language")

    assert stored is not None
    assert stored.value == '"auto"'


async def test_preference_key_is_unique(session: AsyncSession) -> None:
    session.add(Preference(key="duplicate", value="1"))
    await session.commit()

    session.add(Preference(key="duplicate", value="2"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_preference_value_is_required(session: AsyncSession) -> None:
    session.add(Preference(key="no_value"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_preference_description_is_optional(session: AsyncSession) -> None:
    session.add(Preference(key="terse", value="1"))
    await session.commit()

    stored = await session.get(Preference, "terse")

    assert stored is not None
    assert stored.description is None


# --- Timestamps ---


async def test_timestamps_default_on_insert(session: AsyncSession) -> None:
    before = utcnow() - timedelta(seconds=1)
    session.add(Preference(key="timed", value="1"))
    await session.commit()

    stored = await session.get(Preference, "timed")

    assert stored is not None
    assert stored.created_at >= before
    assert stored.updated_at >= before


async def test_timestamps_round_trip_as_utc_aware(session: AsyncSession) -> None:
    """SQLite loses tzinfo unless the custom type reattaches it."""
    session.add(Preference(key="tz", value="1"))
    await session.commit()
    session.expunge_all()

    stored = await session.get(Preference, "tz")

    assert stored is not None
    assert stored.created_at.tzinfo is not None, "timestamp came back naive"
    assert stored.created_at.utcoffset() == timedelta(0), "timestamp is not UTC"


async def test_naive_datetime_is_rejected(session: AsyncSession) -> None:
    """Guessing a timezone for an audit timestamp would be a silent bug.

    SQLAlchemy wraps the underlying ValueError in a StatementError, so the
    wrapper is what a caller actually sees.
    """
    session.add(_device(last_seen_at=datetime(2026, 1, 1, 12, 0, 0)))

    with pytest.raises(StatementError, match="Naive datetime"):
        await session.commit()


async def test_updated_at_changes_on_update(session: AsyncSession) -> None:
    session.add(Preference(key="mutable", value="first"))
    await session.commit()
    stored = await session.get(Preference, "mutable")
    assert stored is not None
    original = stored.updated_at

    stored.value = "second"
    await session.commit()

    assert stored.updated_at >= original


# --- Devices ---


async def test_device_insert_and_select(session: AsyncSession) -> None:
    device = _device(name="laptop", platform=DevicePlatform.WINDOWS)
    session.add(device)
    await session.commit()

    found = (await session.execute(select(Device).where(Device.name == "laptop"))).scalar_one()

    assert found.platform is DevicePlatform.WINDOWS
    assert found.status is DeviceStatus.PENDING, "a new device must not be authorised"


async def test_device_id_defaults_to_a_uuid(session: AsyncSession) -> None:
    device = _device()
    session.add(device)
    await session.commit()

    assert len(device.id) == 36
    assert device.id.count("-") == 4


def test_new_device_id_is_unique() -> None:
    assert new_device_id() != new_device_id()


async def test_device_name_is_unique(session: AsyncSession) -> None:
    session.add(_device(name="same"))
    await session.commit()

    session.add(_device(name="same"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_device_public_key_is_unique(session: AsyncSession) -> None:
    session.add(_device(name="first", public_key="shared-key"))
    await session.commit()

    session.add(_device(name="second", public_key="shared-key"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_device_public_key_may_be_absent(session: AsyncSession) -> None:
    """A pending device has not presented a key yet."""
    session.add(_device(name="unpaired"))
    await session.commit()

    found = (await session.execute(select(Device).where(Device.name == "unpaired"))).scalar_one()

    assert found.public_key is None


@pytest.mark.parametrize("platform", list(DevicePlatform))
async def test_every_platform_can_be_stored(
    session: AsyncSession, platform: DevicePlatform
) -> None:
    session.add(_device(name=f"device-{platform.value}", platform=platform))
    await session.commit()

    found = (
        await session.execute(select(Device).where(Device.name == f"device-{platform.value}"))
    ).scalar_one()

    assert found.platform is platform


@pytest.mark.parametrize(
    ("status", "revoked_at", "expected"),
    [
        (DeviceStatus.PENDING, None, False),
        (DeviceStatus.AUTHORIZED, None, True),
        (DeviceStatus.REVOKED, None, False),
    ],
)
def test_is_authorized_property(
    status: DeviceStatus, revoked_at: datetime | None, expected: bool
) -> None:
    device = _device(status=status, revoked_at=revoked_at)

    assert device.is_authorized is expected


def test_revoked_device_is_never_authorized() -> None:
    """A revocation timestamp overrides a stale status value."""
    device = _device(status=DeviceStatus.AUTHORIZED, revoked_at=utcnow())

    assert device.is_authorized is False


# --- Audit events ---


async def test_audit_event_insert_and_select(session: AsyncSession) -> None:
    session.add(_audit(action="tool.invoke", hash="b" * 64))
    await session.commit()

    found = (await session.execute(select(AuditEvent))).scalar_one()

    assert found.action == "tool.invoke"
    assert found.decision is AuditDecision.ALLOW
    assert found.id >= 1, "primary key must autoincrement"


async def test_audit_ids_increase_monotonically(session: AsyncSession) -> None:
    """The hash chain depends on a strict order."""
    for index in range(3):
        session.add(_audit(hash=f"{index:064d}"))
    await session.commit()

    ids = list((await session.execute(select(AuditEvent.id).order_by(AuditEvent.id))).scalars())

    assert ids == sorted(ids)
    assert len(set(ids)) == 3


async def test_audit_hash_is_unique(session: AsyncSession) -> None:
    session.add(_audit(hash="c" * 64))
    await session.commit()

    session.add(_audit(hash="c" * 64))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_audit_hash_is_required(session: AsyncSession) -> None:
    session.add(AuditEvent(actor="user", action="x", decision=AuditDecision.ALLOW))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_first_audit_event_may_have_no_prev_hash(session: AsyncSession) -> None:
    session.add(_audit(hash="d" * 64, prev_hash=None))
    await session.commit()

    found = (await session.execute(select(AuditEvent))).scalar_one()

    assert found.prev_hash is None


@pytest.mark.parametrize("result", list(AuditResult))
async def test_every_result_state_can_be_stored(session: AsyncSession, result: AuditResult) -> None:
    session.add(_audit(hash=f"{result.value:>064}", result=result))
    await session.commit()

    found = (await session.execute(select(AuditEvent))).scalar_one()

    assert found.result is result


# --- Relationships and foreign keys ---


async def test_audit_event_links_to_device(session: AsyncSession) -> None:
    device = _device(name="linked")
    session.add(device)
    await session.flush()
    session.add(_audit(device_id=device.id, hash="e" * 64))
    await session.commit()

    found = (await session.execute(select(AuditEvent))).scalar_one()

    assert found.device_id == device.id


async def test_foreign_key_enforcement_rejects_unknown_device(
    session: AsyncSession,
) -> None:
    """Proves PRAGMA foreign_keys=ON is actually in force."""
    session.add(_audit(device_id="00000000-0000-0000-0000-000000000000", hash="f" * 64))

    with pytest.raises(IntegrityError):
        await session.commit()


async def test_audit_event_without_device_is_allowed(session: AsyncSession) -> None:
    """Not every event involves a device."""
    session.add(_audit(device_id=None, hash="0" * 64))
    await session.commit()

    found = (await session.execute(select(AuditEvent))).scalar_one()

    assert found.device is None


async def test_deleting_a_device_with_audit_history_is_refused(
    session: AsyncSession,
) -> None:
    """RESTRICT must protect the audit trail."""
    device = _device(name="protected")
    session.add(device)
    await session.flush()
    session.add(_audit(device_id=device.id, hash="1" * 64))
    await session.commit()

    await session.delete(device)
    with pytest.raises(IntegrityError):
        await session.commit()


# --- Containment ---


async def test_database_file_stays_in_the_temporary_directory(
    database: Database, tmp_path: Path
) -> None:
    assert database.settings.path.parent == tmp_path
    assert database.settings.path.exists()
