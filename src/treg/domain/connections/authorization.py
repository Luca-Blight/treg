"""Authorization-method rules for provider connections.

The provider registry supplies data. This module owns the reusable decisions for providers that
offer more than one grant protocol. It has no HTTP or dashboard dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class AuthorizationMethod:
    """One explicit grant method for a logical provider."""

    name: str
    display_name: str
    capabilities: tuple[str, ...]
    connection_name: str
    description: str
    connect_capability: str = ""
    action_label: str = "Add account"
    missing_message: str = ""
    capability_intros: tuple[tuple[str, str], ...] = ()
    capability_details: tuple[tuple[str, tuple[str, ...]], ...] = ()
    scope_aliases: tuple[tuple[str, str], ...] = ()
    scope_riders: tuple[str, ...] = ()
    scope_riders_by_scope: tuple[tuple[str, str], ...] = ()
    overrides: tuple[tuple[str, object], ...] = ()


def method_for_capability(provider: Any, capability: str) -> AuthorizationMethod | None:
    """Return the grant method that owns a capability."""
    matches = [method for method in provider.authorization_methods
               if capability in method.capabilities]
    if len(matches) > 1:
        raise ValueError(
            f"{provider.service} capability {capability!r} has multiple authorization methods"
        )
    if not matches:
        if provider.authorization_methods:
            raise ValueError(
                f"{provider.service} capability {capability!r} has no authorization method"
            )
        return None
    return matches[0]


def method_name(provider: Any, stored: str) -> str:
    """Normalize a stored method, including the provider's declared legacy value."""
    if stored or not provider.authorization_methods:
        return stored
    return provider.legacy_authorization_method or method_for_capability(
        provider, provider.default_capability
    ).name


def provider_profile(provider: Any, method: str) -> Any:
    """Return the protocol profile for one grant method."""
    if not provider.authorization_methods:
        return provider
    name = method_name(provider, method)
    selected = next(
        (item for item in provider.authorization_methods if item.name == name), None
    )
    if selected is None:
        raise ValueError(f"{provider.service} has no authorization method {name!r}")
    return replace(
        provider,
        **dict(selected.overrides),
        authorization_methods=(),
        default_capability_name="",
    )


def endpoint_methods(endpoint: dict) -> tuple[str, ...]:
    """Return supported methods with the endpoint default first."""
    methods = endpoint.get("authorization_methods") or []
    if not methods and endpoint.get("authorization_method"):
        methods = [endpoint["authorization_method"]]
    default = endpoint.get("authorization_method") or ""
    if not default:
        return tuple(methods)
    return tuple([default] + [method for method in methods if method != default])


def select_endpoint_methods(
    endpoint: dict, requested: str,
) -> tuple[str, ...]:
    """Validate an optional caller choice and return the methods to try in order."""
    methods = endpoint_methods(endpoint)
    selected = requested.strip().lower()
    if not selected:
        return methods
    if not methods:
        raise ValueError(
            f"{endpoint['id']} does not support authorization-method selection"
        )
    if selected not in methods:
        raise ValueError(
            f"{endpoint['id']} does not support {selected}; choose " + " or ".join(methods)
        )
    return (selected,)


def method_spec(provider: Any, method: str) -> AuthorizationMethod | None:
    """Return presentation and scope metadata for one grant method."""
    return next(
        (item for item in provider.authorization_methods if item.name == method), None
    )


def required_scopes(endpoint: dict, method: AuthorizationMethod | None) -> list[str]:
    """Translate endpoint scopes into the selected grant's scope dialect."""
    declared = list(endpoint.get("required_scopes") or [])
    if method is None:
        return declared
    aliases = dict(method.scope_aliases)
    result = [aliases.get(scope, scope) for scope in declared]
    result.extend(method.scope_riders)
    result.extend(
        rider for source, rider in method.scope_riders_by_scope if source in declared
    )
    return list(dict.fromkeys(result))
