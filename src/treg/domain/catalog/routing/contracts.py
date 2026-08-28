"""Contracts and adapters: parsing, identity matching, fixture verification."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths as P


@dataclass(frozen=True)
class Contract:
    capability: str
    summary: str
    identity: tuple[tuple[str, ...], ...]      # variants, each a sorted tuple of key names
    identity_types: dict[str, str]
    derive: dict[str, str]                     # field → expression over the identity
    filters: dict[str, Any]
    output: dict[str, dict]                    # core field → {type, required?, note?}
    miss: str
    idempotent: bool = True

    @property
    def required_output(self) -> tuple[str, ...]:
        return tuple(k for k, v in self.output.items() if (v or {}).get("required"))


@dataclass(frozen=True)
class Adapter:
    endpoint_id: str
    accepts: tuple[tuple[str, ...], ...]       # identity variants (sorted key tuples)
    in_map: dict[str, str]                     # contract field → `queryParams.x` | `body.x`
    const: dict[str, Any]                      # fixed provider params (`body.type: work`)
    out_map: dict[str, str]                    # core field → expression over the provider body
    miss: str
    verified: bool = False
    verify_note: str = ""

    def to_upstream(self, identity: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
        """(query params, JSON body) for this provider from a canonical identity."""
        query: dict[str, str] = {}
        body: dict[str, Any] = {}
        doc = {"queryParams": query, "body": body}
        for field_name, target in self.in_map.items():
            v = identity.get(field_name)
            if v is None:
                continue
            P.set_path(doc, target, v if target.startswith("body.") else str(v))
        for target, v in self.const.items():
            P.set_path(doc, target, v)
        return query, body

    def from_upstream(self, provider_body: Any) -> dict[str, Any]:
        return {k: P.evaluate(expr, provider_body) for k, expr in self.out_map.items()}

    def is_miss(self, provider_body: Any) -> bool:
        return bool(P.evaluate(self.miss, provider_body))


def _variants(raw) -> tuple[tuple[str, ...], ...]:
    out = []
    for v in raw or []:
        keys = tuple(sorted(v.keys() if isinstance(v, dict) else v))
        out.append(keys)
    return tuple(out)


def parse_contracts(doc: dict) -> dict[str, Contract]:
    out = {}
    for cap, c in (doc.get("contracts") or {}).items():
        types = {}
        for v in c.get("identity") or []:
            if isinstance(v, dict):
                types.update({k: str(t) for k, t in v.items()})
        out[cap] = Contract(
            capability=cap, summary=str(c.get("summary") or ""), identity=_variants(c.get("identity")),
            identity_types=types, derive=dict(c.get("derive") or {}), filters=dict(c.get("filters") or {}),
            output={k: (v if isinstance(v, dict) else {"type": str(v)}) for k, v in (c.get("output") or {}).items()},
            miss=str(c.get("miss") or ""), idempotent=bool(c.get("idempotent", True)))
    return out


def parse_adapters(doc: dict) -> dict[str, Adapter]:
    out = {}
    for eid, a in (doc.get("adapters") or {}).items():
        out[eid] = Adapter(
            endpoint_id=eid, accepts=_variants(a.get("accepts")), in_map=dict(a.get("in") or {}),
            const=dict(a.get("const") or {}), out_map=dict(a.get("out") or {}), miss=str(a.get("miss") or ""))
    return out


# ---- identity -------------------------------------------------------------------------------

def canonical_identity(contract: Contract, given: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...] | None]:
    """The caller's fields + everything derivable → (identity, the variant they supplied), or
    (identity, None) when no variant is complete. Derived keys count for matching adapters."""
    ident = {k: v for k, v in given.items() if k in contract.identity_types and v not in (None, "")}
    supplied = next((v for v in contract.identity if all(k in ident for k in v)), None)
    if supplied is None:
        return ident, None
    for _ in range(2):  # derive until stable (join needs first+last; split needs full_name)
        for k, expr in contract.derive.items():
            if ident.get(k) in (None, ""):
                v = P.evaluate(expr, ident)
                if v not in (None, ""):
                    ident[k] = v
    return ident, supplied


def adapter_accepts(adapter: Adapter, identity: dict[str, Any]) -> tuple[str, ...] | None:
    """The first accepted variant fully present in the (derived) identity, or None."""
    return next((v for v in adapter.accepts if all(identity.get(k) not in (None, "") for k in v)), None)


# ---- verification ---------------------------------------------------------------------------

def verify(adapter: Adapter, contract: Contract, endpoint: dict, example: Any) -> tuple[bool, str]:
    """Fixture round-trip: `in` must reproduce the endpoint's own `test_request` from the contract's
    view of it, and `out` must fill every required core field from the example response (or the
    example must be a recognised miss). Anything else = not a candidate."""
    if not adapter.accepts or not adapter.out_map or not adapter.miss:
        return False, "adapter incomplete"
    tr = endpoint.get("test_request") or {}
    # Reconstruct the identity from the test request through the adapter's own `in` map.
    ident: dict[str, Any] = {}
    doc = {"queryParams": tr.get("queryParams") or {}, "body": tr.get("body") or {}}
    for field_name, target in adapter.in_map.items():
        v = P.get_path(doc, target)
        if v not in (None, ""):
            ident[field_name] = v
    ident, variant = canonical_identity(contract, ident)
    if variant is None or adapter_accepts(adapter, ident) is None:
        return False, "test_request does not express an accepted identity variant"
    q, b = adapter.to_upstream(ident)
    for k, v in (tr.get("queryParams") or {}).items():
        if k in {t.split(".", 1)[1] for t in adapter.in_map.values() if t.startswith("queryParams.")} and str(q.get(k)) != str(v):
            return False, f"in: queryParams.{k} → {q.get(k)!r}, test_request has {v!r}"
    for k, v in (tr.get("body") or {}).items():
        if k in {t.split(".", 1)[1].split(".")[0] for t in adapter.in_map.values() if t.startswith("body.")} and b.get(k) != v:
            return False, f"in: body.{k} → {b.get(k)!r}, test_request has {v!r}"
    if example is None:
        return False, "no example_response to verify `out` against"
    if adapter.is_miss(example):
        return True, "example is a miss; out unverified on a hit"
    core = adapter.from_upstream(example)
    missing = [k for k in contract.required_output if core.get(k) in (None, "")]
    if missing:
        return False, f"out: example lacks required {missing}"
    return True, ""


def load_routing(directory: Path, endpoints_by_id: dict[str, dict], read_yaml, read_example) -> tuple[dict[str, Contract], dict[str, Adapter]]:
    """Parse both files and verify every adapter against its endpoint's fixtures."""
    contracts = parse_contracts(read_yaml(directory / "contracts.yaml") or {})
    adapters = parse_adapters(read_yaml(directory / "adapters.yaml") or {})
    verified: dict[str, Adapter] = {}
    for eid, ad in adapters.items():
        ep = endpoints_by_id.get(eid)
        contract = contracts.get((ep or {}).get("capability") or "")
        if ep is None or contract is None:
            verified[eid] = Adapter(**{**ad.__dict__, "verified": False, "verify_note": "unknown endpoint or no contract"})
            continue
        ok, note = verify(ad, contract, ep, read_example(ep))
        verified[eid] = Adapter(**{**ad.__dict__, "verified": ok, "verify_note": note})
    return contracts, verified
