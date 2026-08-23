# Keystone — proposal policy

**What this document is.** The lifecycle of a proposal from detection to reversal, and the
conditions under which Keystone will write to its own canonical layer without a human pressing the
button. It describes **only controls that exist in this repository** and names, for each one, the
code or the migration that enforces it. Where a control is absent, it says so under
[§8 Known gaps](#8-known-gaps) rather than being omitted.

Authorities, in order: `docs/project_1785195830421.pdf` (the brief), `docs/SPEC.md` (R11, R13–R16,
R24), `docs/invariant-contract.md` §6 (the sensitive/eligible field sets and the committed fix
targets), `docs/DESIGN.md` (§HTTP API, §Holds-before-writes enforcement).

---

## 1. The lifecycle

```
  detection            decision                 write                 reversal
 ───────────      ─────────────────      ─────────────────      ─────────────────
  conflicts   ->   pending          ->    approved   ->   applied   ->  rolled_back
                   sensitive_hold   ->    rejected
                   (born, never decided)
```

| stage | who | role | enforced by |
|---|---|---|---|
| propose | `recon.reconciler` | `recon_writer` | born `pending`/`sensitive_hold` only, SQLSTATE `KS002` |
| decide | a human, via `POST /api/proposals/{id}/approve` \| `/reject` | `review_writer` | `pending\|sensitive_hold -> approved\|rejected`, and only with a non-null `decided_by`/`decided_at`, SQLSTATE `KS004` |
| apply | `recon.apply.apply_proposal` (a reviewer pressed apply) or `recon.apply.auto_apply` (R24) | `apply_writer` | `approved -> applied`, SQLSTATE `KS004`; the write itself needs a cited, same-transaction ledger row, `KS001`/`KS010`/`KS011` |
| reverse | `recon.apply.rollback_proposal` | `apply_writer` | `applied -> rolled_back`, and the write back must equal the value the apply captured, `KS012` |

Three Postgres roles, three duties, and **no role holds two of them**. `recon.db.role_connection`
authenticates *as* the role (never `SET ROLE`, which is reversible from inside the session and
leaves the connection owned by the schema owner, who bypasses grants). The consequence that is
actually graded: **the automation cannot approve its own work** — `apply_writer` has no transition
into `approved` at all.

A proposal's payload is frozen at birth: `conflict_id`, `fingerprint`, `action`, `confidence`,
`evidence`, `sensitive` and `target_canonical_id` cannot be changed after INSERT (`KS005`). The row
a reviewer reads is the row that gets applied.

## 2. What a proposal may contain

`proposals.action` is a closed vocabulary — exactly `{"set": {path: value, …}}`
(`ck_proposals_action_vocabulary`, migration 0007), with `{"set": {}}` for the evidence-only
proposals of contract §6. There is no other shape, so "what would this write?" is answerable from
the row without interpreting a free-form document.

`proposals.evidence` is the packet `recon.reconciler` assembled — schema `keystone.evidence.v1` —
carrying the conflict, the identity signals, the completeness reasons, the chosen fix and the whole
confidence derivation including the model version and the sha256 of the model file. A score whose
inputs are not recorded is not inspectable (R14), so the inputs travel with the score.

## 3. The sensitive-field rule (R15)

> A proposal touching a sensitive field can never auto-apply at any confidence, and is forced to
> human review and logged as `sensitive_hold`.

R15 names two different things and the distinction is the whole of this section:

* **what kind of conflict is this?** — answered by classifying the conflict's committed *fix target*.
  `SENSITIVE_FIELDS` **is** the classifier: legal name, DOB, government/student id, billing- and
  payer-identity, financially-consequential status, and consent/compliance flags. Contract §6 pins
  the committed fix target of every conflict type, which is what makes the classifier total and
  decidable, and `recon.sensitive.classify` is a pure function of that target path.
* **what will this proposal WRITE?** — answered by reading the **paths `action->'set'` effectively
  writes**: every path the shallow merge would change, including the ones reached *through* a nested
  object, and never the top-level keys the action happens to name. This is the question R15 actually
  forbids an answer to, and it is *not* the first question. A proposal's action is a stored column;
  it is not derived from the classification and nothing before migration 0012 required the two to
  agree.

They came apart exactly as they can. A proposal on a `C2` conflict — an approved case type whose
committed template writes `payments.payment.external_ref` — carrying
`action = {"set": {"crm.contact.email": "…"}}` at confidence 0.99 was classified *not sensitive*,
because C2's fix target is eligible, and was **auto-applied**. Every condition the gate evaluated
held; none of them had looked at what the statement writes. `crm.contact.email` is in
`SENSITIVE_FIELDS` — contract §12 D-7 classifies the billing email sensitive precisely so a C4
cannot be re-pointed to escape the classifier.

They then came apart a **second** time, one level down, and the first repair did not cover it.
`entities.current` is a flat object carrying one nested object, `survived`
(`recon.resolve.SURVIVED_PATHS`), whose nine members are themselves source-qualified contract paths
and six of which are in `SENSITIVE_FIELDS`. So this row was accepted with `sensitive = false`,
`status = pending`:

```json
{"set": {"survived": {"crm.contact.email": "…", "appdb.student.status": "…",
                      "appdb.enrollment.stage": "…", "…the other six carried…": "…"}}}
```

It has exactly **one** write-set key — `survived` — which is in neither committed set, so a gate
reading top-level keys judged that key, and migration 0012's `jsonb_exists_any` over the top-level
keys saw nothing either. What it *writes* is three sensitive paths. Judging the key list is the same
shape of mistake as judging the conflict's classification instead of the write.

Both questions are now asked, in that order, and **neither gate can see a score**:
`recon.apply.sensitivity_gate` classifies the conflict, `recon.apply.write_set_gate` judges
`recon.apply.effective_write_paths` — the paths, not the keys — and `auto_apply_decision` returns at
whichever refuses first, before any expression has read `proposals.confidence`.

**What "effectively writes" means, exactly — and it is a rule about the VALUE, not about the
statement.** The write set is

> the difference between the row the merge would **produce** and the row as it stands — every leaf
> path whose value would differ, one level deep — **plus** every top-level key the action names with a
> non-object value, which is written whether or not it changes.

Per key the action names (and only those, because `||` is shallow and leaves every other key exactly
as it was), the two clauses are independent and a key can satisfy both:

* the action assigns a **non-object** there ⇒ the top-level key is written. No comparison, no look at
  the row: its author could have omitted the key, so naming it is writing it;
* **either side is an object** ⇒ the two memberships are compared, and every member the merge would
  change, ADD or DROP is written as `container->leaf`. A member carried through unchanged is not. A
  non-object side contributes no members;
* an assigned object the merge would change nothing about reports the container key itself, so a
  refusal names something real instead of an empty write set condition 8 would then wave through.

The asymmetry between *naming* a top-level key and *carrying* a nested member is deliberate: §5's
shallow-merge rule gives a nested member's author no choice — a fix that writes one member of a map
must carry the **whole** map, and counting the carried siblings would make every possible write to
`survived` a sensitive write and no member of it could ever be fixed, including the one eligible path
that lives there.

**Stating it over the value rather than over the shape is the third repair of one mistake.**
`merge_preview` required *both* sides to be objects, so `{"set": {"survived": "wiped"}}` — a scalar
erasing a nine-key map — was reported *safe*. `effective_write_paths` then required the *assigned*
side to be one, so the same action reported the single unlisted leaf `survived` and the erasure of six
`SENSITIVE_FIELDS` members was invisible to the gate, to migration 0012's key-level CHECK, and to
`KS013` (whose `nested_only` half emitted nothing at all for it). **The most destructive form of the
attack `KS013` exists to stop was the one form it did not judge, and such a proposal was accepted with
`sensitive = false, status = pending`.** Migration 0014 rewrites the SQL to the rule above; a list, a
scalar, a JSON `null`, an absent key and an object are now one comparison instead of four branches,
and `tests/apply/test_nested_write_set.py` asserts the Python and the SQL equal over a **generated
cross product of every shape on both sides**, in both `nested_only` modes, rather than over the ten
hand-picked pairs it used to — a list that omitted exactly the shape that turned out to be the hole.

**One level, and what is below it.** `entities.current` has exactly one nested level, so both
implementations descend exactly one. A doubly-nested action
(`{"set": {"survived": {"x": {"crm.contact.email": …}}}}`) presents the leaf `x`, which is on neither
committed list: R24's allow-list refuses it and §5's shallow-merge guard refuses it again for erasing
the other eight members. The deeper case is covered by being **refused**, not by being *inspected* —
which is the honest way round while no committed shape reaches there, and is stated so the next
reader does not mistake "at any depth" for a property this code has.

**A member the map did not have is refused too, and by a different control.** `survived`'s membership
is the closed set `recon.resolve.SURVIVED_PATHS`, and every reader projects the map **whole**. So an
action that carries all nine genuine members and ADDS a tenth whose key differs from a real one only
by case (`CRM.contact.email`), by surrounding whitespace, or by a unicode homoglyph (a dotless i, a
Cyrillic е) erases nothing, destroys nothing, and would render the attacker's value beside the genuine
one under a name a human reads as real. R24 refuses it — the added leaf is on neither committed list,
so it is `write_off_allowlist`, which is what eligibility being an **allow-list** rather than the
complement of `SENSITIVE_FIELDS` buys — but **a reviewer pressing APPLY is not behind R24's gate**. So
`merge_preview` reports it as `introduced` and `apply_proposal` refuses it on both paths
(`nested_member_introduced`), in exactly the place and for exactly the reason §5's erasure guard sits
beside it. This one is **code only**; §8.11 says so rather than implying a database backstop.

Four things make "classification wins over confidence" structural rather than aspirational:

1. **`recon.sensitive.classify` cannot see a score.** It takes a conflict type and a set of
   disagreeing paths. There is no confidence parameter and no threshold anywhere in the module.
2. **`recon.apply.sensitivity_gate` cannot see a score either**, and it runs first. On the
   sensitive arm `auto_apply_decision` returns before any expression has read the proposal's
   confidence — proved, not asserted: `tests/apply/test_structural_order.py` hands the gate a
   proposal whose `confidence` attribute *raises on access* and the refusal still lands.
3. **`recon.apply.write_set_gate` cannot see a score either**, and it runs second — still ahead of
   everything in R24. It takes the assignments and, optionally, the canonical row they would be
   merged onto: no conflict, no classification, **no confidence**. That row is what distinguishes a
   nested member the statement replaces from one it carries; it is not a score, and its absence can
   only *widen* the write set — every branch of `effective_write_paths` is conservative when it is
   `None`, so a caller who forgets it cannot thereby admit anything. `apply_proposal` re-asks the
   same question against the row **under its `FOR UPDATE` lock** before an unattended write lands
   (`write_set_refused_under_lock`), so a canonical row that moved between the gate's read and the
   lock cannot turn a carried sibling into a replacement unseen.
4. **The 0.95 comparison is behind a token that needs BOTH verdicts.** The only function that reads
   `AUTO_APPLY_CONFIDENCE_FLOOR` takes an `EligibilityClearance`, and an `EligibilityClearance`
   cannot be constructed around a sensitive classification *or around a refused write set*. Skipping
   either gate is not something a caller can do by forgetting a line.

Three independent facts each hold a proposal on their own — the re-derived classification, the
stored `proposals.sensitive` column, and a `sensitive_hold` status — so one corrupted column is not
enough to unlock a write.

**The database backstops both pairings now.** `KS002` (migration 0005/0006) binds `sensitive` to the
birth *status*: `sensitive = true` born in anything but `sensitive_hold` is refused.
`ck_proposals_sensitive_covers_write_set` (migration 0012) binds `sensitive` to the *write set*:

```sql
sensitive OR NOT jsonb_exists_any(coalesce(action -> 'set', '{}'), <contract §6's paths>)
```

A proposal may not claim `sensitive = false` while naming a `SENSITIVE_FIELDS` path in its action.
Chained with `KS002`, **writing a sensitive path forces the hold** as a property of the table rather
than of two Python call sites. Until 0012 this direction of R15 had no backstop at all — the row
`{"sensitive": false, "status": "pending", "action": {"set": {"crm.contact.dob": …}}}` was accepted
by every committed constraint, and `tests/reconciler/test_reconcile_run.py` pinned that as a
measurement so it could not be quietly overstated. Its scope is stated honestly in §8.4.

**Migration 0013 extends that binding from keys to paths, on both legs, and migration 0014 restates
the rule they share over the merged value (§3).** A CHECK cannot see the row the action would be
merged onto, so it cannot tell a replaced nested member from a carried one; two triggers can:

| trigger | fires on | SQLSTATE | refuses |
|---|---|---|---|
| `keystone_proposals_nested_write_set` | `BEFORE INSERT ON proposals` | `KS013` | `sensitive = false` when the effective write set — computed against the target entity's stored `current` — lands on a §6 sensitive path **reached through a nested object** (the top-level half stays with 0012's CHECK) |
| `keystone_auto_apply_write_set` | `BEFORE INSERT ON proposal_events` | `KS014` | an `applied` event whose `actor` is the unattended actor (`system:auto-apply`) and whose own `before`/`after` differ on a §6 sensitive path — top-level, or one level inside a nested object |

