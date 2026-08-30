"""The Alembic baseline and legacy startup path must create the same fresh schema."""

from __future__ import annotations

import asyncio
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlmodel import SQLModel

from treg import audit, db
from treg.config import get_settings
from treg.maintenance import _alembic_config




def _normalized(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _normalized(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    return str(value)


def _schema_dump(connection) -> dict[str, dict[str, Any]]:
    """Return every application table's columns, constraints, and indexes."""
    inspector = inspect(connection)
    tables = sorted(set(inspector.get_table_names()) - {"alembic_version"})
    result: dict[str, dict[str, Any]] = {}

    for table in tables:
        columns = []
        for column in inspector.get_columns(table):
            columns.append({
                "name": column["name"],
                "type": column["type"].compile(dialect=connection.dialect),
                "nullable": column["nullable"],
                "default": _normalized(column.get("default")),
                "primary_key": column.get("primary_key", 0),
                "autoincrement": _normalized(column.get("autoincrement")),
                "computed": _normalized(column.get("computed")),
                "identity": _normalized(column.get("identity")),
            })

        def sorted_objects(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
            normalized = [_normalized(item) for item in objects]
            return sorted(normalized, key=repr)

        result[table] = {
            "columns": columns,
            "primary_key": _normalized(inspector.get_pk_constraint(table)),
            "foreign_keys": sorted_objects(inspector.get_foreign_keys(table)),
            "unique_constraints": sorted_objects(inspector.get_unique_constraints(table)),
            "check_constraints": sorted_objects(inspector.get_check_constraints(table)),
            "indexes": sorted_objects(inspector.get_indexes(table)),
        }

    return result


async def _dump_current_schema() -> dict[str, dict[str, Any]]:
    async with db._engine.connect() as connection:
        return await connection.run_sync(_schema_dump)


async def _drop_everything() -> None:
    async with db._engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.drop_all)
        await connection.execute(text("DROP TABLE IF EXISTS alembic_version"))


async def _upgrade_to(revision: str) -> None:
    """Migrate through the same packaged Config the deploy path builds (script location included),
    on a disposed engine so the sync Alembic run cannot collide with pooled async connections."""
    await db._engine.dispose()
    await asyncio.to_thread(command.upgrade, _alembic_config(), revision)


def _autogenerate_diff(connection) -> list[Any]:
    context = MigrationContext.configure(connection)
    return compare_metadata(context, SQLModel.metadata)


async def test_alembic_baseline_matches_init_db_on_a_fresh_database(monkeypatch):
    """Compare schema objects on SQLite locally and Postgres in test-postgres CI."""
    await audit.drain()
    await _drop_everything()
    monkeypatch.setattr(get_settings(), "secret_key", "test-only-placeholder-key")

    try:
        await db.init_db()
        init_db_schema = await _dump_current_schema()

        await _drop_everything()
        await db._engine.dispose()

        # Head, not a pinned revision: BOTH sides track live metadata (create_all builds fresh
        # tables from the models; the drift guard below keeps head equal to the models), so this
        # stays green across future revisions. Retires with init_db in Stage 5 PR3.
        await _upgrade_to("head")
        alembic_schema = await _dump_current_schema()

        assert init_db_schema.keys() == alembic_schema.keys()
        for table, schema in init_db_schema.items():
            assert alembic_schema[table] == schema, f"schema mismatch for {table}"
    finally:
        await _drop_everything()
        await db.reset_db()


async def test_alembic_head_has_no_model_drift():
    """Alembic is authoritative after adoption, so head must match SQLModel metadata exactly."""
    await audit.drain()
    await _drop_everything()

    try:
        await _upgrade_to("head")
        async with db._engine.connect() as connection:
            diff = await connection.run_sync(_autogenerate_diff)
        assert diff == []
    finally:
        await _drop_everything()
        await db.reset_db()
