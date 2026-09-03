"""Data access for audit events.

Intentionally missing: ``update`` and ``delete``. The audit log is append-only
at the architecture level, and its hash chain exists to make tampering
detectable. Providing mutation methods "for symmetry" would supply exactly
the tool the design is meant to deny.

Hash computation and chain verification are not implemented here. This layer
persists the ``prev_hash`` and ``hash`` values it is given; the executor will
produce and check them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from nova.storage.models import AuditEvent

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

# Default page size for recent-event queries.
DEFAULT_LIMIT = 50


class AuditEventRepository:
    """Append and read operations for the ``audit_events`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: AuditEvent) -> AuditEvent:
        """Stage an audit event and flush so its id is assigned."""
        self._session.add(event)
        await self._session.flush()
        return event

    async def get(self, event_id: int) -> AuditEvent | None:
        """Return the event, or None when the id is unknown."""
        return await self._session.get(AuditEvent, event_id)

    async def list_recent(
        self,
        limit: int = DEFAULT_LIMIT,
    ) -> Sequence[AuditEvent]:
        """Return the newest events first.

        Ordered by id as well as timestamp: two events written in the same
        clock tick would otherwise come back in an arbitrary order.
        """
        result = await self._session.execute(
            select(AuditEvent)
            .order_by(
                AuditEvent.occurred_at.desc(),
                AuditEvent.id.desc(),
            )
            .limit(limit)
        )
        return result.scalars().all()

    async def list_for_device(
        self,
        device_id: str,
        limit: int = DEFAULT_LIMIT,
    ) -> Sequence[AuditEvent]:
        """Return the newest events recorded against one device."""
        result = await self._session.execute(
            select(AuditEvent)
            .where(AuditEvent.device_id == device_id)
            .order_by(
                AuditEvent.occurred_at.desc(),
                AuditEvent.id.desc(),
            )
            .limit(limit)
        )
        return result.scalars().all()

    async def count(self) -> int:
        """Return the total number of recorded events."""
        result = await self._session.execute(select(func.count()).select_from(AuditEvent))
        return result.scalar_one()
