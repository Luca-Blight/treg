"""Explicit, ordered maintenance tasks run once per release before serving."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from .application.connect import _backfill_provider_extra_tools
from .db import init_db


ReleaseTask = tuple[str, Callable[[], Awaitable[int]]]

# Content-driven tasks stay in a stable order so every release applies the same repairs before
# new code serves traffic. Task bodies remain with their owning subsystem; this is orchestration.
RELEASE_TASKS: tuple[ReleaseTask, ...] = (
    ("provider companion tools", _backfill_provider_extra_tools),
)


async def upgrade() -> None:
    """Prepare the schema, then run every idempotent release task in order."""
    await init_db()
    logger = logging.getLogger("treg.maintenance")
    for name, task in RELEASE_TASKS:
        changed = await task()
        logger.info("upgrade task %s complete: %d row(s) created", name, changed)
