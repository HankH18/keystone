# Keystone — Source, Identity & Invariant Contract

Normative for the seed generator (`recon.seed`), the entity-resolution layer (`recon.er`), the invariant
rule set (`rules/*.sql`), and the grading harness (`recon.suite`). It exists so the **generator and the
detector cannot drift** (R23): both derive from this document and from the *same* code modules —
`recon/normalize.py` (normalization + match keys) and `recon/reference.py` (enum maps, fee schedule,
sensitive-field list, precedence table). Neither side may re-implement either.

Where this document and the brief PDF disagree, the **PDF wins** — stop and flag it.

---

## 1. Source schemas (as generated)

Minimum fields are the brief's Appendix A.2 verbatim. Added fields are marked `[+]` and are permitted
("You may add fields; you may not remove these"). Every added field exists because a mandated conflict
class or FP-guard is otherwise undetectable.

### CRM (HubSpot-shaped) — `contact`
| field | type | notes |
|---|---|---|
| `crm_id` | str `CRM-0000001` | PK |
| `email` | str | the **guardian/billing** email; siblings share it |
| `first_name`, `last_name` | str | the **student's** name (admissions CRM: contact = prospective student) |
| `lifecycle_stage` | str | HubSpot vocab, dirty-cased |
| `created_at`, `updated_at` | ISO-8601 Z | `updated_at < created_at` on ~0.5% |
| `external_id` | str \| null | → `appdb.student.id`. Present on ~60% of linkable contacts |
| `dob` `[+]` | `YYYY-MM-DD` \| null | present ~70%; required for the name+DOB join the brief mandates |
| `grade` `[+]` | str \| null | dirty variants (`"Grade 4"`, `"4"`, `"4th"`) — C6 compares this |
| `state` `[+]` | str \| null | `TX`/`Tx`/`TEXAS` dirt |
| `marketing_consent` `[+]` | bool \| null | **sensitive** (consent flag) |

### CRM — `deal`
`deal_id` (`DEAL-0000001`), `name`, `pipeline`, `stage`, `amount` (dollars, float — CRM-shaped),
`associated_contact_ids[]`, `created_at`, `updated_at`.

### App DB (Postgres) — `student`
`id` (uuid5, deterministic), `first_name`, `last_name`, `dob`, `grade`, `guardian_email`,
`guardian2_email` (~60% null), `status`, `enrollment_year`, `created_at`, `updated_at`,
`[+] student_number` (`S-000123`, the **government/student identifier** — sensitive),
`[+] household_id` (`HH-000123`; a *data* field the generator writes and the detector may read — it is
NOT a cross-source key and never links sources), `[+] communication_opt_out` (bool, sensitive).

> `household_id` is intentionally app-DB-local. C8 detection **must not** depend on it existing in CRM
> or payments; the detector infers households from normalized guardian emails (§4.8) and only uses
> `household_id` as a corroborating signal available in one source.

### App DB — `enrollment`
`id` (uuid5), `student_id`, `program`, `stage`, `deposit_paid_at` (nullable), `crm_deal_id` (nullable,
~60% of linkable), `created_at`, `updated_at`, `[+] billing_owner_email` (sensitive — billing ownership).

### Payments (Stripe-shaped) — `payment`
`payment_id` (`pi_0000001`), `payer_email`, `payer_name`, `amount_cents` (int), `currency` (`"usd"`),
`type` (`fee|deposit|tuition`), `status` (`paid|refunded`), `occurred_at`, `external_ref` (→
`appdb.student.id`, ~60% of linkable), `[+] metadata: {student_name: str|null (~85% present),
program: str|null}`, `[+] refunded_at` (nullable).

---

## 2. Normalization (`recon/normalize.py` — the shared spec, R23)

- `norm_email(e)`: strip, casefold, drop surrounding quotes/backticks. **Only** for `gmail.com` /
  `googlemail.com`: remove `.` in the local part and truncate at `+`. Never for any other domain
  (universal dot-stripping collapses legitimately distinct addresses → false positives).
- `norm_name(s)`: strip, collapse internal whitespace, strip stray `` ` `` `'` `"` characters, casefold,
  NFKD-fold accents. Never merges different spellings (`Jon` ≠ `John`).
- `norm_enum(field, v)`: table-driven from `recon/reference.py`. Unknown value → `None` **plus** an
  `unchecked` note; never raises.
- `norm_dob(v)`: `YYYY-MM-DD` or `None`.
- `match_keys(entity)`: ordered, deterministic — `("ext", <hard id>)`, `("email", norm_email)`,
  `("namedob", (first_norm, last_norm, dob))`. Candidates only; **never** an automatic merge.
- **Idempotence is a property test**: `f(f(x)) == f(x)` for every `norm_*`.

