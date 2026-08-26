# Keystone — Source, Identity & Invariant Contract (v2)

## 0. Scope, authority, shared modules

Normative for the seed generator (`recon.seed`), the entity-resolution layer (`recon.er`), the invariant
rule set (`rules/*.sql`), and the grading harness (`recon.suite`). It exists so the **generator and the
detector cannot drift** (R23): both derive from this document and from the *same* code modules —

- `recon/normalize.py` — normalization + match keys, `QUOTE_CHARS`, `KEY_CLASSES`
- `recon/reference.py` — enum maps, `GRADE_ORDER`, fee schedule, `COMPARED_FIELDS`, `SENSITIVE_FIELDS`,
  `PRECEDENCE`, `canon_value`, `fingerprint`, `fix_target`, `household_key`,
  `household_members_appdb`, `household_members`, `conflict_refs`, committed constants
- `recon/er.py` — the `L` / `P` / `D` / `E` cascades, `entity_links`, `entity_link_candidates`

Neither side may re-implement any of the three.

**Order of authority.** brief PDF > this contract > any other doc in `docs/`. Where this document and the
brief disagree, the **PDF wins** — and the divergence must appear in §12, never silently.

**v2 changes the shape of three things** and every reader must know it up front:
1. Seeding is **two-pass** and the generator runs the detector's real ER cascade (§10 `G31`).
2. Every false-positive guard in §5 is a **construction constraint on the generator** (§10) asserted by a
   named manifest self-check (§9), not an assertion about how the world behaves.
3. Every symbol a predicate consumes is **defined in this document**. A predicate that reaches an
   undefined term is a contract bug, not an implementer's judgement call.

§13 lists everything v1 said that v2 supersedes. Nothing from v1 has been dropped silently.

---

## 1. Source schemas (as generated)

Minimum fields are the brief's Appendix A.2 verbatim. Added fields are marked `[+]` and are permitted
("You may add fields; you may not remove these"). Every added field exists for one of exactly **three**
reasons, stated per field below: **(a)** a mandated conflict class or an FP guard is otherwise
undetectable; **(b)** it populates `SENSITIVE_FIELDS` (§6) so the auto-apply classifier has a real field
path to refuse — `appdb.student.student_number`, `crm.contact.marketing_consent`,
`appdb.student.communication_opt_out`, `appdb.enrollment.billing_owner_email`; or **(c)** it exercises a
committed normalizer under unit test without being compared by any rule — `crm.contact.state` alone.

**Applies to every entity below.** Each entity carrying `created_at` / `updated_at` — contact, deal,
student, enrollment, payment — has `updated_at < created_at` on ~0.5% of its records (A.3, spread through
the dataset including clean records). **No rule may treat an out-of-order timestamp as evidence *of* a
conflict.** The **single** permitted read of `updated_at` by any rule is C13's recency clause (c), which
compares `refunded_at` against the enrollment row's `updated_at`; the ~0.5% out-of-order dirt is **never**
applied to an enrollment whose student holds a `refunded` payment, so that clause is dirt-free by
construction (§10 `G26`). C11's window uses `occurred_at` only.

### 1.1 CRM (HubSpot-shaped) — `contact`
| field | type | notes |
|---|---|---|
| `crm_id` | str `CRM-0000001` | PK. Survivorship tiebreak key (§4.6) |
| `email` | str | the **guardian/billing** email; siblings share it. Always drawn from the student's **primary** `guardian_email` except on planted C4 (§10 `G1`, `G19`) |
| `first_name`, `last_name` | str | the **student's** name (admissions CRM: contact = prospective student) |
| `lifecycle_stage` | str | committed vocabulary, dirty-cased (§2.3) |
| `created_at`, `updated_at` | ISO-8601 Z | |
| `external_id` | str \| null | → `appdb.student.id`. Present on ~60% of linkable contacts, drawn jointly with `dob` and email variance subject to `G3` |
| `dob` `[+]` | `YYYY-MM-DD` \| null | present ~70%; required for the name+DOB join the brief mandates |
| `grade` `[+]` | str \| null | dirty variants (`"Grade 4"`, `"4"`, `"4th"`) — compared by `COMPARED_FIELDS` |
| `state` `[+]` | str \| null | `TX`/`Tx`/`TEXAS` dirt. **Not compared by any rule** — it has no counterpart in any other source. Exercised by a `norm_enum` unit test only |
| `marketing_consent` `[+]` | bool \| null | **sensitive** (consent flag) |

### 1.2 CRM — `deal`
`deal_id` (`DEAL-0000001`), `name`, `pipeline`, `stage`, `amount` (dollars, float — CRM-shaped),
`associated_contact_ids[]` (**required, never empty**), `created_at`, `updated_at`.

> **A deal is per household.** `associated_contact_ids[]` carries the CRM contact of *every* child in that
> household. This is what makes **13,460** deals cover the 16,825 clean paid+enrolled persons at 1.250
> contacts/deal; the remaining **1,540** of the 15,000 cover the `{appdb, crm}` prospect/applied
> households one-for-one (§11.7). `deal.pipeline` is set to the `program` of the household's **anchor
> enrollment** — the enrollment of `household_anchor_student(k)` (§4.8) — on **every** deal, planted or
> not (§10 `G29`).

`amount` is the **only** float-typed field in §1. It never reaches `canon_value` as a float: it is
converted to `Money(round(amount * 100))` at the `stg_crm_deal` boundary and materialized as
`amount_cents` (§2.5). `round()` is **banker's rounding** and no generated amount may sit on a
half-cent boundary, so the tie-break can never decide a graded byte (§2.5 ruling 13, `G39`).

### 1.3 App DB (Postgres) — `student`
`id` (uuid5), `first_name`, `last_name`, `dob`, `grade`, `guardian_email`, `guardian2_email` (~60% null),
`status`, `enrollment_year`, `created_at`, `updated_at`,
`[+] student_number` (`S-000123`, the **government/student identifier** — sensitive),
`[+] household_id` (`HH-000123`), `[+] communication_opt_out` (bool, sensitive).

- `student.id = uuid5(KEYSTONE_NS, str(<generator sequence index>))` — **never** derived from identity
  fields, so a name/DOB collision can never collapse two students onto one PK (§10 `G5`).
- `household_id` is intentionally app-DB-local: a *data* field the generator writes and the detector may
  read as a corroborating signal. It is **not** a cross-source key, it never links sources, and C8
  detection must not depend on it. Households are inferred from normalized guardian emails (§4.8).
- `guardian2_email` is **corroborating evidence only** and is never part of the household key (§4.8).
  It may be nulled only when no contact and no payment in the dataset uses that address (§10 `G3`).

### 1.4 App DB — `enrollment`
`id` (uuid5), `student_id`, `program`, `stage`, `deposit_paid_at` (nullable), `crm_deal_id` (nullable,
~60% of linkable), `created_at`, `updated_at`, `[+] billing_owner_email` (sensitive — billing ownership).

- **Cardinality is 1:1.** A student has **at most one** enrollment. 22,000 students have exactly one;
  3,000 have none (§10 `G12`, §11.5). Every payment-to-enrollment question is therefore unambiguous.
- **`deposit_paid_at` is never cleared once set.** Moving an enrollment to `refunded` / `withdrawn`
  leaves it in place — it is a retained historical fact, and it is itself in `SENSITIVE_FIELDS` so no fix
  may null it. It is **never** a conflict trigger; it appears in `observed_values` only.

### 1.5 Payments (Stripe-shaped) — `payment`
> **Brief divergence (flagged, §12 · D-1).** A.1's Entities column names "payments, **refunds**" as two
> payments-source entities. Keystone models a refund as the same `payment` record transitioning to
> `status='refunded'` with `refunded_at` set, because A.1 caps the payments source at 18,000 records
> *inclusive of refunds* — a separate refund entity would consume that budget twice. There is **no**
> `payments:refund:<id>` source ref.

`payment_id` (`pi_0000001`), `payer_email`, `payer_name`, `amount_cents` (int), `currency` (`"usd"`),
`type` (`fee|deposit|tuition`), `status` (`paid|refunded`), `occurred_at`, `external_ref` (→
`appdb.student.id`, ~60% of linkable), `[+] refunded_at` (nullable, non-null iff `status='refunded'`),
`[+] metadata: {student_first_name: str|null, student_last_name: str|null, program: str|null}`.

- **`metadata.student_first_name` and `metadata.student_last_name` are SEPARATE fields**, each emitted as
  exactly the un-normalized source value. No name string is ever joined, split or re-parsed on either
  side (§4.3 `P2`). The pair is present on ~85% of attributable payments.
- **Joint-gap ban (generator invariant).** A payment omits **both** `external_ref` **and** the
  `metadata.student_first_name` / `student_last_name` pair **only** when it is a planted C2. The marginal
  rates stay at the brief's ~60% / ~85%; the joint absence is forbidden and asserted (§10 `G6`).
- `payer_email` is always drawn from the attributed student's **primary** `guardian_email`, except on
  planted C2 (§10 `G6`). `metadata.program`, when present, equals the attributed enrollment's
  `program` on **every** payment — no plant relaxes it, C12 included (§10 `G13`).

---

## 2. Committed reference data

Everything in this section is data committed in `recon/reference.py` (or `recon/normalize.py` where
noted) and imported unchanged by both sides. **SQL rules may not normalize.** `rules/*.sql` must never
call `lower()`, `trim()`, `replace()`, `regexp_*` or any casefold on an identity field, and may not
compute an ordinal. Normalization is materialized upstream by Python into `stg_*` columns (§3). A
committed lint test greps the rule files for these tokens and fails the build.

### 2.1 Normalization (`recon/normalize.py`)
- `norm_email(e)`: strip, drop **surrounding** quotes/backticks, strip again, casefold. **Only** for
  `gmail.com` / `googlemail.com`: truncate the local part at `+` and remove `.` from it. Never for any
  other domain (universal dot-stripping collapses legitimately distinct addresses → false positives).
- `norm_name(s)`: casefold, NFKD-fold accents (drop combining marks), casefold again, remove every
  `QUOTE_CHARS` character **wherever it occurs**, collapse internal whitespace, trim. **Never** merges
  different spellings (`Jon` ≠ `John`).
- `norm_enum(field, v)`: table-driven from §2.3. Unknown value → `None` **plus** an `unchecked` note;
  never raises.
- `norm_dob(v)`: `YYYY-MM-DD` or `None`.
- `match_keys(entity)`: ordered, deterministic — `("ext", <hard id>)`, `("email", norm_email)`,
  `("namedob", (first_norm, last_norm, dob_norm))`. Candidates only; **never** an automatic merge.
  `KEY_CLASSES = ("ext", "email", "namedob")` is the committed key-class vocabulary (§4.7) and is
  **exported** from `recon/normalize.py`.
- **Idempotence is a property test**: `f(f(x)) == f(x)` for every `norm_*`.

**PINNED — `QUOTE_CHARS`, the committed A.3 quote-dirt set (ruling 7).** Exactly these **seven**
characters, in this committed order, exported from `recon/normalize.py`:

```
QUOTE_CHARS = "\"'`‘’“”"      # " ' ` U+2018 U+2019 U+201C U+201D
```

The four curly quotes are part of the committed set, not an implementer's addition: A.3 sprinkles
typographic quotes through the CRM export and a three-character set would leave `‘Maria’` un-normalized.

**PINNED — quote handling is deliberately ASYMMETRIC between `norm_name` and `norm_email` (ruling 6).**
This is load-bearing and must not be "made consistent":

| function | quote treatment | consequence |
|---|---|---|
| `norm_name` | remove every `QUOTE_CHARS` character **ANYWHERE in the string** | `O'Brien` and `OBrien` normalize **equal** — the same person, spelled two ways, links |
| `norm_email` | strip `QUOTE_CHARS` from the **SURROUNDING** ends only, never from the interior | `o'brien@corp.com` and `obrien@corp.com` normalize **unequal** — two different mailboxes stay different |

Removing quotes from the interior of an address is the same false-positive class as universal
dot-stripping (§2.1's gmail scoping, `G4`): it collapses distinct mailboxes belonging to distinct people
against the clean majority, which is the hardest-graded population. A name is a human spelling of one
identity; an address is a routing key. The asymmetry is the point.

**PINNED — `norm_email` on a value with no `@` (ruling 14).** Trim, strip surrounding quotes, casefold —
and **nothing else**. The gmail local-part rules are **never** applied to a value that has no domain to
scope them to, and the value is never "repaired" into an address. The result is returned verbatim (a
value that is empty once trimmed is `None`, so a NULL `guardian2_email` stays NULL rather than becoming
`""` and colliding). The domain is taken with `rpartition("@")`, so the **last** `@` separates it and a
stray interior `@` can never change the domain.

**PINNED — `match_keys` with a null DOB (ruling 10).** No `namedob` key is emitted unless `first_norm`,
`last_norm` **and** `dob_norm` are **all** non-`None`. A `(first, last, None)` key is never emitted, in
any shape, on any entity. Consequence for §4.7: `entity_link_candidates` therefore carries **no**
`key_class='namedob'` row for a record with a missing or unparseable DOB — such a record is reachable
only by `ext` or `email`. This is intended: `L3` (§4.2) requires both DOBs non-null, so a partial
`namedob` key could only manufacture candidate pairs no cascade rule is allowed to accept, and `R-010`
(C10), which is evaluated over `entity_link_candidates`, would see a `namedob` resolution that no link
rule could ever have made.

### 2.2 Committed constants
| symbol | value |
|---|---|
| `KEYSTONE_NS` | **`17733ea0-28dd-5aeb-a266-c62b3689def8`** — fixed uuid5 namespace, committed literal (ruling 1) |
| `MAX_PAYLOAD_BYTES` | `262144` (256 KiB) — the adapter rejects any single JSONL line exceeding this with the documented 4xx; §7's oversized case is generated at `MAX_PAYLOAD_BYTES + 1` bytes |
| `PAID_IMPLYING_STAGES` | `{deposit_paid, enrolled}` |
| `ENROLLMENT_GRADE_FLOOR` | `"K"` (→ `GRADE_ORDER` 0) |
| `C11_WINDOW_SECONDS` | `600` (strict `<`) |
| `C11_PLANT_MAX_SECONDS` | `300` |
| `LEGIT_REPEAT_MIN_SECONDS` | `1200` |
| `NAME_CORPUS_MIN` | `2000` first names × `1000` last names |

**PINNED — `KEYSTONE_NS` is a committed constant, never re-derived (ruling 1).**

```
KEYSTONE_NS = UUID("17733ea0-28dd-5aeb-a266-c62b3689def8")
```

Its **derivation, recorded for provenance only**, is
`uuid5(NAMESPACE_DNS, "keystone.invariant-contract.v2")`. That expression is how the literal was
obtained once; it is **not** how any code obtains it. `recon/reference.py` holds the literal and every
`uuid5` in the system hangs off that literal.

The reason this is a constant and not an expression: `KEYSTONE_NS` determines **every `person_key`**
(§4.1) and **every `appdb.student.id`** (§1.3). Re-deriving it from a seed string means the seed string
is the real constant, and a whitespace change, a `v2`→`v3` rename or a different namespace argument in
either the generator or the detector silently re-keys the entire dataset — every student PK, every
`person_key`, every `field_lineage` row and therefore R16's oscillation dedup. A committed literal
cannot drift; a re-derivation can. A test asserts **both** the literal **and** that the recorded
derivation still reproduces it, so the provenance note can never rot into a false claim.

### 2.3 Committed enum maps
- **grade** (values): `PK, K, 1..12`. Accepts `Grade 4`, `4`, `4th`, `Fourth`, `grade4`, `Kindergarten`,
  `KG`, `Pre-K`.

  **PINNED — the grade variant families are a CLOSED set (ruling 11).** The eight examples above are
  examples; the committed table is exactly the families below and **nothing else**. Every raw value is
  first folded to a *variant key* — NFKD, casefold, drop combining marks, drop every `QUOTE_CHARS`
  character, then delete all whitespace, `_` and `-` — and looked up in that table.

  | canonical | committed variant family (before folding) |
  |---|---|
  | `PK` | `PK`, `Pre-K`, `PreK`, `Pre K`, `Pre-Kindergarten`, `Prekindergarten` |
  | `K` | `K`, `KG`, `Kindergarten`, `Grade K` |
  | `1`…`12` | `<N>`, `Grade <N>`, `<N>th` (`1st`/`2nd`/`3rd` for 1–3), `<word>` (`first`…`twelfth`), `Grade <N>th`, `Grade <word>` |

  Because separators are deleted by the folding, `Grade 4`, `grade4`, `GRADE-4` and `grade_4` are one
  variant key, not four table rows. The table is built at import and **refuses to be ambiguous**: two
  families folding to the same variant key raise at import rather than resolving by table order.

  **Generator constraint.** The generator may draw grade dirt **only** from this closed set. A
  well-formed grade string outside it (`"Yr 4"`, `"Form IV"`, `"4e"`) normalizes to `None`, which makes
  the `grade` comparison `unchecked` (§5.1) rather than a comparison — the planted C6 becomes an
  unchecked non-event and a golden entry silently turns into a false negative. Drawing outside the
  closed set is a construction bug, not tolerable dirt.
- **`GRADE_ORDER`** (ordinal — pinned because *string* comparison is wrong here: `'PK' < 'K'` is FALSE and
  `'1' < 'K'`, `'10' < 'K'`, `'12' < 'K'` are all TRUE):
  ```
  GRADE_ORDER = {"PK": -1, "K": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6,
                 "7": 7, "8": 8, "9": 9, "10": 10, "11": 11, "12": 12}
  ```
  `grade_ord` is materialized as a `stg_*` column. A property test asserts every canonical grade value
  has an ordinal and that `GRADE_ORDER` is injective.
- **state**: 50-state code map; `TX`/`Tx`/`TEXAS`/`texas` → `TX`. Used by no rule (§1.1).

  **PINNED — exactly the 50 states, and nothing else (ruling 12).** `STATE_VALUES` has **50** entries:
  the fifty United States' USPS codes. It contains **no `DC`**, no territory (`PR`, `GU`, `VI`, `AS`,
  `MP`), no `AA`/`AE`/`AP` military codes and no Canadian province. Each code accepts exactly two
  variants — the code itself and the state's full English name — under the same folding as `grade`
  (case, whitespace, `_`, `-`), which is what makes `TX`, `Tx`, `TEXAS`, `texas`, `  Texas ` and
  `New-Hampshire` all resolve.

  **Generator constraint: the generator must not emit `DC`**, nor any other value outside the committed
  50, in `crm.contact.state`. `crm.contact.state` exists solely to exercise a committed normalizer under
  unit test (§1.1 reason (c)); an emitted `DC` would normalize to `None` and make that field's only
  reason for existing untestable.
