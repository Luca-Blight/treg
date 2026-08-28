#!/usr/bin/env python3
"""Push every URL in the live sitemap to IndexNow, and ping Google with the sitemap.

IndexNow is one POST for up to 10,000 URLs; Bing, Yandex, Seznam and Naver share the feed, so one
submission reaches all of them. Google does not take IndexNow — it gets the sitemap ping, and the
Search Console sitemap resubmission is done through the catalog (`google-search-console.x.webmasters-
sitemaps-submit`) because it needs the property owner's OAuth.

Run after a deploy that adds or retitles pages:

    uv run --frozen python scripts/indexnow_submit.py            # the live site
    uv run --frozen python scripts/indexnow_submit.py --base https://staging.example

Exit code is non-zero when IndexNow rejects the batch (422 = key file not reachable on the host).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request

KEY = "7c2e4a91b5d3f8e6treg2026"  # must match INDEXNOW_KEY in src/treg/routers/web.py


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://treg.to")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    host = re.sub(r"^https?://", "", base)

    with urllib.request.urlopen(f"{base}/sitemap.xml", timeout=30) as r:
        urls = re.findall(r"<loc>([^<]+)</loc>", r.read().decode())
    print(f"{len(urls)} URLs in {base}/sitemap.xml")

    with urllib.request.urlopen(f"{base}/{KEY}.txt", timeout=30) as r:
        served = r.read().decode().strip()
    if served != KEY:
        print(f"key file mismatch: {served!r} != {KEY!r}", file=sys.stderr)
        return 2

    if args.dry_run:
        print("dry run — not submitting")
        return 0

    body = json.dumps({"host": host, "key": KEY, "keyLocation": f"{base}/{KEY}.txt", "urlList": urls}).encode()
    req = urllib.request.Request("https://api.indexnow.org/IndexNow", data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(f"IndexNow: HTTP {r.status}")
    except urllib.error.HTTPError as e:  # noqa: PERF203
        print(f"IndexNow rejected the batch: HTTP {e.code} {e.read().decode()[:200]}", file=sys.stderr)
        return 1

    ping = f"https://www.google.com/ping?sitemap={base}/sitemap.xml"
    try:
        with urllib.request.urlopen(ping, timeout=30) as r:
            print(f"Google sitemap ping: HTTP {r.status}")
    except urllib.error.HTTPError as e:  # noqa: PERF203
        # Google retired the ping endpoint for some properties; the Search Console resubmission
        # is the reliable path and is done separately.
        print(f"Google sitemap ping: HTTP {e.code} (use the Search Console sitemaps API)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
