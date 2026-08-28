---
title: Provider capacity — knowing what treg's own vendor accounts have left
status: shipped (steps B, B′, D)
sources:
  - src/treg/domain/capacity/__init__.py
  - src/treg/domain/capacity/collectors.py
  - src/treg/domain/capacity/policy.py
  - src/treg/domain/capacity/sweep.py
  - src/treg/domain/capacity/view.py
  - src/treg/domain/capacity/routes.py
  - src/treg/domain/capacity/signatures.py
  - src/treg/domain/capacity/verify.py
  - src/treg/domain/capacity/marks.py
  - tests/test_capacity_protect.py
  - src/treg/domain/capacity/overflow_seed.json
  - src/treg/infra/upstream/aggregators/__init__.py
  - src/treg/infra/upstream/aggregators/orthogonal.py
  - src/treg/infra/upstream/aggregators/monid.py
  - src/treg/infra/upstream/aggregators/catalogs.py
  - alembic/versions/0003_overflow_route.py
  - tests/test_capacity_overflow_routes.py
  - src/treg/worker.py
  - scripts/provider_balances.py
  - alembic/versions/0002_capacity_policy_snapshot.py
  - tests/test_capacity_know.py
related:
  - architecture/data-model.md
  - architecture/money.md
  - architecture/proxy-model.md
  - ops/deploy.md
---

# Provider capacity

**Problem.** Tier 4 serves ~2,850 catalog endpoints on treg's own vendor keys. When one of *our*
accounts runs dry, every caller on that endpoint inherits a 402 that isn't theirs to fix — 4,604
such errors in the 30 days to 2026-08-26, almost all on the enrichment (money) workload. The plan
(`docs/PROVIDER-CAPACITY-PLAN.md`, local) has three layers: **know** the runway, **fund** before it
dies, **protect** the call when it dies anyway (refuse-before-reserve, overflow through an
aggregator on the *same* endpoint, typed 503). This fragment covers what is built: **know**.

