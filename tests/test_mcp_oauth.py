"""treg as an OAuth authorization server — the metadata, the refusals, and client registration.

Nothing issues tokens yet. This file covers the half that says NO, deliberately built first: there is
never a window where the server accepts tokens it has not learned to check.

The assertion that matters most is the audience one. Everything else here — signature, expiry, type —
is ordinary token hygiene that any login system needs. `aud` is the one that exists because we are a
RESOURCE server: a user grants a client access to a named resource, and a token addressed to some
other MCP server must not spend a treg balance just because it happens to be validly signed by us.
"""

from __future__ import annotations

import json
import time

import pytest

from treg import mcp, mcp_oauth, session

# The MCP transport helpers live with the MCP tests; a token is only interesting here because it can
# drive a tool, so reuse them rather than keeping a second copy that can drift.
from test_mcp import _call_tool, mcp_session

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


async def test_loopback_redirect_allows_ANY_port_but_not_other_drift():
    """RFC 8252 §7.3: a native/CLI client (Claude Code, Codex, Cursor) registers a PORTLESS loopback
    URI and sends the ephemeral port it actually bound at authorize time. The AS MUST allow the port
    to vary — treg's pure exact-match rejected `http://localhost:3118/callback` against a registered
    `http://localhost/callback` and broke every native client. Port varies; scheme, host and path do
    NOT, and the loopback exception must not leak to public hosts."""
    from treg.mcp_oauth import redirect_uri_allowed

    class C:  # exactly what Claude Code's client-id metadata document registers
        redirect_uris = ["http://localhost/callback", "http://127.0.0.1/callback"]

    # the real failing case + variants that MUST now pass
    assert redirect_uri_allowed(C(), "http://localhost:3118/callback")
    assert redirect_uri_allowed(C(), "http://127.0.0.1:52713/callback")
    assert redirect_uri_allowed(C(), "http://localhost/callback")        # portless still fine
    # but the exception is loopback-only and path-exact — these MUST still be refused
    for bad in ("http://localhost:3118/evil",              # wrong path
                "http://localhost.evil/callback",          # not actually loopback
                "http://evil.test:3118/callback",          # public host — no port flexibility
                "https://localhost:3118/callback",         # scheme drift (registered is http)
                "http://[::1]:9/other"):                   # loopback but wrong path
        assert not redirect_uri_allowed(C(), bad), bad


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


# ---- step 3: the authorization code flow ----------------------------------------------------

def _pkce():
    import base64
    import hashlib
    import secrets

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def _register(clients, redirect="https://client.test/cb"):
    r = await clients.post("/oauth/register",
                           json={"client_name": "Test Client", "redirect_uris": [redirect]})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


async def _signed_in(clients, email="oauth-user@superdesign.dev"):
    """A browser session plus the org it belongs to. The consent step is a HUMAN action, so it needs
    a session cookie rather than a token."""
    r = await clients.post("/users", json={"email": email})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    prev = clients.headers.get("X-Treg-Token")
    clients.headers["X-Treg-Token"] = token
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    me = (await clients.get("/auth/me")).json()
    if prev:
        clients.headers["X-Treg-Token"] = prev
    from sqlmodel import select

    from treg import session as _sess
    from treg.db import session_maker
    from treg.models import User

    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == me["email"]))).scalar_one()
        cookie = _sess.make(user.id, token_version=user.token_version)
    clients.cookies.set("treg_session", cookie)   # on the client: httpx deprecates per-request
    return cookie, org_id