- **program**: `Lower School | Middle School | Upper School | Summer Academy`, accepting case,
  leading/trailing whitespace and `_`/`-`/space variants. Used for **both** `enrollment.program` and
  `payment.metadata.program`; materialized as `program_norm` in `stg_enrollment` and `stg_payment`.
- **deal.pipeline**: the same four values with the same dirty-variant tolerance as `program`.
- **enrollment.stage** (canonical funnel): `prospect, applied, waitlisted, deposit_paid, enrolled,
  withdrawn, refunded`. Materialized as `stage_funnel`.
- **`DEAL_STAGE_TO_FUNNEL`** (bijective onto the funnel, so the cross-source comparison is lossless):
  `New Lead→prospect`, `Application Submitted→applied`, `Waitlisted→waitlisted`,
  `Deposit Received→deposit_paid`, `Closed Won→enrolled`, `Closed Lost→withdrawn`, `Refunded→refunded`.
  Dirty variants accepted: case, `_`/`-`/space, `CLOSED_WON`, `closed won`.
- **`STATUS_TO_FUNNEL`** (`appdb.student.status`): `prospect→prospect`, `applied→applied`,
  `enrolled→enrolled`, `active→enrolled`, `withdrawn→withdrawn`.
- **`LIFECYCLE_TO_FUNNEL`** — total over the committed `lifecycle_stage` vocabulary; a key missing from
  the map raises at import:
  ```
  subscriber            -> None
  lead                  -> prospect
  marketingqualifiedlead-> prospect
  MQL                   -> prospect
  salesqualifiedlead    -> applied
  SQL                   -> applied
  opportunity           -> applied
  customer              -> enrolled
  evangelist            -> None
  other                 -> None
  ```
  `None` on either side of a comparison ⇒ `verdict='unchecked'` for that comparison, **never** a
  disagreement (§5.1). The `None`-mapping subset `{subscriber, evangelist, other}` is how a `withdrawn`
  student is represented on the CRM side — no lifecycle value maps to `withdrawn` (§10 `G18`).
- **Fee schedule** (exact, cents):
  | type | Lower School | Middle School | Upper School | Summer Academy |
  |---|---|---|---|---|
  | `fee` | 10000 | 10000 | 10000 | 10000 |
  | `deposit` | 50000 | 60000 | 75000 | 25000 |
  | `tuition` | 1200000 | 1400000 | 1600000 | 300000 |

### 2.4 `COMPARED_FIELDS` — the ONLY producer of `disagreeing_fields`
Each row is one comparison. Both endpoints are **source-qualified paths** — the same vocabulary as
`SENSITIVE_FIELDS` (§6), so the C6/C14 subset test in §5.5 is well-typed.

| logical | left path | right path | mapper |
|---|---|---|---|
| `name_first` | `crm.contact.first_name` | `appdb.student.first_name` | `norm_name` |
| `name_last` | `crm.contact.last_name` | `appdb.student.last_name` | `norm_name` |
| `dob` | `crm.contact.dob` | `appdb.student.dob` | `norm_dob` |
| `grade` | `crm.contact.grade` | `appdb.student.grade` | `norm_enum('grade', ·)` |
| `stage` | `crm.deal.stage` | `appdb.enrollment.stage` | `DEAL_STAGE_TO_FUNNEL` / `stage_funnel` |
| `lifecycle` | `crm.contact.lifecycle_stage` | `appdb.student.status` | `LIFECYCLE_TO_FUNNEL` / `STATUS_TO_FUNNEL` |

`disagreeing_fields` is the **sorted set of both source-qualified paths of every disagreeing
comparison**. No rule other than `R-006` / `R-014` populates `disagreeing_fields`.

Sensitivity of each endpoint under §6 (this is what drives the C6/C14 partition):

| logical | left sensitive? | right sensitive? | whole-set ⊆ SENSITIVE? | emits |
|---|---|---|---|---|
| `name_first` / `name_last` / `dob` | yes | yes | yes (alone) | **C14** |
| `stage` | yes (`crm.deal.stage`) | yes (`appdb.enrollment.stage`) | yes (alone) | **C14** |
| `grade` | no | no | no | **C6** |
| `lifecycle` | no (`crm.contact.lifecycle_stage`) | yes (`appdb.student.status`) | no | **C6** |

### 2.5 `canon_value` — the canonical value serializer
Committed in `recon/reference.py`, used by **both** sides wherever a value is hashed, compared as text,
or written to `observed_values`:

```
canon_value(v) -> str
  None        -> "\N"
  bool        -> "true" | "false"                    # dispatched BEFORE int (bool is a subclass of int)
  int         -> decimal, no separators
  Money(cents)-> integer cents, decimal   # the explicit wrapper type in recon/reference.py
  float       -> FORBIDDEN; raises ValueError
  date        -> "YYYY-MM-DD"
  timestamp   -> "YYYY-MM-DDTHH:MM:SSZ", normalized to UTC, SECOND precision
  str         -> as-is, with "\", "\x1f" and "\x1e" backslash-escaped
  sequence    -> NORMATIVE, see below   # list | tuple | set | frozenset
  anything else -> raises TypeError; never a Python `repr`
```

`money` is **not** a Python type and is therefore not a dispatch case: the only money-shaped value in the
pinned schemas is `crm.deal.amount` (dollars, float), which is converted to `Money(round(amount * 100))`
at the `stg_crm_deal` boundary (column `amount_cents`) and only ever reaches `canon_value` in that form.
A bare `float` reaching `canon_value` **raises** rather than serializing non-deterministically, so the
§5.4 fingerprint is defined for every value any conflict can carry.

**PINNED — the string escape set and its order.** Exactly three characters are escaped, in exactly this
order: every backslash becomes two backslashes **first**, then every raw `\x1f` (US, §5.4's intra-section
joiner) becomes the four text characters `\x1f`, then every raw `\x1e` (RS, the sequence joiner below)
becomes the four text characters `\x1e`. The backslash pass must run first or the escaping stops being
reversible. Nothing else is escaped — an email, a name or a ref passes through byte-identical.
Consequences that are asserted: `canon_value(None) != canon_value("\N")`, no canonical form of a string
ever contains a raw `\x1f` or a raw `\x1e`, and distinct strings always have distinct canonical forms.

**PINNED — the SEQUENCE case is NORMATIVE, not an implementer extension (ruling 2).** §5.4 pins three
inherently multi-valued `observed_values` keys — `C1.paid_payment_refs`,
`C4.student_guardian_email_norms` and `C9.deal_person_refs` — so a serializer with no sequence case is
an incomplete serializer, and "each side joins them itself" is precisely the generator/detector drift
this module exists to prevent. `list`, `tuple`, `set` and `frozenset` are **one** case; a sequence is a
sorted **multiset**, so element order never reaches the digest.

```
_ELEMENT_SEPARATOR = U+001E (RS)     # committed once, here

canon_value(seq) = RS + concat( e + RS  for e in sorted(elements) )

    elements      = [ escape_element(canon_value(item)) for item in seq ]
    escape_element = replace each backslash with two backslashes,
                     THEN replace each raw RS byte with the four TEXT characters  \ x 1 e
                     (in that order -- the backslash pass must run first)
    sorted        = ascending by code point, over the ESCAPED element encodings
```

Worked, so a re-implementer lands on the same bytes: `canon_value([]) == "\x1e"`;
`canon_value(["a"]) == "\x1ea\x1e"`; `canon_value(["b","a"]) == canon_value({"a","b"}) == "\x1ea\x1eb\x1e"`.

**Worked, for the ESCAPED sort specifically** — the one input shape where the two candidate orders part
company, so a re-implementer can tell which one it built:

```
# Python string literals: `\x1e` is the RAW separator, `\\x1e` its four-character escape
canon_value(["Z", ["a"]]) == "\x1eZ\x1e\\x1ea\\x1e\x1e"
```

The nested child canonicalizes to `\x1ea\x1e`, which sorts **below** `Z` raw (U+001E < U+005A) and
**above** it once escaped (its leading `\x1e` becomes a backslash, U+005C > U+005A). Sorting the raw
canonical forms and escaping afterwards would put the child first; this contract puts `Z` first. Only a
*separator* can expose the difference: the backslash pass alone is a prefix-free, monotone code and
therefore preserves order, which is why no backslash-only example distinguishes the two.

**The encoding is INJECTIVE, and that is a graded property, not a nicety.** The leading `\x1e`, the
per-element trailing `\x1e` and the escaping of `\x1e` inside every element are each load-bearing:

- **`\x1e` must be in the string escape set.** Without it `canon_value(["a\x1eb"])` and
  `canon_value(["a","b"])` are the same bytes — two structurally different `observed_values` maps
  collapsing to one **fingerprint**, which is the idempotency key R16's oscillation dedup and the whole
  proposal pipeline are keyed on. A collision there silently suppresses a real second proposal.
- **Elements are re-escaped when embedded**, so a nested sequence's own separators cannot be mistaken
  for the outer sequence's: `canon_value([["a"],["b"]]) != canon_value(["a","b"])`.
- **The leading marker plus a trailing marker per element** makes a sequence self-delimiting and
  distinguishable from every scalar: no scalar canonical form contains a raw `\x1e` (the string case
  escapes it; the others are digits, letters, `-`, `:`, `T`, `Z` or `\N`). Hence
  `canon_value([]) != canon_value("")`, `canon_value([""]) != canon_value([])` and
  `canon_value(["a"]) != canon_value("a")`.

The one conflation `canon_value` **does** accept is between *scalar types* with the same text —
`canon_value(True) == canon_value("true")`, `canon_value(1) == canon_value("1")`,
`canon_value(Money(1)) == canon_value(1)`. This is safe and intended: §5.4 pins the `observed_values`
key set per conflict type and each key carries one fixed type, so no key can present two types.
Injectivity is required **within a type**, and between a sequence and everything else.

**PINNED — timestamps (ruling 4).** Two clauses, both normative:

1. **Naive is UTC.** A `datetime` with `tzinfo is None` is interpreted as **already UTC** and stamped
   `Z`. It is **never** interpreted in the local zone of whatever machine happens to run the job — the
   generator and the detector must produce the same bytes on a laptop in `America/Chicago` and in a
   container at `UTC`, and §1's `created_at`/`updated_at`/`occurred_at`/`refunded_at` are all ISO-8601 Z
   values whose naive form has already lost only the marker, not the zone. An aware `datetime` is
   converted with `astimezone(UTC)` first.
2. **Second precision; microseconds are TRUNCATED, not rounded.** The pinned format is exactly
   `%Y-%m-%dT%H:%M:%SZ`. `2010-04-05T01:02:03.999999+00:00` canonicalizes to `2010-04-05T01:02:03Z`.
   Rounding would let a sub-second difference move a value across a second boundary and change a
   fingerprint; truncation is monotone and reproducible. Sub-second precision is not carried anywhere in
   this contract — C11's window (`600s`) and C13's recency clause both compare whole seconds.

**PINNED — money rounding (ruling 13).** `Money.from_dollars(amount) = Money(round(amount * 100))`, and
Python's `round()` is **banker's rounding** (round-half-to-even), *not* half-up and *not* truncation:
`round(0.5) == 0`, `round(1.5) == 2`, `round(2.5) == 2`. The distinction from truncation is the common
case and is graded — `int(0.29 * 100) == 28` where `round(0.29 * 100) == 29`, because `0.29 * 100` is
`28.999999999999996` in IEEE-754. Using `int()` would mis-state one cent on a large fraction of the
15,000 deals.

The half-cent tie-break itself is pinned but must be **unobservable**:

> **Generator constraint (`G39`).** No `crm.deal.amount` may sit at exactly a half-cent boundary — i.e.
> `amount * 100` may never be an exact `.5` in IEEE-754 (`0.125`, `0.375`, `12.005`, …). Every generated
> amount is drawn from the fee schedule (§2.3, whole cents) or from a C12 offset in whole cents, so the
> constraint is met by construction and asserted by `sc_amount_no_half_cent`.

The tie-break rule is therefore committed (so the code is defined for every input) yet can never decide
a graded byte. This is deliberate: half-to-even is correct but surprising, and a dataset that exercised
it would make the golden set depend on a rule a reviewer would read as a bug.

Conflict rows are assembled in Python from SQL rule output; `rules/*.sql` never build fingerprint or
`observed_values` strings.

---

## 3. Pipeline shape

```
fixtures/*.jsonl --ReadOnlyAdapter--> raw_records (append-only, per generation, lineage stamped)
                                  --> source_generations (completeness ledger)
raw_records --recon/normalize.py--> stg_crm_contact, stg_crm_deal, stg_student, stg_enrollment, stg_payment
stg_* --recon/er.py (cascade) + recon/resolve.py (materialize)--> entity_links + entity_link_candidates
                                                                 + entities(person) + field_lineage
stg_* + entities --rules/*.sql--> invariant_results (per record) --> conflicts (fingerprinted)
```

`natural_key` is the source PK of a record — `crm_id`, `deal_id`, `student.id`, `enrollment.id`,
`payment_id` — and is what a source ref is built from (§4.1). `(source_id, entity_type, natural_key,
generation)` is the `raw_records` key.

`stg_*` carry both the raw and the normalized columns (`email_norm`, `first_norm`, `last_norm`,
`dob_norm`, `grade_norm`, `grade_ord`, `program_norm`, `stage_funnel`, `payer_email_norm`, …) plus
`generation`.

- **Current state = the rows with `generation = 3`** (§7). Invariants read generation 3 only.
- `field_lineage` retains generations 1–3 for oscillation detection (R4/R16) and is keyed on `person_key`
  (§4.1).
- `source_generations(source_id, generation, entity_type, expected_count, loaded_count, complete bool)` —
  the completeness ledger consumed by §5.3.

```
field_lineage(person_key, field_path, generation, value_canon, source_ref)
```
`field_path` is a **source-qualified path** — the same shape `COMPARED_FIELDS` (§2.4) and
`SENSITIVE_FIELDS` (§6) use, never a bare column name and never a logical name. Its range is
`LINEAGE_PATHS`, declared in `recon/resolve.py` as one path map per `(source_id, record_class)` — all
five record classes of §1, `payments.payment` **included** — with `LINEAGE_PATHS` their union, so a path
cannot be declared without being written and cannot be written without being declared.

**The derivation runs from the maps to `LINEAGE_PATHS`, and never the other way.** It used to read
`set(COMPARED_FIELD_PATHS) | set(SURVIVED_PATHS)`, which tied the reach of lineage to the reach of
detection: R1 mandates field-level lineage on all three sources, and under that definition giving the
payments source lineage meant adding a `payments.*` entry to `COMPARED_FIELD_PATHS` — the vocabulary
§2.4's comparisons name — which would have changed which conflicts exist and moved the committed golden
set. Inverting it severs the dependency in the only direction that can do harm: `COMPARED_FIELD_PATHS`
is unchanged (the 12 paths of §2.4, none of them `payments.*`), payment fields are still **not** compared
fields, and lineage covers all three sources regardless. The two containments that must still hold —
`COMPARED_FIELD_PATHS ⊆ LINEAGE_PATHS` and `SURVIVED_PATHS ⊆ LINEAGE_PATHS`, so §7's A→B→A scan and the
canonical view's `survived` block (§4.6) can name the provenance of every value they carry — are
assertions in `service/tests/er/test_materialization.py` rather than tautologies of the definition.

`value_canon` is `canon_value(v)` (§2.5) of the value that source's record **holds** — the staged raw
value, not its normalized form. Lineage records what a source *said*, so `customer` and `CUSTOMER` are
two different assertions of `crm.contact.lifecycle_stage`. The A→B→A oscillation scan of §7 compares
`value_canon` for **string** equality; §7's "≥25 re-asserting fields" counts distinct
`(person_key, field_path)` pairs. One row per `(person_key, field_path, generation, source_ref)` — and
**every** payment a person owns is written, not only the survived one, so a person holding three payments
carries three rows per payment path per generation.

---

## 4. Entity resolution (deterministic rule cascade)

Fuzzy similarity (splink / Jaro-Winkler) contributes **evidence signals only** and never a link decision.
No fuzzy or ML matching decides anything in this contract.

### 4.1 Refs, persons and `person_key`
A **source ref** is one of: `crm:contact:<crm_id>`, `crm:deal:<deal_id>`, `appdb:student:<id>`,
`appdb:enrollment:<id>`, `payments:payment:<payment_id>`.

**Identity refs** are `appdb:student:<id>` and `crm:contact:<crm_id>`, plus — and only for a payment that
the cascade attributes to **no** person — that payment's own `payments:payment:<id>`. Deals and
enrollments are never identity refs.

**PINNED — the shape of `is_identity_ref` (resolving MINOR-5).** The payment clause is a **scoped**
clause, so the predicate takes the scope as an argument rather than pretending the ref string carries
it:

```
is_identity_ref(ref, *, payment_attributed: bool = False) -> bool

  appdb:student:      -> True   always
  crm:contact:        -> True   always
  payments:payment:   -> True   iff  payment_attributed is False   # "attributed to NO person"
  crm:deal:           -> False  always
  appdb:enrollment:   -> False  always
```

The default is `payment_attributed=False` because the only place a `payments:payment:` ref legitimately
appears **inside a person's ref set** is the §5.2 entity "each payment attributed to no person"; a
payment the cascade *did* attribute contributes its ref to that person's ref set as evidence, never as
identity. `anchor_ref` therefore reads the default and is correct: a payment ref reaching it is, by
construction, an unattributed payment's own ref. Passing `payment_attributed=True` is how a caller that
knows the payment resolved states so, and it makes the C2/C11 refs of §5.4 — which are payment refs —
non-identity for the clean-sample probe. The flag scopes **only** the payment class; it can never make a
student or contact ref non-identity.

```
anchor_ref(person) = the single lowest-sorted identity ref of the person, under the source preference
                     appdb:student:  >  crm:contact:  >  payments:payment:
                     (prefer the earlier class outright; break ties within a class by byte order)
person_key   = uuid5(KEYSTONE_NS, anchor_ref(person))
canonical_id = person_key
```

`person_key` is a **pure function of `anchor_ref`** and is recomputed from the generation-N snapshot; it
is never carried in state. It is stable across generations because the **generator is forbidden from
changing a person's anchor source class between generations**: a person anchored on `appdb:student:` in
any generation retains a student ref in every later generation, and no person anchored on `crm:contact:`
or `payments:payment:` in gen 1 or 2 acquires a higher-preference identity ref in a later generation
(§10 `G25`; the detector-side twin is asserted in §9.2). It is **not** a hash of the ref set: the ref set
changes across generations and would split lineage. The full current ref set is stored as a separate
column on `entities` and is never used as a lineage key.

### 4.2 contact ↔ student
Link on the first rule that fires:

