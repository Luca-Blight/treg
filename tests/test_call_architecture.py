"""Executable boundaries for the Stage 4 call runtime."""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

from treg import bootstrap
from treg.application import billing
from treg.application.call import authorize, reserve, settle
from treg.domain import money


_SRC = Path(__file__).parents[1] / "src" / "treg"

_DATAPLANE_DERIVED_WRITES = {
    "auto_topup_task": (
        (reserve._platform_reserve, "billing.maybe_schedule_autotopup"),
        (billing.maybe_schedule_autotopup, "loop.create_task"),
    ),
    "public_demo_ratestore_hit": (
        (authorize.authorize_call, "publicdemo_policy.enforce_public_demo_ip_cap"),
    ),
    "sandbox_ratestore_hit": (
        (authorize.enforce_public_demo_limit, "publicdemo_policy.enforce_public_demo_ip_cap"),
    ),
    "first_call_adconversion_outbox": (
        (settle._record_first_call, "adsconv.queue"),
    ),
    "lazy_stale_hold_reap": (
        (money.reserve_in_transaction, "reap_stale_holds"),
        (money.reap_stale_holds, "release"),
    ),
}
_EXPECTED_DATAPLANE_WRITES = frozenset({
    "auto_topup_task",
    "public_demo_ratestore_hit",
    "sandbox_ratestore_hit",
    "first_call_adconversion_outbox",
    "lazy_stale_hold_reap",
})
_DERIVED_WRITE_FILES = {
    _SRC / "application" / "billing.py": {"loop.create_task"},
    _SRC / "application" / "call" / "authorize.py": {
        "publicdemo_policy.enforce_public_demo_ip_cap",
    },
    _SRC / "application" / "call" / "reserve.py": {"billing.maybe_schedule_autotopup"},
    _SRC / "application" / "call" / "settle.py": {"adsconv.queue"},
    _SRC / "domain" / "governance" / "publicdemo.py": {
        "ratestore.sweep", "ratestore.rate_check",
    },
    _SRC / "domain" / "money" / "__init__.py": {"reap_stale_holds", "release"},
}
_EXPECTED_DERIVED_WRITE_SITES = {
    ("application/billing.py", "maybe_schedule_autotopup", "loop.create_task"),
    ("application/call/authorize.py", "authorize_call",
     "publicdemo_policy.enforce_public_demo_ip_cap"),
    ("application/call/authorize.py", "enforce_public_demo_limit",
     "publicdemo_policy.enforce_public_demo_ip_cap"),
    ("application/call/reserve.py", "_platform_reserve",
     "billing.maybe_schedule_autotopup"),
    ("application/call/settle.py", "_record_first_call", "adsconv.queue"),
    ("domain/governance/publicdemo.py", "enforce_public_demo_ip_cap", "ratestore.rate_check"),
    ("domain/governance/publicdemo.py", "enforce_public_demo_ip_cap", "ratestore.sweep"),
    ("domain/money/__init__.py", "reap_stale_holds", "release"),
    ("domain/money/__init__.py", "reserve_in_transaction", "reap_stale_holds"),
}


def _call_names(source: str) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(textwrap.dedent(source))):
        if not isinstance(node, ast.Call):
            continue
        parts = []
        current = node.func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        if parts:
            names.add(".".join(reversed(parts)))
    return names


def _forbidden_imports(source: str, forbidden: tuple[str, ...]) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if any(name == root or name.startswith(root + ".") for root in forbidden):
                found.add(name)
    return found


def _package_forbidden_imports(package: Path, forbidden: tuple[str, ...]) -> set[str]:
    return set().union(*(
        _forbidden_imports(path.read_text(), forbidden) for path in package.rglob("*.py")
    ))


def _transaction_calls(source: str) -> set[str]:
    return _call_names(source) & {"db.commit", "db.rollback"}