async def test_the_whole_flow_end_to_end(clients):
    """Register, authorize, approve, exchange — and the token that comes out is one our MCP server
    accepts, for the team the human chose."""
    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients)
    verifier, challenge = _pkce()
    params = {"client_id": client_id, "redirect_uri": "https://client.test/cb",
              "response_type": "code", "code_challenge": challenge,
              "code_challenge_method": "S256", "state": "xyz",
              "resource": mcp_oauth.mcp_resource_url(), "scope": "treg:call"}

    shown = await clients.get("/oauth/authorize", params=params,
                              headers={"Accept": "application/json"})
    assert shown.status_code == 200, shown.text
    assert shown.json()["client"]["name"] == "Test Client"
    assert any(t["org_id"] == org_id for t in shown.json()["teams"]), "the team picker must offer it"

    approved = await clients.post("/oauth/authorize", data={**params, "org_id": org_id}, follow_redirects=False)
    assert approved.status_code == 302
    loc = approved.headers["location"]
    assert loc.startswith("https://client.test/cb?") and "state=xyz" in loc
    code = loc.split("code=")[1].split("&")[0]

    tok = await clients.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "https://client.test/cb", "client_id": client_id,
        "code_verifier": verifier, "resource": mcp_oauth.mcp_resource_url()})
    assert tok.status_code == 200, tok.text
    access = tok.json()["access_token"]
    assert tok.json()["token_type"] == "Bearer"

    claims = mcp._oauth_claims(access)
    assert claims is not None, "the MCP server must accept what we just issued"
    assert claims["org"] == org_id, "the token spends from the team the human picked"


async def test_a_code_can_be_redeemed_only_ONCE(clients):
    """The row is deleted on redemption rather than flagged, so a replay finds nothing. A used code
    that still exists is a race waiting for two redemptions to read it before either writes."""
    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients, "once@superdesign.dev")
    verifier, challenge = _pkce()
    params = {"client_id": client_id, "redirect_uri": "https://client.test/cb",
              "response_type": "code", "code_challenge": challenge,
              "code_challenge_method": "S256", "resource": mcp_oauth.mcp_resource_url()}
    r = await clients.post("/oauth/authorize", data={**params, "org_id": org_id}, follow_redirects=False)
    code = r.headers["location"].split("code=")[1].split("&")[0]
    body = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": "https://client.test/cb", "client_id": client_id,
            "code_verifier": verifier}
    assert (await clients.post("/oauth/token", data=body)).status_code == 200
    second = await clients.post("/oauth/token", data=body)
    assert second.status_code == 400 and second.json()["error"] == "invalid_grant"


async def test_the_WRONG_verifier_cannot_redeem_a_stolen_code(clients):
    """The whole point of PKCE: a code intercepted in the browser redirect is worthless without the
    verifier, which never left the client."""
    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients, "pkce@superdesign.dev")
    _, challenge = _pkce()
    other_verifier, _ = _pkce()
    r = await clients.post("/oauth/authorize", data={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "org_id": org_id,
        "resource": mcp_oauth.mcp_resource_url()}, follow_redirects=False)
    code = r.headers["location"].split("code=")[1].split("&")[0]
    tok = await clients.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "https://client.test/cb", "client_id": client_id,
        "code_verifier": other_verifier})
    assert tok.status_code == 400 and tok.json()["error"] == "invalid_grant"


async def test_an_unregistered_redirect_is_refused_WITHOUT_redirecting(clients):
    """Bouncing an error to an unvalidated URI would be an open redirect, and would hand `state` to
    whoever asked for it. So this is a flat 400, not a 302."""
    client_id = await _register(clients)
    r = await clients.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": "https://attacker.test/steal",
        "response_type": "code", "code_challenge": "x", "code_challenge_method": "S256"},
        follow_redirects=False)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


async def test_pkce_is_mandatory(clients):
    """A client that omits the challenge, or offers `plain`, is refused — otherwise a downgrade is
    available to anyone who asks for it."""
    client_id = await _register(clients)
    for extra in ({}, {"code_challenge": "abc", "code_challenge_method": "plain"}):
        r = await clients.get("/oauth/authorize", params={
            "client_id": client_id, "redirect_uri": "https://client.test/cb",
            "response_type": "code", **extra}, follow_redirects=False)
        assert r.status_code == 302 and "error=invalid_request" in r.headers["location"]


