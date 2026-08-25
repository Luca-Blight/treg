"""Presentation-only web, SEO, tutorial, and public-document routes."""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import html as _html
import html as html_mod
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .. import adsconv, agent_pages, catalog_store, referrals
from .. import session as sess
from ..config import PUBLIC_HOST_ALIASES, get_settings
from ..db import get_session
from ..models import User
from .catalog import (_observed_or_empty, _platform_rows, _provider_display,
                      catalog_platform)
from .dependencies import (OAUTH_RETURN_COOKIE, _is_https, _remember_referral,
                           _take_oauth_return, _user_from_session)


LOCAL_USER_EMAIL = "you@local.treg"   # the single-user identity; a real address is never needed


async def _local_owner(db: AsyncSession) -> User | None:
    """The single-user identity, if this deployment is in that mode."""
    if not get_settings().single_user_ok:
        return None
    return (await db.execute(select(User).where(User.email == LOCAL_USER_EMAIL))).scalar_one_or_none()


# One level deeper than api.py, so anchor assets to the package root.
_WEB_DIR = Path(__file__).parent.parent / "web"


# The app alias preserves the moved handlers' original @app.get decorator text byte-for-byte.
catalog_pages_router = APIRouter()
app = catalog_pages_router


# ---- the crawlable catalog: /catalog and /catalog/<slug> -------------------------------------
#
# The JSON routes above are what agents and the dashboard read. These two render the SAME data as
# server-side HTML, because until now none of it had a URL: the dashboard browses platforms through
# hash routes (/app#platform/<slug>) behind a login, so ~2,600 endpoints across 80 shelves were
# invisible to every crawler and every AI answer engine. No JavaScript here on purpose — the text IS
# the product surface, and it has to be readable by something that will not run a script or click.
#
# `/catalog/<slug>` is registered after the JSON routes so /catalog/platforms, /catalog/search,
# /catalog/endpoints/… and /catalog/examples/… keep matching first. Registration order alone is a
# thin guarantee, so the reserved names are also refused explicitly below.
_CATALOG_RESERVED = {"platforms", "search", "endpoints", "examples"}

_GH = "https://github.com/superdesigndev/treg"


def _usd_short(usd: float) -> str:
    """A dollar figure a person can read. `%g` flips to scientific notation below 1e-4, and a shelf
    advertising "from $1.2e-07 per call" reads as a bug rather than as a price — so anything under
    a hundredth of a cent is labelled as such instead."""
    if not usd:
        return "free"
    return "<$0.0001" if usd < 0.0001 else f"${usd:.3g}"


def _price_label(cost: dict | None) -> str:
    """A price in ONE currency, so rows down a page stay comparable. Mirrors `_cost_usd` in cli.py
    rather than importing it: pulling treg.cli into the server process costs ~200ms and drags the
    whole CLI in for one string (see `_treg_version`)."""
    if not isinstance(cost, dict):
        return ""
    usd = cost.get("usd")
    if usd is None:
        return "own account"     # no rate published — never invent a dollar figure
    if not usd:
        return "free"
    unit = {"per_call": "call", "per_result": "result", "per_success": "success"}.get(
        cost.get("type"), "call")
    return f"{_usd_short(usd)}/{unit}"


def _css_stamp(name: str = "catalog.css") -> str:
    """The stylesheet's own mtime, stamped onto its URL. Skins are served with a real max-age
    (they are static and every page pulls them), so without a stamp an edited skin keeps rendering
    from the browser's copy until the cache expires — the trap `/tutorial.js` already guards."""
    f = _WEB_DIR / name
    try:
        return str(int(f.stat().st_mtime))
    except OSError:
        return "0"


def _serp_desc(text: str, limit: int = 155) -> str:
    """A meta description Google will print whole. Past ~155 characters it truncates mid-sentence,
    so cut at the last sentence that fits, then at the last word."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "? ", "! "):
        i = cut.rfind(sep)
        if i > limit * 0.5:
            return cut[:i + 1]
    return cut[:cut.rfind(" ")].rstrip(",;:") + "."


def _page(title: str, description: str, path: str, body: str, ld: list[dict],
          *, nav_current: str = "", head_extra: str = "", css: str = "catalog.css") -> HTMLResponse:
    """The shared shell for every server-rendered page. One place that owns <title>, the meta
    description, the canonical, the og/twitter card and the JSON-LD, so a new page cannot ship
    without them — that omission is exactly what left the landing page bare for a year.

    The "Start free" CTA carries `?ref=<page>`: a logged-out visit to bare `/app` is bounced to the
    marketing landing with nothing open, which loses the page the visitor was reading. With `ref`
    the app keeps them and opens sign-in in place (see the boot in index.html), and the page that
    produced the signup is recorded."""
    base = get_settings().public_url.rstrip("/")
    ref = quote(path.strip("/").replace("/", "-") or "home", safe="")
    t, d = _esc_html(title), _esc_html(description)
    url = _esc_html(base + path)  # `path` reaches attribute context — escape it like title/description
    # `<` escaped to its \u form inside the JSON: a catalog label containing "</script>" would
    # otherwise close the block early and put the rest of the payload into the document as markup.
    # Still valid JSON, so parsers and Google's validator read it unchanged.
    blocks = "\n".join(
        '<script type="application/ld+json">'
        + json.dumps(b, separators=(",", ":")).replace("<", "\\u003c")
        + "</script>"
        for b in ld)
    def navlink(href: str, label: str, extra: str = "") -> str:
        cur = ' aria-current="page"' if href == nav_current else ""
        return f'<a href="{href}"{cur}{extra}>{label}</a>'
    return HTMLResponse(f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{t}</title>
<meta name="description" content="{d}"/>
<link rel="canonical" href="{url}"/>
<link rel="icon" type="image/svg+xml" href="/favicon.svg"/>
<meta property="og:type" content="website"/>
<meta property="og:site_name" content="treg"/>
<meta property="og:url" content="{url}"/>
<meta property="og:title" content="{t}"/>
<meta property="og:description" content="{d}"/>
<meta property="og:image" content="{base}/media/og.png"/>
<meta property="og:image:width" content="1200"/>
<meta property="og:image:height" content="630"/>
<meta property="og:image:alt" content="treg.to: one key for the whole tool catalog, priced per call"/>
<meta name="twitter:card" content="summary_large_image"/>
<meta name="twitter:title" content="{t}"/>
<meta name="twitter:description" content="{d}"/>
<meta name="twitter:image" content="{base}/media/og.png"/>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist+Pixel&family=Inter:wght@400;450;500;600;650;700&family=DM+Mono:ital,wght@0,400;0,500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/{css}?v={_css_stamp(css)}"/>
{head_extra}
{blocks}
</head>
<body>
<div class="navwrap"><nav class="nav">
  <a class="brand" href="/"><span class="glyph">▚</span> treg</a>
  <div class="links">
    {navlink("/catalog", "Catalog")}
    {navlink("/tutorial", "Tutorial")}
    {navlink("/docs", "API")}
    <a class="hidem" href="{_GH}" target="_blank" rel="noopener">GitHub ↗</a>
    <a class="candy" href="/app?ref={ref}">Start free</a>
  </div>
</nav></div>
{body}
<footer>
  <div class="foot-in">
    <div class="brand"><span class="glyph">▚</span> treg</div>
    <span style="font-family:var(--mono);font-size:12px">· 100% open source</span>
    <span class="sp"></span>
    <a href="/catalog">catalog</a><a href="/tutorial">docs</a><a href="/llms.txt">llms.txt</a
    ><a href="{_GH}" target="_blank" rel="noopener">github ↗</a><a href="/docs">api</a
    ><a href="/terms">terms</a><a href="/privacy">privacy</a>
  </div>
</footer>
</body>
</html>""", headers={"Cache-Control": "public, max-age=600"})


def _spa_catalog_page(title: str, description: str, path: str, ld: list[dict],
                      prerender: str) -> HTMLResponse:
    """Serve the dashboard SPA at a PUBLIC catalog URL, with the head a crawler needs.

    The public catalog is not a second implementation of the marketplace — it IS the marketplace.
    `/catalog` and `/catalog/<slug>` hand back `index.html`, and the Vue app renders the same
    platform views a member sees (its catalog API is unauthenticated, so it works signed out; see
    `publicCatalog` in index.html). That is the whole point: one UI, so the two can never drift
    apart visually the way a hand-built copy would.

    Two things have to be added on the way out:

    1. **The head.** The SPA ships one bare `<title>treg</title>`. Every catalog URL needs its own
       title, description, canonical, og/twitter card and JSON-LD, so they are substituted in here —
       the same trick `_spa_with_og` uses for shared skill/tool links.
    2. **A no-JS fallback.** Vue compiles `#app`'s own innerHTML as its template, so prerendered
       markup cannot go inside it. `#prerender` is therefore a SIBLING, removed by the app on boot.
       It is deliberately plainer than the Vue view — the ledger's row-merging is a chain of
       client-side computeds, and reproducing it server-side would recreate exactly the duplicate
       implementation this design avoids. It carries the TEXT (names, summaries, providers, prices),
       which is what a crawler that does not run scripts is here for.
    """
    index = _WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h3>tools-registry API. Dashboard not bundled.</h3>")
    base = get_settings().public_url.rstrip("/")
    t, d = _esc_html(title), _esc_html(description)
    # `path` carries the {slug} from the URL. Today an unknown slug 404s in catalog_platform before
    # it reaches here, so a quote can't get this far — but that is an upstream lookup's side effect,
    # not a guarantee this function makes. Escape it where it is used, so a future "slug not found →
    # suggestions" page cannot turn a canonical tag into a reflected XSS.
    url = _esc_html(base + path)
    blocks = "\n".join(
        '<script type="application/ld+json">'
        + json.dumps(b, separators=(",", ":")).replace("<", "\\u003c") + "</script>"
        for b in ld)
    meta = (
        f"<title>{t}</title>\n"
        f'<meta name="description" content="{d}"/>\n'
        f'<link rel="canonical" href="{url}"/>\n'
        f'<meta name="robots" content="index, follow"/>\n'   # index.html defaults to noindex
        f'<meta property="og:type" content="website"/>\n'
        f'<meta property="og:site_name" content="treg"/>\n'
        f'<meta property="og:url" content="{url}"/>\n'
        f'<meta property="og:title" content="{t}"/>\n'
        f'<meta property="og:description" content="{d}"/>\n'
        f'<meta property="og:image" content="{base}/media/og.png"/>\n'
        f'<meta property="og:image:width" content="1200"/>\n'
        f'<meta property="og:image:height" content="630"/>\n'
        f'<meta name="twitter:card" content="summary_large_image"/>\n'
        f'<meta name="twitter:title" content="{t}"/>\n'
        f'<meta name="twitter:description" content="{d}"/>\n'
        f'<meta name="twitter:image" content="{base}/media/og.png"/>\n'
        + blocks
    )
    html = index.read_text(encoding="utf-8")
    # index.html carries `robots: noindex` for the authenticated app; these URLs are public, and the
    # `index, follow` in `meta` only wins if the noindex is gone. Stripped BEFORE `meta` is spliced
    # in, so this scan only ever runs over the static bundle — never over a string carrying a
    # caller-supplied title, which is what made it a ReDoS candidate rather than a fixed-cost pass.
    html = re.sub(r'<meta name="robots" content="noindex[^>]*>\s*', "", html, count=1)
    # Match whatever title the page carries, not one exact string — a rename in the dashboard must
    # not be able to switch every catalog page's head off without a word (the same failure
    # `_spa_with_og` was written to survive).
    html, hits = re.subn(r"<title>.*?</title>", lambda _m: meta, html, count=1,
                         flags=re.IGNORECASE | re.DOTALL)
    if not hits:
        html = html.replace("<head>", "<head>\n" + meta, 1)
    marker = '<div id="app"'
    if marker in html:
        html = html.replace(marker, f'<div id="prerender">{prerender}</div>\n{marker}', 1)
    return HTMLResponse(html, headers={"Cache-Control": "public, max-age=600"})


