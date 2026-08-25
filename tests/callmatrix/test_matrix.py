"""The call state matrix specified by CASES.md."""

from __future__ import annotations

import asyncio
from collections import Counter

from httpx import AsyncClient
from sqlmodel import select

from treg import audit, ledger
from treg.db import session_maker
from treg.ledger import with_margin
from treg.models import CallRecord

from test_marketplace_call import (
    EP,
    EP_CALL,
    EP_CALL_MICRO,
    EP_DFS,
    EP_DFS_MICRO,
    EP_MICRO,
    EP_PATH,
    PLATFORM_KEYS,
    _balance,
    _entries,
)

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
    await fake_provider.wait_for_hits(before.hit_count + 1)

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


# B group: every upstream failure closes the hold according to the endpoint's billing rule.


async def _latest_call_record() -> CallRecord:
    await audit.drain()
    async with session_maker() as db:
        return (await db.execute(
            select(CallRecord).order_by(CallRecord.id.desc()).limit(1),
        )).scalars().one()


async def test_b1_per_success_500_refunds(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    before = await snapshot(matrix_clients, fake_provider)
    body = b'{"error":"provider exploded"}'

    response = await matrix_clients.get(
        f"/call/{EP}?aweme_id=7",
        headers={"X-Fake-Status": "500", "X-Fake-Body": body.decode()},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=500,
            body=body,
            cost_micro=0,
            ledger_kinds=("release", "reserve"),
            ledger_reason="call_failed_500",
            audit={"refused_by": None, "cost_charged_micro": 0},
        ),
    )


async def test_b2_per_success_404_refunds(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    before = await snapshot(matrix_clients, fake_provider)
    body = b'{"error":"not found"}'

    response = await matrix_clients.get(
        f"/call/{EP}?aweme_id=missing",
        headers={"X-Fake-Status": "404", "X-Fake-Body": body.decode()},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=404,
            body=body,
            cost_micro=0,
            ledger_kinds=("release", "reserve"),
            audit={"refused_by": None, "cost_charged_micro": 0},
        ),
    )


async def test_b3_per_call_400_is_billable(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    before = await snapshot(matrix_clients, fake_provider)
    charged = with_margin(EP_CALL_MICRO)
    body = b'{"error":"bad caller input"}'

    response = await matrix_clients.get(
        f"/call/{EP_CALL}?group_id=1",
        headers={"X-Fake-Status": "400", "X-Fake-Body": body.decode()},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=400,
            body=body,
            cost_micro=charged,
            balance_delta=-charged,
            ledger_kinds=("settle", "reserve"),
            audit={"refused_by": None, "cost_charged_micro": charged},
        ),
    )


async def test_b4_per_call_429_refunds(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    before = await snapshot(matrix_clients, fake_provider)
    body = b'{"error":"rate limited"}'

    response = await matrix_clients.get(
        f"/call/{EP_CALL}?group_id=1",
        headers={"X-Fake-Status": "429", "X-Fake-Body": body.decode()},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=429,
            body=body,
            cost_micro=0,
            ledger_kinds=("release", "reserve"),
            audit={"refused_by": None, "cost_charged_micro": 0},
        ),
    )


async def test_b5_read_timeout_refunds_and_keeps_evidence(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.get(
        f"/call/{EP}?aweme_id=7", headers={"X-Fake-Net": "timeout"},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=502,
            cost_micro=0,
            treg_error=True,
            ledger_kinds=("release", "reserve"),
            ledger_reason="call_failed_502",
            audit={"refused_by": None, "cost_charged_micro": 0},
            upstream_hits=0,
        ),
    )
    record = await _latest_call_record()
    assert record.error_request and "aweme_id" in record.error_request
    assert record.error_response and "timeout" in record.error_response.lower()


