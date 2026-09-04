"""Tests for the NOVA tool contracts, registry, and execution boundary."""

from __future__ import annotations

from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel, Field

from nova.permissions.engine import PermissionEngine
from nova.permissions.policy import PermissionPolicy, PermissionRule
from nova.tools.base import (
    RiskLevel,
    Tool,
    ToolMetadata,
    ToolOutcome,
    VerificationLevel,
)
from nova.tools.executor import ToolExecutionError, ToolExecutor
from nova.tools.registry import ToolRegistrationError, ToolRegistry


class EchoArguments(BaseModel):
    message: str = Field(min_length=1)


class WrongArguments(BaseModel):
    message: int


class EchoTool(Tool[EchoArguments]):
    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        tool_id="echo",
        name="Echo",
        description="Return the supplied message.",
        risk=RiskLevel.LOW,
        required_permission="test.echo",
        verification=VerificationLevel.NONE,
    )
    input_model: ClassVar[type[BaseModel]] = EchoArguments

    async def execute(self, arguments: EchoArguments) -> ToolOutcome:
        return ToolOutcome(ok=True, data={"message": arguments.message})


class VerifyingTool(Tool[EchoArguments]):
    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        tool_id="verify",
        name="Verifying tool",
        description="A tool whose result is verified.",
        risk=RiskLevel.LOW,
        required_permission="test.verify",
        verification=VerificationLevel.STATE_QUERY,
    )
    input_model: ClassVar[type[BaseModel]] = EchoArguments
    verification_calls: ClassVar[int] = 0
    verification_result: ClassVar[bool | None] = True

    async def execute(self, arguments: EchoArguments) -> ToolOutcome:
        return ToolOutcome(ok=True, data={"message": arguments.message})

    async def verify(
        self,
        arguments: EchoArguments,
        outcome: ToolOutcome,
    ) -> bool | None:
        _ = arguments, outcome
        type(self).verification_calls += 1
        return type(self).verification_result


class FailingTool(Tool[EchoArguments]):
    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        tool_id="failing",
        name="Failing tool",
        description="Returns an expected failure outcome.",
        risk=RiskLevel.LOW,
        required_permission="test.failing",
        verification=VerificationLevel.NONE,
    )
    input_model: ClassVar[type[BaseModel]] = EchoArguments

    async def execute(self, arguments: EchoArguments) -> ToolOutcome:
        return ToolOutcome(
            ok=False,
            detail=f"Could not process: {arguments.message}",
        )


class MissingMetadataTool(Tool[EchoArguments]):
    input_model: ClassVar[type[BaseModel]] = EchoArguments

    async def execute(self, arguments: EchoArguments) -> ToolOutcome:
        return ToolOutcome(ok=True, data={"message": arguments.message})


class MissingInputModelTool(Tool[BaseModel]):
    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        tool_id="missing_input",
        name="Missing input model",
        description="Invalid test tool.",
        risk=RiskLevel.LOW,
        required_permission="test.missing",
    )

    async def execute(self, arguments: BaseModel) -> ToolOutcome:
        _ = arguments
        return ToolOutcome(ok=True)


def _executor(
    tool: type[Tool[Any]],
    *,
    permission: str,
    risk: RiskLevel = RiskLevel.LOW,
    requires_confirmation: bool = False,
) -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(tool)

    policy = PermissionPolicy(
        rules=(
            PermissionRule(
                permission=permission,
                allowed_risk=risk,
                requires_confirmation=requires_confirmation,
            ),
        )
    )

    return ToolExecutor(registry, PermissionEngine(policy))


# --- ToolRegistry ---


def test_registry_registers_valid_tool() -> None:
    registry = ToolRegistry()

    registry.register(EchoTool)

    assert "echo" in registry
    assert len(registry) == 1
    assert registry.get("echo") is EchoTool


def test_registry_returns_none_for_unknown_tool() -> None:
    registry = ToolRegistry()

    assert registry.get("unknown") is None
    assert "unknown" not in registry


def test_registry_exposes_registered_metadata() -> None:
    registry = ToolRegistry()

    registry.register(EchoTool)

    metadata = registry.metadata()

    assert metadata == (EchoTool.metadata,)


def test_registry_rejects_non_tool_object() -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolRegistrationError, match="must subclass Tool"):
        registry.register(cast(type[Tool[Any]], object))


def test_registry_rejects_missing_metadata() -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolRegistrationError, match="valid ToolMetadata"):
        registry.register(MissingMetadataTool)


def test_registry_rejects_missing_input_model() -> None:
    registry = ToolRegistry()

    with pytest.raises(ToolRegistrationError, match="Pydantic input_model"):
        registry.register(MissingInputModelTool)