`KS013` judges the **nested half only**, on purpose: a BEFORE trigger runs ahead of a CHECK, so
judging top-level keys there would take 0012's own refusals away from it and re-badge them, leaving a
VALIDATED table invariant untested and its constraint name absent from the error a caller sees. Each
rule keeps the half it can decide.

`KS014` is the one that needs nothing else to be true: it reads the two columns of the ledger row
itself — no join, no trust in the proposal, no re-derivation — and `KS010`/`KS011` already bind that
row to the canonical write. It is keyed on the **unattended** actor because R15 forbids the machine
writing a sensitive field, not the human: a reviewer may approve a `sensitive_hold` proposal and
apply it by hand, and a trigger that refused that would delete §6's entire C4 fix template.

**Migration 0014 restates that shared rule over the merged value.** `keystone_effective_write_paths()`
(SQL) and `recon.apply.effective_write_paths()` (Python) are two implementations of one rule, so
`tests/apply/test_nested_write_set.py` asserts them **equal** over a generated cross product of every
shape on both sides — absent, JSON `null`, boolean, number, string, empty list, list, empty object,
and objects that add / drop / change / carry a member — in both `nested_only` modes, rather than over
a hand-written list. A drift is a hole in whichever is more permissive, and nothing else in the suite
would see it. The previous list of ten pairs was green while omitting the one shape that was the hole. The same file drops each trigger inside a rolled-back transaction and
shows the row landing again, so neither is a no-op; and
`tests/apply/test_gate.py`'s `(approved case type × sensitive path)` cross product is re-run there
*through* the nested container, because "refused as a key" and "refused as a path" were two different
facts and only the first was true.

