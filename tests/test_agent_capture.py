"""Runtime attribution + the observed-agents roster.

The treg CLI reports which coding agent it runs inside (X-Treg-Client). The registry stamps it on
the audit trail and derives "detected agents" from it: one row per (member, runtime) — the
zero-setup half of the agents story. Attribution, never authentication: nothing may GATE on it.

Proves: the header lands on CallRecord.client (normalized, versions stripped, junk discarded);
/agents/observed groups by member × runtime and excludes plain-terminal + machine traffic; and the
endpoint is admin-only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from conftest import make_upstream

from treg import crypto
from treg.api import app
from treg.db import reset_db, session_maker
from treg.models import CallRecord, Membership, Org, User


def _h(t: str, client: str | None = None) -> dict:
    h = {"X-Treg-Token": t}
    if client is not None:
        h["X-Treg-Client"] = client
    return h


async def _mint(email: str, org_id: int, role: str) -> tuple[str, int]:
    token = crypto.new_token()
    async with session_maker() as s:
        u = (await s.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if u is None:
            u = User(email=email); s.add(u); await s.flush()
        s.add(Membership(user_id=u.id, org_id=org_id, role=role, token_hash=crypto.hash_token(token)))
        await s.commit()
        uid = u.id
    return token, uid


async def _drain_audit() -> None:
    """audit writes are fire-and-forget tasks — let them land before asserting."""
    import asyncio
    from treg import audit
    while audit._pending:
        await asyncio.sleep(0)


@pytest.fixture
async def env():
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        async with session_maker() as s:
            org = Org(name="Team", slug="team"); s.add(org); await s.commit(); await s.refresh(org)
            org_id = org.id
        owner, _ = await _mint("owner@x.dev", org_id, "owner")
        member, member_uid = await _mint("m@x.dev", org_id, "member")
        sid = (await c.post("/secrets", headers=_h(owner), json={"name": "k", "value": "v"})).json()["id"]
        await c.post("/tools", headers=_h(owner),
                     json={"name": "alpha", "base_url": "http://upstream", "secret_id": sid})
        yield SimpleNamespace(c=c, org_id=org_id, owner=owner, member=member, member_uid=member_uid)
    await app.state.http.aclose()


# ---- the stamp ---------------------------------------------------------------------------------
async def test_client_header_lands_on_the_audit_row(env):
    r = await env.c.get("/call/alpha/ok", headers=_h(env.member, "claude-code"))
    assert r.status_code == 200
    await _drain_audit()
    async with session_maker() as s:
        rec = (await s.execute(select(CallRecord))).scalars().one()
        assert rec.client == "claude-code" and rec.user_email == "m@x.dev"


async def test_client_is_normalized_and_junk_is_discarded(env):
    for sent, stored in (("Claude-Code/1.2.3", "claude-code"), ("weird agent!!", "weirdagent"),
                         (None, "")):
        await env.c.get("/call/alpha/ok", headers=_h(env.member, sent))
    await _drain_audit()
    async with session_maker() as s:
        got = {r.client for r in (await s.execute(select(CallRecord))).scalars().all()}
    assert got == {"claude-code", "weirdagent", ""}


# ---- the roster --------------------------------------------------------------------------------
async def test_observed_groups_by_member_and_runtime(env):
    for client in ("claude-code", "claude-code", "codex"):
        await env.c.get("/call/alpha/ok", headers=_h(env.member, client))
    await env.c.get("/call/alpha/ok", headers=_h(env.owner, "claude-code"))
    await _drain_audit()
    rows = (await env.c.get(f"/orgs/{env.org_id}/agents/observed", headers=_h(env.owner))).json()
    key = {(r["member"], r["client"]): r for r in rows}
    assert set(key) == {("m@x.dev", "claude-code"), ("m@x.dev", "codex"),
                        ("owner@x.dev", "claude-code")}
    assert key[("m@x.dev", "claude-code")]["calls_30d"] == 2
    assert key[("m@x.dev", "claude-code")]["used_today"] == 2


async def test_plain_terminal_and_unreported_stay_out(env):
    """`cli` (a human at a prompt) and '' (an SDK or old CLI) would list every member twice."""
    await env.c.get("/call/alpha/ok", headers=_h(env.member, "cli"))
    await env.c.get("/call/alpha/ok", headers=_h(env.member))
    await _drain_audit()
    rows = (await env.c.get(f"/orgs/{env.org_id}/agents/observed", headers=_h(env.owner))).json()
    assert rows == []


async def test_minted_agents_stay_out_of_the_observed_roster(env):
    """A machine identity's calls are already attributed to itself — listing it as 'detected'
    would suggest promoting an agent that already has a token."""
    made = await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner),
                            json={"name": "ci-bot"})
    await env.c.get("/call/alpha/ok", headers=_h(made.json()["token"], "claude-code"))
    await _drain_audit()
    rows = (await env.c.get(f"/orgs/{env.org_id}/agents/observed", headers=_h(env.owner))).json()
    assert rows == []


async def test_observed_is_admin_only(env):
    r = await env.c.get(f"/orgs/{env.org_id}/agents/observed", headers=_h(env.member))
    assert r.status_code == 403


async def test_minted_agent_records_its_creator(env):
    await env.c.post(f"/orgs/{env.org_id}/agents", headers=_h(env.owner), json={"name": "ci-bot"})
    listed = (await env.c.get(f"/orgs/{env.org_id}/agents", headers=_h(env.owner))).json()
    assert listed[0]["created_by"] == "owner@x.dev"