# The fallback's own skin. Scoped to #prerender and written against the dashboard's OWN tokens
# (already defined in index.html), so it reads as the same product for the moment it is on screen.
_PRERENDER_CSS = """<style>
#prerender{max-width:1100px;margin:0 auto;padding:38px 26px 60px;font-family:var(--sans,system-ui);
  color:var(--ink,#1a1a1a)}
#prerender h1{font-size:30px;letter-spacing:-.01em;margin:0 0 8px}
#prerender .lede{color:var(--muted,#7c7c7c);margin:0 0 20px;max-width:64ch}
#prerender h2{font-size:13px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--muted2,#989898);margin:26px 0 10px;padding-bottom:8px;
  border-bottom:1px solid var(--line,#26262322)}
#prerender ul{list-style:none;margin:0;padding:0}
#prerender li{padding:9px 0;border-bottom:1px solid var(--line,#26262322)}
#prerender li b{font-weight:600}
#prerender li i{font-style:normal;color:var(--muted,#7c7c7c);display:block;font-size:13.5px}
#prerender .m{font-family:var(--mono,ui-monospace);font-size:11.5px;
  color:var(--muted2,#989898);margin-top:3px;display:block}
#prerender a{color:var(--teal,#1a7da6);text-decoration:none}
</style>"""


@app.get("/catalog", include_in_schema=False)
async def catalog_index():
    """The catalog index — the marketplace's Catalog view, on a public, indexable URL."""
    base = get_settings().public_url.rstrip("/")
    rows = _platform_rows()
    # The WHOLE catalog, not the sum of the tiles: a tile counts only its browse surface, so the
    # account/utility endpoints (real inventory, listed on each shelf page) would go uncounted and
    # this page would quietly contradict the number on the landing.
    cat = catalog_store.load()
    total_eps = len(cat.endpoints)
    providers = sorted({e["provider"] for e in cat.endpoints})

    cats: dict[str, list[dict]] = {}
    for row in rows:
        cats.setdefault(row["category"], []).append(row)
    sections = []
    for name, items in cats.items():
        lis = []
        for r in items:
            price = _price_label(r["price_from"])
            vendors = ", ".join(_provider_display(p) for p in r["providers"])
            lis.append(
                f'<li><b><a href="/catalog/{_esc_html(r["slug"])}">{_esc_html(r["label"])}</a></b>'
                f'<i>{_esc_html(r["summary"])}</i>'
                f'<span class="m">{r["endpoints"]} endpoints · {r["capabilities"]} capabilities'
                + (f" · from {_esc_html(price)}" if price else "")
                + f" · {_esc_html(vendors)}</span></li>")
        sections.append(f"<h2>{_esc_html(name)}</h2><ul>{''.join(lis)}</ul>")

    prerender = (_PRERENDER_CSS
                 + "<h1>The tool catalog</h1>"
                 + f'<p class="lede">{total_eps:,} endpoints across {len(rows)} platforms and '
                   f"{len(providers)} providers — every tool your agent can call through one key, "
                   "priced up front and billed per call, with no provider signup.</p>"
                 + "".join(sections))

    ld = [
        {"@context": "https://schema.org", "@type": "ItemList",
         "name": "treg tool catalog",
         "description": f"{total_eps} API endpoints across {len(rows)} platforms, callable through one key.",
         "numberOfItems": len(rows),
         "itemListElement": [
             {"@type": "ListItem", "position": i, "name": r["label"],
              "url": f"{base}/catalog/{r['slug']}"}
             for i, r in enumerate(rows, 1)]},
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Catalog", "item": base + "/catalog"}]},
    ]
    return _spa_catalog_page(
        f"Tool catalog — {total_eps:,} API endpoints your agent can call | treg",
        f"Browse {total_eps:,} endpoints across {len(rows)} platforms and {len(providers)} providers "
        "— SEO, social, enrichment, ads and scraping data. One key, priced per call, no provider signup.",
        "/catalog", ld, prerender)


@app.get("/catalog/{slug}", include_in_schema=False)
async def catalog_page(slug: str):
    """One platform shelf — the marketplace's platform view, on a public, indexable URL."""
    if slug in _CATALOG_RESERVED:
        raise HTTPException(status_code=404, detail=f"unknown platform {slug!r}")
    # include_hidden=1, exactly as the SPA asks for it (see `loadPlatform`): the account/utility
    # endpoints are real inventory and the page files them in their own section rather than hiding
    # them. Asking for a different population than the view that is about to replace this would put
    # two different endpoint counts on one URL.
    detail = await catalog_platform(slug, include_hidden=1)
    base = get_settings().public_url.rstrip("/")
    plat = detail["platform"]
    label, category = plat["label"], plat["category"]
    row = next((r for r in _platform_rows() if r["slug"] == slug), None)
    summary = (row or {}).get("summary", "")
    caps = detail["capabilities"]
    eps = [e for cap in caps for e in cap["endpoints"]] + detail["extended"]
    prices = [c["usd"] for e in eps if isinstance(c := e.get("cost"), dict) and c.get("usd")]
    cheapest = _usd_short(min(prices)) if prices else ""

    blocks = []
    for cap in caps:
        lis = []
        for e in cap["endpoints"]:
            price = _price_label(e.get("cost"))
            bits = [_esc_html(e["provider_display"])]
            if e.get("verified"):
                bits.append("live-verified")
            if price:
                bits.append(_esc_html(price))
            bits.append(_esc_html(e["id"]))
            lis.append(f'<li><b>{_esc_html(e["name"])}</b>'
                       f'<i>{_esc_html(e.get("summary") or "")}</i>'
                       f'<span class="m">{" · ".join(bits)}</span></li>')
        blocks.append(f'<h2>{_esc_html(cap["description"] or cap["id"])}</h2><ul>{"".join(lis)}</ul>')

    provs = ", ".join(p["display_name"] for p in detail["providers"].values())
    prerender = (_PRERENDER_CSS
                 + f'<p class="m"><a href="/catalog">← Catalog</a> · {_esc_html(category)}</p>'
                 + f"<h1>{_esc_html(label)}</h1>"
                 + f'<p class="lede">{_esc_html(summary)} {len(eps)} endpoints from '
                   f"{_esc_html(provs)}"
                 + (f", from {_esc_html(cheapest)} per call" if cheapest else "")
                 + ". Jobs that several providers do sit on one row, so you can compare price and "
                   "coverage before you spend a call — <b>choosing is yours</b>; treg does not route "
                   "between providers automatically.</p>"
                 + "".join(blocks))

    desc = (f"{len(eps)} {label.lower()} API endpoints from "
            f"{', '.join(p['display_name'] for p in list(detail['providers'].values())[:3])}"
            + (f", from {cheapest} per call" if cheapest else "")
            + ". Call them through one treg key — no provider signup.")
    ld = [
        {"@context": "https://schema.org", "@type": "ItemList",
         "name": f"{label} — API endpoints on treg",
         "numberOfItems": len(caps),
         "itemListElement": [
             {"@type": "ListItem", "position": i, "name": cap["description"] or cap["id"],
              "url": f"{base}/catalog/{slug}#{cap['id']}"}
             for i, cap in enumerate(caps, 1)]},
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Catalog", "item": base + "/catalog"},
            {"@type": "ListItem", "position": 3, "name": label, "item": f"{base}/catalog/{slug}"}]},
    ]
    return _spa_catalog_page(f"{label} API — {len(eps)} endpoints, priced per call | treg",
                             desc[:300], f"/catalog/{slug}", ld, prerender)


# --------------------------------------------------------------------------- /agents/<agent>

def _hosted() -> bool:
    """True on the reference deployment only. The agent pages describe treg.to's own listings (the
    ChatGPT plugin, the OAuth connector, the free grant), none of which is true of a self-hosted
    registry, so off these hosts the pages do not exist rather than lie."""
    host = (urlsplit(get_settings().public_url).hostname or "").lower()
    return host in PUBLIC_HOST_ALIASES


def _catalog_census() -> tuple[int, int]:
    """(browse-surface endpoint count, platform count): the two numbers the agent pages state."""
    cat = catalog_store.load()
    browse = [e for e in cat.endpoints if e["kind"] not in catalog_store.HIDDEN_KINDS]
    return len(browse), len({e["platform"] for e in browse})


