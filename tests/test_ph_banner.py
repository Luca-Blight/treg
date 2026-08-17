"""The Product Hunt launch banner.

LAUNCH-SCOPED: delete this file together with the `.phb` blocks in landing.html and index.html.
It exists because the banner has two failure modes that are invisible in a browser tab:

* `/app` serves index.html with **no** template substitution, so a `{BASE}`-style placeholder in
  that half would ship literally to every user;
* `/catalog` and `/catalog/<slug>` are served from index.html too, so the "landing + app only"
  scope holds only as long as the `!publicCatalog` gate is on the element.
"""

from __future__ import annotations

from pathlib import Path

from httpx import AsyncClient

from treg import api


PH_URL = "https://www.producthunt.com/products/treg-openrouter-for-tools?launch=treg"
# The $5-credit claim form. The reward is fulfilled by hand (ledger.grant), so a banner promising it
# without a working way to claim is the one failure that costs goodwill rather than clicks.
FORM_URL = (
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSehMyOXRxHXpJ0eUSMogJkQklviaag5rvnquo8aEciEgpPm1g/viewform"
)

_WEB = Path(api.__file__).parent / "web"
LANDING = (_WEB / "landing.html").read_text(encoding="utf-8")
INDEX = (_WEB / "index.html").read_text(encoding="utf-8")


async def test_the_landing_links_to_the_launch(clients: AsyncClient):
    r = await clients.get("/")
    assert r.status_code == 200, r.text
    assert PH_URL in r.text
    assert "{BASE}" not in r.text


def test_the_app_hardcodes_the_url_because_nothing_substitutes_index_html():
    """`dashboard()` returns index.html as a plain FileResponse — a placeholder here never expands."""
    assert PH_URL in INDEX
    assert "{BASE}" not in INDEX.split('class="phb"', 1)[1][:1200]


def test_the_app_banner_is_gated_off_the_public_catalog():
    """/catalog renders from index.html; the gate is the only thing keeping the promo off the
    crawlable catalog pages."""
    assert '<div class="phb" v-if="ph && !publicCatalog">' in INDEX
    assert "dismissPh(" in INDEX


async def test_the_catalog_prerender_carries_no_banner(clients: AsyncClient):
    r = await clients.get("/catalog")
    assert r.status_code == 200, r.text
    prerender = r.text.split('<div id="prerender"', 1)
    assert len(prerender) == 2, "no #prerender block — the catalog page changed shape"
    assert PH_URL not in prerender[1].split('<div id="app"', 1)[0]


def test_the_offer_and_the_way_to_claim_it_travel_together():
    """Whichever half a visitor sees, "we'll add $5" and the form that collects it are on the same
    row — an offer with no claim link is a promise the page can't keep."""
    for name, html in (("landing.html", LANDING), ("index.html", INDEX)):
        assert "$5" in html, name
        assert FORM_URL in html, name
        strip = html.split('class="phb"', 1)[1][:1800]
        assert PH_URL in strip and FORM_URL in strip, f"{name}: claim link is outside the banner"


def test_the_claim_form_url_carries_no_share_dialog_param():
    """`?usp=dialog` comes from Google's share sheet. Harmless, but it is not part of the address."""
    assert "usp=dialog" not in LANDING
    assert "usp=dialog" not in INDEX


def test_the_sticky_strip_offsets_every_layer_of_chrome_beneath_it():
    """The strip sticks, so each sticky offset below it has to add the strip's height. Missing one
    means that layer renders *underneath* the banner — the failure is invisible until you scroll."""
    assert "position:sticky;top:0;z-index:40" in INDEX          # the strip itself
    assert ".top{top:var(--phb-h)}" in INDEX
    assert ".side{top:calc(57px + var(--phb-h));height:calc(100vh - 57px - var(--phb-h))}" in INDEX
    assert "--lbar-top:calc(57px + var(--phb-h));--lsec-top:calc(107px + var(--phb-h))" in INDEX
    assert ".lp-nav{top:var(--phb-h)}" in INDEX


def test_the_offset_overrides_come_after_the_values_they_override():
    """Equal specificity, so source order decides. Declared before `:root{--lbar-top:57px}` (§3.7)
    or before `.side`'s own rule, the overrides silently lose and the offsets go back to ignoring
    the strip."""
    base_vars = INDEX.index(":root{--lbar-top:57px;--lsec-top:107px}")
    base_side = INDEX.index(".side{position:sticky;top:57px")
    override = INDEX.index("--lbar-top:calc(57px + var(--phb-h))")
    assert override > base_vars, "the --lbar-top/--lsec-top override precedes what it overrides"
    assert INDEX.index(".side{top:calc(57px + var(--phb-h))") > base_side


def test_the_height_is_measured_from_a_fresh_lookup():
    """The strip is shorter on mobile, so the height is measured — and the measuring callback must
    re-query `.phb`. Closing over the element made a reused ResizeObserver measure the DETACHED node
    after v-if swapped it, resetting --phb-h to 0 right after the right value was written."""
    fn = INDEX.split("function phbApplyHeight()", 1)
    assert len(fn) == 2, "phbApplyHeight is gone — did the height sync get rewritten?"
    body = fn[1][: fn[1].index("}")]
    assert "document.querySelector('.phb')" in body
    assert "new ResizeObserver(phbApplyHeight)" in INDEX     # the observer gets the re-querying fn
    assert "MutationObserver(()=>phbSyncHeight())" in INDEX  # presence changes, not a Vue hook guess


def test_both_halves_share_one_dismissal_key():
    """Dismissing on the landing must not leave it showing in the app."""
    assert "'treg-ph'" in LANDING
    assert "'treg-ph'" in INDEX
