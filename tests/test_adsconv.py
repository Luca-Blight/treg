import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from conftest import make_upstream
from treg import adsconv, crypto
from treg.api import app
from treg.config import get_settings
from treg.db import _migrate_to_orgs, reset_db, session_maker
from treg.models import AdConversion, Org, Secret


def _h(token: str) -> dict:
    return {"X-Treg-Token": token}


class FakeAdsResponse:
    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body


class FakeAdsClient:
    def __init__(self, response: FakeAdsResponse):
        self.response = response
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@pytest.fixture
def ads_enabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "google_ads_customer_id", "5149790776", raising=False)
    monkeypatch.setattr(settings, "google_ads_developer_token", "dev-tok-test", raising=False)
    monkeypatch.setattr(settings, "google_ads_login_customer_id", "", raising=False)
    monkeypatch.setattr(settings, "ads_conv_org_slug", "platform-ads", raising=False)
    assert adsconv.enabled() is True
    return settings


@pytest.fixture
def ads_disabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "google_ads_customer_id", "", raising=False)
    monkeypatch.setattr(settings, "google_ads_developer_token", "", raising=False)
    monkeypatch.setattr(settings, "ads_conv_org_slug", "", raising=False)
    assert adsconv.enabled() is False
    return settings


@pytest.fixture
async def callenv(ads_enabled):
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


@pytest.mark.parametrize(
    "setting_name",
    ["google_ads_customer_id", "google_ads_developer_token", "ads_conv_org_slug"],
)
def test_tracking_is_disabled_when_any_required_setting_is_missing(
    monkeypatch, ads_enabled, setting_name
):
    monkeypatch.setattr(ads_enabled, setting_name, "", raising=False)
    assert adsconv.enabled() is False


def test_existing_ads_tables_receive_additive_state_columns():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE org (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO org (id) VALUES (1)"))
        conn.execute(text("CREATE TABLE adconversion (id INTEGER PRIMARY KEY)"))

        _migrate_to_orgs(conn)

        assert "ad_click_id_type" in {c["name"] for c in inspect(conn).get_columns("org")}
        conversion_columns = {c["name"] for c in inspect(conn).get_columns("adconversion")}
        assert {"next_attempt_at", "failed_at"} <= conversion_columns


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
        org = Org(name="t", slug="t-adcols", ad_gclid="ABC123",
                  ad_click_id_type="wbraid", ad_landing="p2")
        db.add(org)
        await db.commit()
        got = (await db.execute(select(Org).where(Org.slug == "t-adcols"))).scalar_one()
        assert got.ad_gclid == "ABC123"
        assert got.ad_click_id_type == "wbraid"
        assert got.ad_landing == "p2"
        assert got.first_call_at is None


async def test_queue_writes_one_row_and_is_idempotent(clients, ads_enabled):
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


async def test_queue_is_a_noop_without_a_gclid(clients, ads_enabled):
    # Organic signups are the majority; they must not fill the outbox with unattributable rows.
    async with session_maker() as db:
        org = Org(name="t", slug="t-noclick")
        db.add(org)
        await db.commit()
        await db.refresh(org)
        assert await adsconv.queue(db, org, adsconv.ACTION_SIGNUP) is False


async def test_queue_is_a_noop_when_disabled(clients, ads_disabled):
    async with session_maker() as db:
        org = Org(name="t", slug="t-disabled", ad_gclid="CLICK_DISABLED")
        db.add(org)
        await db.commit()
        await db.refresh(org)
        assert await adsconv.queue(db, org, adsconv.ACTION_SIGNUP) is False
        assert (await db.execute(select(AdConversion))).scalars().all() == []


async def test_signup_persists_the_gclid_cookie(clients, ads_enabled):
    r = await clients.post(
        "/users",
        json={"email": "click@example.com"},
        cookies={"treg_ad": "CLICK_XYZ|p3"},
    )
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.id == r.json()["org_id"]))).scalar_one()
        assert org.ad_gclid == "CLICK_XYZ"
        assert org.ad_click_id_type == "gclid"  # legacy cookie format remains readable
        assert org.ad_landing == "p3"
        assert org.ad_click_at is not None