| id | rule |
|---|---|
| `L1` | `contact.external_id == student.id` (hard key) |
| `L2` | `norm_email(contact.email) ∈ {norm_email(student.guardian_email), norm_email(student.guardian2_email)}` **AND** `(first_norm, last_norm)` equal |
| `L3` | `(first_norm, last_norm, dob_norm)` equal, both `dob` non-null |

`L2` requires the name match precisely because siblings share the guardian email. A candidate pair is
**rejected** if either side is already `L1`-linked to a different record (hard keys win).

### 4.3 payment ↔ person
| id | rule |
|---|---|
| `P1` | `payment.external_ref == student.id` |
| `P2` | `k := payer_email_norm` is a `household_key` (§4.8 — the set of **primary** `guardian_email` values only; `guardian2_email` is never matched here) **AND** `norm_name(metadata.student_first_name) == first_norm` **AND** `norm_name(metadata.student_last_name) == last_norm` for **exactly one** member of `household_members_appdb(k)` — attribute to that member |
| `P3` | `k := payer_email_norm` is a `household_key` **AND** `\|household_members_appdb(k)\| == 1` — attribute to that one child |

No name splitting is performed on either side; both sides call the same `norm_name`. No other payment
attribution is made — an unattributable payment is C2, not a guess.

### 4.4 payment ↔ enrollment
Evaluated only for payments already attributed to a person by `P1..P3`. Because an enrollment is 1:1 with
a student (§1.4), this can never be ambiguous.

| id | rule |
|---|---|
| `E1` | the person's enrollments whose `program_norm` equals `norm_enum('program', metadata.program)` — attribute if **exactly one** matches |
| `E2` | the person has **exactly one** enrollment — attribute to it |

Under `G12` a person has **at most one** enrollment, so `E1` and `E2` can never disagree: `E1` is retained
as the **documented** attribution semantics (and as the branch that survives a future relaxation of the
1:1 rule), not as a live discriminator. Nothing in §11 is justified by exercising an `E1`-vs-`E2`
distinction.

Otherwise the payment is **unattributed to any enrollment**. Three rules are affected, and they are
affected differently:

- `R-013` (C13) yields `verdict='unchecked'` with `detail.reason='enrollment_unattributed'` — never a
  conflict.
- `R-012` (C12) falls back to `norm_enum('program', metadata.program)` exactly as §5.5 specifies, and
  yields `unchecked` with the same reason **only** when that is null or unmappable.
- `R-007` (C7) is **enrollment**-scoped, not payment-scoped. An unattributed payment simply does not count
  toward C7's paid-`deposit`/`tuition` test; C7 is **never** made `unchecked` by one. This is the path
  §12 D-6 relies on for the C8 child dropped from `payments`.

### 4.5 deal ↔ person
| id | rule |
|---|---|
| `D2` | `deal.associated_contact_ids` contains the person's `crm_id` |

**`D2` is the only deal-to-person link rule.** `enrollment.crm_deal_id` is the pointer **under test** by
C9 and must never be used as a link rule. A deal resolves to a **set** of persons (a household deal names
2–4 siblings); an empty set is `unchecked`, never a conflict (§5.5 C9).

### 4.6 Survivorship
Canonical field values: app DB > CRM > payments for identity fields; payments authoritative for money;
**when one source contributes multiple records to a person, the survived per-source value is taken from
the record with the lexicographically smallest source ref (byte order)** — for CRM contacts that is the
lowest `crm_id`. This is deterministic and independent of iteration order, and it is what decides the
survived CRM value for the 300 planted C3 duplicate pairs. Generation is *not* a tiebreak: current state
is generation 3 by definition (§7).

Survivorship never suppresses a conflict — conflicts are computed from the *sources*, not from the
survived value.

### 4.7 Links and link candidates
```
entity_links(canonical_id, source_ref, resolved_ref, link_class, method, generation)
```
`entity_links` holds **accepted** links only — one row per accepted pair per generation. `canonical_id` is
the `person_key` (§4.1) of the person the pair resolves into.
`link_class ∈ {contact_student, payment_person, payment_enrollment, deal_person}`, and `method` is the id
of the **first** cascade rule that fired for that pair:
`method ∈ {L1, L2, L3, P1, P2, P3, E1, E2, D2}` (§§4.2–4.5). `method` is written by `recon/er.py` and is
**never** re-derived by a rule — `R-004` reads it off this column (§5.5 C4), and `G31`(a) asserts every
planted conflict's links carry the **expected** `method`.

```
entity_link_candidates(source_ref, key_class, resolved_ref, generation [, decision, reason])
```
**Every** candidate pair produced by `match_keys` is persisted here regardless of outcome, including
resolutions the cascade discarded. `key_class ∈ {ext, email, namedob}`. `entity_links` holds **accepted**
links only. `R-010` is evaluated over `entity_link_candidates`, never over `entity_links`. The
`decision` / `reason` columns are `[+]` and additionally supply inspectable evidence signals to the
confidence model.

### 4.8 Household inference
```
household_key(student) = norm_email(student.guardian_email)          # PRIMARY guardian email ONLY
household_members_appdb(k) = { app-DB students with household_key == k }
household_members(k)       = household_members_appdb(k)
                             ∪ { CRM contacts with norm_email(contact.email) == k }
```

- Grouping is by **exact** `household_key` — explicitly **not** transitive closure over shared addresses,
  and never union-find.
- `guardian2_email` is corroborating evidence only and is **never** part of the key.
- `appdb.student.household_id` is a corroborating signal only and never decides membership.
- `P3`'s "exactly one child" and C8's "≥2 children" both evaluate `|household_members_appdb(k)|`.
- **The set of `household_key` values is the ONLY email set `P2`/`P3` consult.** `guardian2_email`
  participates in `L2` (§4.2) and nowhere else. There is no set called "household guardian emails".
- `household_anchor_student(k)` = the member of `household_members_appdb(k)` with the lexicographically
  smallest `appdb:student:<id>` ref (ties are impossible — student ids are unique). The household's
  **anchor enrollment** is that student's enrollment; it is what `G29` and §1.2 mean by the household's
  one `program`. Committed as `recon/reference.py:household_anchor_student`.
- Committed as `recon/reference.py:household_key` and imported by generator and detector alike.

**PINNED — `household_members` is EXPORTED, not left as a definition (ruling 15).** All three of
`household_key`, `household_members_appdb` and **`household_members`** are committed callables in
`recon/reference.py` and are on its `__all__`. §0's rule is that neither side may re-implement a shared
symbol, and an unexported symbol that this document nevertheless defines is exactly a symbol a consumer
will re-implement — the R23 drift this contract exists to prevent. The same applies to
`KEY_CLASSES` (§2.1, §4.7), which is exported from `recon/normalize.py`.

Its pinned shape:

```
household_members(students, contacts=()) -> dict[household_key, tuple[member, ...]]
```

- **The key set is exactly the `household_key` values of the supplied STUDENTS.** A household is defined
  by app-DB students; a CRM contact whose `norm_email` matches no student's `household_key` is a
  deal-less lead (§11.4, `G11`) and is a member of **no** household. It never creates a key.
- Members are ordered **app-DB students first**, ascending by `appdb:student:<id>` ref — so `[0]` is
  `household_anchor_student(k)`, identically to `household_members_appdb` — **then** CRM contacts,
  ascending by `crm:contact:<crm_id>` ref. Both orderings are total, so the result never depends on
  input order.
- A contact matching a key contributes **one** member per contact record, so a planted C3 duplicate pair
  (two contacts, one student — §5.6 C3) contributes two contact members. This is a *record* view, not a
  person view: `|household_members_appdb(k)|` — never `|household_members(k)|` — is what `P3`'s "exactly
  one child" and C8's "≥2 children" evaluate (above), and mixing the two would let a C3 duplicate change
  a household's child count.

The generator gives every child in a household the same `guardian_email` **value** (dirty variants are
fine — they normalize equal), which makes the detector's inference exact (§10 `G1`).

---

## 5. Conflict catalogue

### 5.1 Comparison semantics (normative)
A `COMPARED_FIELDS` comparison is evaluated **only when both sides normalize to a non-`None` value**.
`None` on either side yields `verdict='unchecked'` for that comparison and is **never** a disagreement.

**PINNED — the `None` causes are THREE, disjoint and exhaustive (ruling 5).** v1 named two, which left
a present-but-unparseable non-enum value — an unparseable `crm.contact.dob`, a `crm.contact.first_name`
that is nothing but quote characters — with no reason it truthfully fits: it is not `missing_operand`
(the source value was **not** NULL) and it is not `unmapped_enum` (no enum was consulted; `norm_dob` and
`norm_name` are not table-driven). Whichever of the two an implementer picked would be a false statement
in `detail.reason`, and the generator and detector would pick differently. The closed set is:

| the operand normalized to `None` because… | pinned `detail.reason` |
|---|---|
| the source value was **NULL** | `missing_operand` |
| the source value was **present** and the row is **enum-mapped** (`grade`, `stage`, `lifecycle`) but `norm_enum` could not map it (§7) | `unmapped_enum` |
| the source value was **present** and the row is **not enum-mapped** (`name_first`, `name_last`, `dob`) and its normalizer returned `None` | `unparseable_value` |

Which of the three applies is a function of **the comparison row** and **whether the source value was
NULL** — never of a guess about the value's contents. The row's kind is pinned by §2.4's mapper column:
`norm_enum`-driven rows report `unmapped_enum`, `norm_name`/`norm_dob` rows report `unparseable_value`.

**Reason precedence when BOTH operands are `None`.** One comparison emits one reason, so the causes are
ordered: `missing_operand` > `unparseable_value` > `unmapped_enum`. A NULL operand is the most specific
and least ambiguous statement available, so it is reported whenever either side is NULL.

All three codes are reachable inside `R-006`/`R-014`; none is ever a conflict.

The SQL form is pinned as `a IS NOT NULL AND b IS NOT NULL AND a <> b`. The committed rule lint
additionally **fails any `rules/*.sql` containing `IS DISTINCT FROM`**.

A planted conflict may therefore never be created by nulling a field (§10 `G17`).

### 5.2 Emission unit, counting unit, entity
- **C3 and C11 emit exactly one entry per unordered pair** satisfying the predicate. An N-way collision
  would yield C(N,2) entries — and the generator is forbidden from creating any 3-or-more-way collision,
  so the count is never ambiguous (§10 `G8`). `entity_refs` for such an entry is the two source refs,
  sorted. C11's window is `abs(occurred_at delta) < C11_WINDOW_SECONDS` (600s), strictly.
- **C6 and C14 emit exactly one conflict per person per generation**, carrying every disagreeing path in
  `disagreeing_fields[]`. Their minimums count **persons**, not field pairs.
- **C6 and C14 compare survived values across sources only.** A disagreement between two records of the
  *same* source is never C6 or C14 — it is covered by C3 and C10.
- **Entity** = one resolved person (one `person_key` with ≥1 identity ref), **plus** each payment
  attributed to no person. This is the denominator of A.1's ≥85%-fully-consistent gate and of the
  scorecard's false-positive rate (§11.9).

### 5.3 Source completeness (correctness under partial-source failure)
`fixtures/manifest.json` records an expected gen-N record count per `(source, entity_type)`; ingestion
stamps `source_generations`. Any rule whose predicate depends on the **absence** of records from source S
— **C1, C2, C5, C7, C8, C9, C13** — is skipped for the whole run when S's generation-3 load is
incomplete, emitting `verdict='unchecked'` with `detail.reason='source_incomplete'`, never a conflict.
The run is marked `degraded`. Presence/agreement tests (C3, C4, C6, C10, C11, C12, C14) still run. The
suite exercises this with a stub adapter that 5xxs mid-stream.

### 5.4 `entity_refs`, fingerprint, harness key
`entity_refs` for every conflict = **sorted** set of refs per the per-type spec in §5.5. Generator and
detector build this list with the same helper, `recon/reference.py:conflict_refs`.

**The generator never authors an `entity_refs` list.** Seeding is two-pass (§10 `G31`): pass 1
materializes the fixtures; pass 2 runs the detector's own `recon/er.py` over those fixtures and derives
every golden entry's refs from `conflict_refs` applied to that output.

`entity_refs` may contain **only** refs to records present in the **generation-3** snapshot of their
source (§10 `G24`).

```
fingerprint = sha256(
    type
  | "\x1f".join(sorted(canon_value(r) for r in entity_refs))
  | "\x1f".join(sorted(canon_value(p) for p in disagreeing_fields))
  | "\x1f".join(f"{k}={canon_value(v)}" for k, v in sorted(observed_values.items()))
)
```
`observed_values` is a **map**; `canon_value` (§2.5) is used by both sides.

**PINNED — the exact wire format (ruling 3).** Someone re-deriving the digest from this document alone
must land on the same bytes, so every byte of the payload is normative and none of it is a formatting
choice:

