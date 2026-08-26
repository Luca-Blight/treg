"""Framework-neutral call application contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable, Literal

if TYPE_CHECKING:
    from ...models import Tool


Blame = Literal["caller", "treg", "upstream", "org_connection"]

_BLAME_BY_KIND: dict[str, Blame] = {
    "metadata_invalid": "caller",
    "metadata_pin_mismatch": "caller",
    "idempotency_mismatch": "caller",
    "idempotency_in_progress": "treg",
    "invalid_target": "caller",
    "tool_access_denied": "caller",
    "target_not_found": "caller",
    "target_ambiguous": "caller",
    "catalog_retired": "caller",
    "catalog_parameter_invalid": "caller",
    "capability_pinned": "caller",
    "policy_denied": "caller",
    "daily_cap_reached": "caller",
    "public_demo_rate_limited": "caller",
    "trial_allowance_unavailable": "treg",
    "trial_allowance_reached": "caller",
    "platform_cap_unavailable": "treg",
    "platform_daily_cap_reached": "caller",
    "tag_budget_unavailable": "treg",
    "tag_cardinality_exceeded": "caller",
    "tag_blocked": "caller",
    "tag_call_cap_reached": "caller",
    "tag_spend_cap_reached": "caller",
    "insufficient_balance": "caller",
    "injection_failed": "treg",
    "credential_missing": "org_connection",
    "method_mismatch": "caller",
}


class CallFailure(Exception):
    """A call failure translated once by the HTTP adapter."""

    def __init__(
        self,
        kind: str,
        *,
        status_code: int,
        detail: str | dict,
    ) -> None:
        super().__init__(str(detail))
        self.kind = kind
        self.blame = _BLAME_BY_KIND[kind]
        self.status_code = status_code
        self.detail = detail


class IntakeFailed(CallFailure):
    """Caller metadata cannot enter the call pipeline."""


class IdempotencyFailed(CallFailure):
    """An idempotency label conflicts with its stored use or active owner."""


class ResolutionFailed(CallFailure):
    """The requested tool or marketplace target cannot be resolved."""


class AuthorizationFailed(CallFailure):
    """A resolved call target is refused before any money is reserved."""


class ReservationFailed(CallFailure):
    """A metered call is refused before its reservation commits."""


@dataclass(frozen=True)
class IdempotentReplay:
    body: bytes
    status_code: int
    media_type: str
    charged_micro: int
    call_ref: str


@dataclass(frozen=True)
class ResolvedTarget:
    tool: Tool
    upstream: str


@dataclass
class UpstreamResponse:
    status: int
    raw_headers: tuple[tuple[bytes, bytes], ...]
    body_stream: AsyncIterator[bytes]
    close: Callable[[], Awaitable[None]]
