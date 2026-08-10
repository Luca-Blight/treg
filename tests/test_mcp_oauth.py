"""treg as an OAuth authorization server — the metadata, the refusals, and client registration.

Nothing issues tokens yet. This file covers the half that says NO, deliberately built first: there is
never a window where the server accepts tokens it has not learned to check.

The assertion that matters most is the audience one. Everything else here — signature, expiry, type —
is ordinary token hygiene that any login system needs. `aud` is the one that exists because we are a
RESOURCE server: a user grants a client access to a named resource, and a token addressed to some
other MCP server must not spend a treg balance just because it happens to be validly signed by us.
"""

from __future__ import annotations

import time

import pytest

from treg import mcp, mcp_oauth, session

pytestmark = pytest.mark.anyio


# ---- metadata: what we tell a client we support --------------------------------------------

async def test_protected_resource_metadata_names_the_mcp_endpoint(clients):
    r = await clients.get("/.well-known/oauth-protected-resource")
    assert r.status_code == 200
    body = r.json()
    assert body["resource"] == mcp_oauth.mcp_resource_url()
    assert body["resource"].endswith("/mcp/"), "the trailing slash is part of the identifier"
    assert body["authorization_servers"], "a client must be told who issues tokens for us"


async def test_the_metadata_is_served_at_BOTH_lookup_paths(clients):
    """The spec has clients look this up either at the host root or under the resource's own path,
    and which one a given client tries is not ours to choose."""
    a = await clients.get("/.well-known/oauth-protected-resource")
    b = await clients.get("/.well-known/oauth-protected-resource/mcp")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()


async def test_authorization_server_metadata_offers_only_safe_choices(clients):
    """OAuth 2.1 drops the implicit grant, and `plain` PKCE makes the challenge equal to the secret —
    anyone who sees the authorization request could redeem the code. Offering either would be a
    downgrade a client is entitled to take us up on."""
    body = (await clients.get("/.well-known/oauth-authorization-server")).json()
    assert body["response_types_supported"] == ["code"]
    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "plain" not in body["code_challenge_methods_supported"]
    assert body["authorization_endpoint"].endswith("/oauth/authorize")
    assert body["token_endpoint"].endswith("/oauth/token")


async def test_metadata_advertises_BOTH_ways_for_a_client_to_identify_itself(clients):
    """ChatGPT sends a client-id metadata document; Claude Code and most others register
    dynamically. Advertising only one would lock the other out — and we would not notice, because
    the client we test with is the one that does not need registration."""
    body = (await clients.get("/.well-known/oauth-authorization-server")).json()
    assert body["client_id_metadata_document_supported"] is True
    assert body["registration_endpoint"].endswith("/oauth/register")


# ---- the refusals ---------------------------------------------------------------------------

async def test_a_token_for_ANOTHER_resource_is_refused():
    """The load-bearing check. A user consented to some other MCP server; that grant must not spend
    a treg balance merely because we signed the token."""
    ours = mcp_oauth.mcp_resource_url()
    elsewhere = mcp_oauth.make_access_token(user_id=7, org_id=3,
                                            audience="https://evil.example.com/mcp/")
    assert mcp_oauth.read_access_token(elsewhere, expected_audience=ours) is None
    assert mcp._oauth_claims(elsewhere) is None


async def test_our_own_token_is_accepted_and_carries_the_team():
    """Not vacuous: the refusal above only means something if the matching token DOES work, and the
    team has to survive on the token — a person can belong to several, and the choice is made once
    by a human at consent, not guessed per call."""
    ours = mcp_oauth.mcp_resource_url()
    tok = mcp_oauth.make_access_token(user_id=7, org_id=3, audience=ours, scope="treg:call")
    claims = mcp._oauth_claims(tok)
    assert claims is not None
    assert claims["sub"] == 7 and claims["org"] == 3
    assert claims["aud"] == ours and claims["scope"] == "treg:call"


