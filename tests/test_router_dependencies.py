"""Compatibility checks for the shared router dependency extraction."""

from treg import api, timeutil
from treg.routers import dependencies


def test_api_reexports_shared_http_dependencies() -> None:
    names = (
        "Caller",
        "_is_https",
        "_membership_by_token",
        "_resolve_org",
        "_role_at_least",
        "_user_from_identity_token",
        "_user_from_session",
        "require_identity",
        "require_member",
        "require_superadmin",
    )
    for name in names:
        assert getattr(api, name) is getattr(dependencies, name)


def test_api_reexports_shared_time_convention() -> None:
    assert api._utcnow_naive is timeutil.utcnow_naive
    assert api._as_naive is timeutil.as_naive
