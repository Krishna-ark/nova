"""Data access for durable user preferences.

Transaction rule for every repository in this package: the caller owns the
transaction. Methods here stage work on the session via ``add``, ``delete``
or a statement, and the caller decides when to ``commit`` or ``rollback``.

A repository that committed on its own would make it impossible to write two
related changes atomically, and would hide the commit from the code that is
responsible for the outcome. ``flush`` is used where a generated value is
needed before the caller commits, since flush is undoable and commit is not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from nova.storage.models import Preference

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


class PreferenceRepository:
    """CRUD for the ``preferences`` table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> Preference | None:
        """Return the preference, or None when the key is unknown."""
        return await self._session.get(Preference, key)

    async def list_all(self) -> Sequence[Preference]:
        """Return every preference, ordered by key for stable output."""
        result = await self._session.execute(
            select(Preference).order_by(Preference.key)
        )
        return result.scalars().all()

    async def exists(self, key: str) -> bool:
        """Return True when the key is present."""
        result = await self._session.execute(
            select(func.count()).select_from(Preference).where(Preference.key == key)
        )
        return result.scalar_one() > 0

    async def set(
        self,
        key: str,
        value: str,
        description: str | None = None,
    ) -> Preference:
        """Create or update a preference and return it.

        An existing row is updated in place so ``created_at`` survives and
        ``updated_at`` advances via the model's ``onupdate``. ``description``
        is only overwritten when a value is supplied, so setting a value does
        not silently erase the explanation of what the key controls.
        """
        preference = await self._session.get(Preference, key)
        if preference is None:
            preference = Preference(
                key=key,
                value=value,
                description=description,
            )
            self._session.add(preference)
        else:
            preference.value = value
            if description is not None:
                preference.description = description

        await self._session.flush()
        return preference

    async def delete(self, key: str) -> bool:
        """Delete a preference. Returns True when a row was removed.

        Uses the ORM delete rather than a Core ``DELETE`` statement. A Core
        delete bypasses the identity map, so an instance already loaded in
        this session would survive as a stale object.
        """
        preference = await self._session.get(Preference, key)
        if preference is None:
            return False

        await self._session.delete(preference)
        await self._session.flush()
        return True
