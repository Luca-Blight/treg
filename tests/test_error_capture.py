"""Evidence kept for a FAILED platform-tier call: what the caller sent, and what the provider said.

Until this existed a failure recorded a status code and nothing else, so it could not be explained
afterwards — the provider's message was never stored and the caller's parameters survived only inside
`params_hash`, which is one-way. In one real week `tikhub.x.twitter-web-fetch-search-timeline`
returned 80 identical 400s across 6 orgs and none of them could be diagnosed.

Three properties are load-bearing here, and each has its own test below:

1. It is PLATFORM-ONLY. A team calling on its own key is billed by the provider; keeping their
   traffic would help nobody and is the line `IdempotentCall.response_body` already draws.
2. It never retains treg's own credential. That key is shared across every tenant, so a leak is not
   one customer's problem — and providers routinely quote the offending request back in a 400/401.
3. It captures BOTH failure shapes: the provider answering badly, and treg failing to reach it.
"""

from __future__ import annotations

import gzip
import json
from datetime import timedelta

import pytest
from fastapi.responses import StreamingResponse
from httpx import AsyncClient
from sqlalchemy import select

from treg import api as A
from treg.config import get_settings
from treg.db import session_maker
from treg.models import CallRecord

EP = "tikhub.tiktok.video.comments"           # GET, tikhub — header-injected
EP_SPYFU = "spyfu.google.domain.overview"     # GET, spyfu — QUERY-injected (the harder leak case)
EP_POST = "dataforseo.web.page.audit"         # POST — the only shape whose params live in the body
# Keys chosen so ONLY the exact-substring mask can catch them. `_ARGV_SECRET_RE` matches known
# prefixes, JWTs, and any 24+ run of [A-Za-z0-9_-]; a short key containing a '.' matches none of
# those, because the dot breaks the word boundary. Verified by disabling
# `_platform_secret_renderings` and watching these tests fail — with a longer key they passed on the
# regex fallback alone and proved nothing about the defence they exist to cover.
PLATFORM_KEY = "tk.9f2a-Q1"
SPYFU_KEY = "sp.4b7c-Z8"
ADMIN_TOKEN = "ENV-ADMIN-SECRET"
ADMIN = {"X-Treg-Token": ADMIN_TOKEN}       # /admin/* authenticates with the env token, not a member


@pytest.fixture
def platform_on(monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", PLATFORM_KEY)
    monkeypatch.setenv("TREG_PLATFORM_KEY_SPYFU", SPYFU_KEY)
    monkeypatch.setenv("TREG_PLATFORM_KEY_DATAFORSEO", "PLATFORM-DFS-KEY-def456")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub,spyfu,dataforseo")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_relay(status_code: int, body: bytes = b"{}", *, headers: dict | None = None,
                raises: Exception | None = None):
    """A specific UPSTREAM outcome the echo app cannot produce — a provider 4xx with a chosen body."""
    async def _relay(request, upstream_url, tool, secrets, client, drop_params=None,
                     force_identity=False):
        if raises is not None:
            raise raises

        async def _stream():
            yield body

        return StreamingResponse(_stream(), status_code=status_code, headers=headers or {})

    return _relay


async def _row(clients: AsyncClient) -> dict:
    """The newest audit row, read straight from the table.

    Deliberately NOT via `GET /calls`: that endpoint does not expose these columns and must not (see
    the last test), so reading through it would make every assertion below vacuously pass.
    """
    from treg import audit
    await audit.drain()          # fire-and-forget writes must be flushed before reading
    async with session_maker() as db:
        row = (await db.execute(
            select(CallRecord).order_by(CallRecord.id.desc()).limit(1))).scalars().first()
    assert row is not None, "no audit row was written at all"
    return {c: getattr(row, c) for c in (
        "status_code", "tool_name", "credential_tier", "refused_by",
        "error_request", "error_response")}


# ---- the happy path stores nothing --------------------------------------------------------------
# Absence tests, so they pass trivially if capture were removed altogether. What they DO pin is the
# `status_code >= 400` gate — flip capture to unconditional and this one goes red (verified). The
# feature's presence is pinned by the positive tests above it.
async def test_a_successful_platform_call_stores_no_evidence(clients: AsyncClient, platform_on):
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    row = await _row(clients)
    assert row["credential_tier"] == "platform"
    assert row.get("error_request") is None
    assert row.get("error_response") is None


