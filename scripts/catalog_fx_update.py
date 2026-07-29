#!/usr/bin/env python3
"""Refresh src/treg/catalog/fx.yaml from open.er-api.com (free, keyless).

Run on deploy or cron: `uv run python scripts/catalog_fx_update.py`. Only currencies already
listed in fx.yaml are refreshed — adding a new billing currency = add its key once, rerun.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date
from pathlib import Path

import yaml

FX = Path(__file__).resolve().parent.parent / "src" / "treg" / "catalog" / "fx.yaml"


def main() -> int:
    doc = yaml.safe_load(FX.read_text())
    live = json.load(urllib.request.urlopen("https://open.er-api.com/v6/latest/USD", timeout=30))
    if live.get("result") != "success":
        print(f"rate API answered {live.get('result')!r} — fx.yaml left unchanged")
        return 1
    usd_to = live["rates"]
    for cur in doc["rates_to_usd"]:
        if cur == "USD" or cur not in usd_to:
            continue
        doc["rates_to_usd"][cur] = round(1 / usd_to[cur], 4)
    doc["updated"] = date.today().isoformat()
    FX.write_text(
        FX.read_text().split("rates_to_usd:")[0]  # keep the header comment block
        + "rates_to_usd:\n"
        + "".join(f"  {c}: {v}\n" for c, v in doc["rates_to_usd"].items())
        + f"updated: {doc['updated']}\n"
        + f"source: {doc['source']}\n"
    )
    print(f"fx.yaml refreshed: {doc['rates_to_usd']} ({doc['updated']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
