"""The audit queue's drain discipline."""

import asyncio

from treg import audit


async def test_drain_exits_when_a_finished_task_lingers_in_the_pending_set():
    """The CI livelock shape: a completed task still in `_pending` with no removal callback coming.

    Awaiting a gather of already-complete tasks never suspends, so a drain that relies on the
    call_soon'd discard callback spins synchronously forever — timers included. Drain must remove
    what it gathered itself.
    """
    async def _noop() -> None:
        return None

    task = asyncio.create_task(_noop())
    await task
    audit._pending.add(task)
    await asyncio.wait_for(audit.drain(), timeout=5)
    assert task not in audit._pending
