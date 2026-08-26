# The mock service — read this before trusting anything you see in the UI

This directory was written as a stand-in so T-10 could be built and tested
against the pinned contract while the Keystone HTTP API did not exist. **It now
exists** — T-5, T-7 and T-8 landed — which changes what this code is *for*: not
a placeholder for an unknown shape, but a stand-in that must match a known one.
Every remaining difference is therefore a divergence to be justified, and they
are enumerated below rather than left to be discovered.

## How to tell whether you are looking at mock data

Four independent ways, all of them cheap:

1. **The banner.** When the mock is live the app shows a permanent yellow
   banner at the top of every page saying so.
2. **The flag.** The mock is reached only when `VITE_USE_MOCK_API=1`.
   `pnpm dev`, `pnpm build` and `pnpm preview` do **not** set it, so they use
   the real HTTP client and talk to whatever `VITE_API_BASE_URL` names — the
   error state when nothing is listening there.
   `pnpm dev:mock`, `pnpm preview:mock` and the Playwright a11y run do set it.
3. **The bundle.** `pnpm build` does not contain this code at all — the
   selector in `src/lib/apiClient.ts` is a static `import.meta.env` comparison,
   so the dynamic `import('../mocks/mockClient')` is dead-code-eliminated.
   With the flag set the build emits a separate `mockClient-*.js` chunk.
   Compare the two builds and see for yourself.
4. **The imports.** Nothing outside `src/mocks/**` imports this module except
   that one dynamic import and the test harness.

## Where the data comes from

`scripts/build-mock-seed.mjs` copies the **real committed grading artifacts**
into `src/mocks/seed/`:

| seed file | source | what it carries |
| --- | --- | --- |
| `golden-conflicts.json` | `golden/conflicts.json` | all **3,050** golden entries — every one of the fourteen conflict types at its A.4 volume, with the real `entity_refs`, `sources_involved`, `disagreeing_fields` and `observed_values` |
| `golden-summary.json` | `golden/manifest-summary.json` | per-type counts, minimums, self-check results |
| `provenance.json` | — | the sha256 of each source file, so drift is detectable |

Regenerate with `pnpm mock:seed`. Never hand-edit the seed.

Fingerprints are **computed with the contract's own algorithm**
(`fingerprint.ts`, a faithful re-implementation of invariant-contract §2.5
`canon_value` and §5.4, unit-tested against the contract's own byte-for-byte
worked examples) rather than invented, so the ids in the UI are the system's.

The per-type fix target and its sensitivity classification come from
invariant-contract §6 — including the PINNED `fix_target` selector (partition by
comparison row; a wholly-sensitive row decides; ties break to the CRM side).
That is why every C4 and every C14 lands `sensitive_hold`, as the contract says
it must.

## What is invented

Marked `MOCK-ONLY` in `mockClient.ts`:

- **`confidence`** — the committed formula lives in `confidence.yaml` (T-7).
  The mock derives a stable pseudo-score from the fingerprint. **The numbers on
  screen are not the system's confidence scores.**
