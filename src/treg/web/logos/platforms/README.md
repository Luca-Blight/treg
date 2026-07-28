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