async def test_b6_connect_error_refunds_and_keeps_evidence(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.get(
        f"/call/{EP}?aweme_id=7", headers={"X-Fake-Net": "connect"},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=502,
            cost_micro=0,
            treg_error=True,
            ledger_kinds=("release", "reserve"),
            ledger_reason="call_failed_502",
            audit={"refused_by": None, "cost_charged_micro": 0},
            upstream_hits=0,
        ),
    )
    record = await _latest_call_record()
    assert record.error_response and "connection" in record.error_response.lower()


async def test_b7_metered_stream_interruption_is_call_failed(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.get(
        f"/call/{EP}?aweme_id=7",
        headers={"X-Fake-Net": "stream", "X-Fake-Body": '{"partial":true}'},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=502,
            cost_micro=0,
            treg_error=True,
            ledger_kinds=("release", "reserve"),
            ledger_reason="call_failed_502",
            audit={"refused_by": None, "cost_charged_micro": 0},
        ),
    )


# C group: settlement chooses a trustworthy observed cost or the catalog estimate.


async def test_c1_reported_cost_below_estimate_refunds_difference(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    observed = 50
    charged = with_margin(observed)
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.post(
        f"/call/{EP_DFS}",
        json=[{"url": "https://example.com/"}],
        headers={"X-Fake-Cost": "0.00005"},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=200,
            cost_micro=charged,
            balance_delta=-charged,
            ledger_kinds=("settle", "reserve"),
            audit={
                "cost_estimated_micro": EP_DFS_MICRO,
                "cost_observed_micro": observed,
                "cost_charged_micro": charged,
            },
        ),
    )


async def test_c2_missing_reported_cost_falls_back_to_estimate(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    charged = with_margin(EP_DFS_MICRO)
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.post(
        f"/call/{EP_DFS}",
        json=[{"url": "https://example.com/"}],
        headers={"X-Fake-Body": '{"tasks":[]}'},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=200,
            cost_micro=charged,
            balance_delta=-charged,
            ledger_kinds=("settle", "reserve"),
            audit={"cost_observed_micro": None, "cost_charged_micro": charged},
        ),
    )


async def test_c3_malformed_reported_cost_falls_back_to_estimate(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    charged = with_margin(EP_DFS_MICRO)
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.post(
        f"/call/{EP_DFS}",
        json=[{"url": "https://example.com/"}],
        headers={"X-Fake-Cost": '"garbage"'},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=200,
            cost_micro=charged,
            balance_delta=-charged,
            ledger_kinds=("settle", "reserve"),
            audit={"cost_observed_micro": None, "cost_charged_micro": charged},
        ),
    )


async def test_c4_gzip_reported_cost_falls_back_to_estimate(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    charged = with_margin(EP_DFS_MICRO)
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.post(
        f"/call/{EP_DFS}",
        json=[{"url": "https://example.com/"}],
        headers={"X-Fake-Cost": "0.00005", "X-Fake-Gzip": "1"},
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=200,
            cost_micro=charged,
            balance_delta=-charged,
            ledger_kinds=("settle", "reserve"),
            audit={"cost_observed_micro": None, "cost_charged_micro": charged},
        ),
    )
    assert fake_provider.hits[-1].headers["accept-encoding"] == "identity"


async def test_c5_gzip_4xx_body_is_decoded_for_evidence(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    before = await snapshot(matrix_clients, fake_provider)
    body = b'{"error":"compressed bad request"}'

    response = await matrix_clients.post(
        f"/call/{EP_DFS}",
        json=[{"url": "https://example.com/"}],
        headers={
            "X-Fake-Status": "400",
            "X-Fake-Body": body.decode(),
            "X-Fake-Gzip": "1",
        },
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(
            status=400,
            body=body,
            cost_micro=0,
            ledger_kinds=("release", "reserve"),
            audit={"refused_by": None, "cost_charged_micro": 0},
        ),
    )
    record = await _latest_call_record()
    assert record.error_response and "compressed bad request" in record.error_response


# F group: the relay preserves caller bytes while stripping treg's own session boundary.


async def test_f1_duplicate_query_parameters_keep_order(
    matrix_clients: AsyncClient, fake_provider: FakeProvider,
) -> None:
    await _register_echo(matrix_clients)
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.get("/call/echo/search?tag=a&tag=b")

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(status=200, body=b"{}", audit={"refused_by": None}),
    )
    assert fake_provider.hits[-1].query == (("tag", "a"), ("tag", "b"))