**SQL rules may not normalize.** `rules/*.sql` must never call `lower()`, `trim()`, `replace()`,
`regexp_*` or any casefold on an identity field. Normalization is materialized upstream by Python into
`stg_*` tables (§3). A committed lint test greps the rule files for these tokens and fails the build.

### Committed enum maps (`recon/reference.py`)
- **grade**: `PK, K, 1..12`. Accepts `Grade 4`, `4`, `4th`, `Fourth`, `grade4`, `Kindergarten`, `KG`, `Pre-K`.
- **state**: 50-state code map; `TX`/`Tx`/`TEXAS`/`texas` → `TX`.
- **enrollment.stage** (canonical funnel): `prospect, applied, waitlisted, deposit_paid, enrolled, withdrawn, refunded`.
- **deal.stage → funnel** (bijective, so a cross-source comparison is lossless):
  `New Lead→prospect`, `Application Submitted→applied`, `Waitlisted→waitlisted`,
  `Deposit Received→deposit_paid`, `Closed Won→enrolled`, `Closed Lost→withdrawn`, `Refunded→refunded`.
  Dirty variants accepted: case, `_`/`-`/space, `CLOSED_WON`, `closed won`.
- **student.status**: `prospect, applied, enrolled, active, withdrawn` (`active` folds to `enrolled` for comparison).
- **lifecycle_stage**: HubSpot vocab + `MQL`/`SQL` abbreviations.
- `PAID_IMPLYING_STAGES = {deposit_paid, enrolled}`.
- **Fee schedule** (exact, cents) — programs `Lower School | Middle School | Upper School | Summer Academy`:
  | type | Lower | Middle | Upper | Summer |
  |---|---|---|---|---|
  | `fee` | 10000 | 10000 | 10000 | 10000 |
  | `deposit` | 50000 | 60000 | 75000 | 25000 |
  | `tuition` | 1200000 | 1400000 | 1600000 | 300000 |
- `ENROLLMENT_GRADE_FLOOR = "K"` — a household child below this grade is legitimately absent from the
  app DB / payments (the C8 false-positive guard, §4.8).

---

## 3. Pipeline shape

```
fixtures/*.jsonl --ReadOnlyAdapter--> raw_records (append-only, per generation, lineage stamped)
raw_records --recon/normalize.py--> stg_crm_contact, stg_crm_deal, stg_student, stg_enrollment, stg_payment
stg_* --recon/er.py--> entity_links + entities(person) + field_lineage
stg_* + entities --rules/*.sql--> invariant_results (per record) --> conflicts (fingerprinted)
```

`stg_*` carry both the raw and the normalized columns (`email_norm`, `first_norm`, `last_norm`,
`dob_norm`, `grade_norm`, `stage_funnel`, …) plus `generation`. Invariants read the **latest generation**
as current state; `field_lineage` retains all generations for oscillation detection (R4/R16).

---

## 4. Entity resolution (deterministic rule cascade)

`canonical_id = uuid5(KEYSTONE_NS, "|".join(sorted(source_refs)))` — stable across runs.

A **source ref** is one of: `crm:contact:<crm_id>`, `crm:deal:<deal_id>`, `appdb:student:<id>`,
`appdb:enrollment:<id>`, `payments:payment:<payment_id>`. **Identity refs** are only the first and third.

**contact ↔ student** — link on the first rule that fires:
- `L1` `contact.external_id == student.id` (hard key)
- `L2` `norm_email(contact.email) ∈ {norm(student.guardian_email), norm(student.guardian2_email)}`
   **AND** `(first_norm, last_norm)` equal
- `L3` `(first_norm, last_norm, dob_norm)` equal, both `dob` non-null

`L2` requires the name match precisely because siblings share the guardian email. A candidate pair is
rejected if either side is already `L1`-linked to a different record (hard keys win).

**payment ↔ person**: `P1` `external_ref == student.id`; `P2` payer email ∈ household guardian emails
**AND** `norm_name(metadata.student_name)` equals the person's `(first_norm, last_norm)`;
`P3` payer email ∈ guardian emails **AND** the household has exactly one child.
No other payment attribution is made — an unattributable payment is C2, not a guess.

**deal ↔ person**: `D1` `enrollment.crm_deal_id == deal.deal_id` for the person's enrollment;
`D2` `deal.associated_contact_ids` contains the person's `crm_id`.

**Survivorship** (canonical field values): app DB > CRM > payments for identity fields; payments
authoritative for money; most-recent generation breaks ties. Survivorship never suppresses a conflict —
conflicts are computed from the *sources*, not from the survived value.

Fuzzy similarity (splink / Jaro-Winkler) contributes **evidence signals only** and never a link decision.

---

## 5. Conflict catalogue

