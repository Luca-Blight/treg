"""Capability routing — first-party routed endpoints (docs/CAPABILITY-ROUTING-PLAN.md).
`treg.people.email.find` picks a child and runs it through the ordinary call use case."""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import audit
from treg.application.call import service as call_service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.db import session_maker
from treg.domain.catalog import store as catalog_store
from treg.domain.catalog.routing import paths as P
from treg.domain.catalog.routing.contracts import canonical_identity
from treg.domain.catalog.routing.plan import Candidate, cost_at, rank
from treg.models import Hold, LedgerEntry

from test_marketplace_call import _balance, platform_on  # noqa: F401

ROUTED = "treg.people.email.find"


@pytest.fixture
def enrichment_on(monkeypatch, platform_on):
    for p in ("HUNTER", "TOMBA", "LEADMAGIC", "LEADSFORGE", "FINDYMAIL", "AVIATO", "FIBER_AI"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"PLATFORM-{p}-KEY")
    monkeypatch.setenv("TREG_PLATFORM_KEY_TOMBA_SECRET", "PLATFORM-TOMBA-SECRET")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "hunter,tomba,leadmagic,leadsforge,findymail,aviato,fiber-ai")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _relay_by_provider(answers: dict[str, list[tuple[int, dict]]], seen: list):
    """A fake upstream keyed by the vendor host: each provider answers its scripted list in order."""
    async def _relay(request, upstream_url, tool, secrets, client, drop_params=None, force_identity=False):
        provider = next((p for p in answers if p != "*" and p in upstream_url), "*")  # "*": any other vendor
        body = b""
        async for chunk in request.body_stream():
            body += chunk
        seen.append((provider, request.method, dict(request.query_items), json.loads(body) if body else None))
        status, doc = answers[provider].pop(0)
        payload = json.dumps(doc).encode()
        async def _s():
            yield payload
        async def _c():
            return None
        return UpstreamResponse(status, ((b"content-type", b"application/json"),), _s(), _c)
    return _relay


# ---- pure ------------------------------------------------------------------------------------

def test_expression_language():
    doc = {"data": {"email": "a@x.io", "score": 80, "verification": {"status": "valid"}}, "emails": [{"email": "e", "type": "work"}], "none": []}
    assert P.evaluate("data.email", doc) == "a@x.io"
    assert P.evaluate("data.score / 100", doc) == 0.8
    assert P.evaluate("data.verification.status == 'valid'", doc) is True
    assert P.evaluate("data.email == null", doc) is False and P.evaluate("data.missing == null", doc) is True
    assert P.evaluate("none == []", doc) is True and P.evaluate("emails == []", doc) is False
    assert P.evaluate("emails[0].email", doc) == "e" and P.evaluate("emails[3].email", doc) is None
    assert P.evaluate("coalesce(data.missing, data.email)", doc) == "a@x.io"
    assert P.evaluate("split_first(data.name)", {"data": {"name": "Patrick Collison"}}) == "Patrick"
    assert P.evaluate("split_last(data.name)", {"data": {"name": "Patrick"}}) is None
    assert P.evaluate("join(a, b)", {"a": "Patrick", "b": "Collison"}) == "Patrick Collison"
    with pytest.raises(ValueError):
        P.evaluate("nope(a)", doc)


def test_every_shipped_adapter_round_trips_its_fixture():
    cat = catalog_store.load()
    bad = {eid: a.verify_note for eid, a in cat.adapters.items() if not a.verified}
    assert bad == {}, bad
    ep = cat.by_id[ROUTED]
    assert ep["kind"] == "routed" and ep["provider"] == "treg" and len(ep["routed_children"]) >= 8
    assert cat.platform_eligible(ep) and ep["cost_range_usd"][0] < ep["cost_range_usd"][1]
    # a hand-verified round trip on the plan's worked example
    ad = cat.adapters["leadsforge.people.email.find"]
    q, b = ad.to_upstream({"first_name": "Patrick", "last_name": "Collison", "domain": "stripe.com", "full_name": "Patrick Collison"})
    assert b == {"firstName": "Patrick", "lastName": "Collison", "companyDomain": "stripe.com"} and q == {}
    assert ad.from_upstream({"email": "p@stripe.com", "status": "succeeded"}) == {"email": "p@stripe.com", "verified": True}
    assert ad.is_miss({"email": None}) and not ad.is_miss({"email": "x"})


