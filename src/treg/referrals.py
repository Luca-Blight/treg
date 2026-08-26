"""Compatibility alias for the referral domain."""

import sys

from .domain import referrals as _referrals


sys.modules[__name__] = _referrals