`entity_refs` for every conflict = **sorted** set of refs per the per-type spec below. The harness matches
a detected conflict to a golden entry on `(type, tuple(sorted(entity_refs)))`. Generator and detector build
this list with the same helper (`recon/reference.py:conflict_refs`).

`fingerprint = sha256(type | ",".join(sorted(entity_refs)) | ",".join(sorted(disagreeing_fields)) | ",".join(sorted(observed_values)))`

| id | rule | type | min | detection (on the latest generation) | entity_refs | FP guard that must hold |
|---|---|---|---|---|---|---|
| C1 | `R-001` | paid-but-no-deal | 500 | person has ≥1 `paid` payment **and** ≥1 enrollment, but 0 linked CRM deals | identity refs | the ≥3,000 deal-less leads have **no** payment and **no** enrollment |
| C2 | `R-002` | payment-with-no-person | 200 | payment links to no person by `P1..P3` | `payments:payment:<id>` | every legit payment satisfies one of `P1..P3` |
| C3 | `R-003` | duplicate-by-email (in-source) | 300 pairs | two CRM contacts, same generation, equal `email_norm` **and** equal `(first_norm,last_norm)` **and** (`dob` equal or either null) | the two contact refs | **siblings share the email but differ in name** → not flagged |
| C4 | `R-004` | same-person-different-emails | 250 | contact↔student linked **only** by `L3`, and `email_norm(contact)` ∉ student's normalized guardian emails | identity refs | gmail dot/`+alias` variants normalize equal → clean pairs link by `L2` |
| C5 | `R-005` | record-in-one-source-only | 400 | student with `status ∈ {enrolled, active}` and **no** linked contact and **no** linked payment | identity refs | `prospect`/`applied`/`withdrawn` students are exempt |
| C6 | `R-006` | field disagreement | 500 | linked person where any **non-sensitive** compared field disagrees after normalization. Compared: `grade`, `stage` (enrollment funnel vs deal funnel), `state`, `lifecycle_stage`↔`status` | identity refs | dirt (case/whitespace/`Grade 4`) normalizes away; unknown enum → `unchecked`, not a conflict |
| C7 | `R-007` | enrolled-but-unpaid | 300 | enrollment `stage_funnel ∈ PAID_IMPLYING_STAGES` (or `deposit_paid_at` non-null) with no linked `paid` payment of type `deposit|tuition` | identity refs + `appdb:enrollment:<id>` | `prospect/applied/waitlisted` enrollments exempt |
| C8 | `R-008` | dropped sibling | 150 | household (≥2 children) where **exactly one** child is absent from **exactly one** source in which *all* other children are present | dropped child's identity refs | children with `grade < K` are excluded (legitimately pre-enrollment); clean households share a presence mask |
| C9 | `R-009` | stale pointer | 100 | `enrollment.crm_deal_id` names a non-existent deal, **or** a deal whose person ≠ the enrollment's person | identity refs + `appdb:enrollment:<id>` | null `crm_deal_id` (~40%) is not a conflict |
| C10 | `R-010` | merge-collapsed record | 50 | one CRM contact whose `("ext")` key and `("namedob")` key resolve to **two different** students | contact ref + both persons' identity refs | normal contacts resolve to one student, or to none by a given key class |
| C11 | `R-011` | duplicate payment | 50 | same `payment_id` twice in a generation, **or** same `(payer_email_norm, amount_cents, type)` within ±10 min | the two payment refs | legit repeat payments differ in type or are >10 min apart |
| C12 | `R-012` | wrong-amount payment | 100 | `amount_cents` ≠ fee-schedule amount for `(program, type)` | identity refs + payment ref | schedule is exact; program taken from `metadata.program` or the linked enrollment |
| C13 | `R-013` | refund not reflected | 100 | payment `status = refunded` while the person's enrollment `stage_funnel ∈ PAID_IMPLYING_STAGES` and student `status ∈ {enrolled, active}` | identity refs + payment ref + enrollment ref | correctly-reflected refunds move the enrollment to `refunded`/`withdrawn` |
| C14 | `R-014` | sensitive-field-only fix | 50 | a linked person whose **entire** disagreeing-field set ⊆ `SENSITIVE_FIELDS` | identity refs | — |

### Precedence (committed in `recon/reference.py:PRECEDENCE`, imported by generator *and* detector)
1. **C14 over C6** — if a person's disagreeing fields are *entirely* sensitive, the conflict is C14 and
   `R-006` must not also emit C6 for that person. Mixed sets emit C6 only, with the sensitive field
   listed in `disagreeing_fields` (the proposal is still `sensitive_hold`).
