"""First-run onboarding journeys and their transaction boundaries."""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import demo as demo_seed
from ..db import session_maker
from ..domain.identity.access import _norm_email
from ..models import Invite, Org, User


class OnboardError(Exception):
    """A framework-neutral onboarding refusal translated by the HTTP router."""

    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


async def _user(user_id: int, db: AsyncSession) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise RuntimeError("authenticated user disappeared")
    return user


async def provision_demo(*, user_id: int, team_name: str) -> dict:
    async with session_maker() as db:
        return await demo_seed.provision(db, await _user(user_id, db), team_name)


async def skip(*, user_id: int) -> dict:
    async with session_maker() as db:
        user = await _user(user_id, db)
        user.onboarded = True
        await db.commit()
        return {"onboarded": True}


async def reset(*, user_id: int) -> dict:
    async with session_maker() as db:
        return await demo_seed.reset(db, await _user(user_id, db))


async def seed_tool(*, org_id: int, owner_email: str) -> dict:
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        if org is None:
            raise OnboardError("org_not_found")
        return await demo_seed.seed_tool(db, org, owner_email)


async def accept_teammate(*, org_id: int, email: str) -> dict:
    async with session_maker() as db:
        email = _norm_email(email)
        if not email.endswith("@" + demo_seed.DEMO_DOMAIN):
            raise OnboardError("not_demo_email")
        invite = (await db.execute(select(Invite).where(
            Invite.org_id == org_id,
            Invite.email == email,
            Invite.status == "pending",
        ))).scalar_one_or_none()
        if invite is None:
            raise OnboardError("invite_not_found")
        return await demo_seed.accept_demo_invite(db, org_id, invite)
