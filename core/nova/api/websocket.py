"""Authenticated WebSocket endpoint for NOVA.

Group 4B scope: an echo endpoint that proves the authenticated bidirectional
channel works. It carries no commands, invokes no tools and touches no
database. The planner, tool execution and device protocol all arrive later
and will reuse this connection.

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

from nova.api.auth import extract_bearer_token, token_is_valid
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
    provided = extract_bearer_token(
        websocket.headers.get("authorization")
    )

    if not token_is_valid(
        provided,
        settings.security.auth_token,
    ):
        # Closing before accept() rejects the handshake outright.
        logger.warning(
            "websocket_auth_failed",
            client=_client_label(websocket),
        )
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
                await websocket.close(
                    code=CLOSE_POLICY_VIOLATION
                )
                return

            await websocket.send_text(message)

    except WebSocketDisconnect:
        logger.info(
            "websocket_disconnected",
            client=_client_label(websocket),
        )


def _client_label(websocket: WebSocket) -> str:
    """Return a printable client address for logging."""
    client = websocket.client

    if client is None:
        return "unknown"

    return f"{client.host}:{client.port}"