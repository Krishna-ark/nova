"""Tests for NOVA configuration loading and validation.

Isolation rules observed throughout this module:

* Every test either passes ``_env_file=None`` or points at a temporary
  ``.env`` under ``tmp_path``. The developer's real ``.env`` is never read
  and never written.
* An autouse fixture strips inherited ``NOVA_*`` variables so a value in the
  developer's shell cannot change an assertion.
* No test prints or asserts on a real credential. Tokens used here are
  obvious fakes constructed in the test itself.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from nova.config import (
    MIN_TOKEN_LENGTH,
    DatabaseSettings,
    LoggingSettings,
    SecuritySettings,
    ServerSettings,
    Settings,
    default_data_dir,
    generate_auth_token,
    get_settings,
)

# An obvious fake, long enough to satisfy validation. Never a real secret.
FAKE_TOKEN = "test-token-" + ("x" * MIN_TOKEN_LENGTH)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited NOVA_* variables and clear the settings cache.

    Without this, a variable exported in the developer's shell, or a value
    cached by an earlier test, would silently change the result.
    """
    for name in list(os.environ):
        if name.startswith("NOVA_"):
            monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def _write_env(directory: Path, **values: str) -> Path:
    """Write a temporary .env file and return its path."""
    env_file = directory / ".env"
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    return env_file


# --- Valid configuration ---


def test_default_configuration_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the auth token is required; everything else has a usable default."""
    monkeypatch.setenv("NOVA_SECURITY__AUTH_TOKEN", FAKE_TOKEN)

    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.server.host == "127.0.0.1", "must default to loopback only"
    assert settings.server.port == 8765
    assert settings.server.allow_non_local is False, "network exposure must be opt-in"
    assert settings.logging.level == "INFO"
    assert settings.logging.console_pretty is True
    assert settings.database.enable_wal is True, "WAL is required by the architecture"
    assert settings.is_production is False


def test_valid_auth_token_is_accepted() -> None:
    security = SecuritySettings(auth_token=FAKE_TOKEN)

    assert security.auth_token.get_secret_value() == FAKE_TOKEN


def test_secret_is_masked_in_representations(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token must never leak through repr(), str() or model dumps."""
    monkeypatch.setenv("NOVA_SECURITY__AUTH_TOKEN", FAKE_TOKEN)

    settings = Settings(_env_file=None)

    assert FAKE_TOKEN not in repr(settings), "token leaked via repr()"
    assert FAKE_TOKEN not in str(settings), "token leaked via str()"
    assert FAKE_TOKEN not in str(settings.model_dump()), "token leaked via model_dump()"
    assert settings.security.auth_token.get_secret_value() == FAKE_TOKEN


# --- Environment variable loading ---


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_SECURITY__AUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("NOVA_ENVIRONMENT", "production")
    monkeypatch.setenv("NOVA_SERVER__PORT", "9100")
    monkeypatch.setenv("NOVA_LOGGING__LEVEL", "DEBUG")
    monkeypatch.setenv("NOVA_DATABASE__ENABLE_WAL", "false")

    settings = Settings(_env_file=None)

    assert settings.environment == "production"
    assert settings.server.port == 9100
    assert settings.logging.level == "DEBUG"
    assert settings.database.enable_wal is False
    assert settings.is_production is True