def test_identity_variants_derive_and_never_cross():
    contract = catalog_store.load().contracts["people.email.find"]
    ident, variant = canonical_identity(contract, {"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert variant == ("domain", "full_name") and ident["first_name"] == "Patrick" and ident["last_name"] == "Collison"
    ident, variant = canonical_identity(contract, {"first_name": "Patrick", "last_name": "Collison", "domain": "stripe.com"})
    assert ident["full_name"] == "Patrick Collison"
    ident, variant = canonical_identity(contract, {"linkedin_url": "https://www.linkedin.com/in/x"})
    assert variant == ("linkedin_url",) and "domain" not in ident
    assert canonical_identity(contract, {"full_name": "Patrick Collison"})[1] is None


def test_cost_at_and_ranking_math():
    assert cost_at({"usd": 0.0038, "type": "per_result", "per": 1}, {"limit": 10}) == 38_000
    assert cost_at({"usd": 0.0044, "type": "per_result", "per": 25}, {"limit": 10}) == 110_000, "lusha: 1 credit per 25 rows, minimum 1"
    assert cost_at({"usd": 0.0044, "type": "per_result", "per": 25}, {"limit": 40}) == 220_000
    assert cost_at({"usd": 0.005, "type": "per_call"}, {"limit": 10}) == 5_000
    assert cost_at({"usd": None}, {}) is None
    ep = lambda i, t="per_success": {"id": i, "provider": i.split(".")[0], "cost": {"type": t}}
    a = Candidate(ep("a.x"), None, ("domain",), "platform", 24_500, hit_rate=0.4, ok_rate=None, p50_ms=100, last_ok_days=1)
    b = Candidate(ep("b.x", "per_call"), None, ("domain",), "platform", 20_000, hit_rate=0.8, ok_rate=None, p50_ms=100, last_ok_days=1)
    own = Candidate(ep("c.x"), None, ("domain",), "credential", 0, hit_rate=None, ok_rate=None, p50_ms=None, last_ok_days=None)
    assert a.expected_cost_per_hit == pytest.approx(24_500), "per-success: billed only on a hit → price per hit"
    assert b.expected_cost_per_hit == pytest.approx(25_000), "per-call at 80% hit rate: 20000/0.8"
    assert [c.endpoint["id"] for c in rank([a, b, own])] == ["c.x", "a.x", "b.x"]
    assert [c.endpoint["id"] for c in rank([a, b], prefer=["b"])] == ["b.x", "a.x"]
    assert [c.endpoint["id"] for c in rank([a, b], exclude=["a"])] == ["b.x"]
    a.exhausted = True
    assert [c.endpoint["id"] for c in rank([a, b])] == ["b.x"]


# ---- the call path ---------------------------------------------------------------------------

async def test_routed_call_runs_the_cheapest_child_and_returns_output_raw_and_provenance(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "patrick@stripe.com", "score": 99, "first_name": "Patrick", "last_name": "Collison",
                                    "verification": {"status": "valid"}}})]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["output"] == {"email": "patrick@stripe.com", "confidence": 0.99, "first_name": "Patrick", "last_name": "Collison", "verified": True}
    assert d["raw"]["data"]["score"] == 99, "the winning provider's body, verbatim"
    assert d["_treg"]["served_by"] == "tomba.people.email.find" and d["_treg"]["outcome"] == "hit"
    assert r.headers["X-Treg-Served-By"] == "tomba.people.email.find" and r.headers["X-Treg-Providers-Tried"] == "tomba"
    assert seen == [("tomba", "GET", {"domain": "stripe.com", "full_name": "Patrick Collison"}, None)]
    charged = int(r.headers["X-Treg-Cost-Micro"])
    assert charged == 8_900 and before - await _balance(clients) == charged, "tomba's price, nothing else"
    assert d["_treg"]["charged_micro"] == charged
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []
        entries = (await db.execute(select(LedgerEntry))).scalars().all()
    assert {e.call_id for e in entries if e.kind == "settle"} == {r.headers["X-Treg-Call-Id"] + ":r0"}
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    kinds = {(x["tool_name"], x.get("credential_tier")) for x in rows}
    assert (ROUTED, "routed") in kinds and ("tomba.people.email.find", "platform") in kinds


