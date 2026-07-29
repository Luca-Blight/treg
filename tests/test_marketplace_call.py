"""Direct marketplace calls: `treg call <catalog-endpoint-id>` with no registered tool.

The credential ladder (docs/context/interface/cli-audit-2026-07-28.md): an org tool for the
provider wins (tier 1), else an org credential matching the provider is injected via a virtual,
never-persisted tool (tier 2), else the call fails with the connect/secret fix spelled out
(tier 3). Tier 4 (treg's own metered key) does not exist yet — these tests pin that too, by
asserting tier 3 is a hard stop.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from treg import api as A

EP = "tikhub.tiktok.video.comments"          # GET /api/v1/tiktok/web/fetch_post_comment, aweme_id required
EP_PATH = "/api/v1/tiktok/web/fetch_post_comment"


async def test_tier2_org_credential_no_tool(clients: AsyncClient):
    """A secret NAMED for the provider serves the call — and no tool row appears."""
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    r = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["auth"] == "Bearer MKKEY"                 # injected the provider's way
    assert d["raw_path"] == EP_PATH                     # endpoint id resolved to the real path
    assert d["query"] == {"aweme_id": "7", "count": "5"}
    tools = (await clients.get("/tools")).json()
    assert tools == [], "tier 2 must not materialize a tool row"


async def test_tier2_audits_the_endpoint_id(clients: AsyncClient):
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    await clients.get(f"/call/{EP}?aweme_id=7")
    rows = (await clients.get("/calls")).json()
    assert rows and rows[0]["tool_name"] == EP


async def test_tier1_registered_tool_wins(clients: AsyncClient):
    """An org tool for the provider's host serves the call with ITS binding — the registry
    stays authoritative over the marketplace fallback."""
    sid = (await clients.post("/secrets", json={"name": "own-key", "value": "OWN"})).json()["id"]
    await clients.post("/tools", json={"name": "our-tikhub", "base_url": "https://api.tikhub.io", "secret_id": sid})
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})  # tier-2 bait
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    assert r.json()["auth"] == "Bearer OWN"
    rows = (await clients.get("/calls")).json()
    assert rows[0]["tool_name"] == "our-tikhub"


async def test_tier3_no_credential_is_an_actionable_404(clients: AsyncClient):
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "treg connections connect --provider tikhub" in detail
    assert "treg secret add tikhub" in detail          # tikhub is a pasted-key provider


async def test_missing_required_param_fails_before_any_credential(clients: AsyncClient):
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    r = await clients.get(f"/call/{EP}")
    assert r.status_code == 400
    assert "aweme_id" in r.json()["detail"]


async def test_method_mismatch_is_a_400_hint(clients: AsyncClient):
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    r = await clients.post(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 400
    assert "GET" in r.json()["detail"]


async def test_provider_name_404_points_at_the_marketplace(clients: AsyncClient):
    """`treg call tikhub /path` (no such tool) keeps failing, but no longer dead-ends."""
    r = await clients.get("/call/tikhub/api/v1/foo")
    assert r.status_code == 404
    assert "marketplace provider" in r.json()["detail"]


async def test_unknown_dotted_name_stays_a_plain_404(clients: AsyncClient):
    r = await clients.get("/call/no.such.endpoint")
    assert r.status_code == 404


def test_path_placeholders_fill_from_query_and_are_consumed():
    """Pure-function check: `{placeholder}` path params substitute (URL-encoded) from query
    params and are reported as consumed so the relay drops them from the query string."""
    provider = type("P", (), {"base_url": "https://api.example.com"})()
    ep = {"id": "x.y.z", "path": "/v3/sites/{siteUrl}/query", "input": {}}
    url, consumed = A._marketplace_upstream(ep, provider, {"siteUrl": "sc-domain:ex.com", "row": "1"})
    assert url == "https://api.example.com/v3/sites/sc-domain%3Aex.com/query"
    assert consumed == {"siteUrl"}
    with pytest.raises(HTTPException) as exc:
        A._marketplace_upstream(ep, provider, {})
    assert exc.value.status_code == 400 and "siteUrl" in exc.value.detail


async def test_access_probe_reports_the_tier(clients: AsyncClient):
    r = await clients.get(f"/catalog/endpoints/{EP}/access")
    assert r.status_code == 200 and r.json()["tier"] == "none"
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    assert (await clients.get(f"/catalog/endpoints/{EP}/access")).json()["tier"] == "credential"
    sid = (await clients.post("/secrets", json={"name": "k2", "value": "OWN"})).json()["id"]
    await clients.post("/tools", json={"name": "our-tikhub", "base_url": "https://api.tikhub.io", "secret_id": sid})
    assert (await clients.get(f"/catalog/endpoints/{EP}/access")).json()["tier"] == "tool"


async def test_deny_rules_cover_marketplace_calls(clients: AsyncClient):
    """Policy is evaluated on the RESOLVED upstream — an endpoint-id call can't dodge a host block."""
    await clients.post("/secrets", json={"name": "tikhub", "value": "MKKEY"})
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    r = await clients.post(f"/orgs/{org_id}/deny", json={"host": "api.tikhub.io", "note": "no tikhub"})
    assert r.status_code == 200, r.text
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 403
