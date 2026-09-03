# ArcTerm Session Music — required fix and republish

Handoff from the Jazz session (Mac Studio, 2026-08-30). Two bugs were found in the
generative.fm player harness. **6 of the 8 published music packs are broken for every
ArcTerm user**: they play the first notes, then go silent forever.

## Bug 1 — the missing random helper (the important one)

The pieces from `pieces-alex-bainter` call a global the website normally provides:

```js
window.generativeMusic.rng()
```

Our pack harness (`tools/composer/ref-pieces/test-player/src/app.js`) never defines it.
The first time a piece asks for a random number, the callback throws, its scheduling
chain dies, and the music stops after the intro notes.

Affected shipped packs (count of `generativeMusic` uses in each piece):
aisatsana (1), meditation (3), above-the-rain (5), drones-2 (7), little-bells (5),
pinwheels (1). Not affected: skyline (0), eno-machine (0).

### Fix

Add this at the very top of `tools/composer/ref-pieces/test-player/src/app.js`
(before any import):

```js
// The generative.fm site injects this global; pieces depend on it.
// Optional seed makes a performance repeatable (site-identical algorithm, MIT).
(function () {
  const q = new URLSearchParams(location.search);
  const s = q.get('seed');
  function xmur3(str) {
    let h = 1779033703 ^ str.length;
    for (let i = 0; i < str.length; i++) {
      h = Math.imul(h ^ str.charCodeAt(i), 3432918353);
      h = (h << 13) | (h >>> 19);
    }
    return () => {
      h = Math.imul(h ^ (h >>> 16), 2246822507);
      h = Math.imul(h ^ (h >>> 13), 3266489909);
      return (h ^= h >>> 16) >>> 0;
    };
  }
  function sfc32(a, b, c, d) {
    return () => {
      a |= 0; b |= 0; c |= 0; d |= 0;
      let t = (((a + b) | 0) + d) | 0;
      d = (d + 1) | 0;
      a = b ^ (b >>> 9);
      b = (c + (c << 3)) | 0;
      c = (c << 21) | (c >>> 11);
      c = (c + t) | 0;
      return (t >>> 0) / 4294967296;
    };
  }
  let rng = Math.random;
  if (s) { const g = xmur3(s); rng = sfc32(g(), g(), g(), g()); }
  window.generativeMusic = window.generativeMusic || { rng, seed: s || null };
})();
```

## Bug 2 — scheduling starts before play (check whether app.js has it)

In the gallery harness (`src/index.js`) the piece's `schedule()` was called right after
activation, before any play command. Result: a few notes sound at once, the chain never
continues (the transport is not started), and the play button state lies.

The fix applied to `src/index.js`: `schedule()` runs only inside the play action,
followed by `Tone.Transport.start()`. Stop calls `Transport.stop()`, `Transport.cancel()`,
then the dispose function, and clears it.

**Check `src/app.js`**: if its `__arc.play` path calls `schedule()` at activation time
instead of on the play command, apply the same change. If `app.js` already schedules
only on play, leave it.

## Where the corrected reference lives

The Jazz copy on the Mac Studio has both fixes working and verified by 80+ seconds of
continuous playback (agua-ravine):

- `~/devs/jazz/composer/ref-pieces/test-player/src/index.js` (gallery shim, fixed)
- `~/devs/jazz/composer/ref-pieces/test-player/dist-pieces/*/index.html` (seeded shim injected)

The ArcTerm repo copy on the MacBook (`arcterm-private/tools/composer/`) does NOT have
these fixes yet.

## What to do, in order

1. Apply Bug 1's shim to `tools/composer/ref-pieces/test-player/src/app.js`.
2. Check and, if needed, fix Bug 2 in `app.js`.
3. Rebuild all packs: `python3 tools/composer/build-music-packs.py`.
   Bump `version` to `3` for every entry in the `PIECES` dict first —
   the app re-downloads a pack only when the version number rises.
4. Copy `music-dist/*` into the gh-pages `music/` folder and push.
5. Verify: in ArcTerm, delete a broken pack (e.g. aisatsana), re-download it,
   and let it play for at least 3 minutes. Before the fix it dies within ~30 seconds;
   after the fix it must keep developing.
6. Also verify one unaffected pack (skyline) still behaves the same as before.

## Optional, later

- The seeded rng means a pack can ship a fixed, hand-picked performance
  (`?seed=name` on the player URL). Not used yet; the bridge could pass it.
- With the fix in place, the whole 57-piece catalog is a candidate for publishing,
  not only the current 8. Decide separately; sizes are the constraint (GitHub Pages ~1GB).
