"""The API — the only brain. CLI + skill are thin clients over this (charter).

Surface: open user registration (creates a personal org) + per-membership token auth; full CRUD
on secrets and tools; the /skills composer (register a whole skill = bundle + its secrets + its
tool(s) atomically) and /bundles reads; the /call proxy with a fire-and-forget audit record; and
/calls. A tool carries a LIST of bindings (multi-credential), with flat single-binding sugar on POST.

Multi-tenancy: a token = a (user, org) Membership. Every list/create/mutation and the proxy are
scoped to the caller's org; `owner` (creator email) drives the member-vs-admin role gate.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import html as _html
from functools import lru_cache
import hmac
import html as html_mod
import json
import logging
import os
import re
import secrets as _secrets
import shutil
import tempfile
import time
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlsplit, urlunsplit

from sqlalchemy import case, delete, func, or_, text, update

import httpx
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, Form, Header, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlmodel import select

from . import adsconv, agent_pages, analytics, audit, billing, catalog_store, crypto, demo as demo_seed, email as email_sender, health, injectors, ledger, localrun, oauth
from . import oauth_providers
from . import pubfeed, ratestore, reconcile, referrals, runner, sandbox as demo_sandbox
from .config import get_settings, platform_setting_name
from .bootstrap_http import (
    _BODY_ENC_HEADER,
    _BodyDecodeMiddleware,
    _decode_request_body,
    _LEGACY_HOSTS,
    _LegacyHostRedirectMiddleware,
    _REDIRECT_ALWAYS,
    _REDIRECT_PATHS,
    _SecurityHeadersMiddleware,
)
from .caller_metadata import (
    TAG_DEFAULT,
    _MAX_BUDGET_DIMS,
    _META_KEY_RE,
    _client_of,
    _norm_client,
)
from .db import get_session, session_maker
from .domain.identity import session as sess
from .domain.identity.access import (
    AGENT_DOMAIN,
    PUBLIC_DEMO_DOMAIN,
    Caller,
    _can_manage,
    _is_agent_email,
    _is_machine_email,
    _membership_by_token,
    _norm_email,
    _resolve_org,
    _role_at_least,
    _require_can_register,
    _user_from_identity_token,
    _user_from_session,
    require_identity,
    require_member,
    require_superadmin,
)
from .domain.identity.mcp_oauth import (
    REFRESH_TTL_S,
    _ensure_grant,
    _family_org,
    _issue_refresh,
    _refresh_is_live,
    _revoke_refresh_family,
)
from .models import (ROLE_RANK, AdConversion, Bundle, CallRecord, CapabilityPin, CreditBlock,
                     DenyRule, Hold, IdempotentCall, Invite, LedgerEntry, Membership, OAuthClient,
                     OAuthCode, OAuthGrant, OAuthRefresh, Org, PendingOAuth, Project, Referral,
                     RunRecord, Secret, TagBudget, TagSpend, Tool, ToolRequest, User)
from .proxy import relay
from .bootstrap_handlers import _mark_treg_own_errors, _pool_saturated
from .routers import admin as admin_routes
from .routers import billing as billing_routes
from .routers import call as call_routes
from .routers import referrals as referral_routes
from .routers import onboard as onboard_routes
from .routers.call import (
    _parse_call_meta,
    _refusal_kind,
    _release_idempotent_claim,
    _stamp_call_exit,
    call_tool,
    catalog_endpoint_access,
)
from .routers.billing import (
    AutoTopupIn,
    TopupIn,
    _billing_org,
    _return_base,
    billing_autotopup,
    billing_get,
    billing_history,
    billing_portal,
    billing_stripe_webhook,
    billing_topup,
    org_balance,
)
from .routers.referrals import mint_referral_code, my_referrals
from .routers.onboard import (
    OnboardIn,
    SANDBOX_HIT_NS,
    SANDBOX_RATE_MAX,
    SANDBOX_RATE_WINDOW_S,
    TeammateIn,
    demo_sandbox_live,
    demo_sandbox_mint,
    demo_sandbox_skill,
    landing_stripe_feed,
    onboard_accept_teammate,
    onboard_demo,
    onboard_reset,
    onboard_seed_tool,
    onboard_skip,
    skill_install,
    skill_samples,
    stripe_webhook,
)
from .routers.admin import (
    BoolIn,
    _ERROR_EVIDENCE_EXPIRED,
    _ERROR_EVIDENCE_TTL_DAYS,
    _purge_expired_error_evidence,
    _is_last_active_superadmin,
    _tally,
    admin_calls,
    admin_errors,
    admin_health,
    admin_delete_org,
    admin_delete_user,
    admin_org_detail,
    admin_orgs,
    admin_reconcile_drift,
    admin_reconcile_repeats,
    admin_reconcile_spend,
    admin_referrals,
    admin_stats,
    admin_set_superadmin,
    admin_suspend_org,
    admin_suspend_user,
    admin_tools,
    admin_users,
)
from .routers import catalog as catalog_routes
from .routers.catalog import (
    _observed_or_empty,
    _platform_rows,
    _provider_display,
    catalog_endpoint,
    catalog_example,
    catalog_platform,
    catalog_platforms,
    catalog_search,
)
from .routers import auth as auth_routes
from .routers.auth import (
    CLI_APPROVE_MAX_TRIES,
    CLI_TOKEN_TTL,
    HANDSHAKE_TTL,
    EMAIL_CODE_TTL,
    MAX_OTP_ATTEMPTS,
    OTP_NS,
    OTP_START_MAX_PER_EMAIL,
    OTP_START_MAX_PER_IP,
    OTP_START_NS,
    OTP_START_WINDOW_S,
    CliApproveIn,
    EmailStartIn,
    EmailVerifyIn,
    GrantTeamIn,
    OAuthClientRegistration,
    _AUTH_HEAD,
    _CONSENT_CSS,
    _auth_page,
    _authorize_request,
    _consent_page,
    _finish_oauth_login,
    _intercom_user_hash,
    _login_callback_base,
    _LOGIN_CSS,
    _LOGIN_ID_RE,
    _LOGIN_JS,
    _PAIR_ALPHABET,
    _cli_pending,
    _cli_results,
    _cli_states,
    _client_ip,
    _find_or_create_user,
    _live_invite_by_email_token,
    _login_page_html,
    _norm_pair_code,
    _orgs_brief,
    _oauth_error,
    _prune_handshakes,
    _refresh_grant,
    _resolve_oauth_client,
    _same_mcp_resource,
    _wrong_resource,
    AUTH_CODE_TTL_S,
    auth_cli_token,
    auth_cli_approve,
    auth_cli_orgs,
    auth_cli_poll,
    auth_cli_start,
    auth_email_start,
    auth_email_verify,
    auth_github,
    auth_github_callback,
    auth_google,
    auth_google_callback,
    auth_invite_signin,
    auth_invite_signin_confirm,
    auth_logout,
    auth_me,
    auth_revoke_tokens,
    login_page,
    oauth_authorization_server,
    oauth_authorize,
    oauth_authorize_approve,
    oauth_grant_set_team,
    oauth_grants,
    oauth_protected_resource,
    oauth_register,
    oauth_revoke,
    oauth_token,
    openai_apps_challenge,
)
from .application.signup import (
    _ad_attribution_from,
    _grant_signup_promo,
    _redeem_referral,
    _stamp_utm,
    _utm_attribution_from,
)
from .application.connect import (
    CATALOG_STAMP_CAP,
    _autoprovision_provider_tool,
    _backfill_provider_extra_tools,
    _dig,
    _free_connection_name,
    _enrich_resource_labels,
    _owned_connection,
    _provider_bindings,
    _provider_tool_examples,
    _record_connected_identity,
    _upsert_provider_extra_tools,
)
from .routers import connections as connection_routes
from .routers.connections import (
    ExtraCredentialIn,
    OAuthStartIn,
    ResourceRefIn,
    TokenConnectIn,
    connection_resources,
    connect_with_token,
    get_health,
    list_connections,
    oauth_callback,
    oauth_providers_list,
    oauth_start,
    oauth_status,
    revoke_connection,
    run_health,
    set_connection_resource,
    set_extra_credential,
)
from .domain.governance.teams import _make_org_membership, _slugify, _unique_slug
from .domain.governance import access as access_policy
from .domain.governance import budgets as budget_policy
from .domain.governance import publicdemo as publicdemo_policy
from .application.call.idempotency import (
    IDEMPOTENCY_HEADER,
    IDEMPOTENCY_WINDOW_S,
    _IDEM_MAX_KEY,
    _IDEM_SCOPE_SEP,
    _claim_idempotent,
    _idem_display,
    _idempotency_key,
    _replay_idempotent,
    _request_fingerprint,
    _scoped_idempotency_key,
    _store_idempotent,
)
from .application.call.intake import (
    META_HEADER,
    _META_MAX_HEADER,
    CallMeta,
    _NO_META,
    _tag_telemetry,
)
from .routers import orgs as org_routes
from .routers.orgs import (
    INVITE_TTL_DAYS,
    AcceptIn,
    AccessIn,
    AgentIn,
    CapIn,
    DenyRuleIn,
    InviteIn,
    OrgIn,
    OrgSettingsIn,
    PROXY_METHODS,
    ProjectIn,
    RoleIn,
    TagBudgetIn,
    UserIn,
    _LANDING_RE,
    _ORG_SCOPED_MODELS,
    _cascade_delete_org,
    _count_owners,
    _day_start_utc,
    _deny_match,
    _deny_view,
    _drop_member_deny_rules,
    _enforce_deny,
    _known_access_names,
    _known_tool_names,
    _normalize_project_access,
    _normalize_tool_access,
    _org_deny_rules,
    _agent_email,
    _agent_name,
    _public_demo_email,
    _project_view,
    _require_admin_of,
    _require_owner_of,
    _resolve_project,
    _tag_budget_view,
    _usage_rollup,
    _used_today_by_user,
    accept_invite,
    accept_my_invite,
    agent_checkin,
    create_agent,
    create_deny_rule,
    create_invite,
    create_org,
    create_project,
    create_public_token,
    count_today,
    delete_org,
    delete_deny_rule,
    delete_project,
    delete_public_token,
    delete_tag_budget,
    get_org_settings,
    leave_org,
    list_invites,
    list_agents,
    list_cli_deny,
    list_deny_rules,
    list_members,
    list_observed_agents,
    list_orgs,
    list_projects,
    list_tag_budgets,
    list_tag_keys,
    my_usage,
    my_invites,
    org_usage,
    register_user,
    remove_member,
    revoke_agent,
    revoke_invite,
    set_member_access,
    set_member_cap,
    set_member_role,
    set_org_settings,
    set_tag_budget,
    set_tag_default,
    usage_by_tag,
)
from .routers import resources as resources_routes
from .routers.resources import (
    BundleUpdate,
    SecretIn,
    SecretUpdate,
    SkillAnalyzeIn,
    SkillFileIn,
    SkillImportIn,
    SkillIn,
    SkillSecretIn,
    SkillToolIn,
    ToolIn,
    ToolUpdate,
    _SKILL_UPLOAD_MAX_BYTES,
    _SKILL_UPLOAD_MAX_FILES,
    _SKILL_UPLOAD_MAX_TOTAL_BYTES,
    _SECRET_DIR_RE,
    _allowed_server_bins,
    _bundle_allowed,
    _bundle_view,
    _check_upload_size,
    _flat_binding,
    _host_of,
    _materialize_skill_files,
    _normalize_scheme,
    _register_skill_bundle,
    _require_not_live_demo_secret,
    _require_not_live_demo_tool,
    _require_public_base_url,
    _require_secret_ownership,
    _secret_view,
    _sanitize_bundle_files,
    _scan_uploaded_skills,
    _tool_view,
    _validate_bindings,
    _validate_bundle_id,
    _validate_cli_profile,
    _validate_cli_secrets,
    _visible_secret_ids,
    create_secret,
    create_tool,
    analyze_skill_folder,
    delete_secret,
    delete_bundle,
    delete_tool,
    get_bundle,
    get_bundle_by_name,
    get_tool_by_name,
    import_skill_folder,
    list_bundles,
    list_secrets,
    list_tools,
    register_skill,
    update_bundle,
    update_secret,
    update_tool,
)
from .routers.auth_helpers import (
    OAUTH_RETURN_COOKIE,
    _is_https,
    _remember_oauth_return,
    _same_origin,
    _take_oauth_return,
)
from .routers.signup_cookies import (
    REFERRAL_COOKIE,
    REFERRAL_COOKIE_MAX_AGE,
    _remember_referral,
    _take_referral,
)

from .routers import web as web_routes
from .routers.web import (
    LOCAL_USER_EMAIL,
    _LOGO_DIR,
    _MEDIA_DIR,
    _TOUR_DIR,
    _VENDOR_DIR,
    _WEB_DIR,
    _esc_html,
    _local_owner,
    _provider_rows,
    _related_link,
    _usd_short,
    _use_case_page_for,
    use_case_job_page,
)
from .timeutil import as_naive as _as_naive
from .timeutil import utcnow_naive as _utcnow_naive


LOCAL_ORG_NAME = "personal"


async def _bootstrap_single_user() -> None:
    """Frictionless local mode: make the machine's owner exist, so `curl … | sh` lands on a dashboard
    that is already signed in — no account, no email, no password.

    Idempotent, and the token is STABLE across restarts (rotating it every boot would break the CLI
    config the installer just wrote). It is re-minted only when the token file is missing, i.e. the
    user deleted it and needs a new one. Gated by `single_user_ok`, which refuses anything that isn't
    a local sqlite box — see config.Settings.
    """
    s = get_settings()
    if not s.single_user_ok:
        return
    path = Path(s.single_user_token_file).expanduser()
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == LOCAL_USER_EMAIL))).scalar_one_or_none()
        if user is None:
            user = User(email=LOCAL_USER_EMAIL, onboarded=True)
            db.add(user)
            await db.flush()
        # Adopt an org ONLY through a membership this identity already has. Looking one up by the
        # slug `personal` and joining it as owner would, on a database that is not fresh, hand the
        # password-less local identity ownership of a team that belongs to someone else — and an
        # owner is exempt from every ACL. A new team therefore takes a FREE slug (`personal-2`, …)
        # rather than colliding with whatever already holds `personal`.
        membership = (await db.execute(
            select(Membership).where(Membership.user_id == user.id).order_by(Membership.id)
        )).scalars().first()
        token = ""
        if membership is None:
            org = Org(name=LOCAL_ORG_NAME.title(), slug=await _unique_slug(LOCAL_ORG_NAME, db))
            db.add(org)
            await db.flush()
            token = crypto.new_token()  # first boot
            membership = Membership(user_id=user.id, org_id=org.id, role="owner",
                                    token_hash=crypto.hash_token(token))
            db.add(membership)
        else:
            org = await db.get(Org, membership.org_id)
            if not path.exists():
                token = crypto.new_token()  # the token file was removed — mint a replacement
                membership.token_hash = crypto.hash_token(token)
        team = org.slug if org is not None else LOCAL_ORG_NAME
        await db.commit()
    if token:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token)
        path.chmod(0o600)  # the installer reads it; nobody else should
    shown = token or "(unchanged — see " + str(path) + ")"
    print(f"\n  treg is ready — no account needed."
          f"\n  Dashboard  {s.public_url}/app"
          f"\n  Team       {team}"
          f"\n  Token      {shown}\n", flush=True)


# Route definitions stay in this module until refactor stage 2. `bootstrap.create_app` consumes this
# router after import and owns every concrete assembly decision around it.
router = APIRouter()
app = router  # temporary decorator target; replaced by the compatibility FastAPI app at EOF


# The pre-treg.to hostnames must keep answering the API forever — every installed CLI, skill.md
# and .mcp.json in the wild points here with a Bearer token, and most HTTP clients STRIP the
# Authorization header when a redirect crosses hosts (and some MCP clients follow no redirects at
# all). So only browser-facing marketing pages redirect to the canonical host; everything else —
# /call/, /mcp/, auth flows, webhooks, agent-fetched pages like /vendor-listing, install scripts
# fetched by `curl | sh` without -L — is served in place on both hosts.
async def _id_out_of_range(request: Request, exc: OverflowError) -> JSONResponse:
    # A huge all-digit path param (e.g. /secrets/999…) overflows SQLite's 64-bit INTEGER at bind
    # time; that's a non-existent id, not a server fault — surface a 404 instead of a 500.
    return JSONResponse({"detail": "identifier out of range"}, status_code=404)









_app_version_cache: tuple[float, str] | None = None  # (index.html mtime, content hash)


@lru_cache(maxsize=1)
def _treg_version() -> str:
    """The released package version, read from installed metadata.

    Read directly rather than through `cli.cli_version`, which does the same thing: importing
    `treg.cli` here costs ~200ms on first call and pulls the entire CLI into the server process for
    one string. Cached because package metadata cannot change while the process runs.
    """
    try:
        from importlib.metadata import version

        return version("tools-registry")
    except Exception:  # noqa: BLE001 — an editable/source run has no installed metadata
        return "dev"


def _app_version() -> str:
    """A stamp that changes with every deploy of the dashboard bundle: a hash of index.html,
    re-derived when the file's mtime moves (so dev --reload picks up edits too). Long-lived tabs
    compare this against the value they booted with and offer a refresh when it drifts."""
    global _app_version_cache
    index = _WEB_DIR / "index.html"
    try:
        mtime = index.stat().st_mtime
    except OSError:
        return "dev"
    if _app_version_cache is None or _app_version_cache[0] != mtime:
        digest = hashlib.sha256(index.read_bytes()).hexdigest()[:12]
        _app_version_cache = (mtime, digest)
    return _app_version_cache[1]


@app.get("/meta")
async def meta() -> dict:
    """Open: what the dashboard needs to render correct, shareable snippets — the public proxy URL
    (so copy/paste snippets use the real domain, not whatever origin the browser happens to be on)
    — plus the bundle version, so an open tab can detect a new deploy and offer a refresh.

    `treg_version` and `app_version` answer DIFFERENT questions and both are worth having.
    `app_version` is a hash of index.html: it changes whenever the dashboard bundle does, which is
    what an open tab compares to offer a refresh. `treg_version` is the released package version,
    which is what a release check needs — after publishing 0.9.0 there was no way to confirm from the
    live path which version was actually serving, only the commit id.
    """
    s = get_settings()
    return {"public_url": s.public_url.rstrip("/"), "github": bool(s.github_client_id),
            "google": bool(s.google_client_id), "app_version": _app_version(),
            "treg_version": _treg_version(),
            # public ingestion key — only present when this deployment opts in (self-hosters send nothing)
            "posthog_key": s.posthog_key, "posthog_host": s.posthog_host.rstrip("/") if s.posthog_key else "",
            # public workspace id — only present when this deployment opts in (self-hosters load no widget)
            "intercom_app_id": s.intercom_app_id}


@app.get("/providers.json", include_in_schema=False)
async def providers_catalog() -> dict:
    """Open: the provider catalog `treg upload` uses to detect env keys → tools. Served so the CLI can
    refresh it centrally (add a provider here → every CLI picks it up) with its bundled copy as fallback.
    See [env-import](../docs/context/interface/env-import.md)."""
    from . import providers as prov
    return {"version": prov.CATALOG_VERSION, "providers": prov.CATALOG}


# Register the moved Catalog routes at their original position.
router.routes.extend(catalog_routes.public_router.routes)

# Register the moved Catalog-page routes at their original position.
router.routes.extend(web_routes.catalog_pages_router.routes)

# ---- "the catalog doesn't have X" — tool requests -------------------------------------------
TOOLREQ_HIT_NS = "toolreq"
TOOLREQ_RATE_MAX = 10          # filings per IP per window
TOOLREQ_RATE_WINDOW_S = 3600   # 1 hour
TOOLREQ_SOURCES = {"web", "cli", "mcp", "api"}


class ToolRequestIn(BaseModel):
    capability: str          # what they wanted — "Ahrefs backlinks", "flight prices", a provider name
    query: str = ""          # the catalog search that came up empty (agents auto-fill this)
    note: str = ""
    contact: str = ""        # optional reach-back; free text, unverified
    source: str = "web"      # web | cli | mcp | api


@app.post("/tool-requests", include_in_schema=False)
async def create_tool_request(
    body: ToolRequestIn,
    request: Request,
    x_treg_token: str = Header(default=""),
    treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Open: file a "the catalog doesn't have X" report. No auth on purpose — the filer is most
    often an agent that just got zero search results and holds no token, and a signup wall here
    costs exactly the demand signal the catalog team wants. Per-IP rate limiting (ratestore) is
    the abuse valve, same shape as POST /demo/sandbox; field caps bound the row.

    Identity is attribution, never authorization: when the caller happens to be signed in (token
    or same-origin session), the row records who asked so they can be told when it lands — a
    forged cross-origin cookie POST gets stored as anonymous, not rejected, hence the
    `_same_origin` gate on the cookie path only."""
    await ratestore.sweep(db, TOOLREQ_HIT_NS)
    if not await ratestore.rate_check(db, TOOLREQ_HIT_NS,
                                      [(_client_ip(request), TOOLREQ_RATE_MAX)], TOOLREQ_RATE_WINDOW_S):
        await db.commit()  # persist the sweep even on reject
        raise HTTPException(status_code=429, detail="too many tool requests from here — try again later")
    capability = body.capability.strip()
    if not capability:
        raise HTTPException(status_code=422, detail="say what tool/capability you need")
    if len(capability) > 200:
        raise HTTPException(status_code=422, detail="capability is a headline — keep it under 200 chars")
    org_id, user_email = None, ""
    if x_treg_token:
        m = await _membership_by_token(x_treg_token, db)
        user = await db.get(User, m.user_id) if m else await _user_from_identity_token(x_treg_token, db)
        if user is not None and not user.suspended:
            org_id, user_email = (m.org_id if m else None), user.email
    elif treg_session and _same_origin(request):
        user = await _user_from_session(treg_session, db)
        if user is not None:
            user_email = user.email
    row = ToolRequest(
        org_id=org_id,
        user_email=user_email,
        capability=capability,
        query=body.query.strip()[:300],
        note=body.note.strip()[:2000],
        contact=body.contact.strip()[:200],
        source=body.source if body.source in TOOLREQ_SOURCES else "api",
    )
    db.add(row)
    await db.commit()
    return {"id": row.id, "status": "received",
            "note": "logged — requests steer which provider gets keyed next"}