def _anchor(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _logo(domain: str | None, alt: str) -> str:
    """A 20px brand mark from the favicon service the landing uses, or the treg glyph when the
    brand is unknown (never a wrong logo)."""
    if not domain:
        return '<span class="lg lg-none" aria-hidden="true">▚</span>'
    return (f'<img class="lg" src="https://www.google.com/s2/favicons?domain={_esc_html(domain)}&amp;sz=64" '
            f'alt="{_esc_html(alt)}" width="20" height="20" loading="lazy"/>')


def _use_case_page_for(category: str, label: str) -> str | None:
    """The spoke URL for a job on the agent page, or None when no page has been written for it."""
    cslug = agent_pages.category_slug(category)
    for (c, j), spec in agent_pages.USE_CASE_PAGES.items():
        if c == cslug and spec["label"] == label:
            return f"/use-cases/{c}/{j}"
    return None


def _use_case_caps(category_slug: str, label: str) -> tuple[str, ...]:
    for category, jobs in agent_pages.USE_CASES:
        if agent_pages.category_slug(category) == category_slug:
            for lbl, caps in jobs:
                if lbl == label:
                    return caps
    return ()


def _menu_rows(cat, category: str, jobs) -> list[dict]:
    """The use-case menu for one category, priced from the catalog. Shared by the HTML and the
    Markdown renderings of the agent page so the two can never list different jobs."""
    rows = []
    for label, caps in jobs:
        eps = [e for cid in caps for e in cat.for_capability(cid) if e["kind"] not in catalog_store.HIDDEN_KINDS]
        if not eps:  # the test forbids this, but a page must never render an empty promise
            continue
        prices = [c["usd"] for e in eps if (c := cat.cost_view(e.get("cost"), e.get("provider"))) and c["usd"]]
        plats, seen = [], set()
        for cid in caps:
            ceps = [e for e in cat.for_capability(cid) if e["kind"] not in catalog_store.HIDDEN_KINDS]
            if not ceps:
                continue
            slug = ceps[0]["platform"]
            plats.append({"cap": cid, "slug": slug, "dup": slug in seen,
                          "label": (cat.platforms.get(slug) or {}).get("label") or slug,
                          "domain": agent_pages.PLATFORM_DOMAINS.get(slug)})
            seen.add(slug)
        rows.append({"label": label, "caps": caps, "platforms": plats,
                     "providers": len({e["provider"] for e in eps}),
                     "verified": sum(1 for e in eps if e["verified"]),
                     "from_usd": min(prices) if prices else None,
                     # no priced endpoint at all = the team's own account does the job, unmetered
                     "own_account": not prices,
                     "page": _use_case_page_for(category, label)})
    return rows


_COPY_JS = """
<script>
document.querySelectorAll('button[data-copy]').forEach(function(b){
  b.addEventListener('click', async function(){
    try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent='copied'; b.classList.add('done');
          setTimeout(function(){ b.textContent='copy'; b.classList.remove('done'); }, 1400); } catch(e) {}
  });
});
</script>"""

_MD_ALT = '<link rel="alternate" type="text/markdown" href="{href}"/>'


@app.get("/agents/{agent}.md", include_in_schema=False)
@app.get("/agents/{agent}", include_in_schema=False)
async def agent_page(request: Request, agent: str):
    """One client: "I use ChatGPT, what can it do now?" A rotating "The ChatGPT plugin for <role>"
    hero, the install steps for that client, then the use-case menu: plain-words jobs under buyer
    categories, each priced from the catalog. The menu is `agent_pages.USE_CASES`, the same
    taxonomy the use-case pages hang from, so the agent page is the map of the whole site.
    `/agents/<agent>.md` is the same page as Markdown, for agents and answer engines."""
    as_md = request.url.path.endswith(".md")
    raw = agent[:-3] if agent.endswith(".md") else agent
    # Resolve to the dict's OWN key, never the request's bytes: `agent` is interpolated into the
    # canonical, the rel=alternate href and the JSON-LD breadcrumb below, and a path parameter
    # must not reach those unescaped (CodeQL py/reflective-xss). The lookup is case-insensitive,
    # so a differently-cased URL would otherwise serve a 200 whose canonical points at itself: a
    # duplicate page. Send it to the one spelling instead.
    agent = next((k for k in agent_pages.AGENTS if k == raw.lower()), None)
    if agent is None or not _hosted():
        raise HTTPException(status_code=404, detail="unknown agent")
    if raw != agent:
        return RedirectResponse(f"/agents/{agent}" + (".md" if as_md else ""), status_code=301)
    spec = agent_pages.AGENTS[agent]
    cat = catalog_store.load()
    base = get_settings().public_url.rstrip("/")
    n_eps, n_plats = _catalog_census()
    n, p = f"{n_eps:,}", str(n_plats)
    name = spec["name"]
    title = spec["title"].format(n=n, p=p)
    desc = _serp_desc(spec["description"].format(n=n, p=p))
    definition = spec["definition"].format(n=n, p=p)
    menu = [(category, agent_pages.CATEGORY_PROMPTS.get(category, ""), _menu_rows(cat, category, jobs))
            for category, jobs in agent_pages.USE_CASES]
    steps_text = [re.sub(r"<[^>]+>", "", st) for st in spec["install_steps"]]

    if as_md:
        md = [f"# {title}", "", definition, "", f"## Install in {name}", ""]
        md += [f"{i}. {html_mod.unescape(st)}" for i, st in enumerate(steps_text, 1)]
        md += ["", f"## What {name} can do now", "",
               "One row per job. Prices are the provider's own rate with $0.000 markup; rows marked FREE run on your own account and are never metered.", ""]
        for category, prompt, rows in menu:
            md += [f"### {category}", ""]
            if prompt:
                md += [f"Try: \"{prompt}\"", ""]
            for r in rows:
                plats = ", ".join(pl["label"] for pl in r["platforms"] if not pl["dup"])
                price = "FREE with your own account" if r["own_account"] else f"from {_usd_short(r['from_usd'])}"
                link = f"{base}{r['page']}" if r["page"] else f"{base}/catalog/{r['platforms'][0]['slug']}"
                md.append(f"- [{r['label']}]({link}): {plats}. {r['providers']} provider{'s' if r['providers'] != 1 else ''}, {price}.")
            md.append("")
        md += ["## Questions", ""]
        for q, a in spec["faq"]:
            md += [f"**{q}** {a}", ""]
        md += [f"HTML version: {base}/agents/{agent}", f"Setup line for any agent: {agent_pages.SETUP_LINE.format(base=base)}"]
        return PlainTextResponse("\n".join(md), media_type="text/markdown; charset=utf-8",
                                 headers={"Cache-Control": "public, max-age=600"})

    # Only the FIRST role is in the H1 markup: a crawler reads "…plugin for SEO experts", not nine
    # roles run together. The rest ride in a JSON block and the script appends them.
    roles = f'<span class="ri on">{_esc_html(agent_pages.ROLES[0])}</span>'
    more_roles = json.dumps(list(agent_pages.ROLES[1:])).replace("<", "\\u003c")
    steps = "".join(
        f'<div class="steplabel"><span class="n">{i}</span><b>{st}</b></div>'
        for i, st in enumerate(spec["install_steps"], 1))
    shot = (f'<div class="sample"><div class="sbar">{_esc_html(spec.get("install_image_bar") or name)}</div>'
            f'<img src="{_esc_html(spec["install_image"])}" alt="{_esc_html(spec["install_image_alt"])}" '
            f'loading="lazy" style="display:block;width:100%"/>'
            + (f'<div class="sbar" style="border-top:1px solid var(--line);border-bottom:0">'
               f'{_esc_html(spec["install_image_caption"])}</div>' if spec.get("install_image_caption") else "")
            + '</div>' if spec.get("install_image") else "")

    # the platform marks in the hero: the busiest shelves, deduped by brand
    hero_tiles, seen_brand = [], set()
    for _cat_name, _prompt, rows in menu:
        for r in rows:
            for pl in r["platforms"]:
                root = ".".join((pl["domain"] or "").split(".")[-2:])
                if pl["domain"] and root not in seen_brand and len(hero_tiles) < 14:
                    seen_brand.add(root)
                    hero_tiles.append(f'<span class="ptile" title="{_esc_html(pl["label"])}">'
                                      f'{_logo(pl["domain"], pl["label"])}</span>')

    cards, sections = [], []
    for category, prompt, rows in menu:
        anchor = _anchor(category)
        priced = [r["from_usd"] for r in rows if r["from_usd"]]
        free_all = all(r["own_account"] for r in rows)
        meta = (f'{len(rows)} jobs &middot; <b style="color:var(--green)">free</b> on your account' if free_all
                else f'{len(rows)} jobs &middot; from {_esc_html(_usd_short(min(priced)))}' if priced
                else f"{len(rows)} jobs")
        blurb = agent_pages.CATEGORY_BLURBS.get(category, "").format(agent=name)
        cards.append(f'<a class="card" href="#{anchor}"><h4>{_esc_html(category)}</h4>'
                     f'<p>{_esc_html(blurb)}</p>'
                     f'<p style="font-family:var(--mono);font-size:11.5px;color:var(--muted2)">{meta}</p></a>')
        body_rows = []
        for r in rows:
            chips, seen_p = [], set()
            for pl in r["platforms"]:
                if pl["slug"] in seen_p:
                    body_rows.append("")  # keep data-cap discoverable below
                    continue
                seen_p.add(pl["slug"])
                chips.append(f'<a href="/catalog/{_esc_html(pl["slug"])}#{_esc_html(pl["cap"])}" '
                             f'data-cap="{_esc_html(pl["cap"])}">{_logo(pl["domain"], pl["label"])}{_esc_html(pl["label"])}</a>')
            hidden = "".join(f'<span data-cap="{_esc_html(pl["cap"])}" hidden></span>'
                             for pl in r["platforms"] if pl["dup"])
            price = ('<span style="color:var(--green)">free, your account</span>' if r["own_account"]
                     else f'{_esc_html(_usd_short(r["from_usd"]))}')
            name_cell = (f'<a href="{r["page"]}"><b>{_esc_html(r["label"])}</b></a>' if r["page"]
                         else f'<b>{_esc_html(r["label"])}</b>')
            body_rows.append(
                f'<tr><td>{name_cell}{hidden}</td>'
                f'<td style="color:var(--muted)">{" &middot; ".join(chips)}</td>'
                f'<td>{r["providers"]}</td><td>{price}</td></tr>')
        sections.append(
            f'<section id="{anchor}"><div class="wrap"><div class="seclab">{_esc_html(category)}</div>'
            f'<h2>{_esc_html(blurb)}</h2>'
            + (f'<p>Try: <i>&ldquo;{_esc_html(prompt)}&rdquo;</i></p>' if prompt else "")
            + '<div class="tablewrap"><table><thead><tr><th>Job</th><th>Where</th><th>Providers</th>'
              '<th>From</th></tr></thead><tbody>'
            + "".join(body_rows) + '</tbody></table></div></div></section>')

    faq_html = "".join(f'<h3>{_esc_html(q)}</h3><p>{_esc_html(a)}</p>' for q, a in spec["faq"])

    body = (
        '<div class="hero"><div class="wrap">'
        f'<div class="trust" style="margin:0 0 18px"><a href="/">treg.to</a> / '
        f'<a href="/agents/{_esc_html(agent)}">{_esc_html(name)}</a></div>'
        f'<div class="kicker">{n} endpoints &middot; {p} platforms &middot; $0.000 markup</div>'
        f'<h1>The {_esc_html(name)} plugin for <span class="roleslot" id="roleslot">'
        f'<span class="rw" id="rolewheel">{roles}</span></span></h1>'
        f'<script type="application/json" id="roles-more">{more_roles}</script>'
        f'<div class="lede">{_esc_html(definition)}</div>'
        '<div class="ctas">'
        f'<a class="candy" href="/app?ref=agents-{_esc_html(agent)}">Start free</a>'
        '<a class="ghostbtn" href="#use-cases">See what it can do</a></div>'
        '<div class="trust">$1.00 of free credit on every new team &middot; no provider signup &middot; no card</div>'
        f'<div class="subline">Your own keys always win and are never metered. '
        f'{_esc_html(name)} sees the price before it spends.</div>'
        + (f'<div class="provstrip"><div class="pl">a few of the {p} platforms</div>'
           f'<div class="ptiles">{"".join(hero_tiles)}</div></div>' if hero_tiles else "")
        + '</div></div>'

        f'<section id="install"><div class="wrap"><div class="seclab">Get started</div>'
        f'<h2>Install in {_esc_html(name)}</h2>{steps}{shot}</div></section>'

        '<section id="use-cases"><div class="wrap"><div class="seclab">The menu</div>'
        f'<h2>What {_esc_html(name)} can do now</h2>'
        '<p>By job, not by endpoint. The price is the lowest provider&rsquo;s own rate with $0.000 added by '
        'treg.to; <b>free</b> means the job runs on an account you already own and is never metered. Where '
        f'several providers do one job, {_esc_html(name)} sees them side by side and choosing is yours.</p>'
        f'<div class="cards">{"".join(cards)}</div>'
        f'<p style="margin-top:20px"><a href="/catalog">Browse all {n} endpoints &rarr;</a> &middot; '
        f'<a href="/use-cases">read the job guides &rarr;</a></p></div></section>'

        + "".join(sections)

        + f'<section id="faq"><div class="wrap"><div class="seclab">Questions</div>'
          f'<h2>Before you install</h2>{faq_html}</div></section>'

        + '<div class="final"><div class="wrap">'
          f'<h2>Give {_esc_html(name)} the tools</h2>'
          f'<a class="candy" href="/app?ref=agents-{_esc_html(agent)}-final">Start free</a>'
          '<div class="trust">$1.00 of calls free per new team &middot; '
          '<a href="/catalog">browse the catalog</a></div></div></div>'

        + """
<style>
.hero h1{line-height:1.16}
.roleslot{display:inline-block;height:1.16em;overflow:hidden;vertical-align:bottom;position:relative}
.roleslot .rw{display:flex;flex-direction:column;align-items:flex-start;transition:transform .62s cubic-bezier(.2,.7,.2,1)}
.roleslot .ri{height:1.16em;line-height:1.16;flex:none;white-space:nowrap;transition:opacity .4s}
.roleslot .ri:not(.on){opacity:.25}
@media (prefers-reduced-motion:reduce){.roleslot .rw{transition:none}}
</style>
<script>
(function(){
  var w=document.getElementById('rolewheel'); if(!w) return;
  try { JSON.parse((document.getElementById('roles-more')||{}).textContent||'[]').forEach(function(r){
    var s=document.createElement('span'); s.className='ri'; s.textContent=r; w.appendChild(s); }); } catch(e) {}
  var items=w.children, i=0, slot=document.getElementById('roleslot');
  if(matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  function fit(){ slot.style.width=items[i].getBoundingClientRect().width+'px'; }
  fit(); addEventListener('resize', fit);
  setInterval(function(){
    if(scrollY>innerHeight*.8) return;
    i=(i+1)%items.length; w.style.transform='translateY(-'+(i*1.16)+'em)';
    for(var k=0;k<items.length;k++) items[k].classList.toggle('on',k===i);
    fit();
  },3000);
})();
</script>""")

    ld = [
        {"@context": "https://schema.org", "@type": "SoftwareApplication", "name": "treg.to",
         "applicationCategory": "DeveloperApplication", "operatingSystem": "Web",
         "url": base + "/", "description": desc,
         "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD",
                    "description": "Free to install. Calls are metered per call from a prepaid balance at the "
                                   "provider's own rate with no markup; every new team starts with $1.00 free."}},
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg.to", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": name, "item": f"{base}/agents/{agent}"}]},
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q,
             "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in spec["faq"]]},
    ]
    return _page(title, desc[:300], f"/agents/{agent}", body, ld,
                 head_extra=_MD_ALT.format(href=f"{base}/agents/{agent}.md"), css="usecase.css")


def _uc_agent() -> tuple[str, str]:
    """(slug, display name) of the client the use-case pages use as the example."""
    slug = agent_pages.DEFAULT_AGENT
    return slug, agent_pages.AGENTS[slug]["name"]


_UNIT_WORDS = {"per_success": "found", "per_call": "call", "per_result": "result"}


