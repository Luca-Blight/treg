"""Three query-time reports that answer "is the money real?" — asked of the rows phase 3 started
writing, never of a scheduler. Nothing here mutates anything: `ledger.py` owns every write, and this
module is the read side that checks its work against the outside world.

The three questions, and why each one needs its own source of truth:

* **Price drift** (`price_drift`) — did the catalog's price stay true? Compares, per endpoint, the
  estimate we RESERVED against the cost the provider actually REPORTED, both recorded on the same
  `CallRecord` row. Estimates come from a hand-verified sweep; providers re-price whenever they like,
  so a silent 10% climb turns a positive margin negative with nothing on fire. This report is the
  only thing that notices.
* **Provider spend** (`provider_spend`) — reads the LEDGER, not the audit table, because it is the
  number a human holds next to an invoice or a balance delta. Audit rows are fire-and-forget and may
  be missing; ledger rows may not (see `models.LedgerEntry`).
* **Repeat rate** (`repeat_rate`) — measurement only, the groundwork the plan asks for BEFORE any
  cache exists: how much of the bill was the same query twice. Answering it is what makes a cache a
  decision instead of a guess.

Units are integer micro-USD (`*_micro`) with a display-only `*_usd` twin, same contract as
`ledger.py`: never compute against the USD field.

Two aggregations deliberately happen in Python rather than SQL: the ledger's provenance lives in a
JSON `meta` column (portable JSON extraction across SQLite and Postgres is not worth a report), and
these are admin-scale windows over a bounded number of metered calls — the same tradeoff
`admin_stats` already makes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import distinct, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .config import get_settings
from .models import CallRecord, LedgerEntry

# How far the observed cost may sit from the estimate before the endpoint is worth a human's
# attention. 5% is under the platform margin, so a drift that trips this is still profitable — which
# is the point of catching it here rather than at the point it goes negative.
DRIFT_TOLERANCE = 0.05


def usd(amount_micro: int | float) -> float:
    """micro-USD → USD, for DISPLAY only (mirrors `ledger.usd`)."""
    return round(amount_micro / 1_000_000, 6)


def window_start(days: int) -> datetime:
    """Naive-UTC cutoff `days` back — the convention every timestamp column in this app stores."""
    days = max(1, min(int(days), 365))
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)


async def price_drift(
    db: AsyncSession, since: datetime, min_calls: int = 3, *, tolerance: float = DRIFT_TOLERANCE,
) -> list[dict]:
    """Per (endpoint, provider): what we estimated vs what the provider charged, since `since`.

    Only rows carrying BOTH numbers count, which restricts this to the providers that report their
    own charge in-band — today `dataforseo` (top-level `cost`) and `scrapecreators`
    (`credits_charged`), per `api._observed_cost_micro`. Everyone else settles at the estimate, so
    there is no second opinion to compare against and their endpoints simply never appear here. That
    is a real limit of the report, not a bug: drift on a silent provider is only visible on their
    invoice (see `provider_spend`).

    `flagged` is the actionable bit: at least `min_calls` samples AND a mean observed cost more than
    `tolerance` away from the mean estimate — a single call can be an outlier (a `per_result` route
    that returned one row), a run of them is a re-price. A zero mean estimate has no ratio to
    compute, so `drift_ratio` is None and the flag falls back to "we thought it was free and it
    wasn't".

    Sorted worst-drift-first. Amounts are means over the window, not per-call truth: a `per_result`
    endpoint legitimately costs a different amount every call, which is exactly why the flag needs
    several samples before it fires.
    """
    rows = (await db.execute(
        select(
            CallRecord.endpoint_id, CallRecord.provider, func.count(),
            func.sum(CallRecord.cost_estimated_micro), func.sum(CallRecord.cost_observed_micro),
            func.max(CallRecord.cost_observed_micro), func.max(CallRecord.cost_estimated_micro),
        )
        .where(CallRecord.created_at >= since,
               CallRecord.cost_estimated_micro.is_not(None),
               CallRecord.cost_observed_micro.is_not(None))
        .group_by(CallRecord.endpoint_id, CallRecord.provider)
    )).all()

    out: list[dict] = []
    for endpoint_id, provider, calls, est_sum, obs_sum, obs_max, est_max in rows:
        calls = int(calls or 0)
        if not calls:
            continue
        est_mean = (est_sum or 0) / calls
        obs_mean = (obs_sum or 0) / calls
        ratio = (obs_mean - est_mean) / est_mean if est_mean > 0 else None
        drifting = abs(ratio) > tolerance if ratio is not None else obs_mean > 0
        out.append({
            "endpoint_id": endpoint_id, "provider": provider, "calls": calls,
            "estimate_mean_micro": round(est_mean, 3), "estimate_max_micro": int(est_max or 0),
            "observed_mean_micro": round(obs_mean, 3), "observed_max_micro": int(obs_max or 0),
            "estimate_mean_usd": usd(est_mean), "observed_mean_usd": usd(obs_mean),
            "drift_ratio": round(ratio, 4) if ratio is not None else None,
            "drift_micro": round(obs_mean - est_mean, 3),
            "flagged": bool(drifting and calls >= min_calls),
        })
    out.sort(key=lambda r: (-abs(r["drift_ratio"]) if r["drift_ratio"] is not None else -1e9,
                            -r["calls"]))
    return out


async def provider_spend(db: AsyncSession, since: datetime) -> dict:
    """Settled platform spend per provider since `since` — the figure to hold next to the provider's
    own invoice or balance delta.

    Reads `settle` ledger entries, which exist ONLY for platform-tier calls (a call on the org's own
    credential spends nobody's balance and never reserves), so no tier filter is needed beyond that;
    `meta.tier` is honoured when a caller has stamped one. Three numbers come back per provider and
    they are not interchangeable:

      * `charged_micro` — what orgs were billed, margin included. Revenue, not cost.
      * `provider_reported_micro` — the provider's own reported charge, summed over the calls where
        it reported one. A true subtotal of the invoice, covering `reported_calls` of `calls`.
      * `provider_cost_est_micro` — `charged / (1 + margin)`: the whole window's spend backed out of
        the charge. This is the apples-to-apples invoice comparison, and it is an ESTIMATE for every
        call the provider stayed quiet about.

    A gap between `provider_cost_est_micro` and the invoice is either drift (`price_drift` names the
    endpoint) or calls we billed and they didn't, or the reverse. Either way margin was a guess until
    this report is read.
    """
    entries = (await db.execute(
        select(LedgerEntry).where(LedgerEntry.kind == "settle", LedgerEntry.created_at >= since)
    )).scalars().all()

    margin = float(get_settings().platform_margin or 0.0)
    per: dict[str, dict] = {}
    for e in entries:
        meta = e.meta or {}
        if meta.get("tier") not in (None, "", "platform"):
            continue
        p = per.setdefault(str(meta.get("provider") or "unknown"), {
            "provider": str(meta.get("provider") or "unknown"), "calls": 0, "reported_calls": 0,
            "charged_micro": 0, "provider_reported_micro": 0, "orgs": set(), "shortfall_micro": 0,
        })
        p["calls"] += 1
        p["charged_micro"] += int(-e.amount_micro)  # settle entries are negative (money leaving)
        p["orgs"].add(e.org_id)
        p["shortfall_micro"] += int(meta.get("block_shortfall_micro") or 0)
        observed = meta.get("observed_micro")
        if isinstance(observed, (int, float)) and not isinstance(observed, bool):
            p["reported_calls"] += 1
            p["provider_reported_micro"] += int(observed)

    rows = []
    for p in per.values():
        charged = p["charged_micro"]
        cost_est = int(charged / (1.0 + margin)) if margin > 0 else charged
        rows.append({**p, "orgs": len(p["orgs"]),
                     "charged_usd": usd(charged),
                     "provider_reported_usd": usd(p["provider_reported_micro"]),
                     "provider_cost_est_micro": cost_est,
                     "provider_cost_est_usd": usd(cost_est)})
    rows.sort(key=lambda r: -r["charged_micro"])
    total = sum(r["charged_micro"] for r in rows)
    return {"margin": margin, "providers": rows, "calls": sum(r["calls"] for r in rows),
            "charged_micro": total, "charged_usd": usd(total)}


async def repeat_rate(db: AsyncSession, since: datetime, *, top: int = 10) -> dict:
    """How much of the bill was the same call twice, per provider, plus the worst offenders.

    `params_hash` is an identity for "this exact call again" (`api._params_hash`: endpoint + sorted
    query + a digest of the body — bodies are never stored). `repeat_calls = calls - distinct` is
    therefore the number of calls a cache with an infinite TTL would have served for free, and
    `repeat_cost_micro` is what they cost. Cost per row is the observed charge when the provider
    reported one, else the estimate we reserved.

    Measurement only. Nothing here is safe to cache on its own: a cache needs `kind: data` +
    `scope: any_account`, a per-endpoint TTL and a per-provider ToS check on response retention — the
    conditions the plan spells out. This report only says whether it would be worth the trouble.
    """
    rows = (await db.execute(
        select(CallRecord.provider, func.count(), func.count(distinct(CallRecord.params_hash)),
               func.sum(func.coalesce(CallRecord.cost_observed_micro,
                                      CallRecord.cost_estimated_micro, 0)))
        .where(CallRecord.created_at >= since, CallRecord.params_hash.is_not(None),
               CallRecord.params_hash != "", CallRecord.provider.is_not(None))
        .group_by(CallRecord.provider)
    )).all()

    providers = []
    for provider, calls, uniq, cost in rows:
        calls, uniq = int(calls or 0), int(uniq or 0)
        repeats = max(0, calls - uniq)
        providers.append({
            "provider": provider, "calls": calls, "distinct_params": uniq, "repeat_calls": repeats,
            "repeat_ratio": round(repeats / calls, 4) if calls else 0.0,
            "cost_micro": int(cost or 0), "cost_usd": usd(int(cost or 0)),
        })
    providers.sort(key=lambda r: -r["repeat_calls"])

    # The individual queries worth a cache entry: same params_hash more than once, dearest first.
    # `min(endpoint_id)` because a hash already encodes the endpoint — the GROUP BY just needs a name.
    hot = (await db.execute(
        select(CallRecord.params_hash, func.min(CallRecord.endpoint_id), func.min(CallRecord.provider),
               func.count(), func.sum(func.coalesce(CallRecord.cost_observed_micro,
                                                    CallRecord.cost_estimated_micro, 0)))
        .where(CallRecord.created_at >= since, CallRecord.params_hash.is_not(None),
               CallRecord.params_hash != "")
        .group_by(CallRecord.params_hash)
        .having(func.count() > 1)
        .order_by(func.count().desc())
        .limit(max(1, min(int(top), 100)))
    )).all()
    top_repeated = [
        {"params_hash": h, "endpoint_id": ep, "provider": prov, "calls": int(n or 0),
         "cost_micro": int(cost or 0), "cost_usd": usd(int(cost or 0)),
         # What the repeats (every call after the first) cost — a cache's actual saving, assuming the
         # calls were identical in price, which a same-hash call to a per_call endpoint is.
         "repeat_cost_micro": int((cost or 0) * (int(n or 1) - 1) / int(n or 1))}
        for h, ep, prov, n, cost in hot
    ]
    totals_calls = sum(p["calls"] for p in providers)
    totals_repeats = sum(p["repeat_calls"] for p in providers)
    return {
        "calls": totals_calls, "repeat_calls": totals_repeats,
        "repeat_ratio": round(totals_repeats / totals_calls, 4) if totals_calls else 0.0,
        "providers": providers, "top_repeated": top_repeated,
    }