# Register the moved social-login routes at their original position.
router.routes.extend(auth_routes.social_router.routes)


# Register the moved CLI pairing routes at their original position.
router.routes.extend(auth_routes.cli_router.routes)


# Register the moved session identity routes at their original position.
router.routes.extend(auth_routes.session_router.routes)


# Register the moved email OTP routes at their original position.
router.routes.extend(auth_routes.email_router.routes)


router.routes.extend(auth_routes.invite_router.routes)


# Register the moved site routes at their original position.
router.routes.extend(web_routes.site_router.routes)

router.routes.extend(auth_routes.oauth_server_router.routes)


# Register the moved public-document routes at their original position.
router.routes.extend(web_routes.public_docs_router.routes)

# ---- caller auth (token = a Membership; open registration) --------------------------------
router.routes.extend(auth_routes.token_router.routes)


def _require_tool_use_http(caller: Caller, tool: Tool) -> None:
    try:
        access_policy._require_tool_use(caller, tool)
    except access_policy.AccessPolicyError as exc:
        raise HTTPException(status_code=403, detail=exc.detail) from exc






def _require_local_run(caller: Caller) -> None:
    """Gate the LOCAL run tier on the member's `local_run_enabled` (owner exempt). Off → server only."""
    if caller.role != "owner" and not caller.membership.local_run_enabled:
        raise HTTPException(status_code=403, detail=(
            "local execution is disabled for you — run on the server instead (`treg run --server`), "
            "or ask an admin to enable local runs for your account"))


# ---- schemas ------------------------------------------------------------------------------










class GrantIn(BaseModel):
    argv: list[str] = []  # the CLI args the member is about to run (deny-checked + audited)


class RunReportIn(BaseModel):
    audit_id: int      # the grant's audit row — proves this report follows a real grant
    exit_code: int
    verdict: str       # ok | credential_invalid | unknown_error (client matched stderr locally)










router.routes.extend(org_routes.signup_router.routes)


# ---- orgs, invites, members (multi-tenancy management) ------------------------------------


def _now_ms() -> int:
    """A monotonic millisecond stamp for measuring a call's duration — never the wall clock, which can
    step backwards (NTP) and produce a negative latency."""
    return int(time.monotonic() * 1000)




router.routes.extend(auth_routes.grants_router.routes)


router.routes.extend(org_routes.org_entry_router.routes)


router.routes.extend(org_routes.invite_entry_router.routes)


router.routes.extend(onboard_routes.onboard_entry_router.routes)


async def _enforce_public_demo_ip_cap(request: Request, db: AsyncSession) -> None:
    try:
        await publicdemo_policy.enforce_public_demo_ip_cap(_client_ip(request), db)
    except publicdemo_policy.PublicDemoLimitError as exc:
        await db.commit()
        raise HTTPException(status_code=429, detail=exc.detail) from exc
    await db.commit()


# ---- per-user daily usage cap (usage-metering v1) -------------------------------------------




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






router.routes.extend(onboard_routes.sandbox_router.routes)


router.routes.extend(onboard_routes.onboard_teammate_router.routes)


router.routes.extend(org_routes.invite_management_router.routes)


router.routes.extend(org_routes.member_list_router.routes)


router.routes.extend(org_routes.org_usage_router.routes)


router.routes.extend(billing_routes.balance_router.routes)


router.routes.extend(org_routes.tag_controls_router.routes)


router.routes.extend(billing_routes.billing_router.routes)


router.routes.extend(referral_routes.router.routes)


router.routes.extend(billing_routes.webhook_router.routes)


router.routes.extend(org_routes.member_management_router.routes)


router.routes.extend(org_routes.machine_identity_router.routes)


# ---- projects: an optional sub-scope inside an org ------------------------------------------
router.routes.extend(org_routes.projects_router.routes)


# ---- deny rules: org policy over what may be called ----------------------------------------








class CapabilityPinIn(BaseModel):
    capability: str
    provider: str


@app.get("/orgs/{org_id}/pins")
async def list_capability_pins(
    org_id: int, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> list[dict]:
    """The team's provider choices, per capability. Readable by any member — an agent has to know
    what it is allowed to call, and finding out by being refused is a wasted round-trip."""
    if caller.org_id != org_id:      # the token IS the membership (same rule as every org route)
        raise HTTPException(status_code=403, detail="not a member of this org")
    rows = (await db.execute(select(CapabilityPin).where(CapabilityPin.org_id == org_id)
                             .order_by(CapabilityPin.capability))).scalars().all()
    return [{"id": r.id, "capability": r.capability, "provider": r.provider,
             "created_by": r.created_by, "created_at": r.created_at} for r in rows]


@app.post("/orgs/{org_id}/pins")
async def set_capability_pin(
    org_id: int, body: CapabilityPinIn,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Pin a capability to one provider for the whole team (admin+). Re-pinning replaces.

    Both halves are validated against the catalog: an unknown capability, or a provider that does
    not actually serve it, would be a rule that silently blocks every call to a job the team
    genuinely uses — a typo must fail here, loudly, not at 3am in an agent's log."""
    _require_admin_of(org_id, caller)
    cat = catalog_store.load()
    serving = cat.for_capability(body.capability)
    if not serving:
        raise HTTPException(status_code=422, detail=f"unknown capability {body.capability!r}")
    providers = sorted({e["provider"] for e in serving})
    if body.provider not in providers:
        raise HTTPException(status_code=422, detail=(
            f"{body.provider!r} does not serve {body.capability!r} — "
            f"these do: {', '.join(providers)}"))
    # Read the caller's email NOW, as a plain string. A rollback below expires every ORM instance
    # behind `caller`, and touching one afterwards lazy-loads outside the async context —
    # MissingGreenlet, which is how this first failed under concurrency.
    who = caller.email
    row = (await db.execute(select(CapabilityPin).where(
        CapabilityPin.org_id == org_id,
        CapabilityPin.capability == body.capability))).scalars().first()
    if row is None:
        row = CapabilityPin(org_id=org_id, capability=body.capability, provider=body.provider,
                            created_by=who)
        db.add(row)
    else:
        row.provider, row.created_by = body.provider, who
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race to another admin (or another web worker). The UNIQUE index is what actually
        # prevents the duplicate; this makes losing look like the sequential path — re-apply onto
        # the winner's row rather than handing back a 500 for a pin that plainly succeeded.
        await db.rollback()
        row = (await db.execute(select(CapabilityPin).where(
            CapabilityPin.org_id == org_id,
            CapabilityPin.capability == body.capability))).scalars().first()
        if row is None:
            raise
        row.provider, row.created_by = body.provider, who
        await db.commit()
    return {"capability": body.capability, "provider": body.provider,
            "alternatives": [p for p in providers if p != body.provider]}


@app.delete("/orgs/{org_id}/pins")
async def clear_capability_pin(
    org_id: int, capability: str = Query(..., min_length=1, max_length=200),
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Remove a pin (admin+) — the capability goes back to the caller's choice.

    The capability is a QUERY parameter, not a path segment, and that is a safety decision rather
    than a style one. As `/orgs/{id}/pins/{capability}` it was one keystroke from a catastrophe:
    every normalizing HTTP client (httpx included) rewrites `/orgs/1/pins/..` to `/orgs/1` BEFORE
    sending it — which is DELETE /orgs/{id}, the delete-the-team route. `treg org unpin ..` really
    did destroy an org in testing. Server-side validation cannot defend against it, because the
    rewrite happens in the client; taking the value out of the path removes the class entirely."""
    _require_admin_of(org_id, caller)
    rows = (await db.execute(select(CapabilityPin).where(
        CapabilityPin.org_id == org_id, CapabilityPin.capability == capability))).scalars().all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"no pin for {capability!r}")
    for row in rows:      # all of them, in case a duplicate predates the unique index
        await db.delete(row)
    await db.commit()
    return {"capability": capability, "pinned": False}


router.routes.extend(org_routes.policy_router.routes)


router.routes.extend(resources_routes.crud_router.routes)




# ---- tools --------------------------------------------------------------------------------






















# ---- local runs (`treg run`): grant + outcome report (docs/CLI-RUN-PLAN.md) -----------------
# Redact obvious credentials a user might type INLINE (`treg run x -- --token sk_live_…`) before the
# argv is persisted to the audit log — known key prefixes, any high-entropy token, JWTs, AND the value
# that follows a credential-looking flag (so a SHORT password like `--password hunter2` is masked too).
_ARGV_SECRET_RE = re.compile(
    r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|glpat|AKIA|ASIA|AIza|xox[baprs])[A-Za-z0-9_\-]{6,}\b"
    # JWT (base64url with dots). `\b` and the POSSESSIVE `++` are load-bearing, not tidying: without
    # the anchor, input like "eyJeyJeyJ…" offers a fresh start position every three characters and
    # each one scans forward for a `.`, which is quadratic. Anchoring leaves one start; `++` removes
    # backtracking within an attempt (it cannot change what matches here — the class excludes `.`,
    # so the run always ends at the first one). Same shape guards the argv rule below.
    r"|\beyJ[A-Za-z0-9_\-]++\.[A-Za-z0-9_.\-]{8,}"
    r"|\b[A-Za-z0-9_\-]{24,}\b")  # any 24+ high-entropy run — deliberately over-masks (git SHAs, UUIDs)
                                  # since in an audit log a false mask is harmless but a real key isn't
_CRED_FLAG = r"--?(?:token|password|passwd|pass|pwd|api[-_]?key|secret|auth|bearer|credential)s?"
_CRED_FLAG_EQ_RE = re.compile(rf"({_CRED_FLAG})=\S+", re.I)
_CRED_FLAG_BARE_RE = re.compile(rf"^{_CRED_FLAG}$", re.I)


def _redact_argv_list(argv: list[str]) -> list[str]:
    """Per-element redaction that also masks the element FOLLOWING a bare credential flag."""
    out: list[str] = []
    mask_next = False
    for a in argv:
        if mask_next:
            out.append("***"); mask_next = False; continue
        if _CRED_FLAG_BARE_RE.match(a):          # `--password` `hunter2` → mask the value that follows
            out.append(a); mask_next = True; continue
        a = _CRED_FLAG_EQ_RE.sub(r"\1=***", a)   # `--password=hunter2`
        out.append(_ARGV_SECRET_RE.sub("***", a))
    return out


def _redact_argv(argv: list[str]) -> str:
    return " ".join(_redact_argv_list(argv))[:500]






async def _grant_audit(db: AsyncSession, caller: Caller, tool_name: str, method: str, path: str,
                       status: int, client: str = "") -> int:
    """A SYNCHRONOUS audit row (unlike record_call): the grant returns its audit id so the
    run-report can prove it follows a real grant. One insert; this is not the hot proxy path."""
    rec = CallRecord(org_id=caller.org_id, user_email=caller.email, tool_name=tool_name,
                     method=method, path=path[:500], status_code=status, kind="local_run",
                     client=client)
    db.add(rec)
    await db.commit()
    return rec.id


@app.post("/tools/{name}/grant")
async def grant_local_run(
    name: str,
    body: GrantIn,
    request: Request,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Mint the process material for ONE local run of this tool's CLI: the audited, owner-opt-in
    exception to "values are never returned". OAuth secrets release only the expiring leaf; the
    deny check happens here, where the secret lives. Unlike /call (which injects server-side and
    leaks nothing), a grant HANDS the credential value to the caller's machine — so it needs member+
    (a viewer may call but not extract). Loosening this to a per-tool run ACL is a future policy knob."""
    _require_can_register(caller)
    from . import providers as prov
    tool = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id, Tool.name == name))).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    _require_tool_use_http(caller, tool)  # per-member tool + project ACL (call + both run tiers)
    _require_local_run(caller)               # local tier may be disabled for this member (server-only)
    await _enforce_deny(caller, tool.base_url, "", db, tool.project_id)  # host-level policy (see run_tool_server)
    await _enforce_daily_cap(caller, db)  # a local run counts toward the per-user daily cap
    catalog_cli = (prov.match_skill(tool.name) or {}).get("cli")
    profile = localrun.effective_profile(tool, catalog_cli)
    if profile is None:
        raise HTTPException(status_code=409, detail=(
            f"treg doesn't know how to inject credentials into {tool.name!r}. Add a \"cli\" block to the "
            'skill\'s treg.json — template: {"cli": {"bin": "' + tool.name + '", '
            '"inject": [{"secret": "<local secret name>", "via": "env", "name": "<ENV_VAR>"}]}}'))
    if profile.get("unsupported"):
        raise HTTPException(status_code=409, detail=f"{tool.name}: {profile.get('reason', 'this CLI cannot be injected')}")
    if not profile.get("enabled"):
        raise HTTPException(status_code=403, detail=(
            f"local runs are disabled for {tool.name!r} — an owner/admin can enable them: "
            f"treg tool update {tool.name} --local-run on"))
    denied = localrun.check_deny(profile, body.argv)
    if denied:
        pattern, source = denied
        await _grant_audit(db, caller, tool.name, "DENY", _redact_argv(body.argv), 403, _client_of(request))
        raise HTTPException(status_code=403, detail=(
            f"denied by {source}: pattern {pattern!r}. The skill's creator controls this list "
            "(cli.deny in treg.json)."))
    # Runner-proof gate (Bug 1). Handing a member a secret they do NOT own — a shared-key tool they may
    # RUN but not SEE — is allowed only for the isolated treg-run runner, which proves itself with a
    # value the member can't read (`X-Treg-Run-Proof`). A direct member call has no proof, so the raw
    # value never reaches the member's eyes. Owned secrets (or an admin) skip this — you can read a key
    # you already hold.
    inject_sids = {localrun._resolve_secret_id(e, tool) for e in profile.get("inject") or []}
    needs_proof = False
    if not _role_at_least(caller.role, "admin"):
        for sid in (s for s in inject_sids if s is not None):
            sec = await db.get(Secret, sid)
            if sec is not None and sec.owner != caller.email:
                needs_proof = True
                break
    if needs_proof:
        proof = get_settings().run_proof
        supplied = request.headers.get("X-Treg-Run-Proof", "")
        if not (proof and hmac.compare_digest(supplied, proof)):
            await _grant_audit(db, caller, tool.name, "DENY", _redact_argv(body.argv), 403, _client_of(request))
            raise HTTPException(status_code=403, detail=(
                "this tool uses another member's key — running it needs the isolated treg-run runner "
                "(an admin sets it up once: `sudo treg setup-local-run --run-proof …`). A direct grant "
                "can't expose someone else's key value to you."))
    try:
        rendered = await localrun.render_grant(tool, profile, db, request.app.state.http)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — a failed oauth refresh must read clearly, like /call
        raise HTTPException(status_code=502, detail=f"oauth refresh failed: {exc}")
    audit_id = await _grant_audit(db, caller, tool.name, "GRANT", _redact_argv(body.argv), 200, _client_of(request))
    warnings = list(profile.get("warnings") or [])
    ttl = rendered["ttl_seconds"]
    if ttl is not None and ttl <= 0:
        warnings.append("the injected token appears already expired — the run will likely fail; "
                        "an owner may need to reconnect it (treg oauth connect)")
    elif ttl is not None:
        warnings.append(f"the injected token expires in ~{max(1, ttl // 60)} min — "
                        "long-running commands may outlive it")
    return {
        "bin": profile.get("bin", tool.name),
        "inject": rendered["items"],  # delivery-tagged items — the client applies each (env/argv/broker)
        "ttl_seconds": rendered["ttl_seconds"],
        "install": profile.get("install"),
        "noninteractive": profile.get("noninteractive") or [],
        "warnings": warnings,
        "errors": profile.get("errors") or [],
        # Scrub the injected value from the CLI's output when the member doesn't OWN the key (a shared
        # key run through the isolated runner) — so a CLI feature (`gh auth token`, an env dump) can't be
        # used to print it back. Owned/admin runs skip it (you may see your own key) and keep a raw TTY.
        "redact_output": needs_proof,
        "audit_id": audit_id,
    }