async def test_error_on_the_first_child_falls_back_to_the_second(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(503, {"error": "down"})],
         "findymail": [(200, {"contact": {"name": "Patrick Collison", "email": "patrick@stripe.com"}})]}, seen))
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [t["outcome"] for t in d["_treg"]["tried"]] == ["error", "hit"]
    assert d["_treg"]["served_by"] == "findymail.search.name" and d["output"]["email"] == "patrick@stripe.com"
    assert r.headers["X-Treg-Providers-Tried"] == "tomba,findymail"
    assert before - await _balance(clients) == 19_800, "the failed child released its hold; only findymail charged"
    assert seen[1] == ("findymail", "POST", {}, {"name": "Patrick Collison", "domain": "stripe.com"})


async def test_waterfall_is_on_by_default_can_be_turned_off_and_respects_max_cost(clients: AsyncClient, enrichment_on, monkeypatch):
    miss_tomba = (200, {"data": {"email": None, "score": None, "verification": {"status": None}}})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"tomba": [miss_tomba]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Nobody Here", "domain": "stripe.com"},
                           headers={"X-Treg-Route-Waterfall": "0"})
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss" and r.json()["output"]["email"] is None
    assert r.headers["X-Treg-Route-Outcome"] == "miss" and len(seen) == 1, "waterfall off: stop at the first miss"
    # waterfall (the default): miss → next cheapest → hit; skips a candidate that would breach the ceiling
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [miss_tomba], "findymail": [(200, {"contact": {"name": "N H", "email": None}})],
         "hunter": [(200, {"data": {"email": "n@stripe.com", "score": 50, "verification": {"status": "valid"}}})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Nobody Here", "domain": "stripe.com"},
                           headers={"X-Treg-Route-Max-Cost": "0.08"})
    assert r.status_code == 200, r.text
    tried = r.json()["_treg"]["tried"]
    assert [t["outcome"] for t in tried] == ["miss", "miss", "hit"] and r.json()["_treg"]["served_by"] == "hunter.people.email.find"
    assert [p for p, *_ in seen] == ["tomba", "findymail", "hunter"]
    assert r.json()["_treg"]["charged_micro"] == 24_500, "misses on per-success providers are free; only the hit is billed"
    assert [t["charged_micro"] for t in tried] == [0, 0, 24_500]
    # a ceiling the third candidate would breach stops the waterfall there
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [miss_tomba], "findymail": [(200, {"contact": {"name": "N H", "email": None}})],
         "hunter": [(200, {"data": {"email": None, "score": None}})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Nobody Here", "domain": "stripe.com"},
                           headers={"X-Treg-Route-Max-Cost": "0.02"})
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss"
    # free misses do not consume the ceiling, but hunter (2.45¢ > 2¢) and everything dearer is skipped
    assert [p for p, *_ in seen] == ["tomba", "findymail"]
    assert all(t["outcome"] in ("miss", "skipped") for t in r.json()["_treg"]["tried"]) and r.json()["_treg"]["charged_micro"] == 0


async def test_max_cost_below_the_cheapest_refuses_before_any_call(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"tomba": [(200, {})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "P C", "domain": "stripe.com"}, headers={"X-Treg-Route-Max-Cost": "0.001"})
    assert r.status_code == 402 and r.json()["detail"]["error"] == "route_max_cost" and seen == []
    async with session_maker() as db:
        assert (await db.execute(select(Hold))).scalars().all() == []


async def test_identity_no_provider_accepts_is_422_naming_variants(clients: AsyncClient, enrichment_on):
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison"})
    assert r.status_code == 422 and r.json()["detail"]["error"] == "identity_incomplete"
    assert ["domain", "full_name"] in r.json()["detail"]["variants"]