async def test_a_session_cookie_is_not_an_access_token():
    """treg mints session cookies and identity tokens with the same HMAC construction. Without a type
    marker one class of credential would silently validate as another — a browser session becoming an
    MCP grant, which nobody consented to."""
    cookie = session.make(7)
    assert mcp._oauth_claims(cookie) is None
    assert mcp_oauth.read_access_token(cookie, expected_audience=mcp_oauth.mcp_resource_url()) is None


async def test_a_tampered_token_is_refused():
    ours = mcp_oauth.mcp_resource_url()
    tok = mcp_oauth.make_access_token(user_id=7, org_id=3, audience=ours)
    payload, sig = tok.split(".", 1)
    # same signature, different claims: the forgery a signature exists to stop
    forged = mcp_oauth.make_access_token(user_id=99, org_id=99, audience=ours).split(".", 1)[0]
    assert mcp_oauth.read_access_token(f"{forged}.{sig}", expected_audience=ours) is None


async def test_an_expired_token_is_refused():
    ours = mcp_oauth.mcp_resource_url()
    tok = mcp_oauth.make_access_token(user_id=7, org_id=3, audience=ours, ttl=-1)
    assert mcp_oauth.read_access_token(tok, expected_audience=ours) is None


@pytest.mark.parametrize("junk", ["", "not-a-token", "a.b", "....", "Bearer x"])
async def test_malformed_input_is_simply_not_a_token(junk):
    assert mcp_oauth.read_access_token(junk, expected_audience=mcp_oauth.mcp_resource_url()) is None


async def test_the_audience_check_cannot_be_skipped_by_accident():
    """`expected_audience` has no default, and an empty one refuses rather than matching everything.
    The single thing that must never happen quietly is not performing this check."""
    tok = mcp_oauth.make_access_token(user_id=7, org_id=3, audience="")
    assert mcp_oauth.read_access_token(tok, expected_audience="") is None
    with pytest.raises(TypeError):
        mcp_oauth.read_access_token(tok)  # type: ignore[call-arg]


# ---- PKCE ------------------------------------------------------------------------------------

async def test_pkce_accepts_the_right_verifier_and_nothing_else():
    import base64
    import hashlib

    verifier = "a-random-high-entropy-string-from-the-client"
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert mcp_oauth.verify_pkce(verifier, challenge)
    assert not mcp_oauth.verify_pkce("some-other-verifier", challenge)
    assert not mcp_oauth.verify_pkce("", challenge)
    assert not mcp_oauth.verify_pkce(verifier, "")


async def test_a_token_outlives_neither_its_ttl_nor_reason():
    """An hour is deliberate: short enough that a leaked access token expires on its own, long enough
    that a refresh is not happening on every call."""
    assert 300 <= mcp_oauth.ACCESS_TTL_SECONDS <= 24 * 3600
    tok = mcp_oauth.make_access_token(user_id=1, org_id=1,
                                      audience=mcp_oauth.mcp_resource_url())
    claims = mcp_oauth.read_access_token(tok, expected_audience=mcp_oauth.mcp_resource_url())
    assert claims and claims["exp"] <= int(time.time()) + mcp_oauth.ACCESS_TTL_SECONDS + 2


# ---- step 2: client registration — two doors in, one row shape out --------------------------

