from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from conftest import make_upstream
from treg import adsconv
from treg.api import app
from treg.db import reset_db, session_maker
from treg.models import AdConversion, Org


def _h(token: str) -> dict:
    return {"X-Treg-Token": token}


@pytest.fixture
async def callenv():
    """An ad-attributed org with one callable HTTP tool pointed at the fake upstream."""
    await reset_db()
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()),
                                 base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        r = await c.post("/users", json={"email": "caller@example.com"})
        assert r.status_code == 200, r.text
        token, org_id = r.json()["token"], r.json()["org_id"]
        sid = (await c.post("/secrets", headers=_h(token),
                            json={"name": "a-key", "value": "v"})).json()["id"]
        await c.post("/tools", headers=_h(token),
                     json={"name": "alpha", "base_url": "http://upstream", "secret_id": sid})
        async with session_maker() as db:            # attribute the org to an ad click
            org = await db.get(Org, org_id)
            org.ad_gclid = "CLICK_CALL"
            db.add(org)
            await db.commit()
        yield SimpleNamespace(c=c, token=token, org_id=org_id)
    await app.state.http.aclose()


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


async def test_signup_persists_the_gclid_cookie(clients):
    r = await clients.post(
        "/users",
        json={"email": "click@example.com"},
        cookies={"treg_ad": "CLICK_XYZ|p3"},
    )
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.id == r.json()["org_id"]))).scalar_one()
        assert org.ad_gclid == "CLICK_XYZ"
        assert org.ad_landing == "p3"
        assert org.ad_click_at is not None


async def test_signup_without_the_cookie_leaves_attribution_null(clients):
    r = await clients.post("/users", json={"email": "organic@example.com"})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.id == r.json()["org_id"]))).scalar_one()
        assert org.ad_gclid is None


async def test_signup_queues_a_conversion_when_attributed(clients):
    r = await clients.post("/users", json={"email": "conv@example.com"},
                              cookies={"treg_ad": "CLICK_SIGNUP|p1"})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        rows = (await db.execute(select(AdConversion).where(
            AdConversion.org_id == r.json()["org_id"]))).scalars().all()
        assert [x.action for x in rows] == [adsconv.ACTION_SIGNUP]


async def test_org_creation_persists_the_gclid_cookie(clients):
    # The OTHER signup door: a signed-in user creating their first team via /orgs (the browser
    # sign-in path). `clients` is already authenticated (X-Treg-Token from /users registration
    # in the fixture) — this only adds the ad-click cookie on top, same shape as the /users test.
    r = await clients.post(
        "/orgs",
        json={"name": "ad team"},
        cookies={"treg_ad": "CLICK_XYZ|p3"},
    )
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.id == r.json()["org_id"]))).scalar_one()
        assert org.ad_gclid == "CLICK_XYZ"
        assert org.ad_landing == "p3"
        assert org.ad_click_at is not None


async def test_org_creation_without_the_cookie_leaves_attribution_null(clients):
    r = await clients.post("/orgs", json={"name": "organic team"})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.id == r.json()["org_id"]))).scalar_one()
        assert org.ad_gclid is None


async def test_first_successful_call_fires_once(callenv):
    """Two successful calls: one timestamp, one conversion. The second must be a no-op."""
    r1 = await callenv.c.get("/call/alpha", headers=_h(callenv.token))
    assert 200 <= r1.status_code < 400, r1.text
    r2 = await callenv.c.get("/call/alpha", headers=_h(callenv.token))
    assert 200 <= r2.status_code < 400, r2.text

    async with session_maker() as db:
        org = await db.get(Org, callenv.org_id)
        assert org.first_call_at is not None
        rows = (await db.execute(select(AdConversion).where(
            AdConversion.org_id == callenv.org_id,
            AdConversion.action == adsconv.ACTION_FIRST_CALL))).scalars().all()
        assert len(rows) == 1


async def test_unattributed_org_records_timestamp_but_no_conversion(callenv):
    """first_call_at is a product metric and must be set for every team; only ad-clicked ones queue."""
    async with session_maker() as db:
        org = await db.get(Org, callenv.org_id)
        org.ad_gclid = None
        db.add(org)
        await db.commit()

    assert (await callenv.c.get("/call/alpha", headers=_h(callenv.token))).status_code < 400
    async with session_maker() as db:
        org = await db.get(Org, callenv.org_id)
        assert org.first_call_at is not None
        rows = (await db.execute(select(AdConversion).where(
            AdConversion.org_id == callenv.org_id))).scalars().all()
        assert rows == []
