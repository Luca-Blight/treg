"""HTTP routes for prepaid balances and Stripe billing."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from .. import analytics, billing, ledger, referrals
from ..config import get_settings
from ..db import get_session
from ..domain.identity.access import Caller, _role_at_least, require_member
from ..models import Org
from .auth_helpers import _is_https
from .orgs import _require_admin_of


# app is the APIRouter alias so mechanically moved @app decorators stay byte-identical.
app = APIRouter()


@app.get("/orgs/{org_id}/balance")
async def org_balance(
    org_id: int, limit: int = 20, offset: int = 0,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """The org's prepaid balance. Amounts are integer micro-USD (`*_micro`) with a display-only USD
    twin — never compute against the USD field (see ledger.py on why money is integers here).

    **Two audiences, one route.** Any MEMBER sees the figure and the in-flight holds: they are the
    ones spending it, every agent is told to run `treg balance` after a call, and a 402 already hands
    them `balance_micro` anyway — refusing the same number here while shipping it in an error was
    incoherent. The FUNDING DETAIL is admin+: the credit blocks (what was bought, when, what is left
    of each) and the ledger, which together are the org's purchase history, not its wallet.
    """
    if caller.org_id != org_id:
        raise HTTPException(status_code=403, detail="not a member of this org")
    detailed = _role_at_least(caller.role, "admin")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    balance = await ledger.balance_of(db, org_id)
    # Auto-top-up's trigger point until phase 3 calls it right after `reserve`. Fire-and-forget by
    # contract (see billing.maybe_schedule_autotopup): it starts a background task at most, so no
    # Stripe latency lands in this response, and reading a balance can therefore never be slow.
    billing.maybe_schedule_autotopup(caller.org)
    blocks = await ledger.blocks_of(db, org_id)
    holds = await ledger.open_holds_of(db, org_id)
    entries = await ledger.entries_of(db, org_id, limit=limit, offset=offset)
    return {
        "org_id": org_id,
        "balance_micro": balance,
        "balance_usd": ledger.usd(balance),
        "promo_grant_micro": get_settings().promo_grant_micro,
        # admin+ only — see the docstring: the wallet is everyone's, the purchase history is not
        "blocks": [] if not detailed else [
            {"id": b.id, "kind": b.kind, "amount_micro": b.amount_micro,
             "remaining_micro": b.remaining_micro, "remaining_usd": ledger.usd(b.remaining_micro),
             "currency": b.currency, "expires_at": b.expires_at.isoformat() if b.expires_at else None,
             "created_at": b.created_at.isoformat() if b.created_at else None}
            for b in blocks
        ],
        "holds": [
            {"call_id": h.id, "endpoint_id": h.endpoint_id, "amount_micro": h.amount_micro,
             "created_at": h.created_at.isoformat() if h.created_at else None}
            for h in holds
        ],
        "entries": {
            "limit": limit, "offset": offset,
            "items": [] if not detailed else [
                {"id": e.id, "kind": e.kind, "amount_micro": e.amount_micro,
                 "amount_usd": ledger.usd(e.amount_micro), "block_id": e.block_id,
                 "call_id": e.call_id, "endpoint_id": e.endpoint_id, "meta": e.meta,
                 "created_at": e.created_at.isoformat() if e.created_at else None}
                for e in entries
            ],
        },
    }


balance_router = app


app = APIRouter()


class TopupIn(BaseModel):
    amount_usd: float | None = None


class AutoTopupIn(BaseModel):
    enabled: bool
    threshold_usd: float | None = None
    amount_usd: float | None = None
    monthly_cap_usd: float | None = None
    # False when the caller is about to open a top-up Checkout anyway (the dashboard's modal): that
    # page saves the card too, so a second card-capture session would be a wasted Stripe call.
    setup_url: bool = True
    # Explicit, per-request agreement to unattended charges — the MIT mandate. Required to ENABLE when
    # there is no timestamp on file; ignored when disabling (nobody consents to stopping).
    consent: bool = False


def _billing_org(caller: Caller) -> Org:
    """Billing acts on the caller's OWN org and needs admin+ — the same gate as /usage and /balance,
    because a card and a spend policy are the org's money, not a member's preference."""
    _require_admin_of(caller.org_id, caller)
    if caller.org is None:
        raise HTTPException(status_code=404, detail="org not found")
    return caller.org


def _return_base(request: Request) -> str:
    """Where Stripe sends the payer back — the deployment they were actually using, not whatever
    `public_url` says, so a local or preview server returns to itself."""
    host = request.headers.get("host", "")
    if not host:
        return ""
    return f"{'https' if _is_https(request) else request.url.scheme}://{host}"


@app.get("/billing")
async def billing_get(
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """The org's billing state: whether top-ups are available at all on this deployment, whether
    there's a Stripe customer and a saved card, the auto-top-up policy + why it's off if it is, and how
    much of this month's automatic cap has been used."""
    org = _billing_org(caller)
    state = await billing.billing_state(db, org)
    # Merged HERE rather than inside `billing_state`, so billing.py keeps its one job (Stripe) and
    # does not grow a second reason to know about referrals. This is the screen where a referred team
    # is already deciding how much to add, so it is the only place the minimum actually changes a
    # decision — see referrals.offer_for_org.
    state["referral_offer"] = await referrals.offer_for_org(db, org.id)
    return state