def _validate_write_allowlist(allowlist) -> None:
    assert set(allowlist) == _EXPECTED_DATAPLANE_WRITES
    for anchors in allowlist.values():
        for owner, expected_call in anchors:
            assert expected_call in _call_names(inspect.getsource(owner))


def _derived_write_sites(overrides: dict[Path, str] | None = None) -> set[tuple[str, str, str]]:
    sites = set()
    for path, markers in _DERIVED_WRITE_FILES.items():
        tree = ast.parse((overrides or {}).get(path, path.read_text()))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in _call_names(ast.unparse(node)) & markers:
                sites.add((str(path.relative_to(_SRC)), node.name, call))
    return sites


def test_call_runtime_import_edges_point_inward() -> None:
    call_forbidden = ("treg.api", "treg.bootstrap", "treg.routers", "fastapi", "starlette")
    upstream_forbidden = call_forbidden
    assert _package_forbidden_imports(_SRC / "application" / "call", call_forbidden) == set()
    assert _package_forbidden_imports(_SRC / "infra" / "upstream", upstream_forbidden) == set()


@pytest.mark.parametrize(
    ("package", "forbidden", "mutation"),
    [
        ("application/call", ("treg.api",), "from treg.api import app\n"),
        ("application/call", ("fastapi",), "from fastapi import Request\n"),
        ("infra/upstream", ("treg.routers",), "from treg.routers import call\n"),
    ],
)
def test_import_edge_contracts_reject_mutations(package, forbidden, mutation) -> None:
    assert _package_forbidden_imports(_SRC / package, forbidden) == set()
    assert _forbidden_imports(mutation, forbidden)


def test_startup_manifests_keep_dataplane_and_control_work_separate() -> None:
    assert bootstrap.ROLE_BACKGROUND_TASKS == {
        "all": ("treg.adsconv.worker",),
        "dataplane": (),
        "control": ("treg.adsconv.worker",),
    }
    assert "treg.api._bootstrap_single_user" not in bootstrap.ROLE_STARTUP_CHECKS["dataplane"]
    assert "treg.api._bootstrap_single_user" in bootstrap.ROLE_STARTUP_CHECKS["control"]
    assert "treg.mcp.mcp_lifespan" in bootstrap.ROLE_STARTUP_CHECKS["dataplane"]
    assert "treg.mcp.mcp_lifespan" not in bootstrap.ROLE_STARTUP_CHECKS["control"]


def test_dataplane_derived_write_allowlist_is_explicit_and_live() -> None:
    _validate_write_allowlist(_DATAPLANE_DERIVED_WRITES)
    assert _derived_write_sites() == _EXPECTED_DERIVED_WRITE_SITES


def test_dataplane_write_allowlist_rejects_an_unlisted_mutation() -> None:
    mutated = dict(_DATAPLANE_DERIVED_WRITES)
    mutated["unreviewed_request_write"] = ((settle._record_first_call, "db.commit"),)
    with pytest.raises(AssertionError):
        _validate_write_allowlist(mutated)
    path = _SRC / "application" / "call" / "settle.py"
    source = path.read_text() + "\nasync def unreviewed_write(db, org):\n    await adsconv.queue(db, org, 'x')\n"
    assert _derived_write_sites({path: source}) != _EXPECTED_DERIVED_WRITE_SITES


@pytest.mark.parametrize(
    "owner",
    [money.reserve_in_transaction, money.settle_in_transaction, money.release_in_transaction],
)
def test_call_money_transaction_primitives_never_commit(owner) -> None:
    source = inspect.getsource(owner)
    assert _transaction_calls(source) == set()
    mutated = source.rstrip() + "\n    await db.commit()\n"
    assert _transaction_calls(mutated) == {"db.commit"}


def test_lazy_reap_keeps_its_independent_committing_boundary() -> None:
    assert "reap_stale_holds" in _call_names(inspect.getsource(money.reserve_in_transaction))
    assert "release" in _call_names(inspect.getsource(money.reap_stale_holds))
    assert "db.commit" in _call_names(inspect.getsource(money.release))