**A held proposal is not forbidden forever.** R15 forces it to *human review*: a reviewer may
approve a `sensitive_hold` proposal and then apply it, and that act is signed, dated and attributed
in `proposals.decided_by` and in the audit log. What can never happen is the machine taking it
unattended. `POST /api/proposals/{id}/apply?auto=true` refuses such a proposal at every confidence
including 1.0, and the refusal body names the single condition it evaluated.

**Measured consequence, and what it is evidence OF.** All 250 C4 proposals and all 50 C14
proposals in the graded store are `sensitive_hold`, together with 80 mixed C6 — 380 in total — and
none of them writes a path outside `SENSITIVE_FIELDS`. That is a **property of the current dataset**,
measured over `tests/apply/test_sensitive_hold.py`'s SQL-selected population. It is *not* the reason
a hostile proposal is refused, and reading it as one is the mistake this section previously made: the
number 380 says what the committed seed happens to produce, and a seed change could move it without
any control having weakened.

The guarantee is the two gates above and the 0012 constraint, and it is proved by **constructing**
the hostile proposal rather than by surveying the store —
`tests/apply/test_gate.py` builds every (approved case type × sensitive path) pair, 60 proposals that
exist in no fixture, and asserts each is refused; `tests/apply/test_structural_order.py` re-runs the
forbidden write sets with the record's `confidence` attribute rigged to raise on access, so the
refusal is proved to happen without a score being read; `tests/apply/test_write_set_backstop.py`
hand-INSERTs a row per sensitive path and asserts the database refuses it by constraint name. Each
of those files also carries the no-op control that keeps the negatives from passing vacuously.

## 4. The auto-apply gate (R24)

`recon.apply.auto_apply` is a **separate function** from the manual apply path, deliberately: the
gate is the deliverable, and a gate reached through an `if auto:` branch inside the manual path is a
gate that a later edit to the manual path can widen without touching anything that looks like a
gate.

It fires only when **every** one of these holds. The order is the order below; the first two are
each allowed to be sufficient, and the decision returns at the first one that refuses.

Conditions 1 and 2 are the two halves of R15 and they read **different columns**. Read the third
column of the table as the answer to "which column of the row decided this?" — that is the
distinction the previous version of this document blurred, and blurring it is what let a write to
`crm.contact.email` through (§3).