# ---- shape 1: the provider answered badly -------------------------------------------------------
async def test_a_provider_failure_keeps_its_message_and_the_caller_s_query(
        clients: AsyncClient, platform_on, monkeypatch):
    monkeypatch.setattr(A, "relay", _fake_relay(
        400, b'{"error":"aweme_id must be numeric","code":"E_BAD_PARAM"}'))
    r = await clients.get(f"/call/{EP}?aweme_id=not-a-number&count=5")
    assert r.status_code == 400
    row = await _row(clients)
    assert "aweme_id must be numeric" in row["error_response"]
    assert "aweme_id=not-a-number" in row["error_request"]
    assert "count=5" in row["error_request"]


async def test_a_post_body_is_captured_for_a_failed_platform_call(
        clients: AsyncClient, platform_on, monkeypatch):
    """The body is the only place a POST endpoint's parameters live — `path` never carries them."""
    monkeypatch.setattr(A, "relay", _fake_relay(422, b'{"detail":"target is required"}'))
    r = await clients.post(f"/call/{EP_POST}", json=[{"url": "https://example.com/coffee"}])
    assert r.status_code == 422, r.text
    row = await _row(clients)
    assert "target is required" in row["error_response"]
    assert "example.com/coffee" in row["error_request"], "the POST body is the only copy of these"


# ---- shape 2: treg never reached the provider ---------------------------------------------------
async def test_treg_s_own_502_is_explained_too(clients: AsyncClient, platform_on, monkeypatch):
    """The branch where `body` is UNBOUND. These are the failures a bare status explains least:
    upstream timeout, connection reset, failed injection, SSRF refusal."""
    import httpx
    monkeypatch.setattr(A, "relay", _fake_relay(
        200, raises=httpx.ConnectTimeout("timed out after 30s")))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 502
    row = await _row(clients)
    assert row["error_response"].startswith("treg: ")
    assert "upstream request failed" in row["error_response"]
    assert "aweme_id=7" in row["error_request"]


# ---- the tier gate ------------------------------------------------------------------------------
# The gate is STRUCTURAL: the capture code sits inside `if mk is not None and mk.metered:` in
# `call_tool`, so a non-platform call cannot reach it at all. The extra `mk.metered` check inside
# `_audit` is redundant insurance for a future call site, and is therefore NOT pinned by the test
# below — removing it leaves this test passing (verified). Kept anyway: it costs nothing, and this
# is the one column set where a mistake would retain a team's own traffic.
async def test_an_own_key_failure_stores_nothing(clients: AsyncClient, monkeypatch):
    """Tier 2: the org's own credential paid, so the traffic is theirs and treg keeps none of it."""
    await clients.post("/secrets", json={"name": "tikhub", "value": "THEIR-OWN-KEY"})
    monkeypatch.setattr(A, "relay", _fake_relay(400, b'{"error":"their own failure"}'))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 400
    row = await _row(clients)
    assert row["credential_tier"] == "credential"
    assert row.get("error_request") is None
    assert row.get("error_response") is None, "own-key traffic must never be retained"


async def test_a_treg_refusal_stores_nothing(clients: AsyncClient):
    """`refused_by` already explains these, and nothing went upstream to have an error body."""
    r = await clients.get(f"/call/{EP}?aweme_id=7")     # tier 4 off → tier-3 404
    assert r.status_code == 404
    row = await _row(clients)
    assert row["refused_by"] == "resolution"
    assert row.get("error_response") is None


# ---- the credential must never survive ----------------------------------------------------------
async def test_the_platform_key_never_reaches_the_columns_header_injected(
        clients: AsyncClient, platform_on, monkeypatch):
    """The realistic leak: a provider quoting the credential it received back inside its 401 body."""
    monkeypatch.setattr(A, "relay", _fake_relay(
        401, f'{{"error":"invalid key","received":"Bearer {PLATFORM_KEY}"}}'.encode()))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 401
    row = await _row(clients)
    assert PLATFORM_KEY not in row["error_response"], "the platform key survived into the audit row"
    assert PLATFORM_KEY not in (row["error_request"] or "")
    assert "invalid key" in row["error_response"], "redaction must not eat the message"


