"""Compatibility facade for the upstream relay owner."""

import sys

from .infra.upstream import relay as _implementation

sys.modules[__name__] = _implementation
