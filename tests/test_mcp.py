"""The MCP front door — that it answers, and that it enforces the same rules as every other door.

Two halves, and the second is the one that matters. A new entrance onto a paid catalog is only safe
if it cannot see another team's data, cannot spend past the caps, and cannot be talked into serving
an endpoint whose price nobody knows. `mcp.py` gets that by routing through treg's own API rather
than reaching into the internals — these tests are what proves the routing is real and not merely
intended.

The transport is exercised as a real MCP client would: JSON-RPC over the mounted ASGI app.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from treg.api import app

pytestmark = pytest.mark.anyio

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


async def _rpc(client: AsyncClient, method: str, params=None, token: str | None = None):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    # An ABSOLUTE url so the Host header is a real one. The transport enforces DNS-rebinding
    # protection, and the suite's base_url ("http://registry") is deliberately not on the
    # allow-list — see test_an_unknown_host_is_refused.
    r = await client.post("http://localhost/mcp/", json=body, headers=headers)
    return r


async def _call_tool(client: AsyncClient, name: str, args: dict, token: str | None = None) -> dict:
    await _rpc(client, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"}}, token)
    r = await _rpc(client, "tools/call", {"name": name, "arguments": args}, token)
    payload = r.json()
    content = (payload.get("result") or {}).get("content") or []
    if content and content[0].get("type") == "text":
        try:
            return json.loads(content[0]["text"])
        except ValueError:
            return {"_text": content[0]["text"]}
    return payload


@asynccontextmanager
async def mcp_session(client: AsyncClient):
    """Run the MCP lifespan around a block of requests.

    Deliberately NOT a fixture. The lifespan holds an anyio task group, and a fixture enters it in
    one task and exits it in another — anyio refuses that ("Attempted to exit cancel scope in a
    different task"). Entering it inside the test body keeps both ends in the same task.

    That the mounted app needs its lifespan run at all is the trap the module docstring warns about:
    `app.mount()` does not run it, and the transport builds its task group there. It caught me here
    first, which is the cheapest place for it to happen.

    The default `X-Treg-Token` is dropped: MCP carries identity in `Authorization`, and leaving a
    second credential on the client would let a test pass for the wrong reason.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient as _AC

    from treg import mcp as _mcp

    # A FRESH transport per test: `StreamableHTTPSessionManager.run()` may be called once per
    # instance, so the module-level one cannot be restarted between tests. The tools are the same
    # objects either way — they hang off the shared `mcp` server — and they still reach the real
    # `treg.api.app` internally, so the enforcement under test is the production path.
    fresh = _mcp.build_mcp_app()
    host = FastAPI()
    host.mount("/mcp", fresh)
    client.headers.pop("X-Treg-Token", None)
    async with _mcp.mcp_lifespan(fresh):
        async with _AC(transport=ASGITransport(app=host), base_url="http://localhost") as mc:
            mc.headers.update({k: v for k, v in client.headers.items() if k.lower() == "cookie"})
            yield mc


async def test_the_server_lists_exactly_the_five_tools(clients):
    """Five tools, not 2,600. The catalog is DATA reached through a tool, never a tool per endpoint —
    2,600 schemas would bury the model's context and make the catalog unusable."""
    async with mcp_session(clients) as c:
        await _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}})
        r = await _rpc(c, "tools/list")
        names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == {"catalog_search", "catalog_get", "call", "balance", "my_tools"}


async def test_catalog_search_answers_without_any_token(clients):
    """The catalog is public — discovery must work before anyone has signed up, or the first
    impression of the plugin is an auth error."""
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": "backlinks", "limit": 3})
    assert out["results"], out
    first = out["results"][0]
    assert first["endpoint_id"] and first["provider"]
    assert "usd_per_call" in first and "no_key_needed" in first


async def test_search_says_so_when_nothing_matches(clients):
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": "zzzz-no-such-capability"})
    assert out["count"] == 0
    assert "hint" in out


# ---- the half that matters: no token means no data, no spending ----------------------------

@pytest.mark.parametrize("tool", ["call", "balance", "my_tools"])
async def test_every_spending_or_tenant_tool_refuses_without_a_token(clients, tool):
    """A public MCP endpoint onto a paid catalog is the whole risk of this feature. Anything that
    reads a team's data or moves its money must fail closed, and say what to do about it.

    The refusal is now an HTTP 401 carrying `WWW-Authenticate`, rather than an error dict inside a
    200 — see `test_a_protected_tool_answers_401_with_WWW_Authenticate` for why the shape matters.
    This test keeps its original job: proving these three cannot be reached without a credential."""
    args = {"endpoint_id": "tikhub.tiktok.video.comments"} if tool == "call" else {}
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}, headers=MCP_HEADERS)
    assert r.status_code == 401, r.text
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