- **The proposal status mix** for non-sensitive proposals, and `decided_by` /
  `decided_at`. (The sensitive→`sensitive_hold` rule is *not* invented; it is
  the contract's.)
- **The rationale text.** T-8's LLM writes the real one.
- **Run ids** (`run-0001`…`run-0003`) and the scorecard's `generated_at`.
- **`conflicts.status`** — `escalated:oscillation` for the 25 golden entries
  flagged `oscillating`, `open` for the rest. DESIGN pins the column, not its
  vocabulary.
- **The scorecard body shape** — see ASSUMED item A4 in `src/lib/contract.ts`.
- **The audit log.** `listAudit` DERIVES a log from this dataset's own proposals
  rather than standing in for a real `audit_log`: the entries, the actors, the
  token counts and the money are all invented (`mockAuditLog()`), stably, from
  the fingerprint.

**The one thing this mock will not invent is a verification verdict.**
`getScorecard().checks` serves the SEED GENERATOR's self-checks, out of the
committed `golden/manifest-summary.json` this mock is seeded from. It does not
carry `spend-cap-burst` and does not pretend to — that is `python -m
recon.suite`'s verdict on R17's 120-thread burst against the real budget ledger,
and no in-browser stand-in has run it, so `src/routes/Audit.tsx` reports it as
*not reported* and names the command that would report it. Data the mock must
invent to be usable — a confidence, a status mix, a token count — is invented and
labelled; a claim about whether a SAFETY CONTROL WAS VERIFIED is not data, and
fabricating one would make the demo assert something nobody measured.

## Where it knowingly diverges from the service, now that the service exists

The first four are enumerated at the top of `mockClient.ts`, with the reason
each one is kept. The fifth is recorded here only: the module's own `NOTE`
inside `getScorecard` flags the CONFLICT half of the Overview's reconciliation
and is silent about the proposal half, which is the half that self-compares.

1. **`action` carries `kind` / `conflict_type` / `rule_id` beside `set`.** The
   real action has exactly ONE top-level key (`ck_proposals_action_vocabulary`,
   migration 0007). Kept because `src/routes/ProposalDetail.test.tsx:182`
   asserts `action.kind` and that ticket may not edit an existing test. Inert:
   the UI reads `action.set`.
2. **R24's gate is re-derived here.** `mockGate()` evaluates the six conditions
   this module has the data for and names the two it cannot see. The real
   verdict is `recon.apply.auto_apply_decision`. A mock whose `?auto=true`
   always succeeded would be inventing a safety property.
3. **A proposal row here carries no `conflict_type` / `conflict_sources`.** The
   real `_proposal_row` does — `recon/api/review.py` serves A8's `source` /
   `type` filters through the JOIN — so this mock is a faithful stand-in for the
   OTHER A8 case instead, and keeps `filterGuard`'s `unverifiable` arm exercised
   (`src/routes/filterHonesty.test.tsx`); the verified arm is covered by
   `src/lib/filterGuardA8.test.ts`.
4. **The audit log is derived, not mirrored** — the entry above.
5. **The Overview's proposal-mix reconciliation compares this dataset to
   itself.** `getScorecard()` builds `proposals.total` and `proposals.by_status`
   by counting `data.proposals` — the same array `listProposals` pages, and the
   same array `decide()` rewrites when you approve or reject something. Against
   the real service those are two independently-sourced figures (the committed
   scorecard artifact beside a live `GET /api/proposals` count), which is the
   whole point of the row; here they cannot durably disagree. So under
   `dev:mock` that row settles on **Match** after every decision, and the
   **Moved by review** state (`MixCell` in `src/routes/Overview.tsx`, for a mix
   that moved while the total held) shows up at most as a flicker while one of
   the two queries is still refetching — never as the condition it was written
   to report. Kept because the only alternative is a SECOND, invented proposal
   figure, and inventing the disagreement this row exists to *detect* is exactly
   the class of thing the section above forbids. Contrast the conflict half
   directly above it, which is a genuine two-artifact reconciliation and says so
   in `getScorecard`'s own `NOTE`: `conflicts.by_type` comes from
   `golden-summary.json`, while the counts it is compared against come from the
   conflict list in `golden-conflicts.json`.

Anything driven by a REAL service body lives in `src/routes/serviceShape.test.tsx`,
which uses no mock at all.

To point the dashboard at the real service instead: delete nothing in `src/` and
change nothing in the components — the UI talks to `KeystoneApi`
(`src/lib/contract.ts`) and the real `httpClient` already implements it. Set
`VITE_API_BASE_URL` to the service and `VITE_API_KEY` to the committed **admin**
demo key, and leave `VITE_USE_MOCK_API` unset. This directory is kept for the
vitest suite — the tests use it as a fake, which is what it is.
