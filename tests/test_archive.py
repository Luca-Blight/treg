"""The archive skeleton (PR 1): mode gate, eligibility policy, cache key, and the two tables.

No behavior exists yet — the recorder and the serve path arrive in later PRs — so these tests pin
the contracts everything later builds on: the mode degrades safely, the policy refuses every
uncertain input, the key is canonical, and the tables round-trip on both engines (this file runs
in the sqlite suite and in CI's serial Postgres job).
"""

from __future__ import annotations

import pytest

from treg import api as A, archive
from treg.archive import cache_key, content_hash, policy, storable
from treg.models import ArchiveKey, ArchiveSnapshot


# ---------------------------------------------------------------------------------------------
# Mode: a typo must disable, never enable

def test_mode_defaults_off():
    assert archive.mode() == "off"
    assert not archive.recording()
    assert not archive.serving()


@pytest.mark.parametrize("raw,expected", [
    ("shadow", "shadow"), ("serve", "serve"), ("off", "off"),
    ("SERVE", "serve"), ("  shadow ", "shadow"),          # env-var hygiene
    ("on", "off"), ("true", "off"), ("", "off"), ("srve", "off"),  # typos degrade to off
])
def test_mode_parses_and_degrades(monkeypatch, raw, expected):
    from treg.config import get_settings
    monkeypatch.setattr(get_settings(), "archive_mode", raw)
    assert archive.mode() == expected


def test_serve_implies_recording(monkeypatch):
    from treg.config import get_settings
    monkeypatch.setattr(get_settings(), "archive_mode", "serve")
    assert archive.recording() and archive.serving()
    monkeypatch.setattr(get_settings(), "archive_mode", "shadow")
    assert archive.recording() and not archive.serving()


# ---------------------------------------------------------------------------------------------
# Policy: forbidden on every uncertain branch

def test_policy_refuses_uncertainty():
    assert policy(None) == "forbidden"
    assert policy({}) == "forbidden"
    assert policy({"kind": "read"}) == "forbidden"                  # unjudged license
    assert policy({"cache": "everything"}) == "forbidden"           # unknown value
    assert policy({"cache": {"mode": "keep"}}) == "forbidden"       # unknown value, dict form


def test_policy_action_beats_license():
    # Gate order: an action is never stored even when a license field says archive.
    assert policy({"kind": "action", "cache": "archive"}) == "forbidden"


def test_policy_accepts_judged_entries():
    assert policy({"cache": "transient"}) == "transient"
    assert policy({"cache": "archive"}) == "archive"
    # Provenance form — the catalog carries the license quote alongside, policy reads only mode.
    entry = {"cache": {"mode": "archive", "license_quote": "CC0", "source_url": "https://x"}}
    assert policy(entry) == "archive"
    assert storable(entry)
    assert not storable({"kind": "action", "cache": "archive"})


# ---------------------------------------------------------------------------------------------
# Cache key: canonical, deterministic, and blind to transport noise

def test_key_is_deterministic_and_canonical():
    a = cache_key("POST", "prov.x", "https://api.x/v1/q?b=2&a=1", b'{"z": 1, "a": 2}')
    b = cache_key("post", "prov.x", "https://api.x/v1/q?a=1&b=2", b'{"a": 2, "z": 1}')
    assert a == b and len(a) == 64


def test_key_separates_real_differences():
    base = cache_key("GET", "prov.x", "https://api.x/v1/q?a=1")
    assert base != cache_key("POST", "prov.x", "https://api.x/v1/q?a=1")      # method
    assert base != cache_key("GET", "prov.y", "https://api.x/v1/q?a=1")       # endpoint id
    assert base != cache_key("GET", "prov.x", "https://api.x/v1/q?a=2")       # param value
    assert base != cache_key("GET", "prov.x", "https://api.x/v1/q?a=1&b=1")   # extra param