async def test_caller_fault_on_a_child_stops_and_own_key_ranks_first(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    await clients.post("/secrets", json={"name": "hunter", "value": "MY-HUNTER-KEY"})  # tier 2 for hunter
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200 and r.json()["_treg"]["served_by"] == "hunter.people.email.find"
    assert r.json()["_treg"]["tier"] == "credential" and await _balance(clients) == before, "own key: first, and free"
    # a vendor 4xx on the child goes on ONLY to providers that bill nothing for a rejected request
    # (per_success / free): tomba is per_success, so it is asked; when it rejects too, the caller gets
    # route_caller_fault naming both — and no paid-per-call provider was ever asked.
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(400, {"errors": [{"details": "bad"}]})], "tomba": [(400, {"error": "bad"})], "*": [(400, {"error": "bad"})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "route_caller_fault", r.text
    assert [p for p, *_ in seen][:2] == ["hunter", "tomba"] and len(seen) == 3, "at most two fallbacks, then the 4xx is the caller's"
    outcomes = {t["endpoint_id"]: t["outcome"] for t in r.json()["detail"]["tried"]}
    assert outcomes["hunter.people.email.find"] == "error" and outcomes["tomba.people.email.find"] == "error"
    # a scraper's "please retry" 400 (tikhub, live 2026-08-28) is why: the next free-on-failure provider answers
    seen.clear()
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(400, {"errors": [{"details": "bad"}]})],
         "tomba": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200 and r.json()["_treg"]["served_by"] == "tomba.people.email.find", r.text


