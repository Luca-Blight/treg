import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from treg import adsconv
from treg.db import session_maker
from treg.models import AdConversion, Org


def test_usd_to_aud_uses_fixed_rate():
    # 1 AUD = 0.70 USD, so USD converts UP into AUD: US$20.00 -> A$28.57
    assert adsconv.usd_micro_to_aud_micro(20_000_000) == 28_571_428


def test_usd_to_aud_is_integer_only():
    # No float ever appears: 1 micro-USD must not become 1.4285... micro-AUD
    result = adsconv.usd_micro_to_aud_micro(1)
    assert isinstance(result, int)
    assert result == 1


def test_usd_to_aud_zero_and_negative():
    assert adsconv.usd_micro_to_aud_micro(0) == 0
    # Even-divisible negative: -7,000,000 * 10 / 7 = -10,000,000 exactly
    assert adsconv.usd_micro_to_aud_micro(-7_000_000) == -10_000_000
    # Non-exact negative: floor division toward -∞ rounds away from zero
    # -1,000,000 * 10 = -10,000,000; -10,000,000 // 7 = -1,428,572 (not -1,428,571)
    assert adsconv.usd_micro_to_aud_micro(-1_000_000) == -1_428_572


def test_action_ids_cover_every_action():
    assert set(adsconv.CONVERSION_ACTION_IDS) == {
        adsconv.ACTION_SIGNUP, adsconv.ACTION_FIRST_CALL, adsconv.ACTION_PAID
    }


async def test_ad_conversion_is_unique_per_org_and_action(clients):
    async with session_maker() as db:
        org = Org(name="t", slug="t-adsconv")
        db.add(org)
        await db.commit()
        await db.refresh(org)

        db.add(AdConversion(org_id=org.id, action="signup", dedupe_key="signup"))
        await db.commit()

        db.add(AdConversion(org_id=org.id, action="signup", dedupe_key="signup"))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_org_has_ad_attribution_columns(clients):
    async with session_maker() as db:
        org = Org(name="t", slug="t-adcols", ad_gclid="ABC123", ad_landing="p2")
        db.add(org)
        await db.commit()
        got = (await db.execute(select(Org).where(Org.slug == "t-adcols"))).scalar_one()
        assert got.ad_gclid == "ABC123"
        assert got.ad_landing == "p2"
        assert got.first_call_at is None


async def test_queue_writes_one_row_and_is_idempotent(clients):
    async with session_maker() as db:
        org = Org(name="t", slug="t-queue", ad_gclid="CLICK1")
        db.add(org)
        await db.commit()
        await db.refresh(org)

        assert await adsconv.queue(db, org, adsconv.ACTION_SIGNUP) is True
        await db.commit()
        # Second call for the same (org, action) must be a silent no-op, not an error
        assert await adsconv.queue(db, org, adsconv.ACTION_SIGNUP) is False
        await db.commit()

        rows = (await db.execute(
            select(AdConversion).where(AdConversion.org_id == org.id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].uploaded_at is None


async def test_queue_is_a_noop_without_a_gclid(clients):
    # Organic signups are the majority; they must not fill the outbox with unattributable rows.
    async with session_maker() as db:
        org = Org(name="t", slug="t-noclick")
        db.add(org)
        await db.commit()
        await db.refresh(org)
        assert await adsconv.queue(db, org, adsconv.ACTION_SIGNUP) is False
