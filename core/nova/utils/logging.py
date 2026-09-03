"""Structured logging for NOVA."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from structlog.typing import EventDict, Processor, WrappedLogger

    from nova.config.settings import LoggingSettings


_SENSITIVE_KEY_FRAGMENTS = (
    "token",
    "password",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
)

_REDACTED = "***redacted***"

_configured = False


def _redact_sensitive(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Replace values of credential-bearing keys with a redacted value."""
    for key in list(event_dict):
        lowered = key.lower().replace("-", "_")
        if any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS):
            event_dict[key] = _REDACTED
    return event_dict


def _shared_processors() -> list[Processor]:
    """Return processors shared by all logging sinks."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _redact_sensitive,
    ]


def configure_logging(
    settings: LoggingSettings,
    *,
    force: bool = False,
) -> None:
    """Configure NOVA logging."""
    global _configured

    if _configured and not force:
        return

    settings.directory.mkdir(parents=True, exist_ok=True)

    structlog.configure(
        processors=[
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
    )

    file_handler = logging.handlers.RotatingFileHandler(
        filename=settings.file_path,
        maxBytes=settings.max_bytes,
        backupCount=settings.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)

    console_renderer: Processor = (
        structlog.dev.ConsoleRenderer(colors=False)
        if settings.console_pretty
        else structlog.processors.JSONRenderer()
    )

    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            console_renderer,
        ],
    )

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(console_formatter)

    root = logging.getLogger()

    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()

    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.setLevel(settings.level)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        noisy = logging.getLogger(name)
        noisy.handlers.clear()
        noisy.propagate = True

    _configured = True


def get_logger(name: str | None = None) -> Any:
    """Return a NOVA structlog logger."""
    return structlog.stdlib.get_logger(name)


def reset_logging() -> None:
    """Tear down logging configuration. Intended for tests."""
    global _configured

    root = logging.getLogger()

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    structlog.reset_defaults()
    _configured = False
