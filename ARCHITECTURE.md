# Keystone — Architecture

Three read-only sources disagree. Keystone lands them, normalizes them against one shared spec,
resolves identity, runs a committed SQL rule set that emits a per-record verdict, turns the surviving
conflicts into **proposals that no machine may approve**, and lets a human decide. Every mutation of an
existing canonical row must cite the proposal that authorized it, in the same transaction.

Both diagrams below were drawn from the code as it stands, not from a design doc. Line references are
live at the current working tree.

---

## 1. Data flow

```mermaid
flowchart TB
  subgraph SRC["Sources — 3 JSONL snapshot generations, never written"]
    CRM["crm"]
    APP["appdb"]
    PAY["payments"]
  end

  ADP["ReadOnlyAdapter port — adapters/base.py:279<br/>source_id · generations · read<br/>three members, no write member"]
  CRM --> ADP
  APP --> ADP
  PAY --> ADP

  subgraph SYNC["POST /internal/sync — sync_job, 3 pinned stages, api/internal.py:490"]
    ING["stage 1 · ingest_all"]
    NORM["_materialize — ingest.py:1117<br/>normalizes INLINE, in the SAME transaction as the COPY<br/>imports recon/normalize.py, the R23 shared spec"]
    RES["stage 2 · resolve.materialize — resolve.py:750<br/>recon.er cascade"]
    INV["stage 3 · run_invariant_stage — api/internal.py:392<br/>runs rules/NNN_name.vX.sql in filename order"]
  end

  ADP -->|"recon_writer"| ING
  ING -->|"COPY · append-only"| RAW[("raw_records")]
  ING --> LEDG[("ingest_runs<br/>source_generations")]
  RAW --> NORM
  NORM -->|"COPY"| STG[("stg_crm_contact · stg_crm_deal · stg_student<br/>stg_enrollment · stg_payment<br/>THIS is the normalized layer — there is no 4th stage")]
  STG -->|"recon_writer"| RES
  RES --> CANON[("entities · entity_links<br/>entity_link_candidates · field_lineage")]

  STG -->|"schema owner via OPS_DATABASE_URL<br/>the 3 app roles are denied TEMPORARY by migration 0006"| INV
  TMP["session TEMP er_* / ref_* — invariants/context.py:613<br/>the ONLY other input a rule has.<br/>No rule reads entities, entity_links, field_lineage or raw_records"]
  TMP --> INV
  INV -->|"COPY · one verdict per record per rule"| IRES[("invariant_results")]
  INV -->|"persist_run invariants/runner.py:344<br/>ON CONFLICT fingerprint DO UPDATE last_seen_run"| CONF[("conflicts — the conflict store")]

  subgraph RECON["POST /internal/reconcile — reconcile_job, bound at app.py:282"]
    RC["reconcile — reconciler.py:1516<br/>score · classify · fix_action · dedup"]
    RAT["rationale hook — reconciler.py:1358<br/>TEXT ONLY, called after scoring, return value discarded except str"]
  end

  CONF -->|"recon_writer · ORDER BY fingerprint"| RC
  CANON -.->|"read-only evidence inputs:<br/>entity_link_candidates · field_lineage · entities"| RC
  PROP -.->|"read-only: prior proposals, for R16 dedup"| RC
  RC --> RAT
  RAT -->|"reserve → call → settle_capped<br/>llm.py:786 / 898 / 919"| CAP[("budget_ledger<br/>budget_reservations")]
  RC -->|"born pending or sensitive_hold — never approved"| PROP[("proposals — the pending queue")]
  RC -->|"status · last_seen_run · escalation_reason ONLY"| CONF

  PROP --> DASH["Reviewer dashboard<br/>GET /api/conflicts · /api/proposals · /api/entities"]
  DASH -->|"POST approve or reject · review_writer<br/>the only role that may decide"| DEC["_decide — api/review.py:789"]
  DEC --> PROP
  DASH -->|"POST /api/proposals/:id/apply — only on an approved row"| AP["apply_proposal — apply.py:1465<br/>apply_writer"]
  AP -->|"ONE transaction — the citation"| EV[("proposal_events")]
  AP -->|"UPDATE current, updated_at ONLY"| CANON

  ING --> AUD[("audit_log — logging.py:536 redacting chokepoint")]
  RC --> AUD
  DEC --> AUD
  AP --> AUD
```