async def test_a_bogus_token_gets_nothing(clients):
    """Headers are client-supplied input. A well-formed but unknown token must be rejected by the
    database, not accepted because it looks like one."""
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token="tok_not_a_real_token_at_all")
    assert "balance_usd" not in out, out
    assert out.get("error")


async def test_a_real_token_reads_its_OWN_balance(clients):
    """The positive case, and the one that proves the plumbing: a genuine token resolves to its org
    through `/auth/me` and reads that org's balance — the same route the CLI uses."""
    token = (await clients.post("/auth/cli-token")).json()["token"] if False else None
    # the suite's client was created with a real per-org token; recover it before it is dropped
    r = await clients.post("/users", json={"email": "mcpuser@superdesign.dev"})
    token = r.json()["token"]
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=token)
    assert "balance_usd" in out, out
    assert out["balance_usd"] >= 0


async def test_an_IDENTITY_token_resolves_its_team(clients):
    """The bug production found. There are two kinds of token: a PER-ORG token (`treg org agent-new`)
    has its team baked in and `/auth/me` reports it; an IDENTITY token (`treg login` — what most
    people actually hold) belongs to a person who may be in several teams, so `/auth/me` reports no
    org and every `/orgs/{id}/…` route must be told which one. Resolving only the first kind meant
    `balance` answered "could not resolve the team" for the commonest token there is."""
    r = await clients.post("/users", json={"email": "identity-user@superdesign.dev"})
    per_org = r.json()["token"]
    clients.headers["X-Treg-Token"] = per_org
    identity = (await clients.get("/auth/cli-token")).json()["token"]
    assert identity != per_org

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=identity)
    assert "balance_usd" in out, out
    assert out.get("team")


async def test_one_team_cannot_read_another_teams_tools(clients):
    """Tenant isolation, asserted rather than assumed. Two orgs, two tokens: each sees only its own
    registered tools. This is the property a second front door is most likely to lose."""
    a = (await clients.post("/users", json={"email": "org-a@superdesign.dev"})).json()["token"]
    b = (await clients.post("/users", json={"email": "org-b@superdesign.dev"})).json()["token"]
    clients.headers["X-Treg-Token"] = a
    made = await clients.post("/tools", json={"name": "a-only-tool", "base_url": "http://upstream"})
    assert made.status_code == 200, made.text

    async with mcp_session(clients) as c:
        seen_a = await _call_tool(c, "my_tools", {}, token=a)
        seen_b = await _call_tool(c, "my_tools", {}, token=b)
    names_a = {t["name"] for t in seen_a.get("tools", [])}
    names_b = {t["name"] for t in seen_b.get("tools", [])}
    # Not vacuous: org A must genuinely SEE its tool, or "B cannot see it" proves nothing.
    assert names_a, f"org A saw no tools at all — the assertion below would pass for free: {seen_a}"
    assert "a-only-tool" in names_a
    assert "a-only-tool" not in names_b, f"org B saw org A's tool: {seen_b}"


async def test_call_reaches_the_TEAMS_OWN_tools_too(clients):
    """`my_tools` lists what the team registered; `call` must be able to call it.

    The first version pre-checked the catalog and refused anything absent, which made `my_tools` a
    list of things an agent could see and never use — found by trying it on production. `/call/`
    already resolves a team's own tool first and falls back to a catalog id, so the fix was to stop
    second-guessing it."""
    made = await clients.post("/tools", json={"name": "echo", "base_url": "http://upstream"})
    assert made.status_code == 200, made.text
    token = clients.headers.get("X-Treg-Token")

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {"endpoint_id": "echo/anything"}, token=token)
    assert out.get("status") == 200, out
    assert "unknown endpoint" not in json.dumps(out)


async def test_call_refuses_an_endpoint_that_does_not_exist(clients):
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "call", {"endpoint_id": "nope.not.real"}, token="tok_whatever")
    assert "unknown endpoint" in out.get("error", "")
    assert "catalog_search" in out.get("hint", "")   # names the way out, rather than leaving it guessing


async def test_tool_descriptions_do_not_promise_routing(clients):
    """The charter's standing rule, and the one the landing page already had to be corrected for:
    treg COMPARES providers and the caller chooses. These descriptions are read by every model that
    installs the plugin, so a false claim here travels further than the website's did."""
    async with mcp_session(clients) as c:
        await _rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "t", "version": "1"}})
        r = await _rpc(c, "tools/list")
        blob = json.dumps(r.json()).lower()
    for claim in ("routes for you", "automatic failover", "fails over", "picks the best provider"):
        assert claim not in blob


