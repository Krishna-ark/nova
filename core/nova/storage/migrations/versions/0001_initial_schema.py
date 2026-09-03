"""initial schema

Creates the three durable NOVA tables: preferences, devices and audit_events.

Reviewed by hand after autogenerate. Two corrections were made to the
generated output:

1. Autogenerate emitted ``nova.storage.models.base.UtcDateTime(...)`` for the
   timestamp columns without importing it, which would raise NameError at
   migration time.
2. Even with an import added, referencing a live ORM type from a migration is
   wrong: a migration is a frozen historical snapshot, and renaming or moving
   ``UtcDateTime`` later would retroactively break this file. The columns use
   ``sa.DateTime(timezone=True)`` instead, which is the exact type
   ``UtcDateTime`` resolves to at the database level. The ORM contract is
   unchanged; the UTC coercion lives in Python, not in the schema.
3. Autogenerate wrapped every index creation in ``op.batch_alter_table``.
   Batch mode exists to work around SQLite's inability to ALTER an existing
   table: it rebuilds the table, copies the data and swaps it. On a table
   created moments earlier in the same migration that work is pointless, and
   it makes the emitted SQL harder to read. The indexes are created directly.
   ``render_as_batch=True`` remains set in env.py, where future migrations
   that genuinely alter existing tables will need it.

Revision ID: 0001
Revises:
Create Date: 2026-09-03 21:00:14.873822+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # devices is created first: audit_events carries a foreign key to it.
    op.create_table(
        "devices",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "platform",
            sa.Enum(
                "WINDOWS",
                "ANDROID",
                "LINUX",
                "MACOS",
                name="device_platform",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("public_key", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "AUTHORIZED",
                "REVOKED",
                name="device_status",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("os_version", sa.String(length=64), nullable=True),
        sa.Column("agent_version", sa.String(length=32), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_devices")),
        sa.UniqueConstraint("name", name=op.f("uq_devices_name")),
        sa.UniqueConstraint("public_key", name=op.f("uq_devices_public_key")),
    )

    op.create_index(
        op.f("ix_devices_status"),
        "devices",
        ["status"],
        unique=False,
    )

    op.create_table(
        "preferences",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_preferences")),
    )

    op.create_table(
        "audit_events",
        # Integer primary key: audit events need a stable database ordering
        # for later hash-chain processing.
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "actor",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "action",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "tool_name",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column(
            "device_id",
            sa.String(length=36),
            nullable=True,
        ),
        sa.Column(
            "decision",
            sa.Enum(
                "ALLOW",
                "DENY",
                "CONFIRM",
                name="audit_decision",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "result",
            sa.Enum(
                "SUCCESS",
                "FAILED",
                "TIMEOUT",
                "DENIED",
                "OFFLINE",
                "REQUIRES_CONFIRMATION",
                "PARTIAL_SUCCESS",
                "CANCELLED",
                "UNVERIFIED",
                name="audit_result",
                native_enum=False,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column(
            "args_hash",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "detail",
            sa.Text(),
            nullable=True,
        ),
        # Hash-chain columns. prev_hash is NULL for the first row only.
        sa.Column(
            "prev_hash",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "hash",
            sa.String(length=64),
            nullable=False,
        ),
        # RESTRICT, not CASCADE: deleting a device must not erase the record
        # of what it was told to do.
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_audit_events_device_id_devices"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=op.f("pk_audit_events"),
        ),
        sa.UniqueConstraint(
            "hash",
            name=op.f("uq_audit_events_hash"),
        ),
    )

    op.create_index(
        op.f("ix_audit_events_action"),
        "audit_events",
        ["action"],
        unique=False,
    )

    op.create_index(
        "ix_audit_events_actor_occurred_at",
        "audit_events",
        ["actor", "occurred_at"],
        unique=False,
    )

    op.create_index(
        op.f("ix_audit_events_device_id"),
        "audit_events",
        ["device_id"],
        unique=False,
    )

    op.create_index(
        op.f("ix_audit_events_occurred_at"),
        "audit_events",
        ["occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    # Reverse order: audit_events references devices, so it goes first.
    op.drop_index(
        op.f("ix_audit_events_occurred_at"),
        table_name="audit_events",
    )

    op.drop_index(
        op.f("ix_audit_events_device_id"),
        table_name="audit_events",
    )

    op.drop_index(
        "ix_audit_events_actor_occurred_at",
        table_name="audit_events",
    )

    op.drop_index(
        op.f("ix_audit_events_action"),
        table_name="audit_events",
    )

    op.drop_table("audit_events")

    op.drop_table("preferences")

    op.drop_index(
        op.f("ix_devices_status"),
        table_name="devices",
    )

    op.drop_table("devices")