async def test_signup_without_the_cookie_leaves_attribution_null(clients):
    r = await clients.post("/users", json={"email": "organic@example.com"})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.id == r.json()["org_id"]))).scalar_one()
        assert org.ad_gclid is None


async def test_signup_persists_the_braid_field(clients, ads_enabled):
    r = await clients.post(
        "/users",
        json={"email": "braid@example.com"},
        cookies={"treg_ad": "wbraid|BRAID_XYZ|p4"},
    )
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = await db.get(Org, r.json()["org_id"])
        assert org.ad_gclid == "BRAID_XYZ"
        assert org.ad_click_id_type == "wbraid"
        assert org.ad_landing == "p4"


async def test_disabled_signup_ignores_ad_cookie(clients, ads_disabled):
    r = await clients.post(
        "/users",
        json={"email": "disabled-click@example.com"},
        cookies={"treg_ad": "gclid|CLICK_DISABLED|p1"},
    )
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        org = await db.get(Org, r.json()["org_id"])
        assert org.ad_gclid is None
        assert (await db.execute(select(AdConversion))).scalars().all() == []


async def test_adtrack_script_is_empty_when_disabled(clients, ads_disabled):
    r = await clients.get("/adtrack.js")
    assert r.status_code == 200
    assert r.text == ""


async def test_signup_queues_a_conversion_when_attributed(clients, ads_enabled):
    r = await clients.post("/users", json={"email": "conv@example.com"},
                              cookies={"treg_ad": "CLICK_SIGNUP|p1"})
    assert r.status_code == 200, r.text
    async with session_maker() as db:
        rows = (await db.execute(select(AdConversion).where(
            AdConversion.org_id == r.json()["org_id"]))).scalars().all()
        assert [x.action for x in rows] == [adsconv.ACTION_SIGNUP]


async def test_org_creation_persists_the_gclid_cookie(clients, ads_enabled):
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


def test_build_payload_converts_currency_and_formats_time():
    click = datetime(2026, 8, 17, 3, 0, tzinfo=timezone.utc)
    org = Org(id=1, name="t", slug="t", ad_gclid="CLICK1", ad_click_at=click)
    row = AdConversion(id=1, org_id=1, action=adsconv.ACTION_PAID,
                       value_usd_micro=20_000_000,
                       created_at=click + timedelta(hours=6))
    payload = adsconv.build_payload([row], {1: org})
    conv = payload["conversions"][0]
    assert conv["gclid"] == "CLICK1"
    assert conv["conversionAction"].endswith("/conversionActions/7723667020")
    # US$20.00 at the fixed rate -> A$28.571428
    assert conv["conversionValue"] == pytest.approx(28.571428, rel=1e-6)
    assert conv["currencyCode"] == "AUD"
    assert payload["partialFailure"] is True


@pytest.mark.parametrize("click_field", ["gclid", "gbraid", "wbraid"])
def test_build_payload_preserves_click_id_field(click_field):
    org = Org(id=1, name="t", slug="t", ad_gclid="CLICK1", ad_click_id_type=click_field)
    row = AdConversion(id=1, org_id=1, action=adsconv.ACTION_SIGNUP)
    conversion = adsconv.build_payload([row], {1: org})["conversions"][0]
    assert conversion[click_field] == "CLICK1"
    assert set(conversion) & {"gclid", "gbraid", "wbraid"} == {click_field}


def test_build_payload_omits_value_for_non_revenue_actions():
    org = Org(id=1, name="t", slug="t", ad_gclid="C", ad_click_at=datetime.now(timezone.utc))
    row = AdConversion(id=1, org_id=1, action=adsconv.ACTION_SIGNUP, value_usd_micro=0)
    conv = adsconv.build_payload([row], {1: org})["conversions"][0]
    assert "conversionValue" not in conv


