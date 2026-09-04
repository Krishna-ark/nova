"""Safe execution boundary for NOVA tools.

Every tool invocation passes through this executor. The executor validates
the registered tool, validates its arguments, evaluates permissions, executes
the tool, optionally verifies its effect, and returns the resulting outcome.

The executor never accepts arbitrary callables or dynamically supplied code.
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from nova.permissions.engine import PermissionDecision, PermissionEngine
from nova.tools.base import ToolOutcome
from nova.tools.registry import ToolRegistry


class ToolExecutionError(RuntimeError):
    """Raised when a tool cannot be executed safely."""


class ToolExecutor:
    """Execute registered tools through the permission boundary."""

    def __init__(
        self,
        registry: ToolRegistry,
        permissions: PermissionEngine,
    ) -> None:
        self._registry = registry
        self._permissions = permissions

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
            raise ToolExecutionError(f"unknown tool: {tool_id}")

        permission = self._permissions.evaluate(tool_class.metadata)

        if permission.decision is PermissionDecision.DENY:
            raise ToolExecutionError(permission.detail)

        if permission.decision is PermissionDecision.CONFIRM:
            raise ToolExecutionError(
                f"confirmation required for tool '{tool_id}': {permission.detail}"
            )

        try:
            validated_arguments = tool_class.input_model.model_validate(arguments.model_dump())
        except ValidationError as exc:
            raise ToolExecutionError(f"invalid arguments for tool '{tool_id}'") from exc

        tool = tool_class()
        outcome = await tool.execute(validated_arguments)

        if tool.metadata.verification.value != "none":
            verification = await tool.verify(validated_arguments, outcome)

            if verification is False:
                raise ToolExecutionError(f"verification failed for tool '{tool_id}'")

        return outcome
