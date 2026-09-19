"""FastAPI application factory for NOVA.

Scope so far: two unauthenticated status endpoints and one authenticated
WebSocket echo. There is no database access and no tool execution here yet;
those arrive in later groups.

The application is built by a factory rather than created at import time.
A module-level ``app = FastAPI()`` would bind configuration at import, which
makes it impossible for a test to build an app with different settings.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, WebSocket
from pydantic import BaseModel

from nova import __version__
from nova.api.commands import CommandService
from nova.api.websocket import websocket_commands, websocket_echo
from nova.permissions.engine import PermissionEngine
from nova.permissions.policy import PermissionPolicy
from nova.storage import Database
from nova.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from nova.config.settings import Settings


class HealthResponse(BaseModel):
    """Liveness payload.

    Deliberately says nothing about the database or any subsystem: nothing
    else is wired up yet, and a health check that claims more than it checks
    is worse than none.
    """

    status: str
    version: str


class VersionResponse(BaseModel):
    """Build and environment information."""

    name: str
    version: str
    environment: str


def create_app(
    settings: Settings,
    *,
    database: Database | None = None,
    registry: ToolRegistry | None = None,
    permissions: PermissionEngine | None = None,
) -> FastAPI:
    """Build the NOVA API application.

    Args:
        settings: Validated application configuration.

    Returns:
        A configured FastAPI instance with the status routes registered.
    """
    owned_database = database is None
    active_database = database or Database(settings.database)
    active_registry = registry or ToolRegistry()
    active_permissions = permissions or PermissionEngine(PermissionPolicy())
    command_service = CommandService(
        active_database,
        active_registry,
        active_permissions,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned_database:
                await active_database.dispose()

    app = FastAPI(
        title="NOVA",
        version=__version__,
        summary="Permission-controlled AI agent operating layer",
        lifespan=lifespan,
        # The interactive docs are useful locally. They are disabled in
        # production so the API surface is not advertised.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    @app.get("/health", response_model=HealthResponse, tags=["status"])
    async def health() -> HealthResponse:
        """Report that the process is running."""
        return HealthResponse(
            status="ok",
            version=__version__,
        )

    @app.get("/version", response_model=VersionResponse, tags=["status"])
    async def version() -> VersionResponse:
        """Report the application name, version and environment."""
        return VersionResponse(
            name="nova",
            version=__version__,
            environment=settings.environment,
        )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Authenticated echo channel.

        Requires ``Authorization: Bearer <token>``. The handshake is rejected
        before it is accepted when the token is missing or wrong.
        """
        await websocket_echo(websocket, settings)

    @app.websocket("/ws/commands")
    async def command_websocket_endpoint(websocket: WebSocket) -> None:
        """Authenticated structured tool-command channel."""
        await websocket_commands(websocket, settings, command_service)

    return app
