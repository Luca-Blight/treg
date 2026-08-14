# The Claude Code plugin, and where it gets listed

treg's Codex/ChatGPT plugin is built around the **MCP connector**, and that listing sits in a review
queue. The skill does not need MCP: `src/treg/web/skill.md` already teaches an agent to install the
CLI, sign in, and call the catalog. So it also ships as a **skills-only Claude Code plugin**, served
from this repo as its own marketplace — live the moment it merges, with no reviewer in the loop.

## Install

```
/plugin marketplace add superdesigndev/treg
/plugin install treg@treg
```

The skill then loads as `treg:treg`. Nothing else is required: the plugin declares **no MCP server**,
so there is no token to set before it works. Its bootstrap tells the agent to run
`curl -fsSL https://treg.to/install.sh | sh` if `treg --version` fails, and a new team starts with
$1.00 of free balance.

## What is where

```
.claude-plugin/plugin.json        the manifest Claude Code reads
.claude-plugin/marketplace.json   makes this repo its own single-plugin marketplace
skills/treg/SKILL.md              GENERATED — never edit by hand
skills.sh.json                    category grouping for skills.sh / Hermes' Skills Hub
```

`source: "./"` in `marketplace.json` makes the **plugin root the repo root**, which is why
`skills/treg/` is at the top level rather than under `plugin/`. That one path is also what
`npx skills add` resolves and what `clawhub skill publish` takes — one generated tree, four channels.

A consequence worth remembering: anything Claude Code auto-discovers at the repo root ships to users.
Only `skills/` is intended; a root-level `commands/`, `agents/`, or `hooks/` directory would be
published too. `tests/test_plugin.py` asserts none appears.

**Never edit `skills/treg/SKILL.md`.** Change `src/treg/web/skill.md` and regenerate:

```bash
python3 scripts/build_plugin.py            # regenerate BOTH plugins
python3 scripts/build_plugin.py --check    # fail if either is stale (also a test)
```

## Listing runbook

Ordered so that everything self-serve ships before anything with a queue.

### 1. Own marketplace — live on merge

Nothing to submit. Merging to `main` publishes it; the install lines above start working.

Bump `version` in `pyproject.toml` and both manifests together — `test_plugin_version_tracks_the_package`
pins all of them to one value.

### 2. ClawHub (clawhub.ai) — self-serve, CLI-based

`https://clawhub.ai/submit` **404s** (Hermes' CLI still points there; its `--to clawhub` is a stub).
The live path is the npm CLI:

```bash
npm i -g clawhub
clawhub login                                   # GitHub account must be ≥1 week old
clawhub skill publish ./skills/treg --dry-run   # always dry-run first
clawhub skill publish ./skills/treg --slug treg --name "treg" \
  --version 0.11.0 --changelog "..." --tags latest
```

Requirements the generated skill already satisfies: frontmatter `name` + `description` + semver
`version`, with `name` matching the parent directory (`treg`). A security scan runs post-publish;
flagged releases are hidden from the public catalog but stay in your dashboard.

⚠️ **ClawHub forces MIT-0 on every skill, with no per-skill override.** This repo is Apache-2.0 plus
a hosted-service restriction. `SKILL.md` is prose — it documents a hosted service rather than
implementing one — so licensing that one file permissively is consistent with wanting it copied
everywhere. It is a deliberate choice, not an oversight; do not let it drift into a claim about the
server code.

Listing here also feeds Hermes' Skills Hub automatically. Note Hermes hard-codes every ClawHub skill
to `community` trust after the Feb 2026 "ClawHavoc" incident (341 malicious skills), so this is a
discovery channel, not a trust signal.

### 3. aiskillstore/marketplace + skills.sh

`aiskillstore/marketplace` is one of only two repos in Hermes' `KNOWN_MARKETPLACES`, so a PR there is
the cheapest route into Hermes' `claude-marketplace` source. For skills.sh, verify on a scratch
machine that `npx skills add superdesigndev/treg` resolves `skills/treg/` — that one command covers
70+ coding agents beyond Claude Code.

### 4. anthropics/claude-plugins-official — the queue

Submit at <https://claude.ai/settings/plugins/submit>. Highest-trust placement, and it also feeds
Hermes. It is a review queue — the same "takes too long" risk as the MCP track — which is exactly why
it is last: everything above is already shipping by the time it is filed.

Entries in that directory use `git-subdir` with a pinned `ref` **and** `sha`, so being inside this
monorepo is not a blocker. **Tag a release and give them the tag.**

## The fourth door: treg.to itself

`GET /.well-known/skills/index.json` + `/.well-known/skills/treg/SKILL.md` (in `src/treg/api.py`)
make this host a first-class skill source under the agentskills.io convention — no registry, no
review, no third party. Hermes reads it directly. Because it goes through the same `_serve_md` as
`/skill.md`, `{BASE}` is templated to the **serving** host, so a self-hosted registry advertises
itself rather than treg.to.

## Before you submit anywhere

- `uv run --frozen python -m pytest tests/test_plugin.py tests/test_skill_md.py -q`
- `claude plugin validate .` — validates the marketplace manifest the way Claude Code itself parses
  it, which catches shape errors the JSON schema does not.
- Install it for real from a scratch clone, because markup that reads correctly still breaks:

  ```bash
  git clone --depth 1 https://github.com/superdesigndev/treg /tmp/treg-check
  claude plugin marketplace add /tmp/treg-check
  claude plugin install treg@treg
  claude plugin details treg@treg     # then: uninstall + marketplace remove
  ```

  `details` is the real check. It must report **Skills (1) treg** and **MCP servers (0)** — a zero
  skill count is the silent failure this whole layout risks, and any non-zero agents/hooks count
  means something at the repo root leaked into the plugin.
- `claude plugin tag` creates the `treg--v{version}` release tag and validates that `plugin.json`
  and the enclosing marketplace entry agree — worth running before handing a tag to Anthropic.
- Confirm the copy still says treg **compares** providers and the caller chooses. treg does not route
  and does not fail over; the landing page had to be corrected for that claim once already, and a
  store listing is much harder to correct. `test_the_listing_does_not_promise_routing` guards it.
- Confirm the one-line positioning is identical in all three places (`test_one_product_one_sentence`).