def test_nested_delimiter_maps_to_subgroup(monkeypatch: pytest.MonkeyPatch) -> None:
    """NOVA_<GROUP>__<FIELD> must reach the nested model."""
    monkeypatch.setenv("NOVA_SECURITY__AUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("NOVA_LOGGING__BACKUP_COUNT", "9")

    settings = Settings(_env_file=None)

    assert settings.logging.backup_count == 9


# --- .env file loading ---


def test_env_file_is_read(tmp_path: Path) -> None:
    env_file = _write_env(
        tmp_path,
        NOVA_SECURITY__AUTH_TOKEN=FAKE_TOKEN,
        NOVA_SERVER__PORT="9200",
        NOVA_LOGGING__LEVEL="WARNING",
    )

    settings = Settings(_env_file=env_file)

    assert settings.server.port == 9200
    assert settings.logging.level == "WARNING"


def test_environment_variable_wins_over_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit environment must take precedence over the file."""
    env_file = _write_env(
        tmp_path,
        NOVA_SECURITY__AUTH_TOKEN=FAKE_TOKEN,
        NOVA_SERVER__PORT="9200",
    )
    monkeypatch.setenv("NOVA_SERVER__PORT", "9300")

    settings = Settings(_env_file=env_file)

    assert settings.server.port == 9300, "environment must outrank the .env file"


def test_empty_token_in_env_file_is_rejected(tmp_path: Path) -> None:
    """Copying .env.example unchanged must fail, not yield a weak credential."""
    env_file = _write_env(tmp_path, NOVA_SECURITY__AUTH_TOKEN="")

    with pytest.raises(ValidationError, match="at least"):
        Settings(_env_file=env_file)


# --- Invalid configuration ---


def test_missing_auth_token_fails_with_actionable_error() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    message = str(exc_info.value)
    assert "NOVA_SECURITY__AUTH_TOKEN" in message, "error must name the variable"
    assert "secrets.token_urlsafe" in message, "error must show how to generate one"


@pytest.mark.parametrize("length", [0, 1, MIN_TOKEN_LENGTH - 1])
def test_short_auth_token_is_rejected(length: int) -> None:
    with pytest.raises(ValidationError, match="at least"):
        SecuritySettings(auth_token="a" * length)


def test_token_at_exact_minimum_length_is_accepted() -> None:
    """Boundary check: the minimum length itself must pass."""
    security = SecuritySettings(auth_token="a" * MIN_TOKEN_LENGTH)

    assert len(security.auth_token.get_secret_value()) == MIN_TOKEN_LENGTH


@pytest.mark.parametrize("port", [0, 80, 443, 1023, 65536, 99999])
def test_invalid_port_is_rejected(port: int) -> None:
    with pytest.raises(ValidationError):
        ServerSettings(port=port)


@pytest.mark.parametrize("port", [1024, 8765, 65535])
def test_valid_port_is_accepted(port: int) -> None:
    assert ServerSettings(port=port).port == port


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LoggingSettings(level="TRACE")


def test_invalid_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_SECURITY__AUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("NOVA_ENVIRONMENT", "staging")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_unknown_nova_env_var_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must fail loudly instead of being silently discarded."""
    monkeypatch.setenv("NOVA_SECURITY__AUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("NOVA_SERVR__PORT", "9000")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    assert "NOVA_SERVR__PORT" in str(exc_info.value), "error must name the bad variable"


def test_known_env_vars_covers_every_field() -> None:
    """The typo detector must recognise every legitimate variable name."""
    known = Settings.known_env_vars()

    for expected in (
        "NOVA_ENVIRONMENT",
        "NOVA_SECURITY__AUTH_TOKEN",
        "NOVA_SERVER__HOST",
        "NOVA_SERVER__PORT",
        "NOVA_SERVER__ALLOW_NON_LOCAL",
        "NOVA_DATABASE__PATH",
        "NOVA_DATABASE__ENABLE_WAL",
        "NOVA_LOGGING__LEVEL",
        "NOVA_LOGGING__DIRECTORY",
        "NOVA_LOGGING__MAX_BYTES",
    ):
        assert expected in known, f"{expected} missing from known_env_vars()"


# --- Security boundary: network binding ---


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "10.0.0.5"])  # noqa: S104
def test_non_loopback_host_is_refused_by_default(host: str) -> None:
    with pytest.raises(ValidationError, match="non-loopback"):
        ServerSettings(host=host)


def test_non_loopback_error_mentions_the_override() -> None:
    """The refusal must tell the user the supported way to proceed."""
    with pytest.raises(ValidationError) as exc_info:
        ServerSettings(host="0.0.0.0")  # noqa: S104

    assert "NOVA_SERVER__ALLOW_NON_LOCAL" in str(exc_info.value)


def test_non_loopback_host_allowed_when_explicitly_permitted() -> None:
    server = ServerSettings(host="0.0.0.0", allow_non_local=True)  # noqa: S104

    assert server.host == "0.0.0.0"  # noqa: S104
    assert server.allow_non_local is True


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts_are_always_allowed(host: str) -> None:
    assert ServerSettings(host=host).host == host


