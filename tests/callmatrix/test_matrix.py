"""The call state matrix specified by CASES.md."""

from __future__ import annotations

import asyncio

from httpx import AsyncClient

from treg import audit, ledger
from treg.db import session_maker
from treg.ledger import with_margin

from test_marketplace_call import EP, EP_MICRO, EP_PATH, PLATFORM_KEYS

from .asserts import Expect, assert_outcome, snapshot
from .provider import FakeProvider


async def _register_echo(clients: AsyncClient) -> None:
    secret = await clients.post("/secrets", json={"name": "echo-key", "value": "OWN-ECHO-KEY"})
    assert secret.status_code == 200, secret.text
    tool = await clients.post("/tools", json={
        "name": "echo",
        "base_url": "https://fake-provider.example",
        "secret_id": secret.json()["id"],
    })
    assert tool.status_code == 200, tool.text


async def test_a1_own_tool_get(
    matrix_clients: AsyncClient, fake_provider: FakeProvider,
) -> None:
    await _register_echo(matrix_clients)
    before = await snapshot(matrix_clients, fake_provider)
    expected_body = b'{"echo":"get-exact"}'

    response = await matrix_clients.get(
        "/call/echo/ping", headers={"X-Fake-Body": expected_body.decode()},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=200,
            body=expected_body,
            audit={"tool_name": "echo", "credential_tier": None, "refused_by": None},
        ),
    )
    hit = fake_provider.hits[-1]
    assert hit.method == "GET" and hit.path == "/ping"
    assert hit.headers["authorization"] == "Bearer OWN-ECHO-KEY"


async def test_a2_own_tool_post(
    matrix_clients: AsyncClient, fake_provider: FakeProvider,
) -> None:
    await _register_echo(matrix_clients)
    before = await snapshot(matrix_clients, fake_provider)
    request_body = b'{"raw":[1,2],"keep":"bytes"}'
    expected_body = b'{"echo":"post-exact"}'

    response = await matrix_clients.post(
        "/call/echo/items?tag=a&tag=b",
        content=request_body,
        headers={"Content-Type": "application/json", "X-Fake-Body": expected_body.decode()},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=200,
            body=expected_body,
            audit={"tool_name": "echo", "credential_tier": None, "refused_by": None},
        ),
    )
    hit = fake_provider.hits[-1]
    assert hit.method == "POST" and hit.body == request_body
    assert hit.query == (("tag", "a"), ("tag", "b"))


async def test_a3_platform_catalog_call(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    before = await snapshot(matrix_clients, fake_provider)
    expected_body = b'{"result":"catalog-ok"}'
    charged = with_margin(EP_MICRO)

    response = await matrix_clients.get(
        f"/call/{EP}?aweme_id=7", headers={"X-Fake-Body": expected_body.decode()},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=200,
            body=expected_body,
            cost_micro=charged,
            balance_delta=-charged,
            ledger_kinds=("settle", "reserve"),
            audit={
                "endpoint_id": EP,
                "provider": "tikhub",
                "credential_tier": "platform",
                "cost_estimated_micro": EP_MICRO,
                "cost_charged_micro": charged,
                "refused_by": None,
            },
        ),
    )
    hit = fake_provider.hits[-1]
    assert hit.path == EP_PATH
    assert hit.headers["authorization"] == f"Bearer {PLATFORM_KEYS['TIKHUB']}"


async def test_a4_own_credential_shadows_platform(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    secret = await matrix_clients.post(
        "/secrets", json={"name": "tikhub", "value": "OWN-TIKHUB-KEY"},
    )
    assert secret.status_code == 200, secret.text
    before = await snapshot(matrix_clients, fake_provider)
    expected_body = b'{"result":"own-key-ok"}'

    response = await matrix_clients.get(
        f"/call/{EP}?aweme_id=7", headers={"X-Fake-Body": expected_body.decode()},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=200,
            body=expected_body,
            audit={
                "endpoint_id": EP,
                "provider": "tikhub",
                # CASES.md calls this semantic tier "own". The persisted schema names the rung
                # "credential", distinguishing it from a registered-tool credential.
                "credential_tier": "credential",
                # Catalog telemetry keeps the published estimate even when the org's key wins.
                # It remains uncharged, as the response, balance, and money journal prove.
                "cost_estimated_micro": EP_MICRO,
                "cost_charged_micro": None,
                "refused_by": None,
            },
        ),
    )
    assert fake_provider.hits[-1].headers["authorization"] == "Bearer OWN-TIKHUB-KEY"


# E group: refusals happen before a byte reaches the provider.


