---
title: The shippable tools-registry skill (3 personas)
status: shipped
sources:
  - src/treg/web/skill.md
related:
  - interface/cli.md
  - interface/api.md
---

# The `tools-registry` skill

`src/treg/web/skill.md` is the **product** skill that ships to consumers — the agent's whole interface to the
registry (distinct from `.claude/skills/tools-registry-context/`, which maintains *these* design docs).
Its frontmatter `name: tools-registry` + `description` make it loadable by a coding agent.

One skill, three personas:
- **consumer** — discover + call tools with no credentials locally. Teaches the agent-native
  **URL-passthrough** first: take the real upstream URL and prefix it with `{BASE}/call/`
  + the `X-Treg-Token` header; `treg call <tool> <path>` is the CLI shorthand.
- **creator** — turn a local skill into a shared tool: `treg secret add`, `treg tool add` (single-key or
  `--bind` multi-credential), the `treg skill scaffold → push` bundle flow, and `treg oauth connect` for
  browser-consent tokens. Documents the two OAuth modes (auto-refresh vs manual) and the four auth shapes.
- **admin** — inventory + monitor: `treg tool/secret/skill ls`, `treg calls`, and `treg health [--run]`
  (with the per-tool `health_check` probe).

**Distribution:** the file is `{BASE}`-templated and served at **`GET /skill.md`** (`skill_md` in
`api.py`, via `_serve_md`), and `install.sh` best-effort drops it into
`~/.claude/skills/treg/SKILL.md` right after installing the CLI — so `curl {BASE}/install.sh | sh`
gives a machine both the `treg` command AND the skill that teaches an agent to use it. It restates the
invariants (secrets are write-only, use-without-hold, the proxy relays the upstream's truth) and links
`{BASE}/llms.txt` + `{BASE}/tutorial`. It mirrors the surfaces in [api.md](api.md) + [cli.md](cli.md);
keep the three in sync when the API/CLI change.

## `/integrate.md` — the BUILDER skill

A second, separate skill for the other side of the relationship. `skill.md` teaches an agent to **use**
treg; `integrate.md` is pasted into a builder's own repo and pointed at their coding agent so they can
**embed** treg and bill their own customers for it.

It leads with the per-customer billing model rather than the call syntax, deliberately: tagging has to
happen at the one place your backend already sets `Authorization`, and a builder who writes the
plumbing first writes it in a shape that has to be torn out.

The things it insists on, each because getting them wrong is expensive and silent:

- **Tag from the backend, never as a model-supplied argument** — a model omits it mid-chain and the
  spend leaves the invoice.
- **Invoice from `usage/by-tag` (the ledger), never from `/calls`** — audit rows are shed under load.
- **Assert `attributed + unattributed == total`**, and drive `unattributed_micro` to zero; anything
  left is a call site that forgot to tag.
- **Branch on `X-Treg-Error`, not the status code** — a provider's own 4xx is relayed verbatim.
- **Never forward a team-level 402/429 to an end user** — those carry the builder's balance and a
  top-up link. The tag-scoped refusals are safe to surface and carry nothing about the team.
- **Per-customer caps are advisory**, so they must not be sold to end users as exact.

`tests/test_tag_billing.py` pins the header and route names the skill teaches, so a rename that would
silently turn it into wrong instructions fails the suite instead.