| element | pinned value | notes |
|---|---|---|
| hash | **`sha256`**, lower-case hex, **64 characters** | never `sha512`, never `blake2`, never a truncation |
| payload encoding | **UTF-8**, no BOM | the payload is built as one `str` and encoded once |
| **section separator** | the single literal character **`\|`** (U+007C VERTICAL LINE) | exactly four sections, joined by **three** separators; no surrounding whitespace, no trailing separator |
| section 1 | `type` **verbatim**, e.g. `C11` | the committed `CONFLICT_TYPES` spelling: upper-case `C`, no zero padding, never the `R-0NN` rule id |
| **intra-section joiner** | the single character **`\x1f`** (U+001F, US) | used inside sections 2, 3 and 4; an empty section is the empty string |
| section 2 | `sorted(canon_value(r) for r in entity_refs)` joined by `\x1f` | each ref **escaped by `canon_value`** (§2.5) first, then sorted ascending by code point over the **escaped** encodings, **after** de-duplication (§5.5's per-type shape) |
| section 3 | `sorted(canon_value(p) for p in disagreeing_fields)` joined by `\x1f` | same escaping and ordering as section 2; empty (`""`) for every type but C6 / C14 |
| **section 4 item form** | **`f"{k}={canon_value(v)}"`** | the key **verbatim**, then one literal `=` (U+003D), then the canonical value. No space either side of the `=`, no quoting of `k`, no `repr`, no JSON |
| section 4 ordering | `sorted(observed_values.items())` — **by key**, ascending by code point | the map is hashed, so the key set is pinned per type (table above) and an unpinned key is drift |

**PINNED — sections 2 and 3 are ESCAPED, and the payload is therefore INJECTIVE.** Every element of
sections 2, 3 and 4 passes through `canon_value` (§2.5) before it is joined, so no element can contain a
raw `\x1f` and the four sections are unambiguously decodable from the payload. Embedding a ref verbatim
between `\x1f` joiners is exactly the defect §2.5 spells out for sequences: without the escaping,

```
fingerprint("C8", ["appdb:student:a\x1fappdb:student:b"], ...)
  ==  fingerprint("C8", ["appdb:student:a", "appdb:student:b"], ...)
```

— one ref carrying the joiner and two separate refs hash to the **same** digest. Those are two different
conflicts over two different populations sharing one fingerprint, and the fingerprint is the idempotency
key R16's oscillation dedup and the whole proposal pipeline are keyed on; a collision there silently
suppresses a real second proposal. Two independent guards close it: **`make_ref` refuses any
`natural_key` containing a control character (`\x00`–`\x1f`)**, so a colliding ref is not constructible,
and the payload escapes its elements anyway for a ref that reaches the hash without passing through
`make_ref`.

Sorting is over the **escaped** encodings, matching §2.5's sequence case. No committed ref and no
`COMPARED_FIELDS` path contains a backslash, `\x1f` or `\x1e`, so escaped and raw order coincide for
every value this contract can produce and **no committed digest literal moves** — but only one of the two
orders may be pinned, and it is this one. Worked, on the one input shape where they part company:

```
# Python string literals: `\x1f` is the RAW joiner, `\\x1f` its four-character escape
entity_refs = ["appdb:student:a\x1fb", "appdb:student:aZ"]
section 2   = "appdb:student:aZ" + "\x1f" + "appdb:student:a\\x1fb"
```

The joiner-bearing ref sorts **first** raw (U+001F < U+005A) and **last** escaped (its `\x1f` becomes a
leading backslash at that position, U+005C > U+005A). Section 3 behaves identically on
`["crm.contact.grade\x1fx", "crm.contact.gradeZ"]`.

A worked example, byte for byte — `type="C8"`, one ref, no disagreeing fields, three observed values:

```
payload = "C8" + "|" + "appdb:student:s7" + "|" + "" + "|"
        + "dropped_source=crm"        + "\x1f"
        + "eligible_member_count=3"   + "\x1f"
        + "household_key=parent@corp.com"
fingerprint = sha256(payload.encode("utf-8")).hexdigest()
```

Note that section 3 is present and empty, so the payload contains `...s7||dropped_source...` — three
separators, always. Golden digest literals for a table of representative conflicts are committed in the
test suite; they are what makes this table enforceable rather than decorative, because a serialization
change that no structural assertion notices still moves every digest.

The harness matches a detected conflict to a golden entry on `(type, tuple(sorted(entity_refs)))`. For
every matched pair it **additionally** asserts equality of `sorted(disagreeing_fields)`,
`sorted(sources_involved)`, `sorted(observed_values.keys())` and `expected_verdict`, printed as
**field-exactness detail lines** on the scorecard. A mismatch fails the suite; it is **not** a third
scorecard category — the harness reports exactly the brief's two categories, false negative and false
positive.

**Unmatched detections and unmatched golden entries are the categories.** A detected conflict that matches
**no** golden entry **is a false positive**, and a golden entry matched by **no** detected conflict is a
false negative — *regardless of whether any of its refs intersects `golden/clean-sample.json`*. The
clean-sample intersection test of §8 is an **additional, stricter probe** on a 1,000-entity subsample; it
is never the definition of the category. Note that C2's and C11's `entity_refs` are payment refs, which
are identity refs only for an *unattributed* payment (§4.1) — so the clean-sample probe alone can never
see a spurious C11, and the unmatched-detection rule is what grades it.

**`observed_values` keys are pinned per type.** The fingerprint hashes the map, so an unpinned key set is
generator/detector drift with no check to catch it. Values are always `canon_value(v)` (§2.5).

| type | `observed_values` keys |
|---|---|
| C1 | `paid_payment_refs`, `enrollment_ref`, `d2_deal_count` |
| C2 | `payer_email_norm`, `external_ref`, `metadata_name_pair_present` |
| C3 | `email_norm`, `first_norm`, `last_norm`, `dob_norm_a`, `dob_norm_b` |
| C4 | `contact_email_norm`, `student_guardian_email_norms`, `link_method` |
| C5 | `status_funnel`, `linked_contact_count`, `attributed_payment_count` |
| C6 / C14 | **one entry per disagreeing comparison, keyed by the source-qualified path** (§2.4) — both endpoints of every disagreeing row, value = that side's normalized value |
| C7 | `enrollment.stage_funnel`, `enrollment.deposit_paid_at`, `paid_deposit_payment_count` |
| C8 | `household_key`, `dropped_source`, `eligible_member_count` |
| C9 | `enrollment.crm_deal_id`, `deal_present_gen3`, `deal_person_refs` |
| C10 | `ext_resolved_ref`, `namedob_resolved_ref`, `first_norm`, `last_norm`, `dob_norm` |
| C11 | `payer_email_norm`, `amount_cents`, `type`, `occurred_at_delta_seconds` |
| C12 | `amount_cents`, `expected_amount_cents`, `program_norm`, `type` |
| C13 | `refunded_at`, `enrollment.updated_at`, `enrollment.stage_funnel`, `student.status` |

A key absent from a type's row may not be emitted; a key present in it is required.

**PINNED — the VALUE construction of the three multi-valued keys (ruling 16).** The
fingerprint hashes the map, so pinning a key without pinning how its value is built
leaves the two sides free to hash different bytes for the same conflict. All three are
sequences and reach `canon_value`'s sequence case (§2.5):

| key | value |
|---|---|
| `C1.paid_payment_refs` | the sorted `payments:payment:<id>` refs of the person's `paid` payments |
| `C4.student_guardian_email_norms` | the sorted `norm_email` values of the student's `guardian_email` and `guardian2_email`, NULLs dropped |
| `C9.deal_person_refs` | **one `anchor_ref` (§4.1) per person** in the mispointed deal's `D2`-resolved person set, sorted — **not** each person's identity-ref set, and not a `person_key` |

`C9.deal_person_refs` is `anchor_ref`s because §5.5 pins C9's `entity_refs` as the
*enrollment's* person's refs while the mispointed deal's person "appears in
`observed_values`" — one entry per person is what makes the map's cardinality equal the
person-set cardinality the predicate tested. Expanding to identity refs would make a
household deal's entry vary with how many contact records each sibling happens to carry,
which is not a property C9 is about.

### 5.5 The catalogue

Detection is evaluated on **generation 3**. Every FP-guard cell cites the §10 construction constraint
that makes it true; none of them is an assertion about how the world behaves.

| id | rule | type | min | detection (generation 3) | entity_refs | FP guard (made true by) |
|---|---|---|---|---|---|---|
| C1 | `R-001` | paid-but-no-deal | 500 | person has ≥1 `paid` payment **and** ≥1 enrollment, but 0 `D2`-linked CRM deals | identity refs | Every person holding ≥1 `paid` payment and ≥1 enrollment is given a `D2`-linked household deal by construction; the 18,175 deal-less leads have no payment, no enrollment and no student link. The 75 C8 children dropped from `crm` are the one other zero-deal population and are suppressed by `PRECEDENCE` 8 (§12 D-6). **`G9`, `G10`, `G11`, `G36`** |
| C2 | `R-002` | payment-with-no-person | 200 | payment links to no person by `P1..P3` | `payments:payment:<id>` | Every non-planted payment satisfies one of `P1..P3` by construction; the joint `external_ref`+`metadata`-name gap is forbidden. **`G6`** |
| C3 | `R-003` | duplicate-by-email (in-source) | 300 pairs | two CRM contacts, generation 3, equal `email_norm` **and** equal `(first_norm, last_norm)` **and** (`dob_norm` equal or either null) | the two contact refs, sorted | Siblings share the guardian email but are guaranteed to differ in `(first_norm, last_norm)`; no other same-`email_norm` contact pair may exist. **`G5`, `G23`, `G8`** |
| C4 | `R-004` | same-person-different-emails | 250 | contact↔student where `entity_links.method == 'L3'` (read off `entity_links`, never re-derived) **and** `norm_email(contact.email) ∉` the student's normalized guardian emails | identity refs | Dot/`+alias` local-part variation is emitted **only** on `gmail.com`/`googlemail.com`, where it normalizes equal so clean pairs link by `L2`; on every other domain all addresses of one clean person are byte-identical after `norm_email`. **`G3`, `G4`** |
| C5 | `R-005` | record-in-one-source-only | 400 | student with `STATUS_TO_FUNNEL(status) == enrolled` **and** no `entity_links` contact **and** no `P1..P3`-attributed payment | identity refs | Legitimately partial-presence students (A.1's ~30%) carry `status ∈ {prospect, applied, withdrawn}` **only**; `enrolled`/`active` implies a CRM contact and a payments footprint unless the student is a planted C5. **`G16`, `G2`** |
| C6 | `R-006` | field disagreement | 500 | linked person with ≥1 disagreeing `COMPARED_FIELDS` comparison whose disagreeing-path set is **not** wholly ⊆ `SENSITIVE_FIELDS` | identity refs | Dirt (case/whitespace/`Grade 4`) normalizes away; a `None` operand is `unchecked`, never a disagreement; **every** household — planted or not — is funnel-uniform under `G18`, whose only relaxations are the four it names by conflict class; every C3 duplicate contact and the C10 collapsed contact carry `grade`/`lifecycle_stage` identical to the student they link to (`G23`, `G21`), so survivorship (§4.6) cannot manufacture a disagreement. **`G17`, `G18`, `G21`, `G23`, `G37`** |
| C7 | `R-007` | enrolled-but-unpaid | 300 | enrollment `stage_funnel ∈ PAID_IMPLYING_STAGES` **and** no `paid` payment of type `deposit\|tuition` attributed to *that enrollment* by `E1`/`E2`. `deposit_paid_at` is **never** a trigger — it is a retained historical fact on `refunded`/`withdrawn` enrollments and appears in `observed_values` only | identity refs + `appdb:enrollment:<id>` | Enrollments with `stage_funnel ∈ {prospect, applied, waitlisted, withdrawn, refunded}` are exempt. A correctly-reflected refund (enrollment moved to `refunded`/`withdrawn`, `deposit_paid_at` retained) is never C7. The substantive guard is `G38`: a paid-implying `stage_funnel` is **drawn only for children of payments-present households**, so no `{appdb, crm}` or `{appdb}`-only enrollment can reach this predicate. **`G38`, `G14`, `G35`**; the C5 plant is suppressed by `PRECEDENCE` 4, the C13 plant by `PRECEDENCE` 5 and the C8 dropped child by `PRECEDENCE` 8 |
| C8 | `R-008` | dropped sibling | 150 | household (§4.8, `\|household_members_appdb(k)\| ≥ 2`) where **exactly one** eligible child is absent from **exactly one** of the **downstream** sources `{crm, payments}` in which *all* other eligible children are present. **Presence is defined, not assumed:** a child is **present in `crm`** iff it has ≥1 `entity_links` row with `link_class == 'contact_student'`; **present in `payments`** iff it has ≥1 payment attributed to it by `P1..P3`. These are the only two presence predicates, they are evaluated on **generation 3**, and `G2` binds the generator's mask to the same two functions. `appdb` presence is definitional (§4.8 membership) and the app DB is never the dropped source | dropped child's identity refs | A child is excluded from the mask when `GRADE_ORDER[grade_norm] < GRADE_ORDER[ENROLLMENT_GRADE_FLOOR]`, or `student.status == 'withdrawn'`, or their enrollment `stage_funnel ∈ {withdrawn, refunded}`. Within any multi-child household all non-planted children share an identical presence mask **by construction** — A.1's ~30% partial-presence draw is applied to whole households at once, or to single-child households. **`G2`, `G22`** |
| C9 | `R-009` | stale pointer | 100 | `enrollment.crm_deal_id` names a deal **absent from the generation-3 CRM snapshot**, **or** names a deal whose `D2`-resolved person set is **non-empty and does not contain** the enrollment's person. An empty person set yields `verdict='unchecked'`, `detail.reason='deal_unresolved'` | the **enrollment's** person's identity refs + `appdb:enrollment:<id>`. The mispointed deal and its person appear in `observed_values`, never in `entity_refs` | Null `crm_deal_id` (~40%) is not a conflict; a household deal whose `associated_contact_ids` lists every sibling contact resolves to a person **set containing** the enrollment's person and is never C9. **`G9`, `G20`** |
| C10 | `R-010` | merge-collapsed record | 50 | one CRM contact whose `("ext")` candidate and `("namedob")` candidate resolve to **two different, non-null** students in `entity_link_candidates` | exactly three refs — `crm:contact:<id>`, the `appdb:student:<id>` reached by the `ext` key, and the `appdb:student:<id>` reached by the `namedob` key. **No transitive expansion** to either person's other refs | Globally, `(first_norm, last_norm, dob_norm)` is unique across all students and all contacts except the tuples the C3/C10 planters registered, so a normal contact's two key classes resolve to the same student or to none. **`G5`, `G21`** |
| C11 | `R-011` | duplicate payment | 50 pairs | two payments with equal `(payer_email_norm, amount_cents, type)` whose `occurred_at` differ by `< 600s` **and** which both resolve by `P1..P3` to the **same** person. If either resolves to no person, C11 does not fire (C2 covers it) | the two payment refs, sorted | Siblings in a multi-child household share `payer_email_norm` and the flat `fee`=10000 (and the same `deposit` when in the same program); a sibling pair resolves to **two different persons** and is therefore never C11. Planted pairs are ≤300s apart; every legitimate same-person same-type repeat is ≥1200s apart. **`G7`, `G8`** |
| C12 | `R-012` | wrong-amount payment | 100 | `amount_cents` ≠ the fee-schedule amount for `(program, type)`, where `program = program_norm` of the `E1`/`E2`-attributed enrollment; if no enrollment is attributed, `norm_enum('program', metadata.program)`; if that is null or unmappable, `unchecked` | identity refs + payment ref | Every non-planted payment's `amount_cents` is exactly the fee-schedule value for its `(program, type)`, and `metadata.program` equals the attributed enrollment's `program`. **`G13`, `G34`** |
| C13 | `R-013` | refund not reflected | 100 | payment `status='refunded'` where (a) it is the person's **most recent** payment of that `type` on the `E1`/`E2`-attributed enrollment, (b) **no** later `paid` payment of the same `type` exists for that person, (c) `refunded_at` post-dates the enrollment row's `updated_at`, and (d) the enrollment `stage_funnel ∈ PAID_IMPLYING_STAGES` **and** `STATUS_TO_FUNNEL(student.status) == enrolled` | identity refs + payment ref + `appdb:enrollment:<id>` | Every non-planted refunded payment is **either** superseded by a later `paid` payment of the same type (≥1200s later) **or** has its enrollment moved to `refunded`/`withdrawn` **and** its student status out of `{enrolled, active}`. **`G14`, `G15`** |
| C14 | `R-014` | sensitive-field-only fix | 50 | linked person with ≥1 disagreeing `COMPARED_FIELDS` comparison whose disagreeing-path set is **non-empty and wholly ⊆ `SENSITIVE_FIELDS`**. The empty set never fires C14 | identity refs | The empty set is excluded by the predicate itself; a `None` operand is `unchecked`; name/DOB plants are forced to link by `L1` so the pair is resolvable despite the disagreement. **`G17`, `G37`** |

**Rule scope** (what §5.8's R8 stamping means — each rule stamps one `invariant_results` row per row of
the `stg_*` table(s) named here, in generation 3):

| rules | scope |
|---|---|
| `R-001`, `R-005`, `R-006`, `R-008`, `R-014` | `stg_student` |
| `R-002`, `R-011`, `R-012`, `R-013` | `stg_payment` |
| `R-003`, `R-004`, `R-010` | `stg_crm_contact` |
| `R-007`, `R-009` | `stg_enrollment` |
| — | `stg_crm_deal` is in the scope of **no** rule; every `stg_crm_deal` row therefore carries the synthetic `R-000` row of §5.8 |

### 5.6 Generator preconditions per conflict class
These are the plant-side obligations; each is asserted by `G31`'s plantability pass before `golden/` is
written, and by the named checks in §10.

| class | precondition |
|---|---|
| C1 | Plants sit in **single-child** tri-source households, hold a `paid` `deposit` payment at the exact fee-schedule amount, hold exactly one enrollment at `deposit_paid`, and have `crm_deal_id IS NULL` and **no** `D2` deal. |
| C2 | Plants are the **only** payments in the dataset that omit both `external_ref` and the `metadata` name pair; `payer_email` is drawn from an address used by no student and no contact. |
| C3 | The pair meets the full predicate (equal `email_norm`, equal `(first_norm, last_norm)`, `dob_norm` equal or either null) and is registered in the name-collision allowlist. Exactly two contacts per collision. **Both contacts carry `external_id == student.id` for the *same* student**, so both link by `L1` regardless of the duplicate's `dob`, and the pair contributes **one** person to §11.9's denominator, not two. **Both carry identical `COMPARED_FIELDS` values** (`grade`, `lifecycle_stage`) and both agree with that student, so §4.6's lowest-`crm_id` tiebreak cannot manufacture a C6/C14 on a C3 person (`G23`). |
| C4 | `contact.external_id IS NULL`; `contact.dob` and `student.dob` both non-null and equal; `(first_norm, last_norm)` equal; `norm_email(contact.email) ∉ {norm_email(guardian_email), norm_email(guardian2_email)}` so `L2` also cannot fire; the variant address is on a **non-gmail** domain so it cannot normalize back. |
| C5 | App-DB-only students (mask `{appdb}`) carrying `status ∈ {enrolled, active}`; the only students in the dataset allowed to do so with no contact and no payment. |
| C6 | 300 grade-only, 120 lifecycle-only, **80 mixed** (a `name_*`/`dob` path together with a `grade` or `lifecycle` path) so name-spelling disagreements demonstrably appear under C6 too (§12 D-2). Every mixed and every name/DOB-bearing plant carries `contact.external_id == student.id` so `L1` links it. Lifecycle plants draw both sides from the opinionated (non-`None`) subset of `LIFECYCLE_TO_FUNNEL`. |
| C7 | Plants sit in single-child households, hold exactly one enrollment at `deposit_paid`/`enrolled`, hold a `paid` `fee` payment (so they remain payments-present) and **no** `paid` `deposit`/`tuition`, and hold a `D2`-linked deal (so C1 cannot fire). |
| C8 | Drawn from tri-source multi-child households. 75 drop the child from `crm`, 75 from `payments`. The dropped child is otherwise clean; children dropped from `crm` retain a `paid` `deposit` payment. `PRECEDENCE` 8 suppresses the mechanically-implied C1/C7. |
| C9 | 50 point at a `deal_id` absent from the generation-3 CRM snapshot; 50 point at a deal whose `associated_contact_ids` is non-empty and resolves by `D2` to **exactly one other** person, whose contact is `L1`- or `L2`-linked so the resolution is guaranteed. Target deals for the second branch are single-child-household deals. **Branch-1 targets are supernumerary deals** — they exist in generations 1–2 only (§11.7) and the pointing enrollment's household **retains its live gen-3 `D2` deal**, so the deletion never turns the person into an unplanted C1 (`G9`). |
| C10 | `contact.external_id` non-null and resolving to student A; `contact.dob` non-null and equal to student B's `dob`; `(first_norm, last_norm)` equal to student B's; A ≠ B; student B retains its own separate linked contact so `R-005` does not fire on it. Registered in the name-collision allowlist. The contact's `grade` and `lifecycle_stage` **agree with student A** (the person it `L1`-links into), so the *only* disagreeing paths it induces are `name_first`/`name_last`/`dob`. **The induced disagreement is expected**: those three paths are wholly ⊆ `SENSITIVE_FIELDS`, so `R-014` would emit 50 C14 entries the golden set does not carry — `PRECEDENCE` 2 suppresses them, and `G21` asserts no C6 **or** C14 entry survives the filter for any person whose identity refs contain the collapsed contact ref. Neither student A nor student B carries any other plant. |
| C11 | Both payments belong to the **same** person, same `type`, same `amount_cents`, `≤300s` apart, both attributable by `P1..P3`. Exactly two payments per collision. |
| C12 | `amount_cents` is set off the fee schedule by a value that cannot coincide with another `(program, type)` cell; the person has exactly one enrollment and `metadata.program` (when present) matches it. |
| C13 | The person's attributed enrollment is at a paid-implying stage **and** `student.status ∈ {enrolled, active}` — **both** downstream fields left stale. `refunded_at > enrollment.updated_at`, and the enrollment is excluded from the ~0.5% out-of-order timestamp dirt (`G26`) so clause (c) is dirt-free. No later `paid` payment of the same type. Plants sit in **single-child** households so funnel-uniformity (`G18`) does not propagate a paid-implying stage to a sibling that cannot back it. Partially-reflected refunds are **not** planted, since the AND predicate cannot see them. |
| C14 | 30 name-only, 10 dob-only, 10 stage-only. Every name/DOB plant carries `contact.external_id == student.id` (`L1` is the only cascade rule that can link a pair whose names or DOBs disagree). Stage-only plants sit in single-child households so no sibling inherits the disagreement. |

### 5.7 Precedence (committed in `recon/reference.py:PRECEDENCE`, imported by generator *and* detector)

**PINNED — the matching predicate is `entity_refs` set INTERSECTION (ruling 9).** For every suppression
rule below (rules 2–8), a surviving **winner** entry suppresses a **loser** entry **iff**

```
set(loser.entity_refs) ∩ winner_refs  ≠  ∅
```

where `winner_refs` is the union of the `entity_refs` of every surviving entry of the winner's type,
**filtered by that rule's ref-class prefix where the rule names one**. It is **not** ref-set equality,
**not** subset containment, **not** a match on the person's anchor ref, and **not** a re-resolution
through `entity_links`. Set intersection is the same predicate §8 uses to flag a `golden/clean-sample`
entity ("FLAGGED iff any detected conflict's `entity_refs` INTERSECTS that entity's identity refs"), and
using one predicate in both places is what keeps the suppression count and the false-positive count
consistent. Equality would fail immediately: C7 carries `identity refs + appdb:enrollment:<id>` while
C13 carries `identity refs + payment ref + enrollment ref`, so rule 5 would never fire and the C7
population would come out at 400 instead of 300.

**PINNED — rule 2 is keyed ONLY on the collapsed `crm:contact:` ref (ruling 9).** C10's `entity_refs`
are exactly three (§5.5): `crm:contact:<id>` and **two different students'** `appdb:student:<id>`. Only
the contact ref may enter `winner_refs` for rule 2. Including the student refs would suppress C6/C14/C4
on **student B** — a person the collapse did not damage, who retains its own separate linked contact
(`G21`) and whose conflicts are ordinary golden entries. That is a false-negative machine, and it is why
rule 2 alone carries a ref-class filter while rules 3–8 take the winner's whole ref set.

1. **C14 over C6** — if a person's disagreeing paths are *entirely* sensitive, the conflict is C14 and
   `R-006` must not also emit C6 for that person. Mixed sets emit C6 only, with the sensitive paths listed
   in `disagreeing_fields` (the proposal is still `sensitive_hold`). The set classified is the **union**
   of the co-located pair's `disagreeing_fields`, and the union being **empty** keeps the **C6**: §5.5's
   C14 predicate is *non-empty and wholly ⊆ `SENSITIVE_FIELDS`*, so the empty set never fires C14 and the
   partition falls back to C6 rather than holding a proposal on an empty sensitive set. *(Degenerate —
   nothing in `golden/` is built that way — but rule 1 is a total function and the fallback is pinned
   here rather than left to the implementation.)*
2. **C10 over C6/C14/C4** — C6, **C14** and C4 are suppressed for any conflict whose `entity_refs`
   contain the collapsed contact ref. *(Derived in v2, and load-bearing: the C10 contact is `L1`-linked to
   student A while its `(first_norm, last_norm, dob_norm)` equals student **B**'s, so the `name_first`,
   `name_last` and `dob` comparisons on person A necessarily disagree. All three paths are in
   `SENSITIVE_FIELDS`, so under §2.4's partition the disagreeing set is wholly sensitive and `R-014`
   would emit **50** C14 entries against a §11.8 C14 budget of exactly 50 that contains none of them —
   the false-positive class this rule exists to close. C4's clause is now vestigial: C4 requires
   `entity_links.method == 'L3'` and the C10 contact links by `L1`; it is retained for defence in depth.
   See §12 D-2.)*
3. **C2 over C12/C11** — an unattributable payment cannot have a wrong amount or a duplicate partner.
4. **C5 over C1/C7** — a single-source student cannot also be paid-but-no-deal. The **C5-over-C7** half
   is live and carries the 400 C5 plants, whose enrollments sit at a paid-implying stage with no payment
   (`G38`). *(The C5-over-C1 half is vacuous — C5 requires no `P1..P3`-attributed payment while C1
   requires a `paid` one; retained for defence in depth.)*
5. **C13 over C7** — a person whose only `deposit|tuition` payment is `refunded` while the enrollment
   still reads paid-implying is C13; `R-007` must not also emit C7 for that enrollment.
6. **C9 over C1** — an enrollment with a stale `crm_deal_id` is C9; `R-001` must not also emit C1 for that
   person on the strength of the stale pointer. *(Vacuous under `G9` and §4.5's `D2`-only link rule — C1
   counts `D2`-linked deals and never reads `crm_deal_id`, and `G9` guarantees every C9 plant's household
   holds a live `D2` deal. Retained for defence in depth; `sc_construction_sweep` asserts it fires zero
   times.)*
7. **C10 over C5** — retained as a **defensive suppression only**. Under `G21` the collapsed contact
   `L1`-links to student A and student B retains its own linked contact, so **no student is left
   contact-less** and this rule is expected to fire zero times. A non-zero count is a construction bug:
   `sc_c10_preconditions` fails the seed run.
8. **C8 over C1/C7** — the dropped child of a detected C8 is covered by C8; `R-001` and `R-007` must not
   also fire on that child. *(Derived in v2: a child dropped from `crm` mechanically has zero `D2` deals
   and a child dropped from `payments` mechanically has no paid deposit. Neither is constructible away
   without breaking household funnel-uniformity — see §12 D-6.)*
9. **C3 does not suppress C6**, and **C14 does not co-occur with C6** (rule 1). All remaining ordered
   pairs co-occur freely.
10. `golden/conflicts.json` is written **through the same `PRECEDENCE` filter the detector applies** — the
    generator plants intent, the filter decides which entries survive. The ≥10% compound ratio (A.5) is
    `count(surviving entries with non-empty compound_with) / count(surviving entries)` (§8), and pairs
    removed by the mechanical suppressions — rule **2** and rules **4–8** — never appear in
    `compound_with` and so do **not** count toward it.
11. `(type, tuple(sorted(entity_refs)))` is **UNIQUE** across `golden/conflicts.json`; the manifest
    self-check fails on a duplicate key rather than letting the harness loader dedupe it silently.

### 5.8 Records with no applicable invariant (R8)
Every `stg_*` row is stamped in `invariant_results` for every rule whose scope includes it. A row in scope
of **zero** rules gets one synthetic row `(rule_id='R-000', verdict='unchecked',
detail.reason='no_rule_in_scope')`.

Rule scope is pinned in the table below §5.5. `stg_crm_deal` is in the scope of no rule, so **every**
`stg_crm_deal` row carries the synthetic `R-000` row.

**Pinned `verdict` vocabulary, closed:** `ok`, `conflict`, `unchecked`. `detail.reason` is **required** on
`unchecked` and **forbidden** otherwise.

Pinned `detail.reason` vocabulary for `verdict='unchecked'`, each with the condition that is the *only*
one producing it:

| reason | produced by |
|---|---|
| `no_rule_in_scope` | the synthetic `R-000` row (this section) |
| `missing_operand` | one side of a comparison is `None` because the **source value was NULL** (§5.1) |
| `unmapped_enum` | one side of an **enum-mapped** comparison row (`grade`, `stage`, `lifecycle`) is `None` because `norm_enum` **could not map a present value** (§5.1, §7) |
| `unparseable_value` | one side of a **non-enum** comparison row (`name_first`, `name_last`, `dob`) is `None` because `norm_name` / `norm_dob` **could not parse a present value** (§5.1, ruling 5) |
| `enrollment_unattributed` | `R-013` only, and `R-012` only when its `metadata.program` fallback is also null/unmappable (§4.4) |
| `deal_unresolved` | `R-009`: `crm_deal_id` names a live deal whose `D2` person set is empty (§5.5 C9) |
| `source_incomplete` | §5.3: an absence rule skipped because its source's gen-3 load is incomplete |

None of these is ever a crash and none is ever a conflict.

### 5.9 What is deliberately *not* a conflict class
A `pipeline`/`program` mismatch is **not** a conflict class in this contract. `deal.pipeline` is generated
consistent with the household's anchor enrollment `program` by construction (`G29`) and appears in the
unified entity view only; no rule compares it, and it is not auto-apply eligible (§6). This is the
brief's Core #2 example "every deal maps to the correct pipeline", deliberately scoped out and recorded
in §12 **D-4** — not an oversight.

---

## 6. Sensitive fields (normative — auto-apply forbidden, R15/R24)

Classification is a **pure function of the target field path**, evaluated *before* confidence. The list
**is** the whole classifier, and it uses the same source-qualified path vocabulary as `COMPARED_FIELDS`
(§2.4).

```
SENSITIVE_FIELDS = {
  # legal / identity
  "crm.contact.first_name", "crm.contact.last_name", "crm.contact.dob",
  "appdb.student.first_name", "appdb.student.last_name", "appdb.student.dob",
  "appdb.student.student_number",
  # billing ownership  ("the payer or billing-owner of any payment or ACCOUNT")
  "payments.payment.payer_email", "payments.payment.payer_name",
  "appdb.enrollment.billing_owner_email",
  "crm.contact.email", "appdb.student.guardian_email", "appdb.student.guardian2_email",
  # financially-consequential status
  "appdb.enrollment.stage", "appdb.enrollment.deposit_paid_at",
  "appdb.student.status", "payments.payment.status", "crm.deal.stage",
  # consent / compliance
  "crm.contact.marketing_consent", "appdb.student.communication_opt_out",
}
```

- Any field §1 describes as guardian, billing or payer identity is sensitive by construction.
  **Consequence, intended:** all 250 C4 proposals are `sensitive_hold`, not `pending`. C4 remains a
  distinct conflict type and does **not** count toward the C14 minimum — C14 is defined solely by the
  `COMPARED_FIELDS` predicate in §5.5.
- `crm.deal.stage` is sensitive for the same reason `appdb.enrollment.stage` is: the brief names *deal*
  status transitions explicitly, and §2.3's own map is `Deposit Received→deposit_paid`,
  `Closed Won→enrolled`, `Refunded→refunded` — the brief's three examples verbatim.

**Eligible for auto-apply** (stretch #7) when confidence ≥ 0.95 **and** the case type is approved **and**
evidence is complete. The list is exactly the fields some committed fix template actually writes:

```
AUTO_APPLY_ELIGIBLE = {
  "appdb.enrollment.crm_deal_id",     # non-sensitive linkage
  "payments.payment.external_ref",    # non-sensitive linkage
  "crm.contact.external_id",          # non-sensitive linkage
  "crm.contact.grade",                # non-identity formatting
  "crm.contact.lifecycle_stage",      # non-identity formatting
}
```

**Committed fix target per conflict type.** This is what makes the classifier *decidable*: the classifier
is a pure function of the target field path, so the target path of every fix template must itself be
pinned, or "all 250 C4 proposals are `sensitive_hold`" is a hope rather than a consequence.

| type | fix template writes | classification |
|---|---|---|
| C2 | `payments.payment.external_ref` | eligible |
| C9 | `appdb.enrollment.crm_deal_id` | eligible |
| C6, grade-only | `crm.contact.grade` | eligible |
| C6, lifecycle-only | `crm.contact.lifecycle_stage` | eligible (CRM side only) |
| C6 mixed, and **every** C14 | the disagreeing **sensitive** path itself | `sensitive_hold` |
| **C4** | **`crm.contact.email`** — the disagreeing field | `sensitive_hold` |
| C1, C3, C5, C7, C8, C11, C12, C13 | no field write — evidence-only proposal | `escalated` for human review |
| C10 | no field write — human merge review | `escalated` |

**PINNED — how the fix target is chosen when a disagreeing set MIXES sensitive and non-sensitive paths,
and which SIDE the template writes (ruling 8, resolving MINOR-8).** The table above says "the disagreeing
**sensitive** path itself", which names a *set*; the classifier is a pure function of **one** path, so
the selection must be pinned or the two sides pick differently. `fix_target(type, paths)` is the single
committed selector and it resolves in this order:

1. **Partition the disagreeing paths by comparison ROW, not by path.** "Mixed" in §5.6 means a
   `name_*`/`dob` **row** together with a `grade` or `lifecycle` row. A row is *wholly sensitive* when
   **both** its endpoints are in `SENSITIVE_FIELDS` (§2.4's partition table): `name_first`, `name_last`,
   `dob` and `stage` are wholly sensitive; `grade` and `lifecycle` are not.
2. **If any wholly-sensitive row is disagreeing, the target is one of ITS paths** and the proposal is
   `sensitive_hold`. The sensitive half of a mixed set decides the classification — a mixed C6 is never
   auto-appliable on the strength of its `grade` half.
3. **Otherwise the target is the eligible (`AUTO_APPLY_ELIGIBLE`) path** of the disagreeing set —
   `crm.contact.grade` or `crm.contact.lifecycle_stage`.
4. **Ties within a step are broken by taking the CRM-side path**, then by code point.

**PINNED — C6 and C14 fix templates write the CRM side.** Whenever the chosen row has one CRM endpoint
and one app-DB endpoint, the template writes the **`crm.*`** path. This is the convention §6 already
establishes everywhere else and it is not decoration:

- the eligible rows are already CRM-only — `crm.contact.grade`, and `crm.contact.lifecycle_stage` which
  §6 pins as "eligible **only** when the proposal writes the CRM side and leaves `appdb.student.status`
  untouched";
- C4's committed target is `crm.contact.email`, not the guardian email on the app-DB side;
- C2 and C9 write the *pointer* on the side that is stale, and the app DB is the system of record for
  identity fields under §4.6 survivorship (`app DB > CRM > payments`). A reconciler that writes the
  app-DB endpoint is proposing to overwrite the authoritative record with the less authoritative one.

**This corrects the implementation, which selected `sorted(sensitive_paths)[0]`.** Byte order puts
`appdb.*` before `crm.*` on every wholly-sensitive row (`appdb.student.dob` < `crm.contact.dob`,
`appdb.enrollment.stage` < `crm.deal.stage`), so that selector always chose the app-DB side and
contradicted §6's stated convention. Every affected proposal is still `sensitive_hold`, so nothing was
mis-*classified* — but the proposed **target path** named the wrong record, and `AUTO_APPLY_ELIGIBLE`,
the C4 prohibition below and D-7 are all written in terms of the target path. The committed default row
for C14 is `crm.contact.first_name` for the same reason.

**A C4 proposal may never be re-targeted at `crm.contact.external_id` to escape the classifier.** Writing
the linkage field instead of the disagreeing field would silently reclassify all 250 C4 proposals as
auto-appliable, which §6 and §12 D-7 exist to forbid. `crm.contact.external_id` stays on the eligible list
because the C2/C9 linkage templates and the ER-repair path write it; nothing about C4 may.

- **Name/DOB formatting is not on this list** — any write to a name or DOB field is sensitive regardless
  of intent, because sensitivity is by field, not by motive.
- `crm.contact.lifecycle_stage` is eligible **only** when the proposal writes the CRM side and leaves
  `appdb.student.status` untouched.
- `crm.deal.pipeline` is **not** eligible (no committed rule proposes it — §12 D-4) and
  `crm.contact.state` is not eligible (no longer compared by anything).
- A field path in neither set is not auto-applyable: eligibility is an allowlist, not the complement of
  `SENSITIVE_FIELDS`.

---

## 7. Generations, oscillation, malformed payloads

- **3 generations per source. Each `fixtures/{source}/gen{N}/*.jsonl` is a COMPLETE SNAPSHOT of that
  source at generation N** — records unchanged since gen N−1 are re-emitted verbatim. Gen 1 = baseline;
  gen 2 changes and adds records; **gen 3 = current state**, with **≥25 fields re-asserting their gen-1
  value that the app DB disagrees with** (A→B→A).
- **Current state = the rows with `generation = 3`**, globally. Invariants read generation 3 only;
  `field_lineage` retains gens 1–3.
- **Absence of a `natural_key` from a source's gen-3 snapshot IS a deletion**, and is how C8's dropped
  child and C9's non-existent deal are represented.
- **Golden set describes generation 3.** Entries carry `"oscillating": true` where the conflict's field
  oscillated.
- **Oscillation detection**: window scan of `field_lineage` per `(person_key, field)` for the pattern
  `A, B, A` across ascending generations, over **one row per generation** — the one with the
  lexicographically smallest `source_ref`, §4.6's survivorship tiebreak reused so a person carrying
  several records of one source (its payments, say) cannot make the scan depend on row order.
  `field_lineage`, the A,B,A scan and R16's fingerprint dedup are **all keyed on `person_key`** (§4.1),
  which is stable across generations. On oscillation the conflict is marked `escalated:oscillation` and
  the reconciler **must not** re-propose the identical fix (R16).
- **Malformed payloads** live in `fixtures/malformed/cases.jsonl`, one JSON object per line:
  `{case_id, source, entity_type, expect_code, raw}` where `raw` is the **literal payload string** (so
  truncated JSON is representable). **≥20 cases** covering: missing required field, wrong scalar type,
  truncated JSON, null PK, duplicate PK (**CRM contact only**), a non-object JSONL line, and one
  oversized body (`MAX_PAYLOAD_BYTES + 1` bytes). Each must produce the documented `4xx` + structured
  log, never a 500 and never a silent skip. Malformed cases are **excluded** from every count in §5.
  - **Malformedness is structural only.** A well-formed record carrying an unrecognised enum *value* is
    **never** malformed: it ingests normally, `norm_enum` returns `None`, and every rule scoping it yields
    `verdict='unchecked'` with `detail.reason='unmapped_enum'` (§5.8).
  - `duplicate PK` is exercised on a CRM contact, never on a payment, so it cannot collide with `R-011`.
    In-band duplicate `payment_id`s are never emitted into the ordinary payments fixtures; a repeated PK
    reaching the adapter in any generation is a structural rejection (4xx), not a conflict.

---

## 8. Emitted artifacts

```
fixtures/manifest.json                 profile, seed, per-file sha256, all counts,
                                       expected gen-N record count per (source, entity_type)
fixtures/{crm,appdb,payments}/gen{1,2,3}/*.jsonl      complete snapshots (§7)
fixtures/malformed/cases.jsonl
golden/conflicts.json                  [{type, rule_id, entity_refs[], sources_involved[],
                                         disagreeing_fields[], observed_values{}, expected_verdict,
                                         compound_with[], oscillating}]
golden/clean-sample.json               1,000 entities asserted conflict-free (identity refs)
golden/expected-views.json             >=25 hand-checkable unified entity views (the join contract, R10)
golden/manifest-summary.json           counts per conflict type + every A.1/A.4/A.5 minimum
```

**`golden/conflicts.json`**
- `expected_verdict ∈ {"conflict"}` for **every** entry in this file. The reconciler-side statuses
  (`pending`, `sensitive_hold`, `escalated:oscillation`) are a different namespace and never appear here.
- `sources_involved ⊆ {"crm", "appdb", "payments"}`, derived mechanically from the `entity_refs` prefixes
  by `recon/reference.py:conflict_refs`.
- `disagreeing_fields[]` is populated only by `R-006`/`R-014` and only from `COMPARED_FIELDS` paths
  (§2.4).
- `compound_with[]` is **golden-side metadata only**, and it is defined here because A.5's graded ratio is
  computed over it. For each entry it is the **sorted** list of `"{type}|{','.join(sorted(entity_refs))}"`
  keys of the **other surviving** golden entries whose `entity_refs` intersect this entry's. It is
  symmetric, and it is populated **after** the `PRECEDENCE` filter runs, so pairs suppressed by rules 2
  and 4–8 never appear in it (`G32`). A.5's ≥10% compound ratio is
  `count(surviving entries with non-empty compound_with) / count(surviving entries)`, asserted by `G33`.
  **The detector does not emit `compound_with` and the harness does not compare it** — it is not one of
  §5.4's field-exactness assertions.

**`golden/clean-sample.json`**
- The 1,000 entities are a **random draw** (A.6's word) made with the run's own seeded PRNG over
  the sorted conflict-free population — never a stride walk, which samples construction order.
- A clean-sample entity is **FLAGGED iff any detected conflict's `entity_refs` INTERSECTS that entity's
  identity refs.** Every such intersection is reported as one false positive. This is the strict reading:
  nothing hides behind a ref-set-equality technicality.
- Correspondingly the generator guarantees that **no sampled identity ref appears in the `entity_refs`
  of any entry in `golden/conflicts.json`** — including C8's dropped-child refs and C10's two student
  refs (§10 `G28`).

**`golden/manifest-summary.json`** additionally carries `tri_source_student_fraction` (asserted
∈ [0.68, 0.72]), `fully_consistent_entity_fraction` (asserted ≥ 0.85), and the five A.1 per-entity record
counts asserted **individually**: 40,000 / 15,000 / 25,000 / 22,000 / 18,000.

**Determinism** (asserted by `G30` / `sc_determinism`). Every JSON file is written with `json.dumps(obj, sort_keys=True, ensure_ascii=True,
separators=(",", ":"))` and a trailing newline. JSONL lines are individually sorted-key encoded and files
are emitted in a fixed, sorted record order. **Two runs at the same seed must be byte-identical**
(`sha256` of the whole tree).
- The seed entrypoint **sets and asserts `PYTHONHASHSEED=0`**. "Sets" is literal: the variable is
  read at interpreter start-up, so `recon/seed/__main__.py` re-`exec`s **once** (guarded by a
  sentinel) with it in the environment and then asserts the value it ended up with. A caller who
  passes `PYTHONHASHSEED=random` therefore cannot reach the generator at that hash seed.
- No `set` or `dict`-keys iteration may reach an output or a selection decision without an explicit
  `sorted()`.
- `golden/conflicts.json` elements are sorted by `(type, tuple(sorted(entity_refs)))`;
  `golden/clean-sample.json` by identity-ref tuple.
- The determinism check runs the seed twice in two subprocesses and diffs the tree.
- **Seed entropy reaches the ADDRESSING, not only the values.** Which records carry which conflict
  is drawn through the run's own `random.Random(seed)`, so `--seed <n>` moves every primary key in
  every golden entry, `golden/clean-sample.json` included. `fixtures/malformed/cases.jsonl` is the
  **only** emitted file that is identical across seeds; a second seed-invariant file is a defect
  (A.5 forbids conflicts that are "uniformly distributed ... or resolvable by one clever join", and
  a fixed index partition is exactly that at the addressing level). The committed determinism test
  asserts every other file differs.
- **The committed `golden/` tree is regenerated and diffed** against a fresh
  `--profile full --seed <DEFAULT_SEED>` run, per file by `sha256`, in the test suite CI runs. A
  stale committed golden set is a build failure, not a silent grading hazard.

---

## 9. Profiles and the manifest self-check

`--profile dev` (~6,000 records) and `--profile full` (the graded **120,000-record** Appendix-A dataset —
A.1's five volumes; the brief's floor is ≥100,000) share one code path.

- The dev profile scales **conflict volumes only**: A.4 conflict-class counts and A.1 volumes scale by the
  same ratio, with a floor of 5 per class so all 14 classes stay exercised.
- **Structural minimums that are not conflict classes are NOT scaled** and are identical in both
  profiles: malformed cases (≥20, emitted as 24) and re-asserting/oscillating fields (≥25).
  `fixtures/malformed/cases.jsonl` and the oscillation-set size are identical across profiles.
- **Multi-child households and deal-less orphans DO scale** — flagged in §12 **D-13**. A.4's floors
  (≥1,000 households of 2–4 children, ≥3,000 leads) exceed the entire dev student budget of 1,250,
  so holding them at their `full` values is not constructible. Both are asserted against A.4's
  floors on `full` and asserted merely non-degenerate on `dev`.
- The oscillation set spans **at least two distinct `field_path`s**, so R4/R16's A→B→A scan is never
  exercised on one path alone.
- All of this is asserted by `sc_structural_minimums` and `sc_oscillation_spread`, so the clause is
  enforced rather than narrated.
- **All gates, benchmarks, and the committed `golden/` files are `full`.**

### 9.1 The manifest self-check
Runs **after** pass 2 (§10 `G31`) and **before** `golden/` is written. It executes the detector's own
`recon/er.py` over the emitted fixtures. **Any failure fails the seed run loudly and no `golden/` tree is
written.** The named checks are listed in §10, one per construction constraint; in addition it records:

**(a) Volumes, generations and links.**

- per-`(source, entity_type, generation)` record counts. **Generation-3** counts are asserted equal to the
  A.1 volumes **exactly** — 40,000 / 15,000 / 25,000 / 22,000 / 18,000 — because §11.4, §11.6 and §11.7
  already net the deliberate deletions *to* those figures. Generations 1 and 2 additionally carry the
  records deleted before gen 3, so **gen-1 counts are 40,075 CRM contacts** (+75 C8 `crm` drops),
  **15,050 deals** (+50 C9 branch-1 targets) and **18,075 payments** (+75 C8 `payments` drops); students
  and enrollments are 25,000 / 22,000 in every generation;
- the **contact** link-path distribution over generation-3 contacts:
  `count(entity_links.method == 'L3') == 250` — exactly the planted C4 set, the only student-linked
  contacts `L1`/`L2` cannot reach (`G19`); every other student-linked contact carries
  `method ∈ {L1, L2}` (`G3`); `count(contacts with no student link) == 18,175`, and that set is
  **exactly** the deal-less leads of §11.4 (`G11`). No contact is unlinked for any other reason;
- `count(students with no linked contact) == 3,475` — exactly the 3,400 `{appdb}`-only students of §11.3
  **plus** the 75 C8 children dropped from `crm`, and no student outside those two populations;
- `count(persons with ≥1 paid payment ∧ 0 enrollments) == 0` (`G10`);
- `count(clean multi-child households with a non-uniform presence mask) == 0` (`G2`);
- `count(clean students with STATUS_TO_FUNNEL(status) == enrolled ∧ no linked contact ∧ no linked payment) == 0`;
- `tri_source_student_fraction`, `fully_consistent_entity_fraction`, and the five A.1 volumes.

**(b) The construction sweep** — `sc_construction_sweep`, cited by `G31`(c).

Every assertion below is evaluated over **every** generation-3 entity — planted, unsampled and clean
alike, *never* merely over the 1,000 entities in `golden/clean-sample.json`. It is computed with the
shared `recon/normalize.py`, `recon/reference.py` and `recon/er.py` modules: a comparison over committed
reference data, **not** a run of `rules/*.sql`. The generator still never executes an invariant rule, so
`G31`'s non-circularity note stands. A **surplus of one** in any raw column is a construction bug and
fails the seed run — this, and not the clean sample, is what makes the zero-false-positive floor
structural.

| class | raw sweep population (all gen-3 entities) | asserted raw count | survives `PRECEDENCE` |
|---|---|---|---|
| C1 | persons with ≥1 `paid` payment ∧ ≥1 enrollment ∧ 0 `D2`-linked deals | **575** = 500 planted C1 + 75 C8 `crm`-drops, the two sets disjoint | 500 (rule 8 removes 75) |
| C2 | payments unattributable by `P1..P3` | **200** = planted C2 | 200 |
| C3 | unordered gen-3 CRM contact pairs meeting C3's full predicate | **300** pairs = planted C3 | 300 |
| C4 | `entity_links` `contact_student` rows with `method == 'L3'` ∧ `norm_email(contact.email) ∉` the student's normalized guardian emails | **250** = planted C4 | 250 |
| C5 | students with `STATUS_TO_FUNNEL(status) == enrolled` ∧ no linked contact ∧ no `P1..P3` payment | **400** = planted C5 | 400 |
| C6 / C14 | the `COMPARED_FIELDS` sweep (§2.4) over **every** linked person, partitioned by §5.5's subset test | **500** C6 persons + **100** C14 persons (50 planted + the 50 mechanically induced on the C10 persons) | 500 C6 + 50 C14 (rule 2 removes the 50 C10-induced) |
| C7 | enrollments with `stage_funnel ∈ PAID_IMPLYING_STAGES` ∧ no `E1`/`E2`-attributed `paid` `deposit`/`tuition` | **875** = 300 C7 + 100 C13 + 400 C5 + 75 C8 `payments`-drops (`G38`) | 300 (rules 4, 5, 8) |
| C8 | households with `\|household_members_appdb(k)\| ≥ 2` meeting C8's predicate under §5.5's two presence functions | **150** = planted C8 | 150 |
| C9 | enrollments meeting C9's two-branch predicate | **100** = planted C9 (50 per branch) | 100 |
| C10 | contacts whose `ext` and `namedob` candidates resolve to two different non-null students | **50** = planted C10 | 50 |
| C11 | unordered payment pairs meeting C11's predicate | **50** pairs = planted C11 | 50 |
| C12 | payments whose `amount_cents` ≠ the fee-schedule value for the resolved `(program, type)` | **100** = planted C12 | 100 |
| C13 | payments meeting C13's four-clause predicate | **100** = planted C13 | 100 |

Two suppressions are additionally asserted to fire **zero** times, because a non-zero count would mean a
construction bug rather than a legitimate overlap: `PRECEDENCE` 6 (C9 over C1 — vacuous under `G9` and
§4.5) and `PRECEDENCE` 7 (C10 over C5 — vacuous under `G21`).

### 9.2 Suite checks (detector side, `recon.suite`)
The self-check above runs inside the seed. These run inside the **graded harness**, over the *ingested*
generations 1–3, and re-derive their inputs from the database rather than from the generator's state:

- `COUNT(DISTINCT person_key)` is equal across generations for every person present in more than one, and
  every person's `anchor_ref` keeps the same **source class** in every generation it appears in — the
  detector-side twin of `G25` (§4.1). Without this, a person re-anchoring from `crm:contact:` to
  `appdb:student:` between gen 1 and gen 2 splits one lineage in two and silently disables R16's
  oscillation dedup;
- every `field_lineage` row belonging to one person carries that person's single `person_key`, so the
  A→B→A scan of §7 cannot be split by a re-anchoring;
- the two grading categories of §5.4 over the **full** detected set: an unmatched detection is a false
  positive and an unmatched golden entry a false negative, whether or not any ref intersects
  `golden/clean-sample.json`.

---

## 10. Generator construction constraints

**This is the section that replaces hope with construction.** Every FP guard in §5.5 cites one or more
`G`-ids here. Each `G` is enforced by the named manifest self-check assertion, which runs before
`golden/` is written (§9.1); a violated `G` **fails the seed run** and no golden set is produced.

**"Non-planted" is defined per constraint, never globally.** A record, person or household is
*non-planted with respect to `Gn`* iff it carries none of the plants that `Gn`'s own row **explicitly
names** as relaxing `Gn`. **No plant relaxes a `G` it does not name**, and carrying *some* plant does not
exempt a record from *every* `G`. Where a row below says "**every** … planted or not", there is no
relaxation at all. This is the definition the word carries everywhere else in the document (§§1.1, 1.5,
5.5).

| id | constraint | asserted by |
|---|---|---|
| `G1` | Every child in a household is given the **same `guardian_email` value** (dirty variants allowed — they normalize equal). `contact.email` is always drawn from the student's **primary** `guardian_email`, and `payer_email` likewise, except on planted C4 / C2. | `sc_household_key_exact` |
| `G2` | Presence masks are drawn at the **household** level. All children of a multi-child household share one identical `{crm, payments}` presence mask. The 150 planted C8 households are the only exception. | `sc_household_mask_uniform` |
| `G3` | **Every** contact↔student pair the dataset intends links — planted or not. For every CRM contact that represents a student, at least one of `L1`/`L2`/`L3` fires **by construction**: `external_id` presence, `dob` presence and email variance are drawn **jointly** subject to that assertion, never independently. The 250 C4 plants are included (`G19` forces them onto `L3`) and so is the C10 collapsed contact (`G21` forces it onto `L1`). The **only** contacts with no student link are the 18,175 deal-less leads (`G11`); the only students with no linked contact are the 3,400 `{appdb}`-only students and the 75 C8 `crm`-drops. `guardian2_email` may be nulled only when no contact and no payment uses that address. | `sc_link_path_distribution` |
| `G4` | Dot and `+alias` local-part variation is emitted **only** on `gmail.com` / `googlemail.com`. On every other domain, all addresses belonging to one person are byte-identical after `norm_email` — **except** the 250 planted C4, whose variant address is deliberately on a non-gmail domain so it cannot normalize back (§5.6 C4). No other plant relaxes this. | `sc_email_variant_domain` |
| `G5` | (a) Within any shared-guardian-email group, `(first_norm, last_norm)` is unique across children. (b) Globally, the `namedob` key `(first_norm, last_norm, dob_norm)` **resolves to at most one *person*** — at most one `appdb.student`, and at most one `crm.contact` **per person** — except the exact tuples the C3 planter (2 contacts, 1 student) and the C10 planter (2 students, allowlisted) registered. **A contact and the student it represents sharing the tuple is required, not a violation:** `G1` draws the contact's name from the student and `contact.dob` is present on ~70% and equal, so ≈15,000 record pairs share a triple by construction — and `L3` (§4.2) links on exactly that triple, so a record-level uniqueness reading would make `L3` unfireable and the 250 C4 plants unplantable. Records whose `dob_norm` is `None` are compared on `(first_norm, last_norm)` alone for this test and must likewise resolve to at most one person. Name corpus pinned at `NAME_CORPUS_MIN` (2,000 × 1,000 = 2×10⁶ pairs against 43,175 name-bearing records, load factor 2.2%) so rejection sampling terminates. | `sc_namekey_unique` |
| `G6` | Every non-planted payment satisfies one of `P1..P3`. A payment omits **both** `external_ref` and the `metadata` name pair only when it is a planted C2. The self-check runs `P1..P3` over the emitted fixtures and asserts `count(unattributable) == planted C2 count`. | `sc_payment_attributable` |
| `G7` | Planted C11 pairs are `≤ C11_PLANT_MAX_SECONDS` (300s) apart. **Every** legitimate same-person, same-`type`, same-`amount_cents` repeat payment is `≥ LEGIT_REPEAT_MIN_SECONDS` (1200s) apart. Sibling simultaneous `fee` payments are permitted and resolve to different persons. **The guard's population is required to be non-empty**: a budgeted set of two-child tri-source households pays two identical `deposit`s inside C11's 600s window — costing no extra payment record, since the two base payments already exist — so a detector that drops C11's "both resolve to the same person" clause produces a loud false-positive population rather than one stray pair. | `sc_repeat_guard_band`, `sc_c11_guard_population` |
| `G8` | No 3-or-more-way collision is ever created: exactly two contacts per C3 email collision and exactly two payments per C11 key collision, so the pair count is never ambiguous. | `sc_no_nway_collision` |
| `G9` | Deals are allocated **from the C1 invariant, not from a volume target**: *every* person with (≥1 `paid` payment ∧ ≥1 enrollment) has ≥1 `D2`-linked deal **except** (a) the planted C1 set (500) and (b) the 75 C8 children dropped from `crm`, who have no gen-3 CRM contact and therefore cannot appear in any `associated_contact_ids` — their mechanically-implied C1 is suppressed by `PRECEDENCE` 8 (§12 D-6). The two sets are disjoint, so the deal-less-with-payment population is exactly **575**. C1 plants sit in single-child households so no household deal can reach them. Every C9 plant's household retains a live gen-3 `D2` deal, so a stale pointer never manufactures a C1. | `sc_deal_coverage` |
| `G10` | No person has a `paid` payment and zero enrollments. | `sc_paid_implies_enrollment` |
| `G11` | The **18,175** deal-less leads (§11.4; A.4's floor is ≥3,000) have **no** payment, **no** enrollment, **no** student link, appear in no `associated_contact_ids`, and carry a globally unique `email_norm`. They are the **only** contacts in the dataset with no student link. | `sc_lead_purity` |
| `G12` | A student has **at most one** enrollment; 22,000 students have exactly one, 3,000 have none. | `sc_enrollment_cardinality` |
| `G13` | For **every** payment attributed to an enrollment — planted or not, C12 included — `metadata.program` (when present) equals that enrollment's `program`. No plant relaxes this: C12 is planted by moving `amount_cents`, never `metadata.program` (§5.6 C12). Any person carrying a C7/C12/C13 plant has exactly one enrollment. | `sc_program_consistency` |
| `G14` | Every `refunded` payment **except the 100 planted C13** is **either** superseded by a later `paid` payment of the same `type` (≥1200s later, shape (i), 75 records) **or** has its enrollment moved to `refunded`/`withdrawn` **and** its student status out of `{enrolled, active}` (shape (ii), 250 records). Both shapes are present. Every refunded payment — clean or C13 — sits in a **single-child** household, so funnel-uniformity (`G18`) never propagates a refunded or paid-implying funnel to a sibling that cannot back it. | `sc_refund_closure` |
| `G15` | Every C13 plant leaves **both** downstream fields stale (enrollment `stage_funnel` paid-implying **and** `student.status ∈ {enrolled, active}`) with `refunded_at > enrollment.updated_at`. Partially-reflected refunds are never planted. | `sc_c13_plant_staleness` |
| `G16` | Legitimately partial-presence students carry `status ∈ {prospect, applied, withdrawn}` **only**. `enrolled`/`active` with no contact and no payment occurs on planted C5 and nowhere else. | `sc_partial_presence_status` |
| `G17` | Every planted C6/C14 has **both** sides non-null and both normalizing to **distinct non-`None`** canonicals. A planted conflict is never created by nulling a field. | `sc_compare_operands_present` |
| `G18` | **EVERY household — planted or not — is funnel-uniform**: all its enrollments share one `stage_funnel`, its `D2` deal's `DEAL_STAGE_TO_FUNNEL(stage)` equals that funnel, and every member's `lifecycle_stage` is drawn from the `LIFECYCLE_TO_FUNNEL` pre-image of that student's `STATUS_TO_FUNNEL(status)`. A `withdrawn` student carries a `lifecycle_stage` from the `None`-mapping subset `{subscriber, evangelist, other}`, so the comparison is `unchecked`, not a disagreement. **The relaxations are exactly these four, each named by a §5.6 row, and no other plant relaxes any clause:** (i) the **120 lifecycle-only and the lifecycle-bearing mixed C6 plants** move `crm.contact.lifecycle_stage` out of the pre-image — that *is* the plant; (ii) the **10 stage-only C14 plants** move `crm.deal.stage` off the enrollment funnel — that *is* the plant, and they sit in single-child households so no sibling inherits it; (iii) **C13** leaves `enrollment.stage_funnel` paid-implying with `student.status ∈ {enrolled, active}`, which are mutually consistent, so no `stage` or `lifecycle` disagreement is induced; (iv) **C9** leaves `crm_deal_id` stale, which is not a link rule (§4.5), so the household's surviving `D2` deal is unaffected. Outside these four, **no plant may leave `crm.deal.stage` disagreeing with `appdb.enrollment.stage`, or a `lifecycle_stage` outside the pre-image** — this is the sole guard against an unbudgeted C14/C6 storm on planted households, which `G31`(c) would otherwise be the first to discover. | `sc_household_funnel_uniform` |
| `G19` | C4 preconditions of §5.6 hold on every C4 plant. | `sc_c4_preconditions` |
| `G20` | C9 preconditions of §5.6 hold: every different-person stale-pointer target has non-empty `associated_contact_ids` resolving by `D2` to exactly one other person whose contact is `L1`- or `L2`-linked. | `sc_c9_preconditions` |
| `G21` | C10 preconditions of §5.6 hold; both key classes resolve, to two distinct students, and student B retains its own linked contact — so `PRECEDENCE` 7 fires **zero** times and no student is contact-less. The collapsed contact `L1`-links to student A and carries `grade`/`lifecycle_stage` **identical to student A's**, so the only disagreeing paths it induces are `name_first`/`name_last`/`dob`. **Assertion:** after the `PRECEDENCE` filter, **no C6 and no C14 entry survives for any person whose identity refs contain a collapsed contact ref** (rule 2); a survivor is 50 unbudgeted false positives and fails the seed run. Neither student A nor student B carries any other plant. | `sc_c10_preconditions` |
| `G22` | C8 preconditions of §5.6 hold; the dropped child is mask-eligible (`GRADE_ORDER[grade_norm] ≥ 0`, `status != 'withdrawn'`, enrollment `stage_funnel ∉ {withdrawn, refunded}`) and all its siblings are present in the source it is dropped from. | `sc_c8_preconditions` |
| `G23` | No two same-generation CRM contacts share `email_norm` unless they are (a) a golden C3 pair meeting the full predicate or (b) a declared sibling set whose members differ in **`(first_norm, last_norm)`** per `G5`(a) — which is what C3's predicate keys on, and which admits the blended household where two siblings share a first name but differ in surname. Any other same-email pair fails the seed run — a duplicate shape the detector cannot see can never be planted. **Additionally, on every planted C3 pair:** both contacts carry `external_id == student.id` for the same student so both link by `L1` (the pair contributes one person, not two — §11.9), and both carry **identical `COMPARED_FIELDS` values** (`grade`, `lifecycle_stage`) which both agree with that student, so §4.6's lowest-`crm_id` survivorship tiebreak cannot manufacture a C6/C14 disagreement on a C3 person. | `sc_insource_email_unique` |
| `G24` | Each `gen{N}` file is a **complete snapshot**. `entity_refs` may name **only** records present in the gen-3 snapshot of their source; every ref in `golden/conflicts.json` is resolved against the emitted gen-3 fixture index. | `sc_refs_resolve_gen3` |
| `G25` | `person_key` is stable across generations 1–3 for every person present in **more than one**, and **no person's `anchor_ref` changes source class between generations**: a person anchored on `appdb:student:` in any generation retains a student ref in every later generation, and no person anchored on `crm:contact:` or `payments:payment:` in gen 1 or 2 acquires a higher-preference identity ref later. This is what makes §4.1's pure-function definition stable; the detector-side twin runs in §9.2. | `sc_person_key_stable` |
| `G26` | `updated_at < created_at` on ~0.5% of **each** entity type's records, **excluding** any enrollment whose student holds a `refunded` payment. No rule reads `created_at`/`updated_at` as *evidence of* a conflict; the **single** permitted read is C13 clause (c)'s `refunded_at > enrollment.updated_at` recency test (§5.5), which the exclusion above keeps dirt-free. C11 uses `occurred_at` only. | `sc_timestamp_dirt_spread` |
| `G27` | Malformed cases are **structural only** and live exclusively in `fixtures/malformed/cases.jsonl`; `duplicate PK` is exercised on a CRM contact only; the oversized case is exactly `MAX_PAYLOAD_BYTES + 1` bytes; no malformed record is counted in any §5 total. | `sc_malformed_isolation` |
| `G28` | No identity ref sampled into `golden/clean-sample.json` appears in the `entity_refs` of **any** entry in `golden/conflicts.json`. | `sc_clean_sample_disjoint` |
| `G29` | `deal.pipeline` equals the `program` of the household's **anchor enrollment** — the enrollment of `household_anchor_student(k)` (§4.8), which is deterministic for a 2–4 child household where "primary" is not — on **every** deal, planted or not, and is always a value in the committed vocabulary. No plant relaxes it; no rule compares it (§5.9). | `sc_pipeline_consistency` |
| `G30` | `PYTHONHASHSEED=0` is set and asserted; no unsorted `set`/`dict` iteration reaches an output or a selection decision; two subprocess runs at the same seed produce a byte-identical tree. **`sc_determinism` evaluates its clauses at run time and may never be a literal:** (a) it asserts `PYTHONHASHSEED == '0'`; (b) it re-reads every file the run just emitted and asserts its `sha256` equals the value `fixtures/manifest.json` is about to claim; (c) it replays the whole of pass 2 — `recon.er.resolve`, the sweep and `build_golden` — over a **shuffled** gen-3 snapshot and requires the three golden documents byte-identical, which is what turns "no unsorted iteration reaches a selection decision" into an assertion. Byte-identity across two *subprocesses* is the one clause delegated to `tests/seed/test_determinism.py`, because a process cannot observe another process's hash seed. | `sc_determinism` |
| `G31` | **Two-pass seeding.** Pass 1 constructs and materializes all source records (clean + planted). Pass 2 runs the **actual `recon/er.py` cascade** over those records and derives **every** `entity_refs` value in `golden/` from `conflict_refs` applied to that output. **Plantability assertion:** (a) for every planted conflict, every link the conflict's rule presumes exists in `entity_links` and was made by the **expected `method`** (§4.7); (b) the derived refs are byte-identical to the refs being written; (c) the **§9.1(b) construction sweep** — one assertion per conflict class, evaluated over **every** generation-3 entity, planted, unsampled and clean alike, and explicitly **not** merely over `golden/clean-sample.json` — finds exactly the planted population for that class, before and after the `PRECEDENCE` filter. The sweep uses the shared `normalize`/`reference`/`er` modules only; the generator still **never** executes `rules/*.sql`. **An unplantable conflict, or a surplus of one in any sweep column, FAILS THE SEED RUN; it is never written into `golden/`.** | `sc_plantability`, `sc_construction_sweep` |
| `G32` | `golden/conflicts.json` is written through the same `PRECEDENCE` filter the detector applies, and `(type, tuple(sorted(entity_refs)))` is unique across the file. | `sc_precedence_filtered`, `sc_golden_key_unique` |
| `G33` | The §11 allocation holds exactly: the five A.1 volumes, `tri_source_student_fraction ∈ [0.68, 0.72]`, `fully_consistent_entity_fraction ≥ 0.85`, all fourteen A.4 minimums, and A.5's ≥10% compound ratio over **surviving** entries. | `sc_volumes_and_ratios` |
| `G34` | **Every** payment's `amount_cents` is **exactly** the fee-schedule value for its `(program, type)`, **except** the 100 planted C12 — the only relaxation. Planted C12 amounts cannot coincide with any other `(program, type)` cell. | `sc_fee_schedule_exact` |
| `G35` | C7 preconditions of §5.6 hold: each plant holds a `paid` `fee`, no `paid` `deposit`/`tuition`, and a `D2`-linked deal. | `sc_c7_preconditions` |
| `G36` | C1 preconditions of §5.6 hold: single-child household, `paid` `deposit` at schedule amount, one enrollment at `deposit_paid`, `crm_deal_id IS NULL`, zero `D2` deals. | `sc_c1_preconditions` |
| `G37` | C6/C14 plant composition of §5.6 holds, including the **80 mixed** C6 plants that combine a `name_*`/`dob` path with a `grade` or `lifecycle` path. | `sc_c6_c14_composition` |
| `G38` | **A paid-implying stage is drawn only where a payment backs it.** Every enrollment whose `stage_funnel ∈ PAID_IMPLYING_STAGES` belongs to a student holding ≥1 `paid` payment of type `deposit`/`tuition` attributed to that enrollment by `E1`/`E2` — **except** exactly four named, budgeted populations: the 300 planted C7, the 100 planted C13 (`PRECEDENCE` 5), the 400 planted C5 (`PRECEDENCE` 4) and the 75 C8 children dropped from `payments` (`PRECEDENCE` 8), totalling **875**. Constructively: paid-implying stages are drawn **only** for children of payments-present (tri-source) households, so every `{appdb, crm}` and every `{appdb}`-only enrollment carries `stage_funnel ∈ {prospect, applied, waitlisted, withdrawn, refunded}` — 4,200 of the 4,600 partial-presence enrollments by `G16` + funnel-uniformity, and the remaining 400 are the C5 plants. Without this constraint C7's FP guard is a restatement of C7's own predicate and thousands of partial-presence enrollments fire it. | `sc_paid_stage_has_payment` |
| `G39` | **No amount sits on a half-cent boundary (ruling 13).** For **every** `crm.deal.amount` — planted or not, no relaxation — `amount * 100` is never an exact `.5` in IEEE-754, so `Money(round(amount * 100))`'s half-to-even tie-break is **unobservable** in the emitted dataset. Constructively: every amount is a whole number of cents drawn from the fee schedule (§2.3) or offset from it by a whole number of cents (§5.6 C12). The tie-break stays committed so `canon_value`/`Money` are defined for every input, but no golden byte can ever depend on it. | `sc_amount_no_half_cent` |

> **`G31` is the fix for the audit's fourth root cause.** In v1 the generator planted links by
> construction while the detector had to earn them; a single link the detector failed to make turned one
> plant into 1 false negative *and* 1 false positive simultaneously. This is deliberately **not**
> circular: the generator still never runs the invariant rules. *Which* conflicts exist and of *what
> type* remains independent ground truth from the plant record; only the **addressing** (`entity_refs`)
> is shared. Detection stays independently graded.

---

## 11. Volume allocation (worked, and simultaneously satisfiable)

All figures are the `full` profile. Every number below is asserted by `G33`.

### 11.1 Records — A.1 volumes
| source | entity | volume |
|---|---|---|
| CRM | contacts | 40,000 |
| CRM | deals | 15,000 |
| App DB | students | 25,000 |
| App DB | enrollments | 22,000 |
| Payments | payments (incl. refunds) | 18,000 |
| | **total** | **120,000** (brief floor ≥100,000) |

### 11.2 Households and students
| bucket | households | children |
|---|---|---|
| multi-child, 2 children | 2,150 | 4,300 |
| multi-child, 3 children | 650 | 1,950 |
| multi-child, 4 children | 200 | 800 |
| **multi-child subtotal** | **3,000** | **7,050** |
| single-child | 17,950 | 17,950 |
| **total** | **20,950** | **25,000** |

A.4 requires ≥1,000 multi-child households with 2–4 children; 3,000 is chosen because the deal arithmetic
in §11.7 needs ≥2,400 multi-child households in the deal-bearing set (see the derivation there).

### 11.3 Source presence (drawn at household level — `G2`)
| bucket | households | students |
|---|---|---|
| tri-source, multi-child (1,830×2 + 550×3 + 170×4) | 2,550 | 5,990 |
| tri-source, single-child | 11,410 | 11,410 |
| **tri-source subtotal (pre-C8)** | **13,960** | **17,400** |
| partial `{appdb, crm}`, multi-child (320×2 + 100×3 + 30×4) | 450 | 1,060 |
| partial `{appdb, crm}`, single-child | 3,140 | 3,140 |
| partial `{appdb}` only, single-child | 3,400 | 3,400 |
| **partial subtotal** | **6,990** | **7,600** |
| **total** | **20,950** | **25,000** |

The C8 plants remove 150 children from tri-source status (75 dropped from `crm`, 75 from `payments`):

| ratio | value | A.1 target |
|---|---|---|
| tri-source students | 17,400 − 150 = **17,250** | ~70% |
| `tri_source_student_fraction` | **0.690** | asserted ∈ [0.68, 0.72] |
| students in one or two sources | 7,750 (31.0%) | "the remainder" |

**The only students carrying an `{appdb, payments}` mask are the 75 C8 children dropped from `crm`** —
their mask is a *consequence* of the C8 plant, not a household draw. No **clean** household is ever drawn
with that mask, because a clean student with a paid payment, an enrollment and no CRM contact would have
zero `D2` deals and satisfy C1 exactly (`G9`, which excepts those 75 explicitly and no one else); their
mechanically-implied C1 is suppressed by `PRECEDENCE` 8 (§12 D-6). Clean partial households are
`{appdb, crm}` or `{appdb}` only.

### 11.4 CRM contacts — 40,000
| bucket | count | derivation |
|---|---|---|
| contacts linked to tri-source students | 17,325 | 17,400 − 75 dropped from `crm` |
| contacts linked to `{appdb, crm}` students | 4,200 | §11.3 |
| C3 duplicate contacts | 300 | one extra contact per planted pair |
| **deal-less leads** (A.4 orphan noise) | **18,175** | 40,000 − 17,325 − 4,200 − 300 |
| **total (generation 3)** | **40,000** | |

Generations 1–2 additionally carry the **75** contacts of the C8 children later dropped from `crm`, so
the gen-1 contact count is **40,075**. Generation 3 is the A.1 volume exactly (§9.1a).

The lead count is **forced arithmetic**, not a choice: 40,000 contacts against at most ~21,500
student-linked contacts leaves ≥18,000 unlinked. A.4's floor is ≥3,000, so the constraint is satisfied
with wide margin, and the brief's "false-positive test at scale" is exercised harder than mandated. Leads
are safe from every rule: C1 needs a payment and an enrollment (`G11`), C3 needs a shared `email_norm`
(`G11`, `G23`), and C6/C14 need a cross-source link, which a lead does not have.

### 11.5 Enrollments — 22,000 (1:1 with students, `G12`)
| bucket | count |
|---|---|
| tri-source household children (all, including the 150 C8-dropped) | 17,400 |
| partial `{appdb, crm}` students with an enrollment | 2,800 |
| partial `{appdb}` students with an enrollment (includes the 400 C5) | 1,800 |
| **total enrollments** | **22,000** |
| students with **no** enrollment (all partial, all with no `paid` payment — `G10`) | 3,000 |

### 11.6 Payments — 18,000
Payments-present students = 17,400 tri-source household children − 75 dropped from `payments` = **17,325**.
Every payments-present child needs ≥1 attributed payment, because C8's mask is evaluated per child.

| bucket | count |
|---|---|
| base: one payment per payments-present student | 17,325 |
| planted C2 (attributable to nobody) | 200 |
| planted C11 second payment of each pair | 50 |
| superseding `paid` payment after a clean refund (`G14` shape (i), ≥1200s later) | 75 |
| second payment for fee+deposit persons (multi-payment attribution: the person ends with one `paid` `fee` **and** one `paid` `deposit`) | 350 |
| **total (generation 3)** | **18,000** |

Generations 1–2 additionally carry the **75** payments of the C8 children later dropped from `payments`,
so the gen-1 payment count is **18,075**.

**By record type — the allocation C7's guard (`G38`) depends on.** Leaving `type` unallocated is what let
the C7 false-positive storm hide, so it is pinned here:

| record type | count | derivation |
|---|---|---|
| `paid` `deposit`/`tuition` | **16,675** | 16,250 base + 350 second `deposit` + 75 superseding |
| `paid` `fee` (the person's only `paid` record) | **650** | 300 C7 plants + 350 fee+deposit persons' first record |
| `refunded` `deposit` | **425** | 100 C13 + 250 `G14` shape (ii) + 75 `G14` shape (i) |
| planted C2 (attributable to nobody) | **200** | |
| planted C11 second payment | **50** | |
| **total** | **18,000** | |

Base payments decompose as 16,250 + 300 + 350 + 100 + 250 + 75 = **17,325**, one per payments-present
child. **16,675 of the 17,325 payments-present children hold a `paid` `deposit`/`tuition`**; the 650 that
do not are exactly 300 C7 + 100 C13 + 250 shape-(ii) refunds — and the shape-(ii) 250 sit at a
`refunded`/`withdrawn` `stage_funnel`, so only 400 of them can reach C7's predicate. Adding the 400 C5
plants and the 75 C8 `payments`-drops gives §9.1(b)'s asserted C7 raw sweep of **875**. The 100 planted
C12 are drawn from within the 16,675 and consume no extra record (§11.8).

Refund population: 100 planted C13 (single refunded `deposit`, both downstream fields stale) + 250 clean
refunds of `G14` shape (ii) (enrollment → `refunded`/`withdrawn`, status out of `{enrolled, active}`) +
75 clean refunds of shape (i). No refund consumes an extra record except shape (i)'s superseding payment.

> **The payments budget is the binding constraint of the whole dataset, and it has 475 records of slack.**
> Because presence in `payments` costs one record per tri-source child, the *maximum attainable*
> tri-source fraction is `(18,000 − 200 C2 − 50 C11) / 25,000 = 0.710`. A.1's "~70%" is satisfiable;
> the upper end of the asserted band (0.72) is **not constructible**. We pin 0.690, which leaves **250**
> students of margin against the 0.68 floor (0.690 × 25,000 = 17,250 against 0.68 × 25,000 = 17,000) and
> 500 against the 0.710 constructive ceiling (17,750 − 17,250). This is flagged in §12
> (D-5) rather than papered over.

### 11.7 Deals — 15,000 (per household, `D2` only)
Deals must cover every person with (`paid` payment ∧ enrollment) except the 500 planted C1 (`G9`).

| bucket | deals | persons covered |
|---|---|---|
| tri-source multi-child households | 2,550 | 5,915 (5,990 − 75 dropped from `crm`) |
| tri-source single-child households, minus the 500 C1 plants | 10,910 | 10,910 |
| **subtotal — clean paid+enrolled** | **13,460** | **16,825** (1.250 contacts/deal) |
| `{appdb, crm}` households at `prospect`/`applied` | 1,540 | 1,540 |
| **total live at generation 3** | **15,000** | |
| *supernumerary C9 branch-1 targets — present in gens 1–2, deleted before gen 3* | *50* | *0 (their households keep a live deal)* |
| **total emitted at generation 1** | **15,050** | |

The 50 deleted deals are **supernumerary**: each pointing household already holds its own live gen-3
`D2` deal, so the deletion creates a stale `crm_deal_id` (C9 branch 1) without turning the person into an
unplanted C1 (`G9`). Generation-3 deal count is the A.1 volume of 15,000 exactly (§9.1a).

This is ruling F's pinned allocation (~13,500 / ~1,500) realised exactly. **Derivation of the 3,000
multi-child households:** covering 16,825 persons with ≤13,500 deals requires ≥1.246 persons per
deal-bearing household, i.e. the deal-bearing multi-child households must contribute ≥3,365 "extra"
children beyond one each. The 2,550 tri-source multi households contribute 5,990 − 2,550 = 3,440. A.4's
floor of 1,000 multi-child households would contribute at most ~1,400 and the arithmetic would **not**
close — this is why the household count is 3,000, not 1,000.

### 11.8 A.4 conflict minimums — all fourteen, simultaneously
| # | type | min | where it comes from | collides with any other constraint? |
|---|---|---|---|---|
| C1 | paid-but-no-deal | 500 | 500 of the 11,410 tri single-child households | no — deals are allocated *after* C1 selection (`G9`); the only other zero-deal population is the 75 C8 `crm`-drops, suppressed by `PRECEDENCE` 8 |
| C2 | payment-with-no-person | 200 | 200 of the 18,000 payments | no — budgeted in §11.6 |
| C3 | duplicate-by-email | 300 pairs | 300 extra contacts in §11.4 | no — allowlisted in `G5`/`G23` |
| C4 | same-person-different-emails | 250 | 250 of the 21,525 contact-linked students | no — `G3` still requires them to link, and `G19` forces them onto `L3`, which is C4's detection predicate |
| C5 | record-in-one-source-only | 400 | 400 of the 3,400 `{appdb}`-only students | no — `G16` reserves `enrolled` status for them |
| C6 | field disagreement | 500 | 500 of the 21,525 linked persons | no — `G18` makes every other linked person funnel-uniform |
| C7 | enrolled-but-unpaid | 300 | 300 tri single-child households | no — `G35` gives them a `fee` payment and a deal; `G38` keeps every other paid-implying enrollment backed by a payment |
| C8 | dropped sibling | 150 | 150 of the 2,550 tri multi-child households | no — `G2` makes every other multi household mask-uniform |
| C9 | stale pointer | 100 | 100 of the ~13,200 enrollments with non-null `crm_deal_id` | no |
| C10 | merge-collapsed | 50 | 50 contacts, allowlisted in `G5` | no |
| C11 | duplicate payment | 50 pairs | 50 extra payments in §11.6 | no — `G7` guard band |
| C12 | wrong-amount payment | 100 | 100 existing payments | no — `G34` |
| C13 | refund not reflected | 100 | 100 existing refunded payments | no — `G14`/`G15` partition the refund population |
| C14 | sensitive-field-only fix | 50 | 30 name-only + 10 dob-only + 10 stage-only | no — `G37` |
| | **golden entries (min)** | **3,050** | | |

A.5: ≥10% of the 3,050 surviving entries must involve two overlapping causes → **≥305 entries with a
non-empty `compound_with`** (§8 defines the field and the ratio). The generator plants **350** compound
**entities** (e.g. a C8 dropped sibling whose guardian also has an email variant → C8 + C4; a C3 pair
where one twin also carries a stale pointer → C3 + C9); each contributes ≥2 entries to the numerator, so
the surviving numerator is ≥700 against a floor of 305 — roughly 2.3× margin, which is deliberate,
because `PRECEDENCE` removes overlaps and the margin must absorb that. Pairs removed by the mechanical
suppressions (`PRECEDENCE` 2 and 4–8) never enter `compound_with` and do not count (`G32`, `G33`).

### 11.9 The ≥85%-fully-consistent gate
**Entity** is defined in §5.2: one resolved person, plus each payment attributed to no person.

| bucket | entities |
|---|---|
| resolved persons anchored on a student | 25,000 |
| resolved persons anchored on an unlinked lead contact | 18,175 |
| unattributed payments (planted C2) | 200 |
| **denominator** | **43,375** |

The 300 C3 duplicate contacts contribute **zero** new persons: `G23` requires both contacts of a pair to
carry `external_id == student.id`, so both `L1`-link into the same person. The denominator is therefore
exact, not approximate.

Distinct inconsistent entities: 3,050 golden entries touch 3,100 entity slots (C10 names two persons
each); 350 of those are compound overlaps, so **≤2,750 distinct entities are inconsistent**.

| reading | fully-consistent fraction |
|---|---|
| pinned definition (persons + unattributed payments) | (43,375 − 2,750) / 43,375 = **0.9366** |
| strict alternative, excluding lead-only persons | (25,200 − 2,750) / 25,200 = **0.8909** |

The gate holds under **both** readings, so the FP score does not hinge on the denominator argument. The
pinned definition is the one asserted.

---

## 12. Brief divergences (flagged)

Every place this contract interprets or reinterprets a normative brief paragraph. Nothing here is hidden.

| id | brief text | our reading | why |
|---|---|---|---|
| **D-1** | A.1 Entities: "payments, **refunds**" (two payments-source entities) | A refund is the same `payment` record transitioning to `status='refunded'` with `refunded_at` set. There is **no** `payments:refund:<id>` source ref. | A.1 caps the payments source at 18,000 records *inclusive of refunds*; a separate refund entity would consume that budget twice and make §11.6 unsatisfiable. |
| **D-2** | A.4 C6: "sources disagree on grade, stage, or **name spelling**" | Because §6 makes sensitivity a pure function of field path and **both** name paths (and both DOB paths, and both `stage` paths) are sensitive, a name-only, dob-only or stage-only disagreement is emitted as **C14**. A name or DOB disagreement **co-occurring** with a `grade` or `lifecycle` disagreement is emitted as **C6** with all paths listed, and the generator is required to plant 80 such mixed cases (`G37`). The C6 minimum of 500 is met by `grade` and `lifecycle` disagreements. | The alternative is incoherent: v1's C6/C14 pair had two vocabularies and one subset test, which made C14 either unfireable (0 of 50 planted found) or vacuously true on every linked person (~17,500 false positives). Partitioning by field-path sensitivity is the only reading under which both rules are computable and both minimums are reachable. Name spelling still surfaces to the reviewer with the exact disagreeing fields — under a type that additionally forces `sensitive_hold`, which is *stricter* than the brief requires. **One consequence, recorded here because it interacts with C10:** the C10 collapsed contact is `L1`-linked to student A while its `(first_norm, last_norm, dob_norm)` equals student B's, so the name/DOB comparisons on person A necessarily disagree; being wholly sensitive under this partition they would emit 50 C14 entries the §11.8 budget does not carry. `PRECEDENCE` 2 suppresses C6 **and C14** on those persons, and `G21` asserts none survives. |
| **D-3** | A.4 C11: "Same `payment_id`, or same (payer, amount, ±10min) pair twice" | The `payment_id`-repeat branch is **deleted**. A repeated PK reaching the adapter is a structural rejection (4xx) per the brief's own malformed-payload requirement, and both rows would produce the identical ref string so the "two payment refs" set would collapse to one element. C11 is the `(payer, amount, type, <600s)` branch only, further restricted to two payments resolving to the **same** person. | The brief mandates both behaviours for a duplicate PK and they are mutually exclusive. We chose the one the Edge-Cases section states as an absolute ("never a 500 **or a silent skip**"), and made the duplicate-PK case a CRM contact so the two requirements never meet (`G27`). |
| **D-4** | Core #2 invariant example: "every deal maps to the correct pipeline" | `deal.pipeline` is given a committed vocabulary and generated consistent with the household's `program` by construction (`G29`); a pipeline/program mismatch is **not** a conflict class in this contract, and `crm.deal.pipeline` is **not** auto-apply eligible. | Adding a pipeline rule would be a fifteenth conflict type — out of scope. Leaving `pipeline` auto-apply-eligible with no defined correct value would let the reconciler write an unconstrained value into a field no rule can check. |
| **D-5** | A.1: "**~70%** of students must exist in all three sources" | Pinned at **0.690** (17,250 / 25,000). | The payments budget of 18,000 makes 0.710 the hard constructive ceiling (§11.6): every tri-source child costs one payment record. The asserted band [0.68, 0.72] is kept as written, but its top ~1.5 points are unreachable at A.1 volumes. Stated here rather than discovered at seed time. |
| **D-6** | A.4 C8: "exactly one child is missing from exactly one downstream source" | Detection is as written, **plus** `PRECEDENCE` 8 (C8 over C1/C7) suppresses the mechanically-implied conflicts on the dropped child. | A child dropped from `crm` has no contact, therefore no `D2` deal, therefore satisfies C1 verbatim; a child dropped from `payments` has no paid deposit, therefore satisfies C7 verbatim. Neither can be constructed away without breaking household funnel-uniformity (`G18`), which is itself the guard against a C14 storm on `crm.deal.stage` vs `appdb.enrollment.stage`. The suppression is scoped to the dropped child of a *detected* C8 and never hides a C1/C7 elsewhere. |
| **D-7** | Sensitive fields: "Billing ownership: the payer or billing-owner of any payment **or account**" | `crm.contact.email`, `appdb.student.guardian_email` and `appdb.student.guardian2_email` are classified **sensitive**. | §1 itself calls these the guardian/billing emails. Intended consequence: all 250 C4 proposals are `sensitive_hold`. Over-classifying is always safe under the brief; under-classifying is a graded failure. |
| **D-8** | Sensitive fields: "enrollment/**deal** status transitions that create or remove a financial obligation" | `crm.deal.stage` is classified **sensitive** and removed from auto-apply eligibility. | §2.3's own map is `Deposit Received→deposit_paid`, `Closed Won→enrolled`, `Refunded→refunded` — the brief's three examples verbatim. v1 whitelisted it under "routing"; a 0.96-confidence proposal would have written a financial-obligation transition unattended. |
| **D-9** | A.4: "fixtures ship as ≥3 **sync generations** (three snapshots per source)" | Each generation file is a **complete snapshot**; absence from the gen-3 snapshot **is a deletion**. Current state is generation 3 globally. | The delta reading (v1's "gen 2 = corrections + arrivals") makes every unchanged record vanish from current state and turns essentially every conflict into a false negative. It also leaves deletion undefined, and C8's dropped sibling and C9's non-existent deal are representable **only** as absence. |
| **D-10** | A.4: "Multi-child households: ≥1,000 households with 2–4 children" | 3,000 multi-child households. | Not a divergence in kind — the brief states a minimum — but it is a forced one: at 1,000 households the 15,000-deal budget cannot cover the paid+enrolled population (§11.7). Recorded so the number is not mistaken for an arbitrary choice. |
| **D-12** | A.1 record volumes (40,000 / 15,000 / 25,000 / 22,000 / 18,000) | The five volumes are asserted on the **generation-3** snapshot. Generations 1–2 carry 75 extra CRM contacts, 50 extra deals and 75 extra payments — the records deliberately deleted before gen 3 to represent C8's dropped sibling and C9's non-existent deal. | Under D-9's snapshot semantics a deletion is representable **only** as presence in an earlier generation and absence from gen 3. Asserting the A.1 volumes on gen 1 instead would force gen 3 to 39,925 / 14,950 / 17,925 and break §§11.4/11.6/11.7, which already net the deletions to the A.1 figures. |
| **D-11** | `docs/TASKS.md` T-2 non-goal: "generator never imports detector code other than `normalize`" | **Superseded.** `recon/er.py` joins `recon/normalize.py` and `recon/reference.py` on the shared-module list; the generator runs the real ER cascade in pass 2 (`G31`). | Discovered-reality pivot. Not a brief divergence — an internal-doc divergence, recorded here so it is not re-litigated. Detection remains independently graded: the generator still never runs the invariant rules, and which conflicts exist and of what type remains independent ground truth from the plant record. |
| **D-13** | A.4: "Multi-child households: **≥1,000**" and "**≥3,000** CRM contacts are legitimately deal-less leads", read by §9 as structural minimums identical in both profiles | Identical in both profiles for malformed cases and the oscillation set; **scaled with volume** for multi-child households and deal-less leads. `full` carries 3,000 households and 18,175 leads (both far above A.4); `dev` carries the same structures at 1/20 scale. | A.4's floors are stated "per 100k records". At the dev profile's 1,250 students, 1,000 multi-child households of 2–4 children would need ≥2,000 children — the clause is arithmetically unsatisfiable, not merely undesirable. §9 previously asserted the stronger reading with nothing binding it, so the dev profile silently scaled two of the four while the text said none scaled. Both readings are now asserted by `sc_structural_minimums`, whichever applies to the profile. |
| **D-14** | §7 / A.4: "gen 1 = baseline; **gen 2 changes and adds records**; gen 3 = current state" | Generation 2 **changes** records (the oscillation set) and adds **none**. The only cross-generation deltas in the dataset are the ≥25 A→B→A `crm.contact` fields and the three deletion sets (75 contacts, 50 deals, 75 payments) present in gens 1–2 and absent from gen 3. | Under D-12 the A.1 volumes are asserted on the **generation-3** snapshot and §9.1(a) pins the gen-1 counts at exactly gen-3-plus-deletions (40,075 / 15,050 / 18,075). A record that first appears in gen 2 and survives into gen 3 would make the gen-1 count *lower* than that, contradicting a clause the self-check enforces; a record present only in gen 2 would change nothing observable, since invariants read generation 3 only. Recorded rather than papered over: the `raw_records` per-generation append path and the `source_generations` ledger are exercised with zero arrivals, and a future revision that wants arrivals must move §9.1(a)'s pinned gen-1 numbers first. |

---

## 13. Superseded from v1

Everything v1 asserted that v2 replaces. No v1 section was dropped; §§1–9 all survive, reorganised.

| v1 text | status in v2 |
|---|---|
| Shared modules = `normalize.py` + `reference.py` | **Extended** — `er.py` added (§0 preamble, `G31`) |
| `canonical_id = uuid5(NS, "\|".join(sorted(source_refs)))` | **Replaced** by `person_key` anchored on one identity ref (§4.1) |
| `D1` (`enrollment.crm_deal_id == deal.deal_id`) as a deal↔person link rule | **Deleted** (§4.5) — it is the pointer under test by C9 |
| C6 compared list `grade, stage, state, lifecycle_stage↔status` | **Replaced** by `COMPARED_FIELDS` (§2.4); `state` removed from every comparison |
| C7 `(or deposit_paid_at non-null)` | **Deleted** (§5.5 C7) — `deposit_paid_at` is never a trigger |
| C11 `same payment_id twice` branch | **Deleted** (§5.5 C11, §12 D-3) |
| C13 unscoped `status = refunded` predicate | **Replaced** by the four-clause recency-scoped predicate (§5.5 C13) |
| C9 "a deal whose person ≠ the enrollment's person" | **Replaced** by the `D2` person-**set** test with an `unchecked` empty case (§5.5 C9) |
| C10 over `entity_links` | **Replaced** by `entity_link_candidates` (§4.7, §5.5 C10) |
| C10 refs "contact ref + both persons' identity refs" | **Replaced** by exactly three refs, no transitive expansion |
| `fingerprint = sha256(… \| ",".join(sorted(observed_values)))` | **Replaced** by the `\x1f`-delimited form over `sorted(observed_values.items())` with `canon_value` (§5.4) |
| Survivorship tiebreak "most-recent generation" | **Replaced** by lowest source ref / lowest `crm_id` (§4.6) |
| Two dangling cross-references to a non-existent "§4.8" | **Resolved** — §4.8 exists (household inference) |
| "unknown enum" listed among malformed cases | **Removed** (§7) — malformedness is structural only; replaced by a non-object JSONL line |
| `MAX_PAYLOAD_BYTES` referenced with no value | **Pinned** at 262144 (§2.2) |
| Auto-apply eligibility incl. `crm.deal.stage`, `crm.deal.pipeline`, `crm.contact.state` | **Trimmed** to the five fields a fix template writes (§6); `crm.deal.stage` moved into `SENSITIVE_FIELDS` |
| Precedence rules 1–5 | **Extended** to 11 rules (§5.7) |
| "Invariants read the latest generation" | **Pinned** to `generation = 3` with snapshot semantics (§7) |
| "~100,000-record Appendix-A dataset" | **Pinned** at 120,000 records (§9, §11.1) |
| C6 min counted per unknown unit | **Pinned** to persons, one conflict per person per generation (§5.2) |
| FP guards phrased as claims about the world | **Replaced** wholesale by §10's construction constraints, each cited from §5.5 |
| `docs/DESIGN.md` interface/data-model lines for **ER output**, **survivorship tiebreak**, **fingerprint**, **`field_lineage` keying** and the **completeness ledger** | **Superseded**; this contract governs (§0 order of authority). DESIGN.md has been updated to point here. Its "Decisions & rationale" block is untouched. |

---

## 14. Pinned rulings (v2.1) — the ambiguity ledger

Sixteen places where this document previously required an implementer to guess. Each is now normative
text in the section named, and each is bound by a test that fails when the behaviour changes. They are
collected here so a reader can confirm none was left to convention, and so a future edit that softens
one is visible as a change to a numbered ruling rather than as a rewording.

| # | ruling | pinned in |
|---|---|---|
| 1 | `KEYSTONE_NS` is the committed literal `17733ea0-28dd-5aeb-a266-c62b3689def8`; `uuid5(NAMESPACE_DNS, "keystone.invariant-contract.v2")` is recorded as provenance and is never re-derived by code | §2.2 |
| 2 | The `canon_value` **sequence** case is normative, injective, and committed once | §2.5 |
| 3 | The fingerprint wire format: `\|` section separator, `\x1f` intra-section joiner, `k=canon_value(v)` item form, verbatim type, sha256/UTF-8 | §5.4 |
| 4 | Timestamps in `canon_value`: naive is UTC, second precision, microseconds truncated | §2.5 |
| 5 | New `detail.reason` code `unparseable_value` for a present-but-unparseable **non-enum** operand; the `None` causes are three, disjoint, exhaustive and precedence-ordered | §5.1, §5.8 |
| 6 | `norm_name` removes quote characters **anywhere**; `norm_email` strips **surrounding** only — asymmetric on purpose | §2.1 |
| 7 | `QUOTE_CHARS` is the seven-character committed set, curly quotes included | §2.1 |
| 8 | The C6/C14 fix-target selection when a disagreeing set is mixed, and that C6/C14 templates write the **CRM** side (resolves MINOR-8) | §6 |
| 9 | The `PRECEDENCE` matching predicate is `entity_refs` set **intersection**; rule 2 is keyed only on the collapsed `crm:contact:` ref | §5.7 |
| 10 | `match_keys` emits no `namedob` key unless first, last **and** dob are all present, and the consequence for `entity_link_candidates` | §2.1, §4.7 |
| 11 | The grade variant families are a **closed** set the generator may not draw outside of | §2.3 |
| 12 | `state` is exactly the 50 states — no DC, no territories — and the generator must not emit `DC` | §2.3 |
| 13 | `round()` is banker's rounding, and `G39` forbids any amount at exactly half a cent so the tie-break is unobservable | §2.5, §1.2, §10 `G39` |
| 14 | `norm_email` on a value with no `@`: trim / strip surrounding quotes / casefold only — never gmail logic | §2.1 |
| 15 | `household_members` and `KEY_CLASSES` are **exported** shared symbols, with pinned key set and member ordering (resolves MINOR-5's sibling: an unexported shared symbol gets re-implemented) | §4.8, §2.1, §0 |
| 16 | The **value** construction of the three multi-valued `observed_values` keys — `C9.deal_person_refs` in particular: one `anchor_ref` per resolved person, never identity refs and never a `person_key` | §5.4 |
| — | `is_identity_ref(ref, *, payment_attributed=False)` — the payment clause is a scoped argument, not an assumption baked into the ref string (resolves MINOR-5) | §4.1 |
