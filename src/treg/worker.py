"""`treg-worker` — the scheduled, server-side maintainer commands (the `worker` profile).

    treg-worker capacity sweep [--only provider,...] [--json]

Not the light `treg` CLI: these need the server extra (DB, platform keys in the env) and make
outbound calls to third parties, so they run as Render cron jobs with the server's env — never as
dataplane lifespan work (refactor plan §2.2). Money is never moved here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


def _need_server() -> None:
    try:
        import sqlalchemy  # noqa: F401
    except ImportError:  # pragma: no cover — a base install has no DB stack
        print("treg-worker needs the server extra: pip install 'treg[server]'", file=sys.stderr)
        raise SystemExit(2)


async def _capacity_sweep(args) -> int:
    from .db import init_db, session_maker
    from .domain.capacity.sweep import run_sweep

    await init_db()
    only = {p.strip() for p in (args.only or "").split(",") if p.strip()} or None
    async with session_maker() as db:
        result = await run_sweep(db, only=only)
    if args.json:
        print(json.dumps({p: s.to_json() for p, s in result.states.items()}, indent=2))
    else:
        print(f"{'provider':<22}{'remaining':>14}  unit / state")
        for p, s in result.states.items():
            rem = "—" if s.remaining is None else f"{s.remaining:,.2f}"
            print(f"{p:<22}{rem:>14}  {s.unit} · {s.health}" + (f" · {s.note}" if s.note else ""))
        if result.unknown_policies:
            print(f"\nunclassified policies (capacity_type/funding_mode = unknown): "
                  f"{', '.join(result.unknown_policies)}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="treg-worker", description=__doc__)
    sub = ap.add_subparsers(dest="group", required=True)
    cap = sub.add_parser("capacity", help="platform vendor-account capacity")
    capsub = cap.add_subparsers(dest="cmd", required=True)
    sweep = capsub.add_parser("sweep", help="collect balances/quotas → snapshots → latest state")
    sweep.add_argument("--only", help="comma-separated providers (default: all)")
    sweep.add_argument("--json", action="store_true")
    sweep.set_defaults(fn=_capacity_sweep)
    args = ap.parse_args(argv)
    _need_server()
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
