# Build plan — the outbound cluster on treg.to

Scope: the five drafts in this folder, the X distribution, the integrations, and the measurement loop. Ordered by dependency; each step names its file, its test, and the decision it needs.

## 0. Decisions before code (Jason)

| # | Decision | Default if unanswered |
|---|---|---|
| D1 | **Receipt policy.** (a) aggregate receipt only — counts + cost, no CSV, no per-row; (b) current spec — aggregate + anonymised row-outcome CSV; (c) no receipts — publish as prompt + live-price step table, use-case style | (a). Honest, no person data, still a run behind it |
| D2 | **Page type.** Port into `agent_pages.WORKFLOWS` (gets the hub, `.md` twin, HowTo schema, tests for free) vs a new "recipe" type | WORKFLOWS. Don't add a fourth generator |
| D3 | **Slugs.** `/workflows/rebuild-clay` etc. vs job-phrased slugs (`/workflows/find-and-verify-a-lead-list` style) | Job-phrased slug, "Rebuild Clay" in the H1/title. Slugs are forever; brand names aren't |
| D4 | Sponsor the Daily Dose of DS slot (budget) | Ask for the rate; decide after step 5 ships |

## 1. Branch hygiene (30 min)

- This worktree is 245 commits behind `origin/main`; `/workflows`, `/tools`, `/agents`, `/pricing` exist only on main. **Rebase `seo/pages-2026-08-24` onto main** before touching `agent_pages.py`.
- Commit the untracked research + drafts as docs (`marketing/`), separate from code.
- Run `bash .agents/skills/tools-registry-context/scripts/drift.sh` at the end of every code step; `docs/context/interface/seo.md` is the fragment that moves.

## 2. Catalog prerequisites (1–2 days, mostly waiting)

| Item | Why | Where |
|---|---|---|
| Fix `_observed_cost_micro` for findymail / fiber | drafts say "a miss is free"; the ledger bills the miss | `src/treg/…` per memory note; money code — ledger rules apply |
| `catalog_request`: website → markdown | 3 of 8 systems have the step; the AI SDR and Clay pages state it as a gap until it lands | catalog |
| `catalog_request`: LinkedIn post *likers* and keyword-level engagement search | the Gojiberry page's two stated gaps | catalog |
| Confirm response paths | `.data.email`, `.contact.email`, `.items[0]`, `.comments[].linkedinUrl` — read from `catalog get` examples, not yet exercised | one call each |

None of these block writing; they change what the pages are allowed to claim.

## 3. The runs (½ day each, 5 pages)

For each draft: run the script on a small real input, record the aggregate receipt per D1, keep the CSV local. Order = expected search value:

1. Rebuild Clay — 50 contacts through the waterfall. Receipt: found per rung, verified, total.
2. Build your own AI SDR — one week of SDR postings → VP-level contacts → verified emails. (Reuses 1.)
3. The join problem — 7 days of raises ≥ $5M → revenue leader → profile. (Reuses 1.)
4. Rebuild Gojiberry — three competitor posts → commenters → ICP fit → raise check.
5. GTM stack — no run; the price table is generated from the catalog at build time.

Budget: under $10 total at catalog prices. Every run's receipt line goes into the page's `run` block with a date.

## 4. Port to `WORKFLOWS` (1 day)

- One dict per slug in `agent_pages.WORKFLOWS`: `sentence`, `title`, `lede`, `prompt`, `prompt_why` (4), `steps` as `(name, capability, ask, endpoint, why)`, `once`, `run`.
- Capabilities must exist (`test_every_workflow_step_capability_and_endpoint_exist`); every step links to its use-case page via `USE_CASES` by capability or falls back to the agent-page anchor.
- **Strip every em-dash** — `test_workflow_copy_has_no_em_dashes` will fail the drafts as written. Also the brand rule: treg.to, never bare "treg" in copy.
- No routing claims anywhere; "the script picks the order" sentence stays.
- The GTM-stack page is not a workflow (no run). Ship it as a `/resources` entry or the intro of the `/workflows` hub — decide in D2's spirit: no new page type.

## 5. Wiring (½ day)

| From | To | Mechanism |
|---|---|---|
| `/workflows` hub | 4 new + 1 existing | already spreads from `WORKFLOWS`; sitemap and `.md` twins follow |
| 5 outcome pages (`marketing/landing/0*.md` → `usecase-*.html`) | the workflow that extends each | one "Run the full sequence" link block; rebuild with `marketing/landing/build.py` |
| 38 job pages | the workflows that use their capability | reverse index: for each capability in a workflow's steps, add a "Used in" line on the job page — generated, not hand-written |
| `/tools/<provider>` | workflows that call that provider | same reverse index by endpoint |
| `/agents/*` | `/workflows` | one line in the install block |
| `llms.txt`, `skill.md` | `/workflows` | one line each; these are the front door |
| `docs/context/interface/seo.md` | the new pages | drift.sh will flag it |

Tests: `tests/test_seo.py` walks every sitemap entry for 200; add the four workflow slugs to the served-with-crawler-essentials test.

## 6. Distribution (rolling, starts when page 1 is live)

| Week | X (Jason's account) | Elsewhere |
|---|---|---|
| Page 1 live | The join-problem X Article + hook tweet with the receipt screenshot | Reddit: the r/gtmengineering Clay-alternative threads, script not link (weekly loop, u/jzdesign rules) |
| +1 | Price-receipt image #1: email verification, 3 providers | Open the YALC PR (treg provider adapter); list in `gtmagents/gtm-agents` |
| +2 | "Clay's moat in 40 lines" | Fork `LeadMagic/gtm-skills` `prospecting-stack` to `/call/`; offer upstream |
| +3 | Price-receipt #2: email finders, 6 providers | DDoDS brief sent (D4) |
| weekly after | one receipt image, replies via the @treg_ai loop ("when unsure, skip") | — |

Format rules from the teardown: number and receipt in the first line, product named once at the end, "try free" as a self-reply, write for the bookmark.

## 7. Measurement (from day 1)

- Search Console impressions/clicks on `/workflows/*` and the linked job pages, weekly, via the existing GSC + Ads loop (own-key, free).
- First-call rate from those pages — still uninstrumented (memory: the funnel gap). Minimum: a `?src=workflow-<slug>` on the install CTA and a count in the audit log.
- Review at day 30: keep the shelf if any page has impressions on its primary term; otherwise fold the copy into the job pages and stop adding workflows.

## Sequence, compressed

Decisions → rebase → runs 1–4 (parallel with catalog fixes) → port + tests → wiring + docs → publish one, then one a week → distribution cadence → day-30 review.
