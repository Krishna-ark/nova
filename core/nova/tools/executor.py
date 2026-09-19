"""Safe execution boundary for NOVA tools.

Every tool invocation passes through this executor. The executor validates
the registered tool, validates its arguments, evaluates permissions, executes
the tool, optionally verifies its effect, and returns the resulting outcome.

The executor never accepts arbitrary callables or dynamically supplied code.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from nova.permissions.engine import PermissionDecision, PermissionEngine
from nova.security.audit_chain import GENESIS_HASH, canonicalize, compute_event_hash
from nova.storage.models import AuditDecision, AuditEvent, AuditResult, utcnow
from nova.tools.base import ToolOutcome
from nova.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from nova.storage.repositories import AuditEventRepository


class ToolExecutionError(RuntimeError):
    """Raised when a tool cannot be executed safely."""


class ToolExecutor:
    """Execute registered tools through the permission boundary."""

    def __init__(
        self,
        registry: ToolRegistry,
        permissions: PermissionEngine,
        audit_events: AuditEventRepository | None = None,
        actor: str = "system",
    ) -> None:
        """Create an executor.

        ``audit_events`` is session-scoped, like every repository. The caller
        supplies it when an invocation should be durable and remains
        responsible for committing or rolling back the shared transaction.
        Existing in-memory users can omit it until they have storage wiring.
        """
        self._registry = registry
        self._permissions = permissions
        self._audit_events = audit_events
        self._actor = actor

    async def execute(
        self,
        tool_id: str,
        arguments: BaseModel,
    ) -> ToolOutcome:
        """Validate, authorize, execute, and verify a registered tool.

        Args:
            tool_id: ID of the registered tool to execute.
            arguments: Already-validated Pydantic arguments.

        Returns:
            The tool's outcome.

        Raises:
            ToolExecutionError: If the tool is unknown, arguments are invalid
                for the registered tool, or permission is denied.
        """
        tool_class = self._registry.get(tool_id)

        if tool_class is None:
            await self._record_audit_event(
                tool_id=tool_id,
                arguments=arguments,
                decision=AuditDecision.DENY,
                result=AuditResult.DENIED,
                detail=f"Unknown tool: {tool_id}",
            )
            raise ToolExecutionError(f"unknown tool: {tool_id}")

        permission = self._permissions.evaluate(tool_class.metadata)

        if permission.decision is PermissionDecision.DENY:
            await self._record_audit_event(
                tool_id=tool_id,
                arguments=arguments,
                decision=AuditDecision.DENY,
                result=AuditResult.DENIED,
                detail=permission.detail,
            )
            raise ToolExecutionError(permission.detail)

        if permission.decision is PermissionDecision.CONFIRM:
            await self._record_audit_event(
                tool_id=tool_id,
                arguments=arguments,
                decision=AuditDecision.CONFIRM,
                result=AuditResult.REQUIRES_CONFIRMATION,
                detail=permission.detail,
            )
            raise ToolExecutionError(
                f"confirmation required for tool '{tool_id}': {permission.detail}"
            )

        try:
            if not isinstance(arguments, BaseModel):
                raise TypeError("tool arguments must be a Pydantic model")
            validated_arguments = tool_class.input_model.model_validate(arguments.model_dump())
        except ValidationError as exc:
            await self._record_audit_event(
                tool_id=tool_id,
                arguments=arguments,
                decision=AuditDecision.ALLOW,
                result=AuditResult.FAILED,
                detail=f"Invalid arguments for tool '{tool_id}'",
            )
            raise ToolExecutionError(f"invalid arguments for tool '{tool_id}'") from exc
        except TypeError as exc:
            await self._record_audit_event(
                tool_id=tool_id,
                arguments=arguments if isinstance(arguments, BaseModel) else None,
                decision=AuditDecision.ALLOW,
                result=AuditResult.FAILED,
                detail=f"Invalid arguments for tool '{tool_id}'",
            )
            raise ToolExecutionError(f"invalid arguments for tool '{tool_id}'") from exc

        tool = tool_class()
        try:
            raw_outcome: object = await tool.execute(validated_arguments)
            if not isinstance(raw_outcome, ToolOutcome):
                await self._record_audit_event(
                    tool_id=tool_id,
                    arguments=validated_arguments,
                    decision=AuditDecision.ALLOW,
                    result=AuditResult.FAILED,
                    detail=f"Tool '{tool_id}' returned an invalid outcome.",
                )
                raise ToolExecutionError(f"tool '{tool_id}' returned an invalid outcome")
            outcome = raw_outcome
            if tool.metadata.verification.value != "none":
                verification = await tool.verify(validated_arguments, outcome)

                if verification is False:
                    await self._record_audit_event(
                        tool_id=tool_id,
                        arguments=validated_arguments,
                        decision=AuditDecision.ALLOW,
                        result=AuditResult.UNVERIFIED,
                        detail=f"Verification failed for tool '{tool_id}'",
                    )
                    raise ToolExecutionError(f"verification failed for tool '{tool_id}'")
        except ToolExecutionError:
            raise
        except Exception:
            await self._record_audit_event(
                tool_id=tool_id,
                arguments=validated_arguments,
                decision=AuditDecision.ALLOW,
                result=AuditResult.FAILED,
                detail="Tool execution raised an unhandled exception.",
            )
            raise

        await self._record_audit_event(
            tool_id=tool_id,
            arguments=validated_arguments,
            decision=AuditDecision.ALLOW,
            result=AuditResult.SUCCESS if outcome.ok else AuditResult.FAILED,
            detail=outcome.detail,
        )

        return outcome

    async def _record_audit_event(
        self,
        *,
        tool_id: str,
        arguments: BaseModel | None,
        decision: AuditDecision,
        result: AuditResult,
        detail: str | None,
    ) -> None:
        """Stage an event without claiming ownership of the transaction."""
        if self._audit_events is None:
            return

        previous_events = await self._audit_events.list_recent(limit=1)
        prev_hash = previous_events[0].hash if previous_events else GENESIS_HASH
        occurred_at = utcnow()
        args_hash = self._arguments_hash(arguments) if arguments is not None else None
        occurred_at_value = occurred_at.isoformat()

        event = AuditEvent(
            occurred_at=occurred_at,
            actor=self._actor,
            action="tool.execute",
            tool_name=tool_id,
            decision=decision,
            result=result,
            args_hash=args_hash,
            detail=detail,
            prev_hash=prev_hash,
            hash=compute_event_hash(
                actor=self._actor,
                action="tool.execute",
                tool_name=tool_id,
                decision=decision.value,
                result=result.value,
                args_hash=args_hash,
                detail=detail,
                occurred_at=occurred_at_value,
                prev_hash=prev_hash,
            ),
        )
        await self._audit_events.add(event)

    @staticmethod
    def _arguments_hash(arguments: BaseModel) -> str:
        """Return a stable SHA-256 digest of the supplied arguments."""
        payload = canonicalize(arguments.model_dump(mode="json"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