async def test_catalog_get_on_the_routed_endpoint_shows_the_plan(clients: AsyncClient, enrichment_on):
    r = await clients.get(f"/catalog/endpoints/{ROUTED}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["endpoint"]["kind"] == "routed" and d["routing"]["contract"]["identity"]
    plan = d["routing"]["plan"]
    assert plan and plan[0]["usd"] <= plan[-1]["usd"] and plan[0]["accepts"]
    assert "hit_rate" not in plan[0], "unmeasured says nothing rather than nulls"
    assert {c["endpoint_id"] for c in plan} <= set(d["endpoint"]["routed_children"] if "routed_children" in d["endpoint"] else [c["endpoint_id"] for c in plan])


async def test_idempotent_replay_of_a_routed_call_never_calls_a_provider_twice(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    h = {"Idempotency-Key": "route-1"}
    r1 = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"}, headers=h)
    r2 = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"}, headers=h)
    assert r1.status_code == 200 and r2.status_code == 200 and r2.headers.get("X-Treg-Idempotent-Replay") == "true"
    assert r2.json() == r1.json() and len(seen) == 1


def test_a_per_success_miss_settles_at_zero_when_the_adapter_can_tell():
    """Live 2026-08-28: the first waterfall charged tomba, findymail and leadsforge for misses the
    catalog calls free. The adapter's `miss` predicate is the missing knowledge."""
    from test_marketplace_call import _mk
    from treg.application.call import settle as A
    miss_tomba = b'{"data": {"email": null, "score": null, "first_name": "Z", "verification": {"status": null}}}'
    assert A._observed_cost_micro(_mk("tomba", endpoint_id="tomba.people.email.find", cost_type="per_success"), miss_tomba) == 0
    hit_tomba = b'{"data": {"email": "z@x.io", "score": 90}}'
    assert A._observed_cost_micro(_mk("tomba", endpoint_id="tomba.people.email.find", cost_type="per_success"), hit_tomba) is None, "a hit still settles at the estimate"
    assert A._observed_cost_micro(_mk("findymail", endpoint_id="findymail.search.name", cost_type="per_success"), b'{"contact": {"email": null}}') == 0
    assert A._observed_cost_micro(_mk("leadsforge", endpoint_id="leadsforge.people.email.find", cost_type="per_success"), b'{"email": null, "status": "failed"}') == 0
    assert A._observed_cost_micro(_mk("leadsforge", endpoint_id="leadsforge.people.email.find", cost_type="per_call"), b'{"email": null}') is None, "per_call bills the call"
    assert A._observed_cost_micro(_mk("tomba", endpoint_id="tomba.companies.emails.count", cost_type="per_success"), b'{"data": {}}') is None, "no adapter → no opinion"


async def test_discovery_puts_the_routed_parent_first_and_its_children_under_it(clients: AsyncClient):
    r = await clients.get("/catalog/search", params={"q": "find work email"})
    rows = r.json()["results"]
    ids = [x["id"] for x in rows]
    parent = ids.index(ROUTED)
    kids = [i for i, x in enumerate(rows) if x["capability"] == "people.email.find" and x["id"] != ROUTED]
    assert kids and parent < min(kids), "the routed parent leads its capability group"
    assert kids == list(range(parent + 1, parent + 1 + len(kids))), "children sit right under the parent"
    assert rows[parent]["routed_children"] and any("ROUTED" in h for h in r.json()["hints"])
    p = await clients.get("/catalog/platforms/people")
    group = next(c for c in p.json()["capabilities"] if c["id"] == "people.email.find")
    assert group["endpoints"][0]["id"] == ROUTED
    from treg.domain.catalog.store import group_routed
    plain = [{"id": "a", "capability": "x", "kind": "data"}, {"id": "b", "capability": "y", "kind": "data"}]
    assert group_routed(plain) == plain, "no routed row → order untouched"


async def test_hit_verdict_is_recorded_and_becomes_a_hit_rate(clients: AsyncClient, enrichment_on, monkeypatch):
    from treg.domain.catalog import stats
    from treg.models import CallRecord
    hit = (200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})
    miss = (200, {"data": {"email": None, "score": None, "verification": {"status": None}}})
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider({"tomba": [hit, miss, hit]}, seen))
    for _ in range(3):
        assert (await clients.get("/call/tomba.people.email.find?full_name=P%20C&domain=stripe.com")).status_code == 200
    await audit.drain()
    async with session_maker() as db:
        rows = (await db.execute(select(CallRecord).where(CallRecord.endpoint_id == "tomba.people.email.find"))).scalars().all()
        assert sorted(r.hit for r in rows) == [False, True, True], "the verdict, never the body"
        # below the floor → None; the floor is about evidence, not a bug
        assert (await stats.observed(db, ["tomba.people.email.find"]))["tomba.people.email.find"]["hit_rate"] is None
        monkeypatch.setattr(stats, "MIN_HIT_SAMPLES", 3)
        s = (await stats.observed(db, ["tomba.people.email.find"], per_success={"tomba.people.email.find"}))["tomba.people.email.find"]
        assert s["hit_rate"] == pytest.approx(2 / 3, abs=1e-3) and s["hit_samples"] == 3
        # historical rows without a verdict: a per-success 2xx with cost_observed 0 is a miss, > 0 a hit
        for r in rows:
            r.hit = None
            r.cost_observed_micro = 8_900 if r.status_code == 200 and "x" else 0
        rows[0].cost_observed_micro = 0
        await db.commit()
        s = (await stats.observed(db, ["tomba.people.email.find"], per_success={"tomba.people.email.find"}))["tomba.people.email.find"]
        assert s["hit_samples"] == 3 and s["hit_rate"] == pytest.approx(2 / 3, abs=1e-3)
        s = (await stats.observed(db, ["tomba.people.email.find"]))["tomba.people.email.find"]
        assert s["hit_samples"] == 0, "the zero-cost fallback applies to per-success endpoints only"
    # the plan reads it: with a measured hit rate the confidence flips from unmeasured to measured
    monkeypatch.setattr(stats, "MIN_HIT_SAMPLES", 3)
    r = await clients.get(f"/catalog/endpoints/{ROUTED}")
    tomba = next(c for c in r.json()["routing"]["plan"] if c["endpoint_id"] == "tomba.people.email.find")
    assert tomba["hit_rate"] == pytest.approx(2 / 3, abs=1e-3) and tomba["usd_per_hit"] == pytest.approx(0.0089, abs=1e-4)


