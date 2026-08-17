"""Google Ads conversion tracking — the outbox and its uploader.

Unlike audit.py and analytics.py, which are deliberately droppable, a conversion that is
lost is a conversion Google never learns about, and the bidding is then trained on
undercounted data. So the write is DURABLE (a row, in the caller's transaction) and only
the UPLOAD is asynchronous. Nothing here may route through audit.py.
"""

from __future__ import annotations

# Fixed FX, set 2026-08-17: 1 AUD = 0.70 USD. Deliberately a constant rather than a live
# rate so reported conversion value stays stable — a change in ROAS should mean the
# business moved, not that the currency market did. Revisit if the rate drifts far.
AUD_PER_USD_NUM = 10
AUD_PER_USD_DEN = 7

ACTION_SIGNUP = "signup"
ACTION_FIRST_CALL = "first_call"
ACTION_PAID = "paid"

# Created live on account 5149790776 on 2026-08-17 (type UPLOAD_CLICKS).
CONVERSION_ACTION_IDS: dict[str, str] = {
    ACTION_SIGNUP: "7723667014",
    ACTION_FIRST_CALL: "7723667017",
    ACTION_PAID: "7723667020",
}


def usd_micro_to_aud_micro(usd_micro: int) -> int:
    """Convert integer micro-USD to integer micro-AUD at the fixed rate.

    Integer-only, per the money-code rule: a float here would round differently on
    different platforms and the value is uploaded as a monetary amount.

    Note: // floors toward negative infinity, so negative amounts round away from zero
    while positive amounts round toward zero. Real inputs are always positive
    (top-ups); the negative case is defensive only.
    """
    return usd_micro * AUD_PER_USD_NUM // AUD_PER_USD_DEN
