"""Tests for the authenticated tool-command service contract."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from nova.api import create_app
from nova.api.commands import CommandService, ToolCommand
from nova.config import DatabaseSettings, Settings
from nova.permissions.engine import PermissionEngine
from nova.permissions.policy import PermissionPolicy, PermissionRule
from nova.storage import Database
from nova.storage.models import AuditResult, Base
from nova.storage.repositories import AuditEventRepository
from nova.tools.base import RiskLevel, Tool, ToolMetadata, ToolOutcome
from nova.tools.executor import ToolExecutionError
from nova.tools.registry import ToolRegistry

FAKE_TOKEN = "command-token-" + ("x" * 32)


class CommandArguments(BaseModel):
    message: str


class CommandTool(Tool[CommandArguments]):
    metadata: ClassVar[ToolMetadata] = ToolMetadata(
        tool_id="command_echo",
        name="Command echo",
        description="Echo a command message.",
        risk=RiskLevel.LOW,
        required_permission="test.command_echo",
    )
    input_model: ClassVar[type[BaseModel]] = CommandArguments

    async def execute(self, arguments: CommandArguments) -> ToolOutcome:
        return ToolOutcome(ok=True, data={"message": arguments.message})


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    instance = Database(DatabaseSettings(path=tmp_path / "nova.db"))
    async with instance.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield instance
    finally:
        await instance.dispose()


def _service(database: Database) -> CommandService:
    registry = ToolRegistry()
    registry.register(CommandTool)
    permissions = PermissionEngine(
        PermissionPolicy(
            rules=(
                PermissionRule(
                    permission="test.command_echo",
                    allowed_risk=RiskLevel.LOW,
                ),
            )
        )
    )
    return CommandService(database, registry, permissions)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        security={"auth_token": FAKE_TOKEN},
    )


async def _create_schema(database: Database) -> None:
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def test_command_service_executes_and_commits_audit(
    database: Database,
) -> None:
    service = _service(database)

    outcome = await service.execute(
        ToolCommand(
            type="tool.execute",
            request_id="request-1",
            tool_id="command_echo",
            arguments={"message": "hello"},
        )
    )

    assert outcome.ok is True
    assert outcome.data["message"] == "hello"

    async with database.session() as session:
        events = await AuditEventRepository(session).list_recent()

    assert len(events) == 1
    assert events[0].result is AuditResult.SUCCESS


async def test_command_service_commits_denied_audit(
    database: Database,
) -> None:
    service = CommandService(
        database,
        ToolRegistry(),
        PermissionEngine(PermissionPolicy()),
    )

    with pytest.raises(ToolExecutionError, match="unknown tool: missing_tool"):
        await service.execute(
            ToolCommand(
                type="tool.execute",
                request_id="request-2",
                tool_id="missing_tool",
                arguments={"message": "blocked"},
            )
        )

    async with database.session() as session:
        events = await AuditEventRepository(session).list_recent()

    assert len(events) == 1
    assert events[0].result is AuditResult.DENIED


def test_command_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        ToolCommand.model_validate(
            {
                "type": "tool.execute",
                "request_id": "request-3",
                "tool_id": "command_echo",
                "arguments": {},
                "unexpected": True,
            }
        )


def test_command_requires_tool_execute_type() -> None:
    with pytest.raises(ValueError):
        ToolCommand.model_validate(
            {
                "type": "tool.inspect",
                "request_id": "request-4",
                "tool_id": "command_echo",
                "arguments": {},
            }
        )


def test_command_websocket_returns_structured_result(tmp_path: Path) -> None:
    database = Database(DatabaseSettings(path=tmp_path / "socket.db"))
    registry = ToolRegistry()
    registry.register(CommandTool)
    permissions = PermissionEngine(
        PermissionPolicy(
            rules=(
                PermissionRule(
                    permission="test.command_echo",
                    allowed_risk=RiskLevel.LOW,
                ),
            )
        )
    )

    try:
        asyncio.run(_create_schema(database))
        app = create_app(
            _settings(),
            database=database,
            registry=registry,
            permissions=permissions,
        )

        with (
            TestClient(app) as client,
            client.websocket_connect(
                "/ws/commands",
                headers={"Authorization": f"Bearer {FAKE_TOKEN}"},
            ) as websocket,
        ):
            websocket.send_json(
                {
                    "type": "tool.execute",
                    "request_id": "socket-1",
                    "tool_id": "command_echo",
                    "arguments": {"message": "hello"},
                }
            )

            assert websocket.receive_json() == {
                "type": "tool.result",
                "request_id": "socket-1",
                "ok": True,
                "detail": None,
                "data": {"message": "hello"},
            }

            websocket.send_json({"type": "invalid"})

            assert websocket.receive_json() == {
                "type": "tool.error",
                "request_id": None,
                "code": "invalid_command",
                "detail": "Invalid tool command.",
            }
    finally:
        asyncio.run(database.dispose())
