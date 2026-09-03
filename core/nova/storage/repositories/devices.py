"""Data access for device records.

Queries and persistence only. Deciding *whether* a device may be authorised
or revoked belongs to the permission engine; this layer records what it is
told and reports what is stored.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from nova.storage.models import Device, DeviceStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


class DeviceRepository:
    """CRUD for the ``devices`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, device_id: str) -> Device | None:
        """Return the device, or None when the id is unknown."""
        return await self._session.get(Device, device_id)

    async def get_by_name(self, name: str) -> Device | None:
        """Return the device with this name, or None."""
        result = await self._session.execute(
            select(Device).where(Device.name == name)
        )
        return result.scalar_one_or_none()

    async def get_by_public_key(self, public_key: str) -> Device | None:
        """Return the device holding this public key, or None.

        Used during a pairing handshake to recognise a returning agent. The
        cryptographic verification itself is not done here.
        """
        result = await self._session.execute(
            select(Device).where(Device.public_key == public_key)
        )
        return result.scalar_one_or_none()

    async def list_all(self) -> Sequence[Device]:
        """Return every device, ordered by name."""
        result = await self._session.execute(
            select(Device).order_by(Device.name)
        )
        return result.scalars().all()

    async def list_authorized(self) -> Sequence[Device]:
        """Return devices that are authorised and not revoked.

        The ``revoked_at`` check is deliberate rather than redundant: a row
        whose status was not updated alongside its revocation timestamp must
        never be treated as authorised.
        """
        result = await self._session.execute(
            select(Device)
            .where(Device.status == DeviceStatus.AUTHORIZED)
            .where(Device.revoked_at.is_(None))
            .order_by(Device.name)
        )
        return result.scalars().all()

    async def add(self, device: Device) -> Device:
        """Stage a new device and flush so its defaults are populated."""
        self._session.add(device)
        await self._session.flush()
        return device

    async def update(self, device: Device) -> Device:
        """Flush pending changes to an already-tracked device.

        Attribute assignment is what actually changes a persistent object;
        this exists so callers have an explicit, greppable write point and so
        ``updated_at`` is applied before they read it back.
        """
        self._session.add(device)
        await self._session.flush()
        return device

    async def delete(self, device: Device) -> None:
        """Delete a device.

        A device referenced by audit events cannot be removed: the foreign key
        is ``ON DELETE RESTRICT``, so SQLAlchemy raises ``IntegrityError`` on
        commit. That is intentional, and the error is allowed to propagate.
        """
        await self._session.delete(device)
        await self._session.flush()
