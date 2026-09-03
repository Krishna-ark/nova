"""Application configuration for NOVA.

Settings come from environment variables and an optional ``.env`` file.
There is deliberately no YAML here: Phase 1 configuration is a flat set of
scalars, and ``pydantic-settings`` reads ``.env`` natively.

YAML is reserved for data (tool manifests, language lexicons, response
templates) which arrives from Milestone 2 onward, loaded by the subsystem
that owns it rather than by this module.

Security boundaries enforced here:

* ``security.auth_token`` has no default in any environment.
* Binding to a non-loopback address is refused unless explicitly permitted.
* Phase 1 is localhost-only by default.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

MIN_TOKEN_LENGTH = 32


def default_data_dir() -> Path:
    """Return the per-user directory where NOVA keeps runtime state."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "NOVA"
    return Path.home() / ".local" / "share" / "nova"


class ServerSettings(BaseModel):
    """HTTP/WebSocket server binding."""

    model_config = {"extra": "forbid"}

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    allow_non_local: bool = False

    @model_validator(mode="after")
    def _refuse_non_loopback_by_default(self) -> ServerSettings:
        if self.host not in _LOOPBACK_HOSTS and not self.allow_non_local:
            msg = (
                f"Refusing to bind to non-loopback host {self.host!r}. "
                "Phase 1 is localhost-only. If you genuinely intend to expose "
                "NOVA on the network, set "
                "NOVA_SERVER__ALLOW_NON_LOCAL=true."
            )
            raise ValueError(msg)
        return self


class DatabaseSettings(BaseModel):
    """SQLite storage location and connection behaviour."""

    model_config = {"extra": "forbid"}

    path: Path = Field(default_factory=lambda: default_data_dir() / "nova.db")
    enable_wal: bool = True
    busy_timeout_seconds: float = Field(default=5.0, gt=0)

    @property
    def url(self) -> str:
        """Return the async SQLAlchemy URL."""
        return f"sqlite+aiosqlite:///{self.path.as_posix()}"


class LoggingSettings(BaseModel):
    """Structured logging configuration."""

    model_config = {"extra": "forbid"}

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    directory: Path = Field(default_factory=lambda: default_data_dir() / "logs")
    filename: str = "nova.log"
    console_pretty: bool = True
    max_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    backup_count: int = Field(default=5, ge=0)

    @property
    def file_path(self) -> Path:
        """Return the full path to the active log file."""
        return self.directory / self.filename


class SecuritySettings(BaseModel):
    """Credentials and security-relevant toggles."""

    model_config = {"extra": "forbid"}

    auth_token: SecretStr

    @field_validator("auth_token")
    @classmethod
    def _reject_weak_tokens(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if len(token) < MIN_TOKEN_LENGTH:
            msg = (
                f"auth_token must be at least {MIN_TOKEN_LENGTH} characters "
                f"(got {len(token)}). Generate one with: "
                'python -c "import secrets; '
                'print(secrets.token_urlsafe(32))"'
            )
            raise ValueError(msg)
        return value


class Settings(BaseSettings):
    """Root configuration object, validated once at startup."""

    model_config = SettingsConfigDict(
        env_prefix="NOVA_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="forbid",
        validate_default=True,
    )

    environment: Literal["development", "production"] = "development"

    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    security: SecuritySettings

    @model_validator(mode="before")
    @classmethod
    def _require_auth_token(cls, data: Any) -> Any:
        """Give an actionable message when the auth token is absent."""
        if isinstance(data, dict) and not data.get("security"):
            msg = (
                "NOVA_SECURITY__AUTH_TOKEN is not set. NOVA has no default "
                "credential in any environment. Generate one with:\n"
                '  python -c "import secrets; '
                'print(secrets.token_urlsafe(32))"\n'
                "then add it to your .env file as:\n"
                "  NOVA_SECURITY__AUTH_TOKEN=<generated value>"
            )
            raise ValueError(msg)
        return data

    @model_validator(mode="after")
    def _reject_unknown_env_vars(self) -> Settings:
        """Fail loudly on an unknown NOVA environment variable."""
        unknown = sorted(
            name
            for name in os.environ
            if name.startswith("NOVA_") and name.upper() not in self.known_env_vars()
        )

        if unknown:
            msg = (
                f"Unrecognised NOVA environment variable(s): "
                f"{', '.join(unknown)}. Check for a typo."
            )
            raise ValueError(msg)

        return self

    @classmethod
    def known_env_vars(cls) -> set[str]:
        """Return every environment variable name accepted by NOVA."""
        prefix = "NOVA_"
        names: set[str] = set()

        for field_name, field in cls.model_fields.items():
            names.add(f"{prefix}{field_name}".upper())
            annotation = field.annotation

            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                for sub_name in annotation.model_fields:
                    names.add(f"{prefix}{field_name}__{sub_name}".upper())

        return names

    @property
    def is_production(self) -> bool:
        """Return True when running with production settings."""
        return self.environment == "production"

    def ensure_directories(self) -> None:
        """Create directories NOVA needs at startup."""
        self.database.path.parent.mkdir(parents=True, exist_ok=True)
        self.logging.directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()


def generate_auth_token() -> str:
    """Return a cryptographically secure authentication token."""
    return secrets.token_urlsafe(32)