async def test_dynamic_registration_mints_a_client(clients):
    """RFC 7591, unauthenticated by design: registering grants nothing on its own, because every
    token still needs a human to approve at the consent screen."""
    r = await clients.post("/oauth/register", json={
        "client_name": "Claude Code",
        "redirect_uris": ["http://127.0.0.1:8976/callback"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["client_id"].startswith("treg-client-")
    assert body["redirect_uris"] == ["http://127.0.0.1:8976/callback"]
    assert body["token_endpoint_auth_method"] == "none"   # public client + PKCE


async def test_registration_requires_a_usable_redirect_uri(clients):
    """An authorization code is delivered to the redirect URI. A client with none, or with one an
    eavesdropper could read, has nowhere safe to receive it."""
    for uris in ([], ["http://evil.example.com/cb"], ["ftp://x/cb"], ["https://a.test/cb#frag"]):
        r = await clients.post("/oauth/register",
                               json={"client_name": "x", "redirect_uris": uris})
        assert r.status_code == 400, f"{uris} should be refused, got {r.status_code}"
        assert r.json()["error"] == "invalid_redirect_uri"


async def test_loopback_http_is_allowed_because_a_CLI_cannot_hold_a_certificate(clients):
    for uri in ("http://127.0.0.1:1234/cb", "http://localhost:9999/cb"):
        r = await clients.post("/oauth/register", json={"client_name": "cli", "redirect_uris": [uri]})
        assert r.status_code == 201, uri


async def test_redirect_matching_is_EXACT_not_prefix():
    """The classic defeat of this check. `https://good.test/cb.evil` starts with the registered URI,
    and an open redirect under a registered host turns one sloppy page into stolen codes."""
    from treg.mcp_oauth import redirect_uri_allowed

    class C:
        redirect_uris = ["https://good.test/cb"]

    assert redirect_uri_allowed(C(), "https://good.test/cb")
    for evil in ("https://good.test/cb.evil", "https://good.test/cb/../x", "https://good.test/CB",
                 "https://good.test.evil/cb", ""):
        assert not redirect_uri_allowed(C(), evil), evil


# ---- the CIMD fetch: a URL the caller chose, so it must be fenced ---------------------------

@pytest.mark.parametrize("url", [
    "http://example.com/cimd.json",          # not https
    "https://127.0.0.1/cimd.json",           # loopback
    "https://169.254.169.254/latest/meta",   # cloud metadata — the classic SSRF target
    "https://10.0.0.5/cimd.json",            # private
    "https://localhost/cimd.json",
    "not-a-url",
])
async def test_cimd_refuses_unsafe_urls(url):
    """`client_id` is a URL our SERVER fetches, which makes it a request-forgery primitive unless
    fenced. Reuses the same guard the webhook and tool base_url paths already use."""
    from treg.mcp_oauth import fetch_client_id_metadata

    assert await fetch_client_id_metadata(url) is None


async def test_cimd_document_must_claim_its_own_url(monkeypatch):
    """Without this, a document hosted anywhere could assert somebody else's client_id and inherit
    the consent users granted them."""
    import httpx

    from treg import health, mcp_oauth

    monkeypatch.setattr(health, "safe_webhook_url", lambda u: True)
    monkeypatch.setattr(health, "host_is_public", lambda h: True)

    async def fake_get(self, url, **kw):
        return httpx.Response(200, json={"client_id": "https://someone-else.test/cimd.json",
                                         "client_name": "impostor",
                                         "redirect_uris": ["https://impostor.test/cb"]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert await mcp_oauth.fetch_client_id_metadata("https://real.test/cimd.json") is None


async def test_cimd_accepts_a_well_formed_document(monkeypatch):
    """Not vacuous: the refusals above only mean something if a good document DOES load."""
    import httpx

    from treg import health, mcp_oauth

    monkeypatch.setattr(health, "safe_webhook_url", lambda u: True)
    monkeypatch.setattr(health, "host_is_public", lambda h: True)

    async def fake_get(self, url, **kw):
        return httpx.Response(200, json={"client_id": url, "client_name": "ChatGPT",
                                         "redirect_uris": ["https://chatgpt.com/connector/oauth/x",
                                                           "http://evil.test/cb"]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    doc = await mcp_oauth.fetch_client_id_metadata("https://chatgpt.com/cimd.json")
    assert doc is not None
    assert doc["client_name"] == "ChatGPT"
    # the unsafe redirect in the document is dropped rather than accepted alongside the good one
    assert doc["redirect_uris"] == ["https://chatgpt.com/connector/oauth/x"]
