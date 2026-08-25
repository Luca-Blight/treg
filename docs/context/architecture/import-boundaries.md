---
title: Enforced import boundaries
status: shipped
sources:
  - pyproject.toml
  - .github/workflows/ci.yml
  - tests/test_import_lightness.py
related:
  - architecture/composition.md
  - architecture/money.md
  - interface/cli.md
---

# Enforced import boundaries

Import Linter reads the contracts under `tool.importlinter` in `pyproject.toml`. The main CI `test`
job installs the hand-maintained lock with `uv sync --frozen`, then runs
`uv run --frozen lint-imports` before the test suite. Keeping the check in that job reuses the
development environment and avoids a second install for a fast static architecture check.

Stage 1 activates two contracts:

- The explicit lightweight CLI module list cannot directly import any server-extra package, including
  FastAPI, SQLModel, SQLAlchemy, Alembic, database drivers, MCP, Stripe, or cryptography. Imports guarded
  by `TYPE_CHECKING` are excluded globally because they cannot load at runtime. Indirect imports are
  allowed by this contract because optional proxy dependencies may appear in lazily executed internal
  modules; the named CLI modules themselves must remain free of direct server imports.
- `treg.ledger` cannot import `treg.audit`. Money correctness never flows through the best-effort audit
  path, whose writes may be shed under load.

The router package now contains shared HTTP dependencies, but its no-import-from-`api` contract remains
scheduled for the final Stage 2 movement commit. Application and domain layer contracts remain absent
until those packages are migrated in later stages. Activating them earlier would describe a target tree
rather than enforce the current one.

Two direct edges are precise exceptions. `cli.ensure_proxy_dependency` imports `cryptography` only after
the user invokes the optional proxy feature and offers to install the proxy extra first.
`localrun.render_grant` imports SQLModel only when the server executes the grant path. Import Linter treats
function-local imports as ordinary direct edges, so both appear in `ignore_imports`; unmatched ignores are
errors, ensuring a removed or renamed edge cannot leave a stale exception behind.

An ignore covers an entire module edge and therefore cannot detect someone moving either lazy import to
module scope. `tests.test_import_lightness` closes that gap by starting an isolated Python subprocess,
importing every lightweight module, and asserting that no server dependency root appears in `sys.modules`.
Base dependencies such as httpx and questionary remain allowed.
