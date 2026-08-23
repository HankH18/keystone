# The mock service — read this before trusting anything you see in the UI

**The Keystone HTTP API does not exist yet.** T-5, T-7 and T-8 build it. This
directory is a stand-in so T-10 could be built and tested against the pinned
contract in the meantime.

## How to tell whether you are looking at mock data

Four independent ways, all of them cheap:

1. **The banner.** When the mock is live the app shows a permanent yellow
   banner at the top of every page saying so.
2. **The flag.** The mock is reached only when `VITE_USE_MOCK_API=1`.
   `pnpm dev`, `pnpm build` and `pnpm preview` do **not** set it, so they use
   the real HTTP client and will show the error state until the service exists.
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

## What is invented, and will change when the real API lands

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

## What has to change when the real API lands

1. Delete nothing in `src/`, change nothing in the components. The UI talks to
   `KeystoneApi` (`src/lib/contract.ts`) and the real `httpClient` already
   implements it.
2. Work through the **ASSUMED** list at the top of `src/lib/contract.ts`
   (A1–A7) against what T-5/T-7/T-8 actually shipped, and fix
   `src/lib/httpClient.ts` where they differ. Every assumption is confined to
   those two files.
3. Point `VITE_API_BASE_URL` at the service and set `VITE_API_KEY` to the
   committed **admin** demo key.
4. This directory can then be deleted, or kept for the vitest suite — the tests
   use it as a fake, which is what it is.
