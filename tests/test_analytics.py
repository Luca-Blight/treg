"""analytics.py — bounded, batched, droppable, and OFF by default.

The module's whole contract is negative space: with no posthog_key it must do nothing, and
with one it must never raise, never block, and never grow without bound. Each test pins one
of those guarantees; the event payload shape (distinct_id / $groups / $lib) is pinned once.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.requests import Request
from uvicorn.protocols.http.h11_impl import RequestResponseCycle

from treg import analytics
from treg.application.call.types import CallFailure
from treg.bootstrap_handlers import _pool_saturated
from treg.config import get_settings
from treg.routers.call import _translate_call_failure


@pytest.fixture(autouse=True)
async def _clean():
    while analytics._installed_fault_handler is not None:
        analytics.remove_fault_handler(analytics._installed_fault_handler)
    analytics._queue.clear()
    analytics._flusher = None
    analytics._fault_buckets.clear()
    analytics._fault_global = analytics._TokenBucket(
        analytics._FAULT_GLOBAL_CAPACITY, analytics.time.monotonic())
    yield
    # Enabled tests inspect queued payloads but must never send them to a real PostHog host.
    analytics._queue.clear()
    await analytics.drain()
    while analytics._installed_fault_handler is not None:
        analytics.remove_fault_handler(analytics._installed_fault_handler)
    analytics._queue.clear()


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "posthog_key", "phc_test_suite", raising=False)


@pytest.fixture
def posts(monkeypatch):
    """Record batches instead of talking to PostHog."""
    sent: list[list[dict]] = []

    async def _fake_post(batch):
        sent.append(batch)

    monkeypatch.setattr(analytics, "_post", _fake_post)
    return sent


async def test_disabled_is_a_noop():
    # default settings: no key → capture must not even queue (self-hosters send nothing)
    analytics.capture("a@b.c", "tool_called", {"x": 1})
    assert analytics._queue == []
    assert analytics._flusher is None


async def test_batch_shape_and_drain(enabled, posts):
    analytics.capture("a@b.c", "tool_called", {"provider": "tikhub"}, groups={"team": "acme"})
    analytics.capture("a@b.c", "tool_called", {"provider": "dataforseo"})
    await analytics.drain()
    assert len(posts) == 1
    batch = posts[0]
    assert [e["event"] for e in batch] == ["tool_called", "tool_called"]
    first = batch[0]
    assert first["distinct_id"] == "a@b.c"
    assert first["properties"]["provider"] == "tikhub"
    assert first["properties"]["$groups"] == {"team": "acme"}
    assert first["properties"]["$lib"] == "treg-server"
    assert "$groups" not in batch[1]["properties"]  # no groups passed → no key at all


async def test_queue_is_bounded(enabled, monkeypatch):
    monkeypatch.setattr(analytics, "_MAX_PENDING", 10)
    for i in range(25):
        analytics.capture("a@b.c", "e", {"i": i})
    assert len(analytics._queue) == 10  # newest dropped past the bound, no growth


async def test_oversized_drain_splits_batches(enabled, posts, monkeypatch):
    monkeypatch.setattr(analytics, "_BATCH_MAX", 100)
    for i in range(150):
        analytics.capture("a@b.c", "e", {"i": i})
    await analytics.drain()
    assert [len(b) for b in posts] == [100, 50]


async def test_post_failure_is_swallowed_and_capture_never_raises(enabled, monkeypatch):
    async def _boom(batch):
        raise RuntimeError("posthog is down")

    monkeypatch.setattr(analytics, "_post", _boom)
    analytics.capture("a@b.c", "e")
    await analytics.drain()  # must return, not raise
    assert analytics._queue == []  # the batch was dropped, not retried

    # capture() itself swallows internal failures — the never-raise contract call sites
    # (the Stripe webhook above all) depend on
    def _explode():
        raise RuntimeError("scheduling broke")

    monkeypatch.setattr(analytics, "_ensure_flusher", _explode)
    analytics.capture("a@b.c", "e")  # must not raise


def _exception_events() -> list[dict]:
    return [event for event in analytics._queue if event["event"] == "$exception"]


def test_fault_payload_is_secret_minimal_and_truncated(enabled):
    analytics.capture_fault(RuntimeError("x" * 700), component="scheduler")

    event = _exception_events()[0]
    assert event["distinct_id"] == "treg-server"
    assert event["properties"] == {
        "$exception_list": [{
            "type": "RuntimeError",
            "value": "x" * 500,
            "mechanism": {"handled": False},
        }],
        "component": "scheduler",
        "fault_type": "RuntimeError",
        "$lib": "treg-server",
    }
    assert "frames" not in str(event).lower()
    assert "traceback" not in str(event).lower()


def test_fault_value_redacts_query_credentials_before_capture(enabled):
    analytics.capture_fault(RuntimeError(
        "request failed for https://api.example.com/x?api_key=sk-SECRET&y=1"),
        component="relay")

    value = _exception_events()[0]["properties"]["$exception_list"][0]["value"]
    assert value == "request failed for https://api.example.com/x?[redacted]"
    assert "sk-SECRET" not in value
    assert "api_key=" not in value


def test_per_key_throttle_counts_drops_on_next_allowed_event(enabled, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(analytics.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(analytics, "_MAX_PENDING", 20)
    analytics._fault_global = analytics._TokenBucket(analytics._FAULT_GLOBAL_CAPACITY, now[0])

    for _ in range(100):
        analytics.capture_fault(PoolTimeoutError("pool full"), component="db_pool")
    assert len(_exception_events()) == 10
    analytics.capture("person@example.com", "normal_analytics")
    assert len(analytics._queue) == 11  # the storm cannot evict an ordinary event

    now[0] += 6.0  # ten per minute = one replenished token every six seconds
    analytics.capture_fault(PoolTimeoutError("pool full"), component="db_pool")
    assert len(_exception_events()) == 11
    assert _exception_events()[-1]["properties"]["throttled_dropped"] == 90


def test_global_throttle_limits_many_distinct_faults(enabled, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(analytics.time, "monotonic", lambda: now[0])
    analytics._fault_global = analytics._TokenBucket(analytics._FAULT_GLOBAL_CAPACITY, now[0])

    for i in range(70):
        analytics.capture_fault(RuntimeError(str(i)), component=f"site-{i}")
    assert len(_exception_events()) == 60

    now[0] += 1.0
    analytics.capture_fault(RuntimeError("60"), component="site-60")
    assert len(_exception_events()) == 61
    assert _exception_events()[-1]["properties"]["throttled_dropped"] == 1


def test_handler_recursion_guard_swallows_capture_failure(enabled, monkeypatch):
    calls = 0

    def broken_capture(*args, **kwargs):
        nonlocal calls
        calls += 1
        logging.getLogger("capture.failure").error("capture failed too")
        raise RuntimeError("broken fault capture")

    handler = analytics.install_fault_handler()
    monkeypatch.setattr(analytics, "capture", broken_capture)
    logging.getLogger("background.worker").error("original failure")
    assert calls == 1
    analytics.remove_fault_handler(handler)


def test_handler_ignores_analytics_logger_and_children(enabled):
    handler = analytics.install_fault_handler()
    logging.getLogger("treg.analytics").error("self failure")
    logging.getLogger("treg.analytics.sender").error("child failure")
    assert _exception_events() == []
    analytics.remove_fault_handler(handler)


def test_bare_error_log_does_not_forward_formatted_arguments(enabled):
    handler = analytics.install_fault_handler()
    logging.getLogger("background.worker").error("request body was %s", "secret-value")
    analytics.remove_fault_handler(handler)

    event = _exception_events()[0]
    assert event["properties"]["fault_type"] == "background.worker"
    assert event["properties"]["$exception_list"][0]["value"] == "background.worker"
    assert "secret-value" not in str(event)


def test_handler_is_installed_only_while_enabled(enabled):
    root = logging.getLogger()
    uvicorn_error = logging.getLogger("uvicorn.error")
    handler = analytics.install_fault_handler()
    assert handler in root.handlers
    assert handler in uvicorn_error.handlers

    analytics.remove_fault_handler(handler)
    assert handler not in root.handlers
    assert handler not in uvicorn_error.handlers


def test_disabled_handler_install_does_not_touch_logging():
    root_handlers = list(logging.getLogger().handlers)
    uvicorn_handlers = list(logging.getLogger("uvicorn.error").handlers)
    assert analytics.install_fault_handler() is None
    assert logging.getLogger().handlers == root_handlers
    assert logging.getLogger("uvicorn.error").handlers == uvicorn_handlers


class _Transport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _Cycle:
    """Small protocol shell: exercise Uvicorn's real run_asgi logging without a socket."""

    def __init__(self, scope: dict):
        self.logger = logging.getLogger("uvicorn.error")
        self.scope = scope
        self.transport = _Transport()
        self.response_started = False
        self.response_complete = False
        self.disconnected = False
        self.on_response = lambda: None

    async def receive(self):
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(self, message):
        if message["type"] == "http.response.start":
            self.response_started = True
        elif message["type"] == "http.response.body" and not message.get("more_body", False):
            self.response_complete = True

    async def send_500_response(self):
        self.response_started = True
        self.response_complete = True


