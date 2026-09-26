"""Authenticated WebSocket endpoints for NOVA.

The echo WebSocket authenticates clients before echoing text frames. The
structured command WebSocket authenticates clients before accepting tool
commands, routes ``tool.execute`` requests through the command service, and
serves the registered tool catalog for ``tools.list`` requests. Catalog
discovery returns metadata without executing a tool.

Authentication happens **before** the handshake is accepted. Accepting first
and closing afterwards would briefly grant an unauthenticated client an open
socket, and some clients treat that as success.

Known limitation, relevant to the desktop UI milestone: browsers cannot set
an ``Authorization`` header on a WebSocket. The browser client will need a
different mechanism, most likely the ``Sec-WebSocket-Protocol`` handshake.
That is deliberately not built now, because no browser client exists yet and
guessing its shape would be speculative. Passing the token in the query
string is rejected as a design: URLs end up in logs and process listings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from nova.api.auth import extract_bearer_token, token_is_valid
from nova.api.commands import CommandService, ToolCommand, parse_command
from nova.tools.executor import ToolExecutionError
from nova.utils.logging import get_logger

if TYPE_CHECKING:
    from nova.config.settings import Settings


logger = get_logger(__name__)

# RFC 6455 close codes.
CLOSE_POLICY_VIOLATION = 1008

# Longest message the echo endpoint will accept, in characters. A bound is
# required so a client cannot exhaust memory with a single frame.
MAX_MESSAGE_LENGTH = 64 * 1024


async def websocket_echo(websocket: WebSocket, settings: Settings) -> None:
    """Authenticate the client, then echo text frames back to it.

    Args:
        websocket: The incoming connection.
        settings: Validated application configuration.
    """
    if not _authenticate(websocket, settings):
        # Closing before accept() rejects the handshake outright.
        await websocket.close(code=CLOSE_POLICY_VIOLATION)
        return

    await websocket.accept()

    logger.info(
        "websocket_connected",
        client=_client_label(websocket),
    )

    try:
        while True:
            message = await websocket.receive_text()

            if len(message) > MAX_MESSAGE_LENGTH:
                logger.warning(
                    "websocket_message_too_large",
                    client=_client_label(websocket),
                    length=len(message),
                )
                await websocket.close(code=CLOSE_POLICY_VIOLATION)
                return

            await websocket.send_text(message)

    except WebSocketDisconnect:
        logger.info(
            "websocket_disconnected",
            client=_client_label(websocket),
        )


async def websocket_commands(
    websocket: WebSocket,
    settings: Settings,
    service: CommandService,
) -> None:
    """Authenticate and execute structured tool commands."""
    if not _authenticate(websocket, settings):
        await websocket.close(code=CLOSE_POLICY_VIOLATION)
        return

    await websocket.accept()

    try:
        while True:
            message = await websocket.receive_text()

            if len(message) > MAX_MESSAGE_LENGTH:
                await websocket.close(code=CLOSE_POLICY_VIOLATION)
                return

            try:
                command = parse_command(message)
            except (ValidationError, ValueError):
                await websocket.send_json(
                    {
                        "type": "tool.error",
                        "request_id": None,
                        "code": "invalid_command",
                        "detail": "Invalid tool command.",
                    }
                )
                continue

            if not isinstance(command, ToolCommand):
                await websocket.send_json(
                    {
                        "type": "tools.list.result",
                        "request_id": command.request_id,
                        "tools": [
                            metadata.model_dump(mode="json") for metadata in service.list_tools()
                        ],
                    }
                )
                continue

            try:
                outcome = await service.execute(command)
            except ToolExecutionError as exc:
                await websocket.send_json(
                    {
                        "type": "tool.error",
                        "request_id": command.request_id,
                        "code": "tool_execution_failed",
                        "detail": str(exc),
                    }
                )
                continue
            except Exception:
                logger.exception("websocket_command_failed")
                await websocket.send_json(
                    {
                        "type": "tool.error",
                        "request_id": command.request_id,
                        "code": "internal_error",
                        "detail": "The command could not be completed.",
                    }
                )
                continue

            await websocket.send_json(
                {
                    "type": "tool.result",
                    "request_id": command.request_id,
                    "ok": outcome.ok,
                    "detail": outcome.detail,
                    "data": outcome.data,
                }
            )

    except WebSocketDisconnect:
        logger.info(
            "websocket_disconnected",
            client=_client_label(websocket),
        )


def _authenticate(websocket: WebSocket, settings: Settings) -> bool:
    """Validate the configured bearer token before accepting a socket."""
    provided = extract_bearer_token(websocket.headers.get("authorization"))

    if token_is_valid(provided, settings.security.auth_token):
        return True

    logger.warning(
        "websocket_auth_failed",
        client=_client_label(websocket),
    )
    return False


def _client_label(websocket: WebSocket) -> str:
    """Return a printable client address for logging."""
    client = websocket.client

    if client is None:
        return "unknown"

    return f"{client.host}:{client.port}"
