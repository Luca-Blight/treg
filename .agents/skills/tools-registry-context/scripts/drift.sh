#!/usr/bin/env bash
# drift.sh [git-range] — map changed source files to the fragment(s) that document them.
# Project-agnostic: source pathspecs + extensions come from fragments.config. Reads the generated MAP.md.
# Default range: origin/main..HEAD (everything unpushed).
set -euo pipefail

RANGE="${1:-origin/main..HEAD}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="$(cd "$SKILL_DIR/../../.." && pwd)"
MAP="$SKILL_DIR/MAP.md"
CONFIG="$SKILL_DIR/fragments.config"

[ -f "$MAP" ] || { echo "MAP.md not found — run build-map.py first" >&2; exit 1; }

# Pull source pathspecs + optional extension filter from the JSON config (python3 is a dependency).
globs=$(python3 -c "import json;print(' '.join(json.load(open('$CONFIG')).get('source_globs',[])))" 2>/dev/null || true)
extre=$(python3 -c "import json;e=json.load(open('$CONFIG')).get('source_exts',[]);print('\\.('+'|'.join(e)+')\$' if e else '')" 2>/dev/null || true)

cd "$ROOT"

# An EMPTY RANGE and a CLEAN RANGE are different answers, and conflating them is how drift ships.
# The default range is origin/main..HEAD, so running this while standing ON main (typically right
# after a merge, which is exactly when someone thinks to check) diffs a branch against itself: zero
# commits, zero changed files, "no drift" — a pass that proves nothing. Four stale fragments reached
# production behind that reassurance. So: say when there is nothing to compare, and exit non-zero.
if git rev-parse --verify --quiet "${RANGE%%..*}" >/dev/null 2>&1; then
  commits=$(git rev-list --count "$RANGE" 2>/dev/null || echo 0)
  if [ "$commits" -eq 0 ]; then
    echo "⚠ NOTHING TO COMPARE — this is not a pass."
    echo ""
    echo "  '$RANGE' contains no commits: HEAD is at or behind the base, so there is no work to"
    echo "  check. On the default range that means you are standing on the base branch itself."
    echo ""
    echo "  Branch: $(git branch --show-current 2>/dev/null || echo 'detached')  HEAD: $(git log --oneline -1 --format=%h)"
    echo ""
    echo "  To check work that is already merged, name a range that contains it:"
    # Only a VERSION-shaped tag means "the last release" — this repo also carries evidence tags
    # (pr-evidence, …) and `git describe` would happily offer one of those as a release boundary.
    last_tag=$(git tag --list --sort=-v:refname 'v[0-9]*' '[0-9]*' 2>/dev/null | head -1)
    [ -n "$last_tag" ] && printf '    drift.sh %-22s # since the last release\n' "$last_tag..HEAD"
    printf '    drift.sh %-22s # the last 20 commits\n' "HEAD~20..HEAD"
    echo ""
    echo "  Run it BEFORE merging, from the branch, and the default range is the right one."
    exit 2
  fi
fi

changed=$(git diff --name-only "$RANGE" -- $globs 2>/dev/null || true)
[ -n "$extre" ] && changed=$(printf '%s\n' "$changed" | grep -E "$extre" || true)

if [ -z "$changed" ]; then
  echo "No documented-source changes in $RANGE ($(git rev-list --count "$RANGE" 2>/dev/null || echo '?') commit(s) examined)."
  exit 0
fi

echo "Changed sources in $RANGE → fragments to review:"
gap=()
while IFS= read -r f; do
  [ -z "$f" ] && continue
  line=$(grep -F "\`$f\`" "$MAP" | grep ' | ' | head -1 || true)
  if [ -n "$line" ]; then
    docs=$(printf '%s' "$line" | sed -E 's/^\| `[^`]+` \| (.*) \|$/\1/')
    printf '  %-48s → %s\n' "$f" "$docs"
  else
    gap+=("$f")
  fi
done <<< "$changed"

if [ ${#gap[@]} -gt 0 ]; then
  echo ""
  echo "⚠ changed sources with NO fragment (possible doc gap — fold in or write a new fragment):"
  printf '  %s\n' "${gap[@]}"
fi
