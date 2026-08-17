"""The referral program: who invited whom, whether we owe for it, and paying it out.

This module DECIDES; `ledger.py` MOVES. The only money crossing is `ledger.grant(...)`, exactly
as `billing.py`'s only crossing is `ledger.topup(...)`. Nothing here writes `creditblock`,
`ledgerentry` or `org.balance_micro`, and nothing here goes through `audit.py` (which sheds rows
under load — right for analytics, fatal for money).

THE SHAPE, and why it is this shape
-----------------------------------
A friend gets $5, the referrer gets $10, both flat. The qualifying event is the friend's FIRST
PAID TOP-UP, never their signup: `promo_grant_micro` is granted per ORG and a user may create
unlimited orgs, so a signup-triggered bounty is a faucet pointed at itself.

Payment is held for `referral_hold_days` and then granted lazily. There is no scheduler anywhere
in treg by design (see ledger.py's reaper), so `sweep()` is called from paths someone is already
paying for: every top-up, and the Referrals page itself.

WHAT ARBITRATES A DOUBLE PAYOUT
-------------------------------
The `Referral` row, via two UNIQUE constraints — NOT `ledger.grant(once=True)`, whose check is a
SELECT with no backing index and which therefore cannot survive two concurrent redemptions. So
every grant here passes `once=False` and the database decides. Same reasoning as the conditional
UPDATE in `ledger.reserve` and the unique `stripe_payment_intent` in `ledger.topup`.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It never reverses a grant that has already landed. Referral credit burns before purchased credit
(`ledger._KIND_ORDER`), so by the time a dispute arrives the money is usually spent, and inventing
a negative-balance path would mean a second module that can move money. A dispute inside the hold
cancels the payout; a dispute after it is logged for a human. That boundary is the whole reason
the hold exists.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from . import ledger
from .config import get_settings
from .models import CreditBlock, Membership, Org, Referral, User

log = logging.getLogger("treg")

# The charset a code may contain. Codes appear in a URL and get retyped off a screenshot, so the
# generated half avoids look-alikes; the validator stays permissive enough to accept any code we
# have ever minted plus a hand-set vanity one.
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no i/l/o/0/1


def _now() -> datetime:
    """Naive UTC, matching models._now — our timestamp columns are TIMESTAMP WITHOUT TIME ZONE and
    asyncpg rejects a tz-aware value into one."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_code(raw: str) -> str:
    """A code as it arrived (query string, cookie, form) → the canonical form, or "" if it could
    never be one of ours. Called on every read, so a junk cookie is dropped rather than queried."""
    code = (raw or "").strip().lower()
    return code if _CODE_RE.match(code) else ""


def hold_cutoff() -> datetime:
    """Qualified before this instant → payable now."""
    return _now() - timedelta(days=int(get_settings().referral_hold_days))


# ---- the code ----------------------------------------------------------------------------------
async def ensure_code(db: AsyncSession, user: User) -> str:
    """This user's referral code, minting one on first ask. Commits when it mints.

    Lazy because most people never open the Referrals page, and a code nobody has seen is a row
    nobody needs. The retry loop exists because `User.referral_code` is UNIQUE: a collision is
    astronomically unlikely at 8 characters but it is the database's answer, not ours, so it is
    handled rather than assumed away.
    """
    if user.referral_code:
        return user.referral_code
    base = re.sub(r"[^a-z0-9]+", "", (user.email or "").split("@")[0].lower())[:12] or "treg"
    if len(base) < 2:
        base = f"{base}treg"
    for _ in range(5):
        candidate = f"{base}-{''.join(secrets.choice(_ALPHABET) for _ in range(5))}"
        user.referral_code = candidate
        db.add(user)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            await db.refresh(user)
            if user.referral_code:  # someone else minted one for this row first
                return user.referral_code
            continue
        return candidate
    raise RuntimeError("could not mint a unique referral code")


async def user_by_code(db: AsyncSession, code: str) -> User | None:
    code = normalize_code(code)
    if not code:
        return None
    return (await db.execute(select(User).where(User.referral_code == code))).scalars().first()