async def test_the_platform_key_never_reaches_the_columns_query_injected(
        clients: AsyncClient, platform_on, monkeypatch):
    """spyfu authenticates by QUERY PARAM, so its key can come back inside an echoed URL — including
    percent-encoded, which no word-boundary regex would catch."""
    from urllib.parse import quote
    # The key appears TWICE, deliberately: once inside a URL, which the query-shaped rule masks on its
    # own, and once in prose, where nothing but the exact-substring mask will find it. Without the
    # second copy this test passes with `_platform_secret_renderings` disabled entirely — verified.
    echoed = (f'{{"error":"key {SPYFU_KEY} is not valid for this endpoint",'
              f'"url":"https://api.spyfu.com/x?api_key={quote(SPYFU_KEY, safe="")}&domain=a.com"}}')
    monkeypatch.setattr(A, "relay", _fake_relay(403, echoed.encode()))
    r = await clients.get(f"/call/{EP_SPYFU}?domain=a.com")
    assert r.status_code == 403
    row = await _row(clients)
    assert SPYFU_KEY not in row["error_response"]
    assert quote(SPYFU_KEY, safe="") not in row["error_response"]


async def test_a_callers_own_value_in_the_injected_slot_is_dropped(
        clients: AsyncClient, platform_on, monkeypatch):
    """A caller who passes their own value into the param the injector overwrites."""
    monkeypatch.setattr(A, "relay", _fake_relay(400, b'{"error":"bad request"}'))
    r = await clients.get(f"/call/{EP_SPYFU}?domain=a.com&api_key=CALLERS-OWN-SECRET-VALUE")
    assert r.status_code == 400
    row = await _row(clients)
    assert "CALLERS-OWN-SECRET-VALUE" not in (row["error_request"] or "")
    assert "domain=a.com" in row["error_request"], "the useful params must survive"


async def test_a_decoded_basic_credential_never_survives(clients: AsyncClient, platform_on,
                                                         monkeypatch):
    """dataforseo's platform value IS the base64 of `login:password` (config.py says so), and it is
    the largest provider by spend. A provider that decodes Basic auth and reports the two halves
    echoes treg's credential in a form where neither the base64 blob nor `Basic <blob>` appears —
    so only decomposing it can catch this."""
    import base64
    login, password = "treg-ops@example.com", "pw.7Kq2"
    monkeypatch.setenv("TREG_PLATFORM_KEY_DATAFORSEO",
                       base64.b64encode(f"{login}:{password}".encode()).decode())
    get_settings.cache_clear()
    monkeypatch.setattr(A, "relay", _fake_relay(
        401, json.dumps({"error": "auth failed",
                         "received_username": login,
                         "received_password": password}).encode()))
    r = await clients.post(f"/call/{EP_POST}", json=[{"url": "https://example.com"}])
    assert r.status_code == 401
    row = await _row(clients)
    assert login not in row["error_response"], "the Basic login survived"
    assert password not in row["error_response"], "the Basic password survived"


async def test_a_lowercase_percent_encoded_key_never_survives(clients: AsyncClient, platform_on,
                                                              monkeypatch):
    """`quote()` emits UPPERCASE hex; plenty of servers echo lowercase. Under a neutral field name
    the query-shaped rule does not fire either, so the exact mask is the only thing left."""
    from urllib.parse import quote
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "a/b+c=dQ")
    get_settings.cache_clear()
    monkeypatch.setattr(A, "relay", _fake_relay(
        400, f'{{"error":"bad","echo":"{quote("a/b+c=dQ", safe="").lower()}"}}'.encode()))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 400
    row = await _row(clients)
    assert "a%2fb%2bc%3ddq" not in row["error_response"].lower(), "the key leaked"
    # And it must be MASKED, not swallowed by the fail-closed backstop. Without this the test cannot
    # tell the good path from the emergency one, and passes even with every encoding variant removed
    # (verified) — the backstop would simply drop the whole snippet and the key-absence check holds.
    assert "bad" in row["error_response"], "the message should survive; only the key is masked"