async def test_you_cannot_approve_for_a_team_you_are_not_in(clients):
    """`org_id` arrives from the browser, so it is client-supplied. Trusting it would let anyone
    grant a client access to any team by editing one form field."""
    client_id = await _register(clients)
    cookie, _ = await _signed_in(clients, "outsider@superdesign.dev")
    other = await clients.post("/users", json={"email": "stranger@superdesign.dev"})
    prev = clients.headers.get("X-Treg-Token")
    clients.headers["X-Treg-Token"] = other.json()["token"]
    foreign_org = (await clients.get("/orgs")).json()[0]["org_id"]
    if prev:
        clients.headers["X-Treg-Token"] = prev
    _, challenge = _pkce()
    r = await clients.post("/oauth/authorize", data={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "org_id": foreign_org}, follow_redirects=False)
    assert r.status_code == 302 and "error=access_denied" in r.headers["location"]


async def test_a_GET_never_grants_anything(clients):
    """Approval is a POST. A GET that granted access could be triggered by any page able to make the
    browser navigate — even with `org_id` supplied, as here."""
    from sqlmodel import select

    from treg.db import session_maker
    from treg.models import OAuthCode

    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients, "getonly@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "org_id": org_id},
        follow_redirects=False)
    assert r.status_code == 200, "it renders the question"
    assert "location" not in r.headers, "and never redirects back with a code"
    async with session_maker() as db:
        codes = (await db.execute(select(OAuthCode).where(
            OAuthCode.client_id == client_id))).scalars().all()
    assert codes == [], "a GET must not have minted anything"


# ---- step 4: the consent screen — the only place a human sees what they grant ----------------

async def test_the_consent_page_says_what_it_costs_in_WORDS(clients):
    """A screen that lists `treg:call` and calls that informed consent is a formality. The two things
    that actually cost money or expose data have to be legible: this client can spend the team's
    balance, and can use keys the team registered."""
    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients, "consent@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256"})
    assert r.status_code == 200
    page = r.text
    assert "text/html" in r.headers["content-type"]
    assert "Test Client" in page, "the human must see WHICH application is asking"
    assert "consent@superdesign.dev" in page, "and which account they are granting from"
    assert "spends the team's balance" in page
    assert "without seeing them" in page, "keys are used, never revealed — say so"
    assert 'name="org_id"' in page, "the team picker belongs here"


async def test_the_page_warns_when_a_client_registered_ITSELF(clients):
    """Dynamic registration is open by design, so anyone can appear here with any name. The user is
    the only one who can tell whether they started this, and they can only judge if we say so."""
    client_id = await _register(clients)
    cookie, _ = await _signed_in(clients, "warned@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256"})
    assert "registered itself" in r.text
    assert "only continue if you recognise it" in r.text


async def test_json_is_still_available_for_a_non_browser_client(clients):
    """The HTML is for humans; a client that asks for JSON gets the same facts without scraping."""
    client_id = await _register(clients)
    cookie, _ = await _signed_in(clients, "jsonclient@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256"},
        headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json()["client"]["name"] == "Test Client"


async def test_CANCEL_tells_the_client_no_rather_than_hanging(clients):
    """Declining is a real answer. A client left waiting on a redirect that never comes is a worse
    outcome than a clean refusal."""
    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients, "declines@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.post("/oauth/authorize", data={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "org_id": org_id,
        "state": "s1", "decision": "deny"}, follow_redirects=False)
    assert r.status_code == 302
    assert "error=access_denied" in r.headers["location"] and "state=s1" in r.headers["location"]
    assert "code=" not in r.headers["location"]


async def test_a_CROSS_ORIGIN_submission_is_refused(clients):
    """The consent form is the security boundary. Without this, a page anywhere could auto-submit and
    grant itself a team's balance — the user is signed in, so their cookie rides along."""
    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients, "csrf@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.post("/oauth/authorize", data={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "org_id": org_id,
        "decision": "allow"},
        headers={"Origin": "https://attacker.test"}, follow_redirects=False)
    assert r.status_code == 403


async def test_the_consent_page_cannot_be_framed(clients):
    """Clickjacking: an invisible frame over a decoy page turns "Allow" into a click the user thought
    was something else. treg sets this globally; asserting it here is what notices if that changes."""
    client_id = await _register(clients)
    cookie, _ = await _signed_in(clients, "framed@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256"})
    assert r.headers.get("X-Frame-Options") == "DENY"


