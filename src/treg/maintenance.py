"""Explicit, ordered maintenance tasks run once per release before serving."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from importlib.resources import files

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlmodel import SQLModel

from .application.connect import _backfill_provider_extra_tools
from .config import get_settings
from .db import _engine, init_db


ReleaseTask = tuple[str, Callable[[], Awaitable[int]]]

# Content-driven tasks stay in a stable order so every release applies the same repairs before
# new code serves traffic. Task bodies remain with their owning subsystem; this is orchestration.
RELEASE_TASKS: tuple[ReleaseTask, ...] = (
    ("provider companion tools", _backfill_provider_extra_tools),
)

def _alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(files("treg").joinpath("alembic")))
    config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))
    return config


async def _table_names() -> set[str]:
    async with _engine.connect() as connection:
        return await connection.run_sync(lambda sync_connection: set(
            inspect(sync_connection).get_table_names()
        ))


def _apply_adoption_repairs(sync_connection) -> list[str]:
    """Heal the historical shapes the frozen legacy path cannot: columns that only ever existed
    as Alembic revisions. `create_all` never adds a column to an existing table, so a database
    whose archive tables predate revision 0004 (a mid-series dev/archive checkout, or an
    `alembic upgrade 0003` that later lost its version table) is missing the request-shape
    columns and would otherwise be stamped straight past the revision that adds them."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    import sqlalchemy as sa
    import sqlmodel

    inspector = inspect(sync_connection)
    if "archivekey" not in set(inspector.get_table_names()):
        return []
    present = {column["name"] for column in inspector.get_columns("archivekey")}
    # Mirrors revision 0004 exactly, except the temporary server_default stays in place: the ORM
    # always writes these values, and dropping a default on SQLite means a full table rebuild.
    wanted = (
        sa.Column("req_method", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.Column("req_url", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default=""),
        sa.Column("req_body", sa.LargeBinary(), nullable=True),
        sa.Column("req_headers", sa.JSON(), nullable=True),
    )
    operations = Operations(MigrationContext.configure(sync_connection))
    applied = []
    for column in wanted:
        if column.name in present:
            continue
        operations.add_column("archivekey", column)
        applied.append(f"archivekey.{column.name}")
    return applied


def _find_adoption_gaps(sync_connection) -> list[str]:
    """Every table and every column of the model metadata must exist before a stamp. A hand-kept
    subset already let one gap through (the 0004 request-shape columns); the sweep is total so the
    next gap refuses loudly instead of being stamped past."""
    from . import models  # noqa: F401 - populate SQLModel.metadata

    inspector = inspect(sync_connection)
    existing_tables = set(inspector.get_table_names())
    gaps = []
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            gaps.append(f"table {table.name}")
            continue
        actual_columns = {column["name"] for column in inspector.get_columns(table.name)}
        gaps.extend(
            f"column {table.name}.{name}"
            for name in sorted({column.name for column in table.columns} - actual_columns)
        )
    return sorted(gaps)


async def _adoption_repairs() -> list[str]:
    async with _engine.begin() as connection:
        return await connection.run_sync(_apply_adoption_repairs)


async def _adoption_gaps() -> list[str]:
    async with _engine.connect() as connection:
        return await connection.run_sync(_find_adoption_gaps)


async def _upgrade_schema() -> None:
    tables = await _table_names()
    config = _alembic_config()

    if not tables:
        await asyncio.to_thread(command.upgrade, config, "head")
        print("treg schema: alembic upgrade head (empty database)")
        return

    if "alembic_version" in tables:
        await asyncio.to_thread(command.upgrade, config, "head")
        print("treg schema: alembic upgrade head (stamped database)")
        return

    await init_db()
    repaired = await _adoption_repairs()
    if repaired:
        print(f"treg schema: adoption repaired {', '.join(repaired)}")
    gaps = await _adoption_gaps()
    if gaps:
        missing = ", ".join(gaps)
        raise RuntimeError(
            "Cannot adopt the existing database because its legacy schema is incomplete. "
            f"Missing: {missing}. Alembic stamp was not applied."
        )
    await asyncio.to_thread(command.stamp, config, "head")
    print("treg schema: adopted legacy database and stamped head")


async def upgrade() -> None:
    """Prepare the schema, then run every idempotent release task in order."""
    await _upgrade_schema()
    logger = logging.getLogger("treg.maintenance")
    for name, task in RELEASE_TASKS:
        changed = await task()
        logger.info("upgrade task %s complete: %d row(s) created", name, changed)
