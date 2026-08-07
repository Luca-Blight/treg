"""Team policy on WHICH provider serves a job.

The agent chooses (see docs/CAPABILITY-CHOICE-PLAN.md) — a pin is where the team's decision stops
that freedom. These pin the parts that are judgement, not plumbing: that a pin is a GATE rather than
a hint, that a typo cannot silently block a real job, and that a refusal tells the caller what to do
instead.
"""

from __future__ import annotations

from httpx import AsyncClient

CAP = "tiktok.user.profile"
PINNED = "tikhub"
OTHER_EP = "justoneapi.tiktok.user.profile"


async def _org_id(c: AsyncClient) -> int:
    return (await c.get("/orgs")).json()[0]["org_id"]


async def test_a_pin_is_a_gate_not_a_hint(clients: AsyncClient):
    """A hint is honoured by a well-behaved agent and ignored by the one you needed to stop. The
    refusal also names the endpoint the team DOES use — told only "no", an agent tries the next
    provider and is refused again."""
    org = await _org_id(clients)
    r = await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": PINNED})
    assert r.status_code == 200, r.text

    blocked = await clients.get(f"/call/{OTHER_EP}?uniqueId=tiktok")
    assert blocked.status_code == 403
    detail = blocked.json()["detail"]
    assert detail["error"] == "capability_pinned"
    assert detail["pinned_provider"] == PINNED
    assert detail["use_endpoint"].startswith(PINNED)      # "use this instead", not just "no"


async def test_the_suggested_endpoint_is_the_curated_one(clients: AsyncClient):
    """`core` is the curated route for a job; `extended` is the bulk-ingested long tail. Suggesting
    an obscure extended id when a core one exists reads as a broken suggestion."""
    org = await _org_id(clients)
    await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": PINNED})
    detail = (await clients.get(f"/call/{OTHER_EP}?uniqueId=tiktok")).json()["detail"]
    assert detail["use_endpoint"] == "tikhub.tiktok.user.profile"


async def test_a_typo_fails_loudly_at_pin_time(clients: AsyncClient):
    """A pin naming a capability nobody serves, or a provider that does not serve it, would block
    every call to a job the team really uses — and it would be discovered at 3am in an agent's log."""
    org = await _org_id(clients)
    bad_cap = await clients.post(f"/orgs/{org}/pins",
                                 json={"capability": "not.a.capability", "provider": PINNED})
    assert bad_cap.status_code == 422

    bad_prov = await clients.post(f"/orgs/{org}/pins",
                                  json={"capability": CAP, "provider": "stripe"})
    assert bad_prov.status_code == 422
    assert "does not serve" in bad_prov.json()["detail"]
    assert PINNED in bad_prov.json()["detail"]          # ...and says who does


async def test_repinning_replaces_rather_than_stacking(clients: AsyncClient):
    org = await _org_id(clients)
    await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": PINNED})
    await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": "justoneapi"})
    pins = (await clients.get(f"/orgs/{org}/pins")).json()
    assert [p["provider"] for p in pins if p["capability"] == CAP] == ["justoneapi"]


async def test_unpinning_returns_the_choice_to_the_caller(clients: AsyncClient):
    org = await _org_id(clients)
    await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": PINNED})
    assert (await clients.delete(f"/orgs/{org}/pins/{CAP}")).status_code == 200
    # the previously-blocked provider is no longer refused BY THE PIN (it may still need a credential)
    r = await clients.get(f"/call/{OTHER_EP}?uniqueId=tiktok")
    assert r.status_code != 403 or (r.json().get("detail") or {}).get("error") != "capability_pinned"


async def test_members_can_read_the_pins_they_must_obey(clients: AsyncClient):
    """An agent has to know what it may call; learning it by being refused is a wasted round-trip."""
    org = await _org_id(clients)
    await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": PINNED})
    r = await clients.get(f"/orgs/{org}/pins")
    assert r.status_code == 200 and r.json()[0]["capability"] == CAP


async def test_a_pin_is_scoped_to_one_org(clients: AsyncClient):
    """Another team's decision must never refuse your call."""
    org = await _org_id(clients)
    await clients.post(f"/orgs/{org}/pins", json={"capability": CAP, "provider": PINNED})
    r = await clients.post("/users", json={"email": "stranger@elsewhere.dev"})
    other = {"X-Treg-Token": r.json()["token"]}
    assert (await clients.get(f"/call/{OTHER_EP}?uniqueId=tiktok", headers=other)).status_code != 403 \
        or "capability_pinned" not in (await clients.get(f"/call/{OTHER_EP}", headers=other)).text
