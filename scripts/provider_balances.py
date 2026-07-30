#!/usr/bin/env python3
"""What each provider says our account has left, next to what our ledger says we spent.

    uv run python scripts/provider_balances.py [--days 30] [--json]

The manual half of Phase 5's reconciliation: `/admin/reconcile/spend` (see src/treg/reconcile.py)
reports what treg BILLED orgs for platform-key calls, and this prints what the provider's own account
says — the two numbers only agree if the catalog's prices are true. Run it monthly, before and after,
and compare the balance DELTA against the window's spend.

Deliberately NOT wired into the server: it needs the platform keys in the process's env and it makes
outbound calls to a third party, neither of which belongs in a request handler. It is a maintainer's
tool, run by hand, reading the same `TREG_PLATFORM_KEY_*` settings (and therefore the same `.env`)
the deployment uses.

Only the FREE, non-metering balance routes are called — DataForSEO's `/appendix/user_data` (which
also returns the account's whole machine-readable rate card) and TikHub's `/user/get_user_info`.
ScrapeCreators publishes no balance endpoint, so it is listed as unavailable rather than guessed at.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from treg import ledger, reconcile  # noqa: E402
from treg.config import get_settings, platform_setting_name  # noqa: E402
from treg.db import session_maker  # noqa: E402
from treg.oauth_providers import get as get_provider  # noqa: E402

# provider → (path on the provider's own base_url, how to pull a USD balance out of the response).
# Both routes are free and neither meters, so this script can be run as often as you like.
BALANCE_ROUTES = {
    "dataforseo": ("/appendix/user_data",
                   lambda d: (d.get("tasks") or [{}])[0].get("result", [{}])[0]
                   .get("money", {}).get("balance")),
    "tikhub": ("/api/v1/tikhub/user/get_user_info",
               lambda d: (d.get("user_data") or {}).get("balance")),
}


async def _provider_balance(provider: str) -> dict:
    """Ask one provider what it thinks our balance is. Never raises — a failure is a reported row."""
    setting = platform_setting_name(provider)
    # Read the SETTING, not `platform_key_for` — the tier-4 allow-list is a serving kill switch, and a
    # provider we just switched off is exactly one whose final balance we still want to see.
    key = getattr(get_settings(), setting, "") or ""
    if not key:
        return {"provider": provider, "balance_usd": None,
                "note": f"no TREG_{setting.upper()} in the env"}
    spec = BALANCE_ROUTES.get(provider)
    prov = get_provider(provider)
    if spec is None or prov is None:
        return {"provider": provider, "balance_usd": None, "note": "no balance endpoint published"}
    path, extract = spec
    headers = {prov.token_header: prov.token_format.format(secret=key)}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(prov.base_url.rstrip("/") + path, headers=headers)
        if r.status_code >= 400:
            return {"provider": provider, "balance_usd": None,
                    "note": f"HTTP {r.status_code}: {r.text[:160]}"}
        value = extract(r.json())
    except Exception as exc:  # noqa: BLE001 — a reconciliation aid must report, not crash
        return {"provider": provider, "balance_usd": None, "note": f"{type(exc).__name__}: {exc}"}
    return {"provider": provider,
            "balance_usd": round(float(value), 6) if isinstance(value, (int, float)) else None,
            "note": "" if isinstance(value, (int, float)) else "response carried no balance field"}


async def main(days: int, as_json: bool) -> int:
    since = reconcile.window_start(days)
    try:
        async with session_maker() as db:
            spend = await reconcile.provider_spend(db, since)
    except OperationalError as exc:  # the ledger tables land on first server start (db.init_db)
        print(f"no ledger in {get_settings().database_url}: {exc.orig}\n"
              "Point TREG_DATABASE_URL at the deployment's database (the balances below still work).",
              file=sys.stderr)
        spend = {"margin": float(get_settings().platform_margin or 0.0), "providers": []}
    by_provider = {p["provider"]: p for p in spend["providers"]}
    providers = sorted(set(BALANCE_ROUTES) | set(by_provider) | {"scrapecreators"})
    balances = {b["provider"]: b for b in
                await asyncio.gather(*(_provider_balance(p) for p in providers))}

    if as_json:
        print(json.dumps({"since": since.isoformat(), "days": days, "margin": spend["margin"],
                          "providers": [{**balances[p], **by_provider.get(p, {})} for p in providers]},
                         indent=2, default=str))
        return 0

    print(f"provider balances vs ledger spend, last {days}d (since {since:%Y-%m-%d %H:%M} UTC)\n")
    print(f"{'provider':<18}{'their balance':>16}{'our spend (est)':>18}{'billed':>12}{'calls':>8}")
    for p in providers:
        b, s = balances[p], by_provider.get(p, {})
        bal = f"${b['balance_usd']:.4f}" if b["balance_usd"] is not None else "—"
        cost = ledger.usd(s["provider_cost_est_micro"]) if s else 0.0
        print(f"{p:<18}{bal:>16}{'$%.4f' % cost:>18}"
              f"{'$%.4f' % ledger.usd(s.get('charged_micro', 0)):>12}{s.get('calls', 0):>8}")
        if b["note"]:
            print(f"{'':<18}{b['note']}")
    print(f"\n'our spend (est)' backs the {spend['margin']:.0%} platform margin out of what orgs were "
          "billed — it is the figure to compare against the provider's invoice or balance delta.\n"
          "A balance is a POINT in time: the reconciliation is (balance_then - balance_now) vs spend.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30, help="ledger window in days (default: 30)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.days, args.json)))
