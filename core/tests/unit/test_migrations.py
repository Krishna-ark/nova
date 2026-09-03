"""Tests for the NOVA Alembic migration infrastructure.

Isolation rules observed throughout this module:

* Every migration runs against a database under ``tmp_path``. The URL is
  injected through ``config.attributes``, which ``env.py`` consults before
  application settings, so the developer's real ``nova.db`` is unreachable
  from these tests.
* These tests are deliberately synchronous. ``env.py`` calls ``asyncio.run``
  for online migrations, which raises if invoked from inside a running event
  loop. Driving Alembic from a sync test exercises the real code path a
  developer uses on the command line.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

import nova
from nova.storage.models import Base

EXPECTED_TABLES = {"preferences", "devices", "audit_events"}
EXPECTED_TABLES_AFTER_UPGRADE = EXPECTED_TABLES | {"alembic_version"}
INITIAL_REVISION = "0001"

MIGRATIONS_DIR = Path(nova.__file__).parent / "storage" / "migrations"
REPO_ROOT = Path(nova.__file__).parents[2]


def _config(database_path: Path) -> Config:
    """Return an Alembic config targeting a temporary database."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["sqlalchemy.url"] = (
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )
    return config


def _table_names(database_path: Path) -> set[str]:
    """Return the tables present in a SQLite file, excluding internals."""
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            names = set(inspect(connection).get_table_names())
    finally:
        engine.dispose()

    return {name for name in names if not name.startswith("sqlite_")}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_alembic_ini_exists_at_repository_root() -> None:
    assert (REPO_ROOT / "alembic.ini").is_file()


def test_alembic_ini_loads_and_points_at_the_migrations_package() -> None:
    config = Config(str(REPO_ROOT / "alembic.ini"))

    script_location = config.get_main_option("script_location")

    assert script_location == "core/nova/storage/migrations"


def test_alembic_ini_contains_no_database_url() -> None:
    """A real database path must never be committed to source control."""
    contents = (REPO_ROOT / "alembic.ini").read_text(encoding="utf-8")

    assert "sqlalchemy.url =" not in contents


def test_migration_environment_files_exist() -> None:
    assert (MIGRATIONS_DIR / "env.py").is_file()
    assert (MIGRATIONS_DIR / "script.py.mako").is_file()
    assert (MIGRATIONS_DIR / "versions").is_dir()


def test_env_uses_the_orm_metadata_as_the_single_source() -> None:
    """A second handwritten schema definition would drift from the ORM."""
    env_source = (MIGRATIONS_DIR / "env.py").read_text(encoding="utf-8")

    assert "from nova.storage.models import Base" in env_source
    assert "target_metadata = Base.metadata" in env_source


def test_script_directory_reports_the_initial_revision() -> None:
    script = ScriptDirectory(str(MIGRATIONS_DIR))

    assert script.get_current_head() == INITIAL_REVISION


def test_initial_revision_has_no_predecessor() -> None:
    script = ScriptDirectory(str(MIGRATIONS_DIR))

    revision = script.get_revision(INITIAL_REVISION)

    assert revision.down_revision is None


def test_there_is_exactly_one_migration() -> None:
    """Group 3D adds one migration; extras would mean unreviewed churn."""
    script = ScriptDirectory(str(MIGRATIONS_DIR))

    assert len(list(script.walk_revisions())) == 1


# ---------------------------------------------------------------------------
# Upgrade and downgrade
# ---------------------------------------------------------------------------


def test_upgrade_creates_exactly_the_expected_tables(tmp_path: Path) -> None:
    """The migrated database must contain exactly the four expected tables.

    An exact comparison, not a superset check: a stray table created by an
    unreviewed migration would pass ``>=`` unnoticed.
    """
    database_path = tmp_path / "nova.db"

    command.upgrade(_config(database_path), "head")

    assert _table_names(database_path) == EXPECTED_TABLES_AFTER_UPGRADE


def test_upgrade_creates_each_nova_table(tmp_path: Path) -> None:
    """Names each table individually so a failure says which one is missing."""
    database_path = tmp_path / "nova.db"

    command.upgrade(_config(database_path), "head")

    tables = _table_names(database_path)
    for expected in sorted(EXPECTED_TABLES):
        assert expected in tables, f"migration did not create {expected!r}"


def test_downgrade_removes_the_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "nova.db"
    config = _config(database_path)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    assert _table_names(database_path) == {"alembic_version"}, (
        "downgrade must leave only Alembic's revision marker"
    )


def test_upgrade_downgrade_upgrade_round_trip(tmp_path: Path) -> None:
    """The cycle must be repeatable, not one-way."""
    database_path = tmp_path / "nova.db"
    config = _config(database_path)

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    assert _table_names(database_path) == EXPECTED_TABLES_AFTER_UPGRADE


