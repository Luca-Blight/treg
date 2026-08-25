---
title: Application composition and deployment roles
status: shipped
sources:
  - src/treg/bootstrap.py
  - src/treg/routers/web.py
  - scripts/dump_surface.py
related:
  - architecture/import-boundaries.md
  - interface/api.md
  - architecture/mcp-oauth.md
  - ops/deploy.md
---

# Application composition

`bootstrap.create_app(role)` is the FastAPI composition root. `api.py` hosts the ordered route table;
the Catalog and web modules define concern-specific `APIRouter` blocks that `api.py` appends at their
legacy registration points. It then calls the factory once at EOF so the deployed and documented
`treg.api:app` import path remains the default `all` role.

The factory owns concrete assembly: the three middleware registrations, five exception handlers,
static mounts, optional MCP mount and lifespan, GET-to-HEAD widening, the OpenAPI wrapper that hides
implied HEAD operations, shared HTTP client creation, startup work, shutdown drains, and the Ads
conversion worker. Registration order is compatibility behavior. The four stage-0 snapshots stay
byte-identical for `role="all"`.

## Role manifests

Every created app exposes `app.state.role_manifest` with explicit `routes`, `background_tasks`, and
`startup_checks` lists. `tests/test_app_roles.py` pins all three lists for every role.

| Role | HTTP routes and mounts | Background tasks | Startup checks |
|---|---|---|---|
| `all` | The complete existing surface, including `/run`, static files, and `/mcp` | Ads conversion worker when enabled | DB init, provider-tool backfill, single-user bootstrap, HTTP client, MCP lifespan |
| `dataplane` | Only `/call/{rest:path}`; no `/run`, static files, docs, OpenAPI, or MCP | None | DB init, provider-tool backfill, HTTP client |
| `control` | Everything except `/call/{rest:path}`; includes `/run`, static files, and `/mcp` | Ads conversion worker when enabled | DB init, provider-tool backfill, single-user bootstrap, HTTP client, MCP lifespan |

`_CONTROL_ROUTE_KEYS` and `_DATAPLANE_ROUTE_KEYS` assign every `api.router` route to exactly one
owner. App creation fails on an unclassified, stale, duplicate, or multiply-owned key, so adding a
route cannot silently expand the dataplane. Role separation is preparatory in stage 1; only the
`all` role is deployed.

## Route cloning

Each factory call must produce an independent app whose dependency overrides belong to that app.
`_include_routes` therefore shallow-clones every `APIRoute`, points its dependency override provider
at the new FastAPI instance, and rebuilds its request handler. This also avoids the internal
`_IncludedRouter` wrapper added by the current FastAPI `include_router()` implementation, which would
otherwise change route inspection and the committed surface snapshot.

`scripts.dump_surface._lifespan` records the optional MCP lifespan condition against
`treg.bootstrap._mcp`, where optional MCP composition now lives. This is a documentation-only snapshot
correction; the mounted lifespan behavior is unchanged.
