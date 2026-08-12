"""Server-side product analytics (PostHog) — batched, bounded, droppable.

Same discipline as audit.py (rule #2: never block a proxied response), but the sink is
PostHog's /batch/ endpoint instead of the DB. Losing an event here costs a chart, never
money or a response, so every failure mode is a drop: queue full → drop newest, POST
fails → drop the batch, no running loop → drop the event. `capture()` is synchronous and
never raises — call sites stay one line and can sit inside the Stripe webhook handler
(where an exception would 500 and make Stripe retry a payment that already credited).

Gate: an empty `posthog_key` turns the whole module off — self-hosted instances send
nothing, and the test suite (no key set) stays inert. The key is the same PUBLIC ingestion
key the browser uses; /batch/ accepts it.

Unlike audit.py there is no semaphore: that cap exists to protect the shared DB pool,
which HTTP traffic to PostHog never touches. One flusher task micro-batches the queue
(≤ _BATCH_MAX per POST, at most every _FLUSH_INTERVAL_S) so outbound requests stay ~0.5/s
no matter how hot /call runs. The client is opened per flush, not cached at module level —
a loop-bound client is exactly the footgun audit._get_sem() exists to dodge, and at one
flush per two seconds keepalive buys nothing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from .config import get_settings

_queue: list[dict] = []
_MAX_PENDING = 2000        # shed load past this: drop the event rather than grow unbounded
_BATCH_MAX = 100           # events per /batch/ POST
_FLUSH_INTERVAL_S = 2.0    # max staleness before a flush

_flusher: asyncio.Task | None = None


def enabled() -> bool:
    """Empty posthog_key = OFF (self-hosters and tests send nothing)."""
    return bool(get_settings().posthog_key)


def capture(distinct_id: str, event: str, properties: dict | None = None,
            *, groups: dict[str, str] | None = None) -> None:
    """Queue one event. Synchronous, non-blocking, never raises.

    `groups` lands as $groups (e.g. {"team": org_slug}) so server events aggregate on the
    same PostHog group the browser stamps via posthog.group('team', slug).
    """
    try:
        if not enabled() or len(_queue) >= _MAX_PENDING:
            return
        props = dict(properties or {})
        if groups:
            props["$groups"] = groups
        props["$lib"] = "treg-server"
        _queue.append({
            "event": event,
            "distinct_id": distinct_id,
            "properties": props,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        _ensure_flusher()
    except Exception:  # noqa: BLE001 — analytics must never surface into a caller's path
        pass


def _ensure_flusher() -> None:
    """Start (or restart) the flusher on the CURRENT loop. No loop → drop-by-doing-nothing:
    the event stays queued and the next capture from a loop context picks it up."""
    global _flusher
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _flusher is None or _flusher.done() or _flusher.get_loop() is not loop:
        _flusher = loop.create_task(_flush_loop())


async def _flush_loop() -> None:
    while _queue:
        await asyncio.sleep(_FLUSH_INTERVAL_S)
        while _queue:
            batch, _queue[:_BATCH_MAX] = _queue[:_BATCH_MAX], []
            try:
                await _post(batch)
            except Exception:  # noqa: BLE001 — a failed batch is dropped, the loop survives
                pass


async def _post(batch: list[dict]) -> None:
    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(f"{s.posthog_host.rstrip('/')}/batch/",
                              json={"api_key": s.posthog_key, "batch": batch})
    except Exception:  # noqa: BLE001 — a PostHog hiccup costs this batch, nothing else
        pass


async def drain() -> None:
    """Best-effort flush of whatever is queued (shutdown / tests). One attempt per batch."""
    global _flusher
    if _flusher is not None and not _flusher.done():
        _flusher.cancel()
        try:
            await _flusher
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _flusher = None
    while _queue:
        batch, _queue[:_BATCH_MAX] = _queue[:_BATCH_MAX], []
        try:
            await _post(batch)
        except Exception:  # noqa: BLE001 — drain runs in lifespan shutdown; never mask teardown
            pass