# ---- the token has to WORK, not merely validate ---------------------------------------------

async def _grant(clients, email):
    """Run the whole flow and return the access token, plus the team it was granted for."""
    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients, email)
    verifier, challenge = _pkce()
    params = {"client_id": client_id, "redirect_uri": "https://client.test/cb",
              "response_type": "code", "code_challenge": challenge,
              "code_challenge_method": "S256", "resource": mcp_oauth.mcp_resource_url()}
    r = await clients.post("/oauth/authorize", data={**params, "org_id": org_id, "decision": "allow"},
                           follow_redirects=False)
    code = r.headers["location"].split("code=")[1].split("&")[0]
    tok = await clients.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "https://client.test/cb", "client_id": client_id,
        "code_verifier": verifier})
    assert tok.status_code == 200, tok.text
    return tok.json()["access_token"], org_id


async def test_an_oauth_token_actually_CALLS_the_tools(clients):
    """The gap running it found and the unit tests missed: `_oauth_claims` validated a token
    perfectly while every tool still forwarded the raw bearer to the internal API, which has never
    heard of an OAuth token — so all three authenticated tools answered "not signed in".

    Validating a credential and being able to USE it are different claims, and only this one matters
    to a user.
    """
    access, org_id = await _grant(clients, "usable@superdesign.dev")
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=access)
    assert "balance_usd" in out, out
    assert "not signed in" not in json.dumps(out)


async def test_the_token_spends_from_the_team_ON_THE_GRANT(clients):
    """A person in several teams answered "which one" at the consent screen. That answer travels on
    the token, and nothing downstream may re-derive it — re-deriving is how the wrong team's balance
    gets spent, and `balance` used to refuse and ask precisely because it had no answer."""
    access, granted_org = await _grant(clients, "multi@superdesign.dev")
    # a second team for the same person, deliberately created AFTER the grant
    made = await clients.post("/orgs", json={"name": "second-team-after-grant"})
    assert made.status_code == 200, made.text

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=access)
    assert "balance_usd" in out, out
    teams = (await clients.get("/orgs")).json()
    granted_slug = next(t["slug"] for t in teams if t["org_id"] == granted_org)
    assert out["team"] == granted_slug, "the grant's team, not a freshly-guessed one"


async def test_revoking_your_tokens_kills_an_existing_grant(clients):
    """`token_version` is treg's kill switch for a leaked credential. An OAuth grant must honour it
    too, or the one button a user has for "make it stop" would quietly miss this door."""
    access, _ = await _grant(clients, "revoker@superdesign.dev")
    async with mcp_session(clients) as c:
        before = await _call_tool(c, "balance", {}, token=access)
    assert "balance_usd" in before

    from sqlmodel import select

    from treg.db import session_maker
    from treg.models import User
    async with session_maker() as db:
        user = (await db.execute(
            select(User).where(User.email == "revoker@superdesign.dev"))).scalar_one()
        user.token_version += 1
        db.add(user)
        await db.commit()

    async with mcp_session(clients) as c:
        after = await _call_tool(c, "balance", {}, token=access)
    assert "balance_usd" not in after, "a revoked user's grant must stop working"


# ---- step 5: refresh tokens, rotation, and catching a replay --------------------------------

async def _grant_full(clients, email):
    """The whole flow, returning the token response so the refresh token is visible."""
    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients, email)
    verifier, challenge = _pkce()
    params = {"client_id": client_id, "redirect_uri": "https://client.test/cb",
              "response_type": "code", "code_challenge": challenge,
              "code_challenge_method": "S256", "resource": mcp_oauth.mcp_resource_url()}
    r = await clients.post("/oauth/authorize", data={**params, "org_id": org_id, "decision": "allow"},
                           follow_redirects=False)
    code = r.headers["location"].split("code=")[1].split("&")[0]
    tok = await clients.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "https://client.test/cb", "client_id": client_id,
        "code_verifier": verifier})
    assert tok.status_code == 200, tok.text
    return tok.json(), client_id, org_id


