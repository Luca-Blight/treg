"""Explicit, ordered maintenance tasks run once per release before serving."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from importlib.resources import files

from alembic import command, util
from alembic.config import Config
from sqlalchemy import inspect
from sqlmodel import SQLModel

from .application.connect import _backfill_provider_extra_tools
from .db import _db_url, _engine, init_db


ReleaseTask = tuple[str, Callable[[], Awaitable[int]]]

# Content-driven tasks stay in a stable order so every release applies the same repairs before
# new code serves traffic. Task bodies remain with their owning subsystem; this is orchestration.
RELEASE_TASKS: tuple[ReleaseTask, ...] = (
    ("provider companion tools", _backfill_provider_extra_tools),
)


def _alembic_config() -> Config:
    """The one Config both the deploy path and the tests build: the packaged script directory,
    pointed at the same URL the engine uses (%-escaped for configparser interpolation)."""
    config = Config()
    config.set_main_option("script_location", str(files("treg").joinpath("alembic")))
    config.set_main_option("sqlalchemy.url", _db_url.replace("%", "%%"))
    return config


async def _table_names() -> set[str]:
    async with _engine.connect() as connection:
        return await connection.run_sync(lambda sync_connection: set(
            inspect(sync_connection).get_table_names()
        ))


def _find_adoption_gaps(sync_connection) -> list[str]:
    """Every table and every column of the model metadata must exist before a stamp. A hand-kept
    subset already let one gap through (the 0004 request-shape columns); the sweep is total so the
    next gap refuses loudly instead of being stamped past. Deliberately name-level only: types,
    defaults, and indexes legitimately differ between a legacy-built schema and an Alembic-built
    one (the parity test proves the two fresh builds equal at head)."""
    from . import models  # noqa: F401 - populate SQLModel.metadata

    inspector = inspect(sync_connection)
    existing_tables = set(inspector.get_table_names())
    columns_by_table = {
        table: {column["name"] for column in columns}
        for (_, table), columns in inspector.get_multi_columns().items()
    }
    gaps = []
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            gaps.append(f"table {table.name}")
            continue
        actual_columns = columns_by_table.get(table.name, set())
        gaps.extend(
            f"column {table.name}.{name}"
            for name in sorted({column.name for column in table.columns} - actual_columns)
        )
    return sorted(gaps)


async def _upgrade_schema() -> None:
    tables = await _table_names()
    config = _alembic_config()

    if not tables or "alembic_version" in tables:
        state = "empty" if not tables else "stamped"
        try:
            await asyncio.to_thread(command.upgrade, config, "head")
        except util.CommandError as exc:
            if "Can't locate revision" not in str(exc):
                raise
            raise RuntimeError(
                "This database is stamped at a revision this build does not know - the running "
                "code is OLDER than the schema (a rollback past the rollback floor, or a stale "
                "checkout). Deploy a release at least as new as the database. No migration ran."
            ) from exc
        print(f"treg schema: alembic upgrade head ({state} database)")
        return

    await init_db()
    async with _engine.connect() as connection:
        gaps = await connection.run_sync(_find_adoption_gaps)
    if gaps:
        missing = ", ".join(gaps)
        raise RuntimeError(
            "Cannot adopt the existing database because its legacy schema is incomplete. "
            f"Missing: {missing}. Alembic stamp was not applied. If this database was built by "
            "a release older than this one, first install the adoption release - `pip install "
            "'tools-registry[server]==0.14.*'` - complete `python -m treg upgrade` there, then "
            "upgrade onward; otherwise restore the missing objects (or the database) first."
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