Scope: treg-owned platform credentials only. Tiers 1/2 (a caller's own tool or key) are never
consulted or affected by anything here.

## Pieces (`src/treg/domain/capacity/`)

- **`collectors.py`** — the 27 providers' *free* balance/quota calls (`coroutine(client, key) →
  {value, unit, note}`), moved byte-identically from `scripts/provider_balances.py`. Only DataForSEO
  and TikHub speak dollars; everyone else meters credits, rows, searches. `NO_BALANCE_API` names the
  providers that publish no meter (dashboard-only) so they read as "no API", never as a broken key.
  `provider_balance()` never raises — a failure is a row. It reads the *setting*, not
  `platform_key_for`: the tier-4 allow-list is a serving kill switch, and a provider just switched
  off is exactly one whose last balance we still want.
- **`policy.py`** — `CapacityPolicy` defaults per account (`_KNOWN`: capacity type, funding mode,
  source, plus the verified quota/rate facts for lusha, hunter, leadsforge, leadmagic, crustdata,
  tikhub). The population is every `platform_key_*` slot **plus `overflow:orthogonal` /
  `overflow:monid`** (aggregators are prepaid accounts that run dry too). `ensure_policies` inserts
  missing rows only — a hand-edited row is never overwritten — and returns the providers still
  `unknown`, which a person must classify; code never guesses. `latest_state()` is the pure rule:
  no/failed/old (> 6 h) observation → `stale` (never refuses a call); `remaining ≤ 0` on an exact
  observation → `exhausted` until `resets_at`, or until the next sweep can prove otherwise.
- **`sweep.py`** — `run_sweep(db)`: import policies → collect all providers in parallel (DB idle
  while the network is in flight) → one `CapacitySnapshot` per provider → publish each
  `LatestState` to ratestore as `capacity:state:<provider>` (24 h TTL) → one commit. A note that
  looks like a credential is withheld before it is stored. Observe-only: no alerts, no marks the
  call path acts on.
- **`view.py`** — `LatestStateView`: the in-process copy of the published state, reloaded from
  ratestore on a 60 s TTL by an explicit `await load()`; `get()`/`is_exhausted()` are sync and
  I/O-free so `resolve._platform_offer` can read them later without breaking its rule. Invalidation
  story (refactor plan §2.2): time-based only — every replica sees a mark within one TTL. **Nothing
  on the call path reads it yet** (that is step D).

## Where it runs — `treg-worker`

`treg-worker capacity sweep [--only a,b] [--json]` (`src/treg/worker.py`, console script in the
`[server]` extra). It is deliberately **not** a `treg` subcommand: the light CLI may not import the
DB stack (import-linter contract), and the sweep needs the platform keys in the env plus outbound
third-party calls, which are worker-profile work — never dataplane lifespan work. In production it
is a **Render cron job** (`treg-capacity-sweep` in `render.yaml`, hourly) that pulls its env from
the web service via `fromService`; a run against a database missing the tables creates them
(`init_db`). `scripts/provider_balances.py` remains the by-hand reconciliation view (balance beside
ledger spend) over the same collectors.

## Data

`CapacityPolicy` (one row per account; `capacity_type`, `source`, `funding_mode`, auto-funding
fields, runway thresholds, `usd_per_unit_micro` NULL = never invent a dollar figure, `rate_limit`
+ `quota` JSON, `enabled` ⇔ a key exists) and `CapacitySnapshot` (append-only observations:
`remaining`, `total`, `unit`, `resets_at`, `source`, `confidence`, `note`, `error`). Written by the
worker only. Both have the legacy `create_all` path **and** alembic revision `0002` until stage 5;
`tests/test_alembic_baseline.py` proves the two agree. Numbers only — no key or payment detail.

## Boundaries

`treg.domain.capacity` may not import `treg.api`, `treg.routers`, `treg.application`,
`treg.bootstrap`, `treg.audit`, FastAPI or Starlette (contract "Capacity domain does not depend on
outer layers"). Money is never touched: capacity marks are ratestore rows, never balances.

## Overflow routes (step B′) — derived, never hand-written

**Overflow** = the *same* vendor endpoint served through a treg-owned **aggregator** account
(Orthogonal first, Monid second) when our direct account is out. It is a credential rung
(`platform-overflow`), not a vendor: not in the catalog, not searchable, no BYO key. The caller
pays the aggregator's real price, 0% markup, disclosed in-band when it ships (step E).

- **`OverflowRoute`** (`overflowroute`, alembic `0003`): one row per `(endpoint_id, aggregator)` —
  the aggregator's slug/path spelling, its list price (micro-USD), `agg_unit` (call | result),
  `ratio` = aggregator price ÷ our per-event price, `single_result`, `last_verified_at`, and a
  DERIVED `enabled` with `disabled_reason`. Worker-owned; the call path will only read it.
- **`routes.py`** — the rules, in one place (`eligible`): platform-eligible · policy allows
  overflow (tikhub and scrapecreators are barred by decision) · same unit (a per-result
  aggregator price is accepted for a per-call endpoint that returns ≤ 1 record; Hunter's "one
  credit per 10 emails" compares as a per-call price) · `ratio ≤ 4.0`, or for a FREE endpoint of
  ours an aggregator price ≤ `FREE_ROUTE_MAX_USD` (1¢ — free routes still 402 when the account is
  dry) · verified within 7 days. `match_catalogs` derives candidates from the aggregators' catalogs
  by exact `(host, method, path)` (Orthogonal) / `(provider, path)` (Monid); `apply_sync` upserts
  and re-derives `enabled`, and disables any row missing from the current sync.
- **The seed** — `overflow_seed.json`: the 461 candidate pairs from the 2026-08-26 mapping run,
  145 of them carrying `verified_at` (direct vs relay, identical body shape, in-band price = list
  price). Under the rules, `treg-worker overflow sync` enables **113** of the 145 (pinned by test);
  the rest are off for a named reason — 23 are our per-result price vs the aggregator's per-call
  price (an open decision, plan §7), 7 are not platform-eligible, the remainder have no aggregator
  price, a 56× ratio, or a barred provider. The enabled count decays to 0 after 7 days without
  `treg-worker overflow verify`.
- **`signatures.py`** — the signature table: what a provider's error body means for OUR account
  (`balance` / `quota` → exhausted; `burst` → smoothed, never exhausted; `unknown` 429 → logged).
  Lusha's "Daily" 429 and Hunter's "per billing period" 429 are quota exhaustion wearing a 429;
  a `retry-after ≤ 60 s` is a burst. Shared by the sweep, the future call-path trigger and alerts.
- **`infra/upstream/aggregators/`** — the envelopes, and nothing else: `build()` wraps the
  vendor request (Orthogonal `POST /run {api, path, query, body}`; Monid `POST /run {provider,
  endpoint, input}`), `parse()` unwraps the vendor status + body + the real in-band charge, and
  names who to blame when the aggregator itself refused (`aggregator_auth`, `aggregator_balance`,
  `contract` = its stricter schema, no vendor call, no charge; `pending` = a Monid async run to
  poll). Fixtures are recorded bodies (PII hashed) in `tests/fixtures/aggregators/`; every fixture
  round-trips. Keys are passed in by the caller and never read, logged or stored here.
- **`verify.py`** + `treg-worker overflow verify` — the weekly re-verify: one cheap call per
  route through the aggregator (and, when we hold the vendor key, directly), compare the shape
  fingerprint (keys and list/leaf markers, values ignored), stamp `last_verified_at` or disable
  with the reason. Spends real money (bounded by `--max-usd`, default 2¢); needs the aggregator
  keys in the env — a Render cron, never the dataplane.

## Protect, part one (step D) — refuse before reserve

The call path now reads the view and writes one mark (`marks.py`); the mechanics and the
typed `provider_capacity` 503 are documented in `architecture/proxy-model.md` § Platform capacity
and `interface/api.md`. In one line: exhausted provider → 503 before any hold, with alternatives
named; a balance/quota signature on treg's key → exhausted mark in ratestore for the next caller;
burst 429s only logged until D′. Tiers 1/2 untouched.

## Not built yet (plan steps C–F)

Forecasts and alerts (C, gated on the `money-funding-transactions` debt); burst smoothing (D′); the
overflow child cycle through Orthogonal/Monid (E); enabling routes (F). Until F ships, the charter
row "not built yet: routing/failover" stands and treg still relays a vendor's 402 unchanged.
