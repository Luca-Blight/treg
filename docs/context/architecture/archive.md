---
title: Archive — every platform answer, kept and versioned (cache = the newest layer)
status: building
sources:
  - src/treg/archive.py
  - alembic/versions/0002_archive_tables.py
  - src/treg/api.py
  - src/treg/bootstrap.py
related:
  - architecture/data-model.md
  - architecture/proxy-model.md
  - architecture/catalog.md
---

# The archive

Two concepts, one word each — the vocabulary is deliberate and mirrors the charter's discipline:
**cache** is the newest stored answer for a key, served instead of a vendor call while it is
fresh; **archive** is every version of every answer, kept with its timestamp. The cache is the
archive's top layer. History is kept on purpose: it is the future data product (per-key
time-series — backlink profiles over time, price history), not waste.

**Build state (PR 2 of 5).** The skeleton (PR 1) and the recorder (PR 2) exist: in `shadow` or
`serve` mode, every metered 2xx platform answer is observed. The catalog `cache` field at scale
(PR 3), the serve path (PR 4) and the timer learner + refresh worker (PR 5) land behind the same
switch. Do not document any of those as existing until they do.

## The recorder (PR 2)

Hooked in `call_tool` immediately after `_buffer_response` — the one line where "metered platform
call, body already in memory" is a fact, which IS eligibility gate 3. Metered 2xx only; the
`X-Treg-Cache`-style serve headers do not exist yet. `archive.record()` is fire-and-forget with
audit's discipline: bounded pending set (512), failures swallowed with a log line, `drain()` on
shutdown (bootstrap) and in tests. A recorder crash cannot fail a call (tested).

**Counted vs kept.** Statistics and the raw-body `content_hash` are recorded for every observed
answer (a hash is an identity, not the content); body BYTES are kept only when the entry's cache
policy is `transient`/`archive` AND the body fits `archive_max_body_bytes` (default 2 MB —
skipped whole, never truncated). Consecutive identical answers dedup via `body_of`; an identical
answer arriving where bytes were never kept stores them now, so a policy upgrade heals forward
without a backfill. The key URL is rebuilt as the vendor sees it (resolved upstream + forwarded
caller params, resolution-consumed names excluded) — credential injection happens later, in the
relay, so a credential cannot enter the key or the store from the request side. One considered
edge for PR 3's per-provider judgment: a vendor that ECHOES the request credential in a 2xx body
would have it stored — judge `cache` per provider with that in mind.

## The mode switch

`TREG_ARCHIVE_MODE` (config `archive_mode`, default `off`) → `archive.mode()`:
`off` | `shadow` (record + learn, serve nothing — phase 0) | `serve` (shadow + answer eligible
fresh hits — phase 1+). Any unrecognized value degrades to `off`: a typo must disable, never
enable. Rollback in production is a dashboard env edit, no deploy.

## Eligibility — three gates, in order

1. **Kind.** `kind: action` entries are never stored; only data reads pass.
2. **License.** Per catalog entry: `cache: forbidden | transient | archive` — either a bare
   string or a provenance dict `{mode, license_quote, source_url, checked}`, exactly like `cost`
   provenance. **Absent ⇒ forbidden**: an unjudged provider is never stored (the same posture as
   the platform offer's free-only guard — the safe answer is the silent one).
3. **Tier.** Only METERED PLATFORM calls are recorded. Those responses are already fully buffered
   for the settle (`_buffer_response` needs the provider's reported cost), so recording adds no
   latency and no new data path. Own-key and own-tool calls stream and are never touched — that
   is the privacy line, enforced at write time, not filtered at read time.

Gates 1+2 are `archive.policy(entry)`; gate 3 is the hook site's own context.

## The cache key

`archive.cache_key(method, endpoint_id, upstream_url, body, headers)` → sha256 over the canonical
request: uppercased method, catalog endpoint id (a provider URL reshuffle starts a fresh history),
sorted query pairs, canonical-JSON body hash (raw hash for non-JSON), plus only `Accept` and
`Accept-Language` from the caller's headers. Auth/cookies/tracing/encodings never enter the key —
and credentials could not anyway: injection happens after the key is taken.

## Tables (migration 0002)

`ArchiveKey` — one logical question: `key_hash` (unique), `endpoint_id`, `provider`, effective
`policy`, AIMD timer state (`ttl_s`, grow ×1.5 capped on stable refetch / shrink ×0.5 floored on
change — the learner lands in PR 5), change statistics (`change_seen`/`stable_seen`/
`last_changed_at`), learned `volatile_paths` (noisy JSON paths excluded from change detection,
never from stored bytes), and demand (`heat`, `last_requested_at`). Platform-scoped, no `org_id`:
one team's fetch may warm another team's hit, and own-key traffic never enters.

`ArchiveSnapshot` — one version: unique `(key_id, version)`, verbatim `body` bytes, `content_hash`
(raw sha256) for dedup — an identical consecutive answer stores a version row with `body=NULL,
body_of=<carrier row>` instead of the bytes again. Bodies live in Postgres (the `IdempotentCall`
precedent); oversized bodies are skipped by the recorder, never truncated. `origin` says who
fetched: `caller` | `refresh` | `sample`.

## What the archive must never touch

Money. A cached hit will only TAG existing records "cached" — billing of a cached hit is an
explicitly deferred founder decision, and no archive code imports ledger/billing. Relay
faithfulness also extends through time: served bytes are exactly what the vendor sent; noisy-field
stripping exists only on comparison copies inside change detection.

## Tests

`tests/test_archive.py` — mode degradation, policy refusal-by-default, key canonicalization, and
table round-trips; listed in CI's serial Postgres job (never xdist — shared database).