@app.post("/billing/topup")
async def billing_topup(
    request: Request, body: TopupIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Start a hosted Stripe Checkout for a one-off top-up and return its URL.

    Returns a URL, not a credit: the balance moves when Stripe's webhook says the payment succeeded
    (see billing.py). Nothing about this response — including a payer who "completes" the success
    redirect by hand — can create balance.
    """
    org = _billing_org(caller)
    amount = body.amount_usd if body.amount_usd is not None else await billing.next_default_usd(db, org.id)
    try:
        out = await billing.create_topup_checkout(
            db, org, amount, return_base=_return_base(request), email=caller.email)
    except billing.BillingNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except billing.TopupRejected as e:
        raise HTTPException(status_code=422, detail=str(e))
    # The one place the actual payer's identity exists — the webhook that later credits the
    # balance is org-scoped, so the started/completed funnel joins on the team group.
    analytics.capture(caller.email, "topup_started",
                      {"amount_usd": amount, "org": org.slug}, groups={"team": org.slug})
    return out


@app.post("/billing/autotopup")
async def billing_autotopup(
    request: Request, body: AutoTopupIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Set the org's auto-top-up policy (and record consent when enabling).

    Enabling without a card on file is not an error — the preferences and the consent are stored and a
    Stripe-hosted card-capture URL comes back in `setup_url`. Finishing that page fires
    `setup_intent.succeeded`, which saves the payment method and arms the policy. That ordering is
    deliberate: consent is recorded against the numbers the human saw, before any card exists.
    """
    org = _billing_org(caller)
    if body.enabled and not body.consent and not org.autotopup_consented_at:
        raise HTTPException(
            status_code=422,
            detail="enabling auto top-up requires consent: true (you are authorizing charges to a "
                   "saved card without being present)")
    try:
        await billing.set_autotopup(
            db, org, enabled=body.enabled, consent=body.consent,
            threshold_usd=body.threshold_usd, amount_usd=body.amount_usd,
            monthly_cap_usd=body.monthly_cap_usd)
    except billing.TopupRejected as e:
        raise HTTPException(status_code=422, detail=str(e))
    state = await billing.billing_state(db, org)
    if body.enabled and body.setup_url and not org.stripe_default_pm:
        try:
            state["setup_url"] = (await billing.create_setup_checkout(
                db, org, return_base=_return_base(request), email=caller.email))["url"]
        except billing.BillingNotConfigured as e:
            raise HTTPException(status_code=503, detail=str(e))
    return state


@app.get("/billing/history")
async def billing_history(
    limit: int = Query(24, ge=1, le=100),
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """This team's completed top-ups, newest first, each with its invoice PDF or card receipt.

    Read-only in both directions: it moves no money, and the amounts come from our own credit blocks
    rather than from Stripe, so the history can never contradict the balance. Stripe is asked only for
    the document links, and `stripe_ok: false` says they were unavailable — the payments listed are
    still correct.
    """
    org = _billing_org(caller)
    return await billing.list_payments(db, org, limit=limit)


@app.post("/billing/portal")
async def billing_portal(
    request: Request,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """A one-time link into Stripe's hosted billing portal — card, billing address, tax ID, and the
    full invoice archive. 422 until the team has a Stripe customer, which it gets on its first
    payment; `billing_state`'s `portal` flag is what the UI hides the button on."""
    org = _billing_org(caller)
    try:
        return await billing.create_portal_session(db, org, return_base=_return_base(request))
    except billing.BillingNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except billing.TopupRejected as e:
        raise HTTPException(status_code=422, detail=str(e))


billing_router = app


app = APIRouter()


@app.post("/billing/stripe/webhook", include_in_schema=False)
async def billing_stripe_webhook(request: Request, db: AsyncSession = Depends(get_session)) -> dict:
    """Stripe → treg: the ONLY door through which a payment becomes balance.

    A DIFFERENT endpoint from the landing demo's `/stripe/webhook`, with a different signing secret:
    they are different Stripe accounts' events with different consequences, and sharing a path would
    mean one secret could authorize the other's effects. 404 when unconfigured, so a deploy without the
    secret exposes no unauthenticated POST surface.
    """
    if not get_settings().stripe_webhook_secret:
        raise HTTPException(status_code=404)
    payload = await request.body()
    try:
        event = billing.verify_event(payload, request.headers.get("stripe-signature", ""))
    except ValueError:
        # Deliberately terse: a signature oracle should not explain itself.
        raise HTTPException(status_code=400, detail="bad signature")
    try:
        result = await billing.handle_webhook_event(db, event)
    except Exception as e:  # noqa: BLE001
        # 500 tells Stripe to retry, which is what we want for a transient failure — but log loudly:
        # an event that never succeeds is money someone paid and didn't get.
        logging.getLogger("treg.billing").exception("webhook %s failed: %s", event.get("type"), e)
        raise HTTPException(status_code=500, detail="webhook handling failed")
    return {"received": True, **result}


webhook_router = app