| # | check name | condition | what it reads | how it is decided |
|---|---|---|---|---|
| 1 | `not_sensitive` | **the CONFLICT is not sensitive** | `conflicts.type`, `conflicts.disagreeing_fields`, `proposals.sensitive`, `proposals.status` | `sensitivity_gate` → `recon.sensitive.classify`, which resolves the conflict type to its committed §6 fix target and classifies *that path*. Any of the four facts holds the proposal on its own. Returns before condition 2 is considered. |
| 2 | `write_set_eligible` | **every path the ACTION WRITES is permitted** | `proposals.action->'set'` and the target `entities.current` — nothing else | `write_set_gate` over `effective_write_paths`: every path the merge would change, **including one reached through a nested object**, never the top-level key list. Refuses `sensitive_write` if any lands in §6's `SENSITIVE_FIELDS`; refuses `write_off_allowlist` if any is absent from `AUTO_APPLY_ELIGIBLE`. **Eligibility is an allow-list**: a path in *neither* set is refused, never admitted by default. An empty write set clears this and is refused by condition 8. |
| 3 | `approved_case_type` | **approved case type** | `conflicts.type` | `AUTO_APPLY_CASE_TYPES`, *derived* from contract §6's fix-target table (`FIX_TARGETS[t].classification == "eligible"`), not restated. Today: `C2`, `C6`, `C9`. |
| 4 | `target_on_allowlist` | **the CLASSIFICATION's target path is on the allowlist** | the classification from condition 1 | `Classification.auto_apply_eligible_path`. This is a statement about the *committed fix target of the conflict type*, i.e. what §6 says a proposal of this type is supposed to write — **not** about what this proposal does write. Condition 2 is the one that reads the action. Both are kept: 4 catches a conflict whose type has no eligible template, 2 catches an action that departs from it. |
| 5 | `write_matches_fix_target` | **the ACTION writes the committed fix target and nothing else** | the classification from 1 and the write set from 2 | every effective write path must equal `FIX_TARGETS[type]`'s path for this conflict's shape. Conditions 2 and 4 both passed a `C2` carrying `{"set": {"crm.contact.grade": "7"}}` — an eligible, non-sensitive path, on an approved type whose *own* committed template writes `payments.payment.external_ref`. An eligible path the template does not write is still a re-targeting: it is how a conflict of one type acquires another type's fix. |
| 6 | `confidence_floor` | **confidence ≥ 0.95** | `proposals.confidence` | `AUTO_APPLY_CONFIDENCE_FLOOR`, a `Decimal`, compared against `numeric(5,4)`, so exactly 0.9500 passes deterministically. Unreachable unless 1 and 2 both cleared — see §3's point 4. |
| 7 | `complete_evidence` | **complete evidence** | `proposals.evidence` | the packet reports no `incomplete_sources`, no `null_observed_values`, no `partial_evidence` signal and no `partial_evidence_reasons`. A packet of an unknown schema is refused rather than assumed complete. |
| 8 | `writes_a_field` | **writes a field** | `action->'set'` | non-empty. Contract §6's evidence-only proposals write nothing and are escalated for human review, never applied. |
| 9 | `rollback_path` | **known rollback path** | `entities`, `proposal_events` | the target `entities` row exists now, and both single-use citation legs are unspent, so the write *can* be reversed. |
| 10 | `status_appliable` | **status is `approved`** | `proposals.status` | `apply_writer` may only move `approved -> applied` (`KS004`). |

The conditions are identified by **name**, not by number: the audit row, the API body and the
tests all carry `check`, and adding condition 5 renumbered four of them.

**Conditions 1 and 2 are not redundant, and neither implies the other.** A `C4` conflict is held by
1 whatever its action says. A `C2` conflict carrying a `crm.contact.email` write clears 1 — C2's
committed target is `payments.payment.external_ref` and is eligible — and is refused by 2. The
system shipped condition 1 alone, and that is the whole of the defect §3 describes.

The verdict is a value, not a boolean: `AutoApplyDecision` carries every condition and the number
that decided it, it is written to the audit log on refusal as well as on acceptance, and
`GET /api/proposals/{id}` returns it — so "why is this one not applied automatically?" is answerable
from the reviewer's screen.

**Scope of "auto".** Condition 10, `status_appliable`, says an auto-apply still acts on a proposal a
human approved. That
is not a softening of R24, it is the three-role boundary: `apply_writer` has no arc into `approved`,
so "the machine approves and applies" is not representable, and widening that grant is precisely the
change the boundary exists to prevent. Auto-apply here means **the machine may take the approved
write unattended**, and the gate is what decides whether it may. This is stated because it is the
one place where the implementation is narrower than a fast reading of R24.

**Never a source**, argued two ways because a structural argument and a measurement are different
claims. The write targets `entities` and nothing else (`recon.apply.WRITABLE_TABLES`).

* *Structural.* The sources are files behind `recon.adapters.base.ReadOnlyAdapter`, whose Protocol
  has no write member and whose implementations may not carry a write-shaped attribute anywhere in
  their MRO **or on the instance** (a writer bound in `__init__` appears on no class).
  `recon.apply.assert_sources_are_unwritable()` checks that on the adapter objects and returns their
  class names. **It is exactly as exhaustive as `WRITE_NAME_TOKENS` is, and that is a substring list,
  not a decision procedure**: an adapter with `def persist(...)`, `def commit(...)`, `def flush(...)`
  or `def sync(...)` carries no listed token and passes it. It is the cheap structural arm of three
  — the port having no write member is the first, `source_tree_digest()` measuring the bytes across a
  real committed apply is the third — and reading it as the guarantee is the same over-claim this
  document has had to withdraw twice. It also no longer *crashes* on a `__slots__` adapter: the
  instance walk called `vars(adapter)`, which raises `TypeError` on an object with no `__dict__`, so
  the one adapter shape that most resembles deliberate concealment produced the wrong error from the
  wrong rule instead of the named `AssertionError`. It previously iterated the *dict* `build_adapters` returns — which yields its keys —
  so it introspected the three strings `"crm"`, `"appdb"`, `"payments"` and returned
  `("str", "str", "str")`: an assertion advertised as executed, executing nothing. It now refuses
  anything that is not a read-only adapter and refuses an empty adapter set, and the test asserts the
  three class names rather than merely that three things were checked.
* *Measured.* `recon.apply.source_tree_digest()` sha256s every file of the fixture tree.
  `tests/apply/test_apply_lifecycle.py` takes it before and after a **real committed auto-apply and
  rollback**, asserts the canonical row actually moved in between, and asserts every file digest is
  unchanged. Reasoning about the port cannot see a stray `open(…, "w")` elsewhere in the process;
  this can.

`tests/apply/test_merge_shape.py` sabotages an adapter six ways — a write method, a write bound onto
the instance, the dict's keys standing in for adapters, an empty adapter set, and a `__slots__`
adapter with and without a write-shaped slot — and proves the assertion goes red for each and stays
green for the clean one.

### What an auto-applied write is observable IN

R24 says auto-apply "applies only to Keystone's canonical layer". It does — and **every write it
admits is still invisible to every reader**. That is a different failure from an unsafe one, and no
test that asserts "the row moved" can catch it. This section says exactly what is observable and what
is not; §8.10 says why the *unattended* half of the gap cannot be closed without a model change or a
contract change, and gives the number rather than an impression.

`AUTO_APPLY_ELIGIBLE` is written in source-qualified paths; `recon.resolve.VIEW_FIELDS` is the key set
the entity **projection** is built from. **They share no member.**

**Who the readers actually are.** This paragraph used to call `VIEW_FIELDS` "the exact key set every
reader projects … the object the dashboard renders", and the last clause was false. There are exactly
two readers:

