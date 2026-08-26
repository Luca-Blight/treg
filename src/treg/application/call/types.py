"""Framework-neutral call application contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Blame = Literal["caller", "treg", "upstream", "org_connection"]


class CallFailure(Exception):
    """A call failure translated once by the HTTP adapter."""

    def __init__(
        self,
        kind: str,
        *,
        blame: Blame,
        status_code: int,
        detail: str | dict,
    ) -> None:
        super().__init__(str(detail))
        self.kind = kind
        self.blame = blame
        self.status_code = status_code
        self.detail = detail


class IntakeFailed(CallFailure):
    """Caller metadata cannot enter the call pipeline."""


class IdempotencyFailed(CallFailure):
    """An idempotency label conflicts with its stored use or active owner."""


@dataclass(frozen=True)
class IdempotentReplay:
    body: bytes
    status_code: int
    media_type: str
    charged_micro: int
    call_ref: str
