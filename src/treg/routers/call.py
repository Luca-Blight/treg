"""HTTP adapters for the proxied call surface."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from .. import analytics, audit, catalog_store, ledger, oauth, oauth_providers
from .. import sandbox as demo_sandbox
from ..application.call.idempotency import (
    IDEMPOTENCY_HEADER,
    _release_idempotent_claim as release_idempotent_claim,
    _store_idempotent,
)
from ..application.call.authorize import authorize_call, enforce_public_demo_limit
from ..application.call.resolve import (
    MarketplaceCall,
    QueryValues,
    _billed_marketplace,
    _catalog_endpoint_for,
    _enforce_catalog_status,
    _marketplace_secret,
    _may_have_body as may_have_body,
    _oauth_billed_provider,
    _platform_estimate_micro,
    _platform_offer,
    _resolve_call,
    _resolve_marketplace_call,
    resolve_call_target,
    resolve_marketplace_target,
)
from ..application.call.reserve import _enforce_tag_budgets, _platform_reserve
from ..application.call.intake import (
    META_HEADER,
    CallMeta,
    _parse_call_meta as parse_call_meta,
    _tag_telemetry,
    prepare_call_intake,
)
from ..application.call.types import CallFailure
from ..caller_metadata import _client_of
from ..config import get_settings
from ..db import get_session
from ..domain.governance import access as access_policy
from ..domain.governance import publicdemo as publicdemo_policy
from ..domain.identity.access import Caller, require_member
from ..models import Secret, Tool
from ..proxy import relay
from .auth import _client_ip
from .orgs import count_today


# Stage 4b moves the HTTP surface before its call-kernel collaborators. These annotations are
# populated by api.py and retire one phase at a time as commits 6 through 19 assign final owners.
_ERROR_BODY_SLICE: Any
_ERROR_CALLER_BODY_MAX: Any
_ERROR_MASKING_FAILED: Any
_ERROR_RESPONSE_MAX: Any
_await_before_reserve: Any
_buffer_response: Any
_caller_request_snippet: Any
_error_response_evidence: Any
_finish_cancelled_call: Any
_now_ms: Any
_peek_stream_head: Any
_platform_settle: Any
_record_first_call: Any
_redact_snippet: Any
_relay_live_demo: Any
_safe_secret_renderings: Any


# The app alias preserves the moved handlers' decorator text byte-for-byte.
app = APIRouter()
router = app


def _require_tool_use_http(caller: Caller, tool: Tool) -> None:
    try:
        access_policy._require_tool_use(caller, tool)
    except access_policy.AccessPolicyError as exc:
        raise HTTPException(status_code=403, detail=exc.detail) from exc


async def _enforce_public_demo_ip_cap(request: Request, db: AsyncSession) -> None:
    try:
        await publicdemo_policy.enforce_public_demo_ip_cap(_client_ip(request), db)
    except publicdemo_policy.PublicDemoLimitError as exc:
        await db.commit()
        raise HTTPException(status_code=429, detail=exc.detail) from exc
    await db.commit()


async def _enforce_daily_cap(caller: Caller, db: AsyncSession) -> None:
    """Refuse a call/run once the caller has used their per-user daily cap for this org. `-1` (the
    default) = unlimited, so unmetered members pay ZERO extra queries. The sandbox has its own limiter
    and is exempt. Soft by design: the count reads best-effort `CallRecord`s, so under heavy load it
    can lag slightly and fail OPEN (a few extra slip through) — never closed. See docs/USAGE-METERING-PLAN.md."""
    cap = caller.membership.daily_call_cap
    if cap < 0 or demo_sandbox.is_sandbox(caller.org):
        return
    used = await count_today(db, caller.org_id, caller.email)
    if used >= cap:
        raise HTTPException(status_code=429, detail=(
            f"daily usage limit reached ({used}/{cap}) — ask an admin to raise your cap"))


def _translate_call_failure(exc: CallFailure) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _parse_call_meta(request: Request, caller: Caller | None = None) -> CallMeta:
    try:
        return parse_call_meta(request.headers.get(META_HEADER), caller)
    except CallFailure as exc:
        raise _translate_call_failure(exc) from exc


async def _release_idempotent_claim(request: Request) -> None:
    claim = getattr(request.state, "idem_claim", None)
    request.state.idem_claim = None
    await release_idempotent_claim(claim)


def _query_values(request: Request) -> QueryValues:
    return QueryValues(tuple(request.query_params.multi_items()))


def _may_have_body(request: Request) -> bool:
    return may_have_body(tuple(request.headers.raw))


async def _stamp_call_exit(request: Request, resp: Response, status_code: int) -> None:
    """Give one `/call/` exit the three things every other exit gets: the id that joins the response
    to the audit row, the row itself, and the release of any idempotency label the request took.

    Shared by the two handlers that answer a call without reaching `call_tool`'s own bookkeeping.
    Identity comes from `request.state` (stashed at handler entry); an exit that failed before the
    caller was resolved records an anonymous row, which is still the fact that someone knocked."""
    call_ref = getattr(request.state, "call_ref", "") or uuid.uuid4().hex
    request.state.call_ref = call_ref
    resp.headers["X-Treg-Call-Id"] = call_ref
    if (cost_micro := getattr(request.state, "call_cost_micro", None)) is not None:
        resp.headers["X-Treg-Cost-Micro"] = str(cost_micro)
    if not getattr(request.state, "call_audited", False):
        org_id, email = getattr(request.state, "call_identity", (None, ""))
        rest = request.url.path[len("/call/"):]
        audit.record_call(
            org_id=org_id, user_email=email, tool_name=rest.split("/", 1)[0] or "—",
            method=request.method, path=request.url.path, status_code=status_code,
            client=_client_of(request), refused_by=_refusal_kind(status_code),
            telemetry={"call_ref": call_ref})
    # A failed call must not keep its idempotency label. The claim is taken before the upstream
    # call, and a request that dies anywhere after that — a bad parameter, a deny rule, an empty
    # balance, a saturated pool — would otherwise hold the label for the whole window and answer
    # every retry with 409. Worse than the problem this feature exists to solve, and found by the
    # test for it.
    await _release_idempotent_claim(request)

def _refusal_kind(status_code: int) -> str | None:
    """Which gate said no, from the status treg chose for it (models.CallRecord.refused_by).

    Statuses map 1:1 because each gate owns its code on `/call/`: the vendor's own 401/404/429
    never comes through here — a relayed response is a Response, not an HTTPException. 5xx maps
    to None: a 502 is the upstream failing to answer, which is a fact about the provider, and
    must not be counted as a treg refusal."""
    if status_code >= 500:
        return None
    return {401: "auth", 402: "balance", 403: "policy", 404: "resolution", 410: "retired",
            429: "cap"}.get(status_code, "request")

@app.get("/catalog/endpoints/{endpoint_id}/access", include_in_schema=False)
async def catalog_endpoint_access(
    endpoint_id: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """Authenticated dry-run of the marketplace credential ladder — which tier would serve YOU.
    Read by `treg catalog get` to print an honest access line under RUN IT (the open catalog
    endpoints stay unauthenticated; this one needs to know who is asking)."""
    ep = catalog_store.load().by_id.get(endpoint_id)
    if ep is None:
        raise HTTPException(status_code=404, detail=f"unknown endpoint {endpoint_id!r}")
    try:
        _enforce_catalog_status(ep)
    except CallFailure as exc:
        raise _translate_call_failure(exc) from exc
    service = ep["provider"]
    provider = oauth_providers.get(service)
    if provider is None or not provider.base_url:
        return {"tier": "none", "detail": f"{service} isn't proxy-callable yet"}
    # An oauth-billed provider is metered even on the org's own connection (the upstream bills
    # treg's app, not the account) — the dry-run must say so, or the price is a surprise.
    billed_note = ""
    if provider.platform_billed and service in get_settings().oauth_billed_set:
        cv = catalog_store.load().cost_view(ep.get("cost"), service) if ep.get("cost") else None
        est = _platform_estimate_micro(cv, {}) if cv and cv.get("usd") else 0
        billed_note = (f" — metered from the team balance (~${ledger.usd(est):g}/call: "
                       f"{service} bills treg's app per use)") if est else \
                      f" — metered from the team balance ({service} bills treg's app per use)"
    probe = provider.base_url.rstrip("/") + "/" + (ep["path"] or "/").lstrip("/")
    try:
        target = await resolve_call_target(probe, caller, _resolve_call)
        tool = target.tool
        return {"tier": "tool", "metered": bool(billed_note),
                "detail": f"will use this org's registered {tool.name!r} tool{billed_note}"}
    except CallFailure as exc:
        if exc.status_code == 403:
            return {"tier": "restricted", "detail": "a registered tool exists but your access is restricted — ask an admin"}
        if exc.status_code != 404:
            raise _translate_call_failure(exc) from exc
    if await _marketplace_secret(service, caller.org_id, db) is not None:
        return {"tier": "credential", "metered": bool(billed_note),
                "detail": f"will use this org's {service} credential (no tool needed){billed_note}"}
    cost = _platform_offer(ep, provider, caller.org)
    if cost is not None:
        # The number is the honest per-call price at the DEFAULT page size — a `per_result` endpoint
        # costs more or less depending on how many rows the caller asks for, so it is "~".
        est = _platform_estimate_micro(cost, {})
        return {
            "tier": "platform",
            "detail": (f"no key needed — uses treg's {service} key, ~${ledger.usd(est):g}/call "
                       f"from your team balance (treg balance)"),
            "estimated_cost_micro": est,
            "estimated_cost_usd": ledger.usd(est),
        }
    hint = (f"connect with: treg connections connect --provider {service}"
            if not provider.uses_pasted_secret else
            f"connect with: treg connections connect --provider {service}, or treg secret add {service} …")
    return {"tier": "none", "detail": f"no {service} credential in this org yet — {hint}"}

@app.api_route(
    "/call/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def call_tool(
    rest: str,
    request: Request,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
):
    # Identity for the refusal fallback in `_mark_treg_own_errors`: a raise anywhere below (unknown
    # tool, deny rule, daily cap) leaves this handler without an audit row, and the exception handler
    # is the one place every such refusal passes through — but it has no Caller of its own.
    request.state.call_identity = (caller.org_id, caller.email)
    # Faithful-relay: use the RAW request path, not Starlette's decoded path param. Decoding is
    # lossy — an encoded slash (`%2f`) in `rest` would become a real `/` and change the upstream
    # route (npm's scoped publish `PUT /@scope%2fname` 404s as `/@scope/name`). httpx preserves
    # valid percent-escapes, so the original bytes travel through to the upstream one-to-one.
    raw_path = request.scope.get("raw_path")
    if raw_path:
        _, sep, raw_rest = raw_path.decode("ascii", "replace").partition("/call/")
        if sep:
            rest = raw_rest
    # The caller's tags, parsed ONCE and read by everything below — the budgets, the ledger, the
    # idempotency scope and the audit row. Before the idempotency block on purpose: a malformed bag
    # must not burn the caller's label on its way to a 422.
    meta = _parse_call_meta(request, caller)
    # ONE id for this call, minted before anything can spend: it becomes the ledger's call_id on a
    # metered call, lands on the audit row, and goes back as X-Treg-Call-Id — so a builder can join
    # our records to theirs on a single value.
    call_ref = uuid.uuid4().hex
    request.state.call_ref = call_ref
    try:
        intake = await prepare_call_intake(
            meta=meta,
            idempotency_header=request.headers.get(IDEMPOTENCY_HEADER),
            method=request.method,
            rest=rest,
            raw_query=request.url.query or "",
            read_body=request.body,
            caller=caller,
            enforce_tag_budgets=_enforce_tag_budgets,
        )
    except CallFailure as exc:
        raise _translate_call_failure(exc) from exc
    idem_key = intake.idempotency_key
    idem_fingerprint = intake.fingerprint
    if intake.replay is not None:
        replayed = intake.replay
        return Response(
            content=replayed.body,
            status_code=replayed.status_code,
            media_type=replayed.media_type,
            headers={"X-Treg-Idempotent-Replay": "true",
                     "X-Treg-Cost-Micro": str(replayed.charged_micro),
                     **({"X-Treg-Call-Id": replayed.call_ref} if replayed.call_ref else {})},
        )
    # Park it so a failure anywhere below can give the label back. Set AFTER the claim succeeds,
    # so losing the race above never releases the winner's row.
    request.state.idem_claim = intake.claim

    drop_params: set[str] = set()
    mk: MarketplaceCall | None = None
    own_tool_miss: dict | None = None
    try:
        target = await _await_before_reserve(
            resolve_call_target(rest, caller, _resolve_call), request, call_ref)
        tool, upstream_url = target.tool, target.upstream
    except CallFailure as exc:
        # Not a tool → maybe a marketplace endpoint id (`treg call tikhub.tiktok.video.comments`).
        # Only the 404 falls through, so an org tool with the same name always wins.
        ep = _catalog_endpoint_for(rest) if exc.status_code == 404 else None
        if ep is None:
            raise _translate_call_failure(exc) from exc
        if (isinstance(exc.detail, dict)
                and str(exc.detail.get("hint", "")).startswith("your org has tool ")):
            own_tool_miss = exc.detail
        try:
            mk = await _await_before_reserve(resolve_marketplace_target(
                ep,
                method=request.method,
                query=_query_values(request),
                has_body=_may_have_body(request),
                read_body=request.body,
                caller=caller,
                resolve_call=_resolve_call,
            ), request, call_ref)
        except CallFailure as mkexc:
            # Catalog resolution is allowed to fall through from a named miss, but its own 404 must
            # not discard the useful fact discovered there: this org already has a nearby own tool.
            if mkexc.status_code == 404 and own_tool_miss is not None:
                mkexc.detail = {
                    "error": mkexc.detail,
                    "hint": own_tool_miss["hint"],
                    "did_you_mean": own_tool_miss["did_you_mean"],
                }
            # A malformed marketplace call (wrong method, missing param, no credential, 502) must
            # still leave a trace — it's exactly the row the caller will come asking about.
            request.state.call_audited = True
            audit.record_call(
                org_id=caller.org_id, user_email=caller.email, tool_name=ep["id"],
                method=request.method, path=rest, status_code=mkexc.status_code,
                client=_client_of(request), refused_by=_refusal_kind(mkexc.status_code),
                telemetry={"call_ref": call_ref,
                           "endpoint_id": ep["id"], "provider": ep.get("provider"),
                           **_tag_telemetry(meta)})
            analytics.capture(caller.email, "tool_called",
                {"tool_name": ep["id"], "status_code": mkexc.status_code,
                 "client": _client_of(request), "method": request.method,
                 "own_tool": False, "provider": ep.get("provider"), "endpoint_id": ep["id"]},
                groups={"team": caller.org.slug})
            raise _translate_call_failure(mkexc) from mkexc
        tool, upstream_url, drop_params = mk.tool, mk.upstream, mk.consumed
    try:
        await _await_before_reserve(
            authorize_call(
                caller=caller,
                tool=tool,
                upstream_url=upstream_url,
                method=request.method,
                client_ip=_client_ip(request),
            ),
            request,
            call_ref,
        )
    except CallFailure as exc:
        raise _translate_call_failure(exc) from exc

    # The caller's own request bytes, read ONCE when it is safe to buffer them, so a failure can be
    # explained later (see models.CallRecord.error_request). Metered JSON calls already require full
    # buffering. Otherwise only a declared body at or below 64 KiB is cached; large and chunked uploads
    # keep streaming and still retain their query-param half if they fail. Starlette's request cache
    # lets relay stream the same bytes after this read.
    # Named `caller_body`: `body` in this function is the buffered RESPONSE, and confusing the two
    # would file the provider's answer as the caller's request.
    caller_body = b""
    content_length = request.headers.get("content-length")
    small_declared_body = False
    if content_length is not None:
        try:
            small_declared_body = 0 <= int(content_length) <= _ERROR_CALLER_BODY_MAX
        except ValueError:
            small_declared_body = False
    if _may_have_body(request) and ((mk is not None and mk.metered) or small_declared_body):
        try:
            caller_body = await _await_before_reserve(request.body(), request, call_ref)
        except Exception:  # noqa: BLE001 — a caller that hung up must not become a 500 here
            caller_body = b""

    # Snapshot the audit identity NOW: a failed reserve rolls the session back, expiring the ORM
    # instances behind `caller` — reading them inside a later _audit would raise MissingGreenlet.
    audit_org_id, audit_email, audit_tool = caller.org_id, caller.email, tool.name
    audit_slug = caller.org.slug  # PostHog group key — must match the browser's posthog.group('team', slug)

    def _audit(status_code: int, *, observed_micro: int | None = None, charged_micro: int | None = None,
               duration_ms: int | None = None, response_bytes: int | None = None,
               refused_by: str | None = None,
               error_request: str | None = None, error_response: str | None = None) -> None:
        # Audit the attempt too — failures are results worth recording. A marketplace call additionally
        # carries its telemetry (which endpoint, which credential tier, what it cost): still
        # fire-and-forget, because the money itself already landed synchronously in the ledger.
        request.state.call_audited = True  # the refusal fallback in _mark_treg_own_errors stands down
        telemetry: dict = {"call_ref": call_ref}
        if meta.tags:
            # Own-tool calls carry tags too: a builder's usage report has to account for every call
            # their user made, not only the ones that spent treg's money.
            telemetry |= _tag_telemetry(meta)
        if mk is not None:
            telemetry |= {
                "endpoint_id": mk.endpoint_id, "provider": mk.provider, "credential_tier": mk.tier,
                # An org credential riding treg's pay-per-use OAuth app: tier stays tool/credential
                # (the credential IS theirs), this says who the upstream billed.
                **({"oauth_billed": True} if mk.billed_oauth else {}),
                "cost_estimated_micro": mk.estimate_micro or None,  # informational on tiers 1/2
                "cost_observed_micro": observed_micro,
                "cost_charged_micro": charged_micro,
                "duration_ms": duration_ms, "response_bytes": response_bytes,
                "params_hash": mk.params_hash,
            }
        # Sanctioned reversal of PR #139: failed own-key and own-tool calls now retain the same
        # redacted, admin-only, 14-day evidence as marketplace failures. Successes remain empty and
        # `/calls` still never exposes these columns.
        if error_request or error_response:
            telemetry |= {"error_request": error_request, "error_response": error_response}
        audit.record_call(
            org_id=audit_org_id, user_email=audit_email, tool_name=audit_tool,
            method=request.method, path=upstream_url, status_code=status_code,
            client=_client_of(request), refused_by=refused_by, telemetry=telemetry,
        )
        # Product analytics mirror of the row above. Deliberately excludes params, bodies, and the
        # full upstream URL (hostname only) — per-call detail beyond what a chart needs stays in the DB.
        props = {"tool_name": audit_tool, "status_code": status_code,
                 "client": _client_of(request), "method": request.method,
                 "own_tool": mk is None, "duration_ms": duration_ms}
        if mk is not None:
            props |= {"provider": mk.provider, "endpoint_id": mk.endpoint_id,
                      "tier": mk.tier, "metered": mk.metered, "cost_type": mk.cost_type,
                      "charged_micro": charged_micro, "observed_micro": observed_micro}
        else:
            props["provider"] = urlsplit(upstream_url).hostname or ""
        analytics.capture(audit_email, "tool_called", props, groups={"team": audit_slug})

    # Landing-page sandbox: never touch the network — EXCEPT the one live wire. A call to the
    # exact seeded stripe tool (fingerprint-matched; see sandbox.is_live_tool) relays to the real
    # Stripe test API with the env-held demo key. Any tampered/lookalike tool falls through to
    # synthesize below, so there is never a key to exfiltrate from a sandbox org.
    if demo_sandbox.is_sandbox(caller.org):
        live_key = get_settings().demo_stripe_key
        if live_key and demo_sandbox.is_live_tool(tool) and request.method in ("GET", "POST"):
            try:
                await _await_before_reserve(
                    enforce_public_demo_limit(_client_ip(request)), request, call_ref
                )  # one shared wire → meter by client IP
            except CallFailure as exc:
                raise _translate_call_failure(exc) from exc
            await _await_before_reserve(
                db.commit(), request, call_ref
            )  # end the DB phase before network I/O (see the same call before relay())
            try:
                response = await _await_before_reserve(
                    _relay_live_demo(
                        request, upstream_url, live_key, demo_sandbox.visitor_name(caller.org.slug)),
                    request, call_ref)
            except httpx.RequestError as exc:
                _audit(502)
                raise HTTPException(status_code=502, detail=f"upstream request failed: {str(exc) or type(exc).__name__}")
            _audit(response.status_code)
            return response
        secrets = {}
        for sid in {b.get("secret_id") for b in tool.bindings if b.get("secret_id") is not None}:
            s = await _await_before_reserve(db.get(Secret, sid), request, call_ref)
            if s is not None and s.org_id == caller.org_id:
                secrets[sid] = s
        body = (await _await_before_reserve(request.body(), request, call_ref)).decode(
            "utf-8", "replace")
        result = demo_sandbox.synthesize(
            request.method, upstream_url, tool, secrets,
            query=request.query_params.multi_items(), body=body)
        _audit(200)
        return JSONResponse(result)

    # Load every secret the bindings need BEFORE the money gate (api does the DB work; proxy stays
    # I/O-free): whether this call is METERED can depend on the credential itself — a registry X
    # connect rides treg's pay-per-use app, so the org's "own" oauth secret is exactly what makes
    # the call billable. Nothing is reserved yet, so a load failure here leaves no hold behind.
    secrets: dict[int, Secret] = {}
    try:
        # A platform binding carries no secret_id — its value comes from settings at relay time.
        for sid in {b["secret_id"] for b in tool.bindings if b.get("secret_id") is not None}:
            secret = await _await_before_reserve(db.get(Secret, sid), request, call_ref)
            if secret is None or secret.org_id != caller.org_id:
                raise HTTPException(status_code=409, detail="a bound secret is missing")
            secrets[sid] = secret
    except HTTPException as exc:
        _audit(exc.status_code)  # record the failed attempt, same as a mid-relay refusal would
        raise
    billed_provider = _oauth_billed_provider(secrets)
    if billed_provider is not None:
        # The sandbox never reaches here (it returned above); the public demo could, and one shared
        # org must never be able to spend treg's upstream credits — refuse rather than relay free.
        if caller.org.public_demo:
            _audit(403)
            raise HTTPException(status_code=403, detail=(
                f"{billed_provider.display_name} calls are pay-per-use on treg's app and the "
                f"public demo can't spend — create your own team to use this"))
        mk = await _await_before_reserve(_billed_marketplace(
            mk, billed_provider, tool, upstream_url,
            method=request.method,
            query=_query_values(request),
            has_body=_may_have_body(request),
            read_body=request.body,
        ), request, call_ref)

    # Metered — treg's own money is about to be spent (tier 4's platform key, or a registry OAuth
    # connect on a pay-per-use app), so take the money FIRST. Deliberately the last gate before the
    # network: everything above (ACL, deny rules, caps) can still refuse the call, and a refused
    # call must not leave a hold behind for the reaper to clean up.
    if mk is not None and mk.metered:
        # Rendered BEFORE the reserve while `tool` is live. The application closes its reservation
        # session before returning a 402, so refusal evidence cannot rely on a later ORM load. Doing
        # that once turned the refusal an agent is most likely to hit into a 500 with no balance or
        # top-up URL. Same reasoning as `block_id` in billing._credit, and pinned by a test.
        refusal_secrets = _safe_secret_renderings(tool, secrets)
        try:
            # Secret reads above opened the dependency session. Release its pool slot before the
            # application opens the short transaction that owns the reservation.
            await db.commit()
            await _platform_reserve(mk, caller, meta=meta, call_ref=call_ref)
        except asyncio.CancelledError:
            await _finish_cancelled_call(request, mk, call_ref)
            raise
        except (CallFailure, HTTPException) as exc:
            # A call refused for MONEY (402 empty balance / 429 daily cap) is the event the org will
            # ask about first — it must appear in the activity feed, charged 0.
            #
            # Keep the detail, because `cap` alone is not a diagnosis: every 429 maps to it, and that
            # covers a member call cap, a tag call or spend cap, the platform ceiling, a trial
            # allowance and a demo-IP limit. WHICH one is in `exc.detail` and was being discarded —
            # 878 refusals in a week that could not be told apart afterwards. This branch is inside
            # `mk.metered`, so it stays platform-only like every other capture site, and it runs
            # BEFORE relay, so no provider content can reach it.
            #
            # It is NOT free of caller data, though: a tag-cap detail carries the tag's `val` — an
            # end-customer id the builder supplied. That is the caller's own identifier, in the
            # caller's own row, and it is also the thing that makes the refusal diagnosable ("which
            # customer hit the cap"). It is strictly less than the request bodies this feature
            # already retains, and it is bounded by the same redaction and 14-day retention.
            _audit(exc.status_code, charged_micro=0,
                   refused_by="balance" if exc.status_code == 402 else "cap",
                   error_response=(
                       _ERROR_MASKING_FAILED if refusal_secrets is None else
                       _redact_snippet(f"treg: {exc.detail}", refusal_secrets,
                                       _ERROR_RESPONSE_MAX)))
            if isinstance(exc, CallFailure):
                raise _translate_call_failure(exc) from exc
            raise
    body = b""
    response: Response | None = None
    started = _now_ms()
    try:
        # treg keeps oauth tokens fresh: refresh in place if stale, before injecting. Inside the
        # try on purpose — a failed refresh after a reserve must release the hold (502 path below).
        for secret in secrets.values():
            try:
                await oauth.ensure_fresh(secret, db, request.app.state.http)
            except Exception as exc:  # noqa: BLE001 — surface a clear 502 instead of injecting a dead token
                raise HTTPException(status_code=502, detail=f"oauth refresh failed: {exc}")
        # END THE DB PHASE BEFORE NETWORK I/O. From here until the settle this request must hold NO
        # pooled connection. `ledger.reserve` already committed, but the org refresh after it, the
        # secret loads and a token refresh each auto-began a fresh transaction on this session, and
        # SQLAlchemy keeps that transaction's connection checked out until commit — i.e. for the whole
        # upstream round trip. `_platform_settle` then opens its OWN session for a second connection.
        # Two per in-flight call against a 15-slot pool (db.py) deadlocked at 15 concurrent calls: every
        # settle waited on a slot only another waiting call could free, until `pool_timeout` killed one
        # (a bare 500, or a settle that forfeited its charge) and the rest cascaded — every call in a
        # burst "took 30 s" (2026-08-24, reproduced from bootoshi's #9/#10). Nothing below reads `db`
        # (settle, first-call and the idempotent store all run on their own sessions), and the session
        # is `expire_on_commit=False`, so `tool`/`secrets`/`caller.org` stay usable without a reload.
        await db.commit()
        try:
            response = await relay(request, upstream_url, tool, secrets, request.app.state.http,
                                   drop_params=drop_params or None,
                                   force_identity=mk is not None and mk.metered)
            if mk is not None and mk.metered:
                # Metered calls don't stream: settling needs the provider's own reported cost, which is
                # in the body (see _buffer_response). A failure while draining is still an upstream
                # failure, so it becomes a 502 and the hold goes back.
                response, body = await _buffer_response(response)
            elif response.status_code >= 400:
                # Preserve streaming for own-key and own-tool calls while retaining only the small
                # diagnostic head. The replacement response replays every consumed byte verbatim.
                response, body = await _peek_stream_head(response, _ERROR_BODY_SLICE)
        except ValueError as exc:  # a binding/injector mismatch (e.g. non-JSON secret on an oauth binding)
            raise HTTPException(status_code=502, detail=f"credential injection failed: {exc}")
        except httpx.RequestError as exc:  # upstream down/timeout is a gateway fault, not treg's 500
            raise HTTPException(status_code=502, detail=f"upstream request failed: {str(exc) or type(exc).__name__}")
    except asyncio.CancelledError:
        await _finish_cancelled_call(request, mk, call_ref, response)
        raise
    except HTTPException as exc:
        # The provider never produced a billable answer (our own error, a failed injection, an
        # unreachable upstream) → return the hold in full, regardless of the endpoint's billing type.
        metered = mk is not None and mk.metered
        if metered:
            try:
                await _platform_settle(mk, None, reason=f"call_failed_{exc.status_code}")
            except asyncio.CancelledError:
                await _finish_cancelled_call(request, mk, call_ref, response)
                raise
            # The shared exception handler builds the response and adds this zero-cost result.
            request.state.call_cost_micro = 0
        # No provider body exists on this branch. treg's own detail is the explanation instead, and
        # it is the one worth keeping: this branch carries refresh, timeout, injection and SSRF 502s.
        _renderings = _safe_secret_renderings(tool, secrets)
        _audit(exc.status_code, charged_micro=0 if metered else None,
               duration_ms=_now_ms() - started,
               error_request=(
                   _ERROR_MASKING_FAILED if _renderings is None else
                   _caller_request_snippet(request, tool, caller_body, _renderings)),
               error_response=(
                   _ERROR_MASKING_FAILED if _renderings is None else
                   _redact_snippet(f"treg: {exc.detail}", _renderings, _ERROR_RESPONSE_MAX)))
        raise
    except Exception:  # noqa: BLE001 — an unexpected fault is still not the caller's bill
        # The reaper would eventually return this hold anyway; returning it now means a bug in the call
        # path can't make a funded org look broke for the next three minutes.
        if mk is not None and mk.metered:
            try:
                await _platform_settle(mk, None, reason="call_crashed")
            except asyncio.CancelledError:
                await _finish_cancelled_call(request, mk, call_ref, response)
                raise
        raise
    duration_ms = _now_ms() - started
    # First successful call. The common case — an org that already has one — is an in-memory check
    # against `caller.org` (freshly loaded this request by require_member): zero DB cost on a path
    # that runs on every proxied call. Only an org's actual first call touches the database, and it
    # does so via _record_first_call's own session, never the request's `db` (which _platform_settle,
    # right below, is about to settle/release — see its docstring for why that session is off-limits).
    if 200 <= response.status_code < 400 and caller.org_id and caller.org.first_call_at is None:
        try:
            await _record_first_call(caller.org_id)
        except asyncio.CancelledError:
            await _finish_cancelled_call(request, mk, call_ref, response)
            raise
    if mk is not None and mk.metered:
        try:
            charged, observed = await _platform_settle(
                mk, response.status_code, body, headers=response.headers,
                # `provider_failed_`, not `call_failed_`: the latter is the branch above, where treg
                # never got an answer (timeout, SSRF refusal, a failed oauth refresh). Both release a
                # 502 the same way, so a shared prefix would make the two indistinguishable in the
                # journal once the 14-day error evidence expires — and they need different fixes.
                reason=(f"provider_failed_{response.status_code}" if response.status_code >= 500 else ""),
            )
        except asyncio.CancelledError:
            await _finish_cancelled_call(request, mk, call_ref, response)
            raise
        # A relayed non-2xx arrives HERE, as a Response — the vendor's own status is never raised
        # (see _refusal_kind). So this is where the provider's own explanation is captured, and the
        # only place it exists: nothing downstream keeps the body.
        err_request = err_response = None
        if response.status_code >= 400:
            _renderings = _safe_secret_renderings(tool, secrets)
            if _renderings is None:
                err_request = err_response = _ERROR_MASKING_FAILED
            else:
                err_request = _caller_request_snippet(request, tool, caller_body, _renderings)
                err_response = _error_response_evidence(response, body, _renderings)
        _audit(response.status_code, observed_micro=observed, charged_micro=charged,
               duration_ms=duration_ms, response_bytes=len(body),
               error_request=err_request, error_response=err_response)
        if idem_key:
            # Here, and not earlier: this is the first point where BOTH the response and what it
            # actually cost are known, and a replay has to hand back the real charge rather than the
            # estimate that was reserved.
            try:
                await _store_idempotent(idem_key, caller, status_code=response.status_code, body=body,
                                        media_type=response.headers.get("content-type", ""),
                                        charged_micro=charged, metered=True, call_ref=call_ref)
            except asyncio.CancelledError:
                await _finish_cancelled_call(request, mk, call_ref, response)
                raise
            request.state.idem_claim = None      # dealt with; nothing left to release
        # Tell the caller what the call actually cost. Both llms.txt and skill.md instruct an agent to
        # report the price it spent, and until now the only way to find out was to read the balance
        # before and after — which races with any other call and cannot attribute a figure to a
        # request. The header is set only on a METERED call: a team's own key is never charged, and a
        # `0` there would read as "free" rather than "not applicable".
        response.headers["X-Treg-Cost-Micro"] = str(charged)
        response.headers["X-Treg-Call-Id"] = call_ref
        return response
    # Fire-and-forget audit — does not block the streaming response (rule #2). A failed unmetered
    # call has already yielded just enough response bytes to retain redacted evidence; successes
    # still take the untouched streaming path.
    err_request = err_response = None
    if response.status_code >= 400:
        _renderings = _safe_secret_renderings(tool, secrets)
        if _renderings is None:
            err_request = err_response = _ERROR_MASKING_FAILED
        else:
            err_request = _caller_request_snippet(request, tool, caller_body, _renderings)
            err_response = _error_response_evidence(response, body, _renderings)
    _audit(response.status_code, duration_ms=duration_ms,
           error_request=err_request, error_response=err_response)
    if idem_key:
        # Unmetered: nothing was billed, so there is nothing to protect. Dropping the claim frees the
        # label at once instead of making the caller wait out the window to reuse it.
        try:
            await _store_idempotent(idem_key, caller, status_code=response.status_code, body=b"",
                                    media_type="", charged_micro=0, metered=False)
        except asyncio.CancelledError:
            await _finish_cancelled_call(request, mk, call_ref, response)
            raise
        request.state.idem_claim = None
    response.headers["X-Treg-Call-Id"] = call_ref
    return response