# ---- diagnosability, not just presence ----------------------------------------------------------
async def test_an_empty_bodied_429_still_says_when_to_retry(clients: AsyncClient, platform_on,
                                                            monkeypatch):
    """The real tikhub population is 70 401s and 54 429s. Those bodies are often empty or generic,
    and the headers are then the entire diagnosis — 'quota gone, back in 60s' versus 'bad key'."""
    monkeypatch.setattr(A, "relay", _fake_relay(429, b"", headers={
        "retry-after": "60", "x-ratelimit-remaining": "0", "x-request-id": "req-8f2a4c19"}))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 429
    row = await _row(clients)
    assert "retry-after=60" in row["error_response"]
    assert "x-ratelimit-remaining=0" in row["error_response"]
    assert "req-8f2a4c19" in row["error_response"], "the provider's request id must survive"


async def test_a_401_keeps_the_auth_challenge_and_not_the_credential(clients: AsyncClient,
                                                                     platform_on, monkeypatch):
    monkeypatch.setattr(A, "relay", _fake_relay(401, b"", headers={
        "www-authenticate": 'Bearer realm="api", error="invalid_token"'}))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 401
    row = await _row(clients)
    assert "invalid_token" in row["error_response"]
    assert PLATFORM_KEY not in row["error_response"]


async def test_a_provider_correlation_id_survives_redaction(clients: AsyncClient, platform_on,
                                                            monkeypatch):
    """The one thing you quote to a provider's support desk. The argv rule masked every 24+ token,
    so UUIDs, trace ids and request ids were deleted 100% of the time — measured on real bodies, the
    prose always survived and the correlation field never did."""
    trace = "11da3d88-e351-4c07-87ea-f5160d76a87d"          # 36 chars: the old rule ate this
    monkeypatch.setattr(A, "relay", _fake_relay(
        400, f'{{"message":"bad keyword","request_id":"{trace}"}}'.encode()))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 400
    row = await _row(clients)
    assert trace in row["error_response"], "the correlation id must survive"
    assert "bad keyword" in row["error_response"]


async def test_a_real_secret_shape_is_still_masked(clients: AsyncClient, platform_on, monkeypatch):
    """Relaxing the catch-all must not relax the targeted rules: known prefixes and JWTs still go.

    The fixture is the placeholder `.gitleaks.toml` already allowlists for exactly this purpose
    ("proves output redaction masks a key") rather than a fresh invented one — a test whose job is
    to prove secrets get masked should not itself trip the secret scanner, and reusing the existing
    entry keeps the allowlist from growing one line per test that needs a key-shaped string.
    """
    fake_key = "sk_live_ABCDEFGHIJKLMNOP1234"
    fake_jwt = "eyJhbGciOi.JIUzI1NiIsInR5cCI6"
    monkeypatch.setattr(A, "relay", _fake_relay(
        400, f'{{"message":"bad token {fake_key} and {fake_jwt}"}}'.encode()))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 400
    row = await _row(clients)
    assert fake_key not in row["error_response"]
    assert fake_jwt not in row["error_response"]


async def test_a_bodyless_failure_still_leaves_a_row(clients: AsyncClient, platform_on, monkeypatch):
    """A 4xx with no body and none of the allowlisted headers used to produce "" for both snippets,
    and `_audit` only stores evidence when one is truthy — so the row vanished from /admin/errors and
    the failure looked like capture had never run. "Nothing came back" is itself the finding."""
    monkeypatch.setattr(A, "relay", _fake_relay(500, b""))
    # The required param has to be present or treg refuses before relay — the empty half under test
    # is the RESPONSE, which is what a bodyless provider 5xx actually looks like.
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 500
    row = await _row(clients)
    assert row["error_response"] == "<no response body or headers>"
    errs = (await clients.get("/admin/errors", headers=ADMIN)).json()["errors"]
    assert errs, "and it must be visible in the view built to show failures"