def _scope(path: str, query: bytes = b"") -> dict:
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query,
        "root_path": "", "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 1234), "server": ("test", 80), "state": {},
    }


async def _run_through_uvicorn(app: FastAPI, path: str, query: bytes = b"") -> _Cycle:
    cycle = _Cycle(_scope(path, query))
    await RequestResponseCycle.run_asgi(cycle, app)
    return cycle


async def test_unhandled_route_exception_reaches_handler_via_uvicorn(enabled):
    app = FastAPI()

    @app.get("/boom")
    async def boom():
        raise RuntimeError("route exploded")

    handler = analytics.install_fault_handler()
    cycle = await _run_through_uvicorn(app, "/boom")
    analytics.remove_fault_handler(handler)

    assert cycle.transport.closed
    event = _exception_events()[0]
    assert event["properties"]["component"] == "asgi"
    assert event["properties"]["logger"] == "uvicorn.error"
    assert event["properties"]["fault_type"] == "RuntimeError"


async def test_typed_refusals_auth_and_validation_are_not_faults(enabled):
    app = FastAPI()

    @app.get("/call-refusal")
    async def call_refusal():
        failure = CallFailure("tool_access_denied", status_code=403, detail="not allowed")
        raise _translate_call_failure(failure)

    @app.get("/auth-refusal")
    async def auth_refusal():
        raise HTTPException(status_code=401, detail="not authenticated")

    @app.get("/validated")
    async def validated(count: int):
        return {"count": count}

    handler = analytics.install_fault_handler()
    await _run_through_uvicorn(app, "/call-refusal")
    await _run_through_uvicorn(app, "/auth-refusal")
    await _run_through_uvicorn(app, "/validated", b"count=not-an-int")
    analytics.remove_fault_handler(handler)
    assert _exception_events() == []


async def test_typed_pool_saturation_is_explicitly_captured(enabled):
    request = Request(_scope("/health"))
    response = await _pool_saturated(request, PoolTimeoutError("QueuePool limit reached"))
    assert response.status_code == 503
    assert _exception_events()[0]["properties"]["component"] == "db_pool"