async def test_a_grant_comes_with_a_refresh_token(clients):
    """An access token lasts an hour. Without a refresh, every connector breaks hourly and the user
    is the one who has to notice."""
    body, _, _ = await _grant_full(clients, "refresher@superdesign.dev")
    assert body["refresh_token"]
    assert body["expires_in"] == mcp_oauth.ACCESS_TTL_SECONDS


async def test_refreshing_ROTATES_the_token_and_the_new_one_works(clients):
    """The old token is spent, the new one carries on, and the access token that comes out still
    drives the tools — a refresh that returned an unusable token would fail silently an hour later."""
    body, client_id, _ = await _grant_full(clients, "rotate@superdesign.dev")
    first = body["refresh_token"]

    r = await clients.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": first, "client_id": client_id})
    assert r.status_code == 200, r.text
    second = r.json()["refresh_token"]
    assert second and second != first, "a refresh must MINT a replacement, not hand back the same one"

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=r.json()["access_token"])
    assert "balance_usd" in out, out


async def test_replaying_a_SPENT_refresh_token_kills_the_whole_family(clients):
    """The reason the retired row is kept rather than deleted. A spent token being presented again
    means either a client retried after a dropped response or somebody else has a copy — and those
    are indistinguishable from here. Assume the worse one: the cost of being wrong is a sign-in, and
    the cost of the other mistake is somebody's balance."""
    body, client_id, _ = await _grant_full(clients, "replay@superdesign.dev")
    first = body["refresh_token"]

    ok = await clients.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": first, "client_id": client_id})
    assert ok.status_code == 200
    second = ok.json()["refresh_token"]

    replay = await clients.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": first, "client_id": client_id})
    assert replay.status_code == 400
    assert "already used" in replay.json()["error_description"]

    # and the descendant is dead too — containment is the point, not just refusing the replay
    after = await clients.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": second, "client_id": client_id})
    assert after.status_code == 400, "the whole family must be revoked, not only the replayed token"


async def test_a_refresh_cannot_quietly_change_teams(clients):
    """The team was chosen by a human at consent. A refresh renews that grant; it is not a second
    chance to pick, and nothing in the request may influence it."""
    body, client_id, org_id = await _grant_full(clients, "steady@superdesign.dev")
    made = await clients.post("/orgs", json={"name": "team-created-after"})
    assert made.status_code == 200

    r = await clients.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": body["refresh_token"],
        "client_id": client_id})
    assert r.status_code == 200
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=r.json()["access_token"])
    teams = (await clients.get("/orgs")).json()
    granted_slug = next(t["slug"] for t in teams if t["org_id"] == org_id)
    assert out["team"] == granted_slug


async def test_a_refresh_token_is_bound_to_its_client(clients):
    """Otherwise one client's stolen refresh token is every client's."""
    body, _, _ = await _grant_full(clients, "bound@superdesign.dev")
    other = await _register(clients, "https://other.test/cb")
    r = await clients.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": body["refresh_token"], "client_id": other})
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


async def test_revoking_ends_the_family_and_never_leaks_whether_a_token_existed(clients):
    """RFC 7009: always 200. Distinguishing a known token from an unknown one would turn this into
    an oracle for guessing valid ones."""
    body, client_id, _ = await _grant_full(clients, "revoke@superdesign.dev")
    assert (await clients.post("/oauth/revoke", data={"token": "never-existed"})).status_code == 200
    assert (await clients.post("/oauth/revoke",
                               data={"token": body["refresh_token"]})).status_code == 200
    dead = await clients.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": body["refresh_token"],
        "client_id": client_id})
    assert dead.status_code == 400


