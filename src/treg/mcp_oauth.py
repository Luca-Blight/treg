"""treg as an OAuth **authorization server** — the metadata, and the tokens it will issue.

Everywhere else treg speaks OAuth as a *client*: `oauth.py` builds PKCE challenges to sign in with
GitHub and to connect a provider account. This is the other direction — the thing that mints tokens
rather than the thing that redeems them.

This module is deliberately the FIRST piece built, and on its own it issues nothing. It is the part
that **refuses**: the metadata that tells a client what we support, and the validation that rejects a
token which is expired, forged, or minted for somebody else's server. Building the refusal before
the issuance means there is never a window where we accept tokens we have not yet learned to check.

**The `aud` claim is the load-bearing one.** The MCP spec has the client send `resource=<the mcp
url>` on both the authorize and the token request, and the authorization server must copy it into the
token's audience. Without checking it, a token a user granted to *some other* MCP server would work
on ours — the user consented to that server, not to treg, and we would be spending their treg balance
on the strength of it. `read_access_token` therefore takes the expected audience as a REQUIRED
argument. There is no default and no "skip the check" path, because the one thing that must never
happen by accident is not checking.

Not ChatGPT-specific. The MCP authorization spec is the same for every compliant client, so this
serves Claude Code, Cursor and anything else that implements it. See `docs/MCP-OAUTH-PLAN.md`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urljoin

from .config import get_settings

# How long an access token lives. Short, because a refresh token will exist to renew it and a leaked
# access token is only as dangerous as its remaining life.
ACCESS_TTL_SECONDS = 3600

# Marks a token as an MCP access token and nothing else. treg already mints session cookies and
# identity tokens with the same HMAC construction, so without a type marker a session cookie would
# validate here (and vice versa) — one class of token would silently become another, which is a
# privilege escalation waiting for someone to notice it before we do.
_TOKEN_TYPE = "treg-mcp-at"


def _key() -> bytes:
    # Same secret as browser sessions, deliberately: one secret to rotate, and rotating it should
    # invalidate every credential at once rather than leaving some class of token still valid.
    from . import session

    return session._key()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mcp_resource_url() -> str:
    """The canonical identifier for the thing being protected — our MCP endpoint.

    Must match what clients send as `resource`, byte for byte, because that string is what ends up in
    the token's audience and what we compare against. The trailing slash is part of it: the transport
    is mounted at `/mcp` and served at `/mcp/`, and a client that resolves the metadata will use the
    form we publish here.
    """
    return urljoin(get_settings().public_url.rstrip("/") + "/", "mcp/")


def protected_resource_metadata() -> dict:
    """`/.well-known/oauth-protected-resource` — "who guards this, and what may you ask for?"."""
    base = get_settings().public_url.rstrip("/")
    return {
        "resource": mcp_resource_url(),
        "authorization_servers": [base],
        "scopes_supported": ["treg:catalog", "treg:call", "treg:read"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{base}/llms.txt",
    }


def authorization_server_metadata() -> dict:
    """`/.well-known/oauth-authorization-server` — how to get a token.

    `code` alone: no implicit grant, which OAuth 2.1 drops for good reason. `S256` alone: a client
    offering `plain` is a client whose codes can be replayed. `none` for client auth is correct for
    public clients that use PKCE, which is what every MCP client is.
    """
    base = get_settings().public_url.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["treg:catalog", "treg:call", "treg:read"],
        # ChatGPT sends an HTTPS metadata URL as its client_id instead of registering. Advertising
        # this does not replace `registration_endpoint` above — Claude Code and most other clients
        # register dynamically, and supporting only one of the two would lock the others out.
        "client_id_metadata_document_supported": True,
        "service_documentation": f"{base}/llms.txt",
    }


def make_access_token(*, user_id: int, org_id: int, audience: str, scope: str = "",
                      ttl: int = ACCESS_TTL_SECONDS, token_version: int = 0) -> str:
    """Mint an access token bound to a user, ONE team, and one audience.

    `org_id` is on the token rather than resolved per call on purpose. A person can belong to several
    teams, and the question "which team is this spending from?" must be answered once, by a human, at
    the consent screen — not guessed at request time. It is also what makes a granted token
    revocable in a way the user understands: they granted this client access to that team.
    """
    raw = json.dumps({
        "typ": _TOKEN_TYPE,
        "sub": int(user_id),
        "org": int(org_id),
        "aud": audience,
        "scope": scope,
        "tv": int(token_version),
        "exp": int(time.time()) + int(ttl),
    }, separators=(",", ":")).encode()
    sig = hmac.new(_key(), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(sig)}"


def read_access_token(token: str, *, expected_audience: str) -> dict | None:
    """Validate an access token and return its claims, or None.

    `expected_audience` is required and has no default. A token carries the resource its user
    consented to; accepting one addressed elsewhere would let a grant made to another MCP server
    spend a treg balance. Every other check here is ordinary — signature, type, expiry — but this is
    the one that is specific to being a resource server rather than a login system.
    """
    if not token or "." not in token or not expected_audience:
        return None
    try:
        payload, signature = token.split(".", 1)
        raw = _unb64(payload)
        expected_sig = hmac.new(_key(), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(signature), expected_sig):
            return None
        data = json.loads(raw)
        if data.get("typ") != _TOKEN_TYPE:
            return None                      # a session cookie is not an access token
        if data.get("aud") != expected_audience:
            return None                      # minted for a different resource — not ours to honour
        if int(data.get("exp", 0)) < time.time():
            return None
        return {"sub": int(data["sub"]), "org": int(data["org"]), "aud": data["aud"],
                "scope": data.get("scope", ""), "tv": int(data.get("tv", 0)),
                "exp": int(data["exp"])}
    except Exception:  # noqa: BLE001 — a malformed token is simply not a token
        return None


def verify_pkce(verifier: str, challenge: str) -> bool:
    """`S256` only. `plain` is in the spec and is worthless: it makes the challenge equal to the
    secret, so anyone who intercepts the authorization request can redeem the code."""
    if not verifier or not challenge:
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return hmac.compare_digest(_b64(digest), challenge)