Two things this diagram deliberately does **not** show, because the code does not do them:

- **There is no raw → staging → normalized three-step.** `stg_*` *is* the normalized layer.
  `_materialize` (`ingest.py:1117`) runs inside `_land_records`, in the same transaction as the
  landing `COPY`, and calls `recon.normalize` directly.
- **No arrow from `entities` into the invariant engine.** Grepping every `FROM`/`JOIN` in `rules/*.sql`
  returns only `stg_crm_contact`, `stg_crm_deal`, `stg_student`, `stg_enrollment`, `stg_payment` and
  the session-scoped TEMP `er_*` / `ref_*` tables that `invariants/context.py:613` materializes. No
  rule references `entities`, `entity_links`, `entity_link_candidates`, `field_lineage` or
  `raw_records`. The canonical layer feeds the *reconciler*, not the detector.

One label needs a footnote. The three-column `conflicts` grant is what migration
`migrations/versions/0015_escalation_reason_grant.py` establishes; `docs/scorecard.txt` was generated
against a database one migration earlier and carries two notes saying `escalation_reason` is *not*
writable by `recon_writer`. **Those notes are stale, not a disagreement** — they describe the tree
before 0015 and go away on the next suite run. The reconciler is correct either side of it: it asks
`has_column_privilege` once per run and escalates with the columns it actually holds, reporting
`escalation_reason_persisted`, and the reason always reaches the `conflict.escalated` audit row.

---

## 2. One reconcile cycle

```mermaid
sequenceDiagram
  autonumber
  participant Cron as Render cron
  participant API as recon.app
  participant PG as Postgres
  participant Rec as reconcile
  participant Sc as recon.confidence
  participant Bud as recon.budget
  participant Prov as LLM provider
  participant Rev as Reviewer

  Note over Cron,API: hourly at 20 past the hour — infra/render.yaml
  Cron->>API: POST /internal/reconcile with X-Trigger-Secret
  API->>API: trigger_guard, hmac.compare_digest against TRIGGER_SECRET_RECONCILE<br/>per-job secret, no shared-secret fallback, checked before the body is parsed
  API->>PG: claim_run — at-most-once, commits audit trigger.reconcile before the body runs
  API->>PG: provision_run_scope — creates this run's budget scope
  Note right of API: if the scope cannot be provisioned the trigger answers 503<br/>and the job does NOT run: an uncapped run is not allowed
  API->>Rec: reconcile_job run_id — bound in create_app, app.py:282

  Rec->>PG: SELECT conflicts ORDER BY fingerprint, prior proposals, candidates,<br/>field_lineage, source_generations — as recon_writer
  loop each conflict, in fingerprint order
    opt field oscillated A to B to A
      Rec->>PG: UPDATE conflicts status escalated + audit conflict.escalated
    end
    Rec->>Sc: score Signals — 7 committed signals, no LLM input
    Sc-->>Rec: confidence plus the full derivation packet
    Rec->>Rec: classify sensitivity to pending or sensitive_hold<br/>fix_action derives the proposed write
    Note over Rec,Prov: THE SPEND CAP GATES THE RATIONALE TEXT, AND IT GATES IT HERE
    Rec->>Bud: reserve worst case — one atomic INSERT, run scope and daily scope together
    alt cap would be exceeded
      Bud-->>Rec: SQLSTATE KS006 — cap_hit audit row plus alert, no provider call, rationale NULL
    else reservation granted
      Rec->>Prov: complete prompt — text only
      Prov-->>Rec: rationale text
      Rec->>Bud: settle_capped — actual against reserved, releases the difference
    end
    Rec->>PG: INSERT proposals status pending or sensitive_hold<br/>recon_writer holds no UPDATE on proposals at all
    Rec->>PG: INSERT audit_log proposal.created
  end
  Rec->>PG: INSERT audit_log reconcile.run
  Rec-->>API: report
  API-->>Cron: 200 carrying the handler's own verdict, not an optimistic started

  Rev->>API: POST /api/proposals/:id/approve with X-Api-Key admin scope
  API->>PG: UPDATE proposals as review_writer — status, decided_by, decided_at ONLY
  API->>PG: INSERT audit_log proposal.approved, actor prefixed reviewer
  Rev->>API: POST /api/proposals/:id/apply
  API->>PG: as apply_writer, ONE transaction: INSERT proposal_events citing the proposal,<br/>UPDATE proposals status, UPDATE entities.current to OLD.current merged with action.set
  API->>PG: INSERT audit_log proposal.applied
```