async def test_an_unknown_host_is_refused(clients):
    """The SDK's DNS-rebinding protection, proven live rather than trusted.

    It ships ON with an EMPTY allow-list, which 421s EVERYTHING — a deploy looks healthy until the
    first tool call. `mcp._allowed_hosts()` builds the list from this deployment's `public_url` plus
    the loopback names, so this asserts both directions: a known host works (every other test) and an
    unknown one does not."""
    async with mcp_session(clients) as c:
        r = await c.post("http://evil.example.com/mcp/", json={"jsonrpc": "2.0", "id": 1,
                         "method": "tools/list"}, headers=MCP_HEADERS)
    assert r.status_code == 421, r.text


async def test_the_deployments_own_host_is_allowed():
    """The list must contain the host treg actually answers on, or production 421s every call."""
    from urllib.parse import urlsplit

    from treg.config import get_settings
    from treg.mcp import _allowed_hosts

    assert urlsplit(get_settings().public_url).netloc in _allowed_hosts()


async def test_the_price_is_visible_before_spending(clients):
    """An agent that cannot see a price before calling cannot warn the human, and the skill's rule is
    to state the cost first. Search must carry the number."""
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": "backlinks", "limit": 5})
    assert any(r.get("usd_per_call") is not None for r in out["results"])


# ---- what the plugin directory's review checks ---------------------------------------------

async def test_every_tool_declares_what_it_can_do(clients):
    """The submission portal validates `readOnlyHint`/`openWorldHint`/`destructiveHint` against the
    tool's real behaviour, and a model consults them before acting. The four reading tools change
    nothing; `call` is the honest exception — it relays whatever the caller asks to whichever
    upstream the endpoint names, which can be a POST that publishes, an email that sends or a DELETE.
    treg does not model the upstream, so it cannot promise the call is safe, and saying otherwise
    here would be a false assurance in the exact place it matters."""
    from treg.mcp import mcp as server

    ann = {t.name: t.annotations for t in await server.list_tools()}
    assert set(ann) == {"catalog_search", "catalog_get", "call", "balance", "my_tools"}
    for name in ("catalog_search", "catalog_get", "balance", "my_tools"):
        a = ann[name]
        assert a and a.read_only_hint is True, name
        assert a.destructive_hint is False and a.open_world_hint is False, name
    a = ann["call"]
    assert a.read_only_hint is False
    assert a.destructive_hint is True and a.open_world_hint is True


async def test_the_domain_challenge_is_404_until_configured(clients):
    """Empty means unset, and unset must 404. An empty 200 would read to the portal as a
    verification that never completes, which is harder to debug than a plain absence."""
    r = await clients.get("/.well-known/openai-apps-challenge")
    assert r.status_code == 404


async def test_the_domain_challenge_returns_the_token_ALONE(clients, monkeypatch):
    """The portal is explicit: that URL must return the token and nothing else — not JSON, not a
    list, not several tokens. Returning a JSON body here is a documented rejection."""
    from treg import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "openai_apps_challenge", "tok-abc-123", raising=False)
    r = await clients.get("/.well-known/openai-apps-challenge")
    assert r.status_code == 200
    assert r.text == "tok-abc-123"
    assert r.headers["content-type"].startswith("text/plain")


async def test_a_BROWSER_origin_is_accepted(clients):
    """`"*"` is not a wildcard in this SDK — origins are compared literally, and only a `:*` port
    suffix is special. Setting `allowed_origins=["*"]` therefore allowed exactly one origin, the
    literal string "*", and refused every browser with "Invalid Origin header".

    Nothing caught it: the suite and every CLI client send NO Origin header, so the check never ran
    until a real web page called /mcp/. This test sends one deliberately."""
    from treg.config import get_settings

    origin = get_settings().public_url.rstrip("/")
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "browser", "version": "1"}}},
            headers={**MCP_HEADERS, "Origin": origin})
    assert r.status_code == 200, f"a browser at our own origin must be served: {r.text[:120]}"


async def test_an_UNKNOWN_origin_is_still_refused(clients):
    """The protection has to remain real — the fix widens the list, it does not remove the check."""
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={**MCP_HEADERS, "Origin": "https://attacker.example"})
    assert r.status_code == 403, r.text


async def test_every_tool_declares_what_it_RETURNS(clients):
    """ChatGPT's connector review flags a tool with no output schema, and a model that has to guess
    at field names guesses. Each tool now declares its shape."""
    from treg.mcp import mcp as server

    for t in await server.list_tools():
        assert t.output_schema, f"{t.name} has no output schema"
        assert t.output_schema.get("properties"), f"{t.name}'s schema is empty"


