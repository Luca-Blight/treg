"""Every `/call/` refusal leaves a CallRecord stamped `refused_by` — the funnel's early friction.

Before this, a call treg refused BEFORE the relay fell into two buckets: money refusals were
audited (but indistinguishable from a provider's own 402), and everything earlier — bad token,
unknown tool, ACL, deny rule — left no row at all. The 2026-08-12 Hunter incident is the motivating
case: 309 pre-relay refusals read as "Hunter is failing" until the rows were re-derived from
`duration_ms IS NULL`. `refused_by` makes that distinction a column instead of a forensic exercise.
"""

from __future__ import annotations

from httpx import AsyncClient
import pytest
from sqlmodel import select

from treg import api as A
from treg import audit
from treg.db import session_maker
from treg.models import CallRecord


async def _rows() -> list[CallRecord]:
    await audit.drain()  # flush the fire-and-forget writes before reading
    async with session_maker() as db:
        return (await db.execute(select(CallRecord).order_by(CallRecord.id))).scalars().all()


async def _make_echo_tool(clients: AsyncClient, name: str = "echo-tool") -> None:
    s = await clients.post("/secrets", json={"name": f"k-{name}", "value": "sek"})
    await clients.post("/tools", json={"name": name, "base_url": "http://upstream",
                                       "secret_id": s.json()["id"]})


async def test_a_relayed_call_is_not_a_refusal(clients: AsyncClient):
    await _make_echo_tool(clients)
    r = await clients.get("/call/echo-tool/echo?x=1")
    assert r.status_code == 200
    (rec,) = await _rows()
    assert rec.refused_by is None


async def test_unknown_tool_is_recorded_as_a_resolution_refusal(clients: AsyncClient):
    """The pre-refused_by blind spot: a 404 for a tool nobody registered raised before the
    handler's own audit and vanished. The fallback in the exception handler records it."""
    r = await clients.get("/call/no-such-tool/whatever")
    assert r.status_code == 404
    (rec,) = await _rows()
    assert rec.refused_by == "resolution"
    assert rec.tool_name == "no-such-tool"
    assert rec.user_email == "tim@superdesign.dev"  # identity was known — the token was fine
    assert rec.org_id is not None


async def test_bad_token_is_recorded_anonymously_as_an_auth_refusal(clients: AsyncClient):
    """No Caller ever existed, so the row cannot say who — but an anonymous row is still the
    fact that someone knocked with a dead token, which zero rows could never say."""
    r = await clients.get("/call/echo-tool/echo", headers={"X-Treg-Token": "garbage"})
    assert r.status_code == 401
    (rec,) = await _rows()
    assert rec.refused_by == "auth"
    assert rec.org_id is None and rec.user_email == ""


async def test_refusals_reach_the_caller_in_the_audit_log(clients: AsyncClient):
    """`treg audit` is where an org goes to ask "why did my call fail" — the column must be in
    the payload, not only in the table."""
    await clients.get("/call/no-such-tool/whatever")
    await audit.drain()
    (row,) = (await clients.get("/calls")).json()
    assert row["refused_by"] == "resolution"


async def test_retired_catalog_call_is_actionable_audited_and_does_not_shadow_an_own_tool(
    clients: AsyncClient, monkeypatch: pytest.MonkeyPatch,
):
    endpoint = "tikhub.x.linkedin-web-search-people"
    await clients.post("/secrets", json={"name": "tikhub", "value": "provider-key"})

    relayed = False

    async def unexpected_relay(*args, **kwargs):
        nonlocal relayed
        relayed = True
        pytest.fail("a retired catalog row reached the upstream relay")

    real_relay = A.relay
    monkeypatch.setattr(A, "relay", unexpected_relay)
    response = await clients.get(f"/call/{endpoint}")
    assert response.status_code == 410, response.text
    assert response.headers["X-Treg-Error"] == "1"
    assert "collapsed" in response.json()["detail"]
    assert "No replacement is currently catalogued" in response.json()["detail"]
    assert not relayed
    (record,) = await _rows()
    assert record.status_code == 410
    assert record.tool_name == endpoint
    assert record.refused_by == "retired"

    moved = "tikhub.x.linkedin-web-search-jobs"
    replacement = "tikhub.x.linkedin-web-v2-search-jobs"
    moved_response = await clients.get(f"/call/{moved}")
    assert moved_response.status_code == 410
    assert replacement in moved_response.json()["detail"]
    assert [row.refused_by for row in await _rows()] == ["retired", "retired"]

    access = await clients.get(f"/catalog/endpoints/{endpoint}/access")
    assert access.status_code == 410

    # Tier 1 remains authoritative: a team can deliberately register this exact name, and the
    # initial own-tool resolution succeeds before catalog fallback or its retirement gate exists.
    monkeypatch.setattr(A, "relay", real_relay)
    secret = (await clients.post("/secrets", json={"name": "own", "value": "own-key"})).json()
    created = await clients.post("/tools", json={
        "name": endpoint, "base_url": "http://upstream", "secret_id": secret["id"],
    })
    assert created.status_code == 200, created.text
    own = await clients.get(f"/call/{endpoint}")
    assert own.status_code == 200, own.text
    assert own.json()["auth"] == "Bearer own-key"