async def test_refresh_tokens_are_stored_HASHED(clients):
    """A database copy is a database leak, and the refresh token is the long-lived half — the one
    worth stealing."""
    from sqlmodel import select

    from treg.db import session_maker
    from treg.models import OAuthRefresh

    body, _, _ = await _grant_full(clients, "hashed@superdesign.dev")
    async with session_maker() as db:
        rows = (await db.execute(select(OAuthRefresh))).scalars().all()
    assert rows, "expected a stored refresh token"
    assert all(body["refresh_token"] not in (r.token_hash or "") for r in rows)
    assert all(len(r.token_hash) == 64 for r in rows), "sha256 hex, as everywhere else in treg"


# ---- step 6: what an INDEPENDENT client found ------------------------------------------------

async def test_a_resource_we_do_not_serve_is_refused_UP_FRONT(clients):
    """Found by driving the flow with the MCP SDK's own client instead of my curl.

    It sent the URL it had dialled (`http://127.0.0.1:18790/mcp/`) rather than the canonical
    identifier our metadata declares, which is a reasonable thing for a client to do. treg accepted
    it and minted a token that was valid, well-formed and silently useless — the audience did not
    match, so the first tool call answered "not signed in" and pointed the reader at authentication
    when the real problem was the resource.

    Refusing at authorize time turns a confusing failure an hour downstream into a clear one
    immediately, and names the fix.
    """
    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients, "wrongres@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": "https://somewhere-else.test/mcp/"}, follow_redirects=False)
    assert r.status_code == 302
    assert "error=invalid_target" in r.headers["location"]
    assert "well-known" in r.headers["location"], "and it must say where the right value lives"


async def test_omitting_the_resource_is_fine(clients):
    """A client that sends no `resource` gets our canonical one — which is what it would have
    discovered anyway, so refusing would be pedantry rather than safety."""
    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients, "nores@superdesign.dev")
    verifier, challenge = _pkce()
    r = await clients.post("/oauth/authorize", data={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256", "org_id": org_id,
        "decision": "allow"}, follow_redirects=False)
    assert r.status_code == 302 and "code=" in r.headers["location"]
    code = r.headers["location"].split("code=")[1].split("&")[0]
    tok = await clients.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": "https://client.test/cb", "client_id": client_id,
        "code_verifier": verifier})
    assert tok.status_code == 200
    async with mcp_session(clients) as c:
        out = await _call_tool(c, "balance", {}, token=tok.json()["access_token"])
    assert "balance_usd" in out, "the default audience must be the one the MCP server accepts"


