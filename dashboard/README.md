# Keystone dashboard

Reviewer surface for the Keystone reconciliation service: conflicts and
proposals with server-side filters, evidence, confidence, and approve/reject,
and the audit log those actions are written to.

The job it exists to do: **a reviewer must, in seconds, see each conflict, the
disagreeing sources and fields, the proposal, its confidence, its evidence and
its status — and take an approve/reject action.** Everything on screen is in
service of that. There is no brand, and there is no chart: fourteen exact counts
beat a bar of fourteen bars, and the overview's job is to show a *disagreement*
between two figures, which a chart would smooth away.

Stack: Vite + React 19 + TypeScript, Tailwind v4 preflight over a small named
CSS palette, **TanStack Table v9** (`useTable` + `tableFeatures` — not v8's
`useReactTable`), TanStack Query, a 159-line hand-rolled History-API router,
vitest + Testing Library, ESLint, Playwright + axe-core.

## Routes

Four nav items, six routes.

| Route | What it shows |
| --- | --- |
| `/` | Overview — reconciles **twice**: each of the fourteen per-type conflict counts against `/api/conflicts`, and the proposal-status mix against `/api/proposals`, both put beside the figure `/api/scorecard` reports. Says **Mismatch** in words when they disagree, and **Moved by review** when the mix moved while the total held |
| `/conflicts` | Conflict list; server-side filter by source / type / status, server-side pagination |
| `/conflicts/:id` | Row detail: disagreeing sources, disagreeing fields as source-qualified paths grouped into their COMPARED_FIELDS rows — each row's Classification named per side (`CRM — …` / `App DB — …`) rather than as one unattributed verdict — the evidence packet, confidence, fingerprint, status, and the approve/reject action inline |
| `/proposals` | Proposal list; same filters, confidence as a tabular figure |
| `/proposals/:id` | Proposal detail: proposed fix and its target path, confidence, sensitivity, rationale, evidence packet, action record, approve / reject / apply |
| `/audit` | Audit log — `/api/audit` (admin scope, redacted, paged) filtered by the same actor / action / subject the rest of the dashboard shows, with its token and money totals; plus a **Verification checks** panel reading the verdicts `/api/scorecard` carries, and reporting a check the scorecard does not carry as *not reported* rather than as a pass |

Filters and page number live in the URL query string, so any view is
shareable and survives a reload.

## Commands

| Command | What it does |
| --- | --- |
| `pnpm dev` | Dev server on :5173 — **against the real API** |
| `pnpm dev:mock` | Dev server with `VITE_USE_MOCK_API=1` (see below) |
| `pnpm build` | `tsc -b` project references, then `vite build` |
| `pnpm preview` / `pnpm preview:mock` | Serve the production build on :4173 |
| `pnpm lint` | `eslint .` |
| `pnpm test` | `vitest run` (jsdom) — includes `src/lib/httpClient.test.ts`, which drives the **real** HTTP client against a stubbed `fetch` |
| `pnpm test:a11y` | Playwright: axe-core per route, keyboard-only walkthrough, computed contrast. **Fails** if Chromium is missing unless `ALLOW_A11Y_SKIP=1` |
| `pnpm mock:seed` | Regenerate the mock seed from `golden/` |

## The API contract, and what is assumed

`src/lib/contract.ts` is the single source of truth, and it separates two things
explicitly:

- **PINNED** — endpoints, auth header, error shape, `proposals.status` vocabulary
  and the row column names, all taken from `docs/DESIGN.md` §HTTP API,
  §Dashboard ↔ API and §Data models.
