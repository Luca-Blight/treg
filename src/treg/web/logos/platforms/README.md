# Platform logos

One file per **platform slug** in the endpoint catalog (`/catalog/platforms` → `slug`), resolved by
convention as `/logos/platforms/<slug>.svg` — the same no-registry rule the provider logos in the
parent directory follow, one directory up the taxonomy.

The marketplace's platform tiles wear these. A platform with no file here is **not** a bug: the
dashboard falls back to a generated initial tile whose colour is a hash of the slug (`platTileBg`
in `index.html`), so the grid stays complete while the drawn set grows.

Rules for adding one:

- **24×24 viewBox**, no fixed width/height — the tile scales it.
- **Simple geometric marks only.** These are nominative identifications of third-party platforms,
  not reproductions of their artwork: a recognisable silhouette, a wordmark character, or a
  brand-coloured lettermark. Do not paste in complex official artwork.
- **Draw for a light surface.** Every mark renders inside the same near-white `.pt-logo` tile in
  both themes, for the reason the parent README gives: marks are designed against white, ink
  coverage varies enormously, and monochrome marks vanish on one of our two themes if left bare.
- CJK lettermarks (`小`, `微`, `知`, …) all share one shape: a `rx="5.5"` rounded square in the
  brand colour with a single 13px bold character centred on it. Copy an existing one rather than
  redrawing, so the set stays optically even.

## Where the marks come from

The drawn set was refreshed in Aug 2026 against two unified sources, in preference order:

1. **Simple Icons** (CC0 path data, `simple-icons` npm package) — the official monochrome path in
   the brand's published colour, emitted directly. Marks whose brand colour is too light for the
   near-white tile (Snapchat, IMDb, Kick, …) instead wear the brand-colour `rx="5.5"` tile with the
   glyph in black — which is how those brands render themselves on light surfaces.
2. **The brand's own published vector** (Wikimedia Commons originals, product-icon CDNs) for marks
   Simple Icons does not carry (Bing, Yahoo, Yandex, Google Business Profile, Seznam, Youku,
   Toutiao, Truth Social, the multicolour Google Play triangle) — gradients flattened to the
   dominant flat colours, geometry kept verbatim.

Obscure CJK platforms with no published simple mark keep the lettermark convention above.