async def test_f2_encoded_slash_survives_in_raw_path(
    matrix_clients: AsyncClient, fake_provider: FakeProvider,
) -> None:
    await _register_echo(matrix_clients)
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.get("/call/echo/a%2fb")

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(status=200, body=b"{}", audit={"refused_by": None}),
    )
    assert fake_provider.hits[-1].raw_path.lower() == b"/a%2fb"


async def test_f3_treg_cookie_is_scrubbed_both_directions(
    matrix_clients: AsyncClient, fake_provider: FakeProvider,
) -> None:
    await _register_echo(matrix_clients)
    before = await snapshot(matrix_clients, fake_provider)

    response = await matrix_clients.get(
        "/call/echo/cookies",
        headers={
            "Cookie": "treg_session=caller-secret; ordinary=keep-me",
            "X-Custom-Proof": "kept",
            "X-Fake-Set-Cookie": "treg_session=evil; Path=/; HttpOnly",
        },
    )

    await assert_outcome(
        matrix_clients, fake_provider, response, before,
        Expect(status=200, body=b"{}", audit={"refused_by": None}),
    )
    hit = fake_provider.hits[-1]
    assert hit.headers["x-custom-proof"] == "kept"
    assert "ordinary=keep-me" in hit.headers["cookie"]
    assert "treg_session" not in hit.headers["cookie"]
    assert all("treg_session" not in value.lower() for value in response.headers.get_list("set-cookie"))


# G group: the balance's conditional update chooses exactly three winners under concurrency.


async def test_g1_ten_concurrent_calls_compete_for_three_call_balance(
    matrix_clients: AsyncClient, fake_provider: FakeProvider, platform_on,
) -> None:
    charged = with_margin(EP_MICRO)
    org_id = (await matrix_clients.get("/orgs")).json()[0]["org_id"]
    current = await _balance(matrix_clients)
    target = 3 * charged
    assert current > target
    async with session_maker() as db:
        trim = await ledger.reserve(db, org_id, "matrix-g1-trim", current - target)
        await ledger.settle(db, trim, current - target)
    assert await _balance(matrix_clients) == target
    before = await snapshot(matrix_clients, fake_provider)

    responses = await asyncio.gather(*(
        matrix_clients.get(
            f"/call/{EP}?aweme_id={index}", headers={"X-Fake-Sleep": "0.05"},
        )
        for index in range(10)
    ))
    await audit.drain()

    statuses = Counter(response.status_code for response in responses)
    assert statuses == Counter({402: 7, 200: 3})
    assert all(response.headers.get("X-Treg-Error") != "1" for response in responses if response.status_code == 200)
    assert all(response.headers.get("X-Treg-Error") == "1" for response in responses if response.status_code == 402)
    assert all(response.headers.get("X-Treg-Cost-Micro") == str(charged)
               for response in responses if response.status_code == 200)
    assert await _balance(matrix_clients) == 0

    balance_view = await matrix_clients.get(f"/orgs/{org_id}/balance")
    assert balance_view.json()["holds"] == []
    fresh = [entry for entry in await _entries(matrix_clients) if entry["id"] not in before.entry_ids]
    assert Counter(entry["kind"] for entry in fresh) == Counter({"reserve": 3, "settle": 3})
    assert len(fake_provider.hits) - before.hit_count == 3

    rows = (await matrix_clients.get("/calls")).json()
    assert Counter(row["status_code"] for row in rows) == Counter({402: 7, 200: 3})
    assert all(row["cost_charged_micro"] == charged for row in rows if row["status_code"] == 200)
    assert all(row["cost_charged_micro"] == 0 for row in rows if row["status_code"] == 402)
    assert all(response.headers.get("X-Treg-Call-Id") for response in responses), (
        "every concurrent outcome must carry X-Treg-Call-Id")