# ---- attribution (at signup) -------------------------------------------------------------------
async def attribute(db: AsyncSession, *, user: User, org: Org, code: str) -> Referral | None:
    """Record that `org` arrived through `code`. Owes nothing yet. Commits.

    Called right after the signup promo when a new team is created. Returns None — silently — for
    every reason a referral is not applicable: no code, unknown code, self-referral, a demo team, or
    an org that already carries one. None of these are errors the caller should react to; the caller
    is a signup and a referral must never be able to fail one.
    """
    code = normalize_code(code)
    if not code or org is None or org.id is None or org.demo or org.public_demo:
        return None
    referrer = await user_by_code(db, code)
    if referrer is None or referrer.id is None or user.id is None:
        return None
    if referrer.id == user.id:
        return None  # self-referral: the cheapest possible check, and the most common attempt
    row = Referral(code=code, referrer_user_id=referrer.id, referred_user_id=user.id,
                   referred_org_id=org.id, status="pending")
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # This org already has a referral. Whoever got there first keeps it.
        await db.rollback()
        return None
    return row


# ---- qualification (at the friend's first paid top-up) -----------------------------------------
async def qualify(
    db: AsyncSession, *, org_id: int, payment_intent: str, amount_micro: int,
    fingerprint: str | None = None,
) -> Referral | None:
    """The org just paid for the first time — decide whether that earns anybody anything. Commits.

    Returns the row in whatever state it ended in (`qualified`, `capped`, `rejected`) or None if
    this org was never referred. A refusal is RECORDED, not dropped: "why did I not get paid" is the
    first support question a referral program generates, and a deleted row cannot answer it.
    """
    row = (await db.execute(
        select(Referral).where(Referral.referred_org_id == org_id, Referral.status == "pending")
    )).scalars().first()
    if row is None:
        return None

    s = get_settings()

    async def _refuse(reason: str) -> Referral:
        row.status = "rejected"
        row.reject_reason = reason
        row.qualifying_payment_intent = payment_intent
        row.card_fingerprint = fingerprint
        db.add(row)
        await db.commit()
        log.info("referral %s refused: %s", row.id, reason)
        return row

    # Gate 1 — the top-up has to be big enough that buying your own bonus loses money.
    if int(amount_micro) < int(s.referral_min_topup_micro):
        return await _refuse("topup_below_minimum")

    # Gate 2 — this must be the org's FIRST purchase. `_credit` only calls us on a fresh payment,
    # but a second top-up is still a fresh payment, and the bounty is for arriving, not for paying
    # twice. Count blocks rather than trusting the caller's framing.
    purchased = (await db.execute(
        select(CreditBlock.id).where(CreditBlock.org_id == org_id, CreditBlock.kind == "purchased")
    )).scalars().all()
    if len(purchased) > 1:
        return await _refuse("not_first_topup")

    referrer = await db.get(User, row.referrer_user_id)
    if referrer is None or referrer.suspended:
        return await _refuse("referrer_unavailable")

    # Gate 3 — the referrer must have paid us at least once themselves. This is what a throwaway
    # account cannot cheaply satisfy, and it costs a genuine referrer nothing: they are, by
    # construction, already a customer.
    if not await _has_paid(db, referrer.id):
        return await _refuse("referrer_never_topped_up")

    # Gate 4 — the same card may fund exactly one referral, ever. An email address is free and a
    # card is not, so this is the signal that actually survives a determined farm.
    if fingerprint:
        clash = (await db.execute(
            select(Referral.id).where(
                Referral.card_fingerprint == fingerprint,
                Referral.status.in_(("qualified", "paid")),  # type: ignore[attr-defined]
                Referral.id != row.id,
            )
        )).scalars().first()
        if clash is not None:
            return await _refuse("card_already_used")

    # Gate 5 — the lifetime cap. Kept distinct from `rejected` because it is not an accusation:
    # this person referred someone real and simply ran out of self-serve allowance.
    paid_count = len((await db.execute(
        select(Referral.id).where(
            Referral.referrer_user_id == row.referrer_user_id,
            Referral.status.in_(("qualified", "paid")),  # type: ignore[attr-defined]
            Referral.id != row.id,
        )
    )).scalars().all())
    if paid_count >= int(s.referral_cap):
        row.status = "capped"
        row.reject_reason = "referrer_at_cap"
        row.qualifying_payment_intent = payment_intent
        row.card_fingerprint = fingerprint
        db.add(row)
        await db.commit()
        return row

    row.status = "qualified"
    row.qualified_at = _now()
    row.qualifying_payment_intent = payment_intent
    row.card_fingerprint = fingerprint
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # Another delivery of the same PaymentIntent qualified it first. Same answer as the
        # sequential path: it is qualified, and it will be paid exactly once.
        await db.rollback()
        return (await db.execute(select(Referral).where(Referral.id == row.id))).scalars().first()
    return row


