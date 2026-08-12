"""analytics.py — bounded, batched, droppable, and OFF by default.

The module's whole contract is negative space: with no posthog_key it must do nothing, and
with one it must never raise, never block, and never grow without bound. Each test pins one
of those guarantees; the event payload shape (distinct_id / $groups / $lib) is pinned once.
"""

from __future__ import annotations

import pytest

from treg import analytics
from treg.config import get_settings


@pytest.fixture(autouse=True)
async def _clean():
    analytics._queue.clear()
    analytics._flusher = None
    yield
    await analytics.drain()
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