| reader | what it is |
|---|---|
| `recon.api.entities._view_of` | the body of `GET /api/entities` and `GET /api/entities/{key}` |
| `recon.suite.golden` | the R10 join check, which diffs that same projection against the committed `golden/expected-views.json` |

`golden/expected-views.json` is the *shape they are checked against*, not a third reader. **The
dashboard is not a reader of it at all**: `dashboard/src/lib/httpClient.ts` calls `/api/conflicts`,
`/api/proposals`, the three decision verbs and `/api/scorecard`, and never touches the entities
endpoint. Naming it here overstated who would notice an invisible write — the same class of error as
the other five in §8, made on the observability claim rather than on a control.

So an auto-apply of `{"set": {"crm.contact.grade": "7"}}` adds a NEW TOP-LEVEL KEY to
`entities.current` that neither reader projects: the row moves, the digest moves,
`tests/apply/test_apply_lifecycle.py` goes green, and not one value the entity endpoints or the golden
view show has changed.
`tests/apply/test_observable_auto_apply.py::test_the_top_level_form_would_still_be_invisible`
measures exactly that on a real committed proposal: it merges the action the way `KS010` requires and
asserts `_view_of(after) == _view_of(before)`.

**Exactly one eligible path already lives in the view**, and it is not a new one:
`crm.contact.lifecycle_stage`, a member of the nested `survived` map that `VIEW_FIELDS` does project.
It is §6's committed fix target for a lifecycle-only C6 — "eligible (CRM side only)" — so nothing is
widened to reach it. What changes is the *shape* of the write: the canonical layer keeps that field
inside `survived`, so the observable form of the fix writes it **there**, carrying the whole map per
§5, instead of beside it. That form is representable only because the gate now judges paths rather
than keys; under the old rule it was refused as `write_off_allowlist` for naming `survived`.

**`recon.reconciler` now EMITS that form**, so the observable write is the shipped pipeline's and not
a test's. `NESTED_FIX_TARGETS` is derived, not restated — `SURVIVED_PATHS ∩ AUTO_APPLY_ELIGIBLE`,
today exactly `{crm.contact.lifecycle_stage}` — and the template nests only when it holds the
entity's own `survived` map **and** that map already carries the member. Without the map it falls
back to the top-level form (a template that cannot see the map cannot carry it, and guessing the other
eight members would author an erasure); if the map lacks the member it falls back too, because adding
one is the look-alike hole §3 describes. Measured on the graded store: **120** of the 3,050 proposals
come out in the nested form, each writing exactly one effective path,
`survived->crm.contact.lifecycle_stage`. `AUTO_APPLY_ELIGIBLE` was **not** widened and no sensitive
path was made eligible.

**A held target that also lives in `survived` keeps the top-level form.** `appdb.enrollment.stage`,
`appdb.student.status` and `crm.deal.stage` are members of the map too and all three are in
`SENSITIVE_FIELDS`, so no proposal targeting them can auto-apply at any confidence: re-shaping them
would buy no observability R15 lets the machine use, while moving the shape of the 380 committed
`sensitive_hold` rows §3 measures. The intersection with the *eligible* set is the whole rule.

**Making the write visible exposed a wrong VALUE, and it is repaired here.** §2.4's `lifecycle` row
compares `LIFECYCLE_TO_FUNNEL(crm.contact.lifecycle_stage)` against
`STATUS_TO_FUNNEL(appdb.student.status)`, and §5.4 records the *comparison's* values in
`observed_values` — so the authoritative endpoint there is a **funnel** token (`enrolled`) while the
field being written holds a **CRM** one (`customer`, `SQL`, `opportunity`). The template wrote the
funnel token straight onto the CRM field, and `norm_enum('lifecycle_stage', 'enrolled')` is `None`:
the next comparison of that field would be `unchecked` (§5.1) and the conflict would disappear by
becoming unreadable rather than by being fixed. That was undetectable for exactly as long as the write
was invisible. The value is now carried back through the field's own mapper and used only when the
preimage is a **singleton** — `enrolled → customer`; `prospect` and `applied` have three preimages
each and `waitlisted`/`deposit_paid`/`refunded` have none, and an ambiguous or empty preimage makes
the value underivable, exactly as two guardian addresses do for a C4. The same repair applies to
`crm.deal.stage`, whose mapper §2.3 pins as bijective onto the funnel, so it is total there. Measured:
all 120 lifecycle-only C6s carry `enrolled`, so all 120 stay derivable, and the 10 `crm.deal.stage`
proposals (all C14, all held) now name a deal stage instead of a funnel stage.

Proved end to end, committing, in
`test_the_real_proposal_applies_visibly_and_rolls_back`: the row is the reconciler's own,
`review_writer` approves it, `recon.apply.apply_proposal` writes it as `apply_writer` through
`KS010`/`KS011`'s citation, the value is read back **through `_view_of`** — the reader's own
projection, not a re-implementation — the eight sibling members and every other view field are
asserted unmoved, `rollback_proposal` reverses it, and the reader's view is asserted identical to the
one it started as.

**What that demonstration is NOT.** It is the **manual** apply — a human approved it and the machine
wrote it. The unattended path refuses the same row, and §8.10 records why that is structural rather
than a property of this seed.

## 5. The write itself

One transaction, as `apply_writer`:

1. lock the canonical row (`SELECT … FOR UPDATE`) and read `current::text` — the *before* bytes;
2. INSERT the `applied` `proposal_events` row, with `before` and `after` computed **in SQL** from
   that row and the cited proposal;
3. move the proposal `approved -> applied`, which stamps `status_txid` so the deferred trigger can
   tell "being applied now" from "applied yesterday";
4. UPDATE `entities` with the same expression.

The content is not the caller's choice. Since migration 0007 a canonical UPDATE is admitted only
when `NEW.current = OLD.current || (action -> 'set')`, textually as well as semantically; anything
else is `KS010`. Since 0008 the ledger cannot describe a write that did not happen (`KS011`), and at
most one canonical-mutating event per entity per transaction is allowed — which is what makes an
event's `before` provably the value the row held at transaction start.