async def _has_paid(db: AsyncSession, user_id: int | None) -> bool:
    """Has this person's money ever reached us — in ANY org they belong to?"""
    if user_id is None:
        return False
    org_ids = (await db.execute(
        select(Membership.org_id).where(Membership.user_id == user_id)
    )).scalars().all()
    if not org_ids:
        return False
    found = (await db.execute(
        select(CreditBlock.id).where(
            CreditBlock.org_id.in_(org_ids),  # type: ignore[attr-defined]
            CreditBlock.kind == "purchased",
        ).limit(1)
    )).scalars().first()
    return found is not None


# ---- clawback ----------------------------------------------------------------------------------
async def reject_for_payment(db: AsyncSession, payment_intent: str, reason: str) -> int:
    """The funding payment was disputed or refunded — cancel anything it earned that has not landed.
    Returns how many rows were cancelled. Commits.

    Only touches `qualified`. A `paid` row is left alone and merely logged: the credit burns first
    and is typically already spent, and clawing it back would mean a code path that can drive a
    balance negative. That is the boundary the hold window buys us, and it is deliberate.
    """
    rows = (await db.execute(
        select(Referral).where(Referral.qualifying_payment_intent == payment_intent)
    )).scalars().all()
    cancelled = 0
    for row in rows:
        if row.status == "qualified":
            row.status = "rejected"
            row.reject_reason = reason
            db.add(row)
            cancelled += 1
        elif row.status == "paid":
            log.warning(
                "referral %s was already PAID when payment %s was %s — %s micro-USD needs a human",
                row.id, payment_intent, reason,
                row.referrer_reward_micro + row.referred_reward_micro)
    if cancelled:
        await db.commit()
    return cancelled


# ---- payout ------------------------------------------------------------------------------------
async def credit_org_for(db: AsyncSession, user_id: int) -> Org | None:
    """Where this person's reward lands: the OLDEST org they own.

    Deterministic on purpose. A referrer may belong to many teams, and "whichever one they had
    selected when the friend happened to top up" would make the destination depend on a race. The
    oldest owned org is their first team — the one they think of as theirs — and the Referrals page
    names it so the answer is never a surprise.
    """
    memberships = (await db.execute(
        select(Membership).where(Membership.user_id == user_id, Membership.role == "owner")
        .order_by(Membership.id)
    )).scalars().all()
    for m in memberships:
        org = await db.get(Org, m.org_id)
        if org is not None and not org.demo and not org.public_demo and not org.suspended:
            return org
    return None


async def sweep(db: AsyncSession, *, limit: int = 100, referrer_user_id: int | None = None) -> int:
    """Pay out every qualified referral whose hold has elapsed. Returns how many were paid. Commits.

    Lazy and caller-paid, exactly like `ledger.reap_stale_holds`: there is no scheduler to hang this
    on, and a background timer would need leader election on a multi-instance deploy. It runs from
    `billing._credit` (so any top-up anywhere advances the queue) and from the Referrals page (so a
    user checking on their reward is the one who makes it land).

    Never raises. A payout that cannot complete is logged and left `qualified` for the next pass —
    the callers are a Stripe webhook and a page load, and neither may fail over a bonus.
    """
    q = (select(Referral)
         .where(Referral.status == "qualified", Referral.qualified_at < hold_cutoff())
         .order_by(Referral.qualified_at).limit(limit))
    if referrer_user_id is not None:
        q = q.where(Referral.referrer_user_id == referrer_user_id)
    due = (await db.execute(q)).scalars().all()

    paid = 0
    for row in due:
        try:
            if await _pay(db, row):
                paid += 1
        except Exception as exc:  # pragma: no cover — defensive; the next sweep retries
            await db.rollback()
            log.warning("referral %s payout failed, will retry: %s", row.id, exc)
    return paid