async def test_drain_marks_rows_uploaded_and_skips_young_ones(clients, monkeypatch, ads_enabled):
    """A row younger than the upload delay is left alone; an old one is sent and marked.

    _auth_headers reads a real `google-ads` oauth Secret off the platform org named by
    `ads_conv_org_slug` (see adsconv.py) — it is not mocked away, so this sets that org up for
    real: a slug settings can point at, and a MANUAL-mode oauth blob (no refresh_token) so
    oauth.ensure_fresh no-ops rather than trying to hit a real token endpoint through FakeClient.
    """
    client = FakeAdsClient(FakeAdsResponse({"results": [{"gclid": "C"}]}))

    async with session_maker() as db:
        org = Org(name="t", slug="t-drain", ad_gclid="C",
                  ad_click_at=datetime.now(timezone.utc) - timedelta(days=1))
        db.add(org)
        await db.commit()
        await db.refresh(org)
        monkeypatch.setattr(get_settings(), "ads_conv_org_slug", org.slug, raising=False)
        # No refresh_token/client_id/client_secret -> oauth.is_refreshable() is False -> ensure_fresh
        # no-ops instead of calling FakeClient.post against a real token endpoint.
        db.add(Secret(org_id=org.id, name="google-ads", kind="oauth", provider="google-ads",
                      value=crypto.encrypt(json.dumps({"access_token": "tok-test"}))))
        old = AdConversion(org_id=org.id, action=adsconv.ACTION_SIGNUP,
                           created_at=datetime.now(timezone.utc) - timedelta(hours=12))
        young = AdConversion(org_id=org.id, action=adsconv.ACTION_PAID,
                             created_at=datetime.now(timezone.utc))
        db.add(old); db.add(young)
        await db.commit()

        await adsconv.drain_once(db, client)

        await db.refresh(old); await db.refresh(young)
        assert old.uploaded_at is not None
        assert old.next_attempt_at is None
        assert old.failed_at is None
        assert young.uploaded_at is None
        assert len(client.calls) == 1


async def _seed_upload_rows(db, monkeypatch, *, actions, attempts=0):
    """Create the platform OAuth connection and old, upload-due outbox rows."""
    org = Org(name="t", slug="t-upload-batch", ad_gclid="CLICK_BATCH",
              ad_click_at=datetime.now(timezone.utc) - timedelta(days=1))
    db.add(org)
    await db.commit()
    await db.refresh(org)
    monkeypatch.setattr(get_settings(), "ads_conv_org_slug", org.slug, raising=False)
    db.add(Secret(org_id=org.id, name="google-ads", kind="oauth", provider="google-ads",
                  resource_ref="customers/5149790776",
                  value=crypto.encrypt(json.dumps({"access_token": "tok-test"}))))
    rows = [
        AdConversion(org_id=org.id, action=action, attempts=attempts,
                     created_at=datetime.now(timezone.utc) - timedelta(hours=12))
        for action in actions
    ]
    db.add_all(rows)
    await db.commit()
    return org, rows


def _partial_error(index: int, code: str, message: str = "conversion rejected") -> dict:
    return {
        "errorCode": {"conversionUploadError": code},
        "message": message,
        "location": {"fieldPathElements": [{"fieldName": "conversions", "index": index}]},
    }


async def test_drain_acknowledges_only_successful_rows_in_partial_response(
    clients, monkeypatch, ads_enabled
):
    body = {
        "results": [{"gclid": "CLICK_BATCH"}, {}],
        "partialFailureError": {
            "code": 3,
            "message": "partial failure",
            "details": [{"errors": [_partial_error(1, "UNPARSEABLE_GCLID")]}],
        },
    }
    client = FakeAdsClient(FakeAdsResponse(body))
    async with session_maker() as db:
        _org, rows = await _seed_upload_rows(
            db, monkeypatch, actions=(adsconv.ACTION_SIGNUP, adsconv.ACTION_PAID)
        )
        result = await adsconv.drain_once(db, client)
        for row in rows:
            await db.refresh(row)

        assert result == {"sent": 1, "retried": 1, "failed": 0, "status": 200}
        assert rows[0].uploaded_at is not None
        assert rows[0].error == ""
        assert rows[1].uploaded_at is None
        assert rows[1].failed_at is None
        assert rows[1].next_attempt_at is not None
        assert "UNPARSEABLE_GCLID" in rows[1].error