def _uc_providers(cat, eps: list[dict], obs: dict) -> list[dict]:
    """One row per provider for this job: cheapest priced endpoint, best-sampled observed stats,
    the union of its accepted inputs, and the platform it serves."""
    def usd(e):
        cv = cat.cost_view(e.get("cost"), e.get("provider"))
        return cv["usd"] if cv and cv["usd"] else None

    # Keyed by (provider, platform), not provider alone: one provider often serves several
    # platforms for the same job (ScrapeCreators does Instagram AND YouTube), and collapsing those
    # into one row silently drops a whole platform from a multi-platform page.
    out = []
    for prov, plat in sorted({(e["provider"], e["platform"]) for e in eps}):
        peps = [e for e in eps if e["provider"] == prov and e["platform"] == plat]
        priced = sorted([(usd(e), e) for e in peps if usd(e)], key=lambda t: t[0])
        cheapest_e = priced[0][1] if priced else None
        stats = [(obs.get(e["id"]) or {}) for e in peps]
        best = max((st for st in stats if st.get("samples")), key=lambda st: st["samples"], default=None)
        ins = []
        for e in peps:
            inp = e.get("input") or {}
            for section in ("queryParams", "pathParams", "body", "headers"):
                for k, v in (inp.get(section) or {}).items():
                    if isinstance(v, dict) and k not in ins:
                        ins.append(k)
        slug = plat
        out.append({
            "id": prov, "name": _provider_display(prov), "eps": peps,
            "domain": agent_pages.PROVIDER_DOMAINS.get(prov),
            "platform": slug, "platform_label": (cat.platforms.get(slug) or {}).get("label") or slug,
            "usd": priced[0][0] if priced else None,
            "unit": _UNIT_WORDS.get((cheapest_e.get("cost") or {}).get("type"), "call") if cheapest_e else "",
            "cheapest_ep": cheapest_e, "inputs": ins[:6],
            "verified": max((e.get("verified") or "" for e in peps), default=""),
            "ok_rate": best.get("ok_rate") if best else None,
            "p50": best.get("p50_ms") if best else None,
            "samples": best.get("samples") if best else 0,
        })
    return out


def _uc_call(e: dict) -> str:
    tr = e.get("test_request") or {}
    q = " ".join(f"--query {k}={v}" for k, v in (tr.get("queryParams") or {}).items())
    parts = [f"treg call {e['id']}"]
    if q:
        parts.append(q)
    if tr.get("body"):
        parts.append("--data '" + json.dumps(tr["body"], separators=(",", ":")) + "'")
    return " ".join(parts)


