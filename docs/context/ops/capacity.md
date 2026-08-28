---
title: Provider capacity — knowing what treg's own vendor accounts have left
status: shipped (step B, observe-only)
sources:
  - src/treg/domain/capacity/__init__.py
  - src/treg/domain/capacity/collectors.py
  - src/treg/domain/capacity/policy.py
  - src/treg/domain/capacity/sweep.py
  - src/treg/domain/capacity/view.py
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

## Not built yet (plan steps C–F)

Forecasts and alerts (C, gated on the `money-funding-transactions` debt); the exhausted view read
in `_platform_offer` and the typed `provider_capacity` 503 (D); burst smoothing (D′); the
overflow child cycle through Orthogonal/Monid (E); enabling routes (F). Until F ships, the charter
row "not built yet: routing/failover" stands and treg still relays a vendor's 402 unchanged.
