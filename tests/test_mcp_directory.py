"""The Claude directory MCP is catalog-only and OAuth-isolated from the legacy MCP."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from treg import mcp, mcp_oauth

pytestmark = pytest.mark.anyio

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@asynccontextmanager
async def directory_session():
    fresh = mcp.build_mcp_app(server=mcp.directory_mcp, resource_version="v2")
    host = FastAPI()
    host.mount("/mcp/v2", fresh)
    async with mcp.mcp_lifespan(fresh):
        async with AsyncClient(transport=ASGITransport(app=host),
                               base_url="http://localhost") as client:
            yield client


async def _rpc(client: AsyncClient, method: str, params=None, token: str | None = None):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return await client.post("http://localhost/mcp/v2/", json=body, headers=headers)


async def _modern_rpc(client: AsyncClient, method: str, params=None,
                      token: str = "opaque-test-token"):
    body_params = dict(params or {})
    body_params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {"name": "directory-test", "version": "1"},
    }
    return await client.post("http://localhost/mcp/v2/", json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": body_params,
    }, headers={
        **MCP_HEADERS,
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": "2026-07-28",
        "MCP-Method": method,
    })


async def test_v2_declares_exact_directory_contract():
    tools = {tool.name: tool for tool in await mcp.directory_mcp.list_tools()}
    assert list(tools) == [
        "catalog_search", "catalog_get", "catalog_call_read", "catalog_call_write", "balance",
        "catalog_request",
    ]
    assert {name: tool.title for name, tool in tools.items()} == {
        "catalog_search": "Search Treg Catalog",
        "catalog_get": "Get Catalog Endpoint",
        "catalog_call_read": "Call a Read Endpoint",
        "catalog_call_write": "Call a Write Endpoint",
        "balance": "Check Treg Balance",
        "catalog_request": "Request a Catalog Capability",
    }
    assert "my_tools" not in tools and "call" not in tools
    assert "method" not in tools["catalog_call_read"].input_schema["properties"]
    assert "method" not in tools["catalog_call_write"].input_schema["properties"]
    blob = " ".join(tool.description.lower() for tool in tools.values())
    for disallowed in ("use treg first", "official", "anthropic verified", "best provider"):
        assert disallowed not in blob


async def test_v2_does_not_advertise_or_serve_change_subscriptions():
    async with directory_session() as client:
        discovered = await _modern_rpc(client, "server/discover")
        listened = await _modern_rpc(client, "subscriptions/listen", {
            "notifications": {"toolsListChanged": True},
        })

    assert discovered.status_code == 200, discovered.text
    capabilities = discovered.json()["result"]["capabilities"]
    assert capabilities["tools"]["listChanged"] is False
    assert capabilities["prompts"]["listChanged"] is False
    assert capabilities["resources"] == {"listChanged": False, "subscribe": False}
    assert listened.status_code == 404, listened.text
    assert listened.json()["error"] == {
        "code": -32601, "message": "Method not found", "data": "subscriptions/listen",
    }


async def test_v2_annotations_separate_safe_and_unsafe_calls():
    tools = {tool.name: tool for tool in await mcp.directory_mcp.list_tools()}
    read = tools["catalog_call_read"].annotations
    write = tools["catalog_call_write"].annotations
    request = tools["catalog_request"].annotations
    assert read.read_only_hint is True and read.destructive_hint is False and read.open_world_hint is True
    assert write.read_only_hint is False and write.destructive_hint is True and write.open_world_hint is True
    assert request.read_only_hint is False and request.destructive_hint is False


async def test_v2_call_tools_enforce_catalog_method_class_before_calling_api():
    ctx = type("Ctx", (), {"headers": {"authorization": "Bearer test"}})()
    get_id = "tikhub.tiktok.video.comments"
    post_id = "dataforseo.web.page.audit"

    wrong_read = await mcp.directory_catalog_call_read(post_id, ctx=ctx)
    wrong_write = await mcp.directory_catalog_call_write(get_id, ctx=ctx)
    arbitrary = await mcp.directory_catalog_call_read("team-tool/private", ctx=ctx)

    assert "only GET, HEAD or OPTIONS" in wrong_read["error"]
    assert "only POST, PUT, PATCH or DELETE" in wrong_write["error"]
    assert arbitrary["error"].startswith("unknown endpoint")


async def test_v1_and_v2_access_tokens_are_not_interchangeable():
    v1_aud = mcp_oauth.mcp_resource_url("v1")
    v2_aud = mcp_oauth.mcp_resource_url("v2")
    v1 = mcp_oauth.make_access_token(user_id=1, org_id=1, audience=v1_aud)
    v2 = mcp_oauth.make_access_token(user_id=1, org_id=1, audience=v2_aud)

    assert mcp_oauth.read_access_token_any(v1, "v1") is not None
    assert mcp_oauth.read_access_token_any(v1, "v2") is None
    assert mcp_oauth.read_access_token_any(v2, "v2") is not None
    assert mcp_oauth.read_access_token_any(v2, "v1") is None
    assert mcp_oauth.mcp_resource_version(v1_aud.rstrip("/")) == "v1"
    assert mcp_oauth.mcp_resource_version(v2_aud.rstrip("/")) == "v2"


async def test_v2_transport_challenges_with_v2_metadata():
    async with directory_session() as client:
        response = await _rpc(client, "tools/list")
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource/mcp/v2" in challenge
    assert response.headers["cache-control"] == "no-store, no-transform"


async def test_v2_serializes_the_scanner_facing_contract(clients):
    token = (await clients.post("/users", json={"email": "directory-list@superdesign.dev"})).json()["token"]
    async with directory_session() as client:
        initialized = await _rpc(client, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "scanner", "version": "1"},
        }, token)
        assert initialized.status_code == 200
        listed = await _rpc(client, "tools/list", token=token)
    tools = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
    assert set(tools) == {
        "catalog_search", "catalog_get", "catalog_call_read", "catalog_call_write", "balance",
        "catalog_request",
    }
    assert tools["catalog_call_read"]["annotations"] == {
        "readOnlyHint": True, "destructiveHint": False, "idempotentHint": False,
        "openWorldHint": True,
    }
    assert tools["catalog_call_write"]["annotations"]["destructiveHint"] is True
    assert "method" not in tools["catalog_call_read"]["inputSchema"]["properties"]
    assert "method" not in tools["catalog_call_write"]["inputSchema"]["properties"]


async def test_transports_reject_the_other_mcp_versions_token():
    v1 = mcp_oauth.make_access_token(user_id=1, org_id=1,
                                     audience=mcp_oauth.mcp_resource_url("v1"))
    v2 = mcp_oauth.make_access_token(user_id=1, org_id=1,
                                     audience=mcp_oauth.mcp_resource_url("v2"))
    async with directory_session() as client:
        assert (await _rpc(client, "tools/list", token=v1)).status_code == 401
        assert (await _rpc(client, "tools/list", token=v2)).status_code == 200

    legacy = mcp.build_mcp_app(resource_version="v1")
    host = FastAPI()
    host.mount("/mcp", legacy)
    async with mcp.mcp_lifespan(legacy):
        async with AsyncClient(transport=ASGITransport(app=host),
                               base_url="http://localhost") as client:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            wrong = await client.post("http://localhost/mcp/", json=body,
                                      headers={**MCP_HEADERS, "Authorization": f"Bearer {v2}"})
            right = await client.post("http://localhost/mcp/", json=body,
                                      headers={**MCP_HEADERS, "Authorization": f"Bearer {v1}"})
    assert wrong.status_code == 401
    assert right.status_code == 200


async def test_claude_origin_is_explicitly_allowed_and_unknown_origins_are_not():
    origins = mcp._allowed_origins()
    assert origins.count("https://claude.ai") == 1
    assert "https://attacker.example" not in origins

    token = mcp_oauth.make_access_token(user_id=1, org_id=1,
                                        audience=mcp_oauth.mcp_resource_url("v2"))
    async with directory_session() as client:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        allowed = await client.post("http://localhost/mcp/v2/", json=body, headers={
            **MCP_HEADERS, "Authorization": f"Bearer {token}", "Origin": "https://claude.ai",
        })
        blocked = await client.post("http://localhost/mcp/v2/", json=body, headers={
            **MCP_HEADERS, "Authorization": f"Bearer {token}",
            "Origin": "https://attacker.example",
        })
    assert allowed.status_code == 200
    assert blocked.status_code == 403