async def _drain_balance_below(clients: AsyncClient, threshold: int) -> None:
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    balance = (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]
    low, high = 0, balance
    while low < high:
        middle = (low + high + 1) // 2
        if with_margin(middle) <= balance:
            low = middle
        else:
            high = middle - 1
    async with session_maker() as db:
        call_id = await ledger.reserve(db, org_id, "matrix-drain", low)
        await ledger.settle(db, call_id, low)
    remaining = (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]
    assert remaining < threshold


async def test_e1_insufficient_balance(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    charged = with_margin(EP_MICRO)
    await _drain_balance_below(matrix_clients, charged)
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.get(f"/call/{EP}?aweme_id=7")

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=402,
            treg_error=True,
            audit={
                "endpoint_id": EP,
                "provider": "tikhub",
                "credential_tier": "platform",
                "cost_estimated_micro": EP_MICRO,
                "cost_charged_micro": 0,
                "refused_by": "balance",
            },
            upstream_hits=0,
        ),
    )
    detail = response.json()["detail"]
    assert detail["error"] == "insufficient_balance"
    assert detail["balance_micro"] < detail["estimated_cost_micro"]
    assert detail["estimated_cost_micro"] == charged
    assert detail["topup_url"] == "/app#billing"


async def test_e2_daily_member_cap(
    matrix_clients: AsyncClient, fake_provider: FakeProvider,
) -> None:
    await _register_echo(matrix_clients)
    org = (await matrix_clients.get("/orgs")).json()[0]
    members = (await matrix_clients.get(f"/orgs/{org['org_id']}/members")).json()
    owner = next(member for member in members if member["role"] == "owner")
    capped = await matrix_clients.patch(
        f"/orgs/{org['org_id']}/members/{owner['user_id']}/cap",
        json={"daily_call_cap": 1},
    )
    assert capped.status_code == 200, capped.text
    first = await matrix_clients.get("/call/echo/first")
    assert first.status_code == 200, first.text
    await audit.drain()
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.get("/call/echo/second")

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=429,
            treg_error=True,
            # Current implementation detail: this early gate has no marketplace telemetry.
            audit={"refused_by": "cap", "cost_charged_micro": None},
            upstream_hits=0,
        ),
    )


async def test_e3_deny_rule_blocks_resolved_upstream(
    matrix_clients: AsyncClient, fake_provider: FakeProvider,
) -> None:
    await _register_echo(matrix_clients)
    org_id = (await matrix_clients.get("/orgs")).json()[0]["org_id"]
    denied = await matrix_clients.post(
        f"/orgs/{org_id}/deny",
        json={"host": "fake-provider.example", "method": "GET", "note": "matrix E3"},
    )
    assert denied.status_code == 200, denied.text
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.get("/call/echo/private")

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=403,
            treg_error=True,
            # Current implementation detail: this early gate has no marketplace telemetry.
            audit={"refused_by": "policy", "cost_charged_micro": None},
            upstream_hits=0,
        ),
    )


async def test_e4_member_tool_acl(
    matrix_clients: AsyncClient, fake_provider: FakeProvider,
) -> None:
    await _register_echo(matrix_clients)
    org_id = (await matrix_clients.get("/orgs")).json()[0]["org_id"]
    owner_token = matrix_clients.headers["X-Treg-Token"]
    agent = await matrix_clients.post(
        f"/orgs/{org_id}/agents", json={"name": "matrix-e4", "tool_access": []},
    )
    assert agent.status_code in (200, 201), agent.text
    before = await snapshot(matrix_clients, fake_provider)
    matrix_clients.headers["X-Treg-Token"] = agent.json()["token"]
    try:
        response = await matrix_clients.get("/call/echo/blocked")
    finally:
        matrix_clients.headers["X-Treg-Token"] = owner_token

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=403,
            treg_error=True,
            # Current implementation detail: this early gate has no marketplace telemetry.
            audit={"refused_by": "policy", "cost_charged_micro": None},
            upstream_hits=0,
        ),
    )


async def test_e5_public_demo_catalog_call_is_resolution_refusal(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    org_id = (await matrix_clients.get("/orgs")).json()[0]["org_id"]
    published = await matrix_clients.post(f"/orgs/{org_id}/public-token")
    assert published.status_code == 200, published.text
    owner_token = matrix_clients.headers["X-Treg-Token"]
    before = await snapshot(matrix_clients, fake_provider)
    matrix_clients.headers["X-Treg-Token"] = published.json()["token"]
    try:
        response = await matrix_clients.get(f"/call/{EP}?aweme_id=7")
    finally:
        matrix_clients.headers["X-Treg-Token"] = owner_token

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=404,
            treg_error=True,
            audit={"refused_by": "resolution", "cost_charged_micro": None},
            upstream_hits=0,
        ),
    )


