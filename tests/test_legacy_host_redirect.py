"""The legacy-host redirect (treg.superdesign.dev → treg.to).

Only browser-facing marketing/doc pages 301 to the canonical host. API surfaces must be served in
place on the legacy host forever: installed CLIs/skills point there with Bearer tokens, and HTTP
clients strip Authorization on a cross-host redirect (some MCP clients follow no redirects at all).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from treg.api import app


from treg.config import get_settings


@pytest.fixture
async def raw_client(monkeypatch):
    """No auth, no upstream — routing behavior only. Host is set per-request. The test env's
    public_url is localhost; pin it to production's so the Location assertions mean something."""
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://treg.to")
    get_settings.cache_clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
        yield c
    get_settings.cache_clear()


LEGACY = {"host": "treg.superdesign.dev"}


async def test_marketing_pages_redirect_to_canonical(raw_client):
    for path in ("/", "/terms", "/privacy", "/support", "/tutorial", "/login"):
        r = await raw_client.get(path, headers=LEGACY)
        assert r.status_code == 301, path
        assert r.headers["location"] == f"https://treg.to{path}", path


async def test_query_string_survives_the_redirect(raw_client):
    r = await raw_client.get("/?utm_source=x", headers=LEGACY)
    assert r.status_code == 301
    assert r.headers["location"] == "https://treg.to/?utm_source=x"


async def test_api_surfaces_are_served_in_place_on_the_legacy_host(raw_client):
    # Never redirected — a 301 here would strand every installed client. Whatever these routes
    # answer (200, 401, 405…), it must not be a redirect off-host.
    for path in ("/meta", "/llms.txt", "/install.sh", "/selfhost.sh", "/skill.md",
                 "/catalog/search", "/auth/me", "/billing/stripe/webhook"):
        r = await raw_client.get(path, headers=LEGACY)
        assert r.status_code != 301, path


async def test_post_is_never_redirected(raw_client):
    r = await raw_client.post("/", headers=LEGACY)
    assert r.status_code != 301


async def test_canonical_host_is_untouched(raw_client):
    r = await raw_client.get("/", headers={"host": "treg.to"})
    assert r.status_code == 200
