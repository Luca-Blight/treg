"""The MCP front door — treg's own operations, exposed to an agent that has no terminal.

A coding agent reaches treg through the CLI, and the skill tells it which commands to run. An agent
inside ChatGPT or a Codex plugin has no CLI, and telling it to install one is where the visitor
leaves. This module is the other door: five tools over MCP, so an agent can search the catalog, read
a price and make the call without anything being installed first.

**Five tools, not 2,600.** The catalog stays *data* — one tool searches it, one reads an entry, one
calls an endpoint. Exposing every endpoint as its own MCP tool would flood the model's context with
2,600 schemas and make the catalog unusable, which is the opposite of the point.

**One implementation of the rules.** Everything that touches money, tenancy or credentials goes back
through treg's own HTTP API in-process (`httpx.ASGITransport`), rather than reaching into the
internals a second time. `/call/` already enforces the per-member tool ACL, deny rules, the per-user
daily cap, the platform daily cap, the balance reserve and the settle — a second entrance that
re-implemented any of that is exactly how one copy quietly stops being enforced. The cost is one
in-process round trip with no socket, which measured at well under a millisecond.

Only the **public catalog** is read directly (`catalog_store`), because it is already parsed in
memory, needs no database and no identity, and answering from it takes ~1 ms.

**Headers are not identity.** The SDK's own docstring says it and it is worth repeating: the bearer
token arriving on an MCP request is client-supplied input. It is validated against the database on
every call by the same code the HTTP API uses — never trusted because it looks well-formed.

Transport is **stateless streamable HTTP with JSON responses**: production runs more than one
instance, and a session-bound transport would need sticky routing to be reliable.

Mounting has one trap, and it is silent: `app.mount()` does NOT run the mounted app's lifespan, and
the session manager initialises its task group there — every request then fails with "Task group is
not initialized". `api.py` must compose this module's lifespan with its own; see `mcp_lifespan`.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings

from . import catalog_store
from .config import get_settings

# One shared in-process client. ASGITransport speaks straight to the app object — no socket, no DNS,
# no TLS — so this is a function call wearing an HTTP shape, which is what makes reusing the real
# route cheap enough to be the honest choice.
_INTERNAL_BASE = "http://treg.internal"
_TIMEOUT = httpx.Timeout(120.0, connect=5.0)

mcp = MCPServer(
    name="treg",
    title="treg — the tool catalog for your agent",
    description=(
        "Call the tool you need for a task without owning its API key. ~2,600 curated API endpoints "
        "across ~40 providers (SEO, SERP, backlinks, social, people and company enrichment, ads, "
        "scraping). Most need no key at all: treg holds the credential and meters each call from "
        "your team's prepaid balance."
    ),
    instructions=(
        "Ask for the task, not the tool. Start with catalog_search using words for what you want to "
        "DO ('work email', 'backlinks', 'tiktok comments') — not a vendor name. Read the price with "
        "catalog_get before spending, then call. treg COMPARES providers and reports what it "
        "measured; choosing is yours. There is no automatic routing and no failover."
    ),
)


def _bearer(ctx: Context) -> str:
    """The caller's token, straight from the request headers.

    Returns "" when absent — every authenticated tool then fails closed with an instruction rather
    than a stack trace, because a missing token is the single most likely first-run problem.
    """
    headers = ctx.headers or {}
    raw = headers.get("authorization") or headers.get("Authorization") or ""
    return raw.removeprefix("Bearer ").removeprefix("bearer ").strip()


def _need_token() -> dict:
    return {
        "error": "not authenticated",
        "detail": (
            "This MCP server needs a treg token. Get one at https://treg.superdesign.dev "
            "(sign in, then Settings -> copy token) and set it as the TREG_TOKEN environment "
            "variable for this server."
        ),
    }


@asynccontextmanager
async def _api(token: str):
    """An in-process client bound to treg's own ASGI app, carrying the caller's identity."""
    from .api import app  # deferred: api.py imports THIS module to mount it

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=_INTERNAL_BASE,
        timeout=_TIMEOUT,
        headers={"X-Treg-Token": token},
    ) as client:
        yield client


def _body(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text


async def _resolve_org(client: httpx.AsyncClient) -> tuple[int | None, str | None, dict | None]:
    """Which team is this caller acting for? Returns `(org_id, slug, problem)`.

    There are two kinds of token and they answer differently — a distinction that cost a production
    bug here. A PER-ORG token (what `treg org agent-new` mints) has its org baked in and `/auth/me`
    reports it. An IDENTITY token (what `treg login` gives, which is what most people actually hold)
    belongs to a person who may be in several teams, so `/auth/me` reports no org at all and every
    `/orgs/{id}/…` route needs to be told which one.

    So: ask `/auth/me` first, then fall back to `/orgs` and take the active team — the same order
    `cli._active_org_id` uses. When a person is in several teams and none is marked active, say so
    and NAME them rather than silently picking one: reading the wrong team's balance is a confusing
    answer, and spending from it would be worse.
    """
    me = await client.get("/auth/me")
    if me.status_code == 200 and _body(me).get("org_id"):
        body = _body(me)
        return int(body["org_id"]), body.get("org"), None

    r = await client.get("/orgs")
    if r.status_code == 401 or me.status_code == 401:
        return None, None, {"error": "not signed in, or this token is invalid or expired",
                            "hint": "copy a fresh token from https://treg.superdesign.dev"}
    if r.status_code != 200:
        return None, None, {"error": "could not read the teams for this token"}
    orgs = _body(r) or []
    if not orgs:
        return None, None, {"error": "this account is not a member of any team"}
    active = [o for o in orgs if o.get("active")]
    chosen = active[0] if active else (orgs[0] if len(orgs) == 1 else None)
    if chosen is None:
        return None, None, {
            "error": "this account belongs to several teams and none is marked active",
            "teams": [o.get("slug") for o in orgs],
            "hint": "ask the human which team to use, then set TREG_TOKEN to that team's token "
                    "(treg org agent-new) so the choice is unambiguous",
        }
    return int(chosen["org_id"]), chosen.get("slug"), None


# --------------------------------------------------------------------------------------------
# The catalog — public, in memory, no identity needed
# --------------------------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Search ~2,600 API endpoints by WHAT YOU WANT TO DO, not by vendor. Use plain task words: "
        "'work email', 'backlinks for a domain', 'tiktok comments', 'keyword search volume'. "
        "Returns each endpoint's id, provider, price per call, and whether treg can serve it "
        "without you owning an API key. Call this FIRST when a task needs data or an API you have "
        "no key for."
    )
)
async def catalog_search(query: str, limit: int = 8) -> dict:
    cat = catalog_store.load()
    ranked, total = catalog_store.search(query, cat, max(1, min(limit, 25)))
    results = []
    for ep, score in ranked:
        cost = cat.cost_view(ep.get("cost"), ep.get("provider")) or {}
        results.append({
            "endpoint_id": ep["id"],
            "name": ep.get("name") or (ep.get("summary") or "")[:70],
            "provider": ep.get("provider"),
            "usd_per_call": cost.get("usd"),
            "no_key_needed": cat.platform_eligible(ep),
            "score": score,
        })
    out = {"query": query, "count": len(results), "total_matches": total, "results": results}
    if not results:
        out["hint"] = (
            f"nothing matches all of {query!r} — drop a word, or try a different way of saying the task"
        )
    else:
        out["next"] = "catalog_get(endpoint_id) for parameters and the exact price, then call(...)"
    return out


@mcp.tool(
    description=(
        "One endpoint in full: its parameters, the exact price per call, whether treg serves it on "
        "its own key, and the other providers that answer the same capability — with the success "
        "rate and speed treg has measured across real calls. Read this BEFORE call() so you can "
        "tell the human what it will cost."
    )
)
async def catalog_get(endpoint_id: str, ctx: Context) -> dict:
    """Goes through the HTTP route rather than the store: that route attaches the observed
    reliability figures and the capability siblings, and those come from the database."""
    token = _bearer(ctx)
    async with _api(token) as client:
        r = await client.get(f"/catalog/endpoints/{endpoint_id}")
    if r.status_code == 404:
        return {"error": f"unknown endpoint {endpoint_id!r}",
                "hint": "use catalog_search to find the right id"}
    return _body(r)


# --------------------------------------------------------------------------------------------
# Everything below spends money or reads a team's data — all of it goes back through the API
# --------------------------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Call something through treg. Either a CATALOG endpoint by its id (from catalog_search), or "
        "one of THIS TEAM'S own tools as '<tool-name>/<path>' (from my_tools) — e.g. "
        "'render/v1/services'. treg injects the credential server-side and relays the provider's "
        "response unchanged, so you never hold an API key. Catalog calls on treg's key are metered "
        "from the team's prepaid balance; a team's own tool is never metered. Tell the human the "
        "price (from catalog_get) before calling anything that costs more than a cent."
    )
)
async def call(endpoint_id: str, params: dict | None = None,
               method: str | None = None, ctx: Context = None) -> dict:  # type: ignore[assignment]
    token = _bearer(ctx) if ctx else ""
    if not token:
        return _need_token()

    # BOTH halves answer here, exactly as `treg call` does: `/call/{rest}` resolves a team's own tool
    # first and falls back to a catalog id, so an org tool always wins over the catalog. Pre-checking
    # the catalog and refusing anything absent would have made `my_tools` a list of things the agent
    # could see and never call — which is how this gap was found.
    ep = catalog_store.load().by_id.get(endpoint_id)
    if ep is None and "/" not in endpoint_id:
        return {"error": f"unknown endpoint {endpoint_id!r}",
                "hint": "use catalog_search for a catalog id, or my_tools then "
                        "'<tool-name>/<path>' for one of this team's own tools"}

    method = (method or (ep.get("method") if ep else None) or "GET").upper()
    args = params or {}
    async with _api(token) as client:
        # The SAME route the CLI and the proxy use, so the tool ACL, deny rules, both daily caps,
        # the balance reserve and the settle all happen exactly once, in one place.
        if method in ("GET", "HEAD", "DELETE"):
            r = await client.request(method, f"/call/{endpoint_id}", params=args)
        else:
            r = await client.request(method, f"/call/{endpoint_id}", json=args)

    out: dict[str, Any] = {"status": r.status_code, "endpoint_id": endpoint_id, "body": _body(r)}
    spent = r.headers.get("X-Treg-Cost-Micro")
    if spent:
        try:
            out["cost_usd"] = round(int(spent) / 1_000_000, 6)
        except ValueError:
            pass
    if r.status_code == 402:
        out["hint"] = "the team's prepaid balance is short — top up at https://treg.superdesign.dev"
    elif r.status_code >= 400:
        # Whose fault it was matters to an agent deciding whether to retry elsewhere.
        out["whose_error"] = "treg" if r.headers.get("X-Treg-Error") else "provider"
    return out


@mcp.tool(
    description=(
        "The team's prepaid balance in USD, and any spend currently in flight. Check it when a call "
        "is refused for funds, or before a job that will make many calls."
    )
)
async def balance(ctx: Context) -> dict:
    token = _bearer(ctx)
    if not token:
        return _need_token()
    async with _api(token) as client:
        org_id, slug, problem = await _resolve_org(client)
        if problem:
            return problem
        r = await client.get(f"/orgs/{org_id}/balance", headers={"X-Treg-Org": slug or ""})
    body = _body(r)
    if r.status_code != 200:
        return {"error": "could not read the balance", "detail": body}
    return {
        "team": slug,
        "balance_usd": round((body.get("balance_micro") or 0) / 1_000_000, 6),
        "balance_micro": body.get("balance_micro"),
        "holds_micro": body.get("holds_micro"),
    }


@mcp.tool(
    description=(
        "What THIS team has registered and you can call without holding the credential: their own "
        "API keys, OAuth connections, vendor CLIs and skills. A team's own tool always wins over "
        "treg's catalog key, and those calls are never metered."
    )
)
async def my_tools(ctx: Context) -> dict:
    token = _bearer(ctx)
    if not token:
        return _need_token()
    async with _api(token) as client:
        _, slug, problem = await _resolve_org(client)
        if problem:
            return problem
        r = await client.get("/tools", headers={"X-Treg-Org": slug or ""})
    body = _body(r)
    if r.status_code != 200:
        return {"error": "could not list the team's tools", "detail": body}
    tools = body if isinstance(body, list) else body.get("tools", [])
    return {
        "team": slug,
        "count": len(tools),
        "tools": [{"name": t.get("name"), "base_url": t.get("base_url"),
                   "description": t.get("description")} for t in tools],
    }


# --------------------------------------------------------------------------------------------
# Mounting
# --------------------------------------------------------------------------------------------

def _allowed_hosts() -> list[str]:
    """Which `Host` headers the transport will answer to.

    The SDK ships DNS-rebinding protection ON with an EMPTY allow-list, which rejects **everything**
    with a 421 — the default is unusable rather than merely strict, and it fails at request time, so
    a deploy looks healthy right up until the first tool call. Found by the tests here; it would
    otherwise have been found in production.

    The protection is real (it stops a web page from driving a localhost MCP server), so it stays on
    and the list is built instead: this deployment's own `public_url`, the loopback names a developer
    actually uses, and the in-process host the API calls itself on. `TREG_MCP_ALLOWED_HOSTS` adds
    more, comma-separated, for a deployment behind another name — the ngrok dev box, say, or a second
    domain.
    """
    hosts: list[str] = []
    public = urlsplit(get_settings().public_url).netloc
    if public:
        hosts += [public, public.split(":")[0]]
    hosts += ["localhost", "127.0.0.1", "treg.internal"]
    hosts += [h.strip() for h in os.environ.get("TREG_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    # a bare host and host:port are different header values, so allow the common local ports too
    hosts += [f"localhost:{p}" for p in ("8000", "18790")] + [f"127.0.0.1:{p}" for p in ("8000", "18790")]
    return sorted(dict.fromkeys(hosts))


def build_mcp_app():
    """A fresh ASGI app for the MCP transport.

    A factory rather than a bare module-level value because each call builds its own session
    manager, and `StreamableHTTPSessionManager.run()` may be called **once per instance** — a second
    start raises. That is right for a server (one process, one lifespan) and impossible for a test
    suite, which needs a clean instance per test. The tools themselves are shared: they hang off the
    module-level `mcp` server, so a fresh transport still exercises the real implementations.

    `stateless_http` because production runs more than one instance and a session-bound transport
    would need sticky routing; `json_response` because these are request/response tools with nothing
    to stream, and skipping SSE framing is most of the speed.
    """
    return mcp.streamable_http_app(
        streamable_http_path="/", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_allowed_hosts(),
            allowed_origins=["*"],  # identity is the bearer token, not the origin; a CLI sends none
        ),
    )


mcp_app = build_mcp_app()


@asynccontextmanager
async def mcp_lifespan(target=None):
    """MUST be entered by the host app's lifespan. `app.mount()` does not run a mounted app's
    lifespan, and the streamable-HTTP session manager builds its task group there — without this
    every MCP request fails with "Task group is not initialized"."""
    target = target or mcp_app
    async with target.router.lifespan_context(target):
        yield
