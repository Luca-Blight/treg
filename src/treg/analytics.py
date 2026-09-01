"""Server-side product analytics and fault capture (PostHog) — bounded, droppable.

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

Server faults share this intentionally lossy pipe. A root logging handler mirrors ERROR+
records into PostHog's Error Tracking `$exception` event; Uvicorn's error logger gets the
same handler because its default configuration stops propagation before root. Fault payloads
contain only exception type + a short message — never frames, locals, request bodies, or a
user identity. Per-key and global token buckets keep a fault storm from evicting ordinary
analytics. Losing a fault costs an alert, never the availability of the server reporting it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import re
import threading
import time

import httpx

from .config import get_settings

_queue: list[dict] = []
_MAX_PENDING = 2000        # shed load past this: drop the event rather than grow unbounded
_BATCH_MAX = 100           # events per /batch/ POST
_FLUSH_INTERVAL_S = 2.0    # max staleness before a flush

_flusher: asyncio.Task | None = None

_SERVER_DISTINCT_ID = "treg-server"
_FAULT_VALUE_MAX = 500
_FAULT_PER_KEY_CAPACITY = 10.0
_FAULT_PER_KEY_REFILL_S = 60.0 / _FAULT_PER_KEY_CAPACITY
_FAULT_GLOBAL_CAPACITY = 60.0
_FAULT_GLOBAL_REFILL_S = 60.0 / _FAULT_GLOBAL_CAPACITY


class _TokenBucket:
    def __init__(self, capacity: float, now: float):
        self.capacity = capacity
        self.tokens = capacity
        self.updated = now
        self.dropped = 0

    def refill(self, now: float, refill_s: float) -> None:
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed / refill_s)
        self.updated = now


_fault_lock = threading.Lock()
_fault_buckets: dict[tuple[str, str], _TokenBucket] = {}
_fault_global = _TokenBucket(_FAULT_GLOBAL_CAPACITY, time.monotonic())
_fault_capture_active = threading.local()

_handler_lock = threading.Lock()
_installed_fault_handler: FaultCaptureHandler | None = None
_installed_fault_handler_users = 0


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


def _allow_fault(key: tuple[str, str]) -> int | None:
    """Return this key's accumulated drop count, or None when this event is throttled."""
    now = time.monotonic()
    with _fault_lock:
        bucket = _fault_buckets.get(key)
        if bucket is None:
            bucket = _fault_buckets[key] = _TokenBucket(_FAULT_PER_KEY_CAPACITY, now)
        bucket.refill(now, _FAULT_PER_KEY_REFILL_S)
        _fault_global.refill(now, _FAULT_GLOBAL_REFILL_S)
        if bucket.tokens < 1.0 or _fault_global.tokens < 1.0:
            bucket.dropped += 1
            return None
        bucket.tokens -= 1.0
        _fault_global.tokens -= 1.0
        dropped, bucket.dropped = bucket.dropped, 0
        return dropped


def capture_fault(exc: BaseException | None = None, *, component: str,
                  fault_type: str | None = None, logger: str | None = None,
                  value: str | None = None) -> None:
    """Queue a secret-minimal PostHog Error Tracking event. Never raises or touches the DB."""
    if getattr(_fault_capture_active, "active", False):
        return
    _fault_capture_active.active = True
    try:
        if not enabled():
            return
        resolved_type = fault_type or (type(exc).__name__ if exc is not None else logger)
        resolved_type = resolved_type or "ServerFault"
        resolved_value = str(exc) if exc is not None else (value or resolved_type)
        # Query injection puts credentials in URLs, and exception strings (notably httpx's)
        # can include the full request URL. Redact before truncation so no partial key survives.
        resolved_value = re.sub(r"\?\S*", "?[redacted]", resolved_value)
        dropped = _allow_fault((resolved_type, logger or component))
        if dropped is None:
            return
        properties: dict = {
            "$exception_list": [{
                "type": resolved_type,
                "value": resolved_value[:_FAULT_VALUE_MAX],
                "mechanism": {"handled": False},
            }],
            "component": component,
            "fault_type": resolved_type,
        }
        if logger:
            properties["logger"] = logger
        if dropped:
            properties["throttled_dropped"] = dropped
        capture(_SERVER_DISTINCT_ID, "$exception", properties)
    except Exception:  # noqa: BLE001 — reporting a fault must never become another fault
        pass
    finally:
        _fault_capture_active.active = False


class FaultCaptureHandler(logging.Handler):
    """Mirror ERROR+ logs to `capture_fault` without participating in logging failures."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name == __name__ or record.name.startswith(f"{__name__}."):
                return
            # The same instance is on root and uvicorn.error. Custom log configs may propagate
            # Uvicorn records to both, so stamp the record before either path can duplicate it.
            if getattr(record, "_treg_fault_capture_seen", False):
                return
            record._treg_fault_capture_seen = True
            exc = record.exc_info[1] if record.exc_info else None
            capture_fault(
                exc if isinstance(exc, BaseException) else None,
                component="asgi" if record.name == "uvicorn.error" and exc else "logging",
                fault_type=type(exc).__name__ if isinstance(exc, BaseException) else record.name,
                logger=record.name,
                # A bare log message can contain request data. Without exc_info, use the
                # logger identity for grouping instead of forwarding formatted arguments.
                value=None if exc else record.name,
            )
        except Exception:  # noqa: BLE001 — Handler.emit must never escape into its caller
            pass


def install_fault_handler() -> FaultCaptureHandler | None:
    """Install during server lifespan only. Empty posthog_key leaves logging untouched."""
    global _installed_fault_handler, _installed_fault_handler_users
    if not enabled():
        return None
    with _handler_lock:
        if _installed_fault_handler is None:
            _installed_fault_handler = FaultCaptureHandler()
            logging.getLogger().addHandler(_installed_fault_handler)
            # Uvicorn's default `uvicorn` parent has propagate=False, before root.
            logging.getLogger("uvicorn.error").addHandler(_installed_fault_handler)
        _installed_fault_handler_users += 1
        return _installed_fault_handler


def remove_fault_handler(handler: FaultCaptureHandler | None) -> None:
    """Undo one lifespan's installation; safe for disabled or overlapping lifespans."""
    global _installed_fault_handler, _installed_fault_handler_users
    if handler is None:
        return
    with _handler_lock:
        if handler is not _installed_fault_handler:
            return
        _installed_fault_handler_users -= 1
        if _installed_fault_handler_users > 0:
            return
        logging.getLogger().removeHandler(handler)
        logging.getLogger("uvicorn.error").removeHandler(handler)
        _installed_fault_handler = None
        _installed_fault_handler_users = 0


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
