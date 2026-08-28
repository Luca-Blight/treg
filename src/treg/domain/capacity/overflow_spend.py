"""Per-aggregator daily overflow accounting (`OverflowSpend`) — the budget the child cycle checks
before it places a hold, and the row its settle updates in the same transaction."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ...models import OverflowSpend
from ...timeutil import utcnow_naive


def utc_day(now: datetime | None = None) -> str:
    return (now or utcnow_naive()).strftime("%Y-%m-%d")


async def add_in_transaction(db: AsyncSession, aggregator: str, cost_micro: int, delta_micro: int,
                             *, day: str | None = None) -> OverflowSpend:
    """Add one call to the day's row. Does NOT commit: the caller owns the transaction (the child
    settle, or the shadow probe's own short session)."""
    day = day or utc_day()
    now = utcnow_naive()
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        insert = postgresql_insert
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:
        raise RuntimeError(f"unsupported database dialect: {dialect}")
    statement = insert(OverflowSpend).values(
        aggregator=aggregator,
        day=day,
        calls=1,
        cost_micro=int(cost_micro),
        delta_micro=int(delta_micro),
        updated_at=now,
    ).on_conflict_do_update(
        index_elements=[OverflowSpend.aggregator, OverflowSpend.day],
        set_={
            "calls": OverflowSpend.calls + 1,
            "cost_micro": OverflowSpend.cost_micro + int(cost_micro),
            "delta_micro": OverflowSpend.delta_micro + int(delta_micro),
            "updated_at": now,
        },
    ).returning(OverflowSpend).execution_options(populate_existing=True)
    return (await db.execute(statement)).scalar_one()


async def spent_today(db: AsyncSession, aggregator: str, *, day: str | None = None) -> int:
    row = await db.get(OverflowSpend, (aggregator, day or utc_day()))
    return int(row.cost_micro) if row is not None else 0
