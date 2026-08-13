"""The legacy-host redirect (treg.superdesign.dev → treg.to).

Only browser-facing marketing pages — and only for ANONYMOUS visitors — 301 to the canonical host.
Everything else is served in place on the legacy host forever: installed CLIs/skills point there
with Bearer tokens, HTTP clients strip Authorization on a cross-host redirect (some MCP clients
follow no redirects at all), session cookies are host-scoped, and `curl {BASE}/install.sh | sh`
runs without -L. The OAuth login ENTRY points are the one unconditional redirect: their state
cookie must be set on the host the callback lands on.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from treg import session as sess
from treg.api import app
from treg.config import get_settings

LEGACY = {"host": "treg.superdesign.dev"}
OFF_HOST_CODES = (301, 302, 303, 307, 308)   # any of these off-host would strand a client


@pytest.fixture
async def raw_client(monkeypatch):
    """No auth, no upstream — routing behavior only. Host is set per-request. The test env's
    public_url is not production's; pin it so the Location assertions mean something."""
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://treg.to")
    get_settings.cache_clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        yield c
    get_settings.cache_clear()


async def test_marketing_pages_redirect_when_anonymous(raw_client):
    for path in ("/", "/login", "/terms", "/privacy", "/support", "/contact", "/help", "/tutorial"):
        r = await raw_client.get(path, headers=LEGACY)
        assert r.status_code == 301, path
        assert r.headers["location"] == f"https://treg.to{path}", path


async def test_query_string_survives_the_redirect(raw_client):
    r = await raw_client.get("/?utm_source=x", headers=LEGACY)
    assert r.status_code == 301
    assert r.headers["location"] == "https://treg.to/?utm_source=x"


async def test_a_session_holder_is_never_bounced_off_their_cookies(raw_client):
    # The invite flow sets a legacy-host session then lands on `/?invite_org=…` — a redirect here
    # would strand the browser on the canonical host without the session it just minted.
    for path in ("/", "/login", "/tutorial"):
        r = await raw_client.get(f"{path}?invite_org=1", headers=LEGACY,
                                 cookies={sess.COOKIE: "any-value"})
        assert r.status_code not in OFF_HOST_CODES or \
            "treg.to" not in r.headers.get("location", ""), path


async def test_oauth_login_entries_redirect_even_with_a_session(raw_client):
    # The state cookie must be minted on the host the provider calls back to (public_url), or the
    # callback sees no cookie and fails with "Bad state" — so these redirect unconditionally.
    for path in ("/auth/github", "/auth/google"):
        for cookies in ({}, {sess.COOKIE: "any-value"}):
            r = await raw_client.get(path, headers=LEGACY, cookies=cookies)
            assert r.status_code == 301, path
            assert r.headers["location"] == f"https://treg.to{path}", path


async def test_api_and_agent_surfaces_are_served_in_place_on_the_legacy_host(raw_client):
    # Never redirected, in ANY flavor — whatever these routes answer (200/401/404/405…), the
    # Location of a same-host redirect is fine, but nothing may point off-host.
    for path in ("/meta", "/llms.txt", "/install.sh", "/selfhost.sh", "/skill.md",
                 "/quickstart.md", "/tutorial.md", "/vendor-listing", "/vendor-listing.md",
                 "/catalog/search", "/call/x/some-tool", "/auth/me", "/auth/github/callback",
                 "/auth/google/callback", "/auth/cli/poll", "/billing/stripe/webhook",
                 "/.well-known/oauth-protected-resource"):
        r = await raw_client.get(path, headers=LEGACY)
        assert not (r.status_code in OFF_HOST_CODES
                    and "treg.to" in r.headers.get("location", "")), path


async def test_post_is_never_redirected(raw_client):
    r = await raw_client.post("/", headers=LEGACY)
    assert r.status_code not in OFF_HOST_CODES


async def test_lookalike_host_is_not_treated_as_canonical(raw_client):
    # The self-hoster guard is hostname EQUALITY with public_url, not substring: a lookalike like
    # not-treg.superdesign.dev is simply an unknown Host — served, never redirected.
    r = await raw_client.get("/", headers={"host": "not-treg.superdesign.dev"})
    assert r.status_code == 200


async def test_canonical_host_is_untouched(raw_client):
    r = await raw_client.get("/", headers={"host": "treg.to"})
    assert r.status_code == 200


async def test_legacy_mcp_host_and_oauth_audience_stay_valid():
    # The transport allow-list and the token-audience set must both keep honouring the legacy name —
    # every pre-move .mcp.json and OAuth grant depends on it.
    from treg import mcp, mcp_oauth
    assert "treg.superdesign.dev" in mcp._allowed_hosts()
    assert "https://treg.superdesign.dev" in mcp._allowed_origins()
    assert "https://treg.superdesign.dev/mcp/" in mcp_oauth.mcp_resource_audiences()
    # A pre-move access token — audience = the legacy resource URL — must still validate, and so
    # must the canonical one; a token minted for someone else's server must not.
    old = mcp_oauth.make_access_token(user_id=1, org_id=1, scope="treg:read", token_version=0,
                                      audience="https://treg.superdesign.dev/mcp/")
    assert mcp_oauth.read_access_token_any(old) is not None
    foreign = mcp_oauth.make_access_token(user_id=1, org_id=1, scope="treg:read", token_version=0,
                                          audience="https://evil.example/mcp/")
    assert mcp_oauth.read_access_token_any(foreign) is None
