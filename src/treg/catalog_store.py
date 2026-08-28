"""Compatibility facade for the catalog domain's store."""

import sys

from .domain.catalog import store as _implementation

sys.modules[__name__] = _implementation