async def test_http_failure_keeps_retrying_past_row_attempt_ceiling(
    clients, monkeypatch, ads_enabled
):
    client = FakeAdsClient(FakeAdsResponse({"error": "unavailable"}, status_code=503))
    async with session_maker() as db:
        _org, (row,) = await _seed_upload_rows(
            db, monkeypatch, actions=(adsconv.ACTION_SIGNUP,), attempts=7
        )

        first = await adsconv.drain_once(db, client)
        await db.refresh(row)
        assert first["retried"] == 1
        assert row.attempts == 8
        assert row.failed_at is None
        assert row.next_attempt_at is not None

        # Make the scheduled retry due without sleeping. A second transport failure still must not
        # dead-letter the conversion merely because its historical attempt count is now above 8.
        row.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        db.add(row)
        await db.commit()
        second = await adsconv.drain_once(db, client)
        await db.refresh(row)
        assert second["retried"] == 1
        assert row.attempts == 9
        assert row.failed_at is None
        assert row.next_attempt_at is not None


async def test_permanent_row_failure_is_dead_lettered_after_attempt_ceiling(
    clients, monkeypatch, ads_enabled
):
    body = {
        "results": [{}],
        "partialFailureError": {
            "code": 3,
            "details": [{"errors": [_partial_error(0, "UNPARSEABLE_GCLID")]}],
        },
    }
    client = FakeAdsClient(FakeAdsResponse(body))
    async with session_maker() as db:
        _org, (row,) = await _seed_upload_rows(
            db, monkeypatch, actions=(adsconv.ACTION_SIGNUP,), attempts=7
        )
        result = await adsconv.drain_once(db, client)
        await db.refresh(row)

        assert result["failed"] == 1
        assert row.attempts == 8
        assert row.uploaded_at is None
        assert row.next_attempt_at is None
        assert row.failed_at is not None
        assert "UNPARSEABLE_GCLID" in row.error


async def test_unstructured_success_response_does_not_dead_letter_rows(
    clients, monkeypatch, ads_enabled
):
    client = FakeAdsClient(FakeAdsResponse({}))
    async with session_maker() as db:
        _org, (row,) = await _seed_upload_rows(
            db, monkeypatch, actions=(adsconv.ACTION_SIGNUP,), attempts=7
        )
        result = await adsconv.drain_once(db, client)
        await db.refresh(row)

        assert result["retried"] == 1
        assert row.attempts == 8
        assert row.uploaded_at is None
        assert row.failed_at is None
        assert row.next_attempt_at is not None
        assert row.error == "200: missing conversion result"


async def test_duplicate_row_response_is_acknowledged(clients, monkeypatch, ads_enabled):
    body = {
        "results": [{}],
        "partialFailureError": {
            "code": 3,
            "details": [{"errors": [_partial_error(0, "CLICK_CONVERSION_ALREADY_EXISTS")]}],
        },
    }
    client = FakeAdsClient(FakeAdsResponse(body))
    async with session_maker() as db:
        _org, (row,) = await _seed_upload_rows(
            db, monkeypatch, actions=(adsconv.ACTION_SIGNUP,)
        )
        result = await adsconv.drain_once(db, client)
        await db.refresh(row)

        assert result["sent"] == 1
        assert row.uploaded_at is not None
        assert row.error == ""


async def test_auth_uses_explicit_manager_account_not_target_resource_ref(
    clients, monkeypatch, ads_enabled
):
    async with session_maker() as db:
        await _seed_upload_rows(db, monkeypatch, actions=())
        monkeypatch.setattr(
            get_settings(), "google_ads_login_customer_id", "351-912-5194", raising=False
        )
        headers = await adsconv._auth_headers(
            db, FakeAdsClient(FakeAdsResponse({"results": []}))
        )
        assert headers["login-customer-id"] == "3519125194"

        monkeypatch.setattr(get_settings(), "google_ads_login_customer_id", "", raising=False)
        headers = await adsconv._auth_headers(
            db, FakeAdsClient(FakeAdsResponse({"results": []}))
        )
        assert "login-customer-id" not in headers
