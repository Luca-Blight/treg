---
title: Google Ads conversion tracking — capture, outbox, upload
status: shipped
sources:
  - src/treg/adsconv.py
  - src/treg/web/adtrack.js
related:
  - architecture/money.md
  - architecture/multi-tenancy.md
  - architecture/data-model.md
  - architecture/proxy-model.md
  - ops/deploy.md
---

# Google Ads conversion tracking

Off unless both `google_ads_customer_id` and `ads_conv_org_slug` are set (`adsconv.enabled()`) —
keeps the test suite and self-hosted instances from starting a task that only ever no-ops. When off,
nothing is captured, queued or uploaded; the whole feature is additive.

## The chain: capture → store → fire → upload

1. **Capture** (`web/adtrack.js`, a first-party script, no Google tag). It reads `gclid`/`gbraid`/
   `wbraid` off the URL — `gbraid`/`wbraid` are what Google substitutes on iOS traffic, and omitting
   them silently drops a large share of mobile conversions — and writes them into a `treg_ad` cookie
   (90 days, the length of Google's click-through attribution window; `SameSite=Lax` so it survives
   the cross-site top-level navigation an ad click is). No third-party request is made from the
   browser at any point.
2. **Store** (`api._ad_attribution_from`, read at BOTH signup doors — `register_user` (`POST /users`)
   and `create_org` (`POST /orgs`), since a browser visitor who clicked an ad can land on either).
   The cookie is decoded and persisted onto the new `Org`: `ad_gclid`, `ad_landing` (the use-case page
   slug or `ref`/`utm_content`), `ad_click_at`. Once set, never overwritten — attribution is "first
   click that led to a signup," not "most recent."
3. **Fire** — three chokepoints call `adsconv.queue(db, org, action, ...)`, which writes an
   `AdConversion` outbox row inside a `SAVEPOINT` (a nested transaction, not a bare flush or a
   `db.rollback()` — this runs inside the CALLER's transaction, and a plain rollback on a duplicate
   would roll back their work too, e.g. undoing a Stripe credit on a redelivered webhook):
   - `api._grant_signup_promo` → `ACTION_SIGNUP`, queued BEFORE `ledger.grant()`.
   - `api._record_first_call` → `ACTION_FIRST_CALL`, on the org's first successful `/call/`.
   - `billing._credit` → `ACTION_PAID`, on the org's first credited top-up, carrying `value_usd_micro`.
   `queue()` no-ops (returns `False`) for an org with no `ad_gclid` — most orgs — so the conversion
   side stays ad-attributed-only while the product metric it rides alongside (`first_call_at`) is set
   for every org.
