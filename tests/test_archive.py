"""The archive skeleton (PR 1): mode gate, eligibility policy, cache key, and the two tables.

No behavior exists yet — the recorder and the serve path arrive in later PRs — so these tests pin
the contracts everything later builds on: the mode degrades safely, the policy refuses every
uncertain input, the key is canonical, and the tables round-trip on both engines (this file runs
in the sqlite suite and in CI's serial Postgres job).
"""

from __future__ import annotations

import pytest

from treg import archive
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