**Where this diverges from the brief's beat order, and why the code is right.** The brief lists
*"reconciler proposes → spend-cap check → write pending"*. In the implementation the cap gates the
**rationale text**, not the proposal: `reconcile()` calls `_rationale` (`reconciler.py:1613`) and
only then `_insert_proposal` (`:1614`). The cap check lives inside `recon.llm` as
`reserve → complete → settle_capped` (`llm.py:786 / 898 / 919`). There is no cap check between the
proposal and the pending write because **writing a proposal costs nothing** — the only money in the
cycle is the model call, and it is reserved before it is made. Drawing a separate gate after the
proposal would show a control that does not exist.

The brief's three earlier beats — sync, the invariant check, conflict detected — are absent for the
same reason: they are not part of this cycle. They run on a **separate hourly cron** (`"0 * * * *"`
against reconcile's `"20 * * * *"`, `infra/render.yaml`) and appear in diagram 1; a reconcile cycle
begins at an already-populated conflict store, which is what the code does.

Note also that under the default `LLM_PROVIDER=mock`, `rationale_hook_for` (`reconciler.py:1358`)
returns `no_rationale` — *the identical function object* — so every graded run makes zero model calls,
reserves nothing, and produces byte-identical proposals. The `alt` branch above is the live-provider
path.

---

## 3. Rationale

**Adapter boundary.** `ReadOnlyAdapter` (`adapters/base.py:279`) is a `@runtime_checkable` Protocol with
exactly three members — `source_id`, `generations()`, `read()`. No write method: a source is a snapshot
you pull. **Proven, not asserted**: `assert_sources_are_unwritable()` (`apply.py:1863`) walks every
adapter's full MRO **and its instance `__dict__`** — a writer bound at construction lives on no class —
and returns three class names, zero offenders. It publishes its own limit (`apply.py:1876`):
`WRITE_NAME_TOKENS` is a substring list, not a decision procedure. It and `source_tree_digest()`
(`apply.py:1931`, which hashes the whole fixture tree either side of a real committed apply and rollback)
are reached only from tests; the load-bearing arm is the Protocol's missing write member.

**Holds before writes — enforced where.** An `UPDATE` of `entities.current` is accepted only when, **in
the same transaction**, a `proposal_events` row names that `canonical_id`, records `before`/`after` as the
pre- and post-update values (compared by jsonb equality *and* textually), cites an `approved` (or
applying-in-this-transaction) proposal whose `target_canonical_id` is that entity, and the new value
equals `OLD.current || action->'set'` exactly. Citations are single-use (partial unique
indexes); a reversal may only undo the write on top. Beneath that, three roles with column-scoped grants,
connected as **real logins, never `SET ROLE`** (`db.py:286`): `recon_writer` PROPOSES (born
`pending`/`sensitive_hold`, no UPDATE on `proposals`), `review_writer` DECIDES
(`proposals(status, decided_by, decided_at)`), `apply_writer` APPLIES (`entities(current, updated_at)`
only, no INSERT). What it does **not** guarantee, in the same breath:

1. **Canonical CREATION is not citation-guarded, by design** — the pipeline may APPEND, only the guarded
   path may MUTATE. `recon_writer` holds INSERT on `entities`; the INSERT-side check is provenance, not
   authorization (`KS008`: a canonical row must descend from an `entity_links` row), so fabricating one
   costs three INSERTs rather than a proposal.
2. **"Exactly one canonical write" means one write of `current`, not one UPDATE statement** — under an
   evidence-only `{"set": {}}` approval, repeated no-op UPDATEs of other columns satisfy the rule.
3. **The schema owner is not bound, on either environment.** Locally it is a superuser, so
   `SET session_replication_role='replica'` disables every trigger for the session with no DDL. Neon's
   `neondb_owner` is not, and that is refused — but `ALTER TABLE ... DISABLE TRIGGER` needs only table
   ownership, so it still lands. Neon removes the *silent, session-scoped, no-DDL* bypass, not the
   bypass. The boundary binds the three **application** roles, never the owner.
4. **The database checks authorization, not correctness.** The action CHECK pins the *shape* of
   `proposals.action`, and `ck_proposals_sensitive_covers_write_set` (migration 0012) refuses a
   `sensitive = false` proposal whose `action->'set'` names any `SENSITIVE_FIELDS` path — but nothing
   verifies the fix is the *right* one for its conflict, only that the write matches the action.

**The spend cap — enforced where.** The cap lives in the database, not in Python. A Python-side cap
once fell to a red team zeroing `budget_ledger.spent_microusd`, so the capped party was left **no
writable spend column at all**: `recon_writer` holds no INSERT and no UPDATE on `budget_ledger`, and spend
moves only under the `budget_reservations` triggers. `reserve` is one atomic `INSERT` whose
`BEFORE INSERT` trigger locks the ledger row, checks `spent + reserve <= cap` and raises `KS006`
otherwise — reserved **before** the call, so a burst cannot race post-call accounting — and it has **no
`scopes` parameter**, so nobody opts out of the daily cap. `ck_budget_spent_within_cap`
(`0001_initial_schema.py:629`) is the CHECK backstop. Real burst (`docs/scorecard.txt`): **120 contenders,
6 granted, 114 refused, every refusal `KS006`; cap 81,600 µUSD, reserved-while-open 81,600 = exactly the
cap; settled 10,782 = 6 × 1,797; ledger violations 0; over-admitted false; retry wave 10 → 0 granted.** A
cap hit ends the attempt loop.

What that does **not** cover: it binds what may be spent **before** a call, not the **release**
after one. `recon_writer`'s `UPDATE` on `budget_reservations` is column-scoped but carries no row
predicate, so one statement settling every `state = 'open'` row as `never_sent` at `actual = 0` hands
back other runs' reserves too — on a scratch database that took two full run scopes from `spent = cap`
to `spent = 0`, and the next reservation was granted. Migration 0010 bounds *which* rows that may
touch — `ops_attested_outage` is refused to `recon_writer`, and a `never_sent` claim on a reservation
older than `NEVER_SENT_WINDOW_SECONDS = 60` is refused `KS007` — but not how many, and no trigger can
tell a truthful pre-send failure from a fabricated one.

**One change to scale 100k → 10M: detect incrementally over a *persisted* ER projection instead of
rebuilding the whole world in Python every sync.** Stage 3's `build_context` (`invariants/context.py:613`)
→ `load_snapshot` (`:265`) pulls every row of all five `stg_*` tables into Python lists and re-runs the
global `recon.er.resolve` cascade in process — work stage 2 (`resolve.materialize`, `resolve.py:750` →
`resolve_generation`, `:365`) already did and persisted. One sync runs that cascade **twice**, holding the
whole world in RAM both times, so the wall is memory, not CPU. Measured (`api/internal.py:114-117`;
dataset `docs/scorecard.txt:4`): first sync 58.3s = 22.3 ingest + 22.1 materialize + 13.8 invariants over
360,400 records; `bench:invariant-pass` already burns 76% of its `<30s` budget at 3.6% of target volume;
and 376,000 `invariant_results` rows land per sync (`api/internal.py:92-93`), hourly. So: have
stage 2 write the `er_*` projection once, persistent and generation-keyed; point `invariants/context.py`
at it; parameterise the rules on the keys the landed generation touched. Two properties already make that
safe: `persist_run`'s `ON CONFLICT (fingerprint) DO UPDATE SET last_seen_run`
(`invariants/runner.py:344`) makes re-detection idempotent, and `ABSENCE_RULES`
(`invariants/rules.py:44`) names the 7 of 14 rules that fire on a record *not* existing — those cannot be
delta-scoped and move to a periodic full pass.

---

## 4. The confidence model

Deterministic, inspectable, and it is **not** a hardcoded constant and **never** an LLM number.
`recon/confidence.py` imports nothing from `recon.llm`, and `score()` takes a `Signals` value object,
not text — "we did not do it" is a promise and an import graph is a fact. All 14 bases and all 7
weights live in the committed **`confidence.yaml`** (repository root); the evaluator holds no number of
its own beyond the `decimal` precision (`_PRECISION = 28`), two `Decimal(0)` accumulator seeds and the
`Decimal(1)` that builds the quantum. A missing or malformed model file **raises**; it never defaults,
because a model that degrades to hardcoded numbers when its file is unreadable is a hardcoded model with
extra steps.

### The formula

```
clamp01(clamp01(base[conflict_type] + sum(positive)) + sum(negative))
```

pinned as a literal string at `confidence.yaml:65` and asserted verbatim in the suite. Ordered
procedure: pin the `decimal` context inside `localcontext()` so a caller cannot reach in; read the
base; walk `signal_order` accumulating each `weight × value` into the positive or negative half **by
the sign of the committed weight** (a count signal scoring 0 is still a penalty term, or a zero-valued
penalty drifts into the positive half); clamp the positive half; add the negatives; clamp again;
**quantize exactly once** at the end to 4 places, `ROUND_HALF_EVEN`, matching
`proposals.confidence NUMERIC(5,4)` so the stored value is the computed value.

### The seven signals

| # | Signal | Weight | Kind | Fires when |
|---|---|---|---|---|
| 1 | `hard_external_id_agreement` | **+0.35** | boolean | a gen-3 `entity_link_candidates` row with `key_class='ext'` links one of the conflict's `crm:contact:` refs to one of its `appdb:student:` refs, and every key class that resolved lands on a common student |
| 2 | `normalized_email_agreement` | **+0.25** | boolean | same test on `key_class='email'` — independent of #1, so they add |
| 3 | `name_dob_exact` | **+0.20** | boolean | same test on `key_class='namedob'`; `normalize.match_keys` emits no namedob key unless first, last and dob are all present |
| 4 | `amount_date_corroboration` | **+0.10** | boolean | `corroborating_keys[type]` is non-empty **and** every key in it is present and non-null in `observed_values`. Empty list ⇒ 0 by construction |
| 5 | `disagreeing_field` | **−0.10** | **count** | per distinct `COMPARED_FIELDS` comparison **row** in `disagreeing_fields`. The row, not the path — a disagreeing comparison puts *both* source-qualified endpoints in the set. Only C6 and C14 populate it |
| 6 | `partial_evidence` | **−0.15** | boolean | any of: an incomplete gen-3 source; a null/empty pinned `observed_values` entry (but not `False`/`0`); fewer than two distinct sources; two match-key classes resolving one source record to two different entities |
| 7 | `oscillation_observed` | **−0.25** | boolean | the underlying field went A→B→A across three ascending generations, per `scan_field_lineage` over `field_lineage` — or, when that scan has no rows to read, per the `conflicts.oscillating` column the invariant run stamped. The packet records which of the two answered |

### The 14 bases

The base answers one question — *given only that this rule fired, how much of the FIX is already
determined?* — not "is the conflict real"; the engine grades 0 FP / 0 FN against the golden set, so
every conflict here is real. A type whose repair value is read straight off the authoritative record
starts high; one that needs a human to choose between two records starts low.

| | | | | | | | |
|---|---|---|---|---|---|---|---|
| C1 paid-but-no-deal | 0.50 | C2 payment-no-person | 0.35 | C3 duplicate-by-email | 0.45 | C4 same-person-diff-emails | 0.50 |
| C5 one-source-only | 0.50 | C6 field-disagreement | **0.40** | C7 enrolled-unpaid | 0.50 | C8 dropped-sibling | 0.45 |
| C9 stale-pointer | 0.55 | C10 merge-collapsed | **0.30** | C11 duplicate-payment | 0.55 | C12 wrong-amount | 0.55 |
| C13 refund-not-reflected | 0.55 | C14 sensitive-field-only | **0.40** | | | | |

Totality is enforced at parse time: a missing base, or a base for an unknown type, is a hard error.

### Why it clamps twice

Version 1 was `clamp01(base + sum(all))`. That quietly discounted — usually to nothing — every penalty
on a conflict whose positive evidence had already carried the total past `clamp_max`. Measured on the
graded 3,050-proposal store (`confidence.yaml:53`): **1,057 proposals have positive evidence that alone
saturates the clamp, and 191 of those carry a penalty**, which under v1 changed the stored number by
nothing on 181 of them. Two proposals with identical positive evidence, one additionally flagged as
resting on partial evidence, stored byte-identical confidences. That is not "lowered by partial
evidence", and R14 clause (c) requires that it be. Clamping the positive half first and subtracting
after fixes exactly that and nothing else:
wherever `base + sum(positive) ≤ clamp_max` the two versions are arithmetically identical, so no score
outside the saturated region moved, and **no base and no weight changed in the v1 → v2 bump**. The
rejected alternative — rescaling weights so saturation is unreachable — would have moved every score
that was never wrong.

Worked example, pinned as a test: C6 with all three identity signals and one disagreeing row is
`0.40 + 0.35 + 0.25 + 0.20 = 1.20 → clamp → 1.0000`, then `− 0.10 = **0.9000**`, with `raw_total 1.10`,
`positive_total 0.80`, `negative_total −0.10`, `positive_clamped true`, `clamped false`. That test
previously asserted `1.0000`; the old expectation encoded the defect as the contract.

### The ceiling — the gate working in both directions

**C6 and C14 ceiling at exactly 0.9000 and can never auto-apply,** and this is structural rather than
incidental. `R-006` and `R-014` are the only rules that populate `disagreeing_fields`, and both build
that set from rows `WHERE disagrees` — so every one of the 500 C6 and 50 C14 conflicts in the graded
set carries at least one disagreeing row (0 of 550 are empty). Clamp #1 caps the first term at
`clamp_max = 1.0000` for *every* type, independent of the base, so one disagreeing row at −0.10 always
lands after saturation: ceiling `1.0000 − 0.10 = 0.9000`, permanently below R24's
`AUTO_APPLY_CONFIDENCE_FLOOR = 0.95`. Brute-forcing every signal configuration through the real
`score()`, and re-counting `golden/conflicts.json` through the real `disagreeing_row_count`:

```
C6:  MAX with d>=1 = 0.9000   MAX with d==0 = 1.0000   auto-appliable at d>=1 = False
C14: MAX with d>=1 = 0.9000   MAX with d==0 = 1.0000   auto-appliable at d>=1 = False
golden (type, ROW count): {('C6',1):420, ('C6',3):80, ('C14',1):20, ('C14',2):30}
total C6+C14: 550   with ZERO disagreeing rows: 0
```

A guard test derives the ceiling *from the loaded model* — weight, sign and formula shape — so a model
change goes red rather than silently wrong.
C14 is separately and independently immune: `_ALWAYS_HELD_TYPES = {"C14"}` holds every C14 on the
strength of its type before any path or score is consulted, so the ceiling is load-bearing only for C6.

The consequence is stated rather than hidden (`docs/proposal-policy.md` §8): the one auto-apply-
eligible path that the entity view projects is a C6's fix target, and no C6 can reach 0.95; the
proposals R24 *would* take unattended write exactly `{appdb.enrollment.crm_deal_id}` — 50 C9
stale-pointer fixes at ≥0.95 — and that path is not in `recon.resolve.VIEW_FIELDS`. So the honest
count of proposals that are both auto-appliable and observable is **0**. That is the gate working in
both directions, not a defect: sensitivity is a pure function of the target field path, evaluated
**before** confidence and winning over it at every score including 1.0. Putting a sensitivity term in
the formula would make the hold a matter of arithmetic, which is the one thing R15 forbids.

### Determinism, and what the model says about itself

No binary float is ever constructed: every YAML numeric must be a **quoted string** and the parser
refuses any other type outright (`Decimal(0.35)` is `0.34999999999999997779…`), with a regex scan over
the file *text* catching an unquoted literal that every other test would pass. The context is pinned,
not inherited. Terms sum in the committed `signal_order`. There is exactly one quantization. The model
is identified by the **sha256 of its raw bytes**, computed before parsing and stamped into every
evidence packet as `model_sha256`, so an edit that forgets to bump `version` is still visible on every
row written afterwards. Every ordering hazard was searched for and closed at parse time — two signals
sharing a `key_class` is refused, more than one signal defining `corroborating_keys` is refused, and
every set is either reduced to a `len()` or emitted sorted.

`Score.as_dict()` persists the whole derivation into `proposals.evidence['confidence']`: model version
and digest, the formula string, base, every term with its value/weight/contribution, all four totals,
both clamp flags, the precision block, and the raw signals. **A reviewer holding one row can re-derive
its number without the repository.**

Two weaknesses the model self-reports in `confidence.yaml`, kept because overstating discriminating
power is worse than admitting it: signal #4 is **constant within every type** on this dataset (1 for
C1/C11/C12/C13, 0 for the other ten), so it "contributes what adding 0.10 to four bases would"; and
clause (c) of signal #6 is fixed by conflict type for C2/C3/C5/C11, while the term itself varies
per-instance only within C8 and C9, and there via clause (b). The three identity signals are what
actually vary within a type.

One accuracy note: the `[0,1]` bound is guaranteed by the committed `clamp_min`/`clamp_max` values and
asserted in `test_confidence_yaml.py`, not validated inside `_parse`. Since that same test pins the
file's sha256, any drift is a red build — but the range is test-enforced, not code-enforced.

---

## 5. Why plain versioned SQL invariants, not dbt-expectations

Deliberately dbt-expectations-*equivalent*, and deliberately not dbt. **Per-record verdicts are the
grading contract**: `invariant_results` carries one row per in-scope record per rule per run, with the
verdict and the reason. dbt's `store_failures` overwrites per run and does not reliably emit full
rows, so it cannot answer "which rules judged this record on that run" — which is the question the
whole audit story rests on. Keeping the rules as `rules/NNN_name.vX.sql` also keeps them versioned in
the same repo as the code that runs them, lints them at load time, and loads them in deterministic
filename order. Rejected: the full dbt toolchain. (`docs/DESIGN.md` §Decisions & rationale.)