def test_non_local_binding_requires_flag_via_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary must hold when configured through the environment too."""
    monkeypatch.setenv("NOVA_SECURITY__AUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("NOVA_SERVER__HOST", "0.0.0.0")  # noqa: S104

    with pytest.raises(ValidationError, match="non-loopback"):
        Settings(_env_file=None)

    monkeypatch.setenv("NOVA_SERVER__ALLOW_NON_LOCAL", "true")
    assert Settings(_env_file=None).server.host == "0.0.0.0"  # noqa: S104


# --- Derived values and helpers ---


def test_default_data_dir_uses_localappdata_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On Windows the data directory must live under %LOCALAPPDATA%."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert default_data_dir() == tmp_path / "NOVA"


def test_default_data_dir_falls_back_without_localappdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platforms without LOCALAPPDATA use an XDG-style location."""
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    result = default_data_dir()

    assert result.is_absolute()
    assert result.name == "nova"
    assert result.parent.name == "share"


def test_default_data_dir_is_absolute() -> None:
    assert default_data_dir().is_absolute()


def test_database_url_is_async_sqlite(tmp_path: Path) -> None:
    database = DatabaseSettings(path=tmp_path / "nova.db")

    assert database.url.startswith("sqlite+aiosqlite:///")
    assert database.url.endswith("nova.db")
    assert "\\" not in database.url, "URL must use forward slashes on Windows"


def test_log_file_path_is_composed(tmp_path: Path) -> None:
    logging_settings = LoggingSettings(directory=tmp_path, filename="custom.log")

    assert logging_settings.file_path == tmp_path / "custom.log"


def test_generate_auth_token_is_long_enough_and_unique() -> None:
    token = generate_auth_token()

    assert len(token) >= MIN_TOKEN_LENGTH, "generated token must pass validation"
    assert token != generate_auth_token(), "tokens must not repeat"


def test_generated_token_passes_validation() -> None:
    """The helper and the validator must agree."""
    security = SecuritySettings(auth_token=generate_auth_token())

    assert security.auth_token.get_secret_value()


# --- Directory creation ---


def test_ensure_directories_creates_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_SECURITY__AUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("NOVA_DATABASE__PATH", str(tmp_path / "db" / "nova.db"))
    monkeypatch.setenv("NOVA_LOGGING__DIRECTORY", str(tmp_path / "logs"))
    settings = Settings(_env_file=None)

    settings.ensure_directories()

    assert (tmp_path / "db").is_dir(), "database parent directory not created"
    assert (tmp_path / "logs").is_dir(), "log directory not created"


def test_ensure_directories_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_SECURITY__AUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("NOVA_DATABASE__PATH", str(tmp_path / "db" / "nova.db"))
    monkeypatch.setenv("NOVA_LOGGING__DIRECTORY", str(tmp_path / "logs"))
    settings = Settings(_env_file=None)

    settings.ensure_directories()
    settings.ensure_directories()

    assert (tmp_path / "logs").is_dir()


def test_constructing_settings_creates_no_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation must be free of filesystem side effects."""
    target = tmp_path / "never-created"
    monkeypatch.setenv("NOVA_SECURITY__AUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("NOVA_LOGGING__DIRECTORY", str(target))

    Settings(_env_file=None)

    assert not target.exists(), "constructing Settings must not touch the filesystem"


# --- Caching ---


def test_get_settings_returns_cached_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here, so the real one is never read
    monkeypatch.setenv("NOVA_SECURITY__AUTH_TOKEN", FAKE_TOKEN)

    assert get_settings() is get_settings(), "settings must be parsed once"


def test_cache_clear_allows_reconfiguration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests must be able to reset the cache between configurations."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOVA_SECURITY__AUTH_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("NOVA_SERVER__PORT", "9400")
    first = get_settings()

    get_settings.cache_clear()
    monkeypatch.setenv("NOVA_SERVER__PORT", "9500")
    second = get_settings()

    assert first.server.port == 9400
    assert second.server.port == 9500
    assert first is not second
