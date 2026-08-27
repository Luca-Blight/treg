"""Compatibility facade for billing application primitives."""

import sys

from .application import billing as _billing


sys.modules[__name__] = _billing
