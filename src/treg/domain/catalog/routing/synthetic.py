"""The generated `treg.<capability>` catalog rows — never hand-written (plan §5)."""

from __future__ import annotations

from .contracts import Adapter, Contract

ROUTED_KIND = "routed"
# Example values for the generated row's `test_request`, by identity key (the call template reads it).
_EXAMPLE_VALUES = {"full_name": "Patrick Collison", "first_name": "Patrick", "last_name": "Collison",
                   "domain": "stripe.com", "linkedin_url": "https://www.linkedin.com/in/patrickcollison"}
ROUTED_PROVIDER = "treg"


def routed_endpoint(contract: Contract, children: list[dict], adapters: dict[str, Adapter], cost_view) -> dict | None:
    """One row per capability with ≥ 2 verified-adapter children. Price = the children's range."""
    kids = [e for e in children if adapters.get(e["id"]) and adapters[e["id"]].verified and not e.get("status")]
    if len(kids) < 2:
        return None
    prices = sorted(p for p in ((cost_view(e.get("cost"), e["provider"]) or {}).get("usd") for e in kids) if p is not None)
    lo, hi = (prices[0], prices[-1]) if prices else (None, None)
    body = {}
    variants = " | ".join("{" + ", ".join(v) + "}" for v in contract.identity)
    for variant in contract.identity:
        for k in variant:
            body.setdefault(k, {"type": contract.identity_types.get(k, "str"), "required": False,
                                "note": f"identity key (part of {', '.join('+'.join(v) for v in contract.identity if k in v)})"})
    for k, spec in (contract.filters or {}).items():
        body.setdefault(k, {"type": (spec or {}).get("type", "str"), "required": False,
                            "note": f"filter — default {(spec or {}).get('default')!r}" + (f"; {spec.get('note')}" if (spec or {}).get("note") else "")})
    cap = contract.capability
    return {
        "id": f"{ROUTED_PROVIDER}.{cap}",
        "provider": ROUTED_PROVIDER,
        "capability": cap,
        "platform": cap.split(".")[0],
        "domain": "routed",
        "scope": "",
        "kind": ROUTED_KIND,
        "method": "POST",
        "path": f"/{cap}",
        "name": f"{cap} — routed: best of {len(kids)} providers, own keys first",
        "summary": contract.summary,
        "input": {"bodyType": "json", "body": body,
                  "note": (f"Send exactly ONE identity variant: {variants}. "
                           "On a miss treg keeps trying providers, cheapest first, within X-Treg-Route-Max-Cost "
                           f"(default ${contract.default_max_cost_usd or 0.10:g} per call; misses on per-success providers are free). Options ride as "
                           "headers, never in the body: X-Treg-Route-Waterfall: 0 (stop at the first miss), "
                           "X-Treg-Route-Max-Cost: <usd>, X-Treg-Route-Prefer / X-Treg-Route-Exclude: <provider>. The "
                           "response is {output, raw, _treg: {served_by, tried}}; X-Treg-Served-By names the child.")},
        "test_request": {"body": {k: _EXAMPLE_VALUES.get(k, "…") for k in contract.identity[0]}} if contract.identity else {},
        "cost": {"type": "per_success", "value": lo, "currency": "USD", "per": 1, "unit": "call",
                 "source": "inferred", "confidence": "documented", "checked": None,
                 "note": (f"the children's range ${lo:g}–${hi:g} per hit; you pay exactly the child that served, 0% markup"
                          if lo is not None else "children unpriced")},
        "cost_range_usd": [lo, hi],
        "tier": "core",
        "verified": None,
        "miss": {"status": 200, "means": f"output.{contract.miss}"},
        "status": "", "status_note": "", "platform_blocked": "", "superseded_by": "",
        "docs_url": "",
        "example_file": None,
        "routed_children": [e["id"] for e in kids],
    }