async def test_the_output_schema_does_not_BREAK_the_error_paths(clients):
    """The load-bearing detail. A strict schema is validated on the way out, so `{"error": "not
    authenticated"}` would RAISE instead of returning — turning a refusal written to tell an agent
    how to recover into an opaque tool failure.

    Every field is optional so both shapes pass. A schema is a hint to the model, not a gate on our
    own error handling."""
    from treg.mcp import mcp as server

    for t in await server.list_tools():
        assert not t.output_schema.get("required"), (
            f"{t.name} has required output fields — the first error response will raise")

    # and prove it end to end, not just in the schema. A BAD token rather than none: a missing
    # credential is now answered at the transport with a 401, so it never reaches the tool — and it
    # is the tool's own error shape that has to survive the schema.
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token="not-a-real-token")
    assert out.get("error"), out
    assert "hint" in out or "detail" in out, "the recovery instruction must survive the schema"


async def test_the_schema_tolerates_NULLS_not_just_missing_keys(clients):
    """`total=False` says a key may be ABSENT; it does not say the value may be null. Real rows carry
    nulls — a registered tool with no description, an endpoint with no published price — and typing
    those as plain `str` made `my_tools` return a schema error instead of the team's tools.

    Asserted on the schema so the next field added is held to the same rule."""
    from treg.mcp import mcp as server

    tools = {t.name: t.output_schema for t in await server.list_tools()}
    defs = tools["my_tools"].get("$defs", {})
    team_tool = defs.get("TeamTool", {})
    for field, spec in team_tool.get("properties", {}).items():
        allows_null = "null" in str(spec)
        assert allows_null, f"TeamTool.{field} does not allow null — a real row will fail validation"


# ---- refusing in the right SHAPE, not just refusing ------------------------------------------

@pytest.mark.parametrize("tool", ["call", "balance", "my_tools"])
async def test_a_protected_tool_answers_401_with_WWW_Authenticate(clients, tool):
    """The spec has a protected resource reply 401 with `WWW-Authenticate: Bearer
    resource_metadata="…"`, because that header is how a client DISCOVERS it must authenticate and
    where to start. A friendly sentence inside a 200 tells a human what happened and tells a program
    nothing.

    This passed unnoticed because ChatGPT authenticates up front. A client that connects first and
    discovers auth lazily — which the spec allows — would read 200 as success."""
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": {}}}, headers=MCP_HEADERS)
    assert r.status_code == 401, r.text
    challenge = r.headers.get("www-authenticate", "")
    assert challenge.startswith("Bearer "), challenge
    assert "resource_metadata=" in challenge
    assert "/.well-known/oauth-protected-resource" in challenge
    assert r.json()["resource_metadata"].endswith("/.well-known/oauth-protected-resource")


@pytest.mark.parametrize("tool,args", [("catalog_search", {"query": "backlinks"}),
                                       ("catalog_get", {"endpoint_id": "hunter.people.email.find"})])
async def test_the_PUBLIC_tools_still_answer_without_a_token(clients, tool, args):
    """Deliberate, and worth protecting: someone evaluating treg should see what is in the catalog
    and what it costs before creating an account. Nothing tenant-specific or spendable is exposed —
    the same data /catalog/search already serves on the website."""
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": args}}, headers=MCP_HEADERS)
    assert r.status_code == 200, r.text


async def test_discovery_still_works_unauthenticated(clients):
    """`initialize` and `tools/list` must not be challenged, or a client cannot learn what exists
    before deciding to authenticate."""
    async with mcp_session(clients) as c:
        for method in ("initialize", "tools/list"):
            params = {"protocolVersion": "2025-06-18", "capabilities": {},
                      "clientInfo": {"name": "t", "version": "1"}} if method == "initialize" else None
            body = {"jsonrpc": "2.0", "id": 1, "method": method}
            if params:
                body["params"] = params
            r = await c.post("http://localhost/mcp/", json=body, headers=MCP_HEADERS)
            assert r.status_code == 200, f"{method}: {r.status_code}"


async def test_a_BAD_token_is_the_tool_s_business_not_the_transport_s(clients):
    """The challenge fires only when there is NO credential. Deciding whether a token is valid needs
    the database, and doing that in transport middleware would put a second authentication
    implementation in front of the first."""
    async with mcp_session(clients) as c:
        r = await c.post("http://localhost/mcp/", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "balance", "arguments": {}}},
            headers={**MCP_HEADERS, "Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 200
    assert "error" in json.loads(r.json()["result"]["content"][0]["text"])