def test_key_ignores_caller_noise_headers():
    quiet = cache_key("GET", "p.e", "https://api.x/q?a=1")
    noisy = cache_key("GET", "p.e", "https://api.x/q?a=1", headers={
        "Authorization": "Bearer zzz", "Cookie": "s=1", "User-Agent": "curl",
        "X-Treg-Token": "t", "Accept-Encoding": "gzip", "traceparent": "00-x",
    })
    assert quiet == noisy
    # …but Accept genuinely changes some vendors' answers, so it keys.
    assert quiet != cache_key("GET", "p.e", "https://api.x/q?a=1",
                              headers={"Accept": "text/csv"})


def test_key_non_json_body_hashes_raw():
    a = cache_key("POST", "p.e", "https://api.x/q", b"plain text body")
    b = cache_key("POST", "p.e", "https://api.x/q", b"plain text body")
    c = cache_key("POST", "p.e", "https://api.x/q", b"other text body")
    assert a == b != c


def test_content_hash_is_raw_identity():
    assert content_hash(b"same") == content_hash(b"same")
    assert content_hash(b"same") != content_hash(b"Same")


# ---------------------------------------------------------------------------------------------
# Tables: round-trip on the running engine (sqlite locally, Postgres in CI's serial job)

@pytest.mark.anyio
async def test_tables_round_trip(clients):  # clients fixture resets the schema on this engine
    from sqlmodel import select
    from treg.db import session_maker

    async with session_maker() as s:
        key = ArchiveKey(key_hash="k" * 64, endpoint_id="prov.search", provider="prov",
                         policy="transient", ttl_s=3600, volatile_paths=["$.request_id"])
        s.add(key)
        await s.commit()
        await s.refresh(key)

        first = ArchiveSnapshot(key_id=key.id, version=1, status_code=200,
                                media_type="application/json", content_hash=content_hash(b"{}"),
                                body=b"{}", size_bytes=2, origin="caller")
        s.add(first)
        await s.commit()
        await s.refresh(first)
        # Deduplicated second version: same bytes, body carried by reference, not stored again.
        s.add(ArchiveSnapshot(key_id=key.id, version=2, status_code=200,
                              media_type="application/json", content_hash=first.content_hash,
                              body=None, body_of=first.id, size_bytes=2, origin="refresh"))
        await s.commit()

        rows = (await s.execute(select(ArchiveSnapshot).where(ArchiveSnapshot.key_id == key.id)
                                .order_by(ArchiveSnapshot.version))).scalars().all()
        assert [r.version for r in rows] == [1, 2]
        assert rows[0].body == b"{}" and rows[1].body is None
        assert rows[1].body_of == rows[0].id
        stored = (await s.execute(select(ArchiveKey)
                                  .where(ArchiveKey.key_hash == "k" * 64))).scalars().one()
        assert stored.volatile_paths == ["$.request_id"]
        assert stored.change_seen == 0 and stored.heat == 0.0


@pytest.mark.anyio
async def test_key_hash_is_unique(clients):
    from sqlalchemy.exc import IntegrityError
    from treg.db import session_maker

    async with session_maker() as s:
        s.add(ArchiveKey(key_hash="dup", endpoint_id="a"))
        await s.commit()
        s.add(ArchiveKey(key_hash="dup", endpoint_id="b"))
        with pytest.raises(IntegrityError):
            await s.commit()


# ---------------------------------------------------------------------------------------------
# The recorder (PR 2): observe metered platform answers, never touch the call

from httpx import AsyncClient
from sqlalchemy import select

from treg import catalog_store
from treg.config import get_settings
from treg.db import session_maker

EP = "tikhub.tiktok.video.comments"   # tier-4 eligible in the test allow-list, GET, $0.001/call


