#!/usr/bin/env python3
"""Validate the endpoint catalog (src/treg/catalog/*.yaml) — schema shape + referential integrity.

Run from the repo root: `uv run python scripts/catalog_validate.py [service ...]`
Exit 0 = valid. Every violation prints one line: `<file>: <problem>`.

Checks (the success criteria from docs/context/architecture/catalog.md):
  - provider file's `provider` matches its filename and exists in treg.oauth_providers.REGISTRY
  - endpoint ids unique across the WHOLE catalog; id convention `<provider>.<capability>`
  - `capability` exists in capabilities.yaml OR the file's own proposed_capabilities
  - `platform` equals the capability's first segment and exists in capabilities.yaml platforms
  - required fields present; enums valid (scope, method, cost.type)
  - a `verified` endpoint must have an existing example_response file
  - an extended endpoint a verification run has touched claims exactly one non-empty state
    (verified | unverified | untestable | skipped), and an `untestable` one carries no
    test_request for a re-verify run to call it with anyway
  - no obvious credential leak (Authorization/token values) in any catalog file

Two tiers, two rule sets. `<provider>.yaml` holds hand-curated `tier: core` entries and every rule
above applies. `<provider>.extended.yaml` holds the machine-generated coverage tier written by
scripts/catalog_ingest.py: those entries need only id/platform/method/path/summary, and carry no
capability (nothing has been mapped to the taxonomy yet). Id-prefix, id-uniqueness, platform
integrity and the leak scan apply to BOTH — an extended entry that does declare a capability is
held to the core referential rules.

The evidence rule is deliberately tier-blind, and needed no loosening when the extended tier gained
generated `test_request`s and live verification (scripts/catalog_verify_extended.py): `verified`
means the same thing in both files, so in both it must be backed by an `example_response` file that
exists and a `test_request` to re-check it with. A tier decides how much is EXPECTED of an entry,
never what a claim on it is worth.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "src" / "treg" / "catalog"

SCOPES = {"any_account", "own_account"}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
COST_TYPES = {"per_call", "per_result", "per_success", "free", "quota_rows"}
TIERS = {"core", "extended"}
# what an endpoint IS (marketplace browse surface vs. plumbing). Optional — absent reads as "data" —
# but a stated one must be from this set. See docs/context/architecture/catalog.md.
KINDS = {"data", "action", "account", "utility"}
# the section heading an endpoint files under on its platform page — one lowercase word
DOMAIN = re.compile(r"[a-z][a-z0-9_]*")
REQUIRED = {
    "core": ("id", "capability", "platform", "method", "path", "summary"),
    "extended": ("id", "platform", "method", "path", "summary"),
}
# The four outcomes an extended endpoint can have once a verification run has touched it. They are
# mutually exclusive by construction, and an entry that has been through the pipeline must claim
# exactly one — "no state" reads as "never attempted", which is a different fact and a lie once a
# run has been over it. This caught a real regression: re-running an endpoint overwrote its result
# record, dropped the reason string, and stamped an EMPTY state that nothing else noticed.
STATES = ("verified", "unverified", "untestable", "skipped")
# a long token-looking literal anywhere in a catalog file is a leak until proven otherwise
LEAK = re.compile(r"(Bearer\s+[A-Za-z0-9+/_=-]{16,}|[A-Za-z0-9+/]{40,}={0,2})")
# ...but URL and API paths are also long runs of [A-Za-z0-9/], and the extended tier is thousands
# of them. Two things separate them from a credential: a path is built of short slash-separated
# segments, and those segments spell words ("dataforseo", "kolContentTags"). A base64 secret hits
# a `/` only about once per 64 characters, so at least one of its segments stays long and wordless.
WORDY = re.compile(r"[a-z]{8,}")


def looks_like_secret(match: str) -> bool:
    if match.lower().startswith("bearer"):
        return True
    return any(
        len(seg) >= 24 and not WORDY.search(seg) and any(c.isdigit() for c in seg)
        for seg in match.split("/")
    )



def fail(errors: list[str], where: str, msg: str) -> None:
    errors.append(f"{where}: {msg}")


def main(argv: list[str]) -> int:
    errors: list[str] = []
    tax = yaml.safe_load((CATALOG / "capabilities.yaml").read_text())
    platforms = set(tax.get("platforms") or {})
    capabilities = set(tax.get("capabilities") or {})

    sys.path.insert(0, str(ROOT / "src"))
    from treg.oauth_providers import REGISTRY  # noqa: E402

    only = set(argv)
    files = sorted(p for p in CATALOG.glob("*.yaml") if p.name not in ("capabilities.yaml", "fx.yaml"))
    # "tikhub" selects tikhub.yaml AND tikhub.extended.yaml — a service is both its tiers
    service_of = {p: p.stem.removesuffix(".extended") for p in files}
    if only:
        files = [p for p in files if service_of[p] in only]
        missing = only - {service_of[p] for p in files}
        for m in missing:
            fail(errors, m, "no such catalog file")

    seen_ids: dict[str, str] = {}
    for path in files:
        name = path.name
        text = path.read_text()
        data = yaml.safe_load(text)
        if not isinstance(data, dict):
            fail(errors, name, "not a mapping")
            continue
        extended_file = path.name.endswith(".extended.yaml")
        service = data.get("provider")
        if service != service_of[path]:
            fail(errors, name, f"provider '{service}' != filename stem '{service_of[path]}'")
        if service not in REGISTRY:
            fail(errors, name, f"provider '{service}' not in oauth_providers.REGISTRY")
        src = data.get("source")
        # core files cite the docs they were curated from; extended files cite the specs they were
        # generated from, so that a re-run is reproducible from the file alone
        need = "spec_urls" if extended_file else "docs"
        if not isinstance(src, dict) or not src.get(need):
            fail(errors, name, f"source.{need} missing")
        proposed = set(data.get("proposed_capabilities") or {})
        known = capabilities | proposed

        eps = data.get("endpoints")
        if not isinstance(eps, list) or not eps:
            fail(errors, name, "endpoints missing or empty")
            continue
        for ep in eps:
            eid = ep.get("id", "<no id>")
            where = f"{name}:{eid}"
            tier = ep.get("tier", "extended" if extended_file else "core")
            if tier not in TIERS:
                fail(errors, where, f"bad tier '{tier}'")
                tier = "extended" if extended_file else "core"
            if extended_file != (tier == "extended"):
                fail(errors, where, f"tier '{tier}' does not belong in {name}")
            for f in REQUIRED[tier]:
                if not ep.get(f):
                    fail(errors, where, f"missing required field '{f}'")
            if eid in seen_ids:
                fail(errors, where, f"duplicate id (also in {seen_ids[eid]})")
            seen_ids[eid] = name
            if service and not eid.startswith(f"{service}."):
                fail(errors, where, f"id must start with '{service}.'")
            # extended entries are unmapped by design; one that DOES claim a capability is held to
            # the same referential rules as core, so a hand-promoted entry can't drift
            cap = ep.get("capability", "")
            if cap or tier == "core":
                if cap not in known:
                    fail(errors, where, f"capability '{cap}' not in capabilities.yaml or proposed_capabilities")
            plat = ep.get("platform", "")
            if plat not in platforms:
                fail(errors, where, f"platform '{plat}' not in capabilities.yaml platforms")
            if cap and plat and cap.split(".")[0] != plat:
                fail(errors, where, f"platform '{plat}' != capability's first segment '{cap.split('.')[0]}'")
            # `domain` is optional — the loader derives one from the capability id or the path when
            # it is absent. Declaring one overrides that, so it has to be the same SHAPE the derived
            # ones are: one lowercase word, or the platform page grows a section of one.
            # `name` is an optional short DISPLAY title; `summary` stays the provider's own
            # description. Light check only: present ⇒ a non-empty string that fits a row heading.
            nm = ep.get("name")
            if nm is not None and (not isinstance(nm, str) or not nm.strip() or len(nm) > 60):
                fail(errors, where, "name must be a non-empty string of at most 60 chars")
            dom = ep.get("domain")
            if dom is not None and not DOMAIN.fullmatch(str(dom)):
                fail(errors, where, f"domain '{dom}' must be a single lowercase word (a-z0-9_)")
            # `kind` is optional (absent ⇒ data); a stated one must be a known kind
            if ep.get("kind") is not None and ep.get("kind") not in KINDS:
                fail(errors, where, f"kind '{ep.get('kind')}' not one of {sorted(KINDS)}")
            if ep.get("scope", "any_account") not in SCOPES:
                fail(errors, where, f"bad scope '{ep.get('scope')}'")
            if ep.get("method") not in METHODS:
                fail(errors, where, f"bad method '{ep.get('method')}'")
            cost = ep.get("cost")
            if cost is not None or tier == "core":
                # cost is optional in the extended tier — several providers publish prices per API
                # family rather than per route — but a stated cost must still be a real cost model
                if not isinstance(cost, dict) or cost.get("type") not in COST_TYPES:
                    fail(errors, where, f"cost.type missing or not one of {sorted(COST_TYPES)}")
            if ep.get("verified"):
                ex = ep.get("example_response")
                if not ex:
                    fail(errors, where, "verified but no example_response")
                elif not (CATALOG / ex).is_file():
                    fail(errors, where, f"example_response '{ex}' does not exist")
                if not ep.get("test_request"):
                    fail(errors, where, "verified but no test_request (nothing to re-verify with)")
            if tier == "extended":
                # "been through the pipeline" = a run either built it a request or recorded an
                # outcome for it. A freshly ingested entry that has never been verified has
                # neither, and is left alone.
                claimed = [s for s in STATES if ep.get(s)]
                touched = ep.get("test_request") or any(s in ep for s in STATES)
                if len(claimed) > 1:
                    fail(errors, where, f"claims {len(claimed)} states at once: {claimed} — "
                                        "verified/unverified/untestable/skipped are exclusive")
                elif touched and not claimed:
                    empty = [s for s in STATES if s in ep]
                    fail(errors, where, f"no endpoint state: {sorted(empty)} present but empty"
                         if empty else "has a test_request but no verified/unverified/untestable/"
                                       "skipped saying what happened when it was called")
                if ep.get("untestable") and ep.get("test_request"):
                    # `untestable` means no call is possible — but catalog_verify.py --extended
                    # replays anything that HAS a test_request, so the pair is not just a
                    # contradiction on paper: it gets the endpoint called, and billed, by a
                    # re-verification run that was told it was uncallable.
                    fail(errors, where, "untestable but carries a test_request — a re-verify run "
                                        "would call it anyway; drop one of the two")

        if extended_file:
            # extended files are machine-generated from PUBLIC specs and public target ids (TikTok
            # secUids, WeChat export ids, CDN URIs, pagination cursors) — long opaque strings that
            # pattern-match as secrets endlessly. Credentials only ever travel via TREG_CATALOG_CRED
            # env in the verify scripts, so here only the unambiguous leak shape is flagged.
            for m in re.finditer(r"Bearer\s+[A-Za-z0-9+/_=-]{16,}", text):
                fail(errors, name, f"credential literal in file: '{m.group(0)[:24]}…'")
            continue
        for m in LEAK.finditer(text):
            if looks_like_secret(m.group(0)):
                fail(errors, name, f"possible credential literal in file: '{m.group(0)[:24]}…'")

    for e in errors:
        print(e)
    print(f"{'FAIL' if errors else 'OK'} — {len(files)} provider file(s), {len(seen_ids)} endpoint(s), {len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
