"""Tests for the NOVA authenticated WebSocket echo endpoint.

These tests use ``fastapi.testclient.TestClient`` rather than httpx directly,
because ``httpx.ASGITransport`` does not implement the WebSocket protocol.
TestClient drives the app in-process, so no socket is bound and no port
is used.

TestClient is synchronous, so these are plain functions rather than async
tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from nova.api import create_app, extract_bearer_token, token_is_valid
from nova.api.websocket import CLOSE_POLICY_VIOLATION, MAX_MESSAGE_LENGTH
from nova.config import MIN_TOKEN_LENGTH, Settings, get_settings

# An obvious fake, long enough to satisfy validation. Never a real secret.
FAKE_TOKEN = "test-token-" + ("x" * MIN_TOKEN_LENGTH)
WRONG_TOKEN = "wrong-token-" + ("y" * MIN_TOKEN_LENGTH)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited NOVA_* variables and clear the settings cache."""
    for name in list(os.environ):
        if name.startswith("NOVA_"):
            monkeypatch.delenv(name, raising=False)

    get_settings.cache_clear()


def _settings() -> Settings:
    """Build Settings without reading the developer's real .env."""
    os.environ["NOVA_SECURITY__AUTH_TOKEN"] = FAKE_TOKEN
    return Settings(_env_file=None)


@pytest.fixture
def app() -> FastAPI:
    return create_app(_settings())


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _auth() -> dict[str, str]:
    """Return valid authorization headers."""
    return {"Authorization": f"Bearer {FAKE_TOKEN}"}


# --- Bearer token parsing ---


def test_extract_bearer_token_reads_a_valid_header() -> None:
    assert extract_bearer_token("Bearer abc123") == "abc123"


def test_extract_bearer_scheme_is_case_insensitive() -> None:
    """RFC 7235 makes the auth scheme case-insensitive."""
    assert extract_bearer_token("bearer abc123") == "abc123"
    assert extract_bearer_token("BEARER abc123") == "abc123"


def test_extract_bearer_token_strips_surrounding_space() -> None:
    assert extract_bearer_token("Bearer   abc123  ") == "abc123"


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "abc123",
        "Basic abc123",
        "Bearer",
        "Bearer   ",
        "Token abc123",
    ],
)
def test_extract_bearer_token_rejects_bad_headers(
    header: str | None,
) -> None:
    assert extract_bearer_token(header) is None


# --- Token comparison ---


def test_token_is_valid_accepts_the_configured_token() -> None:
    assert (
        token_is_valid(
            FAKE_TOKEN,
            SecretStr(FAKE_TOKEN),
        )
        is True
    )


@pytest.mark.parametrize(
    "provided",
    [
        None,
        "",
        WRONG_TOKEN,
        FAKE_TOKEN[:-1],
        FAKE_TOKEN + "x",
    ],
)
def test_token_is_valid_rejects_anything_else(
    provided: str | None,
) -> None:
    assert (
        token_is_valid(
            provided,
            SecretStr(FAKE_TOKEN),
        )
        is False
    )


def test_token_comparison_is_case_sensitive() -> None:
    assert (
        token_is_valid(
            FAKE_TOKEN.upper(),
            SecretStr(FAKE_TOKEN),
        )
        is False
    )


# --- Authentication on the endpoint ---


def test_connection_succeeds_with_a_valid_token(
    client: TestClient,
) -> None:
    with client.websocket_connect(
        "/ws",
        headers=_auth(),
    ) as websocket:
        websocket.send_text("hello")

        assert websocket.receive_text() == "hello"


def test_connection_is_rejected_without_a_token(
    client: TestClient,
) -> None:
    """An unauthenticated client must never reach an accepted socket."""
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/ws"),
    ):
        pass

    assert exc_info.value.code == CLOSE_POLICY_VIOLATION


def test_connection_is_rejected_with_a_wrong_token(
    client: TestClient,
) -> None:
    headers = {"Authorization": f"Bearer {WRONG_TOKEN}"}

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/ws", headers=headers),
    ):
        pass


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        f"Basic {FAKE_TOKEN}",
        f"Token {FAKE_TOKEN}",
        FAKE_TOKEN,
    ],
)
def test_connection_is_rejected_with_a_malformed_header(
    client: TestClient,
    header: str,
) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/ws",
            headers={"Authorization": header},
        ),
    ):
        pass


def test_token_in_query_string_is_not_accepted(
    client: TestClient,
) -> None:
    """Query parameters end up in logs, so they are not an auth channel."""
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            f"/ws?token={FAKE_TOKEN}",
        ),
    ):
        pass


# --- Echo behaviour ---


def test_message_is_echoed_verbatim(client: TestClient) -> None:
    with client.websocket_connect(
        "/ws",
        headers=_auth(),
    ) as websocket:
        websocket.send_text("Chrome kholo")

        assert websocket.receive_text() == "Chrome kholo"


def test_multiple_messages_are_echoed_in_order(
    client: TestClient,
) -> None:
    with client.websocket_connect(
        "/ws",
        headers=_auth(),
    ) as websocket:
        for index in range(5):
            websocket.send_text(f"message-{index}")

        received = [websocket.receive_text() for _ in range(5)]

    assert received == [f"message-{index}" for index in range(5)]


def test_empty_message_is_echoed(client: TestClient) -> None:
    with client.websocket_connect(
        "/ws",
        headers=_auth(),
    ) as websocket:
        websocket.send_text("")

        assert websocket.receive_text() == ""


def test_json_payload_is_echoed_unchanged(
    client: TestClient,
) -> None:
    """The echo is byte-for-byte; it does not parse or reformat."""
    payload = '{"tool": "open_application", "args": {"app_name": "chrome"}}'

    with client.websocket_connect(
        "/ws",
        headers=_auth(),
    ) as websocket:
        websocket.send_text(payload)

        assert websocket.receive_text() == payload


def test_oversized_message_closes_the_connection(
    client: TestClient,
) -> None:
    """A bound is required so one frame cannot exhaust memory."""
    oversized = "x" * (MAX_MESSAGE_LENGTH + 1)

    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/ws",
            headers=_auth(),
        ) as websocket,
    ):
        websocket.send_text(oversized)
        websocket.receive_text()


def test_message_at_the_size_limit_is_accepted(
    client: TestClient,
) -> None:
    """Boundary check: the limit itself must pass."""
    at_limit = "x" * MAX_MESSAGE_LENGTH

    with client.websocket_connect(
        "/ws",
        headers=_auth(),
    ) as websocket:
        websocket.send_text(at_limit)

        assert websocket.receive_text() == at_limit


def test_client_disconnect_is_handled_cleanly(
    client: TestClient,
) -> None:
    """Closing from the client must not raise on the server."""
    with client.websocket_connect(
        "/ws",
        headers=_auth(),
    ) as websocket:
        websocket.send_text("bye")
        websocket.receive_text()

    with client.websocket_connect(
        "/ws",
        headers=_auth(),
    ) as websocket:
        websocket.send_text("still working")

        assert websocket.receive_text() == "still working"


# --- Route registration ---


def test_websocket_route_is_registered(
    app: FastAPI,
) -> None:
    paths = {route.path for route in app.routes if hasattr(route, "path")}

    assert "/ws" in paths


def test_status_endpoints_still_work(
    client: TestClient,
) -> None:
    """Group 4A behaviour must be unchanged."""
    assert client.get("/health").status_code == 200
    assert client.get("/version").status_code == 200
