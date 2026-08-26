# Keystone — The Reconciliation Trust Layer — Spec

Greenfield. 3-day timebox, graded senior take-home (GT School, 100 pts, pass ≥70).
Source of truth for requirements: the GT Challenger Brief PDF. Where this spec and the PDF ever disagree, the PDF wins.

## Problem & intent

Orgs keep the "same" record in a CRM, an app DB, and a payments system; the copies drift silently until a human trips over the damage. Keystone mirrors all three sources **read-only** into one Postgres surface, continuously checks versioned business invariants, and runs an unattended reconciler that writes **pending fix proposals** (with deterministic confidence + evidence) under a hard spend cap. Nothing writes to production without human approval. The graded question is whether the unattended automation is *trustworthy*, not clever.

## User-visible behavior (requirements)

Ingestion & data
- **R1** WHEN a sync runs, THE SYSTEM SHALL mirror all 3 mock sources (HubSpot-shaped CRM, Postgres app DB, Stripe-shaped payments) through swappable read-only adapters into one normalized schema; every record carries source id, ingest timestamp, and field-level lineage; no code path can write to any source.
- **R2** WHEN a malformed/partial/oversized payload arrives via the adapter path, THE SYSTEM SHALL reject it with a structured 4xx and a log entry — never a 500 or a silent skip.
- **R3** WHEN a source times out or 5xxs, THE SYSTEM SHALL bound the retry/timeout, emit a structured error with status + latency, and never hang a sync.
- **R4** THE SYSTEM SHALL ingest the three fixture generations (snapshots) and retain per-generation history sufficient to detect a field flipping A→B→A.

Invariants & conflicts
- **R5** WHEN a sync completes, THE SYSTEM SHALL run the committed, versioned invariant rule set and record pass/fail **per record** in a queryable results table.
- **R6** THE SYSTEM SHALL match the committed golden set exactly: zero false negatives, zero false positives (verified by the automated harness against `golden/conflicts.json` and `golden/clean-sample.json`).
- **R7** WHEN two sources disagree on a field, or a record is missing from a source where invariants require it, THE SYSTEM SHALL produce a flagged conflict naming the disagreeing sources/fields — deterministic across runs (same input ⇒ same conflict set).
- **R8** Records with no defined invariant SHALL be logged as `unchecked` — never a crash.
- **R9** Duplicates across sources SHALL be deduped/merged per the documented deterministic policy; distinct records (e.g. siblings sharing guardian emails) must never collide.

Query & dashboard
- **R10** THE SYSTEM SHALL expose one unified query endpoint answering a cross-source entity question (person → registered? paid? stage?) whose output matches a hand-checked expected view.
- **R11** THE SYSTEM SHALL serve a dashboard listing conflicts (by type/record/disagreeing sources) and proposals (confidence, evidence, status), filterable by source/type/status, with approve/reject actions; every figure reconciles with the raw ingestion/invariant/proposal logs for the selected window.
- **R12** The dashboard SHALL meet WCAG AA contrast, full keyboard navigation, labeled controls, and non-color-only status indicators.

Reconciler & guardrails
- **R13** WHEN the scheduled reconciler runs, THE SYSTEM SHALL write exactly one proposal per conflict, `status='pending'`, with a confidence score and the evidence used; production/mirror data is unchanged by the run.
- **R14** Confidence SHALL be a [0,1] score computed by a committed deterministic formula over inspectable signals (documented in ARCHITECTURE.md); same conflict + same evidence ⇒ same score; partial/conflicting evidence lowers it. A hardcoded constant or raw LLM-emitted number is a failure.
- **R15** Proposals touching a sensitive field (legal name, DOB, government/student id, billing owner, financially-consequential status, consent/compliance flags) SHALL be classified `sensitive_hold`, can never auto-apply at any confidence, and are forced to human review — demonstrated by the committed C14 tests.
- **R16** WHEN the same conflict re-appears because a source re-asserts stale data (oscillation), THE SYSTEM SHALL NOT re-propose the identical fix: dedup by conflict fingerprint and escalate per the documented policy.
- **R17** THE SYSTEM SHALL enforce a configurable per-run AND hard daily spend cap on model usage: at cap → stop + log + alert (stubbed); no retry bypass; token/cost accounted from provider-reported usage against a committed configurable price table. An automated burst test proves no bypass.
- **R18** Every action (proposal, confidence, tokens, cost, reviewer decision, apply/rollback) SHALL land in an audit log that reconciles with the dashboard.

