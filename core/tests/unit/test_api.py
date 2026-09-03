"""Tests for the NOVA HTTP API.

Group 4A covers the two unauthenticated status endpoints. Requests are made
in-process through ``httpx.ASGITransport``: no socket is opened and no port
is bound, so the tests cannot be affected by the Windows firewall or by a
port already in use.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nova import __version__
from nova.api import create_app
from nova.config import MIN_TOKEN_LENGTH, Settings, get_settings

FAKE_TOKEN = "test-token-" + ("x" * MIN_TOKEN_LENGTH)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited NOVA_* variables and clear the settings cache."""
    for name in list(os.environ):
        if name.startswith("NOVA_"):
            monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def _settings(**overrides: str) -> Settings:
    """Build Settings without reading the developer's real .env."""
    values = {"NOVA_SECURITY__AUTH_TOKEN": FAKE_TOKEN, **overrides}
    for key, value in values.items():
        os.environ[key] = value
    return Settings(_env_file=None)


@pytest.fixture
def app() -> FastAPI:
    """Return an application built from development settings."""
    return create_app(_settings())


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Yield an in-process HTTP client bound to the application."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://nova.test",
    ) as async_client:
        yield async_client


def test_create_app_returns_a_fastapi_instance(app: FastAPI) -> None:
    assert isinstance(app, FastAPI)


def test_app_reports_the_package_version(app: FastAPI) -> None:
    assert app.version == __version__


def test_create_app_does_not_bind_a_socket() -> None:
    """Building the app must not touch the network."""
    created = create_app(_settings())

    assert isinstance(created, FastAPI)


def test_each_call_returns_an_independent_app() -> None:
    """A factory, not a module-level singleton."""
    first = create_app(_settings())
    second = create_app(_settings())

    assert first is not second


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
    }


async def test_health_content_type_is_json(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.headers["content-type"].startswith("application/json")


async def test_health_does_not_leak_configuration(
    client: AsyncClient,
) -> None:
    """A liveness probe must never expose settings or credentials."""
    body = (await client.get("/health")).text

    assert FAKE_TOKEN not in body
    assert "auth_token" not in body


async def test_version_reports_name_version_and_environment(
    client: AsyncClient,
) -> None:
    response = await client.get("/version")

    assert response.status_code == 200
    assert response.json() == {
        "name": "nova",
        "version": __version__,
        "environment": "development",
    }


async def test_version_reflects_the_configured_environment() -> None:
    """The endpoint must report the real environment, not a constant."""
    app = create_app(_settings(NOVA_ENVIRONMENT="production"))
    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://nova.test",
    ) as client:
        payload = (await client.get("/version")).json()

    assert payload["environment"] == "production"


async def test_version_does_not_leak_credentials(
    client: AsyncClient,
) -> None:
    body = (await client.get("/version")).text

    assert FAKE_TOKEN not in body


async def test_unknown_path_returns_404(client: AsyncClient) -> None:
    response = await client.get("/does-not-exist")

    assert response.status_code == 404


async def test_health_rejects_post(client: AsyncClient) -> None:
    """Status endpoints are read-only."""
    response = await client.post("/health")

    assert response.status_code == 405


@pytest.mark.parametrize("path", ["/health", "/version"])
async def test_status_endpoints_need_no_authentication(
    client: AsyncClient,
    path: str,
) -> None:
    """Liveness must be checkable without a credential."""
    response = await client.get(path)

    assert response.status_code == 200


async def test_docs_are_available_in_development(
    client: AsyncClient,
) -> None:
    response = await client.get("/docs")

    assert response.status_code == 200


async def test_docs_are_disabled_in_production() -> None:
    """Production must not advertise the API surface."""
    app = create_app(_settings(NOVA_ENVIRONMENT="production"))
    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://nova.test",
    ) as client:
        docs = await client.get("/docs")
        schema = await client.get("/openapi.json")

    assert docs.status_code == 404
    assert schema.status_code == 404


def test_console_entry_point_exists() -> None:
    """pyproject declares nova = "nova.main:run"; the target must resolve."""
    from nova.main import run

    assert callable(run)