- **ASSUMED (A1–A10)** — shapes DESIGN did *not* promise and this dashboard had
  to pick: the `{items, page, page_size, total}` pagination envelope, the per-id
  GET endpoints, the `conflict_id` / `source` / `type` filters on
  `/api/proposals`, the scorecard body, the `conflicts.status` vocabulary, and
  two `jsonb` interiors. Each is confined to `contract.ts` +
  `src/lib/httpClient.ts`, so reconciling with the real service is a local
  change. The list is exported as DATA — `CONTRACT_ASSUMPTIONS` in
  `src/lib/contract.ts` — with each entry's failure mode marked `loud` or
  `silent`, and `src/lib/contract.test.ts` fails if a `silent` one does not name
  the guard that makes it visible.

  The service has since answered **every** id: `service/tests/api/test_contract_assumptions.py`
  maps each one to the test that answers it and keeps its `NOT_ANSWERED` map
  empty. Two of them were answered by being **wrong**, and the entries say so
  rather than being deleted: `evidence.observed_values` and
  `action.target_path` do not exist — observed values are top-level on the
  conflict row and nested at `evidence.conflict.observed_values`, and an action
  carries exactly one top-level key, `set` (migration 0007's validated CHECK).
  The UI reads the real paths; the A-list keeps the assumed ones as labels
  because that is what the correction is about.

### A8 is the one to read first

The dashboard sends `source` and `type` to `/api/proposals`. DESIGN pins
"(+ filters)" without listing them, and the `proposals` table has **no source
and no type column** — serving those two requires a JOIN to `conflicts`. A
service that does not know a query param does not usually reject it: it answers
`200` with the **unfiltered** page. On a reviewer surface that is worse than an
error.

The service answered it. `recon/api/review.py` serves both filters through the
JOIN and puts the joined `conflict_type` / `conflict_sources` on every proposal
row, so the filter can now be checked against the rows it returned. That does
not retire the guard: a row carrying neither member still cannot prove
anything, which is exactly what the in-browser mock returns.

`src/lib/filterGuard.ts` therefore checks every list response against the query
that produced it. A row that contradicts a filter raises an `ignored` warning; a
filter the row shape cannot speak to raises an `unverifiable` one. Either way the
reviewer gets a `role="alert"` banner above the table instead of a filtered
heading over unfiltered rows. The check runs in the HTTP client and again in the
query hooks, so it covers the mock and any injected client too; it is pure and
idempotent, and a `warnings` key arriving from the service is discarded — a
warning is only ever this dashboard's own verdict.

Anything the contract does not promise is read defensively: `action` and
`evidence` are `jsonb` with no pinned interior, so the evidence packet is
rendered generically key-by-key and the fix target is read with a type guard.
A status value this build does not recognise renders as a labelled badge rather
than crashing.

## Mock mode

**The service API exists** (T-5/T-7/T-8 landed), which changes what the mock is
*for*: it is no longer a placeholder for an unknown shape, it is a stand-in that
must match a known one, and every remaining difference is a divergence to be
justified rather than a gap to be filled. The real HTTP client is the default;
the mock in `src/mocks/` is reached only with `VITE_USE_MOCK_API=1`, is seeded
from the committed `golden/` artifacts, and announces itself with a banner. Read
`src/mocks/README.md` — it lists exactly what is real, what is invented, and
where it still knowingly diverges from the service.

## Accessibility (R12), and how it is verified rather than asserted

- **Status is never colour alone.** Every state carries a text label, a distinct
  icon silhouette and a distinct border treatment. `sensitive_hold` reads
  "Held for human review" — a deliberate hold, not a failure.
- **Contrast is computed, not eyeballed.** `e2e/contrast.spec.ts` walks every
  visible text node in the real browser, resolves the effective painted
  background, and checks the WCAG ratio against 4.5:1 (3:1 for large text). The
  same file walks the **focus ring** the same way, against 3:1 (SC 1.4.11), on
  every route rather than one: the ring is a single colour painted over whatever
  shell a control sits in, so what breaks it is a new dark surface — and
  checking only `/conflicts` let a real 1.70:1 ring on a detail page ship green.
- **Keyboard-only, with focus assertions.** `e2e/keyboard.spec.ts` tabs to a
  conflict, opens it and approves it using nothing but `page.keyboard`, and
  asserts *where focus is* at each step — including that focus moves to the new
  page's `<h1>` after a client-side navigation and is still on the Approve
  button afterwards.
- **Inert, not disabled.** Controls that go inactive use `aria-disabled` plus a
  guarded handler rather than the `disabled` attribute, so a control never
  disables itself out from under the focus that just activated it.
- **axe-core on every route** `e2e/routes.ts` discovers — nine of them: the
  overview, both lists, both lists filtered, both detail views (their ids read
  off the first row rather than invented), the audit log and the not-found page
  — at **zero violations of
  any impact** — the gate asserts exactly what the result is, so a future
  *moderate* regression cannot ship behind a console line. The tag list is
  `wcag2a, wcag2aa, wcag21a, wcag21aa, best-practice`; best-practice is in there
  because leaving it out hid two real defects (the mock banner sat outside every
  landmark, and the overview had two landmarks with the same name).
- **The mock banner is a landmark** (`<aside>` → complementary, "Data source
  notice"), so a screen-reader user navigating by landmarks actually reaches the
  one notice that says the data is fake.

## Configuration

Copy `.env.example` to `.env.local`. The dashboard talks to the HTTP API only
(never Postgres) via `src/lib/api.ts`, which reads `VITE_API_BASE_URL` and sends
`VITE_API_KEY` as the `X-Api-Key` header — the committed **admin** demo key, per
DESIGN §Dashboard ↔ API.

With no `VITE_API_KEY` set, the client raises `ApiConfigError` and **does not
send the request**: an unauthenticated call would come back 401 and read to a
reviewer as "the service is broken", which is the wrong diagnosis. Every other
failure is typed too — `ApiError` (non-2xx, carrying the RFC7807 body),
`ApiNetworkError` (never reached the service) and `ApiParseError` (2xx, body is
not JSON) — and `src/lib/httpClient.test.ts` exercises all of them against the
**real** client with a stubbed `fetch`, asserting the exact URL, method, query
string and headers of every request it makes.
