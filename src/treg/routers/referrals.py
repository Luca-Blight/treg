"""HTTP routes for a person's referral program."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .. import referrals
from ..config import get_settings
from ..db import get_session
from ..domain.identity.access import require_identity
from ..models import User


# app is the APIRouter alias so mechanically moved @app decorators stay byte-identical.
app = APIRouter()


@app.get("/referrals")
async def my_referrals(
    user: User = Depends(require_identity), db: AsyncSession = Depends(get_session),
) -> dict:
    """This person's referral link and everyone who has used it.

    Also runs the payout sweep, scoped to this user. There is no scheduler in treg, so the two
    trigger points are any top-up (`billing._credit`) and this page — which means someone checking
    whether their reward has landed is the one who makes it land. That is the same lazy,
    caller-pays-for-their-own-cleanup bargain as `ledger.reap_stale_holds`.
    """
    # Mint the code here too, not only on POST. Asking for this page IS the lazy trigger the code
    # was always meant to hang off, and every caller needs a usable `link` — a response carrying an
    # empty one is a footgun for any client that doesn't know to POST first.
    try:
        await referrals.ensure_code(db, user)
    except Exception as exc:  # noqa: BLE001 — a code we couldn't mint is an empty link, not a 500
        logging.getLogger("treg").warning("referral code mint failed for user %s: %s", user.id, exc)
    try:
        await referrals.sweep(db, referrer_user_id=user.id)
    except Exception as exc:  # noqa: BLE001 — pragma: no cover
        logging.getLogger("treg").warning("referral sweep failed for user %s: %s", user.id, exc)
    return await referrals.summary(db, user)


@app.post("/referrals/code")
async def mint_referral_code(
    user: User = Depends(require_identity), db: AsyncSession = Depends(get_session),
) -> dict:
    """Mint this person's referral code, or return the one they already have.

    Idempotent, so the dashboard can call it every time the page opens without checking first. Codes
    are minted here rather than at signup because most people never open this page, and a code
    nobody has seen is a unique index entry earning nothing.
    """
    code = await referrals.ensure_code(db, user)
    return {"code": code, "link": f"{get_settings().public_url.rstrip('/')}/?ref={code}"}


router = app
