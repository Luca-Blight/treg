"""Compatibility alias for the money domain."""

import sys

from .domain import money as _money


sys.modules[__name__] = _money
