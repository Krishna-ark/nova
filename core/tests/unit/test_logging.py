"""Tests for NOVA structured logging.

Isolation rules observed throughout this module:

* Every test writes into ``tmp_path``. The developer's real log directory
  under ``%LOCALAPPDATA%\\NOVA\\logs`` is never touched.
* Secrets used here are obvious fakes built in the test. No real
  authentication token is ever passed to a logger.
* An autouse fixture tears logging down before and after each test, so a
  handler left open cannot leak into the next test or hold a file lock.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from nova.config import LoggingSettings
from nova.utils import configure_logging, get_logger, reset_logging

# An obvious fake. Never a real credential.
FAKE_SECRET = "fake-secret-value-must-not-be-logged"


@pytest.fixture(autouse=True)
def _reset_logging_state() -> Iterator[None]:
    """Ensure each test starts and ends with logging fully torn down."""
    reset_logging()
    yield
    reset_logging()


def _read_events(path: Path) -> list[dict[str, Any]]:
    """Flush handlers and return every JSON event written to the log file."""
    for handler in logging.getLogger().handlers:
        handler.flush()
    contents = path.read_text(encoding="utf-8").strip()
    if not contents:
        return []
    return [json.loads(line) for line in contents.splitlines()]


# --- Initialisation ---


def test_configuration_initialises_successfully(tmp_path: Path) -> None:
    settings = LoggingSettings(directory=tmp_path / "logs")

    configure_logging(settings, force=True)

    assert logging.getLogger().handlers, "no handlers were installed"
    assert settings.file_path.exists(), "log file was not created"


def test_missing_directory_is_created(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "logs"
    settings = LoggingSettings(directory=target)

    configure_logging(settings, force=True)

    assert target.is_dir(), "log directory was not created"


def test_two_handlers_are_installed(tmp_path: Path) -> None:
    """One rotating file sink and one console sink."""
    settings = LoggingSettings(directory=tmp_path)

    configure_logging(settings, force=True)

    handlers = logging.getLogger().handlers
    assert len(handlers) == 2, f"expected file + console handlers, got {len(handlers)}"
    assert any(isinstance(handler, logging.handlers.RotatingFileHandler) for handler in handlers), (
        "no rotating file handler installed"
    )


# --- Emitting events ---


def test_event_can_be_emitted(tmp_path: Path) -> None:
    settings = LoggingSettings(directory=tmp_path)
    configure_logging(settings, force=True)

    get_logger("nova.test").info("server_started", port=8765)

    events = _read_events(settings.file_path)
    assert len(events) == 1, f"expected exactly one event, got {len(events)}"


def test_output_is_written_to_the_configured_file(tmp_path: Path) -> None:
    """The file must be the one named in settings, not a default location."""
    settings = LoggingSettings(directory=tmp_path / "custom", filename="app.log")
    configure_logging(settings, force=True)

    get_logger("nova.test").info("written_here")

    assert settings.file_path.exists()
    assert "written_here" in settings.file_path.read_text(encoding="utf-8")


def test_file_output_is_structured_json(tmp_path: Path) -> None:
    settings = LoggingSettings(directory=tmp_path)
    configure_logging(settings, force=True)

    get_logger("nova.test").info("db_connected", database="nova.db", attempts=2)

    event = _read_events(settings.file_path)[0]
    assert event["event"] == "db_connected"
    assert event["database"] == "nova.db"
    assert event["attempts"] == 2, "structured fields must keep their type"
    assert event["level"] == "info"
    assert event["logger"] == "nova.test"
    assert "timestamp" in event, "every event must carry a timestamp"


def test_level_filtering_is_applied(tmp_path: Path) -> None:
    settings = LoggingSettings(directory=tmp_path, level="WARNING")
    configure_logging(settings, force=True)
    log = get_logger("nova.test")

    log.debug("suppressed_debug")
    log.info("suppressed_info")
    log.warning("emitted_warning")

    events = [event["event"] for event in _read_events(settings.file_path)]
    assert events == ["emitted_warning"], f"level filtering failed, got {events}"


def test_stdlib_loggers_share_the_pipeline(tmp_path: Path) -> None:
    """Third-party libraries must land in the same JSON file."""
    settings = LoggingSettings(directory=tmp_path)
    configure_logging(settings, force=True)

    logging.getLogger("sqlalchemy.engine").warning("connection_pool_full")

    events = _read_events(settings.file_path)
    assert any(event["event"] == "connection_pool_full" for event in events), (
        "standard library log records did not reach the NOVA log file"
    )


def test_exception_information_is_captured(tmp_path: Path) -> None:
    settings = LoggingSettings(directory=tmp_path)
    configure_logging(settings, force=True)

    try:
        message = "database unreachable"
        raise RuntimeError(message)
    except RuntimeError:
        get_logger("nova.test").exception("startup_failed")

    event = _read_events(settings.file_path)[0]
    assert event["event"] == "startup_failed"
    assert "database unreachable" in str(event.get("exception", "")), "traceback was not recorded"


# --- Console modes ---


def test_console_pretty_mode_works(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = LoggingSettings(directory=tmp_path, console_pretty=True)
    configure_logging(settings, force=True)

    get_logger("nova.test").info("pretty_event", detail="value")

    captured = capsys.readouterr().out
    assert "pretty_event" in captured, "nothing reached the console"
    assert _read_events(settings.file_path)[0]["event"] == "pretty_event"


def test_console_json_mode_works(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = LoggingSettings(directory=tmp_path, console_pretty=False)
    configure_logging(settings, force=True)

    get_logger("nova.test").info("json_event", detail="value")

    captured = capsys.readouterr().out.strip()
    assert captured, "nothing reached the console"
    parsed = json.loads(captured.splitlines()[-1])
    assert parsed["event"] == "json_event", "console output was not valid JSON"


# --- Redaction ---


@pytest.mark.parametrize(
    "field",
    [
        "auth_token",
        "token",
        "password",
        "secret",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "private_key",
        "AUTH_TOKEN",
        "X-Api-Key",
    ],
)
def test_sensitive_field_is_redacted(tmp_path: Path, field: str) -> None:
    """Credential-bearing keys must never reach disk, whatever their case."""
    settings = LoggingSettings(directory=tmp_path)
    configure_logging(settings, force=True)

    get_logger("nova.test").info("auth_attempt", **{field: FAKE_SECRET})

    raw = settings.file_path.read_text(encoding="utf-8")
    assert FAKE_SECRET not in raw, f"secret leaked to disk via field {field!r}"
    assert _read_events(settings.file_path)[0][field] == "***redacted***"


def test_non_sensitive_fields_survive_redaction(tmp_path: Path) -> None:
    """Redaction must not damage ordinary fields."""
    settings = LoggingSettings(directory=tmp_path)
    configure_logging(settings, force=True)

    get_logger("nova.test").info(
        "auth_attempt",
        auth_token=FAKE_SECRET,
        client_id="safe-to-log",
        attempt=3,
    )

    event = _read_events(settings.file_path)[0]
    assert event["auth_token"] == "***redacted***"
    assert event["client_id"] == "safe-to-log"
    assert event["attempt"] == 3


def test_redaction_applies_to_console_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A secret must not appear on the console either."""
    settings = LoggingSettings(directory=tmp_path, console_pretty=True)
    configure_logging(settings, force=True)

    get_logger("nova.test").info("auth_attempt", password=FAKE_SECRET)

    assert FAKE_SECRET not in capsys.readouterr().out, "secret leaked to the console"


