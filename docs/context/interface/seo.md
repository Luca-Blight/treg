---
title: Search surfaces — robots, sitemap, the crawlable catalog, and the social card
status: shipped
sources:
  - src/treg/api.py
  - src/treg/web/robots.txt
  - src/treg/web/catalog.css
  - src/treg/web/landing.html
  - src/treg/web/support.html
  - assets/brand/og-card.html
related:
  - interface/api.md
  - interface/dashboard.md
  - architecture/catalog.md
---

# Search surfaces

Everything a crawler, a link unfurler or an AI answer engine sees. It is one subsystem because the
pieces only work together: a sitemap is worthless without pages to list, and pages are worthless if
`HEAD` 405s before the crawl starts.

## The problem this fixed

The catalog — ~2,630 endpoints across 80 platform shelves, the entire substance of the product — had
**no URLs**. The dashboard browses platforms through hash routes (`/app#platform/<slug>`) behind a
login, and individual endpoints were expandable rows with no address at all. A crawler could reach
six thin marketing pages and nothing else. On top of that: no `robots.txt`, no `sitemap.xml`, `HEAD`
answering 405 everywhere, no `og:`/`twitter:` tags or image, no structured data, and `/docs` serving
FastAPI's stock Swagger shell — a kilobyte of JavaScript to anything that does not run scripts.

## The pieces

| Path | What it is |
|---|---|
| `/robots.txt` | Bundled file, `{BASE}`-templated. Disallows `/app`, `/login`, auth and OAuth flows, `/call/`, `/mcp`, `/admin`, `/docs/api`. Names the sitemap. |
| `/sitemap.xml` | **Generated**, not bundled — 80 of its URLs come from the catalog. Static pages take `lastmod` from their file's mtime, shelves from the newest mtime under `src/treg/catalog/`. |
| `/catalog` | Server-rendered index: every shelf, grouped by category, with counts, `price_from` and provider names. |
| `/catalog/<slug>` | One shelf: every capability, every endpoint, every price, as real text. |
| `/docs` | Server-rendered API reference built from `app.openapi()`. |
| `/docs/api` | FastAPI's Swagger UI, moved here and `Disallow`ed. ReDoc is off. |
| `/media/og.png` | The 1200×630 social card, served by the pre-existing `/media` mount. |
| `/catalog.css` | Shared skin for all three page types. |

`_page()` in `api.py` is the shell every server-rendered page goes through — it owns `<title>`, the
meta description, the canonical, the og/twitter card and the JSON-LD, so a new page cannot ship
missing them. That omission is exactly what left the landing bare.

## Things that will bite you

**`{BASE}`, never a hardcoded `treg.to`.** Every page is also served by self-hosted registries. A
hardcoded canonical tells their crawler the real page lives on someone else's domain. `landing()`,
`_legal_page()` and `tutorial_page()` all read-and-substitute for this reason — they were plain
`FileResponse`s before. `tests/test_seo.py` asserts no response body leaks a literal `{BASE}`, and
none leaks a hardcoded host when `public_url` is overridden.

**HEAD is widened after registration, and must not leak into the schema.** FastAPI's `APIRoute` pins
`methods` to `{"GET"}` and never adds HEAD (unlike Starlette's plain `Route`), so every page 405'd on
the probe crawlers send first. One loop at the bottom of `api.py` widens every GET-only route. But
FastAPI derives one operation per (path, method), so that widening put **58 duplicate HEAD entries
into `/openapi.json`**, each with a duplicate operation id. `_openapi_without_head()` narrows the
widened routes for the duration of schema generation and puts them back. Only `/call/{rest}`, which
declares HEAD itself, is documented with one.

**`/catalog/<slug>` sits in front of the JSON routes.** `/catalog/platforms`, `/catalog/search`,
`/catalog/endpoints/…` and `/catalog/examples/…` keep matching only because they are registered
first. `_CATALOG_RESERVED` refuses those names explicitly as a second guard, and the tests assert
the JSON routes still answer `application/json` — if the page route ever swallows one, the dashboard
and every installed CLI break at once.

**Structured data must match the visible page.** Google treats schema claiming something the page
does not say as a violation, not a shortcut. The landing's `Offer` figures ($1.00 free, 0% markup)
are asserted against the rendered HTML, and every FAQ question in `support.html`'s schema is
asserted to appear in its body. Edit one, edit the other, same commit.

**Prices need `_usd_short`, not `%g`.** `%g` flips to scientific notation below `1e-4`, and a shelf
advertising "from $1.2e-07 per call" reads as a bug. Anything under a hundredth of a cent renders as
`<$0.0001` — which then has to be HTML-escaped at every use site, because that `<` is real markup.

**The sitemap is walked, not spot-checked.** `test_every_sitemap_url_answers_200` fetches what it
publishes. Rename a route and the sitemap silently starts serving 404s to Google with nothing else
failing.

**`catalog.css` is stamped with its mtime.** It is served with a real `max-age`, so without
`?v=<mtime>` an edited skin keeps rendering from the browser's cache — the same trap `/tutorial.js`
already guards.

## The social card

`assets/brand/og-card.html` is the **source**; `src/treg/web/media/og.png` is the render. Open the
HTML at exactly 1200×630 in a headless browser and screenshot it. The provider favicons are fetched
at render time and baked into the PNG, so the shipped card has no runtime network dependency.

Every brand on the card is a real provider — checked against `catalog_store.load()`, after an early
draft showed Ahrefs, which treg does not carry. LinkedIn's mark is inlined because its Google s2
favicon only resolves at 16px and falls back to a generic globe at 64.

Per-platform cards (`/media/og/<slug>.png`) are a deliberate follow-up. Until then every catalog page
points at the shared one.

## Counts

`2,630 endpoints / 47 providers / 80 platforms`, from `catalog_store.load()`. The landing, `llms.txt`
and the schema all state them and had drifted apart (2,617/42 and ~2,600/~48). Note the catalog index
shows the **whole** catalog, not the sum of its tiles: a tile counts only its browse surface, so the
account/utility endpoints — real inventory, listed on each shelf page — are excluded from tile counts
by `catalog_store.HIDDEN_KINDS`.
