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

_ADOPTION_COLUMNS: dict[str, set[str]] = {
    "archivekey": {"key_hash"},
    "callrecord": {"cached", "hit"},
    "capacitypolicy": {"owner_email"},
    "org": {"platform_overflow_disabled"},
    "overflowroute": {"agg_slug"},
    "overflowspend": {"cost_micro"},
}


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


def _find_adoption_gaps(sync_connection) -> list[str]:
    from . import models  # noqa: F401 - populate SQLModel.metadata

    inspector = inspect(sync_connection)
    existing_tables = set(inspector.get_table_names())
    expected_tables = {table.name for table in SQLModel.metadata.sorted_tables}
    gaps = [f"table {name}" for name in sorted(expected_tables - existing_tables)]

    for table_name, expected_columns in _ADOPTION_COLUMNS.items():
        if table_name not in existing_tables:
            continue
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        gaps.extend(
            f"column {table_name}.{name}"
            for name in sorted(expected_columns - actual_columns)
        )
    return gaps


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
