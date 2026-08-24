"""The call state matrix specified by CASES.md."""

from __future__ import annotations

from httpx import AsyncClient

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
