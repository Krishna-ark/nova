"""Integration tests for audit persistence at the tool execution boundary."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from nova.config import DatabaseSettings
from nova.permissions.engine import PermissionEngine
from nova.permissions.policy import PermissionPolicy, PermissionRule
from nova.security.audit_chain import (
    GENESIS_HASH,
    canonicalize,
    verify_chain_link,
    verify_event_hash,
)
from nova.storage import Database
from nova.storage.models import AuditDecision, AuditResult, Base
from nova.storage.repositories import AuditEventRepository
from nova.tools.base import RiskLevel, Tool, ToolMetadata, ToolOutcome
from nova.tools.executor import ToolExecutionError, ToolExecutor
from nova.tools.registry import ToolRegistry


class AuditArguments(BaseModel):
    """Arguments used by the small tools in this module."""

    message: str = Field(min_length=1)


class AuditedTool(Tool[AuditArguments]):
    """Return either a successful or expected-failure outcome."""

    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        tool_id="audited",
        name="Audited tool",
        description="Tool used to test audit persistence.",
        risk=RiskLevel.LOW,
        required_permission="test.audited",
    )
    input_model: ClassVar[type[BaseModel]] = AuditArguments

    async def execute(self, arguments: AuditArguments) -> ToolOutcome:
        if arguments.message == "fail":
            return ToolOutcome(ok=False, detail="Expected tool failure")
        return ToolOutcome(ok=True, data={"message": arguments.message})


class RaisingTool(Tool[AuditArguments]):
    """Represent a defect in tool implementation."""

    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        tool_id="raising",
        name="Raising tool",
        description="Tool used to test exception audit persistence.",
        risk=RiskLevel.LOW,
        required_permission="test.raising",
    )
    input_model: ClassVar[type[BaseModel]] = AuditArguments

    async def execute(self, arguments: AuditArguments) -> ToolOutcome:
        _ = arguments
        raise RuntimeError("defect")


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    """Yield a database with the full schema, then dispose it."""
    instance = Database(DatabaseSettings(path=tmp_path / "nova.db"))
    async with instance.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield instance
    finally:
        await instance.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    """Yield a transaction-owning test session."""
    async with database.session() as active_session:
        yield active_session


def _executor(
    session: AsyncSession,
    tool: type[Tool[Any]],
    *,
    permission: str,
    requires_confirmation: bool = False,
) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(tool)
    policy = PermissionPolicy(
        rules=(
            PermissionRule(
                permission=permission,
                allowed_risk=RiskLevel.LOW,
                requires_confirmation=requires_confirmation,
            ),
        )
    )
    return ToolExecutor(
        registry,
        PermissionEngine(policy),
        AuditEventRepository(session),
        actor="operator",
    )


def _assert_hash_is_valid(event: Any) -> None:
    assert verify_event_hash(
        actor=event.actor,
        action=event.action,
        tool_name=event.tool_name,
        decision=event.decision.value,
        result=event.result.value,
        args_hash=event.args_hash,
        detail=event.detail,
        occurred_at=event.occurred_at.isoformat(),
        prev_hash=event.prev_hash,
        expected_hash=event.hash,
    )


async def test_allowed_outcomes_are_audited_as_a_verifiable_chain(
    session: AsyncSession,
) -> None:
    executor = _executor(session, AuditedTool, permission="test.audited")

    assert (await executor.execute("audited", AuditArguments(message="ok"))).ok is True
    assert (await executor.execute("audited", AuditArguments(message="fail"))).ok is False
    await session.commit()

    events = sorted(await AuditEventRepository(session).list_recent(), key=lambda event: event.id)

    assert [event.result for event in events] == [AuditResult.SUCCESS, AuditResult.FAILED]
    assert events[0].decision is AuditDecision.ALLOW
    assert events[0].prev_hash == GENESIS_HASH
    assert verify_chain_link(previous_hash=None, current_prev_hash=events[0].prev_hash)
    assert events[1].prev_hash is not None
    assert verify_chain_link(
        previous_hash=events[0].hash,
        current_prev_hash=events[1].prev_hash,
    )
    assert (
        events[0].args_hash
        == hashlib.sha256(canonicalize({"message": "ok"}).encode("utf-8")).hexdigest()
    )
    for event in events:
        _assert_hash_is_valid(event)


async def test_denied_execution_is_audited(session: AsyncSession) -> None:
    executor = _executor(session, AuditedTool, permission="different.permission")

    with pytest.raises(ToolExecutionError, match="not granted by policy"):
        await executor.execute("audited", AuditArguments(message="blocked"))
    await session.commit()

    event = (await AuditEventRepository(session).list_recent())[0]
    assert event.decision is AuditDecision.DENY
    assert event.result is AuditResult.DENIED
    assert event.prev_hash == GENESIS_HASH
    _assert_hash_is_valid(event)


async def test_confirmation_required_execution_is_audited(session: AsyncSession) -> None:
    executor = _executor(
        session,
        AuditedTool,
        permission="test.audited",
        requires_confirmation=True,
    )

    with pytest.raises(ToolExecutionError, match="confirmation required"):
        await executor.execute("audited", AuditArguments(message="confirm"))
    await session.commit()

    event = (await AuditEventRepository(session).list_recent())[0]
    assert event.decision is AuditDecision.CONFIRM
    assert event.result is AuditResult.REQUIRES_CONFIRMATION
    _assert_hash_is_valid(event)


async def test_tool_exception_is_audited_and_reraised(session: AsyncSession) -> None:
    executor = _executor(session, RaisingTool, permission="test.raising")

    with pytest.raises(RuntimeError, match="defect"):
        await executor.execute("raising", AuditArguments(message="trigger"))
    await session.commit()

    event = (await AuditEventRepository(session).list_recent())[0]
    assert event.decision is AuditDecision.ALLOW
    assert event.result is AuditResult.FAILED
    assert event.detail == "Tool execution raised an unhandled exception."
    _assert_hash_is_valid(event)


async def test_executor_leaves_audit_commit_to_its_caller(database: Database) -> None:
    async with database.session() as active_session:
        executor = _executor(active_session, AuditedTool, permission="test.audited")

        await executor.execute("audited", AuditArguments(message="rollback"))
        await active_session.rollback()

    async with database.session() as verify_session:
        assert await AuditEventRepository(verify_session).count() == 0