# D group: idempotency makes retries safe and concurrent duplicates lose the claim.


async def test_d1_same_key_replays_original_response(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    headers = {"Idempotency-Key": "matrix-d1", "X-Fake-Body": '{"answer":"once"}'}
    first = await matrix_clients.get(f"/call/{EP}?aweme_id=7", headers=headers)
    assert first.status_code == 200, first.text
    before = await snapshot(matrix_clients, fake_provider)

    replay = await matrix_clients.get(f"/call/{EP}?aweme_id=7", headers=headers)

    await assert_outcome(
        matrix_clients, fake_provider, replay, before,
        Expect(
            status=200,
            body=first.content,
            cost_micro=with_margin(EP_MICRO),
            audit={"credential_tier": "platform", "cost_charged_micro": with_margin(EP_MICRO)},
            upstream_hits=0,
        ),
    )
    assert replay.headers["X-Treg-Call-Id"] == first.headers["X-Treg-Call-Id"]
    assert replay.headers["X-Treg-Idempotent-Replay"] == "true"


async def test_d2_concurrent_same_key_loser_gets_409(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    headers = {
        "Idempotency-Key": "matrix-d2",
        "X-Fake-Sleep": "0.2",
        "X-Fake-Body": '{"answer":"winner"}',
    }
    before = await snapshot(matrix_clients, fake_provider)
    first_task = asyncio.create_task(
        matrix_clients.get(f"/call/{EP}?aweme_id=7", headers=headers),
    )
    for _ in range(1_000):
        if len(fake_provider.hits) > before.hit_count:
            break
        await asyncio.sleep(0)
    assert len(fake_provider.hits) == before.hit_count + 1, "first call never reached fake provider"

    loser = await matrix_clients.get(f"/call/{EP}?aweme_id=7", headers=headers)
    winner = await first_task

    assert loser.status_code == 409, loser.text
    assert loser.headers["X-Treg-Error"] == "1"
    await assert_outcome(
        matrix_clients, fake_provider, winner, before,
        Expect(
            status=200,
            body=b'{"answer":"winner"}',
            cost_micro=with_margin(EP_MICRO),
            balance_delta=-with_margin(EP_MICRO),
            ledger_kinds=("settle", "reserve"),
            audit={"credential_tier": "platform", "cost_charged_micro": with_margin(EP_MICRO)},
        ),
    )
    rows = (await matrix_clients.get("/calls")).json()
    loser_row = next(row for row in rows if row["status_code"] == 409)
    assert loser_row["refused_by"] == "request"
    loser_call_id = loser.headers.get("X-Treg-Call-Id")
    if loser_call_id is not None:
        assert loser_row["call_ref"] == loser_call_id
    assert loser_call_id, "every /call response must carry X-Treg-Call-Id"


async def test_d3_same_key_with_different_request_is_refused(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    headers = {"Idempotency-Key": "matrix-d3"}
    first = await matrix_clients.get(f"/call/{EP}?aweme_id=7", headers=headers)
    assert first.status_code == 200, first.text
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.get(f"/call/{EP}?aweme_id=8", headers=headers)

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=422,
            treg_error=True,
            audit={"refused_by": "request"},
            upstream_hits=0,
        ),
    )
    assert "different request" in response.json()["detail"]


async def test_d4_failure_releases_key_for_retry(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    key = {"Idempotency-Key": "matrix-d4"}
    before_failure = await snapshot(matrix_clients, fake_provider)
    failed = await matrix_clients.get(
        f"/call/{EP}?aweme_id=7",
        headers={**key, "X-Fake-Status": "500", "X-Fake-Body": '{"error":"temporary"}'},
    )
    await assert_outcome(
        matrix_clients, fake_provider, failed, before_failure,
        Expect(
            status=500,
            body=b'{"error":"temporary"}',
            cost_micro=0,
            ledger_kinds=("release", "reserve"),
            ledger_reason="call_failed_500",
            audit={"refused_by": None, "cost_charged_micro": 0},
        ),
    )
    before_retry = await snapshot(matrix_clients, fake_provider)

    retried = await matrix_clients.get(
        f"/call/{EP}?aweme_id=7",
        headers={**key, "X-Fake-Body": '{"answer":"recovered"}'},
    )

    await assert_outcome(
        matrix_clients, fake_provider, retried, before_retry,
        Expect(
            status=200,
            body=b'{"answer":"recovered"}',
            cost_micro=with_margin(EP_MICRO),
            balance_delta=-with_margin(EP_MICRO),
            ledger_kinds=("settle", "reserve"),
            audit={"refused_by": None, "cost_charged_micro": with_margin(EP_MICRO)},
        ),
    )