async def test_expired_evidence_is_a_state_not_content(clients: AsyncClient, platform_on,
                                                       monkeypatch):
    """The sentinel must never be served as though it were the provider's answer."""
    monkeypatch.setattr(A, "relay", _fake_relay(400, b'{"error":"aged out later"}'))
    await clients.get(f"/call/{EP}?aweme_id=7")
    from treg import audit
    await audit.drain()
    async with session_maker() as db:
        row = (await db.execute(
            select(CallRecord).order_by(CallRecord.id.desc()).limit(1))).scalars().first()
        row.created_at = row.created_at - timedelta(days=A._ERROR_EVIDENCE_TTL_DAYS + 1)
        db.add(row)
        await db.commit()
    d = (await clients.get("/admin/errors?days=30", headers=ADMIN)).json()
    aged = [e for e in d["errors"] if e["expired"]]
    assert aged, "the row is still listed as a failure"
    assert aged[0]["response"] is None, "but its evidence is absent, not the literal sentinel"


async def test_a_cap_refusal_says_WHICH_cap(clients: AsyncClient, platform_on, monkeypatch):
    """`refused_by='cap'` is an aggregation bucket, not a diagnosis: every 429 lands in it, covering
    member call caps, tag caps, the platform ceiling, trial allowances and demo-IP limits. Which one
    it was is in treg's own detail, and 878 refusals a week were discarding it."""
    from fastapi import HTTPException

    async def _boom(*a, **k):
        raise HTTPException(status_code=429, detail="daily call cap reached for this member (500/500)")

    monkeypatch.setattr(A, "_platform_reserve", _boom)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 429
    row = await _row(clients)
    assert row["refused_by"] == "cap", "still aggregates as a cap"
    assert "daily call cap reached for this member" in row["error_response"], "and now says which"


async def test_admin_errors_surfaces_the_method(clients: AsyncClient, platform_on, monkeypatch):
    """A GET at a POST endpoint IS the diagnosis for 47 real apollo failures."""
    monkeypatch.setattr(A, "relay", _fake_relay(400, b'{"error":"nope"}'))
    await clients.get(f"/call/{EP}?aweme_id=7")
    from treg import audit
    await audit.drain()
    errs = (await clients.get("/admin/errors", headers=ADMIN)).json()["errors"]
    assert errs and errs[0]["method"] == "GET"


# ---- awkward bodies -----------------------------------------------------------------------------
async def test_a_gzipped_error_page_does_not_become_replacement_characters(
        clients: AsyncClient, platform_on, monkeypatch):
    """`force_identity` asks a provider not to compress, but a CDN/WAF error page is generated at the
    edge and answers however it likes — and those 403s are exactly what this feature is for."""
    monkeypatch.setattr(A, "relay", _fake_relay(
        403, gzip.compress(b'{"error":"blocked by firewall, ray id 8f2a"}'),
        headers={"content-encoding": "gzip"}))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 403
    row = await _row(clients)
    assert "blocked by firewall" in row["error_response"]
    assert "�" not in row["error_response"]