Nothing is round-tripped through Python. `jsonb` distinguishes `1` from `1.0` as text while
comparing them equal as jsonb, the citation trigger pins both, and `recon.suite.mirror` hashes
`row::text` on a graded determinism path — so a value that has been through `json.loads`/`json.dumps`
is a value that may have changed its bytes.

**A citation is single-use.** One approval authorises exactly one canonical write and one reversal.
Two partial unique indexes on `proposal_events` make a replay impossible rather than merely audited.

### The shallow-merge trap

`entities.current` is a flat object containing one nested object, `survived`
(`recon.resolve.SURVIVED_PATHS`, nine keys). `||` is a **shallow** merge, so an action of
`{"set": {"survived": {"crm.contact.email": …}}}` does not update one survived field — it replaces
the whole map and erases the other eight.

**The guard keys on the shape change, not on both sides being objects.** It used to require *both*
the old and the new value to be Mappings before it looked, so `{"set": {"survived": "wiped"}}` — a
scalar replacing the nine-key map — was reported safe and the apply took the write. That is a
strictly larger loss than the case the guard was built for, admitted because the destruction was more
total. `recon.apply.merge_preview` now reports every sub-key lost when a key stops being an object
(`erased`) and the key itself (`collapsed`), which also catches an empty nested map being replaced by
a scalar — a shape change that erases no *named* sibling.

**A third arm: a member the map did not have.** `safe` is not "erases nothing". `survived`'s
membership is the closed set `SURVIVED_PATHS`, and every reader projects the map **whole**, so an
action carrying all nine genuine members plus a tenth that impersonates one — `CRM.contact.email`,
`"crm.contact.email "`, a dotless-i or Cyrillic-е homoglyph — destroys nothing and would render the
attacker's value beside the genuine one under a name a human reads as real. `merge_preview` reports
it as `introduced`; `apply_proposal` refuses it as `nested_member_introduced`. Bounded honestly: this
is a rule about ADDING to a nested object that already exists. Turning a key that held no object into
one introduces no sibling to be confused with and is judged by R24's write-set gate, which counts
every member such an action carries as a written path.

