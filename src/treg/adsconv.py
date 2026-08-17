"""Google Ads conversion tracking — the outbox and its uploader.

Unlike audit.py and analytics.py, which are deliberately droppable, a conversion that is
lost is a conversion Google never learns about, and the bidding is then trained on
undercounted data. So the write is DURABLE (a row, in the caller's transaction) and only
the UPLOAD is asynchronous. Nothing here may route through audit.py.
"""

from __future__ import annotations

# Fixed FX, set 2026-08-17: 1 AUD = 0.70 USD. Deliberately a constant rather than a live
# rate so reported conversion value stays stable — a change in ROAS should mean the
# business moved, not that the currency market did. Revisit if the rate drifts far.
AUD_PER_USD_NUM = 10
AUD_PER_USD_DEN = 7

ACTION_SIGNUP = "signup"
ACTION_FIRST_CALL = "first_call"
ACTION_PAID = "paid"

# Created live on account 5149790776 on 2026-08-17 (type UPLOAD_CLICKS).
CONVERSION_ACTION_IDS: dict[str, str] = {
    ACTION_SIGNUP: "7723667014",
    ACTION_FIRST_CALL: "7723667017",
    ACTION_PAID: "7723667020",
}


def usd_micro_to_aud_micro(usd_micro: int) -> int:
    """Convert integer micro-USD to integer micro-AUD at the fixed rate.

    Integer-only, per the money-code rule: a float here would round differently on
    different platforms and the value is uploaded as a monetary amount.

    Note: // floors toward negative infinity, so negative amounts round away from zero
    while positive amounts round toward zero. Real inputs are always positive
    (top-ups); the negative case is defensive only.
    """
    return usd_micro * AUD_PER_USD_NUM // AUD_PER_USD_DEN


import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from . import crypto, oauth
from .config import get_settings
from .models import AdConversion, Org, Secret


def enabled() -> bool:
    """Empty customer id = OFF. Keeps the test suite and self-hosted instances inert."""
    s = get_settings()
    return bool(s.google_ads_customer_id and s.ads_conv_org_slug)


async def queue(db: AsyncSession, org: Org, action: str, *,
                value_usd_micro: int = 0, dedupe_key: str = "") -> bool:
    """Record that `org` owes Google a conversion. Returns True if a row was written.

    Call this INSIDE the caller's transaction: the event and its pending conversion must commit
    together, or a crash between them loses a conversion with no trace. The `paid` caller
    (`billing._credit`) cannot honour this — `ledger.topup()` has already committed by the time
    `_credit` gets here, so that one path is a known, accepted two-commit gap rather than a bug (see
    `docs/context/architecture/ads-conversions.md`).

    A no-op when the team has no click to attribute to, which is most teams. Duplicate fires are
    absorbed by the unique constraint rather than checked for first — the check-then-insert race
    is real under concurrent webhook redelivery.
    """
    if not org.ad_gclid:
        return False
    try:
        # A SAVEPOINT, not a bare flush: this runs inside the CALLER's transaction (the signup
        # grant, the Stripe credit), and a plain `db.rollback()` on the duplicate would roll back
        # THEIR work too — a redelivered webhook would undo a credit. The nested block confines the
        # rollback to this insert.
        async with db.begin_nested():
            db.add(AdConversion(org_id=org.id, action=action, dedupe_key=dedupe_key,
                                value_usd_micro=value_usd_micro))
    except IntegrityError:
        return False
    return True


# ---- the uploader ---------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    """Naive UTC. Our datetime columns are TIMESTAMP WITHOUT TIME ZONE and asyncpg rejects a
    tz-aware value into one; see `_now` in models.py, which every other table already follows.
    `api.py` has its own copy of this for the same reason — it is private to that module."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# v21 was sunset 2026-08-05 (JSON 400 UNSUPPORTED_VERSION); every other path that names a Google
# Ads API version in this repo (oauth_providers.GOOGLE_ADS, .agents/skills/google-ads/SKILL.md,
# docs/context/architecture/auth-secrets.md) was repointed at v25 on 2026-08-17 — see commit
# 6541167. The original task brief for this file still said v22 (written before that fix landed);
# v25 is what is actually live, so that is what's pinned here. Bump all four places together next
# time Google sunsets a version — see auth-secrets.md's note on this.
API_VERSION = "v25"
_UPLOAD_DELAY_S = 6 * 3600   # Google will not accept a conversion until hours after the click
_MAX_ATTEMPTS = 8