@pytest.fixture
def platform_on(monkeypatch):
    """Tier 4 the way a deploy turns it on (mirrors test_marketplace_call)."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "PLATFORM-TIKHUB-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def shadow(platform_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "archive_mode", "shadow")


async def _rows():
    await archive.drain()
    async with session_maker() as s:
        keys = (await s.execute(select(ArchiveKey))).scalars().all()
        snaps = (await s.execute(
            select(ArchiveSnapshot).order_by(ArchiveSnapshot.version))).scalars().all()
        return keys, snaps


@pytest.mark.anyio
async def test_recorder_observes_a_metered_call(clients: AsyncClient, shadow):
    r = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r.status_code == 200, r.text
    keys, snaps = await _rows()
    assert len(keys) == 1 and len(snaps) == 1
    assert keys[0].endpoint_id == EP and keys[0].provider == "tikhub"
    # No cache field on this entry yet → policy forbidden → hash-only: counted, never kept.
    assert keys[0].policy == "forbidden"
    assert snaps[0].body is None and snaps[0].body_of is None
    assert snaps[0].size_bytes > 0 and len(snaps[0].content_hash) == 64
    assert snaps[0].origin == "caller"


@pytest.mark.anyio
async def test_recorder_off_by_default(clients: AsyncClient, platform_on):
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200
    keys, snaps = await _rows()
    assert keys == [] and snaps == []


@pytest.mark.anyio
async def test_storable_policy_keeps_bytes_and_dedups(clients: AsyncClient, shadow, monkeypatch):
    entry = catalog_store.load().by_id[EP]
    monkeypatch.setitem(entry, "cache", "transient")
    r1 = await clients.get(f"/call/{EP}?aweme_id=7")
    r2 = await clients.get(f"/call/{EP}?aweme_id=7")   # same key, identical echo answer
    assert r1.status_code == r2.status_code == 200
    keys, snaps = await _rows()
    assert len(keys) == 1 and [s.version for s in snaps] == [1, 2]
    assert keys[0].policy == "transient"
    assert snaps[0].body is not None                    # bytes kept once…
    assert snaps[1].body is None and snaps[1].body_of == snaps[0].id  # …then referenced
    assert keys[0].stable_seen == 1 and keys[0].change_seen == 0


@pytest.mark.anyio
async def test_different_answer_counts_as_change(clients: AsyncClient, shadow, monkeypatch):
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")
    from tests.test_marketplace_call import _fake_relay
    monkeypatch.setattr(A, "relay", _fake_relay(200, b'{"n": 1}'))
    await clients.get(f"/call/{EP}?aweme_id=7")
    monkeypatch.setattr(A, "relay", _fake_relay(200, b'{"n": 2}'))
    await clients.get(f"/call/{EP}?aweme_id=7")
    keys, snaps = await _rows()
    assert len(keys) == 1 and len(snaps) == 2
    assert keys[0].change_seen == 1 and keys[0].stable_seen == 0
    assert keys[0].last_changed_at is not None
    assert snaps[0].body == b'{"n": 1}' and snaps[1].body == b'{"n": 2}'


@pytest.mark.anyio
async def test_different_params_are_different_keys(clients: AsyncClient, shadow):
    await clients.get(f"/call/{EP}?aweme_id=7")
    await clients.get(f"/call/{EP}?aweme_id=8")
    keys, _ = await _rows()
    assert len(keys) == 2


@pytest.mark.anyio
async def test_oversized_body_is_counted_not_kept(clients: AsyncClient, shadow, monkeypatch):
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")
    monkeypatch.setattr(get_settings(), "archive_max_body_bytes", 4)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200
    _, snaps = await _rows()
    assert len(snaps) == 1 and snaps[0].body is None and snaps[0].size_bytes > 4


@pytest.mark.anyio
async def test_error_responses_are_not_recorded(clients: AsyncClient, shadow, monkeypatch):
    from tests.test_marketplace_call import _fake_relay
    monkeypatch.setattr(A, "relay", _fake_relay(500, b"boom"))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 500
    keys, snaps = await _rows()
    assert keys == [] and snaps == []


@pytest.mark.anyio
async def test_a_recorder_crash_never_fails_the_call(clients: AsyncClient, shadow, monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("recorder exploded")
    monkeypatch.setattr(archive, "_store", _boom)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    await archive.drain()