def test_upgrade_records_the_revision(tmp_path: Path) -> None:
    database_path = tmp_path / "nova.db"

    command.upgrade(_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            revision = MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()

    assert revision == INITIAL_REVISION


# ---------------------------------------------------------------------------
# Schema fidelity
# ---------------------------------------------------------------------------


def test_migrated_schema_matches_the_orm_metadata(tmp_path: Path) -> None:
    """The autogenerate discipline check.

    After the initial migration is applied, comparing the live schema against
    the ORM metadata must report no differences. A non-empty diff means the
    migration and the models have drifted apart.
    """
    database_path = tmp_path / "nova.db"

    command.upgrade(_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "target_metadata": Base.metadata,
                },
            )
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert differences == [], f"schema drift detected: {differences}"


def test_foreign_key_is_preserved_with_restrict(tmp_path: Path) -> None:
    database_path = tmp_path / "nova.db"

    command.upgrade(_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            foreign_keys = inspect(connection).get_foreign_keys("audit_events")
    finally:
        engine.dispose()

    assert len(foreign_keys) == 1
    assert foreign_keys[0]["referred_table"] == "devices"
    assert foreign_keys[0]["constrained_columns"] == ["device_id"]
    assert foreign_keys[0]["options"].get("ondelete") == "RESTRICT"


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("devices", ["name"]),
        ("devices", ["public_key"]),
        ("audit_events", ["hash"]),
    ],
)
def test_unique_constraints_are_preserved(
    tmp_path: Path,
    table: str,
    columns: list[str],
) -> None:
    database_path = tmp_path / "nova.db"

    command.upgrade(_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            constraints = inspect(connection).get_unique_constraints(table)
    finally:
        engine.dispose()

    assert any(
        constraint["column_names"] == columns for constraint in constraints
    ), f"unique constraint on {table}.{columns} missing"


@pytest.mark.parametrize(
    ("table", "primary_key"),
    [
        ("preferences", ["key"]),
        ("devices", ["id"]),
        ("audit_events", ["id"]),
    ],
)
def test_primary_keys_are_preserved(
    tmp_path: Path,
    table: str,
    primary_key: list[str],
) -> None:
    database_path = tmp_path / "nova.db"

    command.upgrade(_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            constraint = inspect(connection).get_pk_constraint(table)
    finally:
        engine.dispose()

    assert constraint["constrained_columns"] == primary_key


def test_audit_hash_chain_columns_are_created(tmp_path: Path) -> None:
    database_path = tmp_path / "nova.db"

    command.upgrade(_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            columns = {
                column["name"]: column
                for column in inspect(connection).get_columns("audit_events")
            }
    finally:
        engine.dispose()

    assert columns["prev_hash"]["nullable"] is True
    assert columns["hash"]["nullable"] is False


def test_nullable_public_key_is_preserved(tmp_path: Path) -> None:
    database_path = tmp_path / "nova.db"

    command.upgrade(_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            columns = {
                column["name"]: column
                for column in inspect(connection).get_columns("devices")
            }
    finally:
        engine.dispose()

    assert columns["public_key"]["nullable"] is True
    assert columns["name"]["nullable"] is False


def test_indexes_are_preserved(tmp_path: Path) -> None:
    database_path = tmp_path / "nova.db"

    command.upgrade(_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            device_indexes = {
                index["name"] for index in inspector.get_indexes("devices")
            }
            audit_indexes = {
                index["name"] for index in inspector.get_indexes("audit_events")
            }
    finally:
        engine.dispose()

    assert "ix_devices_status" in device_indexes
    assert "ix_audit_events_actor_occurred_at" in audit_indexes
    assert "ix_audit_events_occurred_at" in audit_indexes


# ---------------------------------------------------------------------------
# Offline mode
# ---------------------------------------------------------------------------


def test_offline_mode_emits_sql_without_a_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Offline mode must produce SQL without connecting.

    Alembic writes offline SQL to stdout, so the output is captured rather
    than redirected to a file handle.
    """
    database_path = tmp_path / "never-created.db"

    command.upgrade(_config(database_path), "head", sql=True)

    script = capsys.readouterr().out

    assert "CREATE TABLE devices" in script
    assert "CREATE TABLE audit_events" in script
    assert "ON DELETE RESTRICT" in script
    assert not database_path.exists(), "offline mode must not create a database"


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


def test_migrations_stay_within_the_temporary_directory(
    tmp_path: Path,
) -> None:
    """Explicit guard: the real NOVA database must not be touched."""
    database_path = tmp_path / "nova.db"

    command.upgrade(_config(database_path), "head")

    assert database_path.exists()
    assert database_path.parent == tmp_path
    assert _table_names(database_path) == EXPECTED_TABLES_AFTER_UPGRADE