2. **C10 over C6/C4** — a merge-collapsed contact suppresses C6 and C4 for the same contact.
3. **C2 over C12/C11** — an unattributable payment cannot have a wrong amount or a duplicate partner.
4. **C5 over C1/C7** — a single-source student cannot also be paid-but-no-deal.
5. Everything else co-occurs freely; ≥10% of planted conflicts are compound (A.5) and appear as
   **multiple golden entries for the same entity, one per type** — matched per-entry, never double-counted.

### Records with no applicable invariant (R8)
Every `stg_*` row is stamped in `invariant_results` for every rule whose scope includes it. A row in
scope of **zero** rules gets one synthetic row `(rule_id='R-000', verdict='unchecked')`. An unmappable
enum value yields `verdict='unchecked'` with the reason in `detail`. Neither is ever a crash and neither
is a conflict.

---

## 6. Sensitive fields (normative — auto-apply forbidden, R15/R24)

Classification is a **pure function of the target field path**, evaluated *before* confidence:

```
SENSITIVE_FIELDS = {
  # legal / identity
  "crm.contact.first_name", "crm.contact.last_name", "crm.contact.dob",
  "appdb.student.first_name", "appdb.student.last_name", "appdb.student.dob",
  "appdb.student.student_number",
  # billing ownership
  "payments.payment.payer_email", "payments.payment.payer_name",
  "appdb.enrollment.billing_owner_email",
  # financially-consequential status
  "appdb.enrollment.stage", "appdb.enrollment.deposit_paid_at",
  "appdb.student.status", "payments.payment.status",
  # consent / compliance
  "crm.contact.marketing_consent", "appdb.student.communication_opt_out",
}
```

Eligible for auto-apply (stretch #7) when confidence ≥ 0.95 **and** the case type is approved **and**
evidence is complete: non-sensitive linkage (`appdb.enrollment.crm_deal_id`,
`payments.payment.external_ref`, `crm.contact.external_id`), routing (`crm.deal.pipeline`,
`crm.deal.stage`), and non-identity formatting (`crm.contact.grade`, `crm.contact.state`,
`crm.contact.lifecycle_stage`). **Name/DOB formatting is not on this list** — any write to a name or DOB
field is sensitive regardless of intent, because sensitivity is by field, not by motive.

---

## 7. Generations, oscillation, malformed payloads

- **3 generations** per source. Gen 1 = baseline. Gen 2 = corrections + arrivals (some conflicts resolve,
  some appear). Gen 3 = current state; **≥25 fields re-assert their gen-1 value that the app DB
  disagrees with** (A→B→A).
- **Golden set describes generation 3** (current state). Entries carry `"oscillating": true` where the
  conflict's field oscillated.
- Oscillation detection: window scan of `field_lineage` per `(canonical_id, field)` for the pattern
  `A, B, A` across ascending generations. On oscillation the conflict is marked
  `escalated:oscillation`, and the reconciler **must not** re-propose the identical fix (R16).
- **Malformed payloads** live in `fixtures/malformed/cases.jsonl`, one JSON object per line:
  `{case_id, source, entity_type, expect_code, raw}` where `raw` is the **literal payload string** (so
  truncated JSON is representable). ≥20 cases covering: missing required field, wrong type, truncated
  JSON, unknown enum, null PK, duplicate PK, and one oversized body (> `MAX_PAYLOAD_BYTES`). Each must
  produce the documented `4xx` + structured log, never a 500 and never a silent skip. Malformed cases are
  **excluded** from every count in §5.

---

## 8. Emitted artifacts

```
fixtures/manifest.json                 profile, seed, per-file sha256, all counts
fixtures/{crm,appdb,payments}/gen{1,2,3}/*.jsonl
fixtures/malformed/cases.jsonl
golden/conflicts.json                  [{type, rule_id, entity_refs[], sources_involved[],
                                         disagreeing_fields[], observed_values{}, expected_verdict,
                                         compound_with[], oscillating}]
golden/clean-sample.json               1,000 entities asserted conflict-free (identity refs)
golden/expected-views.json             ≥25 hand-checkable unified entity views (the join contract, R10)
golden/manifest-summary.json           counts per conflict type + every A.4/A.5 structural minimum
```

Every JSON file is written with `json.dumps(obj, sort_keys=True, ensure_ascii=True,
separators=(",", ":"))` and a trailing newline. JSONL lines are individually sorted-key encoded and the
files are emitted in a fixed, sorted record order. **Two runs at the same seed must be byte-identical**
(`sha256` of the whole tree compared by the determinism check).

## 9. Profiles

`--profile dev` (~5,000 records) and `--profile full` (the graded ~100,000-record Appendix-A dataset)
share one code path; the profile scales volumes and every conflict minimum by the same ratio (dev floors
at 5 per class so all 14 classes stay exercised). **All gates, benchmarks, and the committed `golden/`
files are `full`.** The manifest self-check asserts the Appendix-A minimums whenever `profile == full`.