async def test_a_registered_tool_for_a_provider_ranks_first_and_is_free(clients: AsyncClient, enrichment_on, monkeypatch):
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"hunter": [(200, {"data": {"email": "p@stripe.com", "score": 90, "verification": {"status": "valid"}}})]}, seen))
    sid = (await clients.post("/secrets", json={"name": "my-hunter", "value": "OWN-HUNTER"})).json()["id"]
    r = await clients.post("/tools", json={"name": "our-hunter", "base_url": "https://api.hunter.io/v2", "secret_id": sid})
    assert r.status_code == 200, r.text
    before = await _balance(clients)
    r = await clients.post(f"/call/{ROUTED}", json={"full_name": "Patrick Collison", "domain": "stripe.com"})
    assert r.status_code == 200, r.text
    assert r.json()["_treg"]["served_by"] == "hunter.people.email.find" and r.json()["_treg"]["tier"] == "tool"
    assert await _balance(clients) == before and r.json()["_treg"]["charged_micro"] == 0


async def test_mcp_search_shows_the_routed_parent_first_with_its_children(clients: AsyncClient):
    from treg import mcp as M
    out = await M._catalog_search_impl("find work email", 12, surface=M._TEAM_SURFACE) if "surface" in M._catalog_search_impl.__code__.co_varnames else await M._catalog_search_impl("find work email", 12)
    ids = [r["endpoint_id"] for r in out["results"]]
    parent = ids.index(ROUTED)
    kids = [i for i, r in enumerate(out["results"]) if r["provider"] != "treg" and r["endpoint_id"].split(".", 1)[1] in ("people.email.find", "people.email.find.linkedin", "search.name")]
    assert kids and parent < min(kids)
    assert out["results"][parent]["routed"].startswith("treg picks among")


def test_filters_reach_adapters_through_in_expr_and_array_bodies():
    cat = catalog_store.load()
    contract = cat.contracts["google.keywords.ideas"]
    req, variant = canonical_identity(contract, {"keyword": "coffee"})
    assert variant == ("keyword",) and req["country"] == "us" and req["limit"] == 20, "filter defaults ride with the identity"
    req, _ = canonical_identity(contract, {"keyword": "coffee", "country": "GB", "limit": 5})
    q, b = cat.adapters["dataforseo.google.keywords.ideas"].to_upstream(req)
    assert b == [{"keyword": "coffee", "location_code": 2826, "language_code": "en", "limit": 5}], "task list body, GB → 2826"
    q, b = cat.adapters["seranking.google.keywords.ideas"].to_upstream(req)
    assert q == {"keyword": "coffee", "source": "uk", "limit": "5"}
    q, b = cat.adapters["serpapi.google.keywords.ideas"].to_upstream(req)
    assert q == {"q": "coffee", "gl": "gb", "hl": "en", "engine": "google_autocomplete"}
    q, b = cat.adapters["tomba.people.email.verify"].to_upstream({"email": "a@b.io"})
    assert q == {"email": "a@b.io"}, "a pathParams target travels as a query value the proxy folds into the path"
    assert cost_at({"usd": 0.00179, "type": "per_result", "per": 1}, req) == 8_950, "priced at the requested limit"
    ep = cat.by_id["treg.google.keywords.ideas"]
    assert ep["input"]["body"]["country"]["note"].startswith("filter — default 'us'")


async def test_a_keyless_provider_is_dropped_at_planning_not_failed_at_call_time(clients: AsyncClient, enrichment_on, monkeypatch):
    """Live 2026-08-28: exa is platform-eligible but this deployment held no exa key; the child's
    'no credential' 404 aborted the routed call. Planning must drop it and name why."""
    from treg.application.call.route import RouteOptions, build_plan
    cat = catalog_store.load()
    ep = cat.by_id["treg.people.email.find"]
    monkeypatch.setenv("TREG_PLATFORM_KEY_AVIATO", "")  # aviato stays eligible, but keyless
    get_settings.cache_clear()
    class _Org: id = 1
    class _Caller: org_id = 1; org = _Org()
    plan = await build_plan(ep, {"linkedin_url": "https://www.linkedin.com/in/x"}, _Caller(), RouteOptions.from_headers(lambda k: None))
    assert "aviato.people.email.find" not in [c.endpoint["id"] for c in plan.candidates]
    assert any(d["endpoint_id"] == "aviato.people.email.find" and "no aviato key" in d["why"] for d in plan.dropped)


