#!/usr/bin/env python3
"""Generate every plugin's SKILL.md from the one source: `src/treg/web/skill.md`.

Two shop windows, two variants, one source:

- **codex**  -> `plugin/skills/treg/SKILL.md` — ships an MCP connector, so its bootstrap points at
  the five tools and tells the reader NOT to reach for a terminal.
- **claude** -> `skills/treg/SKILL.md` — skills-only by design (nothing about it waits on a
  directory review), so its bootstrap says the opposite: there are no tools, install the CLI. It
  also gets a `version:` stamped into its frontmatter, which is what ClawHub requires and Claude
  Code ignores.

The Claude variant sits at the REPO ROOT rather than under `plugin/` because that one path is
simultaneously what Claude Code's plugin loader auto-discovers, what `npx skills add` resolves, and
what `clawhub skill publish` takes — one generated tree, four distribution channels.


The plugin is a shop window — a listing in the directory ChatGPT and Codex share — and what it ships
is the SAME skill `treg skill bootstrap` already writes into `~/.codex/skills/`. Copying that file by
hand would be a second source of truth for the product's most-read page, and it would rot: the served
copy changes whenever the product does, and nothing would notice the plugin drifting behind it. So it
is generated, and `tests/test_plugin.py` fails if the checked-in copy is stale.

Two transformations, and each exists for a reason the served file does not have:

1. `{BASE}` is a placeholder the SERVER substitutes per request (`api.py` `/skill.md`). Nothing
   substitutes it inside an installed plugin, so a raw copy would ship the literal string `{BASE}`
   and every URL in it would be broken. It is baked to the public deployment here.

2. A short section is prepended mapping the skill onto the plugin's MCP tools. The served skill is
   written around the `treg` command line, because that is how every other install path reaches
   treg. A plugin user has no terminal and no CLI — they have the connector's tools. Without this
   the first run is an agent dutifully trying to run a shell command that does not exist, which is
   the listing's whole conversion funnel spent on an error message. The rest of the page still
   carries what matters: when treg is the right move, and how to choose between providers.

Usage:  python3 scripts/build_plugin.py [--check]
        --check exits 1 if the generated file differs from what is checked in (used by the test).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "treg" / "web" / "skill.md"
PUBLIC_BASE = "https://treg.to"

CODEX_TARGET = ROOT / "plugin" / "skills" / "treg" / "SKILL.md"
CLAUDE_TARGET = ROOT / "skills" / "treg" / "SKILL.md"

CODEX_BOOTSTRAP = """
## You already have treg — use the tools, not the terminal

This plugin ships a **connector**, so treg is available to you as tools right now. Nothing to
install:

| tool | use it for |
|---|---|
| `catalog_search` | find an endpoint by WHAT YOU WANT TO DO — "work email", "backlinks", "tiktok comments" |
| `catalog_get` | one endpoint's parameters and its exact price, **before** you spend |
| `call` | make the call; treg injects the credential and relays the answer |
| `balance` | the team's prepaid balance |
| `my_tools` | what this team registered and you can call without holding the key |

The rest of this page explains **when** treg is the right move and **how to choose** between
providers. It is written around the `treg` command line, which a human uses for the same jobs — read
`treg catalog search` as `catalog_search`, `treg call` as `call`, and so on.

**If the tools are not there**, the connector has no token yet: the human sets `TREG_TOKEN` (from
{BASE} → sign in → copy token) for this plugin. A new team starts with **$1.00 of free balance**,
so there is nothing to pay before the first call. Say that plainly and stop — do not ask them for a
provider's API key, which is the thing treg exists to avoid.

---
"""

# The Claude Code plugin ships NO connector — it is skills-only, deliberately, so that nothing about
# it waits on a directory review. That inverts the Codex bootstrap: there are no tools, only the CLI,
# and the CLI may not exist yet. The served skill already carries a full "First: install + sign in"
# section, so this stays short — it exists to stop a first run dying on `command not found` before
# the reader ever gets that far, and to pre-empt the one failure that wastes the human's money or
# their patience (asking them for a provider key treg exists to avoid).
CLAUDE_BOOTSTRAP = """
## You have treg as a skill — everything below runs through the `treg` CLI

This plugin ships the skill, not a connector, so there are no treg tools in your tool list. Every
command on this page is a real shell command. **If `treg --version` fails, install it first:**

```bash
curl -fsSL {BASE}/install.sh | sh   # installs the CLI + points it here
treg login                                   # browser sign-in; first login registers you
```

A new team starts with **$1.00 of free balance**, so there is nothing to pay before the first call.
If sign-in is needed, say so plainly and stop — do not ask the human for a provider's API key, which
is the thing treg exists to avoid.

---
"""

# variant -> (target, bootstrap, stamp_version). One source, two shop windows.
VARIANTS = {
    "codex": (CODEX_TARGET, CODEX_BOOTSTRAP, False),
    "claude": (CLAUDE_TARGET, CLAUDE_BOOTSTRAP, True),
}


def package_version() -> str:
    """The one version, read from pyproject — the same value `tests/test_plugin.py` pins both
    manifests to. Read rather than duplicated so a release bump cannot leave the skill behind."""
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split('"')[1]
    raise SystemExit("pyproject.toml has no `version = \"…\"` line")


def render(variant: str) -> str:
    target, bootstrap, stamp_version = VARIANTS[variant]
    text = SOURCE.read_text(encoding="utf-8")
    if "---" not in text:
        raise SystemExit(f"{SOURCE} has no frontmatter — refusing to guess where it ends")
    # Keep the frontmatter exactly as served (name + description drive discovery in BOTH surfaces),
    # and insert the bootstrap immediately after it, before the skill's own opening.
    _, fm, body = text.split("---", 2)
    if stamp_version:
        # ClawHub REQUIRES a semver `version` in frontmatter (and requires `name` to match the
        # parent directory, which `treg` already does). Claude Code ignores the extra key, so
        # stamping it here is what lets one generated file satisfy both registries.
        fm = f"{fm.rstrip()}\nversion: {package_version()}\n"
    out = f"---{fm}---\n{bootstrap}\n{body.lstrip(chr(10))}"
    return out.replace("{BASE}", PUBLIC_BASE)


def main() -> int:
    check = "--check" in sys.argv
    stale = False

    for variant, (target, _, _) in VARIANTS.items():
        generated = render(variant)
        current = target.read_text(encoding="utf-8") if target.exists() else None
        rel = target.relative_to(ROOT)

        if check:
            if current == generated:
                print(f"OK — {rel} matches {SOURCE.relative_to(ROOT)}")
            else:
                print(f"STALE — {rel} does not match {SOURCE.relative_to(ROOT)}",
                      file=sys.stderr)
                stale = True
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated, encoding="utf-8")
        print(f"wrote {rel}  ({len(generated.splitlines())} lines)")

    if stale:
        print("  regenerate with: python3 scripts/build_plugin.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