async def test_a_null_origin_from_a_redirect_chain_is_not_treated_as_cross_site(clients):
    """The intermittent failure Unclecode hit: approve worked on one attempt and was refused on
    another. `Origin: null` is a browser reporting an OPAQUE origin, which happens after certain
    redirect chains — a consent page reached by way of a sign-in bounce through GitHub, say. It is
    not evidence of a cross-site request.

    `Sec-Fetch-Site` is the corroboration that makes accepting it safe: the browser sets it and script
    cannot, so another site cannot forge `same-origin`.
    """
    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients, "nullorigin@superdesign.dev")
    _, challenge = _pkce()
    body = {"client_id": client_id, "redirect_uri": "https://client.test/cb",
            "response_type": "code", "code_challenge": challenge,
            "code_challenge_method": "S256", "org_id": org_id, "decision": "allow"}

    ok = await clients.post("/oauth/authorize", data=body, follow_redirects=False,
                            headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 302 and "code=" in ok.headers["location"]

    # ...but `null` WITHOUT that corroboration is still refused: the guard is narrowed, not removed.
    nope = await clients.post("/oauth/authorize", data=body, follow_redirects=False,
                              headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"})
    assert nope.status_code == 403
    assert "Sec-Fetch-Site: cross-site" in nope.json()["detail"], "the refusal must say what it saw"


# ---- signing in mid-authorization must RETURN you to the authorization ----------------------

async def test_a_signed_out_user_is_returned_to_the_consent_screen(clients):
    """The bug ChatGPT found on its first real connect. A signed-out user clicking "Sign in with
    treg" landed on the dashboard and the authorization was silently dropped.

    The cause was a `?next=` query I invented and nothing implemented, so the sign-in doors simply
    ignored it. The destination is now parked in a cookie and resumed at the dashboard, which is the
    one point every browser door ends at.
    """
    client_id = await _register(clients)
    _, challenge = _pkce()
    params = {"client_id": client_id, "redirect_uri": "https://client.test/cb",
              "response_type": "code", "code_challenge": challenge,
              "code_challenge_method": "S256"}

    clients.cookies.clear()
    sent_away = await clients.get("/oauth/authorize", params=params, follow_redirects=False)
    assert sent_away.status_code == 302
    parked = (sent_away.cookies.get("treg_oauth_return") or "").strip('"')
    assert parked.startswith("/oauth/authorize?"), f"nothing was parked: {parked!r}"
    assert params["client_id"] in parked, "the parked destination must be THIS request"

    # now sign in and land on the dashboard, as every browser door does
    cookie, _ = await _signed_in(clients, "returner@superdesign.dev")
    clients.cookies.set("treg_oauth_return", parked)
    back = await clients.get("/app", follow_redirects=False)
    assert back.status_code == 302, "the dashboard must resume the parked authorization"
    assert back.headers["location"].startswith("/oauth/authorize?")


async def test_the_parked_destination_cannot_be_used_as_an_open_redirect(clients):
    """The cookie is only honoured for /oauth/authorize. Accepting any path would make it a general
    "send me anywhere after login" primitive — a phishing aid rather than a feature."""
    cookie, _ = await _signed_in(clients, "openredir@superdesign.dev")
    for evil in ("https://attacker.test/", "//attacker.test/", "/app/../oauth/authorize?x=1",
                 "/anything-else"):
        clients.cookies.set("treg_oauth_return", evil)
        r = await clients.get("/app", follow_redirects=False)
        assert r.status_code == 200, f"{evil!r} must NOT be honoured, got {r.status_code}"
    clients.cookies.delete("treg_oauth_return")


async def test_a_signed_out_visitor_is_not_bounced_in_a_loop(clients):
    """Resuming only once signed in. Otherwise the dashboard would send them to /oauth/authorize,
    which would send them back here, forever."""
    clients.cookies.clear()
    clients.cookies.set("treg_oauth_return", "/oauth/authorize?client_id=x")
    r = await clients.get("/app", follow_redirects=False)
    assert r.status_code == 200


async def test_the_team_picker_shows_which_team_can_PAY(clients):
    """Choosing a team here decides which balance the client spends for the life of the grant, so
    "which of these can actually pay?" has to be visible at the moment of choosing.

    Unclecode picked a $0.00 team on the first real ChatGPT connect; the call was refused and nothing
    on the page could have told him. A team with no balance is still a legitimate choice — a team's
    OWN keys are never metered — so it is LABELLED rather than hidden."""
    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients, "picker@superdesign.dev")
    _, challenge = _pkce()
    r = await clients.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256"})
    assert r.status_code == 200
    page = r.text
    # a new team carries the $1.00 grant, so the amount must be on the option
    assert "$1.00" in page, f"the balance is not shown in the picker: {page[page.find('org_id'):][:300]}"

    # and the JSON view carries it too, for a client that renders its own picker
    j = await clients.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256"},
        headers={"Accept": "application/json"})
    assert any("balance_usd" in t for t in j.json()["teams"])


async def test_a_team_with_no_balance_is_labelled_not_hidden(clients):
    """Removing the option would be wrong: work that uses the team's OWN registered keys is never
    metered, so an empty team is a perfectly good choice for it."""
    from sqlmodel import select

    from treg.db import session_maker
    from treg.models import Org

    client_id = await _register(clients)
    cookie, org_id = await _signed_in(clients, "broke@superdesign.dev")
    async with session_maker() as db:
        org = (await db.execute(select(Org).where(Org.id == org_id))).scalar_one()
        org.balance_micro = 0
        db.add(org)
        await db.commit()

    _, challenge = _pkce()
    page = (await clients.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": "https://client.test/cb", "response_type": "code",
        "code_challenge": challenge, "code_challenge_method": "S256"})).text
    assert "no balance" in page
    assert f'value="{org_id}"' in page, "the team must still be selectable"
