"""The audit queue's drain discipline."""

import asyncio
import subprocess
import sys

from treg import audit


async def test_drain_exits_when_a_finished_task_lingers_in_the_pending_set():
    """The CI livelock shape: a completed task still in `_pending` with no removal callback coming.

    Awaiting a gather of already-complete tasks never suspends (Python ≥3.10 gather returns a done
    future eagerly), so a drain that relies on the call_soon'd discard callback spins synchronously
    forever — asyncio timers included, which is why the in-process `wait_for` below could never fire
    against the broken shape. Drain must remove what it gathered itself.
    """
    async def _noop() -> None:
        return None

    task = asyncio.create_task(_noop())
    await task
    audit._pending.add(task)
    await asyncio.wait_for(audit.drain(), timeout=5)
    assert task not in audit._pending


def test_drain_livelock_regression_fails_instead_of_wedging():
    """Run the same shape in a subprocess with a parent-side deadline.

    A regression here livelocks the event loop at ~100% CPU, starving every in-process watchdog —
    the only reliable referee lives outside the process. A wedged suite was exactly how the original
    bug presented on CI; a reintroduction must fail in seconds instead.
    """
    program = (
        "import asyncio\n"
        "from treg import audit\n"
        "async def main():\n"
        "    async def _noop():\n"
        "        return None\n"
        "    task = asyncio.create_task(_noop())\n"
        "    await task\n"
        "    audit._pending.add(task)\n"
        "    await audit.drain()\n"
        "    assert not audit._pending\n"
        "asyncio.run(main())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], timeout=30, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-500:]