@app.post("/tools/{name}/run-report")
async def report_local_run(
    name: str,
    body: RunReportIn,
    caller: Caller = Depends(require_member),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """The client's post-run verdict (it matches stderr against the grant's error patterns LOCALLY
    and sends only this enum — raw output never leaves the machine). credential_invalid flips the
    granted secrets to invalid via the same health fields the runner uses."""
    _require_can_register(caller)  # marking a credential invalid is a register-tier action, not a read
    if body.verdict not in localrun.VERDICTS:
        raise HTTPException(status_code=422, detail=f"verdict must be one of {localrun.VERDICTS}")
    grant_rec = await db.get(CallRecord, body.audit_id)
    # Bind the report to the SAME user who received the grant — otherwise a member could invalidate
    # another user's secrets (a DoS) by guessing a sequential audit_id.
    if (grant_rec is None or grant_rec.org_id != caller.org_id or grant_rec.method != "GRANT"
            or grant_rec.tool_name != name or grant_rec.user_email != caller.email):
        raise HTTPException(status_code=404, detail="no matching grant for that audit_id")
    tool = (await db.execute(select(Tool).where(Tool.org_id == caller.org_id, Tool.name == name))).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    marked: list[str] = []
    if body.verdict == "credential_invalid":
        from . import providers as prov
        profile = localrun.effective_profile(tool, (prov.match_skill(tool.name) or {}).get("cli")) or {}
        # Mark only the credentials this run actually INJECTED (the ones the CLI used) — not every HTTP
        # binding — and never a `param` (it's config, not a credential; mirrors health.run_all's guard).
        sids = {localrun._resolve_secret_id(e, tool) for e in profile.get("inject") or []}
        now = _utcnow_naive()
        for sid in [s for s in sids if s is not None]:
            secret = await db.get(Secret, sid)
            if secret is not None and secret.org_id == caller.org_id and secret.kind != "param":
                secret.health_status = "invalid"
                secret.health_detail = f"local run of {tool.name} reported an auth failure (exit {body.exit_code})"
                secret.health_checked_at = now
                marked.append(secret.name)
    await _grant_audit(db, caller, tool.name, "REPORT", f"exit={body.exit_code} verdict={body.verdict}", 200)
    return {"ok": True, "marked_invalid": marked}


# ---- skills (bundle composer): register a whole skill atomically --------------------------
# ---- skills: analyze / import an uploaded folder (the dashboard mirror of `treg upload skills`) ----
router.routes.extend(resources_routes.skill_router.routes)
# ---- audit read ---------------------------------------------------------------------------
@app.get("/calls")
async def list_calls(
    limit: int = 50, days: int | None = None, before_id: int | None = None,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """This team's recent calls. `days` windows it and `before_id` pages backwards — a builder
    reconciling a month cannot do it through a newest-first limit alone.

    Analytics, NOT an invoice: these rows are written fire-and-forget and the queue sheds them under
    load. Money comes from `/orgs/{id}/usage/by-tag`, which reads the ledger.
    """
    limit = max(1, min(limit, 500))
    # The failure-evidence columns are not in the response below and are not fetched either: they are
    # the two wide columns on this table, this endpoint returns up to 500 rows, and a column nobody
    # reads should not cross the wire. Deferring also means adding them to the payload later has to be
    # a deliberate edit in two places, not an accident in one.
    q = (select(CallRecord)
         .options(defer(CallRecord.error_request), defer(CallRecord.error_response))
         .where(CallRecord.org_id == caller.org_id))
    if days is not None:
        q = q.where(CallRecord.created_at >= _day_start_utc() - timedelta(days=max(1, min(days, 365)) - 1))
    if before_id is not None:
        q = q.where(CallRecord.id < before_id)
    rows = (await db.execute(q.order_by(CallRecord.id.desc()).limit(limit))).scalars().all()
    return [
        {
            "id": c.id,
            "user_email": c.user_email,
            "tool_name": c.tool_name,
            "method": c.method,
            "path": c.path,
            "status_code": c.status_code,
            "kind": c.kind,
            "client": c.client,
            # Marketplace telemetry — all null for a plain tool call (see models.CallRecord). Kept in
            # the same row a caller already reads, so "what did this cost me" needs no second endpoint.
            "endpoint_id": c.endpoint_id,
            "provider": c.provider,
            "credential_tier": c.credential_tier,
            "cost_estimated_micro": c.cost_estimated_micro,
            "cost_observed_micro": c.cost_observed_micro,
            "cost_charged_micro": c.cost_charged_micro,
            "duration_ms": c.duration_ms,
            "response_bytes": c.response_bytes,
            "params_hash": c.params_hash,
            # non-null = treg said no before anything went upstream (see models.CallRecord) — the
            # one field that tells "the provider failed" apart from "we refused" in `treg audit`.
            "refused_by": c.refused_by,
            # The caller's own tags (X-Treg-Meta), for a builder reconciling this row against their
            # records. Money is NOT invoiced from here — see the ledger-backed usage endpoint.
            "call_ref": c.call_ref,
            "budget_dim": c.budget_dim,
            "budget_val": c.budget_val,
            "tags": c.tags,
            "created_at": c.created_at.isoformat(),
        }
        for c in rows
    ]


@app.get("/calls/{call_ref}")
async def get_call(
    call_ref: str, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> dict:
    """One call by the `X-Treg-Call-Id` it returned — the join key for a builder's own records.

    Also reports the LEDGER's view of the same reference, because that is the durable one: the audit
    row is fire-and-forget and may have been shed, while the money entries were written synchronously.
    A 404 here therefore means "no audit row", not "this call never happened" — check `ledger` in the
    body before concluding anything about money.
    """
    row = (await db.execute(select(CallRecord).where(
        CallRecord.org_id == caller.org_id, CallRecord.call_ref == call_ref))).scalars().first()
    entries = (await db.execute(select(LedgerEntry).where(
        LedgerEntry.org_id == caller.org_id, LedgerEntry.call_id == call_ref)
        .order_by(LedgerEntry.created_at))).scalars().all()
    if row is None and not entries:
        raise HTTPException(status_code=404, detail="no call with that id")
    view = None
    if row is not None:
        view = {"id": row.id, "call_ref": row.call_ref, "user_email": row.user_email,
                "tool_name": row.tool_name, "method": row.method, "path": row.path,
                "status_code": row.status_code, "kind": row.kind, "client": row.client,
                "endpoint_id": row.endpoint_id, "provider": row.provider,
                "credential_tier": row.credential_tier,
                "cost_estimated_micro": row.cost_estimated_micro,
                "cost_observed_micro": row.cost_observed_micro,
                "cost_charged_micro": row.cost_charged_micro,
                "duration_ms": row.duration_ms, "response_bytes": row.response_bytes,
                "refused_by": row.refused_by, "budget_dim": row.budget_dim,
                "budget_val": row.budget_val, "tags": row.tags,
                "created_at": row.created_at.isoformat() if row.created_at else None}
    return {
        "call": view,
        "ledger": [{"kind": e.kind, "amount_micro": e.amount_micro, "endpoint_id": e.endpoint_id,
                    "created_at": e.created_at.isoformat() if e.created_at else None}
                   for e in entries],
        "charged_micro": sum(-e.amount_micro for e in entries if e.kind == "settle"),
    }


@app.get("/runs")
async def list_runs(
    limit: int = 50, caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Audit log for CLI executions (`treg run`, both tiers), scoped to the caller's org — each row
    tagged `where`: "server" (RunRecord) or "local" (a `local_run` GRANT on the member's machine).
    Local successes carry no exit code (only failures report back), so `exit_code` is null for them.
    Ids are prefixed (s/l) so the two sources never collide as list keys."""
    limit = max(1, min(limit, 500))
    server = (await db.execute(
        select(RunRecord).where(RunRecord.org_id == caller.org_id)
        .order_by(RunRecord.id.desc()).limit(limit)
    )).scalars().all()
    # A local run is audited as its GRANT (kind="local_run"); the redacted argv lives in `path`.
    local = (await db.execute(
        select(CallRecord).where(
            CallRecord.org_id == caller.org_id, CallRecord.kind == "local_run",
            CallRecord.method == "GRANT")
        .order_by(CallRecord.id.desc()).limit(limit)
    )).scalars().all()
    rows = [
        {"id": f"s{r.id}", "user_email": r.user_email, "tool": r.bundle_name,  # bundle_name = tool (historical)
         "argv": r.argv, "exit_code": r.exit_code, "duration_ms": r.duration_ms,
         "where": "server", "client": r.client, "created_at": r.created_at.isoformat()}
        for r in server
    ] + [
        {"id": f"l{c.id}", "user_email": c.user_email, "tool": c.tool_name,
         "argv": (c.path or "").split(), "exit_code": None, "duration_ms": None,
         "where": "local", "client": c.client, "created_at": c.created_at.isoformat()}
        for c in local
    ]
    rows.sort(key=lambda x: x["created_at"], reverse=True)
    return rows[:limit]


# ---- OAuth connect flow (Phase C): mint the first token via browser consent --------------














router.routes.extend(connection_routes.oauth_router.routes)






router.routes.extend(connection_routes.resources_router.routes)


















router.routes.extend(connection_routes.token_router.routes)




router.routes.extend(connection_routes.management_router.routes)




router.routes.extend(connection_routes.status_router.routes)


# ---- credential health (Phase B): validate all creds + alert owners ----------------------
router.routes.extend(connection_routes.health_router.routes)




# ---- super-admin: cross-tenant read + control (env token OR is_superadmin user) -----------


# Register the moved admin read routes at their original position.
router.routes.extend(admin_routes.reads_router.routes)

router.routes.extend(admin_routes.mutations_router.routes)


router.routes.extend(admin_routes.reports_router.routes)

# ---- the proxy: call a tool without holding its credential --------------------------------
async def _resolve_call(rest: str, caller: Caller, db: AsyncSession) -> tuple[Tool, str]:
    """Resolve `/call/<rest>` to (tool, full upstream URL), scoped to the caller's org. Shapes:

    - URL-passthrough (agent-facing): rest is the real upstream URL. Resolve the tool by host
      (indexed) + longest base_url prefix — the caller types no treg vocabulary, just the API.
    - Named (CLI/legacy): rest = "<tool-name>/<upstream-path>".

    Both lookups are constrained to `org_id`, so two orgs resolve independently (and may reuse
    a tool name or an upstream host without colliding).

    Passthrough candidates are additionally filtered by the caller's ACL (project scope AND the
    per-tool list) *before* the longest-prefix tiebreak. That ordering matters: a same-host tool the
    caller cannot use must not be able to cause a 409 — or win the tiebreak — for someone who can't
    even see it in `list_tools`. This narrows the candidate set, so it can never grant access: whatever
    resolves still passes the access-policy gate. The named shape needs no filter (it resolves one tool).
    """
    org_id = caller.org_id
    norm = _normalize_scheme(rest)
    if norm.startswith("http://") or norm.startswith("https://"):
        try:
            host = urlsplit(norm).netloc.lower()
        except ValueError:  # malformed passthrough URL (e.g. unbalanced IPv6 brackets) → 400, not 500
            raise HTTPException(status_code=400, detail="malformed upstream URL")
        on_host = (await db.execute(
            select(Tool).where(Tool.host == host, Tool.org_id == org_id)
        )).scalars().all()
        candidates = [t for t in on_host if access_policy._tool_usable(caller, t)]  # can't use it → can't 409 on it
        # Match on a path-segment boundary, not a raw string prefix: base `.../v2` must NOT match
        # request `.../v20/...` (that would inject v2's credential onto an unregistered sibling path).
        def _prefix_match(base: str) -> bool:
            b = base.rstrip("/")
            return norm == b or norm.startswith(b + "/")

        matches = [t for t in candidates if _prefix_match(t.base_url)]
        if not matches:
            # Tell "no such tool" and "not yours to use" apart. If the ACL filter above is the ONLY
            # reason nothing matched, this is a 403 like the named shape would give — a 404 here
            # would send an admin hunting for a registration that already exists. The message names
            # the HOST the caller already typed, never the internal tool name the ACL hides.
            if any(_prefix_match(t.base_url) for t in on_host):
                raise HTTPException(status_code=403, detail=(
                    f"you don't have access to the registered tool for {host!r} in this team — an "
                    "admin can grant it (dashboard → Team, or `treg org access <you> …`)"))
            raise HTTPException(status_code=404, detail=f"no registered tool for upstream {host!r}")
        # Tiebreak on the NORMALIZED length so `.../v1` and `.../v1/` count equal (a real 409), not
        # one silently "longer" than the other.
        longest = max(len(t.base_url.rstrip("/")) for t in matches)
        top = [t for t in matches if len(t.base_url.rstrip("/")) == longest]
        if len(top) > 1:
            # A hand-registered tool for the same API (often predating the OAuth registry, and
            # frequently holding a stale credential) collides on host with the one connect
            # auto-provisioned. Both are real tools, so neither base_url is "longer" — but they are
            # not equally intended: the registry-provisioned one is the live connection the user
            # just authorised, and URL-passthrough is the AGENT-facing mode, so 409-ing here breaks
            # exactly the callers who never typed a tool name. Prefer the provider-backed tool.
            provider_owned = []
            for t in top:
                sids = {b.get("secret_id") for b in (t.bindings or []) if b.get("secret_id") is not None}
                for sid in sids:
                    s = await db.get(Secret, sid)
                    if s is not None and s.org_id == org_id and s.provider:
                        provider_owned.append(t)
                        break
            if len(provider_owned) == 1:
                return provider_owned[0], norm
            names = ", ".join(repr(t.name) for t in sorted(top, key=lambda t: t.name))
            raise HTTPException(status_code=409, detail=(
                f"ambiguous: multiple tools match {host!r}: {names}; call one by name as "
                "/call/<name>/<path>"))
        return top[0], norm

    name, _, path = rest.partition("/")
    tool = (
        await db.execute(select(Tool).where(Tool.name == name, Tool.org_id == org_id))
    ).scalar_one_or_none()
    if tool is None:
        cat = catalog_store.load()
        # A DOTTED name that reached here was meant to be a catalog endpoint id and missed — a
        # near-miss id, most often one segment off. Answering "no tool 'lusha.companies-signals' in
        # this org" describes the wrong half of treg and leaves the caller nothing to try; naming
        # the real id turns the dead end back into the next call.
        if (name not in cat.by_id and "." in name and not path
                and (near := catalog_store.near_ids(name, cat))):
            raise HTTPException(status_code=404, detail={
                "error": f"no endpoint {name!r} in the catalog",
                "hint": "did you mean " + ", ".join(near) + "?",
                "did_you_mean": near})
        detail = f"no tool {name!r} in this org"
        # A caller may have mistaken a catalog-looking operation for a path on the connected own
        # tool. Look only at callable tools inside this org and only on the error path; the first
        # dotted segment is the provider/tool convention (`google-analytics.report` →
        # `google-analytics`). Connection
        # suffixes also count, so an org whose surviving account is `google-analytics-2` still gets
        # an actionable route. Keep catalog_store.near_ids above provider-local and unchanged.
        own_tools = (await db.execute(
            select(Tool).where(Tool.org_id == org_id)
        )).scalars().all()
        first_segment = name.partition(".")[0]
        own_near = sorted({
            t.name for t in own_tools
            if access_policy._tool_usable(caller, t) and (
                name.startswith(t.name + ".")
                or t.name == first_segment
                or t.name.startswith(first_segment + "-")
            )
        }, key=lambda candidate: (-len(candidate), candidate))
        if own_near:
            suggested = own_near[0]
            raise HTTPException(status_code=404, detail={
                "error": detail,
                "hint": (f"your org has tool {suggested!r} — call "
                         f"/call/{suggested}/<path>"),
                "did_you_mean": own_near,
            })
        # A bare provider name (`treg call tikhub /path`) stays a miss, but points at the
        # marketplace form instead of dead-ending — its endpoints are callable without a tool.
        if oauth_providers.get(name) is not None or name in cat.provider_meta:
            detail += (f" — but {name!r} is a marketplace provider; call its endpoints directly: "
                       f"treg catalog search <what you need> → treg call <endpoint-id>")
        raise HTTPException(status_code=404, detail=detail)
    base = tool.base_url.rstrip("/")
    # No path → the base URL itself, WITHOUT a trailing slash: a base pinned to a full resource
    # (e.g. .../v1/charges) must relay as-is — Stripe 404s `/v1/charges/`.
    return tool, (f"{base}/{path.lstrip('/')}" if path else base)


# ---- direct marketplace calls: `treg call <catalog-endpoint-id>`, no tool registration ----------
# See docs/context/interface/cli-audit-2026-07-28.md (design section). The registry stays "our
# stuff"; the catalog is "everything callable". Credential ladder: (1) an org tool bound to the
# provider — resolved via the URL-passthrough shape, so ACLs and ambiguity handling are identical —
# then (2) an org credential matching the provider, injected via a VIRTUAL tool that is never
# persisted (no registry pollution), then (4) TREG'S OWN key for the provider, metered against the
# org's prepaid balance — the keyless first call — and only then (3) an actionable error naming the
# connect/secret fix.
#
# Tier 4 is the only rung that spends OUR money, so it is fenced on every side: the endpoint must be
# `platform_eligible` (priced, price-provenanced, live-verified, not the caller's own account's
# business — see catalog_store.platform_eligible), the provider must be allow-listed AND keyed
# (config.platform_key_for — the kill switch), the org must not be a demo, the estimated cost is
# RESERVED from the balance before the request leaves, and a per-org daily ceiling caps the damage a
# runaway agent can do. The key itself only ever exists as a `platform_setting` NAME in a virtual
# tool's bindings; `relay` reads the value from settings at call time, so no platform credential is
# stored, listable, or reachable from a local run.

def _catalog_endpoint_for(rest: str) -> dict | None:
    """The catalog endpoint `rest` names, or None. Only a dotted, slash-free rest can be an
    endpoint id, so tool names and URL/named shapes never reach the catalog lookup."""
    if "/" in rest or "." not in rest or rest.startswith("http"):
        return None
    return catalog_store.load().by_id.get(rest)


def _enforce_catalog_status(ep: dict) -> None:
    """Refuse a catalog id the provider has retired or broken, with its migration story.

    This runs only after `_resolve_call` has failed and `_catalog_endpoint_for` has identified a
    real catalog id. A team's own tool with the same name therefore still wins, and URL-passthrough
    calls never enter this path at all.
    """
    status = str(ep.get("status") or "").strip().lower()
    if not status:
        return
    detail = f"{ep['id']} is {status}"
    if note := str(ep.get("status_note") or "").strip():
        detail += f": {note}"
    if successor := str(ep.get("superseded_by") or "").strip():
        detail += f" Use {successor} instead."
    elif alternatives := _capability_alternatives(ep):
        # 41 of the 50 TikHub retirements have no same-provider successor, so `superseded_by` has
        # nothing to say for them. A cross-provider sibling is the only help left — and it is the
        # difference between a tombstone and a migration path.
        detail += " " + " ".join(alternatives)
    else:
        detail += " No replacement is currently catalogued."
    raise HTTPException(status_code=410, detail=detail)


async def _marketplace_secret(service: str, org_id: int, db: AsyncSession) -> Secret | None:
    """Tier 2's credential: an org secret tagged with this provider (registry connects), else one
    NAMED exactly for it (`treg secret add tikhub …`). Newest wins — a reconnect supersedes."""
    tagged = (await db.execute(
        select(Secret).where(Secret.org_id == org_id, Secret.provider == service)
        .order_by(Secret.id.desc())
    )).scalars().first()
    if tagged is not None:
        return tagged
    return (await db.execute(
        select(Secret).where(Secret.org_id == org_id, Secret.name == service)
        .order_by(Secret.id.desc())
    )).scalars().first()


@dataclass
class MarketplaceCall:
    """One resolved catalog-endpoint call: where it goes, who paid for the credential, and — when
    treg's own key is paying — what the ledger is holding for it. `call_tool` carries this from
    resolution through the relay to the settle and the telemetry row, so the endpoint id and the
    credential tier are recorded even when the call fails."""

    tool: Tool                      # real (tier 1) or virtual + never persisted (tiers 2/4)
    upstream: str
    consumed: set[str]              # query params eaten by `{placeholder}` path substitution
    endpoint_id: str
    provider: str
    tier: str                       # tool | credential | platform
    cost_type: str = ""             # cost.type — decides whether a 4xx is billable (per_call is)
    estimate_micro: int = 0         # RAW provider estimate; the ledger applies the margin
    params_hash: str = ""
    call_id: str | None = None      # the ledger hold, once reserved (metered calls only)
    # The call rides a REGISTRY OAUTH CONNECT of a provider that bills treg's app per use (X's
    # pay-per-use: the app owner pays whoever's token made the call). Orthogonal to `tier` — the
    # credential is genuinely the org's own (tier 1/2), but the upstream bill is ours, so the call
    # is metered anyway. Set by `_billed_marketplace` after the bound secrets are known.
    billed_oauth: bool = False
    unit_micro: int = 0             # RAW per-resource price for a per_result settle-by-count

    @property
    def metered(self) -> bool:
        """True when OUR money is at stake: treg's platform key (tier 4), or an org credential that
        rides treg's pay-per-use OAuth app (`billed_oauth`). Tiers 1/2 on a provider that bills the
        account owner stay unmetered — there the org's own account pays."""
        return self.tier == "platform" or self.billed_oauth


# A `per_result` price is per ROW, so an estimate needs a row count. The caller's own limit param is
# the best available signal; without one, assume a page. Capped, because `limit=100000` must not be
# able to reserve an org's whole balance for a single call — the settle corrects the estimate either way.
_PLATFORM_PAGE_DEFAULT = 20
_PLATFORM_PAGE_MAX = 100
_LIMIT_PARAMS = ("limit", "count", "depth", "page_size", "per_page", "num", "max_results", "size")


def _body_limit(body: bytes) -> int | None:
    """A row-count signal from a JSON body: an explicit limit key first (dataforseo takes
    `[{..., "limit": 3}]`, lusha `{"limit": 1}`), else the ARRAY LENGTH — providers that take a
    list of inputs (brightdata's urls, dataforseo's tasks) bill one result per item, so a 1-item
    body estimating at the 20-row default overstated 20x (seen live: $0.03 shown for a $0.0015
    call). Under-estimating is safe either way — the settle trues up, overruns included."""
    if not body:
        return None
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    items = None
    if isinstance(doc, list) and doc:
        items = len(doc)
        doc = doc[0]
    if not isinstance(doc, dict):
        return items
    for name in _LIMIT_PARAMS:
        val = doc.get(name)
        if isinstance(val, int) and not isinstance(val, bool) and val > 0:
            return val
    return items


def _platform_estimate_micro(cost: dict, query, body: bytes = b"") -> int:
    """What one call is expected to cost the platform, in RAW micro-USD (no margin — ledger.reserve
    applies that). Rounds UP: a fraction of a micro-dollar is not representable and must not round to
    free. Returns 0 for a genuinely free endpoint, which reserves nothing."""
    usd = cost.get("usd")
    if usd is None:
        return 0
    n = 1
    if cost.get("type") in ("per_result", "quota_rows"):
        asked = None
        for name in _LIMIT_PARAMS:
            raw = query.get(name)
            if raw is not None and str(raw).strip().isdigit():
                asked = int(str(raw).strip())
                break
        if asked is None:
            asked = _body_limit(body)  # POST providers put the row count in the body, not the query
        n = max(1, min(asked or _PLATFORM_PAGE_DEFAULT, _PLATFORM_PAGE_MAX))
    # Round to 9 dp BEFORE the ceil: float artifacts (0.0015 × 3 → 4500.000000001) must not
    # over-reserve a phantom micro-dollar.
    raw_micro = round(usd * n * 1_000_000, 9)
    whole = int(raw_micro)
    return whole + 1 if raw_micro > whole else whole


# ---- oauth-billed metering: providers whose upstream bill lands on treg's app -------------------
# X moved to pay-per-use (Feb 2026): the APP OWNER is billed per resource read / per post written,
# whoever's user token made the call. A registry connect rides treg's app, so those calls spend
# treg's prepaid credits and must be metered against the org's balance — the same reserve→settle
# path as tier 4. A BYO connect (/oauth/start with the caller's own client_id) stores
# `secret.provider == ""` and is therefore never flagged: its upstream bill is already the org's.

def _usd_to_micro(usd: float) -> int:
    """USD → RAW micro-USD, rounded UP like `_platform_estimate_micro` — a fraction of a
    micro-dollar must not round to free."""
    raw = round(usd * 1_000_000, 9)
    whole = int(raw)
    return whole + 1 if raw > whole else whole


def _truthy(value) -> bool:
    """Provider query/body booleans arrive as strings or JSON booleans; interpret both."""
    return value is True or (isinstance(value, str) and value.strip().lower() in ("1", "true", "yes"))


def _json_object(body: bytes) -> dict:
    try:
        doc = json.loads(body) if body else {}
    except (ValueError, UnicodeDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _input_count(doc: dict, keys: tuple[str, ...]) -> int:
    """Count request records without mistaking field-selection arrays for billable inputs."""
    sizes = [len(doc[k]) for k in keys if isinstance(doc.get(k), list)]
    return max(sizes, default=1)


def _credit_modifiers(cost: dict, query, doc: dict) -> tuple[bool, float, float, float]:
    """Return (free, reserve add, settle add, add per requested result) from catalog rules.

    The request SHAPE stays provider-aware, but every credit NUMBER stays in the provider YAML.
    A documented but live-unbilled rider can stay in the safety hold with `reserve_only: true`.
    This prevents a rate-card edit from leaving hardcoded arithmetic in the billing path.
    """
    free, added, settled_added, per_result = False, 0.0, 0.0, 0.0
    modifiers = cost.get("modifiers")
    if not isinstance(modifiers, dict):
        return free, added, settled_added, per_result
    for name, rule in modifiers.items():
        if not isinstance(rule, dict):
            continue
        location = rule.get("location", "query")
        if location == "query":
            values = [query.get(name)]
        elif location == "body":
            values = [doc.get(name)]
        elif location == "lookups":
            lookups = doc.get("lookups") if isinstance(doc.get("lookups"), list) else []
            values = [item.get(name) for item in lookups if isinstance(item, dict)]
        else:
            continue
        when = rule.get("when", "truthy")
        matches = (any(value not in (None, "") for value in values)
                   if when == "present" else any(_truthy(value) for value in values))
        if not matches:
            continue
        if rule.get("set_credits") == 0:
            free = True
        if isinstance(rule.get("add_credits"), (int, float)):
            added += float(rule["add_credits"])
            if not rule.get("reserve_only"):
                settled_added += float(rule["add_credits"])
        if isinstance(rule.get("add_credits_per_result"), (int, float)):
            per_result += float(rule["add_credits_per_result"])
    return free, added, settled_added, per_result


def _marketplace_pricing(
    provider: str, endpoint_id: str, cost: dict | None, query, body: bytes
) -> tuple[int, int]:
    """Return (reserve estimate, response-count unit), in raw micro-USD.

    The catalog remains the price source. This helper only models provider rules that one fixed
    scalar cannot express: Crustdata batch-shaped single calls and Aviato preview/add-on/bulk modes.
    `unit` is non-zero only when the response must decide the final charge.
    """
    if not cost:
        return 0, 0
    estimate = _platform_estimate_micro(cost, query, body)
    unit = (_usd_to_micro(cost["usd"])
            if cost.get("type") in ("per_result", "quota_rows") and cost.get("usd") else 0)
    if provider == "crustdata" and endpoint_id in (
        "crustdata.companies.enrich", "crustdata.people.enrich"
    ):
        doc = _json_object(body)
        count = _input_count(doc, (
            "domains", "names", "professional_network_profile_urls", "business_emails"
        ))
        return _usd_to_micro(float(cost.get("usd") or 0) * count), unit
    if provider != "aviato":
        return estimate, unit

    rate = catalog_store.load().credit_rates.get("aviato")
    if not rate:
        return estimate, unit
    def credit_micro(credits):
        return _usd_to_micro(float(credits) * rate)

    doc = _json_object(body)
    free, added, settled_added, per_result = _credit_modifiers(cost, query, doc)
    if free:
        return 0, 0
    credits = float(cost.get("value") or 0) + added
    settled_credits = float(cost.get("value") or 0) + settled_added
    if endpoint_id in ("aviato.companies.enrich.bulk", "aviato.people.enrich.bulk"):
        lookups = doc.get("lookups") if isinstance(doc.get("lookups"), list) else []
        per_record = credit_micro(credits)
        return per_record * max(1, len(lookups)), credit_micro(settled_credits)
    if per_result:
        raw = query.get("perPage")
        asked = int(raw) if raw is not None and str(raw).isdigit() else _PLATFORM_PAGE_DEFAULT
        asked = max(1, min(asked, _PLATFORM_PAGE_MAX))
        # The documented rider stays in the safety hold. A catalog `settle: base` rule can release
        # it after the response when multi-row balance evidence proves that the provider did not
        # charge or deliver the add-on.
        return credit_micro(credits + asked * per_result), 0
    if cost.get("modifiers"):
        settle_unit = credit_micro(settled_credits) if added != settled_added else 0
        return credit_micro(credits), settle_unit
    return estimate, 0


def _oauth_billed_provider(secrets: dict[int, Secret]):
    """The flagged OAuthProvider whose registry connect this call's bindings ride, or None.
    Three gates: the secret is a REGISTRY connect (`secret.provider` is only ever set by the
    callback of a provider-mode /oauth/start — BYO connects carry ""), the registry entry says the
    upstream bills treg's app (`platform_billed`), and this deployment opted into charging
    (`TREG_OAUTH_BILLED_PROVIDERS`, the kill switch — empty keeps today's free behavior)."""
    billed = get_settings().oauth_billed_set
    if not billed:
        return None
    for s in secrets.values():
        if s.kind == "oauth" and s.provider and s.provider in billed:
            p = oauth_providers.get(s.provider)
            if p is not None and p.platform_billed:
                return p
    return None


def _billed_endpoint_match(service: str, method: str, path: str) -> dict | None:
    """The catalog endpoint a URL-passthrough call to `path` lands on, or None. Exact-path entries
    win over templated ones ({id} → one segment), so `/2/users/me` matches the own-account read and
    not `/2/users/{id}`. Purely for pricing + telemetry — never for routing."""
    best, best_placeholders = None, 99
    for ep in catalog_store.load().by_id.values():
        if ep.get("provider") != service or (ep.get("method") or "GET").upper() != method:
            continue
        template = ep.get("path") or "/"
        placeholders = template.count("{")
        if placeholders >= best_placeholders:
            continue
        pattern = re.sub(r"\{\w+\}", "[^/]+", re.escape(template).replace(r"\{", "{").replace(r"\}", "}"))
        if re.fullmatch(pattern, path):
            best, best_placeholders = ep, placeholders
    return best


def _post_has_link(body: bytes) -> bool:
    """Whether a write body's `text` carries a URL — X prices those at `billed_write_link_usd`
    (13x a plain post). Sniffs only the text field, not the whole body, so a quote-post id or a
    docs URL in some other field can't inflate the price."""
    if not body:
        return False
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    text = doc.get("text") if isinstance(doc, dict) else None
    return bool(isinstance(text, str) and re.search(r"https?://|www\.", text))


def _oauth_billed_estimate(provider, ep: dict | None, method: str, query, body: bytes) -> tuple[int, str, int]:
    """What this oauth-billed call is expected to cost, RAW micro-USD (ledger applies the margin)
    → (estimate_micro, cost_type, unit_micro). A priced catalog entry wins (the curated x.yaml
    carries per-endpoint rates: own-account reads are 5x cheaper, user lookups 2x dearer than the
    default); the provider-level rates cover the extended/passthrough long tail. `unit_micro` is
    the per-resource price a `per_result` settle counts the response against."""
    cv = catalog_store.load().cost_view(ep.get("cost"), provider.service) if ep and ep.get("cost") else None
    # A ZERO price must fall through to the provider rate, not bill zero — on an oauth-billed
    # provider the upstream charges us whatever the catalog says, so `free` there is a catalog bug
    # (a stale ingest), never a fact. Spelled out because it used to ride on `0.0` being falsy:
    # the same expression read as "no price recorded" and "the price is nothing", and the catalog
    # could publish free while the balance was debited the fallback.
    if cv and cv.get("type") != "free" and cv.get("usd"):
        ctype = str(cv.get("type") or "per_call")
        est = _platform_estimate_micro(cv, query, body)
        if method != "GET" and provider.billed_write_link_usd and _post_has_link(body):
            est = max(est, _usd_to_micro(provider.billed_write_link_usd))
        return est, ctype, (_usd_to_micro(cv["usd"]) if ctype in ("per_result", "quota_rows") else 0)
    if method == "GET":
        rate = provider.billed_read_usd
        est = _platform_estimate_micro({"type": "per_result", "usd": rate}, query, body)
        return est, "per_result", _usd_to_micro(rate)
    if provider.billed_write_link_usd and _post_has_link(body):
        return _usd_to_micro(provider.billed_write_link_usd), "per_call", 0
    return _usd_to_micro(provider.billed_write_usd), "per_call", 0


async def _billed_marketplace(
    mk: MarketplaceCall | None, provider, tool: Tool, upstream_url: str, request: Request
) -> MarketplaceCall:
    """Flag (or, for a URL-passthrough call, build) the `MarketplaceCall` that meters an
    oauth-billed relay. The catalog id shape arrives with an `mk` (tier 1/2 — keep its endpoint id
    and telemetry identity); the passthrough shape gets one made here, priced off the catalog
    entry its path lands on so both shapes pay the same price for the same route."""
    body = await request.body() if _may_have_body(request) else b""
    method = request.method.upper()
    if mk is None:
        path = urlsplit(upstream_url).path or "/"
        ep = _billed_endpoint_match(provider.service, method, path)
        endpoint_id = ep["id"] if ep else f"{provider.service}.passthrough"
        mk = MarketplaceCall(
            tool=tool, upstream=upstream_url, consumed=set(), endpoint_id=endpoint_id,
            provider=provider.service, tier="tool",
            params_hash=_params_hash(endpoint_id, request.query_params.multi_items(), body))
    else:
        ep = catalog_store.load().by_id.get(mk.endpoint_id)
    est, ctype, unit = _oauth_billed_estimate(provider, ep, method, request.query_params, body)
    mk.billed_oauth, mk.estimate_micro, mk.cost_type, mk.unit_micro = True, est, ctype, unit
    return mk


def _params_hash(endpoint_id: str, query_items: list[tuple[str, str]], body: bytes) -> str:
    """An identity for "this exact call again": sha256 over the endpoint id, the ORDER-INDEPENDENT
    query, and a digest of the body. The body itself is never stored or logged — only its hash — so
    this is safe to keep forever and is the future cache key (plan phase 5, repeat-rate measurement)."""
    h = hashlib.sha256()
    h.update(endpoint_id.encode("utf-8", "replace"))
    for k, v in sorted(query_items):
        h.update(b"\x1f" + f"{k}={v}".encode("utf-8", "replace"))
    h.update(b"\x1e" + (hashlib.sha256(body).digest() if body else b""))
    return h.hexdigest()


def _platform_bindings(provider) -> list[dict]:
    """Tier 4's injection: the SAME header/param shape a pasted key of this provider gets
    (`_provider_bindings`), except the value is named rather than carried — `relay` reads
    `platform_setting` from settings at call time. That is the whole security model: treg's key is
    never written to a Secret row (unreadable by the tenant, unexportable by a local run, and
    `api.py`'s cross-org secret check would reject it anyway)."""
    setting = platform_setting_name(provider.service)
    if provider.token_location == "query":
        bindings = [{"platform_setting": setting, "injector": "env", "location": "query",
                     "name": provider.token_param, "format": provider.token_format}]
    else:
        bindings = [{"platform_setting": setting, "injector": "env", "location": "header",
                     "name": provider.token_header, "format": provider.token_format}]
    # Keep tier 4 protocol-identical to BYOK. Required provider headers are constants, but they
    # still use the same platform setting reference so the normal binding validator and injector
    # own the whole shape. Crustdata's x-api-version pin is the first provider that needs this.
    source = {k: v for k, v in bindings[0].items()
              if k in ("platform_setting", "injector", "secret_field")}
    bindings.extend({**source, "location": "header", "name": name, "format": value}
                    for name, value in provider.required_headers)
    # A per-user credential PAIR (Tomba's key+secret headers) needs treg's own second half on
    # tier 4. platform_extra_setting is tier-4-only by design: extra_credential_setting would also
    # ride user connects, pairing a user's key with treg's secret — a pair the provider rejects.
    if provider.needs_extra_credential and provider.platform_extra_setting:
        bindings.append({"platform_setting": provider.platform_extra_setting, "injector": "env",
                         "location": "header", "name": provider.extra_credential_header,
                         "format": "{secret}"})
    return bindings


def _platform_offer(ep: dict, provider, org: Org) -> dict | None:
    """May tier 4 serve `ep` for this org, and at what price? The cost view when yes, None when no.

    Every clause is a refusal we WANT to be boring: an unpriced/unknown-confidence price
    (`platform_eligible`), a provider nobody enabled (`platform_key_for` — key AND allow-list), an
    OAuth provider (a platform key is meaningless for one: the credential is a user's own account),
    or a demo org (the sandbox and the public demo must never be able to spend real money — the
    landing page is reachable by anyone with the URL)."""
    if not provider.uses_pasted_secret:
        return None
    cat = catalog_store.load()
    if not cat.platform_eligible(ep):
        return None
    if not get_settings().platform_key_for(ep["provider"]):
        return None
    if demo_sandbox.is_sandbox(org) or org.public_demo:
        return None
    return cat.cost_view(ep.get("cost"), ep["provider"]) or None


def _capability_alternatives(ep: dict, *, limit: int = 3) -> list[str]:
    """Other providers' endpoints for the same capability, best first — derived, never hand-written.

    A dead end that names only the provider the caller asked for is the reason one org spent 268
    calls on `meta-ad-library.meta-ads.library.search` while `scrapecreators.…-search-ads` — the
    same `capability` string, on a key treg already holds — sat one row away answering 192 of 208
    calls for fourteen other teams. The refusal knew the capability the whole time.

    Read from `cat.endpoints`, which `_parse` has already stripped of marked rows, so a retirement
    stops being suggested the moment it is marked and no list here needs maintaining. This
    COMPARES, it does not route: treg never fails over on the caller's behalf (see the charter),
    so this names the options and their prices and leaves the choice where it belongs.

    Deliberately synchronous and I/O-free. Measured success would need `endpoint_stats.observed`
    and a DB round-trip on an error path — which is how a 404 turns into a 500 — and the caller's
    next step, `catalog get`, already ranks the same siblings by observed success.
    """
    capability = ep.get("capability")
    if not capability:  # only curated capabilities can find siblings; nothing is better than a guess
        return []
    cat = catalog_store.load()
    settings = get_settings()
    ranked = []
    for alt in cat.for_capability(capability):
        if alt["id"] == ep["id"]:
            continue
        cost = cat.cost_view(alt.get("cost"), alt["provider"])
        usd = cost.get("usd") if cost else None
        # "Servable" is the caller's real question: not "does another row exist" but "can treg
        # answer it for me right now". Both halves of tier 4, exactly as `_platform_offer` asks.
        servable = bool(cat.platform_eligible(alt) and settings.platform_key_for(alt["provider"]))
        ranked.append((not servable, usd if usd is not None else float("inf"), alt["id"], usd, servable))
    if not ranked:
        return []
    ranked.sort()
    lines = [f"another provider serves {capability}:"]
    for _, _, alt_id, usd, servable in ranked[:limit]:
        price = "price unknown" if usd is None else ("free" if usd == 0 else f"~${usd:g}/call")
        how = "callable now on treg's key" if servable else f"needs your own {alt_id.split('.')[0]} credential"
        lines.append(f"  {alt_id}  {price}  ({how})")
    return lines


def _marketplace_no_credential(service: str, ep_id: str, provider, ep: dict | None = None) -> HTTPException:
    """Tier 3: the actionable dead-end. Every line names a real command; a pasted-key provider
    gets the `secret add` route too (name it for the service so the ladder finds it)."""
    lines = [f"no {service} credential in this org — {ep_id} is a marketplace endpoint"]
    lines.append(f"  connect one:  treg connections connect --provider {service}")
    if provider.uses_pasted_secret:
        lines.append(f"  or add a key: treg secret add {service} --env-var {service.upper().replace('-', '_')}_API_KEY")
    lines.append(f"  or register the tool yourself: treg tool add {service} --base-url {provider.base_url} …")
    if ep is not None:
        lines.extend(_capability_alternatives(ep))
    return HTTPException(status_code=404, detail="\n".join(lines))


_VALID_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def _marketplace_upstream(ep: dict, provider, query_params) -> tuple[str, set[str]]:
    """The full upstream URL for an endpoint-id call, with `{placeholder}` path params filled from
    the caller's query params (they are consumed — dropped from the relayed query). Missing
    required params fail HERE, before a credential is touched or money spent."""
    path, consumed = ep["path"] or "/", set()
    for name in re.findall(r"{(\w+)}", path):
        value = query_params.get(name)
        if value is None:
            raise HTTPException(status_code=400, detail=(
                f"{ep['id']} needs --query {name}=<value> (a path parameter of {ep['path']})"))
        # Agents often pass `siteUrl` straight from GSC's sites list, where it may already be
        # encoded. Preserve a value containing a real %HH escape; otherwise encode it exactly once.
        # A literal/invalid percent sequence has no valid escape and therefore becomes `%25`.
        rendered = value if _VALID_PERCENT_ESCAPE_RE.search(value) else quote(value, safe="")
        path = path.replace("{%s}" % name, rendered)
        consumed.add(name)
    inp = ep.get("input") or {}
    required = [k for k, v in (inp.get("queryParams") or {}).items()
                if isinstance(v, dict) and v.get("required") and query_params.get(k) is None]
    if required:
        raise HTTPException(status_code=400, detail=(
            f"{ep['id']} requires --query " + " --query ".join(f"{k}=<value>" for k in required)))
    return provider.base_url.rstrip("/") + "/" + path.lstrip("/"), consumed


async def _enforce_capability_pin(ep: dict, caller: Caller, db: AsyncSession) -> None:
    """Refuse a catalog call that goes around the team's pin for that capability.

    A pin is a decision the team already made ("for finding work emails we use Hunter"), so the
    answer names the endpoint they DO use — an agent that gets told "no" without being told "use
    this instead" will simply try the next provider and be refused again.

    Enforced here rather than in the client so it does not depend on the caller's goodwill, and
    before anything is reserved, so a refusal never has to un-hold money."""
    cap = ep.get("capability")
    if not cap or caller.org_id is None:
        return
    pin = (await db.execute(select(CapabilityPin).where(
        CapabilityPin.org_id == caller.org_id,
        CapabilityPin.capability == cap).order_by(CapabilityPin.id))).scalars().first()
    if pin is None or pin.provider == ep["provider"]:
        return
    cat = catalog_store.load()
    # Suggest the OBVIOUS endpoint, not merely the first one in file order: `core` is the curated
    # route for a job, `extended` is the bulk-ingested long tail. Suggesting
    # `tikhub.x.tiktok-analytics-fetch-creator-info-and-milestones` when `tikhub.tiktok.user.profile`
    # exists reads as a broken suggestion and sends the caller somewhere they did not ask to go.
    mine = [e for e in cat.for_capability(cap) if e["provider"] == pin.provider]
    mine.sort(key=lambda e: ((e.get("tier") or "") != "core", not cat.platform_eligible(e), e["id"]))
    alt = mine[0]["id"] if mine else None
    raise HTTPException(status_code=403, detail={
        "error": "capability_pinned",
        "message": (f"this team uses {pin.provider!r} for {cap!r}"
                    + (f" — call {alt} instead" if alt else "")
                    + f". An admin can change it: treg org unpin {cap}"),
        "capability": cap, "pinned_provider": pin.provider, "use_endpoint": alt,
    })


async def _resolve_marketplace_call(
    ep: dict, request: Request, caller: Caller, db: AsyncSession
) -> MarketplaceCall:
    """Walk the credential ladder for a catalog endpoint id → a `MarketplaceCall`.

    The tool is either the org's own registered tool for that provider (tier 1 — passthrough
    resolution, so ACL filtering and the provider-owned tiebreak apply unchanged) or a virtual,
    never-persisted Tool named after the ENDPOINT (tiers 2 and 4) — so the audit trail records the
    endpoint id, and a member's restricted tool list can never contain it (governance: restricted
    members get no direct marketplace calls; `_require_tool_use` enforces that downstream).

    NOTHING is reserved here. Resolution only PRICES the call; `call_tool` reserves after the deny
    rules and caps have had their say, so a refused call never has to un-hold money."""
    await _enforce_capability_pin(ep, caller, db)
    _enforce_catalog_status(ep)
    service = ep["provider"]
    provider = oauth_providers.get(service)
    if provider is None or not provider.base_url:
        raise HTTPException(status_code=502, detail=(
            f"{ep['id']} is cataloged but {service!r} isn't proxy-callable yet"))
    if request.method.upper() != (ep.get("method") or "GET").upper():
        raise HTTPException(status_code=400, detail=(
            f"{ep['id']} is {ep['method']} — add --method {ep['method']}"))
    upstream, consumed = _marketplace_upstream(ep, provider, request.query_params)
    # The telemetry identity of this call, computed once. The body is read here (Starlette caches it,
    # so the relay still streams the same bytes) only for its HASH — never stored, never logged.
    body = await request.body() if _may_have_body(request) else b""
    phash = _params_hash(ep["id"], request.query_params.multi_items(), body)
    # The catalog's estimate travels on EVERY tier — informational on tiers 1/2 (the provider bills
    # the org's own account; Activity shows "estimated") and the reserve amount on tier 4 only
    # (`metered` gates the ledger, so this never charges a balance for an own-key call).
    cv = catalog_store.load().cost_view(ep.get("cost"), service) if ep.get("cost") else None
    info_est, info_unit = _marketplace_pricing(
        service, ep["id"], cv, request.query_params, body)
    common = dict(upstream=upstream, consumed=consumed, endpoint_id=ep["id"], provider=service,
                  params_hash=phash, cost_type=str((ep.get("cost") or {}).get("type") or ""),
                  estimate_micro=info_est,
                  # The per-ROW price, carried on every tier (settle only reads it on metered calls):
                  # a `per_result` settle that can't count rows can only ever bill the estimate,
                  # which is how 6,000 delivered Bright Data records once billed as one (2026-08-24).
                  unit_micro=info_unit)
    try:  # tier 1 — the org registered this provider: their tool, their bindings, their ACLs
        tool, resolved = await _resolve_call(upstream, caller, db)
        return MarketplaceCall(tool=tool, tier="tool", **{**common, "upstream": resolved})
    except HTTPException as exc:
        if exc.status_code != 404:  # 403 (ACL) / 409 (ambiguous) are real answers, not fall-through
            raise
    secret = await _marketplace_secret(service, caller.org_id, db)  # tier 2 — credential, no tool
    if secret is not None:
        virtual = Tool(  # NEVER added to the session — no registry pollution, by design
            org_id=caller.org_id, name=ep["id"], owner=secret.owner,
            base_url=provider.base_url, host=_host_of(provider.base_url),
            bindings=_provider_bindings(provider, secret),
        )
        return MarketplaceCall(tool=virtual, tier="credential", **common)
    # tier 4 — treg's own key, metered against the org's balance. Shadowed by tiers 1 and 2 above:
    # an org that brought its own credential is billed by the provider, not by us, and must never be
    # silently switched onto our key (their quota, their rate limits, their data agreements).
    cost = _platform_offer(ep, provider, caller.org)
    if cost is not None:
        virtual = Tool(
            org_id=caller.org_id, name=ep["id"], owner=caller.email,
            base_url=provider.base_url, host=_host_of(provider.base_url),
            bindings=_platform_bindings(provider),
        )
        return MarketplaceCall(tool=virtual, tier="platform", **{
            **common, "cost_type": str(cost.get("type") or "per_call"),
            "estimate_micro": info_est, "unit_micro": info_unit})
    raise _marketplace_no_credential(service, ep["id"], provider, ep)


def _may_have_body(request: Request) -> bool:
    """Whether this request could carry a body worth hashing. Mirrors proxy._has_body — a GET with no
    content-length must not be awaited for a body it never sends."""
    cl = request.headers.get("content-length")
    if cl is not None and cl != "0":
        return True
    return "chunked" in request.headers.get("transfer-encoding", "").lower()




# ---- tier-4 metering: reserve → relay → settle/release ------------------------------------------
async def _enforce_trial_allowance(caller: Caller, provider: str, db: AsyncSession) -> None:
    """Per-team, per-UTC-day call allowance for TRIAL-POOL providers (fx.yaml `kind: treg_trial`).

    A trial provider is served on treg's own FREE-tier key at a $0 price, so the price gives no
    brake at all — one looping agent would drain the shared vendor quota for every team at once.
    The allowance is the brake, and it lives in the same fx entry as the zero (catalog.trial_pools).

    Counted from audit rows: successful (2xx) calls only, because a failed call produced nothing —
    the same line billability draws. `tool_name` is the endpoint id, so the provider is its prefix.
    The audit is written fire-and-forget, so the count can lag a call or two under load; for a free
    trial that slack is acceptable and bounded. Own-key (tier 2) calls never reach this check — a
    team with its own key is never throttled by the trial it does not use.

    FAIL-CLOSED like the platform cap: the quota being protected is the shared vendor key, and
    serving blind when the count cannot be read is how the pool dies for everyone."""
    allowance = catalog_store.load().trial_pools.get(provider)
    if not allowance:
        return
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0,
                                                   tzinfo=None)
    try:
        used = (await db.execute(
            select(func.count(CallRecord.id)).where(
                CallRecord.org_id == caller.org_id,
                CallRecord.tool_name.like(f"{provider}.%"),  # type: ignore[union-attr]
                CallRecord.status_code >= 200, CallRecord.status_code < 300,
                CallRecord.created_at >= day_start))).scalar_one()
    except Exception as exc:  # noqa: BLE001 — cannot verify the pool ⇒ do not drain it
        logging.getLogger("treg.ledger").warning(
            "trial-allowance check failed for org %s / %s: %s", caller.org_id, provider, exc)
        raise HTTPException(status_code=429, detail=(
            f"cannot verify today's {provider} trial usage right now — retry shortly, or use "
            "your own key: treg connections connect"))
    if used >= allowance:
        raise HTTPException(status_code=429, detail={
            "error": "trial_allowance_reached", "provider": provider,
            "allowance_per_day": allowance, "used_today": int(used),
            "message": (f"this team has used its free {provider} trial for today "
                        f"({used}/{allowance} calls). It resets at 00:00 UTC — or connect your "
                        f"own {provider} key for unmetered calls at your plan's limits: "
                        "treg connections connect"),
        })


async def _enforce_platform_daily_cap(caller: Caller, add_micro: int, db: AsyncSession) -> None:
    """Per-org, per-UTC-day ceiling on tier-4 spend. FAIL-CLOSED, unlike `_enforce_daily_cap`: that one
    meters calls and may let a few extra through under load, this one meters OUR money, so a query that
    cannot answer refuses the call. The cap is the blast radius of a runaway agent (and of a pricing
    mistake in the catalog) — the balance alone is not enough, because auto-top-up can refill it."""
    cap = budget_policy._effective_daily_cap(caller.org)
    try:
        spent = await ledger.spent_today(db, caller.org_id)
    except Exception as exc:  # noqa: BLE001 — cannot verify the ceiling ⇒ do not spend
        logging.getLogger("treg.ledger").warning(
            "platform daily-cap check failed for org %s: %s", caller.org_id, exc)
        raise HTTPException(status_code=429, detail=(
            "cannot verify today's platform spend right now — refusing to spend the team balance "
            "(retry shortly, or use your own key: treg connections connect)"))
    if spent + add_micro > cap:
        raise HTTPException(status_code=429, detail={
            "error": "platform_daily_cap_reached",
            "message": (f"this team has reached its daily limit for calls on treg's keys "
                        f"(${ledger.usd(spent):g} of ${ledger.usd(cap):g} today). It resets at 00:00 UTC. "
                        f"To keep going now, connect your own key: "
                        f"treg connections connect --provider <provider>"),
            "spent_today_micro": spent, "daily_cap_micro": cap, "estimated_cost_micro": add_micro,
        })


def _month_start_utc() -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _resolve_tag_budget(db: AsyncSession, org_id: int, dim: str, val: str) -> TagBudget | None:
    """The limit in force for one tag value: its own override, else the dimension's default, else
    none (unlimited — the shipped state, until a team sets a default).

    ONE indexed query for both, so adding defaults costs the call path nothing. A registry row
    (`auto`) is skipped: it exists to make the cardinality check cheap, and treating it as an override
    would mean the default never applied to anything that had ever been called.
    """
    rows = (await db.execute(select(TagBudget).where(
        TagBudget.org_id == org_id, TagBudget.dim == dim,
        TagBudget.val.in_([val, TAG_DEFAULT])))).scalars().all()
    own = next((r for r in rows if r.val == val and not r.auto), None)
    return own or next((r for r in rows if r.val == TAG_DEFAULT), None)


async def _enforce_tag_budgets(caller: Caller, meta: CallMeta, db: AsyncSession,
                               add_micro: int | None = None) -> None:
    """Refuse a call that breaches a builder-set limit on one of its tags.

    Two passes, called from two places. `add_micro is None` is the PRE-FLIGHT pass (blocked status and
    the daily call count), which runs before the idempotency replay so a blocked user can neither take
    a lock nor be served an answer cached before they were blocked. `add_micro` set is the SPEND pass,
    which needs the estimate and therefore runs inside `_platform_reserve`.

    Every declared dimension is evaluated and the FIRST breach in declaration order refuses, so the
    outcome is deterministic when budgets stack.

    THE CAPS ARE SOFT — advisory, not a gate. `ledger.reserve` is exact because the balance is a
    materialized column, so its check and its debit are one conditional UPDATE. A per-tag total is an
    aggregate over rows, so N concurrent calls can each read a compliant figure and together exceed
    the cap; the overshoot is bounded by concurrency × per-call estimate. That is acceptable ONLY
    because the hard gates sit behind this one: the org balance and the platform daily cap. Making it
    exact would need a second materialized authority on spend, reset daily, decremented on release and
    corrected on settle divergence — four new ways to disagree with ledger.py, which is the one module
    allowed to move money. Never document these caps to builders as hard limits.
    """
    if not meta.tags:
        return
    dims = budget_policy._budget_dims_of(caller.org)
    for dim in dims:
        val = meta.tags.get(dim)
        if not val:
            continue
        try:
            # Registering the value (cardinality bound) happens on the pre-flight pass only; both
            # passes then resolve override → default.
            if add_micro is None:
                result = await budget_policy._tag_budget(
                    db, caller.org_id, dim, val, create=True)
                if result.created:
                    await db.commit()
            row = await _resolve_tag_budget(db, caller.org_id, dim, val)
        except budget_policy.BudgetPolicyError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        except Exception as exc:  # noqa: BLE001 — cannot verify a ceiling ⇒ do not spend
            logging.getLogger("treg.ledger").warning(
                "tag budget check failed for org %s (%s=%s): %s", caller.org_id, dim, val, exc)
            raise HTTPException(status_code=429, detail={
                "error": "tag_budget_unavailable", "dim": dim, "val": val,
                "message": "cannot verify this budget right now — retry shortly",
            })
        if row is None:
            continue
        if add_micro is None:
            if row.status == "blocked":
                raise HTTPException(status_code=403, detail={
                    "error": "tag_blocked", "dim": dim, "val": val,
                    "message": f"{dim} {val!r} is blocked",
                })
            if row.calls_per_day is not None and row.calls_per_day >= 0:
                # From the LEDGER's tag rows, never CallRecord: audit rows are shed under load, so a
                # count cap would let a burst through exactly when it matters — and CallRecord only
                # carries the PRIMARY dimension, so a cap on any other declared key matched nothing
                # and never fired at all.
                used = await ledger.tag_calls_since(
                    db, caller.org_id, dim, val, _day_start_utc())
                if used >= row.calls_per_day:
                    raise HTTPException(status_code=429, detail={
                        "error": "tag_call_cap_reached", "dim": dim, "val": val,
                        "used_today": int(used), "calls_per_day": row.calls_per_day,
                        "message": f"{dim} {val!r} has used its {row.calls_per_day} calls for today",
                    })
            continue
        for cap, since, period in ((row.daily_cap_micro, _day_start_utc(), "day"),
                                   (row.monthly_cap_micro, _month_start_utc(), "month")):
            if cap is None:
                continue
            spent = await ledger.tag_spent_since(db, caller.org_id, dim, val, since)
            if spent + add_micro > cap:
                # Deliberately NOT the org-level 402/429 shape: that one carries the team's balance and
                # a top-up link, and this response is the one a builder renders to their own end user.
                raise HTTPException(status_code=429, detail={
                    "error": "tag_spend_cap_reached", "dim": dim, "val": val,
                    "spent_micro": spent, "cap_micro": cap, "period": period,
                    "estimated_cost_micro": add_micro,
                    "message": (f"{dim} {val!r} has reached its spend limit for this {period} "
                                f"(${ledger.usd(spent):g} of ${ledger.usd(cap):g})"),
                })


async def _platform_reserve(mk: MarketplaceCall, caller: Caller, db: AsyncSession,
                            meta: CallMeta = _NO_META,
                            call_ref: str | None = None) -> None:
    """Withhold this call's estimated cost BEFORE a byte goes upstream, and record the hold on `mk`.
    Insufficient balance is a 402 whose body an agent can act on without reading prose.

    `meta` is the caller's parsed X-Treg-Meta bag, passed explicitly rather than hung off `mk`:
    attribution decides who a reselling builder bills, and it belongs to the request, not to the
    endpoint match. The already-parsed object travels, never a bare dict — re-deriving the primary
    dimension here would be a second place that could disagree about who pays."""
    # The builder's own per-tag ceilings first: a refusal that belongs to ONE of their users must
    # not surface as the team-wide balance error, which names the builder's private numbers.
    await _enforce_tag_budgets(caller, meta, db, add_micro=mk.estimate_micro)
    await _enforce_platform_daily_cap(caller, mk.estimate_micro, db)
    await _enforce_trial_allowance(caller, mk.provider, db)
    # Read before `reserve`: a failed reserve rolls the session back and expires the ORM instance, and
    # a lazy attribute load inside the except would raise MissingGreenlet on the 402 path.
    auto_on = bool(caller.org.autotopup_enabled and caller.org.autotopup_consented_at)
    prefs = billing.autotopup_prefs(caller.org) if auto_on else None
    try:
        mk.call_id = await ledger.reserve(
            db, caller.org_id, mk.endpoint_id, mk.estimate_micro,
            meta={"tier": "oauth" if mk.billed_oauth else "platform",
                  "provider": mk.provider, "cost_type": mk.cost_type},
            tags=meta.tags, call_id=call_ref)
        # reserve moves balance via a raw conditional UPDATE, so the ORM instance is stale — refresh
        # before the threshold check or a crossing goes unnoticed until some later request.
        await db.refresh(caller.org)
        billing.maybe_schedule_autotopup(caller.org)
    except ledger.InsufficientBalance as exc:
        wallet = f"treg's {mk.provider} " + ("app (pay-per-use)" if mk.billed_oauth else "key")
        # For a billed OAuth call "connect your own key" is not the fix — the connection already
        # exists; the way off the meter is bringing your OWN developer app to /oauth/start.
        alt = (f"  or bring your own {mk.provider} developer app (BYO OAuth) — those calls are never metered"
               if mk.billed_oauth else
               f"  or use your own key: treg connections connect --provider {mk.provider}")
        # A team that keeps hitting this by hand is the one that should hear about auto top-up; a team
        # that already has it on needs to know it is the cooldown/cap holding, not a missing card —
        # otherwise the natural reading of "add funds" is that auto top-up is broken.
        if auto_on:
            auto_line = (f"  auto top-up:    on — adds ${ledger.usd(prefs['amount_micro']):g} when the balance "
                         f"drops below ${ledger.usd(prefs['threshold_micro']):g}, at most once per "
                         f"{get_settings().autotopup_cooldown_s // 60} min and "
                         f"${ledger.usd(prefs['monthly_cap_micro']):g}/month. Raise the amount or the "
                         f"cap if your burn outruns it: treg topup --auto on --amount 50 --cap 500")
        else:
            auto_line = "  auto top-up:    off — refill automatically instead: treg topup --auto on --threshold 5 --amount 20"
        raise HTTPException(status_code=402, detail={
            "error": "insufficient_balance",
            "message": (f"{mk.endpoint_id} would cost ~${ledger.usd(exc.required_micro):g} on {wallet} "
                        f"and this team's balance is ${ledger.usd(exc.balance_micro):g}.\n"
                        f"  add funds:      {get_settings().public_url}/app#billing\n"
                        f"{auto_line}\n"
                        + alt),
            "balance_micro": exc.balance_micro,
            "estimated_cost_micro": exc.required_micro,
            "topup_url": "/app#billing",
            "autotopup_enabled": auto_on,
            "provider": mk.provider,
            "endpoint_id": mk.endpoint_id,
        })


# 4xx statuses that mean "the provider did not serve this, and it is NOT the caller's input" — our
# credential was rejected, exhausted, throttled, or the request timed out. The provider bills nothing
# for these, so neither may we: charging here would pass OUR expired or over-quota platform key on to
# a team as real spend, and for a builder reselling treg it would land on their end customers' bills.
# 403 is deliberately included even though some providers use it for a genuinely caller-driven
# "resource not accessible": when it is unclear whether the provider charged us, the safe direction
# is not to charge. Absorbing a rare few micro-USD is recoverable; over-billing out of an append-only
# ledger is not.
_NOT_THE_CALLERS_FAULT = frozenset({401, 402, 403, 405, 407, 408, 429})


def _platform_billable(status_code: int, cost_type: str) -> bool:
    """Does a response with this status cost us money? (plan §2.2)
      2xx                        → yes, the provider served it.
      4xx                        → only under `per_call`, and only when the rejection is about the
                                   CALLER'S INPUT (400/404/422 …): the provider charges for accepting
                                   such a request, so it is on the caller. A credential/quota refusal
                                   (`_NOT_THE_CALLERS_FAULT`) is on us and is never billed — a 405
                                   rejects the method OUR catalog selected, while a 429 on a
                                   SHARED-plan key is treg's own saturation. Billing either would
                                   charge teams for our metadata or congestion. Under
                                   `per_result`/`per_success` a rejected request produced nothing.
      5xx / 3xx / network error  → no. An upstream failure is never billed to the caller.
    """
    if 200 <= status_code < 300:
        return True
    if 400 <= status_code < 500:
        return cost_type == "per_call" and status_code not in _NOT_THE_CALLERS_FAULT
    return False


_PLATFORM_BODY_MAX = 8 * 1024 * 1024  # buffer ceiling for a metered response (API JSON, not downloads)

# ---- failure evidence: what a failed call is allowed to leave behind ----------------------------
# Sized to hold a real provider error whole — a typical 400 body is 80-300 characters and a verbose
# JSON one about 800 — while still capping a ~14KB CDN error page and a caller stuck in a retry loop.
_ERROR_RESPONSE_MAX = 2000
_ERROR_REQUEST_MAX = 1000
# Unmetered calls keep streaming unless the caller explicitly declared a small body. Starlette's
# request cache then lets relay replay those exact bytes without a second read from the socket.
_ERROR_CALLER_BODY_MAX = 64 * 1024
# Sliced off the FRONT before any decode, so an 8MB single-line HTML error page never gets decoded or
# regex-scanned on the request path. Every limit above is characters; this one is bytes.
_ERROR_BODY_SLICE = 8192
_ERROR_MASKING_FAILED = "<redacted: could not render credentials for masking>"

# Third-party secret shapes. `_EVIDENCE_SECRET_RE` below covers values that LOOK like a key; these
# two cover the places a value hides by its NAME instead — in a URL or a JSON body — which
# `_CRED_FLAG_EQ_RE` misses because it requires a leading dash (it was written for argv, where
# `--token=x` is the only shape).
_QUERY_CRED_RE = re.compile(
    r"(?i)((?:api[-_]?key|apikey|key|token|secret|password|passwd|pwd|auth|access[-_]?token"
    r"|sig|signature)\"?\s*[=:]\s*\"?)[^&\s\"',}]+")
_URL_USERINFO_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")

# `_ARGV_SECRET_RE`'s catch-all masks ANY 24+ run of [A-Za-z0-9_-], which is right for an argv log and
# wrong here: it deletes 100% of provider correlation identifiers — UUIDs, ULIDs, 32-char trace ids,
# request ids — which are exactly what you quote to a provider's support desk. Measured on real error
# bodies: the prose always survived, the correlation field never did. So for evidence we keep the
# TARGETED half (known key prefixes, JWTs) and drop the catch-all. Platform credentials do not depend
# on it — they have exact masking plus the fail-closed backstop below — and the owner has accepted
# that a third-party secret may occasionally survive here.
_EVIDENCE_SECRET_RE = re.compile(
    r"\b(?:sk|pk|rk|ghp|gho|ghs|ghu|glpat|AKIA|ASIA|AIza|xox[baprs])[A-Za-z0-9_\-]{6,}\b"
    # Anchored + possessive for the same reason as the argv rule above, and it matters MORE here:
    # this one runs on a PROVIDER's response body, which is uncontrolled input on the request path.
    r"|\beyJ[A-Za-z0-9_\-]++\.[A-Za-z0-9_.\-]{8,}")

# Response headers worth keeping on a FAILED platform call. An empty-bodied 401 or 429 is otherwise
# undiagnosable, and these say which of "bad credential" / "wrong scheme" / "quota gone" / "retry in
# N" it was. Allowlisted, never the whole bag: `authorization` and `set-cookie` live in there too.
_EVIDENCE_HEADERS = (
    "retry-after", "www-authenticate",
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset",
    "x-request-id", "x-requestid", "request-id", "x-correlation-id", "x-amzn-requestid",
    "cf-ray", "x-trace-id",
)


_SENSITIVE_JSON_SECRET_KEYS = {
    "access_token", "refresh_token", "id_token", "token", "client_secret", "api_key", "secret",
    "password", "private_key",
}


def _secret_renderings(tool: Tool, secrets: dict[int, Secret]) -> list[str]:
    """Every spelling of every injected credential for this tool, longest first.

    This is the primary defence and the only deterministic one: platform credentials come from a
    named setting and org credentials from an encrypted Secret, so both can be matched exactly instead
    of guessed at. Providers routinely quote the offending request back inside a 400/401 body — the
    header they received, or the full URL including the query — and a key can survive
    `_EVIDENCE_SECRET_RE` by simply not looking like a known key shape. Exact substring masking is why
    the deterministic layer carries the weight here and the pattern layer is only a net.

    treg injects the value verbatim, but a PROVIDER may hand it back transformed, and a transform it
    can reverse is one we have to anticipate. Four families, all observed shapes rather than guesses:

    * the raw value, and the value after the binding's `format` (`Bearer {secret}`, `Basic {secret}`);
    * percent-encoded — twelve providers authenticate by query param, so the key comes back inside an
      echoed URL. Both cases: `quote()` emits UPPERCASE hex (`%2F`) and plenty of servers echo lower;
    * JSON-escaped, because a body quoting a URL usually writes `\\/` for `/`;
    * **the DECODED halves of a Basic credential.** `config.py` states that dataforseo's platform
      value is already the base64 of `login:password`. A provider that decodes Basic auth and reports
      `{"received_username": …, "received_password": …}` echoes treg's credential in a form where
      neither the base64 blob nor `Basic <blob>` appears. dataforseo is the largest provider by
      spend, so this is the opposite of theoretical.
    """
    out: set[str] = set()

    def add(value: str) -> None:
        """One secret and every spelling of it a provider might echo back."""
        if not value or len(value) < 4:
            return  # too short to mask without redacting half the message
        enc = quote(value, safe="")
        out.update({value, enc, enc.lower(), quote_plus(value), value.replace("/", "\\/"),
                    json.dumps(value, ensure_ascii=False)[1:-1]})

    def add_credential(value: str) -> None:
        add(value)
        for part in _basic_credential_parts(value):  # mask what a provider can DECODE
            add(part)

    for binding in tool.bindings or []:
        fmt = str(binding.get("format") or "{secret}")
        # A constant provider header can share the binding's credential reference so the normal
        # injector owns the whole protocol shape, but it does not inject that credential. Treating
        # its literal format as a secret spelling masked ordinary dates such as Crustdata's API
        # version from every failure-evidence snippet.
        if "{secret}" not in fmt:
            continue
        setting = binding.get("platform_setting")
        if setting:
            value = getattr(get_settings(), setting, None)
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            add_credential(value)
            add(fmt.format(secret=value))
            continue

        sid = binding.get("secret_id")
        if sid is None:
            continue
        plain = crypto.decrypt(secrets[sid].value)
        add_credential(plain)
        injector = binding.get("injector", "env")
        if injector not in ("oauth", "secret_file"):
            add(fmt.format(secret=plain.strip()))
            continue

        field = str(binding.get("secret_field") or "access_token")
        token = injectors._token_from_json(plain, field)
        add_credential(token)
        add(f"Bearer {token}")
        add(fmt.format(secret=token.strip()))
        data = json.loads(plain)
        if not isinstance(data, dict):
            raise ValueError("JSON credential is not an object")
        sensitive = _SENSITIVE_JSON_SECRET_KEYS | {field.lower()}
        for key, value in data.items():
            if isinstance(key, str) and key.lower() in sensitive and isinstance(value, str):
                add_credential(value)
    # Longest first so `Bearer abc` is masked as a unit before the bare `abc` inside it turns the
    # line into `Bearer ***` — same result here, but the ordering stops a shorter secret that is a
    # substring of a longer one from fragmenting it into an unmatchable remainder.
    return sorted((s for s in out if len(s) >= 4), key=len, reverse=True)


def _safe_secret_renderings(tool: Tool, secrets: dict[int, Secret]) -> list[str] | None:
    """Render credentials for masking, or signal that evidence must be replaced wholesale."""
    try:
        return _secret_renderings(tool, secrets)
    except Exception as exc:  # noqa: BLE001 — malformed/encrypted credentials must fail closed
        logging.getLogger("treg").warning("could not render credentials for error masking: %s", exc)
        return None


def _basic_credential_parts(value: str) -> list[str]:
    """`login:password` and its two halves, when `value` is the base64 of a Basic credential.

    Returns [] for anything that is not — an ordinary API key rarely base64-decodes to printable text
    containing a colon, and a false positive here only costs an extra (harmless) mask.
    """
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except Exception:  # noqa: BLE001 — not base64, or not text: simply not a Basic credential
        return []
    if ":" not in decoded or not decoded.isprintable():
        return []
    login, _, password = decoded.partition(":")
    return [p for p in (decoded, login, password) if p]


def _decode_error_body(raw: bytes, content_encoding: str = "", content_type: str = "") -> str:
    """Bytes off the wire → something a human can read, or an honest marker saying why not.

    `force_identity` asks the provider not to compress a metered response, but a CDN or WAF error page
    is generated at the edge and answers however it likes — and those 403s are exactly the responses
    this feature exists to explain. `relay` streams `aiter_raw()`, so nothing has decoded them.
    """
    if not raw:
        return ""
    enc = (content_encoding or "").strip().lower()
    if enc and enc != "identity":
        if enc not in ("gzip", "deflate"):  # br, zstd — no stdlib decoder we can rely on
            return f"<{enc}-encoded, {len(raw)} bytes, not decoded>"
        try:
            # INCREMENTAL and capped just past the evidence limit. `gzip.decompress` is unbounded, so
            # slicing the INPUT to 8KiB does not bound the OUTPUT: 20MB of one repeated byte
            # compresses to under 20KB, and a bomb would expand to megabytes that four regexes then
            # walk synchronously on the request path.
            d = zlib.decompressobj(16 + zlib.MAX_WBITS if enc == "gzip" else -zlib.MAX_WBITS)
            raw = d.decompress(raw, _ERROR_RESPONSE_MAX * 4)
        except Exception:  # noqa: BLE001 — a truncated slice of a gzip stream is expected to fail
            return f"<{enc}-encoded, {len(raw)} bytes, undecodable>"
    text = raw.decode("utf-8", "replace")
    # A binary payload decoded with errors="replace" is a wall of U+FFFD that says nothing. Report the
    # shape instead, keeping a short hex head so the content type is still identifiable.
    if text.count("�") > len(text) // 5 or ("\x00" in text[:512]):
        return f"<binary {content_type or 'response'}, {len(raw)} bytes, head={raw[:32].hex()}>"
    return text


def _caller_request_snippet(request: Request, tool: Tool, caller_body: bytes,
                            secrets: list[str]) -> str:
    """What the CALLER actually sent, redacted — the half of a failure treg otherwise forgets.

    `CallRecord.path` stores the catalog's upstream URL with only `{placeholder}` path params filled,
    so the caller's real query and body survive nowhere else (`params_hash` is one-way). Without this
    a 400 cannot be explained even when the provider says exactly what was wrong with it.

    Query params are read from the INBOUND request, which never carries an injected credential:
    injection builds a separate outbound list (see proxy.relay). The binding's own query names are
    dropped anyway, for the caller who passed a value into the slot the injector overwrites.
    """
    drop = {b.get("name", "Authorization") for b in (tool.bindings or [])
            if b.get("location", "header") == "query"}
    parts = []
    pairs = [f"{k}={v}" for k, v in request.query_params.multi_items() if k not in drop]
    if pairs:
        parts.append("?" + "&".join(pairs))
    if caller_body:
        parts.append(_decode_error_body(caller_body[:_ERROR_BODY_SLICE], "",
                                        request.headers.get("content-type", "")))
    return _redact_snippet(" ".join(parts), secrets, _ERROR_REQUEST_MAX)


def _redact_snippet(text: str, secrets: list[str], limit: int) -> str:
    """Mask, THEN truncate — never the other way round.

    Truncating first can cut a 40-character token down to a 12-character survivor that no longer
    matches the 24+ rule, which is how a "redacted" field ends up holding half a key. `_redact_argv`
    already gets this order right; this follows it.
    """
    if not text:
        return ""
    for secret in secrets:  # exact and deterministic, before any pattern guessing
        text = text.replace(secret, "***")
    text = _URL_USERINFO_RE.sub("://***:***@", text)
    text = _QUERY_CRED_RE.sub(r"\1***", text)
    text = _EVIDENCE_SECRET_RE.sub("***", text)
    text = " ".join(text.split())  # collapse newlines/indentation; these are read in a table
    # Fail closed. Everything above is a list of transforms we thought of; this asks whether a secret
    # survived one we did not, by re-checking a NORMALISED copy (percent-decoded, JSON-unescaped,
    # lowercased). If one is still there, drop the whole snippet: losing a debugging message is a bad
    # day, leaking the credential every tenant shares is a much worse one.
    if secrets:
        probe = unquote(text.replace("\\/", "/")).lower()
        if any(s.lower() in probe for s in secrets):
            return "<redacted: a credential survived masking>"
    if len(text) <= limit:
        return text
    # Truncation can expose a partial token at the seam that was safe only while whole.
    return re.sub(r"[A-Za-z0-9_\-+/=.]{8,}$", "***", text[:limit]) + "…"



def _brightdata_record_count(body: bytes) -> int | None:
    """How many RECORDS a Bright Data Web Scraper response delivered, or None for "settle at the
    estimate". Bright Data bills $1.50/1000 records *delivered* and reports no charge field, so the
    response body is the only bill we will ever see. Counting it is what closed the 39x gap found
    2026-08-24: $13.61 consumed upstream in three weeks vs $0.35 billed, because a per_result call
    always settled as ONE record — a Google Play reviews job that delivered ~6,000 records billed
    $0.0015.

    Shapes, per docs + live traffic:
      - sync /scrape and /snapshot downloads, format=json → a JSON ARRAY, one element per record;
      - the >60s sync fallback and /trigger → a JSON OBJECT carrying `snapshot_id` — zero records
        HERE; the job's records bill when the snapshot is downloaded (its catalog entry is priced
        per_result for exactly that reason);
      - format=ndjson → one JSON object per line; format=csv → header line + one line per record.
    A body that STARTS like JSON but does not parse is treated as truncated (the metered buffer
    caps at _PLATFORM_BODY_MAX and drops the tail) → None, settle at the estimate, never a
    line-count guess over a partial payload. Any other unrecognised shape → None for the same
    reason: when we cannot count, the estimate is the honest number."""
    if body[:2] == b"\x1f\x8b":  # compress=true gzips the download — we can't count, estimate wins
        return None
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        try:  # ndjson: every line is its own record — EVERY line must parse, or it isn't ndjson
            for ln in lines:
                json.loads(ln)
            return len(lines)
        except ValueError:
            pass
        if text[0] in "[{":  # JSON that broke mid-stream: the 8MB buffer truncated it
            return None
        return len(lines) - 1 if len(lines) > 1 else None  # csv: header + rows
    if isinstance(doc, list):
        return len(doc)
    if isinstance(doc, dict):
        # Zero records delivered, whatever the object says: the async handoff (`snapshot_id` — the
        # records bill at the snapshot download), an early download's {"status": "running"}, or any
        # other envelope. Pay-per-success means an answer with no records costs nothing.
        return 0
    return None

def _observed_cost_micro(mk: MarketplaceCall, body: bytes, headers=None) -> int | None:
    """The provider's OWN reported charge for this call, in micro-USD, or None when it doesn't say.

    For an oauth-billed `per_result` call (X reads), the response body IS the bill: X charges per
    resource returned, so counting `data` beats trusting the estimate — a timeline asked for 100
    posts that returned 7 settles at 7, and an empty page settles at zero. The count is capped at
    the reserved estimate's row assumption only implicitly (a bigger-than-asked response charges
    more, which `ledger.settle` handles as an overrun).

    Three providers volunteer the number, in two different denominations:
      - dataforseo: a top-level `cost` in USD — including 0 when it decided not to charge (a free
        route, or a request it rejected before metering). That zero is real information and settles the
        call at zero, which is why the test is `>= 0` and not truthiness.
      - scrapecreators (`credits_charged`), akta and leadmagic (`credits_consumed`): provider
        credits, converted through the provider's credit rate (fx.yaml) — the same conversion
        `cost_view` uses, so a settle can't disagree with the catalog's price. Akta is the one that
        NEEDS this: its enrich route is priced per SECTION requested and its news route adds a
        per-article rider, so the catalog's single estimate can only be an upper bound — the actual
        charge lives here. LeadMagic answers a miss with 2xx and `credits_consumed: 0` (observed at
        verify time), so honouring the field is what keeps a free miss from billing the estimate;
        it also reports fractions (email verify is 0.25).
      - lusha: `billing.creditsCharged`, one level down — the same reported-credits contract,
        including 0 on a 2xx miss (the captured people.enrich example IS one) and the 2-credit
        company enrich. Converted through the lusha rate like the others.
      - apollo: DERIVED, not reported. Apollo answers a miss with 2xx (`organization: null` on
        enrich, an empty `organizations` page on search) and charges nothing for it, so status-based
        billing alone would bill the caller for a response Apollo gave away. The body says whether
        the charged thing came back; when it didn't, the call settles at 0.
      - hunter (domain search): DERIVED too, and for the opposite reason — its price is not
        per row but one whole SEARCH credit per 10 emails returned, rounded up, with an empty
        domain free. `data.emails` is the only place that number exists.
      - hunter (email finder): DERIVED, the flat case — one whole SEARCH credit when an email is
        found, nothing on a miss ("a miss is free", per Hunter's own pricing), yet a miss still
        answers HTTP 200, so the estimate billed the full credit for a name Hunter had nothing on.
      - tikhub: REPORTED in prose rather than a number. Every envelope says whether the call is
        billed; only the explicit no-charge phrasing settles at zero, because TikHub really does
        charge for a 2xx whose payload is an embedded error (verified live 2026-07-30 — see
        docs/context/architecture/catalog.md, "the provider decides what counts as success").

    Everyone else settles at the estimate. This is the same signal the catalog's `observed_cost`
    harvests, which is what lets phase 5's drift detector compare the two numbers directly."""
    provider = mk.provider
    catalog = catalog_store.load()
    ep = catalog.by_id.get(mk.endpoint_id)
    cost = catalog.cost_view(ep.get("cost"), provider) if ep else None
    if cost and cost.get("settle") == "base" and cost.get("usd") is not None:
        # The reserve can include documented request riders while the observed settlement remains
        # the catalog base. Aviato simple search earned this rule from two multi-row live probes:
        # enrich=true returned only id rows and charged the same 0.25-credit base both times.
        return _usd_to_micro(float(cost["usd"]))
    if provider == "crustdata" and headers is not None:
        raw = headers.get("x-credits-used")
        rate = catalog_store.load().credit_rates.get("crustdata")
        try:
            credits = float(raw)
        except (TypeError, ValueError):
            credits = -1
        if credits >= 0 and rate:
            return _usd_to_micro(credits * rate)
    if not body:
        return None
    if provider == "brightdata" and mk.cost_type == "per_result" and mk.unit_micro > 0:
        # DERIVED by counting records — Bright Data's bill is per record delivered and the body is
        # the only place that number exists (see _brightdata_record_count for the shapes).
        n = _brightdata_record_count(body)
        return None if n is None else n * mk.unit_micro
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if provider == "aviato" and mk.endpoint_id == "aviato.people.enrich.bulk":
        if isinstance(doc, list) and mk.unit_micro > 0:
            return sum(item is not None for item in doc) * mk.unit_micro
        return None
    if not isinstance(doc, dict):
        return None
    if provider == "aviato" and mk.endpoint_id == "aviato.companies.enrich.bulk":
        rows = doc.get("companies")
        if isinstance(rows, list) and mk.unit_micro > 0:
            return sum(item is not None for item in rows) * mk.unit_micro
        return None
    if provider == "aviato" and cost and cost.get("settle") == "modifiers" and mk.unit_micro > 0:
        # The request-time unit excludes catalog modifiers marked reserve_only. Bulk routes above
        # multiply that unit by successful rows; a single route settles one such unit.
        return mk.unit_micro
    if mk.billed_oauth and mk.cost_type == "per_result" and mk.unit_micro > 0:
        data = doc.get("data")
        n = len(data) if isinstance(data, list) else (1 if data else 0)
        return n * mk.unit_micro
    if provider == "dataforseo":
        cost = doc.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
            return int(cost * 1_000_000 + 0.5)
        return None
    if provider in ("scrapecreators", "akta", "leadmagic"):
        credits = doc.get("credits_charged" if provider == "scrapecreators" else "credits_consumed")
        rate = catalog_store.load().credit_rates.get(provider)
        if isinstance(credits, (int, float)) and not isinstance(credits, bool) and credits >= 0 and rate:
            return int(credits * rate * 1_000_000 + 0.5)
        return None
    if provider == "lusha":
        billing = doc.get("billing")
        credits = billing.get("creditsCharged") if isinstance(billing, dict) else None
        rate = catalog_store.load().credit_rates.get("lusha")
        if isinstance(credits, (int, float)) and not isinstance(credits, bool) and credits >= 0 and rate:
            return int(credits * rate * 1_000_000 + 0.5)
        return None
    if provider == "hunter" and mk.endpoint_id == "hunter.companies.emails":
        # DERIVED, like apollo. Hunter's domain search does not bill per row at all: it takes ONE
        # whole search credit per 10 emails RETURNED, rounded up, and a domain it knows nobody at is
        # free. Neither half of that rule survives being flattened into the catalog's per-row price
        # (1 credit ÷ 10 = $0.00245/result), so settling at the estimate is wrong in BOTH
        # directions — a search with no `limit` reserved the 20-row default page and settled a
        # ZERO-email answer at $0.0490, 20x the published per-result price for results nobody got,
        # while `limit=1` on a domain that did answer settled at $0.00245, a tenth of the credit
        # Hunter actually took. The returned list is the bill.
        data = doc.get("data")
        emails = data.get("emails") if isinstance(data, dict) else None
        rate = catalog_store.load().credit_rates.get("hunter")
        if isinstance(emails, list) and rate:
            credits = -(-len(emails) // 10)  # whole credits, rounded up; no emails = no charge
            return int(credits * rate * 1_000_000 + 0.5)
        return None
    if provider == "hunter" and mk.endpoint_id == "hunter.people.email.find":
        # DERIVED, the flat case of the same family: the finder takes ONE whole search credit when
        # it finds an email and nothing when it doesn't — the catalog note says "a miss is free" in
        # as many words, yet a miss still answers HTTP 200 with `email: null`, so settling at the
        # estimate billed the full credit ($0.0245) for a name Hunter had nothing on. A body
        # without the `email` key (an error shape) still falls back to the estimate.
        data = doc.get("data")
        rate = catalog_store.load().credit_rates.get("hunter")
        if isinstance(data, dict) and "email" in data and rate:
            return int(rate * 1_000_000 + 0.5) if data["email"] else 0
        return None
    if provider == "tikhub":
        # REPORTED in prose rather than a number: every TikHub envelope states whether the call is
        # billed. A 2xx whose payload is an embedded error still says "This request will incur a
        # charge." and TikHub really does charge us for it (verified live 2026-07-30 — see
        # docs/context/architecture/catalog.md, "the provider decides what counts as success"), so
        # a dead page settling at the estimate is faithful, not an over-charge. Only the explicit
        # no-charge phrasing settles at zero; anything else stays at the estimate.
        msg = doc.get("message")
        if isinstance(msg, str):
            low = msg.lower()
            if "won't be charged" in low or "will not be charged" in low or "not incur" in low:
                return 0
        return None
    if provider == "apollo":
        # Only the shapes whose billing rule is documented and body-decidable: company enrichment
        # (1 credit per organization returned, null on a miss) and company search (1 credit per
        # non-empty PAGE). A body carrying neither key — people enrichment's 1-9 credit range
        # included — falls through to the estimate rather than guessing.
        rate = catalog_store.load().credit_rates.get("apollo")
        if rate:
            for key in ("organization", "organizations"):
                if key in doc:
                    return int(rate * 1_000_000 + 0.5) if doc[key] else 0
        return None
    return None


async def _buffer_response(response: StreamingResponse) -> tuple[Response, bytes]:
    """Drain a relayed streaming response into memory and return an equivalent plain Response.

    Metered calls give up streaming on purpose: settling needs the provider's own reported cost (which
    lives in the body) and the telemetry row wants the response size, and neither can be known while
    the bytes are still in flight. These are JSON API answers — the same payloads the catalog stores as
    examples — so the memory cost is a few KB, and buffering happens BEFORE anything is sent to the
    caller, which is what lets a mid-stream upstream failure still become a clean 502 + release."""
    chunks, size = [], 0
    async for chunk in response.body_iterator:
        raw = chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8", "replace")
        size += len(raw)
        if size <= _PLATFORM_BODY_MAX:
            chunks.append(raw)
    body = b"".join(chunks)
    if response.background is not None:  # the relay's upstream-close task — run it now, not later
        await response.background()
        response.background = None
    out = Response(content=body, status_code=response.status_code)
    # Carry the upstream's headers verbatim (the relay already dropped hop-by-hop + our own), with a
    # content-length that matches what we are actually about to send.
    out.raw_headers = [(k, v) for k, v in response.raw_headers if k.lower() != b"content-length"]
    out.raw_headers.append((b"content-length", str(len(body)).encode()))
    return out, body


async def _peek_stream_head(response: StreamingResponse, limit: int) -> tuple[StreamingResponse, bytes]:
    """Read at most ``limit`` response bytes for evidence, then replay every byte to the caller.

    Unmetered calls retain their streaming contract. The consumed chunks are yielded first by the
    replacement response, followed by the untouched iterator; the relay's upstream-close background
    task moves with it and therefore still runs after the caller finishes reading.
    """
    iterator = response.body_iterator.__aiter__()
    consumed: list[bytes] = []
    head = bytearray()
    while len(head) < limit:
        try:
            chunk = await iterator.__anext__()
        except StopAsyncIteration:
            break
        raw = chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8", "replace")
        consumed.append(raw)
        head.extend(raw[:limit - len(head)])

    async def replay():
        for chunk in consumed:
            yield chunk
        async for chunk in iterator:
            yield chunk

    out = StreamingResponse(replay(), status_code=response.status_code,
                            background=response.background)
    response.background = None
    out.raw_headers = list(response.raw_headers)
    return out, bytes(head)


def _error_response_evidence(response: Response, body: bytes, secrets: list[str]) -> str:
    """Build the redacted provider half of a failed-call evidence row."""
    # Headers first: a 401 or 429 often has an empty or generic body, and `Retry-After` /
    # `WWW-Authenticate` / the rate-limit trio are then the entire diagnosis.
    hdrs = " ".join(f"{h}={response.headers[h]}" for h in _EVIDENCE_HEADERS
                    if response.headers.get(h))
    evidence = _redact_snippet(
        (f"[{hdrs}] " if hdrs else "") +
        _decode_error_body(body[:_ERROR_BODY_SLICE],
                           response.headers.get("content-encoding", ""),
                           response.headers.get("content-type", "")),
        secrets, _ERROR_RESPONSE_MAX)
    return evidence or "<no response body or headers>"


async def _platform_settle(
    mk: MarketplaceCall, status_code: int | None, body: bytes = b"", *, headers=None,
    reason: str = ""
) -> tuple[int, int | None]:
    """Close the hold for a metered call → (charged_micro, observed_micro). `charged_micro` is what
    actually hit the org's balance (0 on a release) — the number the Activity feed must show, because
    the estimate alone over-reports a released call as spend.

    `status_code=None` means the provider never answered us (our own 4xx, an injection error, a network
    failure) — always a release, never a charge, whatever the endpoint's billing type says.

    Never raises: the caller already has their answer (or their error), and a ledger hiccup must not
    turn a served call into a 500. A hold that fails to close is not lost money either — the reaper
    releases it, which errs in the org's favour. Runs on its OWN session because the request's session
    may be mid-rollback from the very error we are releasing for."""
    if not mk.metered or not mk.call_id:
        return 0, None
    billable = status_code is not None and _platform_billable(status_code, mk.cost_type)
    observed = _observed_cost_micro(mk, body, headers) if billable else None
    call_id, mk.call_id = mk.call_id, None  # closing is once-only, even if two paths try
    charged = 0

    async def _close() -> int:
        async with session_maker() as db:
            if billable:
                return await ledger.settle(db, call_id, observed, meta={
                    "provider": mk.provider, "status_code": status_code, "cost_type": mk.cost_type,
                    "cost_source": "provider" if observed is not None else "estimate"})
            await ledger.release(db, call_id, reason=reason or f"not_billable_{status_code}",
                                 meta={"provider": mk.provider, "cost_type": mk.cost_type,
                                       "status_code": status_code})
            return 0

    try:
        try:
            charged = await _close()
        except PoolTimeoutError:
            # No pool slot within `pool_timeout`: a transient wait, not a broken ledger. A settle that
            # gives up here forfeits the charge (the hold is reaped in the org's favour) — real revenue,
            # so one short retry is worth it. Anything else falls straight through to the log.
            await asyncio.sleep(0.5)
            charged = await _close()
    except Exception as exc:  # noqa: BLE001 — loudly, but never into the caller's response
        logging.getLogger("treg.ledger").error(
            "settle/release failed for call %s (%s, status %s): %s",
            call_id, mk.endpoint_id, status_code, exc, exc_info=True)
    return charged, observed


async def _finish_cancelled_call(
    request: Request,
    mk: MarketplaceCall | None,
    call_ref: str,
    response: Response | None = None,
) -> None:
    """Finish compensation before propagating cancellation from a call that may have reserved."""
    # A cancelled request cannot own this cleanup: another cancellation while it is returning the
    # first one would strand the upstream response, hold, or idempotency label halfway through.
    async def _cleanup() -> None:
        # Every branch contains its own failure: raising here would replace the original cancellation
        # when shield joins this task, instead of letting the remaining compensation finish.
        if response is not None and response.background is not None:
            background, response.background = response.background, None
            try:
                await background()
            except (Exception, asyncio.CancelledError):  # noqa: BLE001
                logging.getLogger("treg.proxy").error(
                    "upstream close failed for cancelled call %s", call_ref, exc_info=True)
        if mk is not None and mk.metered:
            # `ledger.reserve` may have committed without returning, so `mk.call_id` is not an
            # authority here. The pre-reserve call_ref is the hold id in either outcome, and release
            # conditionally claims it: committed means refund, rolled back means a safe no-op.
            mk.call_id = None
            try:
                async with session_maker() as cleanup_db:
                    await ledger.release(
                        cleanup_db,
                        call_ref,
                        reason="call_cancelled",
                        meta={"provider": mk.provider, "cost_type": mk.cost_type,
                              "status_code": None},
                    )
            except (Exception, asyncio.CancelledError):  # noqa: BLE001
                logging.getLogger("treg.ledger").error(
                    "cancellation release failed for call %s", call_ref, exc_info=True)
        try:
            await _release_idempotent_claim(request)
        except (Exception, asyncio.CancelledError):  # noqa: BLE001
            logging.getLogger("treg.idempotency").error(
                "cancellation claim release failed for call %s", call_ref, exc_info=True)

    cleanup = asyncio.create_task(_cleanup())
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # A repeated cancel may interrupt the shield await, but not its child. Keep joining the
            # same cleanup task so compensation completes before the original cancellation escapes.
            continue
    await cleanup


async def _await_before_reserve(awaitable, request: Request, call_ref: str):
    """Release an owned idempotency label if cancellation lands before the money gate."""
    try:
        return await awaitable
    except asyncio.CancelledError:
        await _finish_cancelled_call(request, None, call_ref)
        raise


async def _record_first_call(org_id: int) -> None:
    """Set Org.first_call_at once — the metric that decides whether a marketing channel is real (see
    marketing/landing/_measurement.md). A CONDITIONAL UPDATE, not read-then-write: concurrent first
    calls would both see NULL and both fire. Set for EVERY org (it is a product metric in its own
    right); adsconv.queue() itself no-ops for orgs with no ad_gclid, so the conversion side stays
    ad-attributed-only.

    Runs on its OWN session, same reason as _platform_settle: this fires after the response is built,
    while the request's `db` may still be mid-settlement (or mid-rollback from one), and a commit or
    rollback issued here would land on THAT transaction instead of this one. Never raises — a metric
    write must not turn a working proxied call into a 500."""
    try:
        async with session_maker() as db:
            result = await db.execute(
                update(Org)
                .where(Org.id == org_id, Org.first_call_at.is_(None))
                .values(first_call_at=_utcnow_naive())  # naive UTC — asyncpg rejects tz-aware here
            )
            if result.rowcount:
                org_row = await db.get(Org, org_id)
                if org_row is not None:
                    await adsconv.queue(db, org_row, adsconv.ACTION_FIRST_CALL)
                await db.commit()
    except Exception:  # noqa: BLE001 — loudly, but never into the caller's response
        logging.getLogger("treg.adsconv").error(
            "first_call_at update/queue failed for org %s", org_id, exc_info=True)


async def _relay_live_demo(request: Request, upstream_url: str, key: str, visitor: str):
    """The sandbox's ONE real upstream call (the landing live wire). Deliberately narrower than
    relay(): form-encoded only, auth header built here from the env key (never from a sandbox
    secret), and `metadata[visitor]` is OVERRIDDEN server-side so the landing feed's name is
    always ours, whatever the caller put in the body."""
    from urllib.parse import parse_qsl, urlencode
    http: httpx.AsyncClient = request.app.state.http
    headers = {"Authorization": f"Bearer {key}"}
    content = None
    if request.method == "POST":
        body = (await request.body()).decode("utf-8", "replace")
        pairs = [(k, v) for k, v in parse_qsl(body, keep_blank_values=True) if k != "metadata[visitor]"]
        pairs.append(("metadata[visitor]", visitor))
        content = urlencode(pairs)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    r = await http.request(request.method, upstream_url, params=request.query_params.multi_items(),
                           content=content, headers=headers)
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"))


# Stage 4b moves the HTTP surface before the call kernel. Each binding retires when commits 6
# through 19 place that collaborator in its final application, domain, or infrastructure owner.
call_routes._enforce_catalog_status = _enforce_catalog_status
call_routes._marketplace_secret = _marketplace_secret
call_routes._platform_estimate_micro = _platform_estimate_micro
call_routes._platform_offer = _platform_offer
call_routes._resolve_call = _resolve_call
call_routes._ERROR_BODY_SLICE = _ERROR_BODY_SLICE
call_routes._ERROR_CALLER_BODY_MAX = _ERROR_CALLER_BODY_MAX
call_routes._ERROR_MASKING_FAILED = _ERROR_MASKING_FAILED
call_routes._ERROR_RESPONSE_MAX = _ERROR_RESPONSE_MAX
call_routes._await_before_reserve = _await_before_reserve
call_routes._billed_marketplace = _billed_marketplace
call_routes._buffer_response = _buffer_response
call_routes._caller_request_snippet = _caller_request_snippet
call_routes._catalog_endpoint_for = _catalog_endpoint_for
call_routes._enforce_daily_cap = _enforce_daily_cap
call_routes._enforce_deny = _enforce_deny
call_routes._enforce_public_demo_ip_cap = _enforce_public_demo_ip_cap
call_routes._enforce_tag_budgets = _enforce_tag_budgets
call_routes._error_response_evidence = _error_response_evidence
call_routes._finish_cancelled_call = _finish_cancelled_call
call_routes._may_have_body = _may_have_body
call_routes._now_ms = _now_ms
call_routes._oauth_billed_provider = _oauth_billed_provider
call_routes._peek_stream_head = _peek_stream_head
call_routes._platform_reserve = _platform_reserve
call_routes._platform_settle = _platform_settle
call_routes._record_first_call = _record_first_call
call_routes._redact_snippet = _redact_snippet
call_routes._relay_live_demo = _relay_live_demo
call_routes._require_tool_use_http = _require_tool_use_http
call_routes._resolve_marketplace_call = _resolve_marketplace_call
call_routes._safe_secret_renderings = _safe_secret_renderings
router.routes.extend(call_routes.router.routes)




# ---- server-side CLI execution (Tier 0 `treg run`) ---------------------------------------
class RunIn(BaseModel):
    tool: str             # the tool name in the caller's org (its `cli` profile drives execution)
    args: list[str] = []  # argv passed to the CLI (secrets are injected via env, never here)
    timeout_s: int | None = None


@app.post("/run")
async def run_tool_server(
    body: RunIn, request: Request,
    caller: Caller = Depends(require_member), db: AsyncSession = Depends(get_session),
) -> dict:
    """Run a tool's CLI **on the treg server**, with its `cli.inject` secrets injected into the
    child process — the caller never holds the key. Both run tiers read the same `Tool.cli`
    profile; any tool WITH a profile is server-runnable (no per-tool opt-in — unlike the local
    tier, the key never reaches the member, and the bin allow-list still gates what executes).
    See docs/CLI-RUN-PLAN.md.

    member+ (executing argv server-side is a register-tier capability, not a read); the sandbox is
    excluded (it never touches the real world). A non-zero CLI exit is a normal 200 result with
    `exit_code` set; only a failure to *start* (not enabled / CLI absent) is a 4xx."""
    _require_can_register(caller)
    if demo_sandbox.is_sandbox(caller.org):
        raise HTTPException(status_code=403, detail="CLI run is disabled in the sandbox")
    tool = (
        await db.execute(select(Tool).where(Tool.name == body.tool, Tool.org_id == caller.org_id))
    ).scalar_one_or_none()
    if tool is None:
        raise HTTPException(status_code=404, detail=f"no tool {body.tool!r} in this org")
    _require_tool_use_http(caller, tool)  # per-member tool + project ACL
    # A run executes a CLI, so there is no request path to match — evaluate the tool's own upstream
    # host, which is what a host-level rule ("nobody may reach api.stripe.com") is really saying.
    await _enforce_deny(caller, tool.base_url, "", db, tool.project_id)
    await _enforce_daily_cap(caller, db)  # a server run counts toward the per-user daily cap
    try:
        exec_bin = runner.resolve_exec_bin(tool)  # the SAME resolution run_tool execs — never diverges
    except runner.RunError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if exec_bin not in _allowed_server_bins():
        raise HTTPException(status_code=422, detail=(
            f"{exec_bin!r} is not approved for server runs — only catalog-known CLIs may run on the "
            "server (an admin can allow more via TREG_RUN_ALLOWED_BINS). Use `treg run --local` instead."))
    timeout = max(1, min(body.timeout_s or runner.DEFAULT_TIMEOUT_S, 600))
    try:
        async with runner.run_slot(caller.email):  # cap concurrent server runs (global + per-user)
            result = await runner.run_tool(tool, list(body.args), db, timeout_s=timeout)
    except runner.RunBusy as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except runner.RunError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    audit.record_run(
        org_id=caller.org_id, user_email=caller.email, bundle_name=tool.name,
        argv=_redact_argv_list(list(body.args)),  # redact any credential typed inline before it's stored
        exit_code=result.exit_code, duration_ms=result.duration_ms, client=_client_of(request),
    )
    return {
        "tool": tool.name,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": result.duration_ms,
        "timed_out": result.timed_out,
    }


# ---- view helpers (never leak secret values) ----------------------------------------------




# Deployment and imports keep using `treg.api:app`; the concrete assembly now lives in bootstrap.
from .bootstrap import create_app  # noqa: E402

app = create_app()
# Moved handlers retain api.py's original final global binding for calls such as app.openapi().
web_routes.app = app