# --- Reset and idempotency ---


def test_reset_removes_all_handlers(tmp_path: Path) -> None:
    settings = LoggingSettings(directory=tmp_path)
    configure_logging(settings, force=True)
    assert logging.getLogger().handlers

    reset_logging()

    assert logging.getLogger().handlers == [], "handlers survived reset_logging()"


def test_reconfiguration_after_reset_succeeds(tmp_path: Path) -> None:
    settings = LoggingSettings(directory=tmp_path)
    configure_logging(settings, force=True)
    reset_logging()

    configure_logging(settings, force=True)
    get_logger("nova.test").info("after_reset")

    assert _read_events(settings.file_path)[-1]["event"] == "after_reset"


def test_repeated_configuration_does_not_duplicate_handlers(tmp_path: Path) -> None:
    """Calling configure_logging twice must not double every log line."""
    settings = LoggingSettings(directory=tmp_path)

    configure_logging(settings, force=True)
    baseline = len(logging.getLogger().handlers)
    configure_logging(settings)
    configure_logging(settings)

    assert len(logging.getLogger().handlers) == baseline, "handlers were duplicated"


def test_forced_reconfiguration_replaces_handlers(tmp_path: Path) -> None:
    """force=True must replace handlers, not append to them."""
    settings = LoggingSettings(directory=tmp_path)

    configure_logging(settings, force=True)
    baseline = len(logging.getLogger().handlers)
    configure_logging(settings, force=True)

    assert len(logging.getLogger().handlers) == baseline


def test_single_event_is_recorded_once(tmp_path: Path) -> None:
    """Guards against a duplicate-handler regression producing double writes."""
    settings = LoggingSettings(directory=tmp_path)
    configure_logging(settings, force=True)
    configure_logging(settings)

    get_logger("nova.test").info("only_once")

    events = [e for e in _read_events(settings.file_path) if e["event"] == "only_once"]
    assert len(events) == 1, f"event written {len(events)} times"


# --- Rotation ---


def test_rotation_settings_reach_the_handler(tmp_path: Path) -> None:
    """The configured limits must be applied to the standard library handler."""
    settings = LoggingSettings(directory=tmp_path, max_bytes=4096, backup_count=3)

    configure_logging(settings, force=True)

    rotating = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.handlers.RotatingFileHandler)
    ]
    assert len(rotating) == 1
    assert rotating[0].maxBytes == 4096
    assert rotating[0].backupCount == 3


def test_rotation_produces_backup_files(tmp_path: Path) -> None:
    """Rotation is driven by the standard library, with no extra dependency."""
    settings = LoggingSettings(directory=tmp_path, max_bytes=2048, backup_count=2)
    configure_logging(settings, force=True)
    log = get_logger("nova.test")

    for index in range(200):
        log.info("filler_event", index=index, payload="x" * 100)

    rotated = sorted(tmp_path.glob("nova.log.*"))
    assert rotated, "expected at least one rotated log file"
    assert len(rotated) <= settings.backup_count, (
        f"backup_count={settings.backup_count} exceeded: {len(rotated)} files"
    )


def test_backup_count_zero_keeps_no_backups(tmp_path: Path) -> None:
    settings = LoggingSettings(directory=tmp_path, max_bytes=2048, backup_count=0)
    configure_logging(settings, force=True)
    log = get_logger("nova.test")

    for index in range(200):
        log.info("filler_event", index=index, payload="x" * 100)

    assert sorted(tmp_path.glob("nova.log.*")) == [], "backups kept despite count of 0"


def test_logging_never_writes_outside_the_temporary_directory(tmp_path: Path) -> None:
    """Explicit guard: the real NOVA log directory must stay untouched."""
    settings = LoggingSettings(directory=tmp_path)
    configure_logging(settings, force=True)

    get_logger("nova.test").info("contained")

    written = [path for path in tmp_path.iterdir() if path.is_file()]
    assert written, "nothing was written to the temporary directory"
    assert all(path.parent == tmp_path for path in written)
