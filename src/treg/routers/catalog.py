"""Open endpoint-Catalog JSON routes."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit, catalog_store, endpoint_stats, oauth_providers
from ..db import get_session


# The app alias preserves the moved handlers' original @app.get decorator text byte-for-byte.
app = APIRouter()
public_router = app

# ---- endpoint catalog (what you can DO once connected) ------------------------------------
# The credential registry above answers "how do I connect Moz?"; these answer "and then what can I
# call?" — platform-grouped operations with cost, verification date and captured example responses.
# Open like /providers.json: the data is curated public documentation, no org's anything is in it.
# See docs/context/architecture/catalog.md.
def _provider_display(service: str) -> str:
    p = oauth_providers.get(service)
    return p.display_name if p else service


def _platform_rows() -> list[dict]:
    """The platform shelves, busiest first — one builder shared by the JSON route below and the
    server-rendered /catalog page, so the two can never disagree about what is on the shelf."""
    cat = catalog_store.load()
    rows = []
    for slug, plat in cat.platforms.items():
        # The census counts the BROWSE surface only: account/utility ("management") endpoints are
        # real inventory but they are not what a marketplace tile advertises, so they never inflate
        # the endpoint/capability/verified counts or the "from …" price. They still ship in the
        # platform-detail list (with `kind` set) — see catalog_platform's ?include_hidden.
        eps = [e for e in cat.for_platform(slug) if e["kind"] not in catalog_store.HIDDEN_KINDS]
        if not eps:  # a taxonomy entry no provider implements (or only plumbing) is grid noise
            continue
        rows.append({
            "slug": slug,
            "label": plat["label"],
            "category": plat["category"],
            "featured": plat.get("featured"),  # rank within its category's Featured shelf; null = not featured
            "summary": plat.get("summary", ""),
            # cheapest priced endpoint, for the card's "from …" corner. Ordering compares the
            # server-computed USD figure, so CNY rows and provider-credit rows sort against dollar
            # rows honestly; the ORIGINAL value+currency rides along for display.
            # cheapest PAID option — zero-cost utility routes (rate-card freebies) would otherwise
            # advertise a misleading "from $0"; genuinely free own-account access is signaled by the
            # UI separately when a platform has only free endpoints (price_from stays null).
            "price_from": min(
                (c for e in eps if (c := cat.cost_view(e.get("cost"), e.get("provider"))) and c["usd"]),
                key=lambda c: c["usd"],
                default=None,
            ),
            "capabilities": len({e["capability"] for e in eps if e["capability"]}),
            "endpoints": len(eps),
            "verified": len([e for e in eps if e["verified"]]),
            "providers": sorted({e["provider"] for e in eps}),
        })
    rows.sort(key=lambda r: (-r["endpoints"], r["slug"]))
    return rows


@app.get("/catalog/platforms")
async def catalog_platforms() -> dict:
    """Open: the platform shelves of the endpoint catalog, busiest first."""
    return {"platforms": _platform_rows(), "generated_from": "catalog"}


@app.get("/catalog/platforms/{slug}")
async def catalog_platform(slug: str, include_hidden: int = 0) -> dict:
    """Open: one platform's operations, grouped by capability so the same job across providers sits
    on one row — that grouping is what makes comparison (and a future failover router) possible.

    By default the account/utility ("management") endpoints are dropped from every shape below — the
    browse view is data + action. `?include_hidden=1` returns the whole surface (each endpoint still
    carries `kind`, so a client can file the plumbing behind an expander); `hidden_count` always
    reports how many were set aside so a caller can label that expander without a second request."""
    cat = catalog_store.load()
    eps = cat.for_platform(slug)
    if not eps:
        raise HTTPException(status_code=404, detail=f"unknown platform {slug!r}")
    hidden_count = len([e for e in eps if e["kind"] in catalog_store.HIDDEN_KINDS])
    if not include_hidden:
        eps = [e for e in eps if e["kind"] not in catalog_store.HIDDEN_KINDS]
    grouped: dict[str, list[dict]] = {}
    extended: list[dict] = []
    pairs: list[tuple[dict, dict]] = []
    for ep in eps:
        view = catalog_store.endpoint_view(ep, _provider_display(ep["provider"]), cat)
        pairs.append((ep, view))
        if ep["capability"]:
            grouped.setdefault(ep["capability"], []).append(view)
        else:
            extended.append(view)
    for views in grouped.values():
        # routed parent first, then core before mapped-extended, verified before not — same
        # convention as search ranking
        views.sort(key=lambda v: (v.get("kind") != "routed", v["tier"] != "core", not v["verified"], v["id"]))
    return {
        "platform": {"slug": slug,
                     "label": cat.platforms.get(slug, {}).get("label", slug),
                     "category": cat.platforms.get(slug, {}).get("category", "Other")},
        # The capability grouping `treg catalog` renders. The dashboard reads `domains` below, but
        # this is the shape the CLI has been written against since the catalog shipped, and the same
        # endpoints appear in both — a client picks the axis it wants, neither is a subset.
        "capabilities": [
            {"id": cap, "description": cat.capabilities.get(cap, ""), "endpoints": grouped[cap]}
            for cap in sorted(grouped)
        ],
        "extended": extended,
        # account/utility endpoints set aside — how many, so the page can label its expander. When
        # ?include_hidden=1 they are already folded into the shapes above (tagged by `kind`).
        "hidden_count": hidden_count,
        # The ledger the platform page renders: sections by subject, ordered and merged server-side
        # so every client shows the same page (see `catalog_store.domain_rows`).
        "domains": catalog_store.domain_rows(pairs, cat.capabilities),
        # Provider-wide facts (limits, pricing page, docs), once per provider rather than copied onto
        # every row — an expanded endpoint needs them and shouldn't cost a second request.
        "providers": {
            service: {"service": service, "display_name": _provider_display(service),
                      **cat.provider_meta.get(service, {})}
            for service in sorted({ep["provider"] for ep in eps})
        },
    }


def _per_success(cat, endpoint_ids: list[str]) -> set[str]:
    return {i for i in endpoint_ids if ((cat.by_id.get(i) or {}).get("cost") or {}).get("type") == "per_success"}


async def _observed_or_empty(db: AsyncSession, endpoint_ids: list[str]) -> dict[str, dict]:
    """What the served calls say about these endpoints — or `{}` if that query is unavailable.

    Telemetry must never take the catalog down: the catalog answers signed-out readers and is the
    step every agent starts from, while these numbers are an enrichment on top of it.
    """
    try:
        return await endpoint_stats.observed(db, endpoint_ids, per_success=_per_success(catalog_store.load(), endpoint_ids))
    except Exception:  # noqa: BLE001
        logging.getLogger("treg.catalog").warning("endpoint stats unavailable", exc_info=True)
        return {}


@app.get("/catalog/search")
async def catalog_search(q: str = "", limit: int = 25,
                         db: AsyncSession = Depends(get_session)) -> dict:
    """Open: free-text search across the whole catalog — the DISCOVER half of the loop.

    An agent that knows what it wants ("tiktok comments") shouldn't have to guess which platform
    shelf hides it. Ranking is plain token matching (see `catalog_store.search`) so results are
    reproducible and explainable; equal scores — the common case, not the edge — then break on what
    treg has MEASURED and on price, so the cut stops being file order. `hints` carries the next
    command, since finding the endpoint is never the goal — inspecting or calling it is."""
    cat = catalog_store.load()
    limit = max(1, min(limit, 100))
    ranked, total, tie_truncated = catalog_store.rank_band(q, cat, limit)
    stats = await _observed_or_empty(db, [ep["id"] for ep, _ in ranked])
    ranked = catalog_store.rerank(ranked, stats, cat)[:limit]
    results = [
        catalog_store.endpoint_view(ep, _provider_display(ep["provider"]), cat)
        | catalog_store.endpoint_context(ep, cat)
        # The evidence that decided the order, shown rather than merely applied: a caller comparing
        # two rows should be able to see WHY one is above the other.
        | {"score": score, "observed": stats.get(ep["id"])}
        for ep, score in ranked
    ]
    results = catalog_store.group_routed(results)
    if not q.strip():
        hints = ["pass ?q= — e.g. /catalog/search?q=tiktok+comments"]
    elif not results:
        # The miss IS the signal: log it (fire-and-forget, see models.SearchMiss) so the queries the
        # catalog couldn't answer surface in the usage report next to the ToolRequests they rarely
        # become. One source for this whole route — web, CLI and raw API all arrive here, and
        # guessing which from headers would be a made-up column.
        audit.record_search_miss(query=q.strip(), source="api")
        hints = [f"nothing matches {q!r} closely enough — try different task words, or browse `treg catalog` for the platform shelves",
                 "still missing? POST /tool-requests {\"capability\": \"<what you need>\"} — "
                 "requests steer which provider gets added next"]
    else:
        hints = [f"treg catalog get {results[0]['id']}   # params, cost and an example response",
                 f"{catalog_store.call_template(cat.by_id.get(results[0]['id'], ranked[0][0]))}   # run it — key injected server-side"]
        routed_row = next((r for r in results if r.get("kind") == "routed"), None)
        if routed_row is not None:
            hints.insert(1, f"{routed_row['id']} is ROUTED: treg picks among {len(routed_row.get('routed_children') or [])} "
                            f"providers (own keys first, then cheapest per hit) and names the one that served; "
                            f"call a child id to choose the provider yourself")
        if total > len(results):
            hints.append(f"{total - len(results)} more matches — raise limit (max 100)")
        if tie_truncated:
            # No silent caps. Every row here scored the same, more of them scored the same than the
            # evidence sort was allowed to weigh, so the tail of this list is back to being ordered
            # by nothing in particular — say so instead of letting it read as a ranked answer.
            hints.append(f"{q!r} matches too broadly to rank on measured reliability past the first "
                         f"{catalog_store.RERANK_BAND} equally-scoring rows — add a word to narrow it")
    out = {"query": q, "count": len(results), "total": total, "results": results, "hints": hints}
    if not results and q.strip():
        # the rows that JUST missed the admission gate and which words they missed — an agent (or
        # the CLI display) turns this straight into the corrected query
        near = catalog_store.near_misses(q, cat)
        if near:
            out["near"] = near
            first = near[0]
            hints.insert(1, f"nearest: {first['endpoint_id']} matches "
                            f"{', '.join(first['matches'])} but not {', '.join(first['missing'])}")
    return out


@app.get("/catalog/endpoints/{endpoint_id}")
async def catalog_endpoint(endpoint_id: str, db: AsyncSession = Depends(get_session)) -> dict:
    """Open: everything about ONE endpoint — the INSPECT half of the loop.

    Deliberately one round-trip: params, cost, the sibling providers offering the same capability
    (so a price/verification comparison needs no second call), a paste-ready `call_template`, and the
    captured example response inline — an agent shouldn't have to fetch /catalog/examples to learn
    the response shape it is about to parse."""
    cat = catalog_store.load()
    ep = cat.by_id.get(endpoint_id)
    if ep is None:
        # Name the near misses. An id that is one segment off is the common miss, and a bare 404
        # ends the search → get → call loop at its first step with nothing to try next.
        raise HTTPException(status_code=404, detail={
            "error": f"unknown endpoint {endpoint_id!r}",
            "hint": catalog_store.unknown_id_hint(endpoint_id, cat),
            "did_you_mean": catalog_store.near_ids(endpoint_id, cat)})
    view = (catalog_store.endpoint_view(ep, _provider_display(ep["provider"]), cat)
            | catalog_store.endpoint_context(ep, cat))
    siblings = [
        catalog_store.endpoint_view(other, _provider_display(other["provider"]), cat)
        for other in sorted(cat.for_capability(ep["capability"]), key=lambda e: e["id"])
        if other["id"] != ep["id"]
    ]
    example = None
    path = catalog_store.example_path(endpoint_id)
    if path is not None and path.is_file():
        try:
            example = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            example = None
    # What the calls we have already served say about this endpoint and its alternatives — the half
    # of "compare providers" that only treg can answer (see endpoint_stats + CAPABILITY-CHOICE-PLAN).
    # Attached to the SAME response because the choice is made here; a second round-trip to compare
    # reliability is a round-trip an agent will skip.
    stats = await _observed_or_empty(db, [endpoint_id] + [s["id"] for s in siblings])
    view = view | {"observed": stats.get(endpoint_id)}
    siblings = [s | {"observed": stats.get(s["id"])} for s in siblings]

    routing = None
    if ep.get("kind") == "routed":
        # The QUOTE (routing plan §3.5): the contract and the ranked children on treg's key, priced
        # at a one-row lookup. Own keys rank first at call time (this route is open, so it cannot
        # know the caller's); nothing is reserved here.
        from ..domain.catalog.routing.plan import Candidate, cost_at, rank
        contract = cat.contracts.get(ep["capability"])
        kids = [cat.by_id[i] for i in ep.get("routed_children") or [] if i in cat.by_id]
        cands = []
        for k in kids:
            st = stats.get(k["id"]) or {}
            ad = cat.adapters.get(k["id"])
            cands.append(Candidate(k, ad, ad.accepts[0] if ad and ad.accepts else (), "platform",
                                   cost_at(cat.cost_view(k.get("cost"), k["provider"]), {}), st.get("hit_rate"),
                                   st.get("ok_rate"), st.get("p50_ms"), st.get("last_ok_days")))
        routing = {
            "contract": {"identity": [list(v) for v in contract.identity], "output": contract.output,
                         "miss": contract.miss, "derive": contract.derive} if contract else None,
            "plan": [c.view() | {"accepts": [list(v) for v in c.adapter.accepts]} for c in rank(cands)],
            "options": {"X-Treg-Route-Waterfall": "1 = keep trying on a miss (opt-in; multiplies cost)",
                        "X-Treg-Route-Max-Cost": "USD ceiling for the whole call",
                        "X-Treg-Route-Prefer": "provider[,provider]", "X-Treg-Route-Exclude": "provider[,provider]"},
        }
    return {
        "endpoint": view | ({"routed_children": ep.get("routed_children")} if ep.get("kind") == "routed" else {}),
        "provider": {"service": ep["provider"], "display_name": _provider_display(ep["provider"]),
                     **cat.provider_meta.get(ep["provider"], {})},
        "siblings": siblings,
        **({"routing": routing} if routing is not None else {}),
        "call_template": catalog_store.call_template(ep),
        "example_response": example,
        "hints": [f"{catalog_store.call_template(ep)}   # run it — key injected server-side"]
                 + ([f"treg catalog get {siblings[0]['id']}   # the same job from {siblings[0]['provider']}"]
                    if siblings else []),
    }


@app.get("/catalog/examples/{endpoint_id}", include_in_schema=False)
async def catalog_example(endpoint_id: str) -> Response:
    """Open: the captured response of one endpoint, as recorded by scripts/catalog_verify.py.

    The id is resolved through the loaded catalog BEFORE any path is built, so raw input never
    reaches the filesystem. The files are truncated + PII-scrubbed public data (see the fragment)."""
    path = catalog_store.example_path(endpoint_id)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail=f"no example response for {endpoint_id!r}")
    return Response(content=path.read_bytes(), media_type="application/json")
