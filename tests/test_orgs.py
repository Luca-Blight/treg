

# ---- deleting a team must clear everything that points at it -------------------------------

async def test_org_delete_clears_EVERY_org_scoped_table(clients):
    """The list in `_cascade_delete_org` has to stay in step with the schema, and twice it did not:
    the money tables arrived with the prepaid balance and `CapabilityPin` with capability pins, and
    neither was added. The effect was invisible until someone tried it — and because every NEW team
    is granted $1.00, every team has a CreditBlock, so NO team could be deleted at all. Production
    returned a bare 500.

    So this walks the models module rather than restating the list: any future model carrying an
    `org_id` fails here until it is handled."""
    import inspect

    from sqlmodel import SQLModel

    from treg import models as m
    from treg.api import _ORG_SCOPED_MODELS

    covered = {model.__name__ for model in _ORG_SCOPED_MODELS}
    missing = []
    for name, obj in vars(m).items():
        if not (inspect.isclass(obj) and issubclass(obj, SQLModel) and obj is not SQLModel):
            continue
        table = getattr(obj, "__table__", None)
        if table is not None and "org_id" in table.columns and name not in covered:
            missing.append(name)
    assert not missing, (
        f"these models carry org_id but _cascade_delete_org never deletes them: {missing}. "
        f"A team holding any of these rows cannot be deleted — the foreign key fails with a 500.")


async def test_a_team_with_a_BALANCE_can_still_be_deleted(clients):
    """The exact production failure, end to end. A team is granted $1.00 on creation, so it owns a
    CreditBlock and a LedgerEntry from its first moment — which is precisely why every delete broke.

    WORTH KNOWING: this test passes even with the bug present. The suite runs on SQLite, which does
    not enforce foreign keys by default; production is Postgres, which does. That difference is why
    the bug shipped and why the tests were quiet about it. The guard that actually holds is
    `test_org_delete_clears_EVERY_org_scoped_table` above, which checks the SCHEMA rather than the
    behaviour. This test is kept because it documents the real-world shape of the failure, but do not
    mistake it for the protection."""
    r = await clients.post("/orgs", json={"name": "throwaway-team"})
    assert r.status_code == 200, r.text
    org_id, slug, token = r.json()["org_id"], r.json()["org"], r.json()["token"]
    # the new team's OWN token: a per-org token has its org baked in, so X-Treg-Org cannot switch it
    hdr = {"X-Treg-Token": token}

    bal = await clients.get(f"/orgs/{org_id}/balance", headers=hdr)
    assert bal.status_code == 200 and bal.json()["balance_micro"] > 0, (
        "expected the new-team grant — without a balance this test would not reproduce the bug")

    gone = await clients.request("DELETE", f"/orgs/{org_id}", params={"confirm": slug}, headers=hdr)
    assert gone.status_code == 200, gone.text
    assert (await clients.get(f"/orgs/{org_id}/balance", headers=hdr)).status_code != 200
