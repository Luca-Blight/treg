"""HTTP routes for first-run team onboarding."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..application import onboard as onboard_use_cases
from ..domain.identity.access import (
    Caller,
    _require_can_register,
    require_identity,
    require_member,
)
from ..models import User
from .orgs import _require_admin_of


# app is the APIRouter alias so mechanically moved @app decorators stay byte-identical.
app = APIRouter()


_ONBOARD_HTTP_ERRORS = {
    "org_not_found": (404, "org not found"),
    "not_demo_email": (400, "onboarding auto-accept is for demo teammates only"),
    "invite_not_found": (404, "no pending invite for that email"),
}


def _onboard_http_error(exc: onboard_use_cases.OnboardError) -> HTTPException:
    status_code, detail = _ONBOARD_HTTP_ERRORS[exc.kind]
    return HTTPException(status_code=status_code, detail=detail)


class OnboardIn(BaseModel):
    team_name: str = "Acme Design"


@app.post("/onboard/demo")
async def onboard_demo(
    body: OnboardIn | None = None,
    user: User = Depends(require_identity),
) -> dict:
    """Seed a sandbox team owned by the caller — fake teammates (one per role) + a working `echo`
    tool + sample activity — so a brand-new user can feel the product immediately. Idempotent
    (reuses an existing demo team); marks the caller onboarded. Same seed for dashboard + CLI."""
    return await onboard_use_cases.provision_demo(
        user_id=user.id, team_name=(body.team_name if body else "Acme Design"))


@app.post("/onboard/skip")
async def onboard_skip(
    user: User = Depends(require_identity),
) -> dict:
    """Dismiss onboarding without seeding — so it's never auto-offered again."""
    return await onboard_use_cases.skip(user_id=user.id)


@app.post("/onboard/reset")
async def onboard_reset(
    user: User = Depends(require_identity),
) -> dict:
    """Remove the caller's demo team(s) + demo teammates from their real teams — a clean exit."""
    return await onboard_use_cases.reset(user_id=user.id)


onboard_entry_router = app

# The second router preserves the later attachment point after the landing-sandbox routes.
app = APIRouter()


class TeammateIn(BaseModel):
    email: str


@app.post("/onboard/seed-tool")
async def onboard_seed_tool(
    caller: Caller = Depends(require_member),
) -> dict:
    """Pre-seed the working `echo` tool into the caller's active team so the no-key call in the
    dashboard onboarding just works (the user builds the team + invites by hand; the tool is on us)."""
    _require_can_register(caller)
    try:
        return await onboard_use_cases.seed_tool(
            org_id=caller.org_id, owner_email=caller.email)
    except onboard_use_cases.OnboardError as exc:
        raise _onboard_http_error(exc) from exc


@app.post("/onboard/accept-teammate")
async def onboard_accept_teammate(
    body: TeammateIn, caller: Caller = Depends(require_member),
) -> dict:
    """Auto-accept the fake teammate the user just invited during onboarding, so it lands in the
    roster instantly (they feel the invite, then see the loop close). Admin+ only, demo email only."""
    _require_admin_of(caller.org_id, caller)
    try:
        return await onboard_use_cases.accept_teammate(
            org_id=caller.org_id, email=body.email)
    except onboard_use_cases.OnboardError as exc:
        raise _onboard_http_error(exc) from exc


onboard_teammate_router = app