@app.get("/use-cases/{category}/{job}.md", include_in_schema=False)
@app.get("/use-cases/{category}/{job}", include_in_schema=False)
async def use_case_job_page(request: Request, category: str, job: str,
                            db: AsyncSession = Depends(get_session)):
    """One job. The reader does one thing, the prompt; everything else is what the agent sees
    before it calls. The page takes one of three FORMS, chosen from the data rather than by hand:

      short      one provider, so there is nothing to compare (all of "connect your own accounts")
      platforms  the job spans several platforms, which are not alternatives to one another
      compare    several providers doing one job on one platform: the full comparison

    Everything job-specific comes from `agent_pages.USE_CASE_PAGES`; the example client comes from
    `DEFAULT_AGENT`, so writing page two is data entry. `.md` serves the same page as Markdown.
    """
    as_md = request.url.path.endswith(".md")
    raw = (category, job[:-3] if job.endswith(".md") else job)
    # Same rule as `agent_page`: the slugs reach the canonical and the JSON-LD, so they come from
    # the table's own key, and a differently-cased URL is redirected rather than duplicated.
    key = next((k for k in agent_pages.USE_CASE_PAGES
                if k == (raw[0].lower(), raw[1].lower())), None)
    if key is None or not _hosted():
        raise HTTPException(status_code=404, detail="unknown use case")
    if raw != key:
        return RedirectResponse(f"/use-cases/{key[0]}/{key[1]}" + (".md" if as_md else ""),
                                status_code=301)
    # Fresh names on purpose: rebinding the parameters themselves does not read as a taint kill to
    # CodeQL, and the request's spelling must not be what the page prints.
    cat_slug, job_slug = key
    spec = agent_pages.USE_CASE_PAGES[key]
    cat = catalog_store.load()
    base = get_settings().public_url.rstrip("/")
    agent_slug, agent_name = _uc_agent()
    cat_label = next((c for c, _ in agent_pages.USE_CASES if agent_pages.category_slug(c) == cat_slug), cat_slug)
    caps = _use_case_caps(cat_slug, spec["label"])
    eps = [e for cid in caps for e in cat.for_capability(cid) if e["kind"] not in catalog_store.HIDDEN_KINDS]
    if not eps:
        raise HTTPException(status_code=404, detail="no endpoints for this job")
    obs = await _observed_or_empty(db, [e["id"] for e in eps])
    provs = _uc_providers(cat, eps, obs)

    def usd_of(e):
        cv = cat.cost_view(e.get("cost"), e.get("provider"))
        return cv["usd"] if cv and cv["usd"] else None

    platforms = sorted({p["platform_label"] for p in provs})
    form = "short" if len(provs) == 1 else ("platforms" if len(platforms) > 1 else "compare")
    # data-provider must stay unique in the DOM when one provider appears under two platforms
    for pr in provs:
        pr["row_id"] = pr["id"] if form != "platforms" else f'{pr["id"]}-{pr["platform"]}'
    noun = spec.get("result_noun", "result")

    # Cheapest is claimed PER BILLING UNIT. A per-call endpoint that returns a thousand rows is not
    # dearer than a per-result one, and ranking them together names the wrong winner: 38 of the 66
    # jobs on the menu mix units.
    cheapest_by_unit: dict[str, dict] = {}
    for pr in provs:
        u = pr["unit"]
        if pr["usd"] and (u not in cheapest_by_unit or pr["usd"] < cheapest_by_unit[u]["usd"]):
            cheapest_by_unit[u] = pr
    units = list(cheapest_by_unit)
    headline = cheapest_by_unit[units[0]] if units else None
    reliable = sorted([p for p in provs if p["samples"] and p["ok_rate"] is not None],
                      key=lambda p: (-p["ok_rate"], p["p50"] or 9e9, -p["samples"]))
    n = str(len({p["id"] for p in provs}))
    n_ver = sum(1 for e in eps if e.get("verified"))
    latest_verified = max((e.get("verified") or "" for e in eps), default="")
    setup = agent_pages.SETUP_LINE.format(base=base)

    def money(x):
        return _usd_short(x)

    def pct(x):
        return f"{round(x * 100)}%" if x is not None else ""

    def ms(x):
        return (f"{x/1000:.1f}s" if x >= 1000 else f"{int(x)}ms") if x else ""

    def unit_plural(u: str) -> str:
        return {"found": f"{noun}s found", "result": "results"}.get(u, "calls")

    title = spec.get("title", "{sentence}: {n} providers | treg.to").format(
        sentence=spec["sentence"], n=n, agent=agent_name,
        cheapest=money(headline["usd"]) if headline else "free on your own account")
    lede = spec["lede"].format(n=n, agent=agent_name,
                               cheapest=money(headline["usd"]) if headline else "free on your own account")
    bits_desc = [spec["sentence"] + "."]
    if form == "short":
        bits_desc.append("Runs on the account you already own, so treg.to never meters it.")
    elif headline:
        bits_desc.append(f"{n} providers compared, cheapest {money(headline['usd'])} per {headline['unit']}.")
    bits_desc.append(f"The prompt that works in {agent_name}, with the price shown before the call.")
    desc = _serp_desc(" ".join(bits_desc))

    if as_md:
        md = [f"# {spec['sentence']}", "", lede, "",
              f"## What's the best way to ask {agent_name}?", "",
              f"Setup line (paste into any agent): `{setup}`", "",
              f'Then ask: "{spec["prompt"]}"', ""]
        md += [f"- **{t}** {d}" for t, d in spec["prompt_why"]]
        md += ["", "## Why go through treg.to", ""] + [f"- **{t}** {d}" for t, d in agent_pages.WHY_TREG]
        if form == "short":
            e0 = provs[0]["eps"][0]
            md += ["", "## How it works", "",
                   f"One provider does this job: {provs[0]['name']} (`{e0['id']}`), on the account you already own. "
                   "You connect it once, treg.to keeps the token server side, and the call is never metered.",
                   "", f"    {_uc_call(e0)}", ""]
        else:
            md += ["", f"## Behind the scenes: what {agent_name} sees before it calls", "",
                   f"treg.to does not choose for you. It hands {agent_name} this comparison and it picks, "
                   "or you tell it how.", ""]
            if units:
                md += [f"### {spec.get('q_cheapest', 'Which is cheapest?')}", ""]
                for u in units:
                    pu = cheapest_by_unit[u]
                    md.append(f"- Cheapest per {u}: {pu['name']} at {money(pu['usd'])} (`{pu['cheapest_ep']['id']}`)")
                if len(units) > 1:
                    md += ["", "Those units are not interchangeable: one call can return many results, "
                               "so compare on the unit you will actually be billed in."]
            if reliable:
                md += ["", f"### {spec.get('q_reliable', 'Which is the most reliable?')}", ""]
                md += [f"- {p['name']}: {pct(p['ok_rate'])} over {p['samples']} calls, {ms(p['p50'])} median"
                       for p in reliable[:6]]
                md += ["", "Measured on treg.to traffic; not a controlled benchmark."]
            md += ["", f"### {spec.get('q_compare', 'How do they compare?')}", ""]
            for plat in (platforms if form == "platforms" else [None]):
                rows_ = [p for p in provs if plat is None or p["platform_label"] == plat]
                if plat:
                    md += [f"#### {plat}", ""]
                md += ["| Provider | Price | Accepts | Verified |", "|---|---|---|---|"]
                for p in sorted(rows_, key=lambda p: (p["usd"] is None, p["usd"] or 0)):
                    price = f"{money(p['usd'])} per {p['unit']}" if p["usd"] else "own account, free"
                    md.append(f"| {p['name']} | {price} | {', '.join(p['inputs'])} | {p['verified'] or 'unverified'} |")
                md.append("")
        md += ["Endpoints:", ""] + [f"- `{e['id']}`: {_uc_call(e)}" for e in eps]
        if spec.get("voices"):
            md += ["", "## What people actually struggle with", "", spec["voices_intro"], ""]
            for head, quote, who, url, answer in spec["voices"]:
                md += [f"**{head}**", "", f'> "{quote}" ({who}: {url})', "",
                       f"What this page can do about it: {answer}", ""]
        md += ["", "## What actually differs", ""] + [f"- {x}" for x in spec["notes"]]
        md += ["", f"## {spec.get('what_is_heading', 'What is this?')}", "", spec["what_is"], "", "## Questions", ""]
        for q, a in spec["faq"]:
            md += [f"**{q}** {a}", ""]
        md += [f"HTML version: {base}/use-cases/{cat_slug}/{job_slug}"]
        return PlainTextResponse("\n".join(md), media_type="text/markdown; charset=utf-8",
                                 headers={"Cache-Control": "public, max-age=600"})

    # ---------------------------------------------------------------- html (landing-page skin)
    ptiles = "".join(
        f'<span class="ptile" title="{_esc_html(p["name"])}">{_logo(p["domain"], p["name"])}</span>'
        for p in provs[:12] if p["domain"])
    provstrip = (f'<div class="provstrip"><div class="pl">compared on this page</div>'
                 f'<div class="ptiles">{ptiles}</div></div>' if ptiles else "")
    agent_icons = "".join(
        f'<span class="ptile" title="{_esc_html(label)}">'
        f'<img src="https://unpkg.com/@lobehub/icons-static-png@latest/light/{icon}.png" alt="{_esc_html(label)}" loading="lazy"/></span>'
        for aid, label, icon in agent_pages.AGENT_ICONS[:6])
    hero_price = (f"from {_esc_html(money(headline['usd']))} per {headline['unit']}"
                  if headline else "free on the account you already own")

    def promptbox(label: str, text: str) -> str:
        return ('<div class="promptbox"><div class="ph">'
                f'<span>{_esc_html(label)}</span>'
                f'<button class="copybtn" data-copy="{_esc_html(text)}">copy</button></div>'
                f'<pre>{_esc_html(text)}</pre></div>')

    why_cards = "".join(f'<div class="card"><h4>{_esc_html(t)}</h4><p>{_esc_html(d)}</p></div>'
                        for t, d in spec["prompt_why"])
    treg_cards = "".join(f'<div class="card"><h4>{_esc_html(t)}</h4><p>{_esc_html(d)}</p></div>'
                         for t, d in agent_pages.WHY_TREG)

    def price_cell(p: dict) -> str:
        return (f'{_esc_html(money(p["usd"]))} <span style="color:var(--muted2)">per {p["unit"]}</span>'
                if p["usd"] else '<span style="color:var(--green)">free, your own account</span>')

    def rel_cell(p: dict) -> str:
        return (f'{pct(p["ok_rate"])} <span style="color:var(--muted2)">({p["samples"]} calls)</span>'
                if p["samples"] else '<span style="color:var(--muted2)">not yet measured</span>')

    def prov_table(rows_: list[dict]) -> str:
        body_rows = "".join(
            f'<tr data-provider="{_esc_html(p["id"])}">'
            f'<td><b>{_logo(p["domain"], p["name"])}{_esc_html(p["name"])}</b></td>'
            f'<td>{price_cell(p)}</td>'
            f'<td style="color:var(--muted)">{_esc_html(", ".join(p["inputs"]) or "see endpoints")}</td>'
            f'<td>{rel_cell(p)}</td>'
            f'<td style="color:var(--muted2)">{_esc_html(p["verified"] or "unverified")}</td>'
            '</tr>' for p in rows_)
        return ('<div class="tablewrap"><table><thead><tr>'
                '<th>Provider</th><th>Price</th><th>Accepts</th><th>Success rate</th><th>Verified</th>'
                f'</tr></thead><tbody>{body_rows}</tbody></table></div>')

    sections = []
    if form == "short":
        p0, e0 = provs[0], provs[0]["eps"][0]
        sections.append(
            '<section id="how"><div class="wrap"><div class="seclab">How it works</div>'
            f'<h2>One provider, on the account you already own</h2>'
            f'<p>{_logo(p0["domain"], p0["name"])}<b>{_esc_html(p0["name"])}</b> answers this job. You connect it once, '
            'treg.to keeps the token server side, and the call is never metered.</p>'
            f'<div class="sample"><div class="sbar">the call</div><pre>{_esc_html(_uc_call(e0))}</pre></div>'
            f'<p style="font-size:12.5px;color:var(--muted)">Every endpoint on this connection is listed on the '
            f'<a href="/catalog/{_esc_html(e0["platform"])}">{_esc_html((cat.platforms.get(e0["platform"]) or {}).get("label") or e0["platform"])} shelf</a>.</p>'
            + '</div></section>')
    else:
        inner = [f'<p>treg.to does not choose for you. It hands {_esc_html(agent_name)} this comparison, with the '
                 f'price shown before any call, and {_esc_html(agent_name)} picks. Or you <b>tell it how</b>: '
                 '"cheapest", "most reliable", "the one that takes what I have", or a provider by name.</p>']
        if headline:
            cheap_cards = "".join(
                f'<div class="card"><h4>Cheapest per {_esc_html(u)}</h4>'
                f'<p>{_logo(cheapest_by_unit[u]["domain"], cheapest_by_unit[u]["name"])}'
                f'<b>{_esc_html(cheapest_by_unit[u]["name"])}</b> at {_esc_html(money(cheapest_by_unit[u]["usd"]))}'
                + (f' &middot; {_esc_html(cheapest_by_unit[u]["platform_label"])}' if form == "platforms" else "")
                + '</p></div>' for u in units)
            inner.append(f'<h3 id="cheapest">{_esc_html(spec.get("q_cheapest", "Which is cheapest?"))}</h3>'
                         f'<div class="cards">{cheap_cards}</div>')
            if len(units) > 1:
                inner.append('<blockquote>Those units are not interchangeable: one call can return many results, '
                             'so compare on the unit you will actually be billed in.</blockquote>')
        if reliable:
            rel_rows = "".join(
                f'<tr><td><b>{_logo(p["domain"], p["name"])}{_esc_html(p["name"])}</b></td>'
                f'<td>{pct(p["ok_rate"])}</td><td>{ms(p["p50"])}</td><td style="color:var(--muted2)">{p["samples"]} calls</td></tr>'
                for p in reliable[:6])
            inner.append(f'<h3 id="reliable">{_esc_html(spec.get("q_reliable", "Which is the most reliable?"))}</h3>'
                         '<div class="tablewrap"><table><thead><tr><th>Provider</th><th>Success</th><th>Median</th>'
                         f'<th>Sample</th></tr></thead><tbody>{rel_rows}</tbody></table></div>'
                         '<blockquote>Measured on treg.to traffic: real calls, real inputs, and sample sizes differ '
                         'by provider. Live reliability, not a controlled benchmark.</blockquote>')
        inner.append(f'<h3 id="compare">{_esc_html(spec.get("q_compare", "How do they compare?"))}</h3>')
        if form == "platforms":
            for plat in platforms:
                rows_ = sorted([p for p in provs if p["platform_label"] == plat],
                               key=lambda p: (p["usd"] is None, p["usd"] or 0))
                inner.append(f'<h4 data-platform-group="{_esc_html(plat)}">{_esc_html(plat)}</h4>' + prov_table(rows_))
        else:
            inner.append(prov_table(sorted(provs, key=lambda p: (p["usd"] is None, p["usd"] or 0))))
        if headline and headline["cheapest_ep"]:
            shelves = ", ".join(
                f'<a href="/catalog/{_esc_html(sl)}">{_esc_html((cat.platforms.get(sl) or {}).get("label") or sl)}</a>'
                for sl in sorted({p["platform"] for p in provs}))
            inner.append(
                '<h3>Run one</h3>'
                f'<div class="sample"><div class="sbar">the cheapest verified call</div>'
                f'<pre>{_esc_html(_uc_call(headline["cheapest_ep"]))}</pre></div>'
                f'<p style="font-size:12.5px;color:var(--muted)">Swap the id for any provider above. '
                f'All {len(eps)} endpoints behind this job, with their parameters and captured responses, '
                f'are on the {shelves} shelf.</p>')
        inner.append(
            '<h3>How these numbers are made</h3>'
            '<div class="who">'
            '<div><b>Prices</b>Each provider&rsquo;s own published rate, converted to US dollars for one chargeable '
            'event of the unit they bill in. treg.to adds $0.000. Where a provider bills in credits, the conversion '
            'uses the rate on their public pricing page'
            + (f', last checked {_esc_html(latest_verified)}.' if latest_verified else '.') + '</div>'
            '<div><b>Success rate</b>treg.to&rsquo;s own served calls over the last 30 days: 2xx counts as a success, '
            '5xx and timeouts as a failure. A 4xx is excluded, because it usually means the caller sent bad '
            'parameters and one bad query should not make a healthy endpoint look broken.</div>'
            '<div><b>What this is not</b>A controlled benchmark. These are real calls with real inputs, so sample '
            'sizes and the difficulty of what was asked differ by provider. Treat the rates as live reliability, '
            'not a like-for-like test.</div>'
            '<div><b>Verified</b>The date treg.to last called the endpoint end to end and confirmed the shape of '
            'its response and the price it charged.</div>'
            '</div>')
        sections.append(f'<section id="bts"><div class="wrap"><div class="seclab">Behind the scenes</div>'
                        f'<h2>What {_esc_html(agent_name)} sees before it calls</h2>'
                        + "".join(inner) + '</div></section>')

    voices_html = "".join(
        f'<h3>{_esc_html(head)}</h3>'
        f'<blockquote>&ldquo;{_esc_html(quote)}&rdquo; '
        f'<a href="{_esc_html(url)}" rel="nofollow noopener" target="_blank">{_esc_html(who)}</a></blockquote>'
        f'<p><b>What this page can do about it:</b> {_esc_html(answer)}</p>'
        for head, quote, who, url, answer in spec.get("voices", []))
    voices_section = ('<section id="voices"><div class="wrap"><div class="seclab">From the field</div>'
                      '<h2>What people actually struggle with</h2>'
                      f'<p>{_esc_html(spec.get("voices_intro", ""))}</p>{voices_html}</div></section>'
                      if spec.get("voices") else "")
    notes = "".join(f'<h4>{_esc_html(x.split(".")[0])}.</h4><p>{_esc_html(x.split(".", 1)[1].strip())}</p>'
                    if "." in x else f"<p>{_esc_html(x)}</p>" for x in spec["notes"])
    related = "".join(
        f'<a class="card" href="{_use_case_page_for(cat_label, lbl) or ("/agents/" + agent_slug + "#" + agent_pages.category_slug(cat_label))}">'
        f'<h4>{_esc_html(lbl)}</h4><p>Another job in {_esc_html(cat_label.lower())}.</p></a>'
        for lbl in spec.get("related", ()))
    faq_html = "".join(f'<h3>{_esc_html(q)}</h3><p>{_esc_html(a)}</p>' for q, a in spec["faq"])

    # The "instead of" anchor: what the same job costs on subscriptions from the providers on this
    # page whose plan prices are recorded in marketing/landing/_facts.md, against a real run here.
    # Only sourced figures are named; with none, the anchor is the catalog's own spread.
    plans = [(p["name"], agent_pages.PLAN_PRICES[p["id"]]) for p in provs
             if p["id"] in agent_pages.PLAN_PRICES]
    pricewall = ""
    if headline:
        run_n = 100
        run_cost = headline["usd"] * run_n
        if plans:
            plans = sorted(plans, key=lambda t: -t[1])[:2]
            old_total = sum(v for _, v in plans)
            old_note = " + ".join(f"{k} ${v}/mo" for k, v in plans) + ", at list"
            old_v, old_k = f"${old_total}/mo", "instead of"
        else:
            dearest = max((p for p in provs if p["usd"]), key=lambda p: p["usd"])
            old_total = dearest["usd"] * run_n
            old_note = f"{dearest['name']}, the dearest here, for the same {run_n}"
            old_v, old_k = f"${old_total:,.2f}", "the wide end"
        pricewall = (
            '<section id="cost"><div class="wrap"><div class="seclab">The economics</div>'
            f'<h2>What {run_n} of these actually costs</h2>'
            '<div class="pricewall">'
            f'<div class="pw old"><div class="k">{old_k}</div><div class="v">{old_v}</div>'
            f'<div class="s">{_esc_html(old_note)}</div></div>'
            '<div class="arrow">&rarr;</div>'
            f'<div class="pw new"><div class="k">you pay</div><div class="v">${run_cost:,.2f}</div>'
            f'<div class="s">{run_n} &times; {_esc_html(money(headline["usd"]))} at {_esc_html(headline["name"])}, '
            'metered per call</div></div></div>'
            '<p style="font-size:12.5px;color:var(--muted)">Subscription figures are provider list prices recorded in '
            'treg.to&rsquo;s own catalog grid; per-call prices are what treg.to charges today, with $0.000 added.</p>'
            '</div></section>')

    body = (
        '<div class="hero"><div class="wrap">'
        f'<div class="trust" style="margin:0 0 18px"><a href="/">treg.to</a> / <a href="/use-cases">Use cases</a> / '
        f'<a href="/agents/{agent_slug}#{agent_pages.category_slug(cat_label)}">{_esc_html(cat_label)}</a></div>'
        f'<div class="kicker">{n} providers &middot; {hero_price} &middot; $0.000 markup</div>'
        f'<h1>{_esc_html(spec["sentence"])}</h1>'
        f'<div class="lede">{_esc_html(lede)}</div>'
        '<div class="ctas">'
        f'<a class="candy" href="/app?ref=uc-{_esc_html(job_slug)}">Start free</a>'
        '<a class="ghostbtn" href="#bts">See the comparison</a></div>'
        f'<div class="trust">$1.00 of free credit on every new team &middot; no provider signup &middot; no card</div>'
        f'<div class="subline">{n_ver} of {len(eps)} endpoints on this page are live-verified against the provider.</div>'
        f'{provstrip}</div></div>'

        + pricewall +
        '<section id="ask"><div class="wrap"><div class="seclab">Try it</div>'
        f'<h2>What&rsquo;s the best way to ask {_esc_html(agent_name)}?</h2>'
        f'<div class="steplabel"><span class="n">1</span><b>Set your agent up, once</b></div>'
        + promptbox("in your agent's chat", setup)
        + f'<div class="steplabel"><span class="n">2</span><b>Ask for the job</b></div>'
        + promptbox("the prompt", spec["prompt"])
        + f'<div class="provstrip"><div class="pl">works in</div><div class="ptiles">{agent_icons}</div></div>'
        + f'<h3>Why this prompt works</h3><div class="cards">{why_cards}</div>'
        + (f'<div class="sample"><div class="sbar">{_esc_html(agent_name)}</div>'
           f'<img src="{_esc_html(spec["result_image"])}" alt="{_esc_html(agent_name)} answering" '
           'style="display:block;width:100%"/></div>' if spec.get("result_image") else "")
        + '</div></section>'

        '<section id="why"><div class="wrap"><div class="seclab">Why treg.to</div>'
        '<h2>Why go through treg.to</h2>'
        f'<div class="cards">{treg_cards}</div></div></section>'

        + "".join(sections) + voices_section

        + '<section id="notes"><div class="wrap"><div class="seclab">The detail</div>'
          f'<h2>What actually differs</h2>{notes}</div></section>'

        + f'<section id="what"><div class="wrap"><div class="seclab">Background</div>'
          f'<h2>{_esc_html(spec.get("what_is_heading", "What is this?"))}</h2>'
          f'<p>{_esc_html(spec["what_is"])}</p></div></section>'

        + f'<section id="faq"><div class="wrap"><div class="seclab">Questions</div>'
          f'<h2>Before you start</h2>{faq_html}</div></section>'

        + (f'<section id="related"><div class="wrap"><div class="seclab">Related</div>'
           f'<h2>Other jobs your agent can do</h2><div class="cards">{related}</div></div></section>' if related else "")
        + _COPY_JS)
    ld = [
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "treg.to", "item": base + "/"},
            {"@type": "ListItem", "position": 2, "name": "Use cases", "item": base + "/use-cases"},
            {"@type": "ListItem", "position": 3, "name": cat_label,
             "item": f"{base}/agents/{agent_slug}#{agent_pages.category_slug(cat_label)}"},
            {"@type": "ListItem", "position": 4, "name": spec["sentence"],
             "item": f"{base}/use-cases/{cat_slug}/{job_slug}"}]},
        {"@context": "https://schema.org", "@type": "ItemList", "name": title, "numberOfItems": len(provs),
         "itemListElement": [{"@type": "ListItem", "position": i, "name": p["name"],
                              "url": f"{base}/use-cases/{cat_slug}/{job_slug}#compare"}
                             for i, p in enumerate(provs, 1)]},
        {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}
            for q, a in spec["faq"]]},
    ]
    return _page(title, desc[:300], f"/use-cases/{cat_slug}/{job_slug}", body, ld,
                 head_extra=_MD_ALT.format(href=f"{base}/use-cases/{cat_slug}/{job_slug}.md"),
                 css="usecase.css")