def _conversion_time(dt: datetime) -> str:
    """Ads wants 'yyyy-mm-dd hh:mm:ss+hh:mm'. ISO with a 'Z' is rejected.

    `dt` comes out of the database as NAIVE UTC (see models._now), so it is stamped with UTC, not
    converted. `.astimezone()` on a naive value would read it as LOCAL time — on the Sydney deploy
    target that shifts every conversion by 10-11 hours, which Google would either reject as
    pre-dating the click or attribute to the wrong day.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


def build_payload(rows: list[AdConversion], orgs: dict[int, Org]) -> dict:
    """Turn outbox rows into an uploadClickConversions body.

    `partialFailure` so one bad row cannot reject the batch — results are read per row.
    Value is converted to the ACCOUNT's currency here, at upload time; the outbox stores USD so a
    rate change never rewrites history.
    """
    cid = get_settings().google_ads_customer_id
    conversions = []
    for row in rows:
        org = orgs.get(row.org_id)
        if org is None or not org.ad_gclid:
            continue
        conv = {
            "gclid": org.ad_gclid,
            "conversionAction": f"customers/{cid}/conversionActions/{CONVERSION_ACTION_IDS[row.action]}",
            "conversionDateTime": _conversion_time(row.created_at),
        }
        if row.value_usd_micro:
            # The one permitted float: the Ads API's conversionValue field is a wire double, so a
            # decimal amount is what the JSON boundary requires. The arithmetic that produced the
            # micro amount stayed integral (usd_micro_to_aud_micro).
            conv["conversionValue"] = usd_micro_to_aud_micro(row.value_usd_micro) / 1_000_000
            conv["currencyCode"] = "AUD"
        conversions.append(conv)
    return {"conversions": conversions, "partialFailure": True}


async def _auth_headers(db: AsyncSession, client) -> dict[str, str]:
    """Bearer + developer-token (+ login-customer-id if the connection is scoped to a client
    account under a manager), for a direct call to the Ads REST API.

    This does NOT go through proxy.relay/injectors.py — the uploader is not a caller-issued
    `/call/` request, it's treg spending its OWN platform connection, so there is no Tool/bindings
    row to resolve credentials from. Instead it reads the `google-ads` OAuth `Secret` stored on
    the platform org named by `ads_conv_org_slug`, the same shape a normal registry connect
    produces (see api.py's `/oauth/callback`, which stores `kind="oauth"` + a JSON blob).

    Raises if the platform org, its google-ads connection, or a usable token is missing — the
    caller (`drain_once`, called from `worker`'s try/except) logs that and retries next pass
    rather than crashing the loop.
    """
    settings = get_settings()
    org = (await db.execute(
        select(Org).where(Org.slug == settings.ads_conv_org_slug)
    )).scalar_one_or_none()
    if org is None:
        raise RuntimeError(f"ads_conv_org_slug {settings.ads_conv_org_slug!r} matches no org")
    secret = (await db.execute(
        select(Secret).where(Secret.org_id == org.id, Secret.provider == "google-ads",
                              Secret.kind == "oauth")
    )).scalars().first()
    if secret is None:
        raise RuntimeError(f"org {settings.ads_conv_org_slug!r} has no google-ads oauth connection")
    # Same call health.py:205 makes from its own background task — refreshes in place if stale,
    # no-ops for a still-valid or MANUAL-mode (non-refreshable) token.
    await oauth.ensure_fresh(secret, db, client)
    blob = json.loads(crypto.decrypt(secret.value))
    token = blob.get("access_token") or blob.get("token")
    if not token:
        raise RuntimeError("google-ads secret decrypted with no access token")
    headers = {
        "Authorization": f"Bearer {token}",
        "developer-token": settings.google_ads_developer_token,
        "Content-Type": "application/json",
    }
    # resource_ref is the account this connection was pointed at during connect/discovery — stored
    # as the full resource name ("customers/1234567890"); login-customer-id wants the bare digits.
    # Only needed when acting on a client account under a manager (SKILL.md #7) — omit rather than
    # send an empty/wrong header, which Ads reports as 401 UNAUTHENTICATED, not a targeting error.
    if secret.resource_ref:
        headers["login-customer-id"] = secret.resource_ref.rsplit("/", 1)[-1]
    return headers


async def drain_once(db: AsyncSession, client) -> dict:
    """Upload one batch of due rows. Returns a small dict for logging.

    Due = not yet uploaded, older than the click-availability delay, under the attempt ceiling.
    Every failure marks the row and leaves it for the next pass; nothing is dropped silently.
    """
    if not enabled():
        return {"sent": 0, "reason": "disabled"}
    # Naive UTC on BOTH sides: created_at is a naive column, and comparing it against a tz-aware
    # value is an asyncpg error on Postgres (and a silently wrong comparison elsewhere).
    cutoff = _utcnow_naive() - timedelta(seconds=_UPLOAD_DELAY_S)
    rows = (await db.execute(
        select(AdConversion)
        .where(AdConversion.uploaded_at.is_(None),
               AdConversion.created_at <= cutoff,
               AdConversion.attempts < _MAX_ATTEMPTS)
        .limit(100)
    )).scalars().all()
    if not rows:
        return {"sent": 0}
    orgs = {o.id: o for o in (await db.execute(
        select(Org).where(Org.id.in_({r.org_id for r in rows})))).scalars().all()}
    payload = build_payload(rows, orgs)
    if not payload["conversions"]:
        return {"sent": 0}
    cid = get_settings().google_ads_customer_id
    url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{cid}:uploadClickConversions"
    headers = await _auth_headers(db, client)
    resp = await client.post(url, json=payload, headers=headers)
    now = _utcnow_naive()
    for row in rows:
        row.attempts += 1
        if resp.status_code == 200:
            row.uploaded_at = now
            row.error = ""
        else:
            row.error = f"{resp.status_code}: {resp.text[:300]}"
        db.add(row)
    await db.commit()
    return {"sent": len(rows), "status": resp.status_code}


async def worker(session_factory, client) -> None:
    """Drain forever. Runs from `lifespan`; a failure here must never take the server down."""
    log = logging.getLogger("treg")
    while True:
        try:
            async with session_factory() as db:
                await drain_once(db, client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bad batch must not kill the loop
            log.warning("ads conversion drain failed: %s", exc)
        await asyncio.sleep(300)
