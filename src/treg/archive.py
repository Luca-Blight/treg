"""The archive — every platform answer, kept and versioned. (PR 1: skeleton, no behavior.)

Two concepts, one word each (charter discipline):

- **cache**: the newest stored answer for a key, served instead of a vendor call while it is
  fresh. Serving arrives in a later PR and only when `TREG_ARCHIVE_MODE=serve`.
- **archive**: every version of every answer, forever, each with its timestamp. The cache is the
  archive's newest layer. History is deliberately kept — it is the future data product, not waste.

What this module owns: the mode gate, the per-endpoint eligibility policy, and the cache key.
What it will NEVER own: money. A cached hit tags records "cached" and nothing else — billing of a
cached hit is an explicitly deferred product decision, and no code here may touch ledger/billing.

The mode is a three-position switch, staged like a rollout and reversible without a deploy:
  off    → the archive does not exist at runtime (default).
  shadow → record + learn from responses we already relay; serve nothing (phase 0).
  serve  → shadow, plus eligible fresh hits are answered from the store (phase 1+).

Eligibility is three gates, in order, all of which must pass:
  1. Kind — actions (submit/send/write) are never stored; only data reads pass.
  2. License — per catalog entry: `cache: forbidden | transient | archive`, carried with the
     license quote and source URL exactly like `cost` provenance. Absent field ⇒ forbidden:
     a provider nobody has judged must not be stored by default.
  3. Tier — only METERED PLATFORM calls (treg's own vendor key) are recorded; those responses are
     already fully buffered for the settle, so recording adds no latency and no new data path.
     Own-key and own-tool calls stream and are never touched.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import parse_qsl

from .config import get_settings

# ---------------------------------------------------------------------------------------------
# Mode

MODES = ("off", "shadow", "serve")


def mode() -> str:
    """The archive's runtime mode. Any unrecognized value degrades to "off": a typo in an env var
    must disable the feature, never accidentally enable serving."""
    m = (get_settings().archive_mode or "off").strip().lower()
    return m if m in MODES else "off"


def recording() -> bool:
    return mode() in ("shadow", "serve")


def serving() -> bool:
    return mode() == "serve"


# ---------------------------------------------------------------------------------------------
# Eligibility (gates 1 + 2; gate 3 — the tier — is the caller's context and is checked at the
# hook site, where "metered platform call" is already an established fact)

# The catalog's per-entry cache policy values. Absent/unknown ⇒ "forbidden" (see policy()).
CACHE_FORBIDDEN = "forbidden"   # license forbids storing, or nobody has judged it yet
CACHE_TRANSIENT = "transient"   # short-lived cache only; old versions are prunable
CACHE_ARCHIVE = "archive"       # keep versions long-term (public-domain and license-cleared)

_STORABLE = (CACHE_TRANSIENT, CACHE_ARCHIVE)


def policy(entry: dict[str, Any] | None) -> str:
    """Gates 1+2 for one catalog entry, returning the effective cache policy.

    `entry` is the endpoint's catalog mapping (the same dict the resolver already holds). The
    default is FORBIDDEN on every uncertain branch — an entry that is missing, an action, or an
    unjudged license must never be stored. This is the same posture as the platform offer's
    free-only guard: the safe answer is the silent one.
    """
    if not entry:
        return CACHE_FORBIDDEN
    if entry.get("kind") == "action":  # gate 1 — never store an action's answer
        return CACHE_FORBIDDEN
    declared = entry.get("cache")
    if isinstance(declared, dict):  # provenance form: {mode, license_quote, source_url, checked}
        declared = declared.get("mode")
    if declared in _STORABLE:  # gate 2 — an explicit, judged license decision
        return str(declared)
    return CACHE_FORBIDDEN


def storable(entry: dict[str, Any] | None) -> bool:
    return policy(entry) in _STORABLE


# ---------------------------------------------------------------------------------------------
# The cache key



def cache_key(
    method: str,
    endpoint_id: str,
    upstream_url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """One deterministic key per logical request: sha256 over the canonical request shape.

    Canonical means: uppercased method, the catalog endpoint id (so a provider's URL reshuffle
    starts a fresh history instead of poisoning the old one), the URL with its query pairs
    SORTED (param order is transport noise, not meaning), a hash of the body bytes, and the few
    caller headers that can change a vendor's answer (Accept, Accept-Language) — everything else
    (auth, cookies, tracing, encodings) is excluded by the allow-list below: headers vary per
    caller/hop without changing what the vendor computes, and keying on them would shatter one
    logical answer into many dead entries. Credentials never appear at all — injection happens
    after the key is taken.

    POST bodies hash canonically when they parse as JSON (sorted keys, separators pinned), raw
    otherwise: two JSON bodies that differ only in key order are the same question.
    """
    base, _, query = upstream_url.partition("?")
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    kept_headers = sorted(
        (k.lower(), v.strip())
        for k, v in (headers or {}).items()
        if k.lower() in ("accept", "accept-language")
    )
    material = json.dumps(
        {
            "m": method.upper(),
            "e": endpoint_id,
            "u": base,
            "q": pairs,
            "b": _body_digest(body),
            "h": kept_headers,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _body_digest(body: bytes | None) -> str:
    if not body:
        return ""
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return hashlib.sha256(body).hexdigest()
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def content_hash(body: bytes) -> str:
    """The stored body's identity, for deduplication across versions: same bytes, one blob.
    (Change DETECTION will strip noisy fields before comparing — that arrives with the learner in
    a later PR and never alters what is stored: bytes are kept verbatim, always.)"""
    return hashlib.sha256(body).hexdigest()