@app.get("/use-cases", include_in_schema=False)
async def use_cases_hub():
    """The hub the spokes hang from. A sitemap is not a crawl path: before this existed, the only
    link into a use-case page was one row on one agent page's menu."""
    if not _hosted():
        raise HTTPException(status_code=404, detail="not found")
    cat = catalog_store.load()
    base = get_settings().public_url.rstrip("/")
    _, agent_name = _uc_agent()
    by_cat: dict[str, list[str]] = {}
    for (c, j), spec in agent_pages.USE_CASE_PAGES.items():
        label = next((cl for cl, _ in agent_pages.USE_CASES if agent_pages.category_slug(cl) == c), c)
        caps = _use_case_caps(c, spec["label"])
        eps = [e for cid in caps for e in cat.for_capability(cid) if e["kind"] not in catalog_store.HIDDEN_KINDS]
        nprov = len({e["provider"] for e in eps})
        prices = [cv["usd"] for e in eps if (cv := cat.cost_view(e.get("cost"), e.get("provider"))) and cv["usd"]]
        meta = (f"{nprov} provider{'s' if nprov != 1 else ''} &middot; from {_esc_html(_usd_short(min(prices)))}"
                if prices else "free on your own account")
        blurb = spec["lede"].format(n=nprov, agent=agent_name,
                                    cheapest=_usd_short(min(prices)) if prices else "free")
        by_cat.setdefault(label, []).append(
            f'<a class="pcard" href="/use-cases/{c}/{j}"><h3>{_esc_html(spec["sentence"])}</h3>'
            f'<p>{_esc_html(blurb[:140])}</p><div class="meta">{meta}</div></a>')
    blocks = "".join(f'<section class="cat"><h2 id="{_anchor(c)}">{_esc_html(c)}</h2>'
                     f'<div class="grid">{"".join(v)}</div></section>' for c, v in by_cat.items())
    body = (
        '<main class="wrap"><div class="phead">'
        '<div class="crumbs"><a href="/">treg.to</a> / <a href="/use-cases">Use cases</a></div>'
        '<h1>What you can have your agent do</h1>'
        '<p class="lede">One page per job: the prompt that works, what the call costs, and every provider '
        'that does it. All of it through one treg.to key, at the provider&rsquo;s own rate with $0.000 markup.</p>'
        '</div>' + blocks
        + '<section class="cat"><h2>Everything else</h2><div class="cap"><p style="margin:0">These are the jobs '
          'written up so far. The full menu is on the agent pages, and the whole catalog is at '
          '<a href="/catalog">/catalog</a>.</p></div></section></main>')
    ld = [{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "treg.to", "item": base + "/"},
        {"@type": "ListItem", "position": 2, "name": "Use cases", "item": base + "/use-cases"}]}]
    return _page("What you can have your agent do | treg.to",
                 "One page per job: the prompt that works in ChatGPT or Claude, what the call costs, and "
                 "every provider that does it, compared. One treg.to key, no markup.",
                 "/use-cases", body, ld)


@app.get("/catalog.css", include_in_schema=False)
async def catalog_css():
    """The shared skin for /catalog, /catalog/<slug> and /docs — the landing's tokens, one copy."""
    f = _WEB_DIR / "catalog.css"
    if not f.exists():
        raise HTTPException(status_code=404, detail="catalog.css not bundled")
    return FileResponse(f, media_type="text/css", headers={"Cache-Control": "public, max-age=600"})


# ---- the API reference ------------------------------------------------------------------------
# Prose first, because the schema cannot say the load-bearing part: what /call/ actually does. Kept
# short and factual — the tutorial teaches, this page is the reference a reader lands on from search.
_DOCS_INTRO = """
<h2>How a call works</h2>
<p>You make the <b>real upstream request</b> — the provider's own path, its own parameters, its own
response. treg injects the credential server-side and relays the answer verbatim. Nothing here
models a provider's API, which is why an upstream change does not break us and why the caller never
holds a secret.</p>
<pre class="call">curl -H "Authorization: Bearer $TREG_TOKEN" \\
  "{BASE}/call/moz.web.url.metrics"</pre>
<p>Prefix any catalogued endpoint id with <code>/call/</code>. If your team has its own key for that
provider, treg uses it and the call is <b>not metered</b>; otherwise eligible endpoints are served on
treg's key and metered against your prepaid balance at the provider's own rate.</p>

<h2>Finding an endpoint</h2>
<p>Search by what you want to <i>do</i>, not by vendor: <code>GET /catalog/search?q=backlinks</code>.
When several providers can do the same job, <code>/catalog/platforms/{slug}</code> lists them side by
side with measured success rate, speed and price. <b>Choosing is yours</b> — treg compares, but it
does not route between providers automatically and does not fail over.</p>
<p>The whole catalog is also browsable as pages: <a href="/catalog">/catalog</a>.</p>

<h2>Other ways in</h2>
<p><a href="/llms.txt">/llms.txt</a> is the file to point a coding agent at — it teaches the whole
protocol in one fetch. <code>curl -fsSL {BASE}/install.sh | sh</code> installs the CLI. The MCP
endpoint is at <code>{BASE}/mcp</code>. An interactive console for everything below lives at
<a href="/docs/api">/docs/api</a>.</p>

<h2>Endpoints</h2>
<p>Authenticated requests carry <code>Authorization: Bearer &lt;token&gt;</code> (or
<code>X-Treg-Token</code>). The catalog routes are open and need no token.</p>
"""


@app.get("/docs", include_in_schema=False)
async def docs_page():
    """The API reference, rendered server-side from the OpenAPI schema.

    Replaces the stock Swagger UI at this path (now /docs/api), which was a script shell — the
    landing page linked "api" here and a crawler that followed it found an empty document.
    """
    base = get_settings().public_url.rstrip("/")
    schema = app.openapi()

    def rank(path: str) -> tuple:
        """The proxy first, then the catalog, then the rest alphabetically. Sorting purely by path
        opened the reference on /admin/* — super-admin plumbing, and the worst possible first
        impression of the API on a page built to be someone's search result."""
        return (0 if path.startswith("/call/") else 1 if path.startswith("/catalog") else 2, path)

    # Auth travels the same way on every route; naming it on all 135 rows is noise, and the page
    # says it once above. `/admin/*` is super-admin only — still in openapi.json, not advertised here.
    _PLUMBING = {"x-treg-token", "treg_session", "authorization"}
    ops = []
    for path in sorted(schema.get("paths", {}), key=rank):
        if path.startswith("/admin"):
            continue
        for method, op in sorted(schema["paths"][path].items()):
            if method.lower() == "head":     # implied by GET; see `_openapi_without_head`
                continue
            params = ", ".join(p["name"] for p in op.get("parameters", []) or []
                               if p["name"].lower() not in _PLUMBING)
            summary = op.get("summary") or ""
            # FastAPI takes the description from the docstring; only the first paragraph belongs on
            # a reference index, and the rest is written for maintainers rather than callers.
            desc = (op.get("description") or "").strip().split("\n\n")[0].replace("\n", " ")
            ops.append(
                f'<div class="op"><div class="sig"><span class="verb">{_esc_html(method.upper())}</span>'
                f'<code>{_esc_html(path)}</code></div>'
                + (f"<p>{_esc_html(summary or desc)}</p>" if (summary or desc) else "")
                + (f'<div class="params">{_esc_html(params)}</div>' if params else "")
                + "</div>")

    body = f"""<main class="wrap">
<div class="phead">
  <div class="crumbs"><a href="/">treg</a> / api</div>
  <h1>API reference</h1>
  <p class="lede">One base URL, one token. Call any of {len(ops)} documented operations, or proxy a
  real request to any of 2,630 catalogued provider endpoints through <code>/call/</code>.</p>
  <div class="facts">
    <span>base <b>{_esc_html(base)}</b></span>
    <span><b>Bearer</b> token auth</span>
    <span><a href="/openapi.json">openapi.json</a></span>
    <span><a href="/docs/api">interactive console</a></span>
  </div>
</div>
<section class="cat">
  <div class="prose">{_DOCS_INTRO.replace("{BASE}", _esc_html(base))}</div>
  {"".join(ops)}
</section>
</main>"""
    ld = [{"@context": "https://schema.org", "@type": "TechArticle",
           "headline": "treg API reference",
           "description": "How to call 2,630 provider API endpoints through one treg token.",
           "url": f"{base}/docs"}]
    return _page("API reference — call any tool through one endpoint | treg",
                 "The treg HTTP API: proxy a real request to any of 2,630 catalogued provider "
                 "endpoints through /call/, with the credential injected server-side. Plus the "
                 "catalog, org, billing and tool-management routes.",
                 "/docs", body, ld, nav_current="/docs")


site_router = APIRouter()
app = site_router


def _esc_html(s: str) -> str:
    """The stdlib escaper, not a hand-rolled replace() chain.

    Same four substitutions as before plus `'` -> `&#x27;`, so every call site is at least as safe.
    The reason to delegate is not correctness but legibility to tooling: static analysis models
    `html.escape` as an XSS sanitizer and cannot know that a private chain of `.replace()` calls is
    one, so every escaped value stayed 'tainted' and the real sinks were buried in false positives.
    """
    return _html.escape(str(s), quote=True)