The committed fix template **does** write `survived` now — §4's observable form, `{"set":
{"survived": {…the whole nine-key map, `crm.contact.lifecycle_stage` replaced…}}}` — so this guard is
a description of current behaviour and not only of the next template. **Both apply paths refuse an
unsafe proposal** — the check lives in `apply_proposal`, the single statement the manual and the
automatic routes both go through, rather than in R24's gate, which is pure and holds no entity value
to merge against. A fix that writes one member of a nested map must carry the whole map, and may not
extend it.

## 6. Rollback

`recon.apply.rollback_proposal` restores the entity to the state the apply captured. The value
written back is `proposal_events.before` of that proposal's `applied` row, copied **column to column
inside the database** — nothing is parsed, re-serialized or reassembled from field values. So
"byte-identical" is a property of the statement, not a claim that the merge is invertible.

It is checked twice: by digest (`sha256` of `current::text`, before the transaction ends) and by the
database at COMMIT — `KS012` refuses a reversal whose cited apply did not leave the value that is
currently on the row, so a rollback can never silently discard a later approved write that has
landed on top.

Reversal is available exactly once per proposal, and only from `applied`.

## 7. Oscillation: dedup or escalate

Two distinct rules, both in `recon.reconciler._skip_reason`, evaluated in this order. Stated
precisely, because the two are easy to blur into a stronger claim than the code makes:

* **Dedup by fingerprint.** If a non-rejected proposal with the same `conflict.fingerprint` already
  exists, the conflict is not re-proposed at all (`skip_reason = fingerprint`). One proposal per
  conflict, idempotent on fingerprint, within a run and across runs.
* **Escalate on oscillation, and never re-propose the identical fix.** If the underlying field
  re-asserted a previous value across generations — contract §7's A → B → A window, scanned over
  `field_lineage` — the conflict row is marked `escalated` with reason `oscillation`. A proposal is
  **still written on first detection**: the escalation is a flag that tells the reviewer this field
  flips back and forth and a one-shot canonical write will not settle it. What is suppressed is the
  *repeat*: an oscillating conflict whose fix action equals one already proposed for that
  fingerprint is skipped (`skip_reason = oscillation`).

Measured on the graded run: 3,050 conflicts seen, 3,050 proposals written, **25** escalated on
oscillation — exactly the 25 that `golden/conflicts.json` marks `"oscillating": true` — with
`skipped_oscillation = 0` and `skipped_fingerprint = 0`, because it was the first run against a
clean proposal store. Both skips are the second-run behaviour.

## 8. Known gaps

Stated because this project has now shipped documentation naming a control that did not exist, or
naming a reader that does not read, **six** times: §4's `approved_case_type` condition read as though the path a proposal WRITES was
checked against the allow-list when only the classification was; §3's "none of them writes a path outside
`SENSITIVE_FIELDS`" presented a property of the seed as a property of the gate;
`assert_sources_are_unwritable()` was advertised as R24's "never to sources, executed rather than
asserted in prose" while inspecting three strings; §8.7 called a nested sensitive write "refused as
`write_off_allowlist`, the outcome is correct" when the proposal that carries the whole nine-key
`survived` map was **accepted** with `sensitive = false, status = pending` and admitted by the gate;
§8.4 named `approved_case_type` as what refuses the D-7 escape when the sensitivity gate refuses
it two conditions earlier; and §4 called `VIEW_FIELDS` "the object the dashboard renders" when the
dashboard's client never calls the entities endpoint at all — an overclaim about who would NOTICE an
invisible write, which is how the observability gap stayed comfortable. All six are repaired above.
This list is what is still true.

1. **`conflicts.escalation_reason` is usually NULL.** `recon_writer`'s UPDATE grant on `conflicts`
   is column-scoped to `(status, last_seen_run)` (migration 0004), so `_escalate` writes the status
   and puts the reason in the audit row only. The API renders `escalated:<reason>` when the row
   carries one and bare `escalated` otherwise; the dashboard shows the latter as a labelled unknown
   status (a loud failure, `contract.ts` A6). Fixing it means widening a grant in a migration this
   ticket does not own.
2. **Not a gap any more: `GET /api/scorecard` IS built.** This entry used to read "`GET
   /api/scorecard` is not built"; T-14 built `recon/api/scorecard.py`, `create_app()` mounts it
   (`tests/integration/test_route_table.py` pins the mount) and
   `tests/suite/test_scorecard_endpoint.py` drives it — admin-scoped, 401 without a key, 403 for a
   client key, and a loud 503 rather than an empty body when no artifact has been written.
   `contract.ts` A4 is therefore answered, and `tests/api/test_contract_assumptions.py` now lists
   it under `ANSWERED_BY` instead of `NOT_ANSWERED`. **The entry is kept, and not deleted, because
   it was wrong in the direction this list exists to catch**: a document claiming a control is
   MISSING when it exists is the inverse phantom control, and it misleads exactly the reader who is
   deciding what still has to be built. The numbering below is unchanged because §8.4, §8.7, §8.10
   and §8.11 are cited by name from `docs/` and from `tests/apply/`.
3. **Auto-apply acts on approved proposals only** — §4, "Scope of auto".
4. **The 0012 backstop covers one direction, and it constrains rows rather than DDL.**
   `ck_proposals_sensitive_covers_write_set` refuses `sensitive = false` over a `SENSITIVE_FIELDS`
   path. Three things it does not do:
   * *The allow-list half is code only.* Nothing in the schema requires a non-sensitive write to be
     in `AUTO_APPLY_ELIGIBLE`. That is deliberate — the allow-list is R24's auto-apply condition, not
     a statement about which proposals may exist, and a human-reviewed manual apply of an unlisted
     path is legitimate — but it means §4 condition 2's `write_off_allowlist` arm has no database
     backstop. `recon.apply.write_set_gate` is the only thing enforcing it.
   * *A held proposal writing a NON-sensitive path is not refused.* The C4 re-targeting escape §6 and
     §12 D-7 forbid — a C4 re-pointed at `crm.contact.external_id` — carries `sensitive = false` and
     an eligible write set, so the constraint is silent on it. What refuses it is **§4 condition 1,
     the sensitivity gate**: `FIX_TARGETS['C4']` pins `crm.contact.email`, which is sensitive, so the
     classification holds the proposal before its action is read at all. (This bullet previously
     named condition 3, `approved_case_type`. C4 is indeed not an approved case type, so condition 3
     is a second reason — but it is not the one that fires, it is never reached, and naming the wrong
     refusal is how a control gets removed by someone who reads the doc and cannot find it in the
     trace. `tests/apply/test_nested_write_set.py::test_the_c4_retargeting_escape_is_refused_by_the_classifier_first`
     pins the *order* rather than the outcome.) Both reasons are Python; §4 condition 5,
     `write_matches_fix_target`, is a third and also Python.
   * *It binds the owner's rows, not the owner.* A CHECK is not a grant, so the schema owner is
     refused the bad row exactly as `recon_writer` is — asserted in
     `tests/apply/test_write_set_backstop.py`. But the owner runs migrations and may
     `ALTER TABLE … DROP CONSTRAINT`, which the same file demonstrates inside a rolled-back
     transaction. Owner-level enforcement here is **defence in depth, not a boundary**. The boundary
     is the three non-owner roles.
5. **Migration 0012's path list is frozen and could drift from contract §6.** A migration is a
   historical artifact, so it writes §6's twenty paths out as literals rather than importing
   `recon.reference`. Drift is therefore possible and is made *loud* rather than prevented:
   `test_the_installed_constraint_still_matches_the_contract` reads the installed constraint back out
   of `pg_get_constraintdef()`, parses its literals and compares them with `SENSITIVE_FIELDS`, naming
   both sets when they differ. Adding a path to §6 requires a follow-up migration or that test is
   red.
6. **Confidence is never re-derived at apply time.** The gate reads the score and the packet that
   `recon.reconciler` persisted and froze (`KS005`). It does not re-run the model, so a model change
   does not retroactively re-score a stored proposal. That is deliberate — the reviewer decided on
   the row they saw — but it means "confidence ≥ 0.95" is a statement about the score at proposal
   time.
7. ~~**The write-set gate reads the top-level keys of `action->'set'` only.**~~ **CLOSED, and the
   entry was wrong.** It claimed the nested case was "refused as `write_off_allowlist` rather than as
   `sensitive_write` — the outcome is correct, only the reason would be the wrong one". It was not.
   §5's shallow-merge rule requires a nested fix to carry the **whole** map, and a proposal carrying
   the whole nine-key `survived` map with `crm.contact.email`, `appdb.student.status` and
   `appdb.enrollment.stage` replaced was **accepted** by every committed constraint with
   `sensitive = false, status = pending`, cleared §5's guard (it erases no sibling), and was admitted
   by the gate. The mitigation this list claimed did not exist. What now covers it: §3's effective
   write set in `recon.apply`, and `KS013`/`KS014` in migration 0013. The *general* lesson is
   recorded in place of the gap: a rule stated over the keys of a document is not a rule over the
   paths it writes, and the two differ exactly where the document nests.

   **And a third instance of the same mistake, closed by migration 0014.** 0013's repair was itself
   stated over a SHAPE: it descended only when the value being *assigned* was an object. So
   `{"set": {"survived": "wiped"}}` — a scalar, a list or a JSON `null` replacing the nine-key map and
   erasing **six** `SENSITIVE_FIELDS` members, strictly more destructive than the attack 0013 was
   built for — reported the single unlisted leaf `survived`, `KS013`'s `nested_only` half emitted
   nothing at all for it, and 0012's key-level CHECK saw only `survived`. The row was accepted with
   `sensitive = false, status = pending`. The rule is now stated over the merged VALUE (§3) in both
   implementations, and the parity test is a generated cross product of every shape rather than a
   hand-written list. The lesson generalises the one above: **a rule that asks what shape a statement
   has is not a rule about what the statement does**, and every repair so far has had to be re-stated
   one level further down until it was written over the value.
8. **`source_tree_digest()` measures the fixture tree, not "the sources" in general.** It proves a
   real apply run left `fixtures/` byte-identical. If a source were ever backed by something other
   than that tree — a live API, a second root — the measurement would silently cover less than its
   name suggests. Today `recon.adapters.default_fixtures_root()` is the only root any adapter reads.
   The companion structural check is bounded too: `WRITE_NAME_TOKENS` is a substring list, so
   `def persist(...)` passes it — see §4's "Never a source".

9. **0012 and 0013 cannot be applied to a database that already holds a violating row, and neither
   repairs one.** They are different failures and both need saying:
   * **0012 is a `VALIDATED` CHECK, so `alembic upgrade head` FAILS outright** on a database holding
     a `sensitive = false` proposal that names a §6 path. The migration does not run; nothing is
     half-applied (DDL here is transactional).
   * **0013's triggers bind new rows only.** A trigger cannot re-check what is already stored, so a
     pre-existing nested violation survives the upgrade silently. `upgrade head` succeeding is *not*
     evidence that the store is clean.

   Enumerate before upgrading, as the schema owner:

   ```sql
   -- 0012's population: a top-level key naming a sensitive path
   SELECT p.id, p.status, jsonb_object_keys(p.action -> 'set') AS path
     FROM proposals p
    WHERE NOT p.sensitive
      AND jsonb_exists_any(coalesce(p.action -> 'set', '{}'), <§6's paths>);

   -- 0013's population, once its functions exist (they install even if the triggers are
   -- later dropped): a path reached THROUGH a nested object. NOTE: migration 0014 changes
   -- what this function reports for a NON-object replacing an object, so a store enumerated
   -- before 0014 must be enumerated again after it -- 0014 binds new rows, not existing ones.
   SELECT p.id, p.status, keystone_effective_write_paths(
            coalesce(p.action -> 'set', '{}'), e.current, true) AS nested_paths
     FROM proposals p LEFT JOIN entities e ON e.canonical_id = p.target_canonical_id
    WHERE NOT p.sensitive
      AND keystone_effective_write_paths(
            coalesce(p.action -> 'set', '{}'), e.current, true) && <§6's paths>;
   ```

   **Remediation is DELETE, not UPDATE.** `KS005` freezes `action`, `sensitive` and
   `target_canonical_id` after INSERT, so a violating row cannot be re-classified in place — an
   `UPDATE proposals SET sensitive = true` is refused by the freeze trigger. The owner must delete
   the offending proposals (and any `proposal_events` citing them), then re-run
   `recon.reconciler.reconcile`, which regenerates them correctly because the classifier and
   `_assert_action_matches_classification` were never the thing that was broken. A row that was
   already **applied** is a canonical write that has happened: reverse it with
   `recon.apply.rollback_proposal` first, so the ledger records the reversal, and only then delete.

10. **Nothing the machine may take unattended is observable, and that is structural.**
    §4's "What an auto-applied write is observable in" makes one eligible path observable, and
    `recon.reconciler` now emits it: 120 of the 3,050 committed proposals write
    `survived->crm.contact.lifecycle_stage`, and `test_the_real_proposal_applies_visibly_and_rolls_back`
    applies one, reads it back through the reader's own projection, rolls it back and reads it again.
    **That demonstration is the MANUAL apply.** Auto-apply cannot be demonstrated on it, and the
    reason is not this seed:

    * **No C6 or C14 can ever reach the 0.95 floor.** §2.4 makes `R-006` and `R-014` the only rules
      that populate `disagreeing_fields`, so a conflict of either type carries at least one
      disagreeing comparison ROW by definition. `confidence.yaml` v2 computes
      `clamp01(clamp01(base + positives) + negatives)` — the positive half is clamped to `1.0000`
      **before** the penalties are subtracted — and the `disagreeing_field` term is `-0.10` per row.
      The ceiling for any conflict carrying one disagreeing row is therefore exactly **0.9000**, below
      R24's floor. `test_no_c6_can_ever_reach_the_auto_apply_floor` derives that from the loaded model
      (weight, sign and formula shape) rather than from the store, so a model change makes it red
      rather than silently wrong. The store agrees: 120 lifecycle-only C6s, best score **0.9000**.
    * **The only eligible path inside the view is a C6's fix target.** So the two facts compose:
      what the machine may take unattended is never observable, and what is observable the machine may
      never take unattended.
    * **What the machine WOULD take is real and is invisible.** The proposals R24 admits once approved
      write exactly `{appdb.enrollment.crm_deal_id}` — 50 C9s at ≥0.95 — and that path is not in
      `VIEW_FIELDS`. The path set is the structural claim; the population behind it is a dataset
      property and the test asserts only that it is non-empty.

    **The honest count of proposals that are both auto-appliable and visible is 0**, and no seed
    change moves it. Three routes could close it; only two are available, and neither is this
    ticket's:

    1. **Change the model** so a single disagreeing row leaves headroom — a smaller
       `disagreeing_field` weight, or subtracting penalties before the clamp. That is a
       `confidence.yaml` edit, a `version` bump, and a new confidence vector for all 3,050 proposals:
       R14's committed formula and R22's determinism artifacts both move with it.
    2. **Project one of the other four eligible paths.** `appdb.enrollment.crm_deal_id` is written by
       50 proposals that already clear every R24 condition; making it observable means adding it to
       `recon.resolve.VIEW_FIELDS` and to `golden/expected-views.json`, which is the committed R10
       join contract — a contract change, not a code change. It is the smallest of the three.
    3. **Lower the floor.** Not available: R24 pins ≥0.95.

    The previous version of `tests/apply/test_observable_auto_apply.py` solved the problem by having
    the test supply a confidence of `0.9900` on a proposal it hand-wrote. That made the demo green
    while proving nothing about the shipped pipeline, and it is withdrawn: this file now applies the real row at its real score through the path R15 leaves to a
    human, and pins the unattended path's refusal
    (`test_the_unattended_path_refuses_the_same_row_on_its_score`) instead of engineering around it.

11. **The look-alike guard is code, not schema.** A member ADDED to `survived` — a case, whitespace
    or homoglyph variant of a genuine path — writes a leaf on neither committed list, so R24 refuses
    it; a reviewer pressing APPLY is not behind R24, and what refuses it there is
    `recon.apply.merge_preview`'s `introduced` arm in `apply_proposal`
    (`ApplyError('nested_member_introduced')`). There is no SQLSTATE for it. A trigger could be
    written — the diff is in the ledger row `KS014` already reads — but keying it on the unattended
    actor would leave the human case (the one that matters here) uncovered, and keying it on every
    actor would refuse the migration that legitimately adds a tenth survived path. Recorded rather
    than claimed.