def test_a_contract_may_set_its_own_default_ceiling():
    from treg.application.call.route import RouteOptions, DEFAULT_MAX_COST_MICRO
    cat = catalog_store.load()
    assert cat.contracts["people.search"].default_max_cost_usd is None, "the $1 default covers every current ladder"
    assert RouteOptions.from_headers(lambda k: None, 500_000).max_cost_micro == 500_000
    assert RouteOptions.from_headers(lambda k: None).max_cost_micro == DEFAULT_MAX_COST_MICRO
    assert RouteOptions.from_headers(lambda k: "0.02" if k == "x-treg-route-max-cost" else None, 500_000).max_cost_micro == 20_000


async def test_routed_call_and_access_name_the_providers_dropped_for_this_deployment(clients: AsyncClient, enrichment_on, monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_AVIATO", "")
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(call_service, "relay", _relay_by_provider(
        {"tomba": [(200, {"data": {"email": None, "score": None, "verification": {"status": None}}})],
         "findymail": [(200, {"contact": {"name": "x", "email": None}})],
         "leadsforge": [(200, {"email": None, "status": "failed"})]}, seen))
    r = await clients.post(f"/call/{ROUTED}", json={"linkedin_url": "https://www.linkedin.com/in/x"}, headers={"X-Treg-Route-Max-Cost": "0.03"})
    assert r.status_code == 200 and r.json()["_treg"]["outcome"] == "miss"
    dropped = r.json()["_treg"]["dropped"]
    assert any(d["endpoint_id"] == "aviato.people.email.find" and "no aviato key" in d["why"] for d in dropped)
    hunter = next(d for d in dropped if d["endpoint_id"] == "hunter.people.email.find")
    assert hunter == {"endpoint_id": "hunter.people.email.find", "why": "needs {domain, full_name} | {domain, first_name, last_name}"}
    a = await clients.get(f"/catalog/endpoints/{ROUTED}/access")
    assert a.status_code == 200 and a.json()["tier"] == "routed" and a.json()["detail"].startswith("routed — ")
    assert "aviato.people.email.find" in a.json()["detail"]


def test_a_caller_may_send_everything_it_knows_and_each_provider_gets_only_its_variant():
    cat = catalog_store.load()
    contract = cat.contracts["people.phone.find"]
    everything = {"email": "p@stripe.com", "linkedin_url": "https://www.linkedin.com/in/p", "full_name": "Patrick Collison", "domain": "stripe.com"}
    ident, variant = canonical_identity(contract, everything)
    assert ident["first_name"] == "Patrick"
    from treg.domain.catalog.routing.contracts import adapter_accepts
    tomba = cat.adapters["tomba.people.phone.find"]
    v = adapter_accepts(tomba, ident)
    q, b = tomba.to_upstream(ident, v)
    assert q == {"email": "p@stripe.com"}, "tomba insists on exactly one identifier — only the matched variant is sent"
    lf = cat.adapters["leadsforge.people.phone.find"]
    q, b = lf.to_upstream(ident, adapter_accepts(lf, ident))
    assert b == {"firstName": "Patrick", "lastName": "Collison", "companyDomain": "stripe.com"}, "derived names, and not the LinkedIn URL"
    # filters always travel, whatever the variant
    kw = cat.contracts["google.keywords.ideas"]
    req, v = canonical_identity(kw, {"keyword": "coffee", "country": "de"})
    q, b = cat.adapters["seranking.google.keywords.ideas"].to_upstream(req, v)
    assert q == {"keyword": "coffee", "source": "de", "limit": "20"}
    # every adapter still verifies with the change
    assert all(a.verified for a in cat.adapters.values())