async def test_a_binary_body_is_described_not_mangled(clients: AsyncClient, platform_on, monkeypatch):
    monkeypatch.setattr(A, "relay", _fake_relay(500, b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00" * 8))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 500
    row = await _row(clients)
    assert row["error_response"].startswith("<binary")


async def test_a_huge_error_page_is_truncated(clients: AsyncClient, platform_on, monkeypatch):
    huge = b"<html><body>" + b"the server encountered an error. " * 2000 + b"</body></html>"
    monkeypatch.setattr(A, "relay", _fake_relay(500, huge))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 500
    row = await _row(clients)
    assert len(row["error_response"]) <= A._ERROR_RESPONSE_MAX + 1


def test_a_compression_bomb_does_not_expand_without_bound():
    """Slicing the input to 8KiB bounds the INPUT, not the output: 20MB of one repeated byte
    compresses to under 20KB, so an unbounded `gzip.decompress` hands megabytes to four regexes
    synchronously on the request path.

    Asserted against `_decode_error_body` DIRECTLY, not through a call: end to end the later
    truncation to 2000 chars hides the difference entirely, so the test would pass with the cap
    removed (verified) and pin nothing but the truncation that already existed. The intermediate is
    the only place the bound is observable.
    """
    bomb = gzip.compress(b"A" * 20_000_000)
    assert len(bomb) < 32_000, "sanity: the bomb really is small compressed"
    out = A._decode_error_body(bomb, "gzip")
    assert len(out) <= A._ERROR_RESPONSE_MAX * 4 + 1, "decompression ran unbounded"


def test_unknown_telemetry_costs_a_column_not_the_whole_row():
    """The pre-existing bug this feature had to fix first: `record_call` splats telemetry into
    `CallRecord(**fields)`, so ONE key without a column raised inside `_write`, where a bare except
    swallowed it — and the entire audit row vanished with no trace anywhere."""
    from treg.audit import _known_fields

    kept = _known_fields(CallRecord, {"provider": "tikhub", "error_response": "boom",
                                      "not_a_column_at_all": "xyz"})
    assert kept == {"provider": "tikhub", "error_response": "boom"}
    assert CallRecord(user_email="a@b.c", tool_name="t", method="GET", path="/x",
                      status_code=400, **kept).provider == "tikhub"


# ---- the customer-facing surface is unchanged ---------------------------------------------------
async def test_the_columns_are_not_exposed_to_the_team_yet(clients: AsyncClient, platform_on,
                                                           monkeypatch):
    """v1 is admin-only. `/calls` builds an explicit field list, so a new column cannot appear there
    by accident — this pins that, because the follow-on that exposes it must be deliberate."""
    monkeypatch.setattr(A, "relay", _fake_relay(400, b'{"error":"nope"}'))
    await clients.get(f"/call/{EP}?aweme_id=7")
    from treg import audit
    await audit.drain()
    body = (await clients.get("/calls")).text
    assert "error_response" not in body, "the customer feed must not expose the evidence columns yet"


# ---- the admin view, and ageing ------------------------------------------------------------------
async def test_admin_errors_lists_only_failed_platform_calls(clients: AsyncClient, platform_on,
                                                             monkeypatch):
    monkeypatch.setattr(A, "relay", _fake_relay(400, b'{"error":"aweme_id must be numeric"}'))
    await clients.get(f"/call/{EP}?aweme_id=bad")
    monkeypatch.setattr(A, "relay", _fake_relay(200, b'{"ok":true}'))
    await clients.get(f"/call/{EP}?aweme_id=7")          # a success must not appear
    from treg import audit
    await audit.drain()

    d = (await clients.get("/admin/errors", headers=ADMIN)).json()
    assert d["retention_days"] == A._ERROR_EVIDENCE_TTL_DAYS
    assert len(d["errors"]) == 1, "only the failure carries evidence"
    e = d["errors"][0]
    assert e["status"] == 400 and e["provider"] == "tikhub"
    assert "aweme_id must be numeric" in e["response"]
    assert "aweme_id=bad" in e["request"]


async def test_evidence_ages_out_but_the_audit_row_survives(clients: AsyncClient, platform_on,
                                                            monkeypatch):
    """Retention blanks the two columns and touches nothing else — the call itself is the audit
    trail and has to outlive its evidence."""
    monkeypatch.setattr(A, "relay", _fake_relay(400, b'{"error":"stale failure"}'))
    await clients.get(f"/call/{EP}?aweme_id=bad")
    from treg import audit
    await audit.drain()

    async with session_maker() as db:
        row = (await db.execute(
            select(CallRecord).order_by(CallRecord.id.desc()).limit(1))).scalars().first()
        row.created_at = row.created_at - timedelta(days=A._ERROR_EVIDENCE_TTL_DAYS + 1)
        db.add(row)
        await db.commit()
        call_id, status = row.id, row.status_code

    assert (await clients.get("/admin/errors", headers=ADMIN)).json()["expired_rows_purged"] == 1
    async with session_maker() as db:
        aged = await db.get(CallRecord, call_id)
        assert aged.error_response == A._ERROR_EVIDENCE_EXPIRED, "aged out, not silently NULL"
        assert aged.status_code == status, "the rest of the audit row is untouched"
        assert aged.endpoint_id == EP