Scheduling, auth, security
- **R19** Scheduled jobs SHALL be triggered via HTTPS with a per-job shared-secret header; requests without it are 401.
- **R20** API clients SHALL read only their own scope; org-wide views require an admin scope; demo client keys are committed. All secrets live in env/vault; `.env.example` documents every variable; keys never committed or logged. TLS in deployment.
- **R21** Logging SHALL support a privacy-safe mode (hash/preview vs full body) with a documented retention policy; PII redaction in stored logs (stretch #10) is built.

Seed data (graded deliverable)
- **R22** A committed deterministic generator (`seed --seed <n>`, default seed committed) SHALL produce the full Appendix-A dataset byte-for-byte reproducibly: A.1 volumes (~100k: 40k contacts, 15k deals, 25k students, 22k enrollments, 18k payments), A.2 minimum schemas, A.3 dirty-but-clean noise, A.4 conflict manifest (C1–C14 minimums; ≥1,000 multi-child households; ≥3,000 legit deal-less orphans; 3 sync generations with ≥25 re-asserting fields; ≥20 malformed payloads), A.5 ≥10% compound-cause conflicts, and export `golden/conflicts.json` + `golden/clean-sample.json` (1,000 asserted-clean entities) on every run.
- **R23** Generator and detector SHALL share one normalization module so the golden set cannot drift from the detector.

Stretches (all in scope)
- **R24** (#7) Auto-apply as a separate function: fires only at confidence ≥0.95, approved case types, complete evidence, with a recorded rollback path; never touches sensitive fields; applies only to Keystone's canonical layer — never to sources.
- **R25** (#8) Semantic incident grouping via pgvector clusters related conflicts, surfaced on the dashboard.
- **R26** (#10) PII redaction in stored logs + a documented retention policy.

## Constraints

- Stack (locked): Python 3.12 + FastAPI service, splink (deterministic mode) for ER candidates; TypeScript/React (Vite) dashboard; PostgreSQL; invariants as versioned SQL rule files run by the service (framed as dbt-expectations-equivalent); Anthropic Haiku, temperature 0, prompt caching, for proposal rationale only. *Outcome — two departures from that list, and what shipped instead:* splink was dropped for the fallback DESIGN.md §Decisions & rationale had already approved — a hand-rolled deterministic cascade blocking on the same match keys (`service/recon/er.py`), behind the same interface; `splink` is not a dependency and appears nowhere under `service/recon/`. And the configured model is `claude-opus-5` (`service/recon/config.py:220`), not Haiku; that model's API removed `temperature`, so sending it is a 400 — the request omits sampling params there and sends `temperature=0` only on models that still accept it. Prompt caching, and rationale-text-only, are unchanged.
- Benchmarks (all measured by the committed harness): cross-source query <1s p95 on 100k (20 runs); full invariant/reconciliation pass <30s on 100k; ingestion ≥500 rec/s sustained from stubs; golden-set exact; spend cap exact under burst; dashboard <1s p95 on 100k. Two of these are measured narrower than they read, and the scorecard rows say so rather than rounding it off: `bench:detect-persist-reconcile` times detect + persist + reconcile (24.22s of the 30s) and **excludes the one-time canonical materialization**, which the same run timed at 14.22s — so the end-to-end pass is ≥38.43s and does *not* fit 30s; materialization is a precondition of the graded pass, not a stage of it. `bench:dashboard-api-p95` (115.6ms) is **service-side only** — in-process ASGI, no network, no TLS, no browser, no render — so it is a floor on a page load rather than a page load.
- ≥80% test coverage on core logic (adapters, normalization, invariants, joins, proposal-gating, spend cap), enforced by the `coverage` row of `python -m recon.suite` — *not* by CI, whose pytest step is bare and passes no `--cov-fail-under`. CI (GitHub Actions, private repo) runs tests + linter on every push.
- Runs end-to-end from a clean checkout on committed synthetic fixtures — no real PII, no live connectors, no keys required for the mock path.
- Deploy: Render (Starter) service + Neon Postgres (scale-to-zero off during grading); docker-compose for local. One honest limit found in deployment: the one-time canonical build (materialize) does not fit the 512 MB Render starter web plan and has to be run as a larger one-off job; later syncs take the `already_current` path and stay inside it.
- Agentic execution: build phases are Claude Code sessions; effort is measured in sessions/tickets, not hours.

## Non-goals

- No live CRM/payments/messaging connectors (documented as possible, never built). No writer to any source, ever.
- No SSO/OAuth/login UI/user management. No multi-region/HA/horizontal scaling (100k→10M is a written rationale only). No brand/visual identity beyond clean shadcn/Tailwind. No ticketing/messaging ingestion (stretch #9 skipped — no fixtures exist for it). No secrets managers beyond env/vault, no monitoring stacks; `/health` + structured logs + stubbed spend-cap alert is the ceiling.
- The LLM never detects conflicts, never computes confidence, never writes anywhere. Rationale text only.
- No process journals, changelogs, or per-action agent documentation in the repo.

## Success criteria

1. `recon.suite` scorecard: golden diff 0 FN / 0 FP; join check passes; proposal-safety check passes (N conflicts → N pending, mirror unchanged, C14 held); burst test stops the spend exactly at cap (the reservation is refused and nothing is charged — the run's own fate is the caller's, and the two callers differ; DESIGN.md §Budget ledger says which); all six benchmarks green. (R5–R7, R13–R17, R22)
2. Two full runs from the same seed produce byte-identical datasets, identical conflict sets, and identical confidence scores. (R14, R22)
3. Grader path works: clean checkout → documented setup → seed → service + dashboard up → suite green. *Measured — "in minutes" is the brief's phrase for the quick-start, and it is the **running system** that meets it, not the full scorecard:* `python -m recon.suite` runs its `coverage` row **first** by design (it shells out to pytest, so it has to happen before the pipeline takes its snapshot rather than under it), and the committed scorecard clocks that one row at **32 m 29 s** of real pytest. README's "three ways in, by time budget" table is the authority on the wall clock and states the clean-clone-to-full-scorecard total; the ~1-minute path through the suite is the correctness subset, `recon.suite --only golden-diff --only clean-sample --only join-check --no-write`. Part of this criterion is that neither long step blocks *silently*: each is announced at the point it is run, so the grader chooses the wait instead of discovering it. (README)
4. Deliverables present: README, ARCHITECTURE.md (data-flow + sequence Mermaid rendering on GitHub + ≤1-page rationale covering adapter boundary, in-code holds-before-writes enforcement point, cap non-bypass, one 100k→10M change), AI_USAGE.md, committed invariant rules + price table + harness + scorecard output, `.env.example`, demo client keys, endpoint contract, proposal/auto-apply policy doc, privacy/retention policy doc, deployed URL.
5. Demo video beats executable in order: architecture walk → read-only mirror → dashboard populates → conflict flagged → pending proposal with evidence → low-confidence routes to review while ≥0.95 non-sensitive auto-applies with rollback → spend cap refuses a burst at the cap.

## Open questions

None — decisions locked 2026-08-22 (language, invariant engine, stretch scope, hosting, all defaults).