@app.get("/", include_in_schema=False)
async def landing(request: Request, treg_session: str = Cookie(default=""),
                  db: AsyncSession = Depends(get_session)):
    """Serve the marketing landing at the root. Any query string (invite links, OAuth returns,
    tour deep-links) belongs to the SPA, so those requests fall through to the dashboard —
    the landing is only the clean, parameterless front door. A signed-in visitor belongs on
    the dashboard, so a live session redirects to /app instead of re-showing the pitch.

    `?ref=<code>` is the ONE exception, and it has to be: a referral link's whole job is to show a
    stranger the pitch. Falling through to the SPA would send someone who has never heard of treg
    to an empty dashboard shell — so a lone `ref` counts as parameterless, and the code is parked in
    a cookie on the way past. It is only redeemed much later, when they create their first team.
    """
    page = _WEB_DIR / "landing.html"
    ref = referrals.normalize_code(request.query_params.get("ref", ""))
    # Only `ref` may be present. Anything else alongside it belongs to the SPA, and a referral code
    # is not a reason to hijack an invite or an OAuth return.
    ref_only = set(request.query_params.keys()) <= {"ref"}
    if page.exists() and (not request.query_params or (ref and ref_only)):
        if treg_session and await _user_from_session(treg_session, db):
            return RedirectResponse("/app", status_code=302)
        # Read-and-substitute rather than a bare FileResponse: the canonical, og:url and og:image
        # are `{BASE}`-templated so they name the serving host. Hardcoded, a self-hosted registry
        # would tell crawlers its front page really lives on treg.to.
        html = page.read_text(encoding="utf-8").replace(
            "{BASE}", get_settings().public_url.rstrip("/"))
        resp = HTMLResponse(html, headers={"Cache-Control": "no-cache"})
        if ref:
            _remember_referral(resp, request, ref)
        return resp
    return await dashboard(request, treg_session, db)


@app.get("/app", include_in_schema=False)
async def dashboard(
    request: Request, treg_session: str = Cookie(default=""),
    db: AsyncSession = Depends(get_session),
):
    """Serve the single-file dashboard (same-origin, so it calls this API directly).

    Also the place a parked OAuth authorization resumes. Every browser sign-in door — GitHub, Google,
    the email code — ends here, so honouring the cookie at this ONE point covers all of them, rather
    than threading a return value through five handlers that each finish differently (two redirect,
    one answers JSON).

    In frictionless local mode the dashboard opens ALREADY SIGNED IN: with no valid session we
    attach one for the machine's single user, so `curl … | sh` reaches a working dashboard without
    an account. Only reachable when `single_user_ok` holds (local sqlite + loopback URL), so this
    can never hand a session to a stranger on a real deploy.
    """
    index = _WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h3>tools-registry API. Dashboard not bundled.</h3>")
    signed_in = await _user_from_session(treg_session, db)
    # A parked authorization resumes here, but ONLY once the user is actually signed in — otherwise
    # this would bounce them back to /oauth/authorize, which would bounce them here again.
    if signed_in and (parked := _take_oauth_return(request)) is not None:
        resume = RedirectResponse(parked, status_code=302)
        resume.delete_cookie(OAUTH_RETURN_COOKIE)
        return resume
    resp = FileResponse(index, headers={"Cache-Control": "no-cache"})
    if not signed_in:
        owner = await _local_owner(db)
        if owner is not None:
            resp.set_cookie(sess.COOKIE, sess.make(owner.id, token_version=owner.token_version),
                            httponly=True, samesite="lax",
                            secure=_is_https(request),
                            max_age=sess.TTL_SECONDS)
    return resp


