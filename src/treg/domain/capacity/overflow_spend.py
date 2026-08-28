"""Per-aggregator daily overflow accounting (`OverflowSpend`) — the budget the child cycle checks
before it places a hold, and the row its settle updates in the same transaction."""

from __future__ import annotations

from datetime import datetime

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
    row = await db.get(OverflowSpend, (aggregator, day))
    if row is None:
        row = OverflowSpend(aggregator=aggregator, day=day)
        db.add(row)
    row.calls += 1
    row.cost_micro += int(cost_micro)
    row.delta_micro += int(delta_micro)
    row.updated_at = utcnow_naive()
    return row


async def spent_today(db: AsyncSession, aggregator: str, *, day: str | None = None) -> int:
    row = await db.get(OverflowSpend, (aggregator, day or utc_day()))
    return int(row.cost_micro) if row is not None else 0