4. **Upload** (`adsconv.worker`, started from `lifespan` when `adsconv.enabled()`, drains every 300s).
   `drain_once` selects due rows — not yet uploaded, older than a 6-hour delay, under 8 attempts — and
   POSTs one batch to the Google Ads API `uploadClickConversions` with `partialFailure` (one bad row
   can't reject the batch). The delay exists because a `gclid` is not valid for upload until hours
   after the click; uploading too early is rejected. A failed row is left for the next pass, never
   dropped silently, until `_MAX_ATTEMPTS` is hit.

## Idempotency: the outbox's unique constraint, not a check-then-insert

`AdConversion` has one row per `(org_id, action)` (`uq_adconversion_org_action`) — this, not an
application-level check, is what makes every fire site safe under a retried signup or a redelivered
Stripe webhook: `queue()` tries the insert and treats the resulting `IntegrityError` as "already
recorded," rather than racing a SELECT against a concurrent insert.

## Durable outbox, deliberately NOT audit.py or analytics.py

`audit.py` and `analytics.py` are both deliberately **droppable** — `audit.py` sheds rows past its
queue bound, `analytics.py` is lossy by design — because losing a `CallRecord` or a PostHog event
costs nothing but a metric. A lost `AdConversion` is different: it is a conversion Google never learns
about, which trains the campaign's bidding on undercounted data, silently, in the direction that makes
the campaign look worse than it is. So the write is durable (a row, in the firing code's transaction)
and only the upload is best-effort/retried. Nothing in `adsconv.py` routes through `audit.py`.

## `first_call_at` is NOT derived from `CallRecord`

`Org.first_call_at` is set by a conditional `UPDATE` in `api._record_first_call`, not read off the
audit table. `audit.py` sheds rows under exactly the load that makes "first call" data most valuable —
a busy launch — so deriving the flag from `CallRecord` would undercount precisely when traffic is
highest. `_record_first_call` runs on its **own session**, opened fresh via `session_maker()`, and
never raises into the caller's response. This is deliberate: an earlier version committed on the
request's own `db` session mid-settlement (the proxy call was still being billed) and broke 8 billing
tests, because that reaches into the money transaction from outside `ledger.py` — the same rule
`money.md` states for ledger writes applies here by extension, even though `adsconv.py` itself never
touches balance.

## Atomicity: two of three fire sites are atomic with their event, one is not

- **`signup`** — atomic. `adsconv.queue()` runs before `ledger.grant()` in `_grant_signup_promo`, and
  `grant()`'s own commit lands both rows together.
- **`first_call`** — atomic. `_record_first_call` queues the conversion and commits once, on its own
  session.
- **`paid`** — **not atomic**. `billing._credit` calls `ledger.topup()`, which commits internally
  (`ledger.py`'s money-write rule), before it queues the `paid` conversion and commits that
  separately. A crash between the two commits loses the conversion permanently: a Stripe webhook
  redelivery finds the payment already credited (`fresh` is `False`), and the fire site that would
  have queued the conversion never runs again.

  This gap was found in review and the decision (2026-08-17) was to **accept it and document it
  honestly** rather than restructure `ledger.py` to make the credit and the conversion one
  transaction. The cheap fix, if the gap ever matters in practice: a reconciliation sweep over orgs
  that have a credited payment (a `CreditBlock`) and a `gclid` but no `paid` `AdConversion` row, in
  the shape of `reconcile.py`'s other read-only reports. Not built.

## Fixed FX: 1 AUD = 0.70 USD, set 2026-08-17

`usd_micro_to_aud_micro` converts at a **constant** rate (`AUD_PER_USD_NUM=10, AUD_PER_USD_DEN=7`),
deliberately not a live rate, so a change in reported ROAS means the business moved, not that the
currency market did. Integer arithmetic throughout (`usd_micro * 10 // 7`) — the one permitted float
is at the JSON boundary in `build_payload`, because the Ads API's `conversionValue` field is a wire
double; the value that reaches it is computed from the already-integral micro amount, never the other
way around. The outbox stores the original USD amount, never AUD, so a future FX correction doesn't
need to rewrite history — conversion happens once, at upload time.

## The three conversion actions

Created live on Google Ads account `5149790776` (type `UPLOAD_CLICKS`):

| Action | id | Marked |
|---|---|---|
| `signup` | `7723667014` | SECONDARY |
| `first_call` | `7723667017` | PRIMARY |
| `paid` (first top-up) | `7723667020` | PRIMARY |

`signup` is deliberately **secondary**, not primary: `marketing/landing/_measurement.md` argues a
signup measures curiosity, not commercial intent, so it should inform Google's targeting without
being a bidding goal. `first_call` and `paid` are the two events the campaign should actually bid
toward — an agent successfully calling a tool, and a team paying for more balance.

## API version

The uploader pins Google Ads API **v25** (`adsconv.API_VERSION`). v21 was sunset 2026-08-05; the pin
moved to v25 on 2026-08-17 across every place a version is hard-coded (`oauth_providers.GOOGLE_ADS`,
`.agents/skills/google-ads/SKILL.md`, this repo's catalog yaml) — see `architecture/auth-secrets.md`
for the full four-places-at-once list and the two-failure-modes note (a dead version returns a typed
`UNSUPPORTED_VERSION`, not the HTML 404 a never-existent version returns).

## Testing hazard: the shared test database

The suite's default SQLite file is `./treg-test.db`, and `reset_db()` (test-only) drops and recreates
every table. Two pytest runs against the same file concurrently corrupt each other — one run's
`reset_db()` mid-flight drops a table the other run is about to query — and the failure surfaces as a
misleading `no such table` error that looks like a flake, not a concurrency bug. This cost this
feature's development several hours and one conversation-round wrongly dismissing a real bug as a
flake before the cause was found. Isolate a run with:

```bash
TREG_TEST_DB_URL="sqlite+aiosqlite:///./some-other.db" uv run --frozen python -m pytest -q
```

## Not built

- **No router / auto-failover** — not applicable here (there is one destination, Google Ads), but
  stated for consistency with the rest of the catalog: treg does not model automatic choosing.
- **The reconciliation sweep for the `paid` atomicity gap** (above) — named as the cheap fix, not
  implemented.
- **The live-click verification** — needs a real ad click, cannot be automated, and runs after this
  documentation lands, before any real spend.