def _spa_with_og(kind: str, name: str):
    """Serve the SPA at a shareable detail path (/app/skills/x, /app/tools/x) with per-resource
    og/twitter meta so link unfurls show what was shared. The meta echoes only the URL's own
    name segment — no DB read, so an unauthenticated crawler learns nothing it didn't send."""
    index = _WEB_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("<h3>tools-registry API. Dashboard not bundled.</h3>")
    label = "skill" if kind == "skills" else "tool"
    safe = _esc_html(name)
    meta = (
        f"<title>{safe} · Treg</title>\n"
        f'<meta property="og:title" content="{safe} — shared {label}"/>\n'
        f'<meta property="og:description" content="A {label} shared via Treg. '
        f'Sign in to preview it and get the one-command install."/>\n'
        f'<meta name="twitter:card" content="summary"/>'
    )
    # Match WHATEVER title the page carries, not one exact string. It was pinned to
    # `<title>tools-registry</title>`, the page says `<title>treg</title>`, so the replacement
    # silently did nothing and every shared link unfurled blank — a rename in the dashboard must
    # not be able to switch this off without a word.
    html, hits = re.subn(r"<title>.*?</title>", lambda _m: meta, index.read_text(encoding="utf-8"),
                         count=1, flags=re.IGNORECASE | re.DOTALL)
    if not hits:  # no title at all: still emit the meta rather than serve a bare page
        html = html.replace("<head>", "<head>\n" + meta, 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/app/marketplace/{service}", include_in_schema=False)
async def dashboard_marketplace(
    service: str, request: Request, treg_session: str = Cookie(default=""),  # noqa: ARG001 — the SPA reads the path itself
    db: AsyncSession = Depends(get_session),
):
    """One integration's page. Served as the plain SPA: unlike /app/skills/<x> there is no og meta
    to add, because a marketplace page is only meaningful to a signed-in member of the org."""
    return await dashboard(request, treg_session, db)


@app.get("/app/skills/{name}", include_in_schema=False)
async def dashboard_skill_page(name: str):
    return _spa_with_og("skills", name)


@app.get("/app/tools/{name}", include_in_schema=False)
async def dashboard_tool_page(name: str):
    return _spa_with_og("tools", name)


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt():
    """Agent-readable overview (llms.txt convention) — an AI agent that fetches this learns the
    whole registry: the call protocol, discovery, auth, CLI, skills, and links to the tutorial/docs.
    The serving domain is templated in so links stay correct across deploys."""
    f = _WEB_DIR / "llms.txt"
    if not f.exists():
        raise HTTPException(status_code=404, detail="llms.txt not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base), media_type="text/plain; charset=utf-8")


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    """Crawler policy. `{BASE}`-templated like llms.txt, so a self-hosted registry advertises its own
    sitemap rather than treg.to's."""
    f = _WEB_DIR / "robots.txt"
    if not f.exists():
        raise HTTPException(status_code=404, detail="robots.txt not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base),
                             media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "max-age=3600"})


# The outcome landing pages: one per vertical, the destinations for search ads and the organic
# `/use-cases/` cluster. Their COPY is generated from marketing/landing/*.md — never hand-edit
# the HTML in web/, it is overwritten by that build. The slug is the public URL and is quoted in
# live ad campaigns, so treat this map as an API: add freely, never rename or remove without a
# redirect.
_USE_CASES = {
    "seo-data-for-ai-agents": "usecase-seo.html",
    "lead-enrichment-for-ai-agents": "usecase-enrichment.html",
    "social-trend-research-for-ai-agents": "usecase-social.html",
    "competitor-ad-research-for-ai-agents": "usecase-ads.html",
    "company-research-for-ai-agents": "usecase-company.html",
}


# The pages a crawler should know about. Everything here must answer 200 to a GET — a sitemap that
# lists a redirect or a 404 is worse than no sitemap, so `tests/test_seo.py` walks every entry.
# Deliberately absent: /contact and /help (alias URLs for the one support.html), /vendor-listing.md
# (the text/plain twin of /vendor-listing), /login (302s to /app), /app* (authenticated SPA),
# /connect-demo (noindex by design), and the shell installers.
_SITEMAP_PAGES: tuple[tuple[str, str, str], ...] = (
    # (path, source file for lastmod — "" means use the catalog's, priority)
    ("/", "landing.html", "1.0"),
    ("/catalog", "", "0.9"),
    ("/tutorial", "tutorial.html", "0.8"),
    ("/docs", "", "0.7"),
    ("/resources", "resources.html", "0.8"),
    ("/vendor-listing", "vendor-listing.md", "0.5"),
    ("/support", "support.html", "0.4"),
    ("/terms", "terms.html", "0.2"),
    ("/privacy", "privacy.html", "0.2"),
    # The outcome pages. Listed WITHOUT a trailing slash on purpose: `/use-cases/<slug>/` 307s to
    # this form, and a sitemap that lists a redirect is worse than no sitemap. Their canonical tags
    # match these exactly. `_USE_CASES` is the one source for the set, so a new page is listed the
    # moment it is routed, and `tests/test_seo.py` will fail if one stops answering 200.
    *(
        (f"/use-cases/{slug}", name, "0.8")
        for slug, name in _USE_CASES.items()
    ),
)


@lru_cache(maxsize=1)
def _catalog_mtime() -> str:
    """The newest mtime under the catalog directory, as a sitemap `lastmod` date. The catalog is
    read-only and changes only on deploy, so one scan per process is enough."""
    newest = 0.0
    for f in (Path(catalog_store.__file__).parent / "catalog").rglob("*.yaml"):
        try:
            newest = max(newest, f.stat().st_mtime)
        except OSError:  # noqa: PERF203 -- a file vanishing mid-scan is not worth failing the sitemap
            continue
    return _iso_day(newest)


def _iso_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat() if ts else ""


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml():
    """Generated, not bundled: 80 of its URLs are the catalog's platform shelves, which move with the
    catalog rather than with a checked-in file. Every URL is absolute on `public_url` so a self-host
    publishes its own pages, and so the copy served on a legacy host still names the canonical one."""
    base = get_settings().public_url.rstrip("/")
    cat_day = _catalog_mtime()
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']

    def add(path: str, lastmod: str, priority: str) -> None:
        out.append("<url>")
        out.append(f"<loc>{_esc_html(base + path)}</loc>")
        if lastmod:
            out.append(f"<lastmod>{lastmod}</lastmod>")
        out.append(f"<priority>{priority}</priority>")
        out.append("</url>")

    for path, src, priority in _SITEMAP_PAGES:
        day = cat_day
        if src:
            f = _WEB_DIR / src
            day = _iso_day(f.stat().st_mtime) if f.exists() else ""
        add(path, day, priority)
    for row in _platform_rows():
        add(f"/catalog/{row['slug']}", cat_day, "0.6")
    # The agent pages exist only on the hosted deployment (see `_hosted`); their lastmod follows the
    # hand-written copy, which is what changes between deploys.
    if _hosted():
        copy_day = _iso_day(Path(agent_pages.__file__).stat().st_mtime)
        for slug in agent_pages.AGENTS:
            add(f"/agents/{slug}", copy_day, "0.8")
        add("/use-cases", copy_day, "0.8")
        for (c, j) in agent_pages.USE_CASE_PAGES:
            add(f"/use-cases/{c}/{j}", copy_day, "0.7")
    out.append("</urlset>")
    return Response("\n".join(out), media_type="application/xml; charset=utf-8",
                    headers={"Cache-Control": "max-age=3600"})


@app.get("/install.sh", include_in_schema=False)
async def install_sh():
    """`curl -fsSL {BASE}/install.sh | sh` — installs the treg CLI and points it at this server.
    The serving domain is templated in so it targets whichever host is live (dev box or the real
    domain after deploy)."""
    f = _WEB_DIR / "install.sh"
    if not f.exists():
        raise HTTPException(status_code=404, detail="install.sh not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base), media_type="text/x-shellscript; charset=utf-8")


@app.get("/selfhost.sh", include_in_schema=False)
async def selfhost_sh():
    """`curl -fsSL {BASE}/selfhost.sh | sh` — run your OWN registry locally, with no account.

    Different from install.sh, which only installs the CLI and points it at THIS server. This one
    brings up a server on the caller's machine in single-user mode, so they land on a dashboard that
    is already signed in. Value first, account later."""
    f = _WEB_DIR / "selfhost.sh"
    if not f.exists():
        raise HTTPException(status_code=404, detail="selfhost.sh not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base),
                             media_type="text/x-shellscript; charset=utf-8")


def _serve_md(name: str) -> PlainTextResponse:
    """Serve a bundled markdown file as inline text (so "open in new tab" shows it, not a download),
    with the serving domain templated in. Backs the 'copy markdown' buttons on the docs pages."""
    f = _WEB_DIR / name
    if not f.exists():
        raise HTTPException(status_code=404, detail=f"{name} not bundled")
    base = get_settings().public_url.rstrip("/")
    return PlainTextResponse(f.read_text(encoding="utf-8").replace("{BASE}", base),
                             media_type="text/plain; charset=utf-8")


@app.get("/quickstart.md", include_in_schema=False)
async def quickstart_md():
    """The quick-start as raw markdown — copy it or open it in a tab and use it anywhere."""
    return _serve_md("quickstart.md")


@app.get("/tutorial.md", include_in_schema=False)
async def tutorial_md():
    """The full tutorial as raw markdown (mirrors the interactive /tutorial)."""
    return _serve_md("tutorial.md")


@app.get("/tutorial-import-shell.md", include_in_schema=False)
async def tutorial_import_shell_md():
    """Focused tutorial: CLI auto-import (`treg upload clis`) + shell mode (`treg shell`) + the
    local-run security sandbox. Linked from the main tutorial."""
    return _serve_md("tutorial-import-shell.md")


@app.get("/tutorial-access.md", include_in_schema=False)
async def tutorial_access_md():
    """Focused tutorial: per-member team access control (which tools a member may use + the local-run
    toggle). Linked from the main tutorial."""
    return _serve_md("tutorial-access.md")


@app.get("/vendor-listing", include_in_schema=False)
@app.get("/vendor-listing.md", include_in_schema=False)
async def vendor_listing_md(request: Request):
    """Vendor listing instructions — what a vendor's coding agent reads before raising a PR that
    adds their API to the catalog. Linked from the dashboard's "List your API" modal."""
    resp = _serve_md("vendor-listing.md")
    # Two URLs, one document. `text/plain` cannot carry a <link rel=canonical>, so the duplicate is
    # suppressed with the header equivalent: /vendor-listing is the indexed one (it is what the
    # sitemap lists), /vendor-listing.md keeps serving agents and stays out of the index.
    if request.url.path.endswith(".md"):
        resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@app.get("/integrate.md", include_in_schema=False)
async def integrate_md():
    """The BUILDER skill: how to put treg inside your own product and bill your own customers for it.

    Distinct from `skill.md`, which teaches an agent to USE treg. This one is pasted into a builder's
    repo and pointed at their coding agent, so it leads with the per-customer billing model — the
    part that changes how the plumbing is written, and therefore has to be read before any of it is.
    """
    return _serve_md("integrate.md")


@app.get("/skill.md", include_in_schema=False)
async def skill_md():
    """The OFFICIAL treg Claude skill (3 personas), {BASE}-templated to this server.
    install.sh drops it into ~/.claude/skills/treg/ so agents learn treg at CLI install."""
    return _serve_md("skill.md")


@app.get("/favicon.svg", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """The ▚ brand mark. Served at both paths so browsers that auto-request /favicon.ico stop 404ing."""
    ico = _WEB_DIR / "favicon.svg"
    if not ico.exists():
        raise HTTPException(status_code=404, detail="favicon not bundled")
    return FileResponse(ico, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@app.get("/tutorial.js", include_in_schema=False)
async def tutorial_js():
    """The shared interactive-tutorial data + highlighter (window.TREG_TUTORIAL / tregHL).
    Loaded by both the dashboard Help view and the standalone tutorial page, so they never drift."""
    js = _WEB_DIR / "tutorial.js"
    if not js.exists():
        raise HTTPException(status_code=404, detail="tutorial.js not bundled")
    # no-cache, like index.html and the landing: the page includes this as a bare `<script
    # src="/tutorial.js">` with no version query, so without the header a browser keeps serving the
    # tutorial from before the last deploy until someone hard-refreshes.
    return FileResponse(js, media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/legal.css", include_in_schema=False)
async def legal_css():
    """The shared skin for /terms and /privacy (landing-page tokens, one copy)."""
    f = _WEB_DIR / "legal.css"
    if not f.exists():
        raise HTTPException(status_code=404, detail="legal.css not bundled")
    return FileResponse(f, media_type="text/css", headers={"Cache-Control": "no-cache"})


def _legal_page(name: str) -> HTMLResponse:
    page = _WEB_DIR / name
    if not page.exists():
        raise HTTPException(status_code=404, detail=f"{name} not bundled")
    # `{BASE}`-substituted rather than sent as a plain FileResponse, so each page's canonical and
    # og:url name the host actually serving it. A hardcoded treg.to would tell a self-hosted
    # registry's crawler that the real page lives on someone else's domain.
    base = get_settings().public_url.rstrip("/")
    html = page.read_text(encoding="utf-8").replace("{BASE}", base)
    # no-cache: a legal page must not be served stale after we publish an update.
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/terms", include_in_schema=False)
async def terms_page():
    """Terms of Service for the HOSTED registry (self-hosted instances are governed by LICENSE)."""
    return _legal_page("terms.html")


@app.get("/privacy", include_in_schema=False)
async def privacy_page():
    """Privacy policy. Also the URL given to OAuth providers at app-registration/verification time
    (Google requires a reachable privacy policy carrying the Limited Use disclosure), so this path
    is effectively public API — don't rename it without updating the provider consoles."""
    return _legal_page("privacy.html")


@app.get("/adtrack.js", include_in_schema=False)
async def adtrack_js():
    """First-party ad-click capture (see the file itself): sets the `treg_ad` cookie that
    `_ad_attribution_from` reads at signup. No Google script, no third-party request."""
    headers = {"Cache-Control": "no-cache"}
    if not adsconv.enabled():
        # The page keeps one static script include, but an unconfigured/self-hosted deployment must
        # not collect an advertising cookie at all. A stale cookie is also ignored at signup below.
        return Response(content="", media_type="application/javascript", headers=headers)
    f = _WEB_DIR / "adtrack.js"
    if not f.exists():
        raise HTTPException(status_code=404, detail="adtrack.js not bundled")
    # no-cache, same reasoning as tutorial.js: served as a bare `<script src="/adtrack.js">` with no
    # version query, so without this header a browser would keep an ad-window-stale copy after a fix.
    return FileResponse(f, media_type="application/javascript", headers=headers)


@app.get("/resources", include_in_schema=False)
async def resources_page():
    """The hub for the outcome pages. It exists for two reasons beyond navigation: without it the
    `/use-cases/*` pages are orphans that no crawler reaches, and it gives the footer one durable
    link instead of five that grow every time a page is added."""
    page = _WEB_DIR / "resources.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="resources.html not bundled")
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/usecase.css", include_in_schema=False)
async def usecase_css():
    """The shared skin for /use-cases/* (landing-page tokens, one copy — same deal as legal.css)."""
    f = _WEB_DIR / "usecase.css"
    if not f.exists():
        raise HTTPException(status_code=404, detail="usecase.css not bundled")
    return FileResponse(f, media_type="text/css", headers={"Cache-Control": "no-cache"})


@app.get("/use-cases/{slug}", include_in_schema=False)
async def use_case_page(slug: str):
    """One outcome page. Unlike the root landing this does NOT redirect a signed-in visitor to
    /app: these are ad destinations, and bouncing a returning user away from the page they paid
    to reach would make the campaign data unreadable."""
    name = _USE_CASES.get(slug.strip("/").lower())
    if not name:
        raise HTTPException(status_code=404, detail="unknown use case")
    page = _WEB_DIR / name
    if not page.exists():
        raise HTTPException(status_code=404, detail=f"{name} not bundled")
    # no-cache: these are edited against live campaign data and must never serve stale.
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


public_docs_router = APIRouter()
app = public_docs_router


def _skill_frontmatter() -> dict[str, str]:
    """The bundled skill's frontmatter, read at request time rather than duplicated in code — the
    description is what drives discovery in every registry, and a second copy of it would drift."""
    f = _WEB_DIR / "skill.md"
    if not f.exists():
        raise HTTPException(status_code=404, detail="skill.md not bundled")
    text = f.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise HTTPException(status_code=404, detail="skill.md has no frontmatter")
    out: dict[str, str] = {}
    for line in text.split("---", 2)[1].strip().splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


@app.get("/.well-known/skills/index.json", include_in_schema=False)
async def well_known_skills_index():
    """Advertise treg's own skill under the agentskills.io well-known convention.

    This makes THIS host a first-class skill source: an agent that supports the standard can install
    treg from treg.to directly, with no directory, no review queue and no third party in the middle
    — the same skill the plugins ship and `install.sh` drops, reached by whoever asks the domain.
    """
    fm = _skill_frontmatter()
    return JSONResponse({"skills": [{
        "name": fm.get("name", "treg"),
        "description": fm.get("description", ""),
        "files": ["SKILL.md"],
    }]})


@app.get("/.well-known/skills/treg/SKILL.md", include_in_schema=False)
async def well_known_skill_md():
    """The skill itself, at the path `index.json` promises. Deliberately the same `_serve_md` the
    canonical `/skill.md` uses, so `{BASE}` is templated to the serving host here too — a self-hosted
    registry advertises ITSELF, not treg.to."""
    return _serve_md("skill.md")


@app.get("/connect-demo", include_in_schema=False)
async def connect_demo_page():
    """A page that PRETENDS to be someone else's app, so the OAuth flow can be seen end to end.

    It uses only public endpoints — register, authorize, token, revoke, and /mcp/ — with nothing
    privileged about being served from treg's own domain. The point is to watch the whole dance in a
    browser before trusting it inside ChatGPT, where a failure surfaces as a shrug rather than an
    error message.
    """
    return _legal_page("connect-demo.html")


@app.get("/connect-demo/callback", include_in_schema=False)
async def connect_demo_callback():
    """Where treg sends the browser back. Hands the code to the opener and closes."""
    return _legal_page("connect-demo-callback.html")


@app.get("/support", include_in_schema=False)
@app.get("/contact", include_in_schema=False)
@app.get("/help", include_in_schema=False)
async def support_page():
    """How to get help. Three paths for one page because people guess differently, and because a
    plugin-directory listing must give a Support URL that resolves — a 404 there reads as an
    abandoned product. Like `/privacy`, this path is effectively public API once it is filed with a
    directory or an OAuth console: don't rename it without updating them."""
    return _legal_page("support.html")


@app.get("/tutorial", include_in_schema=False)
async def tutorial_page():
    """Standalone shareable interactive tutorial (same STEPS[] as the dashboard Help view)."""
    page = _WEB_DIR / "tutorial.html"
    if not page.exists():
        return HTMLResponse("<h3>Tutorial not bundled.</h3>")
    # Stamp the tutorial.js URL with the bundle version. `no-cache` alone is not enough: a browser
    # that cached the file BEFORE that header existed applies a heuristic lifetime and never
    # revalidates, so an edited tutorial silently keeps serving the old steps (cost an hour to find).
    js = _WEB_DIR / "tutorial.js"
    stamp = int(js.stat().st_mtime) if js.exists() else 0   # tutorial.js's OWN mtime: _app_version()
    html = page.read_text(encoding="utf-8").replace(       # hashes index.html and would not move
        'src="/tutorial.js"', f'src="/tutorial.js?v={stamp}"')
    html = html.replace("{BASE}", get_settings().public_url.rstrip("/"))  # canonical + og:url
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


# Provider logos, resolved by convention: /logos/<service>.svg, matching `service` in
# oauth_providers.py. Keyed off the name the registry already has, so adding a provider needs no
# second registration step — drop the file in and it appears. Public and unauthenticated: they are
# brand marks, not data, and the dashboard renders them before the caller is known.
_LOGO_DIR = _WEB_DIR / "logos"


# Demo recordings — the plugin-directory submission requires a publicly reachable video URL, and
# hosting it ourselves means no third-party account decides whether reviewers can watch it.
_MEDIA_DIR = _WEB_DIR / "media"


# The interactive dashboard tour (matted screenshots) — served + its WebP images, at /dashboard-tour/.
_TOUR_DIR = _WEB_DIR / "tour"


# Third-party front-end libraries, vendored rather than pulled from a CDN at page load. The
# dashboard is a single hand-written Vue file with no bundler, so Vue arrives as a plain <script>
# — and while that script came from unpkg.com, any network that cannot reach unpkg rendered the
# signed-in dashboard as a blank page (issue #137: a mainland-China visitor, ERR_CONNECTION_CLOSED,
# then `Vue is not defined`). Serving it ourselves means the dashboard depends on exactly one
# origin: whoever served the page can serve its runtime. It also closes the supply-chain hole in
# the old floating `vue@3` tag, which let whatever npm published next run in an authed session.
# Filenames carry their version, so a bump is a visible one-line change and caches never collide.
_VENDOR_DIR = _WEB_DIR / "vendor"