def test_registry_rejects_duplicate_tool_id() -> None:
    registry = ToolRegistry()

    registry.register(EchoTool)

    with pytest.raises(ToolRegistrationError, match="already registered"):
        registry.register(EchoTool)


# --- ToolExecutor ---


@pytest.mark.asyncio
async def test_executor_rejects_unknown_tool() -> None:
    executor = ToolExecutor(
        ToolRegistry(),
        PermissionEngine(PermissionPolicy()),
    )

    with pytest.raises(ToolExecutionError, match="unknown tool: echo"):
        await executor.execute(
            "echo",
            EchoArguments(message="hello"),
        )


@pytest.mark.asyncio
async def test_executor_denies_missing_permission() -> None:
    executor = _executor(
        EchoTool,
        permission="different.permission",
    )

    with pytest.raises(
        ToolExecutionError,
        match="is not granted by policy",
    ):
        await executor.execute(
            "echo",
            EchoArguments(message="hello"),
        )


@pytest.mark.asyncio
async def test_executor_rejects_confirmation_required() -> None:
    executor = _executor(
        EchoTool,
        permission="test.echo",
        requires_confirmation=True,
    )

    with pytest.raises(
        ToolExecutionError,
        match="confirmation required for tool 'echo'",
    ):
        await executor.execute(
            "echo",
            EchoArguments(message="hello"),
        )


@pytest.mark.asyncio
async def test_executor_rejects_risk_above_policy_limit() -> None:
    executor = _executor(
        EchoTool,
        permission="test.echo",
        risk=RiskLevel.LOW,
    )

    EchoTool.metadata = EchoTool.metadata.model_copy(update={"risk": RiskLevel.HIGH})

    try:
        with pytest.raises(
            ToolExecutionError,
            match="exceeds the policy limit",
        ):
            await executor.execute(
                "echo",
                EchoArguments(message="hello"),
            )
    finally:
        EchoTool.metadata = EchoTool.metadata.model_copy(update={"risk": RiskLevel.LOW})


@pytest.mark.asyncio
async def test_executor_executes_allowed_tool() -> None:
    executor = _executor(
        EchoTool,
        permission="test.echo",
    )

    outcome = await executor.execute(
        "echo",
        EchoArguments(message="hello"),
    )

    assert outcome.ok is True
    assert outcome.data["message"] == "hello"


@pytest.mark.asyncio
async def test_executor_revalidates_arguments() -> None:
    executor = _executor(
        EchoTool,
        permission="test.echo",
    )

    with pytest.raises(
        ToolExecutionError,
        match="invalid arguments for tool 'echo'",
    ):
        await executor.execute(
            "echo",
            cast(BaseModel, WrongArguments(message=123)),
        )


@pytest.mark.asyncio
async def test_executor_returns_expected_tool_failure() -> None:
    executor = _executor(
        FailingTool,
        permission="test.failing",
    )

    outcome = await executor.execute(
        "failing",
        EchoArguments(message="missing"),
    )

    assert outcome.ok is False
    assert outcome.detail == "Could not process: missing"


@pytest.mark.asyncio
async def test_executor_skips_verification_for_none() -> None:
    executor = _executor(
        EchoTool,
        permission="test.echo",
    )

    outcome = await executor.execute(
        "echo",
        EchoArguments(message="hello"),
    )

    assert outcome.ok is True


@pytest.mark.asyncio
async def test_executor_runs_verification_when_declared() -> None:
    VerifyingTool.verification_calls = 0
    VerifyingTool.verification_result = True

    executor = _executor(
        VerifyingTool,
        permission="test.verify",
    )

    outcome = await executor.execute(
        "verify",
        EchoArguments(message="hello"),
    )

    assert outcome.ok is True
    assert VerifyingTool.verification_calls == 1


@pytest.mark.asyncio
async def test_executor_rejects_failed_verification() -> None:
    VerifyingTool.verification_calls = 0
    VerifyingTool.verification_result = False

    executor = _executor(
        VerifyingTool,
        permission="test.verify",
    )

    try:
        with pytest.raises(
            ToolExecutionError,
            match="verification failed for tool 'verify'",
        ):
            await executor.execute(
                "verify",
                EchoArguments(message="hello"),
            )

        assert VerifyingTool.verification_calls == 1
    finally:
        VerifyingTool.verification_result = True


@pytest.mark.asyncio
async def test_executor_accepts_verification_returning_none() -> None:
    VerifyingTool.verification_calls = 0
    VerifyingTool.verification_result = None

    executor = _executor(
        VerifyingTool,
        permission="test.verify",
    )

    outcome = await executor.execute(
        "verify",
        EchoArguments(message="hello"),
    )

    assert outcome.ok is True
    assert VerifyingTool.verification_calls == 1
