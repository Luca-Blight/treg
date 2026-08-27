"""Compatibility facade for the catalog domain's served-call statistics."""

import sys

from .domain.catalog import stats as _implementation

sys.modules[__name__] = _implementation
