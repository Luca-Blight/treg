"""Shared four-book assertions for every call matrix case."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from httpx import AsyncClient, Response

from treg import audit

from test_marketplace_call import _balance, _entries, _telemetry

from .provider import FakeProvider


@dataclass(frozen=True)
class Snapshot:
    balance_micro: int
    entry_ids: frozenset[str]
    hit_count: int


@dataclass(frozen=True)
class Expect:
    status: int
    body: bytes
    cost_micro: int | None = None
    treg_error: bool = False
    balance_delta: int = 0
    ledger_kinds: tuple[str, ...] = ()
    ledger_reason: str | None = None
    audit: Mapping[str, Any] = field(default_factory=dict)
    upstream_hits: int = 1


async def snapshot(clients: AsyncClient, provider: FakeProvider) -> Snapshot:
    entries = await _entries(clients)
    return Snapshot(
        balance_micro=await _balance(clients),
        entry_ids=frozenset(entry["id"] for entry in entries),
        hit_count=len(provider.hits),
    )


async def assert_outcome(
    clients: AsyncClient,
    provider: FakeProvider,
    response: Response,
    before: Snapshot,
    expect: Expect,
) -> dict:
    """Check the HTTP response, money journal, audit row, and provider hit book."""
    await audit.drain()

    assert response.status_code == expect.status, response.text
    assert response.content == expect.body
    call_id = response.headers.get("X-Treg-Call-Id")
    assert call_id, "every /call response must carry X-Treg-Call-Id"
    assert (response.headers.get("X-Treg-Error") == "1") is expect.treg_error
    if expect.cost_micro is None:
        assert "X-Treg-Cost-Micro" not in response.headers
    else:
        assert response.headers.get("X-Treg-Cost-Micro") == str(expect.cost_micro)

    balance = await _balance(clients)
    assert balance - before.balance_micro == expect.balance_delta
    balance_view = (await clients.get("/orgs")).json()[0]
    detail = await clients.get(f"/orgs/{balance_view['org_id']}/balance")
    assert detail.status_code == 200, detail.text
    assert detail.json()["holds"] == [], "a completed call must not leave an open hold"

    entries = await _entries(clients)
    fresh = [entry for entry in entries if entry["id"] not in before.entry_ids]
    assert tuple(entry["kind"] for entry in fresh) == expect.ledger_kinds
    if expect.ledger_reason is not None:
        release = next(entry for entry in fresh if entry["kind"] == "release")
        assert release["meta"]["reason"] == expect.ledger_reason

    row = await _telemetry(clients)
    assert row["call_ref"] == call_id
    assert row["status_code"] == expect.status
    for key, value in expect.audit.items():
        assert row[key] == value, f"audit {key}: {row[key]!r} != {value!r}"

    assert len(provider.hits) - before.hit_count == expect.upstream_hits
    return row
