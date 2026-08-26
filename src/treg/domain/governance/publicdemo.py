"""Shared public-demo call limiter policy."""

from typing import Callable

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ... import ratestore


_client_ip: Callable[[Request], str]


# Per-IP limiter for /call with a PUBLIC-DEMO token (the landing page publishes one shared member
# token, so the per-user daily cap is meaningless there — thousands of strangers are one "user").
PUBLIC_DEMO_HIT_NS = "pubdemo_call"
PUBLIC_DEMO_RATE_MAX = 10      # calls per IP per window
PUBLIC_DEMO_RATE_WINDOW_S = 60


async def _enforce_public_demo_ip_cap(request: Request, db: AsyncSession) -> None:
    """Per-IP cap for a call made with a SHARED public credential — the published demo token or
    the sandbox live wire. Both are one identity for thousands of strangers, so meter by client IP
    rather than by user. Commits the sweep + recorded hit (get_session never auto-commits) and
    raises 429 when the window is exhausted."""
    await ratestore.sweep(db, PUBLIC_DEMO_HIT_NS)
    allowed = await ratestore.rate_check(
        db, PUBLIC_DEMO_HIT_NS, [(_client_ip(request), PUBLIC_DEMO_RATE_MAX)], PUBLIC_DEMO_RATE_WINDOW_S)
    await db.commit()
    if not allowed:
        raise HTTPException(status_code=429, detail=(
            f"demo limit reached ({PUBLIC_DEMO_RATE_MAX} calls/min per IP) — try again in a minute"))
