// First-party ad-click capture. No Google script, no third-party request: this reads the click id
// off our own URL and stores it in our own cookie, which the signup POST then carries to the server.
// gbraid/wbraid are what Google substitutes for gclid on iOS traffic — omitting them silently drops
// a large share of mobile conversions.
(function () {
  try {
    var q = new URLSearchParams(window.location.search);
    var id = q.get('gclid') || q.get('gbraid') || q.get('wbraid');
    if (!id) return;
    // The use-case pages set data-page on <body> (their own ev() already reads it) — more reliable
    // than any query parameter, since a visitor can navigate on-site before the query string is
    // available. utm_content is what _measurement.md specifies; ref is the use-case pages' own CTA
    // convention (?ref=p1, see index.html's logged-out redirect). Fall back through both so
    // attribution does not come back empty on whichever convention a given page happens to use —
    // the homepage has no data-page.
    var landing = (document.body && document.body.dataset && document.body.dataset.page)
               || q.get('utm_content') || q.get('ref') || '';
    // 90 days: Google's click-through conversion window. Lax so it survives the top-level
    // navigation from the ad, which is a cross-site GET.
    var v = encodeURIComponent(id + '|' + landing);
    document.cookie = 'treg_ad=' + v + ';path=/;max-age=7776000;samesite=lax' +
      (window.location.protocol === 'https:' ? ';secure' : '');
  } catch (e) { /* never break the page for a marketing cookie */ }
})();
