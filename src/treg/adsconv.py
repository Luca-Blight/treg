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


from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

from .config import get_settings
from .models import AdConversion, Org


def enabled() -> bool:
    """Empty customer id = OFF. Keeps the test suite and self-hosted instances inert."""
    s = get_settings()
    return bool(s.google_ads_customer_id and s.ads_conv_org_slug)


async def queue(db: AsyncSession, org: Org, action: str, *,
                value_usd_micro: int = 0, dedupe_key: str = "") -> bool:
    """Record that `org` owes Google a conversion. Returns True if a row was written.

    Call this INSIDE the caller's transaction: the event and its pending conversion must commit
    together, or a crash between them loses a conversion with no trace.

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
