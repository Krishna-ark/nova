"""Repository layer for NOVA storage.

Repositories own database access only. Business rules, permission decisions
and device lifecycle logic live above this layer, and transaction boundaries
belong to the caller.
"""

from nova.storage.repositories.audit import AuditEventRepository
from nova.storage.repositories.devices import DeviceRepository
from nova.storage.repositories.preferences import PreferenceRepository

__all__ = [
    "AuditEventRepository",
    "DeviceRepository",
    "PreferenceRepository",
]
