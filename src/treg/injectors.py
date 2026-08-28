"""Compatibility facade for the upstream injector owner."""

import sys

from .infra.upstream import injectors as _implementation

sys.modules[__name__] = _implementation
