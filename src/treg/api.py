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
from .bootstrap_handlers import _mark_treg_own_errors, _pool_saturated
from .routers import admin as admin_routes
from .routers import billing as billing_routes
from .routers import call as call_routes
from .routers import referrals as referral_routes
from .routers import onboard as onboard_routes
from .routers.call import (
    _enforce_daily_cap,
    _enforce_public_demo_ip_cap,
    _parse_call_meta,
    _refusal_kind,
    _release_idempotent_claim,
    _require_tool_use_http,
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
from .domain.governance import budgets as budget_policy
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
from .application.call.resolve import (
    MarketplaceCall,
    _LIMIT_PARAMS,
    _PLATFORM_PAGE_DEFAULT,
    _PLATFORM_PAGE_MAX,
    _VALID_PERCENT_ESCAPE_RE,
    _billed_endpoint_match,
    _billed_marketplace,
    _body_limit,
    _capability_alternatives,
    _catalog_endpoint_for,
    _credit_modifiers,
    _enforce_capability_pin,
    _enforce_catalog_status,
    _input_count,
    _json_object,
    _marketplace_no_credential,
    _marketplace_pricing,
    _marketplace_secret,
    _marketplace_upstream,
    _may_have_body,
    _oauth_billed_estimate,
    _oauth_billed_provider,
    _params_hash,
    _platform_bindings,
    _platform_estimate_micro,
    _platform_offer,
    _post_has_link,
    _resolve_call,
    _resolve_marketplace_call,
    _truthy,
    _usd_to_micro,
)
from .application.call.reserve import (
    _enforce_platform_daily_cap,
    _enforce_tag_budgets,
    _enforce_trial_allowance,
    _month_start_utc,
    _platform_reserve,
    _resolve_tag_budget,
)
from .application.call.evidence import (
    _ERROR_BODY_SLICE,
    _ERROR_CALLER_BODY_MAX,
    _ERROR_MASKING_FAILED,
    _ERROR_REQUEST_MAX,
    _ERROR_RESPONSE_MAX,
    _EVIDENCE_HEADERS,
    _EVIDENCE_SECRET_RE,
    _QUERY_CRED_RE,
    _SENSITIVE_JSON_SECRET_KEYS,
    _URL_USERINFO_RE,
    _basic_credential_parts,
    _caller_request_snippet,
    _decode_error_body,
    _error_response_evidence,
    _redact_snippet,
    _safe_secret_renderings,
    _secret_renderings,
)
from .application.call.settle import (
    _NOT_THE_CALLERS_FAULT,
    _PLATFORM_BODY_MAX,
    _brightdata_record_count,
    _buffer_response,
    _finish_cancelled_call,
    _observed_cost_micro,
    _peek_stream_head,
    _platform_billable,
    _platform_settle,
    _record_first_call,
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






router.routes.extend(auth_routes.grants_router.routes)


router.routes.extend(org_routes.org_entry_router.routes)


router.routes.extend(org_routes.invite_entry_router.routes)


router.routes.extend(onboard_routes.onboard_entry_router.routes)


# ---- per-user daily usage cap (usage-metering v1) -------------------------------------------




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



# ---- tier-4 metering: reserve → relay → settle/release ------------------------------------------



























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
