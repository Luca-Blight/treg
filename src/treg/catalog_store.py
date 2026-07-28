"""Reader for the endpoint catalog (`src/treg/catalog/*.yaml`) — the operations layer.

The marketplace registry (`oauth_providers.py`) catalogs CREDENTIALS: how to connect a provider.
This reads the other half: what you can DO once connected — which endpoints exist per platform,
what they cost, and which ones were actually verified against the live API. See
`docs/context/architecture/catalog.md` for the data's schema and curation process.

SERVER-SIDE ONLY. The CLI must never import this (it pulls in pyyaml, which is a `[server]` extra);
`treg catalog` is a thin client over the /catalog/* endpoints like every other CLI command.

The data is read-only and changes only on deploy, so it is parsed once and cached in the process.
A missing or half-written catalog dir degrades to an empty catalog rather than failing startup —
serving no endpoints is a worse product, not a broken server.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

CATALOG_DIR = Path(__file__).parent / "catalog"
EXAMPLES_DIRNAME = "examples"
CAPABILITIES_FILE = "capabilities.yaml"
FX_FILE = "fx.yaml"


@dataclass(frozen=True)
class Catalog:
    fx: dict[str, float] = field(default_factory=dict)           # currency -> USD rate (fx.yaml)
    platforms: dict[str, dict] = field(default_factory=dict)     # slug -> {label, category}
    capabilities: dict[str, str] = field(default_factory=dict)   # id -> description
    endpoints: list[dict] = field(default_factory=list)          # normalized endpoint dicts
    by_id: dict[str, dict] = field(default_factory=dict)
    provider_meta: dict[str, dict] = field(default_factory=dict)  # service -> {limits, pricing_url, docs}

    def for_capability(self, capability: str) -> list[dict]:
        return [e for e in self.endpoints if capability and e["capability"] == capability]

    def for_platform(self, slug: str) -> list[dict]:
        return [e for e in self.endpoints if e["platform"] == slug]

    def for_provider(self, service: str) -> list[dict]:
        return [e for e in self.endpoints if e["provider"] == service]

    def cost_view(self, cost) -> dict | None:
        """A cost dict with a computed `usd` field. Source of truth stays in the provider's
        BILLING currency; USD is derived at serve time from fx.yaml so a rate refresh re-prices
        the whole catalog at once. `usd` is None when the value or rate is unknown."""
        if not isinstance(cost, dict):
            return None
        value, cur = cost.get("value"), cost.get("currency", "USD")
        rate = self.fx.get(cur)
        usd = round(value * rate, 6) if isinstance(value, (int, float)) and rate else None
        return {**cost, "usd": usd}


_CACHE: Catalog | None = None


def load(*, refresh: bool = False, directory: Path | None = None) -> Catalog:
    """The parsed catalog, cached for the life of the process. `refresh`/`directory` exist for tests."""
    global _CACHE
    if directory is not None:
        return _parse(Path(directory))
    if _CACHE is None or refresh:
        _CACHE = _parse(CATALOG_DIR)
    return _CACHE


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse(directory: Path) -> Catalog:
    if not directory.is_dir():
        return Catalog()
    taxonomy = _read_yaml(directory / CAPABILITIES_FILE)
    # Platform values are {label, category} mappings (category = the marketplace tab the platform
    # browses under); a bare-string value is accepted as a label-only shorthand.
    platforms = {
        slug: (v if isinstance(v, dict) else {"label": str(v)}) | {"category": (v.get("category") if isinstance(v, dict) else None) or "Other"}
        for slug, v in (taxonomy.get("platforms") or {}).items()
    }
    capabilities = dict(taxonomy.get("capabilities") or {})

    endpoints: list[dict] = []
    by_id: dict[str, dict] = {}
    provider_meta: dict[str, dict] = {}
    # Sorted so a rebuild is deterministic; `<service>.extended.yaml` sorts right after
    # `<service>.yaml`, and both land under the same provider (curation splits a provider's
    # core operations from the long tail across two files).
    for path in sorted(directory.glob("*.yaml")):
        if path.name in (CAPABILITIES_FILE, FX_FILE):
            continue
        doc = _read_yaml(path)
        provider = doc.get("provider") or path.name.split(".")[0]
        # A capability a provider file proposes is not yet in the shared taxonomy, but it is
        # already the join key of its endpoints — carry its description so the API can label it.
        for cap, desc in (doc.get("proposed_capabilities") or {}).items():
            capabilities.setdefault(cap, desc)
        # Provider-wide facts live once at the top of the file, not on every endpoint; the detail
        # view reunites them with the endpoint. `<service>.extended.yaml` fills only what its core
        # file left blank, so the curated file stays authoritative.
        meta = provider_meta.setdefault(provider, {})
        for key, value in (("limits", doc.get("limits")),
                           ("pricing_url", doc.get("pricing_url")),
                           ("docs", (doc.get("source") or {}).get("docs"))):
            if value and not meta.get(key):
                meta[key] = str(value)
        for raw in doc.get("endpoints") or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            ep = _normalize(raw, provider, directory)
            if ep["id"] in by_id:  # first file wins; ids are unique by validator contract
                continue
            by_id[ep["id"]] = ep
            endpoints.append(ep)

    for ep in endpoints:  # a platform seen only in provider files still deserves a label
        platforms.setdefault(ep["platform"], {"label": ep["platform"], "category": "Other"})
    fx = dict((_read_yaml(directory / FX_FILE) or {}).get("rates_to_usd") or {"USD": 1.0})
    return Catalog(fx=fx, platforms=platforms, capabilities=capabilities, endpoints=endpoints,
                   by_id=by_id, provider_meta=provider_meta)


# The subject an endpoint is ABOUT, within its platform — the section a platform page files it
# under. Order matters: the first hit wins, so the specific keys ("showcase", "hashtag") sit ahead of
# the general ones they contain a word of. Deliberately shared across platforms: a douyin "comment"
# route and a tiktok one belong under the same heading, and one table beats sixty bespoke ones.
DOMAIN_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("backlink", "backlinks"), ("llm_mention", "mentions"), ("llm_response", "ai answers"),
    ("llm_scraper", "ai answers"), ("llm", "ai answers"), ("keyword", "keywords"), ("serp", "serp"),
    ("shop", "shop"), ("showcase", "shop"), ("product", "shop"),
    ("ads", "ads"), ("ad_", "ads"),
    ("analytics", "analytics"), ("creator", "creator"), ("search", "search"),
    ("hashtag", "hashtag"), ("challenge", "hashtag"),
    ("comment", "video"), ("video", "video"), ("post", "video"), ("aweme", "video"),
    ("music", "music"), ("sound", "music"), ("live", "live"),
    ("user", "user"), ("profile", "user"), ("follow", "user"),
    ("feed", "feed"), ("publish", "publishing"),
)
DOMAIN_OTHER = "other"
# Path segments that say how a route is CALLED, not what it is about. DataForSEO ends every
# synchronous route in `/live`, which read as a subject and filed 33 unrelated SEO endpoints under a
# "live" heading — the same trap for a version prefix or a `/json` suffix.
DOMAIN_NOISE = {"live", "task_post", "task_get", "task_ready", "tasks_ready", "tasks_fixed",
                "json", "html", "xml", "api", "rest", "open", "web", "app", "mobile", "desktop",
                # provider BRAND/family segments: how the vendor organises its API, not what the
                # route is about — reading them as a subject put a "dataforseo_labs" heading on the
                # Google page. Filtered here so such routes classify by their function keywords.
                "dataforseo_labs", "appendix", "ai_optimization"}
_VERSION_SEG = re.compile(r"v\d+(?:\.\d+)*")
_WORD_SEG = re.compile(r"[a-z][a-z0-9_]*")


def _domain(raw: dict, capability: str, platform: str) -> str:
    """Which section of its platform's page this endpoint belongs to.

    Four sources, best first: the yaml's own `domain:` (curation always wins), the capability id's
    middle segment (`tiktok.video.comments` → video — the taxonomy already encodes the subject), a
    keyword read off the path and summary, and failing that the path's own leading subject segment
    (`/v3/backlinks/anchors/live` → backlinks), which is usually how a provider organises its API.
    """
    explicit = str(raw.get("domain") or "").strip().lower()
    if explicit:
        return explicit
    parts = capability.split(".")
    if len(parts) >= 3 and parts[1]:
        return parts[1]
    segments = [s for s in str(raw.get("path") or "").lower().split("/")
                if s and s not in DOMAIN_NOISE and not _VERSION_SEG.fullmatch(s)]
    # The PATH only, never the summary. Prose says "the Live SERP API…" and "your own post…" about
    # endpoints that are neither, and one false heading is worse than a row in `other`: a section a
    # visitor doesn't trust is a section they stop reading.
    haystack = " ".join(segments)
    for key, domain in DOMAIN_KEYWORDS:
        if key in haystack:
            return domain
    # Only a GROUPING segment counts — one with an operation name after it. The last segment IS the
    # operation ("generate_xbogus"), and a section per operation is not a section at all.
    candidates = [s for s in segments if s != platform and _WORD_SEG.fullmatch(s)]
    return candidates[0] if len(candidates) > 1 else DOMAIN_OTHER


def _effective_cost(raw: dict):
    """The price to display: the declared cost block, else the price the verifier OBSERVED.

    Providers that price per API family (DataForSEO) ship no per-route cost block, which left every
    row reading "—" even after live verification had recorded the provider's own stated charge.
    An observed price is real money that was actually billed — better display data than silence.
    Zero observed cost means the call was genuinely free (some routes bill nothing)."""
    cost = raw.get("cost")
    if isinstance(cost, dict) and (cost.get("value") is not None or cost.get("type") == "free"):
        return cost
    oc = raw.get("observed_cost")
    if oc is None:
        return cost or None
    if oc == 0:
        return {"type": "free", "note": "no charge observed at verification"}
    return {"type": "per_call", "value": oc, "currency": "USD",
            "note": "price observed at live verification (provider-reported charge)"}


def _normalize(raw: dict, provider: str, directory: Path) -> dict:
    capability = raw.get("capability") or ""
    platform = raw.get("platform") or (capability.split(".")[0] if capability else "other")
    verified = raw.get("verified")
    return {
        "id": str(raw["id"]),
        "provider": provider,
        "capability": capability,
        "platform": platform,
        "domain": _domain(raw, capability, platform),
        "scope": raw.get("scope") or "",
        "method": (raw.get("method") or "GET").upper(),
        "path": raw.get("path") or "",
        # optional short display title; `summary` stays the provider's own description, verbatim
        "name": str(raw.get("name") or "").strip(),
        "summary": raw.get("summary") or "",
        "input": raw.get("input") or {},
        # the request the verifier actually made — a proven set of values, which is what makes
        # `call_template` a paste-ready line rather than a shape with placeholders in it
        "test_request": raw.get("test_request") or {},
        "cost": _effective_cost(raw),
        # Absent `tier` means core: the curated first wave predates the split, and treating an
        # unmarked endpoint as extended would hide it from the platform view entirely.
        "tier": raw.get("tier") or "core",
        "verified": str(verified) if verified else None,
        "docs_url": raw.get("docs_url") or "",
        "example_file": _example_file(raw, directory),
    }


def _example_file(raw: dict, directory: Path) -> str | None:
    """The captured example response's filename, or None when it isn't on disk.

    Only the BASENAME of the declared path is honoured — examples live in one flat directory, and a
    data file must never be able to point the server at an arbitrary path.
    """
    declared = raw.get("example_response") or f"{EXAMPLES_DIRNAME}/{raw['id']}.json"
    name = Path(str(declared)).name
    if not name.endswith(".json"):
        return None
    return name if (directory / EXAMPLES_DIRNAME / name).is_file() else None


def example_path(endpoint_id: str) -> Path | None:
    """Filesystem path of a KNOWN endpoint's example response, or None.

    The id is resolved through the loaded catalog first, so nothing a caller types is ever joined
    into a path — traversal attempts simply miss the lookup.
    """
    ep = load().by_id.get(endpoint_id)
    if ep is None or not ep["example_file"]:
        return None
    return CATALOG_DIR / EXAMPLES_DIRNAME / ep["example_file"]


# ---- shaping ---------------------------------------------------------------------------------

def endpoint_view(ep: dict, provider_display: str, cat: Catalog | None = None) -> dict:
    """The public JSON shape of one endpoint (the dashboard + CLI render this)."""
    return {
        "id": ep["id"],
        "provider": ep["provider"],
        "provider_display": provider_display,
        # short display title (may be ""); clients fall back to `summary` when absent
        "name": ep.get("name") or "",
        "summary": ep["summary"],
        "method": ep["method"],
        "path": ep["path"],
        "scope": ep["scope"],
        "tier": ep["tier"],
        # the section of its platform page this row files under (see `_domain`)
        "domain": ep.get("domain") or DOMAIN_OTHER,
        # the whole point of a row is the call it stands for, so the line that makes it is part of
        # the row — not a second request away
        "call_template": call_template(ep),
        "cost": cat.cost_view(ep["cost"]) if cat else ep["cost"],
        "verified": ep["verified"],
        "docs_url": ep["docs_url"],
        "has_example": bool(ep["example_file"]),
        # the request schema, split by location (pathParams/queryParams/body + notes) — without it
        # the dashboard can show what comes BACK (example_response) but not what to SEND
        "input": ep.get("input") or None,
    }


def endpoint_context(ep: dict, cat: Catalog) -> dict:
    """The join keys an endpoint row needs to stand ALONE (search results, the detail view) — inside
    a platform page these are implied by the page itself, so `endpoint_view` leaves them out."""
    return {
        "capability": ep["capability"],
        "capability_description": cat.capabilities.get(ep["capability"], ""),
        "platform": ep["platform"],
        "platform_label": cat.platforms.get(ep["platform"], {}).get("label", ep["platform"]),
    }


def domain_rows(pairs: list[tuple[dict, dict]], capabilities: dict[str, str]) -> list[dict]:
    """One platform's `(endpoint, view)` pairs as the LEDGER the platform page renders: domain
    sections, each holding merged rows (a job several providers do) before single rows.

    The ordering is decided here rather than in the browser so every client — and every screenshot —
    agrees on it: `other` last because it is the junk drawer, the rest busiest-first because the
    biggest section is the one a visitor came for, and merged before single because a comparable job
    is worth more than a lone route.
    """
    by_cap: dict[str, list[dict]] = {}
    rows: list[dict] = []
    for ep, view in pairs:
        if ep["capability"]:
            by_cap.setdefault(ep["capability"], []).append(view)
        else:
            # `name` is the curated short title; `summary` is documentation prose and can run to a
            # paragraph, so it leads a row only until a name exists for that endpoint.
            rows.append({"kind": "single", "capability": None,
                         "description": view["name"] or view["summary"],
                         "domain": view["domain"], "endpoints": [view]})

    for cap, eps in by_cap.items():
        title = capabilities.get(cap, "") or cap
        # A capability only ONE provider implements is not a comparison, so it does not merge: it
        # becomes a row per endpoint, each led by its own summary, same as an unmapped route. Folding
        # a provider's two takes on one job into a single row would show one route and the OTHER's
        # price. The capability id stays on every row either way; it is a join key, never a heading.
        if len({e["provider"] for e in eps}) > 1:
            rows.append({"kind": "merged", "capability": cap, "description": title,
                         "domain": eps[0]["domain"], "endpoints": eps})
            continue
        rows += [{"kind": "single", "capability": cap, "description": e["name"] or e["summary"] or title,
                  "domain": e["domain"], "endpoints": [e]} for e in eps]

    sections: dict[str, list[dict]] = {}
    for row in rows:
        sections.setdefault(row["domain"], []).append(row)
    for section in sections.values():
        section.sort(key=lambda r: (r["kind"] != "merged", r["description"].lower(), r["endpoints"][0]["id"]))
    order = sorted(sections, key=lambda d: (d == DOMAIN_OTHER, -len(sections[d]), d))
    return [{"domain": d, "rows": sections[d]} for d in order]


# ---- search ----------------------------------------------------------------------------------
# Deliberately plain: token containment over a few weighted fields. The catalog is ~2k endpoints of
# curated English, so an embedding index would add a dependency and a build step to beat "tiktok
# comments" by nothing. Ranking has to be PREDICTABLE above all — an agent that can't guess why a row
# won can't refine its query.
_SPLIT = re.compile(r"[^a-z0-9]+")

# what a token hit is worth, by where it landed
W_CAPABILITY = 3   # capability id/description + platform label/slug — the curated vocabulary
W_SUMMARY = 2      # the endpoint's own one-line description
W_PATH = 1         # id, path, provider — a hit here is incidental as often as it is meaningful


def _tokens(text: str) -> list[str]:
    return [t for t in _SPLIT.split(text.lower()) if t]


def _haystacks(ep: dict, cat: Catalog) -> list[tuple[int, str]]:
    plat = cat.platforms.get(ep["platform"], {})
    return [
        (W_CAPABILITY, " ".join((ep["capability"], cat.capabilities.get(ep["capability"], ""),
                                 plat.get("label", ""), ep["platform"])).lower()),
        (W_SUMMARY, ep["summary"].lower()),
        (W_PATH, " ".join((ep["id"], ep["path"], ep["provider"])).lower()),
    ]


def search(query: str, cat: Catalog, limit: int = 25) -> tuple[list[tuple[dict, int]], int]:
    """`(ranked [(endpoint, score)], total_matches)` for a free-text query.

    EVERY token must match somewhere (AND, not OR): a query is a refinement, so "tiktok comments"
    must not return every tiktok endpoint. Score sums each token's best field weight; ties break to
    core-before-extended, then verified-before-not, then id — so the ordering is total and stable.
    """
    tokens = _tokens(query)
    if not tokens:
        return [], 0
    scored: list[tuple[dict, int]] = []
    for ep in cat.endpoints:
        fields = _haystacks(ep, cat)
        score = 0
        for tok in tokens:
            best = max((w for w, text in fields if tok in text), default=0)
            if not best:
                break
            score += best
        else:
            scored.append((ep, score))
    scored.sort(key=lambda row: (-row[1], row[0]["tier"] != "core", not row[0]["verified"], row[0]["id"]))
    return scored[:max(limit, 0)], len(scored)


# ---- call template ---------------------------------------------------------------------------
def _placeholder(spec: dict) -> str:
    example = spec.get("example")
    if example not in (None, ""):
        return str(example)
    return f"<{spec.get('type') or 'value'}>"


def _required_examples(params) -> dict:
    """Required params only, valued by their documented example (else a typed placeholder). Optional
    params are omitted — the template is the SHORTEST call that works, not a parameter reference."""
    if not isinstance(params, dict):
        return {}
    return {k: _placeholder(v) for k, v in params.items()
            if isinstance(v, dict) and v.get("required")}


def call_template(ep: dict) -> str:
    """A paste-ready `treg call …` line for this endpoint.

    Values come from `test_request` first — that request was actually run against the live API on the
    verification date, so the line is known-good rather than merely well-shaped.

    The target is the ENDPOINT ID, not `<provider> <path>`: the id form works with no registered
    tool (the server walks the marketplace credential ladder), and path `{placeholders}` ride as
    `--query` params the server substitutes — so the line is copy-paste-true for everyone.
    """
    inp = ep.get("input") or {}
    test = ep.get("test_request") or {}

    parts = ["treg call", ep["id"]]
    if ep["method"] != "GET":
        parts += ["--method", ep["method"]]
    # Path params first (the server folds them into the path), then query params proper.
    for name, value in {**_required_examples(inp.get("pathParams")), **(test.get("pathParams") or {})}.items():
        parts += ["--query", f"{name}={value}"]
    for name, value in {**_required_examples(inp.get("queryParams")), **(test.get("queryParams") or {})}.items():
        parts += ["--query", f"{name}={value}"]

    body = test.get("body") if test.get("body") is not None else (_required_examples(inp.get("body")) or None)
    if body:
        # single-quoted so the JSON's own quotes survive a paste into any POSIX shell
        parts += ["--data", "'" + json.dumps(body, separators=(",", ":"), ensure_ascii=False) + "'"]
    return " ".join(parts)


def _input_hint(ep: dict) -> str:
    """The one thing a stamped example can't show from method+path: what to send."""
    inp = ep.get("input") or {}
    note = (inp.get("note") or "").strip()
    required: list[str] = []
    for where in ("pathParams", "queryParams", "body"):
        params = inp.get(where)
        if isinstance(params, dict):
            required += [k for k, v in params.items() if isinstance(v, dict) and v.get("required")]
    bits = []
    if required:
        bits.append("required: " + ", ".join(required))
    if note:
        bits.append(note)
    return "; ".join(bits)


def tool_examples(service: str) -> list[dict]:
    """Catalog knowledge in the shape `Tool.examples` already uses — `{method, path, note}`.

    Only VERIFIED core endpoints: a tool's examples are a promise that the call works, and the
    catalog's `verified` date is the only evidence we have that it does.
    """
    out = []
    for ep in load().for_provider(service):
        if ep["tier"] != "core" or not ep["verified"] or not ep["path"]:
            continue
        note = ep["summary"] or ep["id"]
        hint = _input_hint(ep)
        if hint:
            note = f"{note}. {hint}"
        if ep["capability"]:
            note = f"{note} [capability: {ep['capability']}]"
        out.append({"method": ep["method"], "path": ep["path"], "note": note})
    return out
