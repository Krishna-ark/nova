"""Tests for the NOVA storage repositories.

Isolation rules observed throughout this module:

* Every test runs against a database created under ``tmp_path``. The real
  database at ``%LOCALAPPDATA%\\NOVA\\nova.db`` is never opened or created.
* Each test disposes its engine, so no connection or WAL sidecar survives.
* The caller owns the transaction, so tests commit explicitly. That is the
  behaviour under test, not an incidental detail.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
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
    utcnow,
)
from nova.storage.repositories import (
    AuditEventRepository,
    DeviceRepository,
    PreferenceRepository,
)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    """Yield a Database with the schema created, then dispose it."""
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


@pytest.fixture
def preferences(session: AsyncSession) -> PreferenceRepository:
    return PreferenceRepository(session)


@pytest.fixture
def devices(session: AsyncSession) -> DeviceRepository:
    return DeviceRepository(session)


@pytest.fixture
def audit(session: AsyncSession) -> AuditEventRepository:
    return AuditEventRepository(session)


def _device(
    name: str = "test-laptop",
    *,
    platform: DevicePlatform = DevicePlatform.WINDOWS,
    status: DeviceStatus = DeviceStatus.PENDING,
    public_key: str | None = None,
) -> Device:
    """Build an unsaved Device for tests."""
    return Device(
        name=name,
        platform=platform,
        status=status,
        public_key=public_key,
    )


def _audit_event(
    *,
    hash_value: str,
    actor: str = "user",
    action: str = "tool.invoke",
    device_id: str | None = None,
    result: AuditResult | None = None,
) -> AuditEvent:
    """Build an unsaved AuditEvent for tests."""
    return AuditEvent(
        actor=actor,
        action=action,
        decision=AuditDecision.ALLOW,
        device_id=device_id,
        result=result,
        hash=hash_value,
    )


# --- Preferences ---


async def test_preference_set_creates_a_row(
    preferences: PreferenceRepository,
    session: AsyncSession,
) -> None:
    await preferences.set("response_language", '"auto"')
    await session.commit()

    stored = await preferences.get("response_language")

    assert stored is not None
    assert stored.value == '"auto"'


async def test_preference_get_returns_none_for_missing_key(
    preferences: PreferenceRepository,
) -> None:
    assert await preferences.get("does-not-exist") is None


async def test_preference_set_updates_an_existing_row(
    preferences: PreferenceRepository,
    session: AsyncSession,
) -> None:
    await preferences.set("autonomy_level", "1")
    await session.commit()

    await preferences.set("autonomy_level", "2")
    await session.commit()

    stored = await preferences.get("autonomy_level")

    assert stored is not None
    assert stored.value == "2"


async def test_preference_update_preserves_created_at(
    preferences: PreferenceRepository,
    session: AsyncSession,
) -> None:
    """An update must not look like a fresh insert."""
    created = await preferences.set("tracked", "first")
    await session.commit()
    original_created_at = created.created_at

    await preferences.set("tracked", "second")
    await session.commit()

    stored = await preferences.get("tracked")

    assert stored is not None
    assert stored.created_at == original_created_at
    assert stored.updated_at >= original_created_at


async def test_preference_set_does_not_create_duplicates(
    preferences: PreferenceRepository,
    session: AsyncSession,
) -> None:
    await preferences.set("single", "a")
    await preferences.set("single", "b")
    await session.commit()

    assert len(await preferences.list_all()) == 1


async def test_preference_set_keeps_description_when_omitted(
    preferences: PreferenceRepository,
    session: AsyncSession,
) -> None:
    """Changing a value must not erase the explanation of the key."""
    await preferences.set(
        "documented",
        "1",
        description="What this controls",
    )
    await session.commit()

    await preferences.set("documented", "2")
    await session.commit()

    stored = await preferences.get("documented")

    assert stored is not None
    assert stored.description == "What this controls"


async def test_preference_description_can_be_replaced(
    preferences: PreferenceRepository,
    session: AsyncSession,
) -> None:
    await preferences.set("described", "1", description="old")
    await session.commit()

    await preferences.set("described", "1", description="new")
    await session.commit()

    stored = await preferences.get("described")

    assert stored is not None
    assert stored.description == "new"


async def test_preference_list_all_is_ordered_by_key(
    preferences: PreferenceRepository,
    session: AsyncSession,
) -> None:
    for key in ("zulu", "alpha", "mike"):
        await preferences.set(key, "1")
    await session.commit()

    keys = [preference.key for preference in await preferences.list_all()]

    assert keys == ["alpha", "mike", "zulu"]


async def test_preference_list_all_is_empty_initially(
    preferences: PreferenceRepository,
) -> None:
    assert await preferences.list_all() == []


async def test_preference_exists(
    preferences: PreferenceRepository,
    session: AsyncSession,
) -> None:
    await preferences.set("present", "1")
    await session.commit()

    assert await preferences.exists("present") is True
    assert await preferences.exists("absent") is False


async def test_preference_delete_removes_the_row(
    preferences: PreferenceRepository,
    session: AsyncSession,
) -> None:
    await preferences.set("temporary", "1")
    await session.commit()

    deleted = await preferences.delete("temporary")
    await session.commit()

    assert deleted is True
    assert await preferences.get("temporary") is None


async def test_preference_delete_missing_key_reports_false(
    preferences: PreferenceRepository,
) -> None:
    assert await preferences.delete("never-existed") is False


# --- Devices ---


async def test_device_add_and_get(
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    device = await devices.add(_device("laptop"))
    await session.commit()

    stored = await devices.get(device.id)

    assert stored is not None
    assert stored.name == "laptop"
    assert stored.platform is DevicePlatform.WINDOWS


async def test_device_add_assigns_an_id_before_commit(
    devices: DeviceRepository,
) -> None:
    """add() flushes, so the generated id is available to the caller."""
    device = await devices.add(_device("flushed"))

    assert device.id
    assert len(device.id) == 36


async def test_device_get_returns_none_for_unknown_id(
    devices: DeviceRepository,
) -> None:
    assert await devices.get("00000000-0000-0000-0000-000000000000") is None


async def test_device_get_by_name(
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    await devices.add(_device("named-device"))
    await session.commit()

    found = await devices.get_by_name("named-device")

    assert found is not None
    assert found.name == "named-device"


async def test_device_get_by_name_returns_none_when_absent(
    devices: DeviceRepository,
) -> None:
    assert await devices.get_by_name("no-such-device") is None


async def test_device_get_by_public_key(
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    await devices.add(
        _device(
            "keyed",
            public_key="ed25519-public-key",
        )
    )
    await session.commit()

    found = await devices.get_by_public_key("ed25519-public-key")

    assert found is not None
    assert found.name == "keyed"


async def test_device_get_by_public_key_returns_none_when_absent(
    devices: DeviceRepository,
) -> None:
    assert await devices.get_by_public_key("unknown-key") is None


async def test_device_list_all_is_ordered_by_name(
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    for name in ("tablet", "laptop", "phone"):
        await devices.add(_device(name))
    await session.commit()

    names = [device.name for device in await devices.list_all()]

    assert names == ["laptop", "phone", "tablet"]


async def test_device_list_all_is_empty_initially(
    devices: DeviceRepository,
) -> None:
    assert await devices.list_all() == []


async def test_device_list_authorized_filters_by_status(
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    await devices.add(
        _device(
            "pending-device",
            status=DeviceStatus.PENDING,
        )
    )
    await devices.add(
        _device(
            "authorized-device",
            status=DeviceStatus.AUTHORIZED,
        )
    )
    await devices.add(
        _device(
            "revoked-device",
            status=DeviceStatus.REVOKED,
        )
    )
    await session.commit()

    names = [device.name for device in await devices.list_authorized()]

    assert names == ["authorized-device"]


async def test_device_list_authorized_excludes_revoked_timestamp(
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    """A revocation timestamp must win over a stale status value."""
    device = _device(
        "inconsistent",
        status=DeviceStatus.AUTHORIZED,
    )
    device.revoked_at = utcnow()

    await devices.add(device)
    await session.commit()

    assert await devices.list_authorized() == []


async def test_device_update_persists_changes(
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    device = await devices.add(_device("upgradable"))
    await session.commit()

    device.status = DeviceStatus.AUTHORIZED
    device.agent_version = "1.2.3"

    await devices.update(device)
    await session.commit()

    stored = await devices.get(device.id)

    assert stored is not None
    assert stored.status is DeviceStatus.AUTHORIZED
    assert stored.agent_version == "1.2.3"


async def test_device_update_advances_updated_at(
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    device = await devices.add(_device("touched"))
    await session.commit()
    original = device.updated_at

    device.os_version = "Windows 11 25H2"

    await devices.update(device)
    await session.commit()

    assert device.updated_at >= original


async def test_device_delete_removes_the_row(
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    device = await devices.add(_device("disposable"))
    await session.commit()
    device_id = device.id

    await devices.delete(device)
    await session.commit()

    assert await devices.get(device_id) is None


async def test_duplicate_device_name_raises(
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    """Integrity errors propagate rather than being swallowed."""
    await devices.add(_device("collision"))
    await session.commit()

    with pytest.raises(IntegrityError):
        await devices.add(_device("collision"))


async def test_duplicate_public_key_raises(
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    await devices.add(
        _device(
            "first",
            public_key="shared",
        )
    )
    await session.commit()

    with pytest.raises(IntegrityError):
        await devices.add(
            _device(
                "second",
                public_key="shared",
            )
        )


# --- Audit events ---


async def test_audit_add_and_get(
    audit: AuditEventRepository,
    session: AsyncSession,
) -> None:
    event = await audit.add(_audit_event(hash_value="a" * 64))
    await session.commit()

    stored = await audit.get(event.id)

    assert stored is not None
    assert stored.action == "tool.invoke"
    assert stored.decision is AuditDecision.ALLOW


async def test_audit_add_assigns_an_id_before_commit(
    audit: AuditEventRepository,
) -> None:
    event = await audit.add(_audit_event(hash_value="b" * 64))

    assert event.id >= 1


async def test_audit_get_returns_none_for_unknown_id(
    audit: AuditEventRepository,
) -> None:
    assert await audit.get(999_999) is None


async def test_audit_count(
    audit: AuditEventRepository,
    session: AsyncSession,
) -> None:
    assert await audit.count() == 0

    for index in range(3):
        await audit.add(_audit_event(hash_value=f"{index:064d}"))

    await session.commit()

    assert await audit.count() == 3


async def test_audit_list_recent_returns_newest_first(
    audit: AuditEventRepository,
    session: AsyncSession,
) -> None:
    base_time = utcnow()

    for index in range(3):
        event = _audit_event(
            hash_value=f"{index:064d}",
            action=f"action.{index}",
        )
        event.occurred_at = base_time + timedelta(minutes=index)
        await audit.add(event)

    await session.commit()

    actions = [event.action for event in await audit.list_recent()]

    assert actions == [
        "action.2",
        "action.1",
        "action.0",
    ]


async def test_audit_list_recent_respects_the_limit(
    audit: AuditEventRepository,
    session: AsyncSession,
) -> None:
    for index in range(10):
        await audit.add(_audit_event(hash_value=f"{index:064d}"))

    await session.commit()

    assert len(await audit.list_recent(limit=4)) == 4


async def test_audit_list_recent_is_empty_initially(
    audit: AuditEventRepository,
) -> None:
    assert await audit.list_recent() == []


async def test_audit_ordering_is_stable_for_identical_timestamps(
    audit: AuditEventRepository,
    session: AsyncSession,
) -> None:
    """Same-tick events must not come back in an arbitrary order."""
    moment = utcnow()

    for index in range(3):
        event = _audit_event(
            hash_value=f"{index:064d}",
            action=f"action.{index}",
        )
        event.occurred_at = moment
        await audit.add(event)

    await session.commit()

    ids = [event.id for event in await audit.list_recent()]

    assert ids == sorted(ids, reverse=True)


async def test_audit_list_for_device_filters_correctly(
    audit: AuditEventRepository,
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    first = await devices.add(_device("device-one"))
    second = await devices.add(_device("device-two"))
    await session.commit()

    await audit.add(
        _audit_event(
            hash_value="1" * 64,
            device_id=first.id,
        )
    )
    await audit.add(
        _audit_event(
            hash_value="2" * 64,
            device_id=second.id,
        )
    )
    await audit.add(
        _audit_event(
            hash_value="3" * 64,
            device_id=None,
        )
    )
    await session.commit()

    events = await audit.list_for_device(first.id)

    assert len(events) == 1
    assert events[0].device_id == first.id


async def test_audit_list_for_device_respects_the_limit(
    audit: AuditEventRepository,
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    device = await devices.add(_device("busy-device"))
    await session.commit()

    for index in range(6):
        await audit.add(
            _audit_event(
                hash_value=f"{index:064d}",
                device_id=device.id,
            )
        )

    await session.commit()

    assert (
        len(
            await audit.list_for_device(
                device.id,
                limit=2,
            )
        )
        == 2
    )


async def test_audit_list_for_unknown_device_is_empty(
    audit: AuditEventRepository,
) -> None:
    assert await audit.list_for_device("00000000-0000-0000-0000-000000000000") == []


@pytest.mark.parametrize(
    "result",
    list(AuditResult),
)
async def test_audit_stores_every_result_state(
    audit: AuditEventRepository,
    session: AsyncSession,
    result: AuditResult,
) -> None:
    event = await audit.add(
        _audit_event(
            hash_value=f"{result.value:>064}",
            result=result,
        )
    )
    await session.commit()

    stored = await audit.get(event.id)

    assert stored is not None
    assert stored.result is result


async def test_audit_repository_is_append_only() -> None:
    """No mutation method may be added "for symmetry"."""
    public = {name for name in dir(AuditEventRepository) if not name.startswith("_")}

    assert public == {
        "add",
        "get",
        "list_recent",
        "list_for_device",
        "count",
    }
    assert "update" not in public
    assert "delete" not in public


async def test_duplicate_audit_hash_raises(
    audit: AuditEventRepository,
    session: AsyncSession,
) -> None:
    await audit.add(_audit_event(hash_value="c" * 64))
    await session.commit()

    with pytest.raises(IntegrityError):
        await audit.add(_audit_event(hash_value="c" * 64))


# --- Foreign keys ---


async def test_audit_for_unknown_device_violates_the_foreign_key(
    audit: AuditEventRepository,
) -> None:
    """Proves PRAGMA foreign_keys=ON is in force through the repository.

    The violation surfaces on ``add``, because that flushes to obtain the
    generated id. Failing at flush rather than at commit is preferable: the
    caller learns immediately, next to the offending statement.
    """
    with pytest.raises(IntegrityError):
        await audit.add(
            _audit_event(
                hash_value="d" * 64,
                device_id=("00000000-0000-0000-0000-000000000000"),
            )
        )


async def test_deleting_a_device_with_audit_history_is_refused(
    audit: AuditEventRepository,
    devices: DeviceRepository,
    session: AsyncSession,
) -> None:
    """RESTRICT must protect the audit trail from the repository layer too."""
    device = await devices.add(_device("protected"))
    await session.commit()

    await audit.add(
        _audit_event(
            hash_value="e" * 64,
            device_id=device.id,
        )
    )
    await session.commit()

    with pytest.raises(IntegrityError):
        await devices.delete(device)
        await session.commit()


# --- Transaction ownership ---


async def test_repository_does_not_commit_on_its_own(
    database: Database,
) -> None:
    """The caller owns the transaction; a rollback must discard the work."""
    async with database.session() as first:
        await PreferenceRepository(first).set(
            "uncommitted",
            "1",
        )
        await first.rollback()

    async with database.session() as second:
        assert await PreferenceRepository(second).get("uncommitted") is None


async def test_committed_work_is_visible_to_a_new_session(
    database: Database,
) -> None:
    async with database.session() as first:
        await PreferenceRepository(first).set(
            "committed",
            "1",
        )
        await first.commit()

    async with database.session() as second:
        stored = await PreferenceRepository(second).get("committed")

        assert stored is not None
        assert stored.value == "1"


async def test_device_add_is_rolled_back_without_commit(
    database: Database,
) -> None:
    async with database.session() as first:
        await DeviceRepository(first).add(_device("ghost"))
        await first.rollback()

    async with database.session() as second:
        assert await DeviceRepository(second).get_by_name("ghost") is None


async def test_audit_add_is_rolled_back_without_commit(
    database: Database,
) -> None:
    """Even an audit insert obeys the caller's transaction boundary."""
    async with database.session() as first:
        await AuditEventRepository(first).add(_audit_event(hash_value="f" * 64))
        await first.rollback()

    async with database.session() as second:
        assert await AuditEventRepository(second).count() == 0


async def test_multiple_repositories_share_one_transaction(
    database: Database,
) -> None:
    """A device and its audit entry must be able to commit atomically."""
    async with database.session() as active:
        device = await DeviceRepository(active).add(_device("atomic"))

        await AuditEventRepository(active).add(
            _audit_event(
                hash_value="0" * 64,
                device_id=device.id,
            )
        )

        await active.rollback()

    async with database.session() as verify:
        assert await DeviceRepository(verify).get_by_name("atomic") is None
        assert await AuditEventRepository(verify).count() == 0


# --- Isolation ---


async def test_repositories_bind_to_the_session_they_are_given(
    database: Database,
) -> None:
    async with database.session() as first, database.session() as second:
        await PreferenceRepository(first).set(
            "session-scoped",
            "1",
        )

        assert await PreferenceRepository(second).get("session-scoped") is None


async def test_database_stays_in_the_temporary_directory(
    database: Database,
    tmp_path: Path,
) -> None:
    """Explicit guard: the real NOVA database must not be touched."""
    assert database.settings.path.parent == tmp_path
    assert database.settings.path.exists()
