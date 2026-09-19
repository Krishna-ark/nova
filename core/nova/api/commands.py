"""Structured command protocol for the authenticated tool channel."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from nova.permissions.engine import PermissionEngine
from nova.storage import Database
from nova.storage.repositories import AuditEventRepository
from nova.tools.base import ToolOutcome
from nova.tools.executor import ToolExecutionError, ToolExecutor
from nova.tools.registry import ToolRegistry


class ToolCommand(BaseModel):
    """One request accepted by the tool-command WebSocket."""

    model_config = {"extra": "forbid"}

    type: Literal["tool.execute"]
    request_id: str = Field(min_length=1, max_length=128)
    tool_id: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    arguments: dict[str, Any] = Field(default_factory=dict)


class CommandService:
    """Execute one command with a fresh session and transaction."""

    def __init__(
        self,
        database: Database,
        registry: ToolRegistry,
        permissions: PermissionEngine,
        *,
        actor: str = "websocket",
    ) -> None:
        self._database = database
        self._registry = registry
        self._permissions = permissions
        self._actor = actor

    async def execute(self, command: ToolCommand) -> ToolOutcome:
        """Execute a command and commit handled execution outcomes."""
        async with self._database.session() as session:
            executor = ToolExecutor(
                self._registry,
                self._permissions,
                AuditEventRepository(session),
                actor=self._actor,
            )
            arguments = _arguments_model(command.arguments)

            try:
                outcome = await executor.execute(command.tool_id, arguments)
            except ToolExecutionError:
                await session.commit()
                raise

            await session.commit()
            return outcome


def _arguments_model(arguments: dict[str, Any]) -> BaseModel:
    """Wrap transport arguments for executor-side model validation."""
    model = _TransportArguments.model_validate(arguments)
    return model


class _TransportArguments(BaseModel):
    """Flexible transport shell; the registered tool remains authoritative."""

    model_config = {"extra": "allow"}


async def execute_command(
    service: CommandService,
    command: ToolCommand,
) -> ToolOutcome:
    """Small injectable seam used by the WebSocket transport."""
    return await service.execute(command)


__all__ = [
    "CommandService",
    "ToolCommand",
    "ToolExecutionError",
    "execute_command",
]