async def _pay(db: AsyncSession, row: Referral) -> bool:
    """Grant both sides and mark the row paid. Returns False if it could not be paid this pass.

    THE CLAIM COMES FIRST. The row is flipped to `paid` and committed BEFORE any credit moves, so
    that the unique row — not the grant — is what stops a double payout when two instances sweep at
    the same time. Losing the claim means somebody else is paying it; granting first and claiming
    second would mean the loser had already granted.

    The cost of that ordering is the opposite failure: a crash between the claim and the grant pays
    nobody and leaves the row saying otherwise. That is the right way round for money — it is
    visible in `/admin/referrals` as a paid row with a null block id, and it errs toward paying
    once rather than twice.
    """
    s = get_settings()
    referred_org = await db.get(Org, row.referred_org_id)
    referrer_org = await credit_org_for(db, row.referrer_user_id)
    if referred_org is None or referrer_org is None:
        log.warning("referral %s has nowhere to pay (referrer org missing) — skipping", row.id)
        return False

    claimed = (await db.execute(
        Referral.__table__.update()
        .where(Referral.__table__.c.id == row.id, Referral.__table__.c.status == "qualified")
        .values(status="paid", paid_at=_now())
    )).rowcount
    await db.commit()
    if claimed != 1:
        return False  # another sweep claimed it between our SELECT and here

    meta = {"referral_id": row.id, "code": row.code}
    referred_block = await ledger.grant(
        db, referred_org.id, amount_micro=int(s.referral_referred_micro), kind="referral",
        once=False, meta={**meta, "side": "referred"})
    referrer_block = await ledger.grant(
        db, referrer_org.id, amount_micro=int(s.referral_referrer_micro), kind="referral",
        once=False, meta={**meta, "side": "referrer"})

    fresh = await db.get(Referral, row.id)
    if fresh is not None:
        fresh.referred_block_id = referred_block.id if referred_block else None
        fresh.referrer_block_id = referrer_block.id if referrer_block else None
        fresh.referred_reward_micro = referred_block.amount_micro if referred_block else 0
        fresh.referrer_reward_micro = referrer_block.amount_micro if referrer_block else 0
        db.add(fresh)
        await db.commit()
    return True


# ---- the read model ----------------------------------------------------------------------------
async def summary(db: AsyncSession, user: User) -> dict:
    """Everything the Referrals page renders, for ONE user.

    Scoped to `referrer_user_id == user.id` in the query itself rather than filtered afterwards:
    this response carries the referred person's email address, so a scoping mistake here leaks
    another user's data rather than merely miscounting — the one failure in this feature that is
    worse than losing money.
    """
    s = get_settings()
    rows = (await db.execute(
        select(Referral).where(Referral.referrer_user_id == user.id)
        .order_by(Referral.created_at.desc())  # type: ignore[attr-defined]
    )).scalars().all()

    emails: dict[int, str] = {}
    for uid in {r.referred_user_id for r in rows}:
        friend = await db.get(User, uid)
        if friend is not None:
            emails[uid] = friend.email

    hold = timedelta(days=int(s.referral_hold_days))
    items, earned, pending = [], 0, 0
    for r in rows:
        if r.status == "paid":
            earned += r.referrer_reward_micro
        elif r.status == "qualified":
            pending += int(s.referral_referrer_micro)
        items.append({
            "email": emails.get(r.referred_user_id, ""),
            "status": r.status,
            "reason": r.reject_reason,
            "signed_up_at": r.created_at.isoformat() if r.created_at else None,
            "topped_up_at": r.qualified_at.isoformat() if r.qualified_at else None,
            "paid_at": r.paid_at.isoformat() if r.paid_at else None,
            "pays_at": ((r.qualified_at + hold).isoformat()
                        if r.status == "qualified" and r.qualified_at else None),
            "reward_micro": (r.referrer_reward_micro if r.status == "paid"
                             else int(s.referral_referrer_micro) if r.status == "qualified" else 0),
        })

    credit_org = await credit_org_for(db, user.id) if user.id else None
    paid_count = sum(1 for r in rows if r.status in ("qualified", "paid"))
    code = user.referral_code or ""
    return {
        "code": code,
        "link": f"{get_settings().public_url.rstrip('/')}/?ref={code}" if code else "",
        "credit_org": ({"org_id": credit_org.id, "name": credit_org.name} if credit_org else None),
        "eligible": await _has_paid(db, user.id),
        "terms": {
            "referrer_micro": int(s.referral_referrer_micro),
            "referred_micro": int(s.referral_referred_micro),
            "min_topup_micro": int(s.referral_min_topup_micro),
            "hold_days": int(s.referral_hold_days),
        },
        "cap": {"paid": paid_count, "limit": int(s.referral_cap)},
        "totals": {
            "signed_up": len(rows),
            "topped_up": sum(1 for r in rows if r.status in ("qualified", "paid", "capped")),
            "earned_micro": earned,
            "pending_micro": pending,
        },
        "referrals": items,
    }
