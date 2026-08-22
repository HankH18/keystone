"""Committed reference data -- the second half of the anti-drift keystone (R23).

Contract v2 SS2.2-SS2.5, SS4.1, SS4.8, SS5.2, SS5.4, SS5.7, SS6. Both the seed
generator and the detector import this module; **neither may re-implement any of
it**. Anything computable two ways is a silent divergence, and the exact-match
grade is lost the moment one appears.

In particular three things are exported as *one callable each*, never as a table
each side interprets for itself:

* `apply_precedence(entries)` -- the SS5.7 filter. `golden/conflicts.json` is
  written through it and the detector materializes conflicts through it.
* `conflict_refs(...)`      -- the SS5.4/SS5.5 `entity_refs` builder.
* `fingerprint(...)`        -- the SS5.4 hash.

Layering: this module imports `recon.normalize` and nothing else from `recon`.
The canonical vocabularies live in `recon/normalize.py` (they are what drives
`norm_enum`) and are re-exported here rather than restated.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from recon.normalize import (
    DEAL_STAGE_VALUES,
    GRADE_VALUES,
    KEY_CLASSES,
    LIFECYCLE_VALUES,
    PROGRAM_VALUES,
    STAGE_FUNNEL_VALUES,
    STATE_VALUES,
    STATUS_VALUES,
    match_keys,
    norm_dob,
    norm_email,
    norm_enum,
    norm_name,
)

__all__ = [
    "A1_VOLUMES",
    "AUTO_APPLY_ELIGIBLE",
    "C11_PLANT_MAX_SECONDS",
    "C11_WINDOW_SECONDS",
    "COMPARED_FIELDS",
    "COMPARED_FIELD_BY_LOGICAL",
    "COMPARED_FIELD_BY_PATH",
    "COMPARED_FIELD_PATHS",
    "CONFLICT_MINIMUMS",
    "CONFLICT_TYPES",
    "DEAL_STAGE_TO_FUNNEL",
    "DEAL_STAGE_VALUES",
    "ENROLLMENT_GRADE_FLOOR",
    "FEE_SCHEDULE",
    "FIX_TARGETS",
    "FUNNEL_VALUES",
    "GRADE_ORDER",
    "GRADE_VALUES",
    "IDENTITY_REF_CLASSES",
    "KEYSTONE_NS",
    "KEY_CLASSES",
    "LEGIT_REPEAT_MIN_SECONDS",
    "LIFECYCLE_TO_FUNNEL",
    "LIFECYCLE_VALUES",
    "MAX_PAYLOAD_BYTES",
    "MECHANICAL_SUPPRESSIONS",
    "NAME_CORPUS_MIN",
    "NAME_CORPUS_MIN_FIRST",
    "NAME_CORPUS_MIN_LAST",
    "OBSERVED_VALUE_KEYS",
    "PAID_IMPLYING_STAGES",
    "PAYMENT_TYPES",
    "PRECEDENCE",
    "PROGRAM_VALUES",
    "REF_CLASSES",
    "REF_SPECS",
    "RULE_ID_BY_TYPE",
    "SENSITIVE_FIELDS",
    "SOURCE_IDS",
    "STAGE_FUNNEL_VALUES",
    "STATE_VALUES",
    "STATUS_TO_FUNNEL",
    "STATUS_VALUES",
    "UNCHECKED_REASONS",
    "UNCHECKED_REASON_PRECEDENCE",
    "VERDICTS",
    "ComparedField",
    "Comparison",
    "FixTarget",
    "Money",
    "PrecedenceRule",
    "RefSpec",
    "TotalMap",
    "anchor_ref",
    "apply_precedence",
    "assert_unique_conflict_keys",
    "canon_value",
    "compare_field",
    "compare_record",
    "conflict_key",
    "conflict_refs",
    "conflict_type_for_paths",
    "disagreeing_fields",
    "fee_amount_cents",
    "fingerprint",
    "fix_target",
    "grade_ord",
    "household_anchor_student",
    "household_key",
    "household_members",
    "household_members_appdb",
    "is_auto_apply_eligible",
    "is_identity_ref",
    "is_sensitive",
    "make_ref",
    "match_keys",
    "norm_dob",
    "norm_email",
    "norm_enum",
    "norm_name",
    "parse_ref",
    "person_key",
    "ref_source",
    "sources_involved",
    "student_ref",
    "validate_observed_values",
]


# ======================================================================================
# SS2.2 Committed constants
# ======================================================================================

#: SS2.2 -- fixed uuid5 namespace, committed literal. Every `uuid5` in the system
#: (student ids, `person_key`) is drawn from it, so it may never change.
KEYSTONE_NS: uuid.UUID = uuid.UUID("17733ea0-28dd-5aeb-a266-c62b3689def8")

#: SS2.2 -- 256 KiB. A single JSONL line above this is rejected by the adapter with
#: the documented 4xx; SS7's oversized case is `MAX_PAYLOAD_BYTES + 1` bytes.
MAX_PAYLOAD_BYTES: int = 262144

#: SS2.2 -- enrollment funnel stages that assert a payment has happened.
PAID_IMPLYING_STAGES: frozenset[str] = frozenset({"deposit_paid", "enrolled"})

#: SS2.2 -- the C8 mask-eligibility floor (`GRADE_ORDER` 0).
ENROLLMENT_GRADE_FLOOR: str = "K"

#: SS2.2 -- C11's window, strict `<` (SS5.2).
C11_WINDOW_SECONDS: int = 600
#: SS2.2 -- planted C11 pairs are at most this far apart (`G7`).
C11_PLANT_MAX_SECONDS: int = 300
#: SS2.2 -- every legitimate same-person same-type repeat is at least this far apart (`G7`).
LEGIT_REPEAT_MIN_SECONDS: int = 1200

#: SS2.2 -- `NAME_CORPUS_MIN` = 2000 first names x 1000 last names (`G5`).
NAME_CORPUS_MIN_FIRST: int = 2000
NAME_CORPUS_MIN_LAST: int = 1000
NAME_CORPUS_MIN: tuple[int, int] = (NAME_CORPUS_MIN_FIRST, NAME_CORPUS_MIN_LAST)

#: SS3 -- the three source ids.
SOURCE_IDS: tuple[str, ...] = ("appdb", "crm", "payments")

#: SS1.5 -- payment `type` vocabulary.
PAYMENT_TYPES: tuple[str, ...] = ("fee", "deposit", "tuition")

#: SS5.8 -- pinned, closed verdict vocabulary.
VERDICTS: frozenset[str] = frozenset({"ok", "conflict", "unchecked"})

#: SS5.8 -- pinned `detail.reason` vocabulary; required on `unchecked`, forbidden otherwise.
UNCHECKED_REASONS: frozenset[str] = frozenset(
    {
        "no_rule_in_scope",
        "missing_operand",
        "unmapped_enum",
        # SS5.1/SS5.8 ruling 5: a PRESENT but unparseable **non-enum** operand -- an
        # unparseable `crm.contact.dob`, a name that is nothing but quote characters.
        # It is not `missing_operand` (the source value was not NULL) and not
        # `unmapped_enum` (no enum was consulted; `norm_dob`/`norm_name` are not
        # table-driven), so forcing it into either would put a false statement in
        # `detail.reason` -- and the two sides would force it differently.
        "unparseable_value",
        "enrollment_unattributed",
        "deal_unresolved",
        "source_incomplete",
    }
)

#: SS5.1 ruling 5 -- one comparison emits one reason, so the three `None` causes are
#: ordered. A NULL operand is the most specific statement available and wins.
UNCHECKED_REASON_PRECEDENCE: tuple[str, ...] = (
    "missing_operand",
    "unparseable_value",
    "unmapped_enum",
)

#: SS5.5 -- the fourteen conflict types, in committed order.
CONFLICT_TYPES: tuple[str, ...] = tuple(f"C{n}" for n in range(1, 15))

#: SS5.5 -- conflict type -> the rule that emits it.
RULE_ID_BY_TYPE: dict[str, str] = {f"C{n}": f"R-{n:03d}" for n in range(1, 15)}

#: SS11.1 -- the five A.1 generation-3 record volumes (`full` profile, `G33`).
A1_VOLUMES: dict[tuple[str, str], int] = {
    ("crm", "contact"): 40000,
    ("crm", "deal"): 15000,
    ("appdb", "student"): 25000,
    ("appdb", "enrollment"): 22000,
    ("payments", "payment"): 18000,
}

#: SS11.8 -- the fourteen A.4 minimums (`full` profile, `G33`). C3 and C11 count pairs.
CONFLICT_MINIMUMS: dict[str, int] = {
    "C1": 500,
    "C2": 200,
    "C3": 300,
    "C4": 250,
    "C5": 400,
    "C6": 500,
    "C7": 300,
    "C8": 150,
    "C9": 100,
    "C10": 50,
    "C11": 50,
    "C12": 100,
    "C13": 100,
    "C14": 50,
}


# ======================================================================================
# SS2.3 Committed enum maps (total; a missing key raises at import)
# ======================================================================================


class TotalMap(Mapping[Any, Any]):
    """A mapping pinned total over a declared domain (SS2.3).

    Construction raises `ValueError` -- **at import time** -- if the mapping does
    not cover the declared domain exactly. Looking up a key outside the domain
    raises `KeyError`: an undeclared key is a bug in the caller, not dirty data.
    Dirty data reaches `norm_enum`, which returns `None` (SS5.1).
    """

    __slots__ = ("_data", "domain", "name")

    def __init__(self, name: str, domain: Iterable[Any], mapping: Mapping[Any, Any]) -> None:
        self.name = name
        self.domain = tuple(domain)
        declared = set(self.domain)
        provided = set(mapping)
        missing = sorted(declared - provided)
        extra = sorted(provided - declared)
        if missing or extra:
            raise ValueError(
                f"{name} is not total over its declared domain: missing={missing!r} extra={extra!r}"
            )
        self._data = dict(mapping)

    def __getitem__(self, key: Any) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self.domain)

    def __len__(self) -> int:
        return len(self.domain)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TotalMap({self.name!r}, {len(self.domain)} keys)"


#: SS2.3 -- **ordinal**, pinned because *string* comparison is wrong here:
#: `'PK' < 'K'` is FALSE and `'1' < 'K'`, `'10' < 'K'`, `'12' < 'K'` are all TRUE.
GRADE_ORDER: TotalMap = TotalMap(
    "GRADE_ORDER",
    GRADE_VALUES,
    {
        "PK": -1,
        "K": 0,
        "1": 1,
        "2": 2,
        "3": 3,
        "4": 4,
        "5": 5,
        "6": 6,
        "7": 7,
        "8": 8,
        "9": 9,
        "10": 10,
        "11": 11,
        "12": 12,
    },
)

if len(set(GRADE_ORDER.values())) != len(GRADE_ORDER):  # pragma: no cover - import guard
    raise ValueError("GRADE_ORDER is not injective")

#: SS2.3 -- the canonical enrollment funnel.
FUNNEL_VALUES: tuple[str, ...] = STAGE_FUNNEL_VALUES

#: SS2.3 -- bijective onto the funnel, so the cross-source comparison is lossless.
DEAL_STAGE_TO_FUNNEL: TotalMap = TotalMap(
    "DEAL_STAGE_TO_FUNNEL",
    DEAL_STAGE_VALUES,
    {
        "New Lead": "prospect",
        "Application Submitted": "applied",
        "Waitlisted": "waitlisted",
        "Deposit Received": "deposit_paid",
        "Closed Won": "enrolled",
        "Closed Lost": "withdrawn",
        "Refunded": "refunded",
    },
)

if sorted(DEAL_STAGE_TO_FUNNEL.values()) != sorted(FUNNEL_VALUES):  # pragma: no cover
    raise ValueError("DEAL_STAGE_TO_FUNNEL is not bijective onto the funnel")

#: SS2.3 -- `appdb.student.status` -> funnel.
STATUS_TO_FUNNEL: TotalMap = TotalMap(
    "STATUS_TO_FUNNEL",
    STATUS_VALUES,
    {
        "prospect": "prospect",
        "applied": "applied",
        "enrolled": "enrolled",
        "active": "enrolled",
        "withdrawn": "withdrawn",
    },
)

#: SS2.3 -- total over the committed `lifecycle_stage` vocabulary. The `None`-mapping
#: subset {subscriber, evangelist, other} is how a `withdrawn` student is represented
#: on the CRM side: no lifecycle value maps to `withdrawn` (`G18`). `None` on either
#: side of a comparison is `verdict='unchecked'`, never a disagreement (SS5.1).
LIFECYCLE_TO_FUNNEL: TotalMap = TotalMap(
    "LIFECYCLE_TO_FUNNEL",
    LIFECYCLE_VALUES,
    {
        "subscriber": None,
        "lead": "prospect",
        "marketingqualifiedlead": "prospect",
        "MQL": "prospect",
        "salesqualifiedlead": "applied",
        "SQL": "applied",
        "opportunity": "applied",
        "customer": "enrolled",
        "evangelist": None,
        "other": None,
    },
)

if "withdrawn" in set(LIFECYCLE_TO_FUNNEL.values()):  # pragma: no cover - import guard
    raise ValueError("no lifecycle value may map to `withdrawn` (G18)")

#: SS2.3 -- the fee schedule, exact, in cents. Total over program x payment type.
FEE_SCHEDULE: TotalMap = TotalMap(
    "FEE_SCHEDULE",
    tuple((program, kind) for program in PROGRAM_VALUES for kind in PAYMENT_TYPES),
    {
        ("Lower School", "fee"): 10000,
        ("Lower School", "deposit"): 50000,
        ("Lower School", "tuition"): 1200000,
        ("Middle School", "fee"): 10000,
        ("Middle School", "deposit"): 60000,
        ("Middle School", "tuition"): 1400000,
        ("Upper School", "fee"): 10000,
        ("Upper School", "deposit"): 75000,
        ("Upper School", "tuition"): 1600000,
        ("Summer Academy", "fee"): 10000,
        ("Summer Academy", "deposit"): 25000,
        ("Summer Academy", "tuition"): 300000,
    },
)


def grade_ord(grade: str | None) -> int | None:
    """`GRADE_ORDER` ordinal of a raw grade value, or `None` if unmappable.

    Materialized as the `grade_ord` `stg_*` column (SS2.3). Ordinal, never a string
    comparison: `grade_ord("PK") < grade_ord("K") < grade_ord("1")`.
    """
    canonical = norm_enum("grade", grade)
    if canonical is None:
        return None
    return GRADE_ORDER[canonical]


def fee_amount_cents(program: str | None, payment_type: str | None) -> int | None:
    """The exact scheduled amount in cents for `(program, type)` (SS2.3), else `None`.

    `program` may be dirty -- it goes through `norm_enum('program', ...)`. `None`
    means the pair is unresolvable, which C12 turns into `unchecked` (SS4.4).
    """
    canonical = norm_enum("program", program)
    if canonical is None or payment_type not in PAYMENT_TYPES:
        return None
    return FEE_SCHEDULE[(canonical, payment_type)]


# ======================================================================================
# SS2.5 canon_value -- the canonical value serializer
# ======================================================================================


@dataclass(frozen=True, order=True)
class Money:
    """Integer cents (SS2.5). The explicit wrapper type; a bare `float` is forbidden.

    `crm.deal.amount` (dollars, float -- CRM-shaped) becomes
    `Money(round(amount * 100))` at the `stg_crm_deal` boundary and only ever
    reaches `canon_value` in that form.
    """

    cents: int

    def __post_init__(self) -> None:
        if isinstance(self.cents, bool) or not isinstance(self.cents, int):
            raise TypeError(
                f"Money(cents) requires an int, got {type(self.cents).__name__}; "
                "a float amount must be converted with round(amount * 100)"
            )

    @classmethod
    def from_dollars(cls, amount: float | int) -> Money:
        """`Money(round(amount * 100))` -- the one pinned float->Money conversion (SS1.2).

        `round()` is **banker's rounding** (half-to-even), pinned by SS2.5 ruling 13:
        `round(0.5) == 0`, `round(1.5) == 2`, `round(2.5) == 2`.

        What is graded here is the difference from **truncation**, not the tie-break.
        `0.29 * 100` is `28.999999999999996` in IEEE-754, so `int()` yields 28 where
        `round()` yields 29; the same one-cent error hits `1.15` (114 vs 115) and
        `8.7` (869 vs 870). Across 15,000 deals that is a systematic understatement.

        The tie-break itself is committed so this function is defined for every input,
        but `G39` forbids the generator from emitting any amount on a half-cent
        boundary, so no golden byte can ever depend on half-to-even.
        """
        return cls(round(amount * 100))


_NULL_SENTINEL = "\\N"
#: SS5.4's intra-section joiner.
_UNIT_SEPARATOR = "\x1f"
#: SS2.5 (ruling 2) -- the sequence element separator, committed **once**, here.
_ELEMENT_SEPARATOR = "\x1e"
_UNIT_ESCAPE = "\\x1f"
_ELEMENT_ESCAPE = "\\x1e"


def _escape_element(canonical: str) -> str:
    """Escape one already-canonical element for embedding in a sequence (SS2.5).

    Backslash pass FIRST, then the raw element separator -- the reverse order is not
    reversible. This is what makes a nested sequence's own separators unmistakable
    for the outer sequence's, so `canon_value([["a"], ["b"]]) != canon_value(["a", "b"])`.
    """
    return canonical.replace("\\", "\\\\").replace(_ELEMENT_SEPARATOR, _ELEMENT_ESCAPE)


def canon_value(value: Any) -> str:
    """Serialize a value canonically (SS2.5).

    Used by **both** sides wherever a value is hashed, compared as text, or written
    to `observed_values` / `field_lineage.value_canon`.

        None         -> "\\N"
        bool         -> "true" | "false"       # dispatched BEFORE int (bool subclasses int)
        int          -> decimal, no separators
        Money(cents) -> integer cents, decimal
        float        -> FORBIDDEN; raises ValueError
        date         -> "YYYY-MM-DD"
        timestamp    -> "YYYY-MM-DDTHH:MM:SSZ", naive-is-UTC, SECOND precision
        str          -> as-is, with "\\", "\\x1f" and "\\x1e" backslash-escaped
        sequence     -> normative; see below
        other        -> raises TypeError; never a Python `repr`

    A bare `float` raises rather than serializing non-deterministically, so the
    SS5.4 fingerprint is defined for every value any conflict can carry.

    **Timestamps (SS2.5 ruling 4).** A naive `datetime` is interpreted as **already
    UTC**, never in the local zone of whatever machine runs the job -- the generator
    and the detector must emit the same bytes on a laptop in `America/Chicago` and in
    a UTC container. Microseconds are **truncated**, not rounded: rounding would let a
    sub-second difference move a value across a second boundary and change a
    fingerprint.

    **Sequences (SS2.5 ruling 2) are NORMATIVE, not an implementer extension.** Three
    pinned `observed_values` keys are multi-valued (`C1.paid_payment_refs`,
    `C4.student_guardian_email_norms`, `C9.deal_person_refs`), so a serializer with no
    sequence case is incomplete and "each side joins them itself" is the drift this
    module exists to prevent. `list`/`tuple`/`set`/`frozenset` are ONE case and a
    sequence is a sorted **multiset**, so element order never reaches the digest::

        canon_value(seq) = RS + "".join(e + RS for e in sorted(escaped elements))

    The encoding is **INJECTIVE**, and that is graded, not a nicety -- the fingerprint
    is the idempotency key R16's oscillation dedup runs on, so two structurally
    different `observed_values` maps colliding to one digest silently suppresses a
    real second proposal. Three things make it injective, all load-bearing:

    * `\\x1e` is in the string escape set. Without it
      `canon_value(["a\\x1eb"]) == canon_value(["a", "b"])` -- the original defect.
    * elements are re-escaped when embedded, so
      `canon_value([["a"], ["b"]]) != canon_value(["a", "b"])`.
    * the leading marker plus one trailing marker per element makes a sequence
      self-delimiting: no scalar canonical form contains a raw `\\x1e`, hence
      `canon_value([]) != canon_value("")` and `canon_value(["a"]) != canon_value("a")`.

    The one conflation accepted is between *scalar types* carrying the same text
    (`canon_value(True) == canon_value("true")`). SS5.4 pins the `observed_values` key
    set per type and each key carries one fixed type, so no key can present two.
    """
    if value is None:
        return _NULL_SENTINEL
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Money):
        return str(value.cents)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise ValueError(
            "canon_value: float is FORBIDDEN (SS2.5). Wrap money as "
            "Money(round(amount * 100)); every other float is non-deterministic."
        )
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        # Backslash pass FIRST or the escaping stops being reversible.
        return (
            value.replace("\\", "\\\\")
            .replace(_UNIT_SEPARATOR, _UNIT_ESCAPE)
            .replace(_ELEMENT_SEPARATOR, _ELEMENT_ESCAPE)
        )
    if isinstance(value, list | tuple | set | frozenset):
        elements = sorted(_escape_element(canon_value(item)) for item in value)
        return _ELEMENT_SEPARATOR + "".join(
            f"{element}{_ELEMENT_SEPARATOR}" for element in elements
        )
    raise TypeError(f"canon_value: unsupported type {type(value).__name__}")


# ======================================================================================
# SS6 Sensitive fields, auto-apply allowlist, committed fix targets
# ======================================================================================

#: SS6 -- the whole classifier. Sensitivity is a pure function of the target field
#: path, evaluated *before* confidence.
SENSITIVE_FIELDS: frozenset[str] = frozenset(
    {
        # legal / identity
        "crm.contact.first_name",
        "crm.contact.last_name",
        "crm.contact.dob",
        "appdb.student.first_name",
        "appdb.student.last_name",
        "appdb.student.dob",
        "appdb.student.student_number",
        # billing ownership
        "payments.payment.payer_email",
        "payments.payment.payer_name",
        "appdb.enrollment.billing_owner_email",
        "crm.contact.email",
        "appdb.student.guardian_email",
        "appdb.student.guardian2_email",
        # financially-consequential status
        "appdb.enrollment.stage",
        "appdb.enrollment.deposit_paid_at",
        "appdb.student.status",
        "payments.payment.status",
        "crm.deal.stage",
        # consent / compliance
        "crm.contact.marketing_consent",
        "appdb.student.communication_opt_out",
    }
)

#: SS6 -- eligibility is an allowlist, **not** the complement of `SENSITIVE_FIELDS`.
AUTO_APPLY_ELIGIBLE: frozenset[str] = frozenset(
    {
        "appdb.enrollment.crm_deal_id",
        "payments.payment.external_ref",
        "crm.contact.external_id",
        "crm.contact.grade",
        "crm.contact.lifecycle_stage",
    }
)

if SENSITIVE_FIELDS & AUTO_APPLY_ELIGIBLE:  # pragma: no cover - import guard
    raise ValueError("a field path may not be both sensitive and auto-apply eligible")


def is_sensitive(field_path: str) -> bool:
    """SS6 classifier: a pure function of the target field path.

    **Exact set membership, never a prefix test.** `SENSITIVE_FIELDS` "**is** the whole
    classifier" (SS6), so a path is sensitive iff it is literally in the set. A prefix
    test would classify `crm.contact.first_name_suffix` as sensitive on the strength of
    `crm.contact.first_name`, and the reverse prefix test would classify the whole
    `crm.contact.` namespace -- either way the classifier stops being decidable from the
    committed list and starts depending on how a future field happens to be named.
    """
    return field_path in SENSITIVE_FIELDS


def is_auto_apply_eligible(field_path: str) -> bool:
    """SS6: eligible for auto-apply at confidence >= 0.95, approved type, full evidence."""
    return field_path in AUTO_APPLY_ELIGIBLE


@dataclass(frozen=True)
class FixTarget:
    """The committed fix target for one conflict type (SS6).

    `field_path is None` means the proposal writes no field (evidence-only), which
    is `escalated` for human review.
    """

    conflict_type: str
    field_path: str | None
    classification: str  # "eligible" | "sensitive_hold" | "escalated"


def _fix_target(conflict_type: str, field_path: str | None) -> FixTarget:
    if field_path is None:
        classification = "escalated"
    elif is_sensitive(field_path):
        classification = "sensitive_hold"
    elif is_auto_apply_eligible(field_path):
        classification = "eligible"
    else:  # pragma: no cover - import guard; no committed template writes such a path
        raise ValueError(f"fix target {field_path!r} is neither sensitive nor eligible")
    return FixTarget(conflict_type, field_path, classification)


#: SS6 -- committed fix target per conflict type. C6 is resolved by
#: `fix_target(...)` because its target depends on which paths disagree.
FIX_TARGETS: TotalMap = TotalMap(
    "FIX_TARGETS",
    CONFLICT_TYPES,
    {
        "C1": _fix_target("C1", None),
        "C2": _fix_target("C2", "payments.payment.external_ref"),
        "C3": _fix_target("C3", None),
        "C4": _fix_target("C4", "crm.contact.email"),
        "C5": _fix_target("C5", None),
        # C6's default row is the grade-only template; `fix_target()` refines it.
        "C6": _fix_target("C6", "crm.contact.grade"),
        "C7": _fix_target("C7", None),
        "C8": _fix_target("C8", None),
        "C9": _fix_target("C9", "appdb.enrollment.crm_deal_id"),
        "C10": _fix_target("C10", None),
        "C11": _fix_target("C11", None),
        "C12": _fix_target("C12", None),
        "C13": _fix_target("C13", None),
        # Every C14 writes the disagreeing sensitive path itself; `fix_target()`
        # substitutes the concrete path when the disagreeing set is supplied. The
        # default row is the CRM side, per SS6 ruling 8.
        "C14": _fix_target("C14", "crm.contact.first_name"),
    },
)

#: SS6 ruling 8 -- the CRM-side prefix. C6/C14 fix templates write the CRM record.
_CRM_PATH_PREFIX = "crm."


def _crm_side_first(paths: Iterable[str]) -> str:
    """SS6 ruling 8: pick the **CRM-side** path, breaking any remaining tie by code point.

    Byte order puts `appdb.*` before `crm.*` on every wholly-sensitive comparison row
    (`appdb.student.dob` < `crm.contact.dob`, `appdb.enrollment.stage` < `crm.deal.stage`),
    so a plain `sorted(...)[0]` always selects the app-DB side -- which contradicts the
    CRM-side convention SS6 establishes for every other template (`crm.contact.grade`,
    `crm.contact.lifecycle_stage` "CRM side only", `crm.contact.email` for C4) and would
    propose overwriting the authoritative record: SS4.6 survivorship is
    `app DB > CRM > payments` for identity fields.
    """
    ordered = sorted(paths)
    for path in ordered:
        if path.startswith(_CRM_PATH_PREFIX):
            return path
    return ordered[0]


def fix_target(conflict_type: str, paths: Sequence[str] = ()) -> FixTarget:
    """The committed fix target for a conflict (SS6), refined by its disagreeing paths.

    * C6 grade-only     -> `crm.contact.grade`            (eligible)
    * C6 lifecycle-only -> `crm.contact.lifecycle_stage`  (eligible, CRM side only)
    * C6 mixed, every C14 -> the disagreeing **sensitive** path itself (`sensitive_hold`)
    * everything else   -> the pinned row of `FIX_TARGETS`

    The selection order is pinned by SS6 ruling 8, because "the disagreeing sensitive
    path" names a *set* while the classifier is a pure function of **one** path:

    1. partition the disagreeing paths by comparison **ROW**, not by path;
    2. if any wholly-sensitive row disagrees, the target is one of ITS paths and the
       proposal is `sensitive_hold` -- the sensitive half of a mixed set decides;
    3. otherwise the target is the `AUTO_APPLY_ELIGIBLE` path of the set;
    4. ties within a step are broken by taking the **CRM-side** path, then by code point.

    A C4 proposal may never be re-targeted at `crm.contact.external_id` to escape
    the classifier (SS6, SS12 D-7); C4's row is `crm.contact.email`, full stop.
    """
    if conflict_type not in FIX_TARGETS:
        raise ValueError(f"unknown conflict type {conflict_type!r}")

    ordered = tuple(sorted(paths))
    sensitive_paths = tuple(path for path in ordered if is_sensitive(path))

    if conflict_type == "C14":
        if not sensitive_paths:
            return FIX_TARGETS["C14"]
        return _fix_target("C14", _crm_side_first(sensitive_paths))

    if conflict_type == "C6":
        # "Mixed" is defined by comparison ROW, not by path: SS5.6's mixed C6 is a
        # `name_*`/`dob` path together with a `grade` or `lifecycle` path. The
        # `lifecycle` row has one sensitive endpoint (`appdb.student.status`) and is
        # still eligible -- SS6 pins it as "CRM side only".
        rows = {COMPARED_FIELD_BY_PATH[path] for path in ordered if path in COMPARED_FIELD_BY_PATH}
        held = sorted(
            path for row in rows if row.wholly_sensitive for path in row.paths if path in ordered
        )
        if held:
            return _fix_target("C6", _crm_side_first(held))
        eligible = sorted(path for path in ordered if is_auto_apply_eligible(path))
        if eligible:
            return _fix_target("C6", _crm_side_first(eligible))
        return FIX_TARGETS["C6"]

    return FIX_TARGETS[conflict_type]


# ======================================================================================
# SS2.4 COMPARED_FIELDS -- the ONLY producer of `disagreeing_fields`
# ======================================================================================


def _map_grade(value: Any) -> str | None:
    return norm_enum("grade", value)


def _map_deal_stage(value: Any) -> str | None:
    canonical = norm_enum("deal_stage", value)
    return None if canonical is None else DEAL_STAGE_TO_FUNNEL[canonical]


def _map_stage_funnel(value: Any) -> str | None:
    return norm_enum("stage", value)


def _map_lifecycle(value: Any) -> str | None:
    canonical = norm_enum("lifecycle_stage", value)
    return None if canonical is None else LIFECYCLE_TO_FUNNEL[canonical]


def _map_status(value: Any) -> str | None:
    canonical = norm_enum("status", value)
    return None if canonical is None else STATUS_TO_FUNNEL[canonical]


@dataclass(frozen=True)
class ComparedField:
    """One SS2.4 comparison row. Both endpoints are source-qualified paths."""

    logical: str
    left_path: str
    right_path: str
    left_mapper: Any
    right_mapper: Any
    #: SS5.1 ruling 5 -- the reason this row reports when a **present** operand
    #: normalizes to `None`. Enum-mapped rows (`grade`, `stage`, `lifecycle`) report
    #: `unmapped_enum`; `norm_name`/`norm_dob` rows report `unparseable_value`. It is a
    #: property of the ROW, never a guess about the value's contents.
    unmapped_reason: str = "unmapped_enum"

    @property
    def paths(self) -> tuple[str, str]:
        return (self.left_path, self.right_path)

    @property
    def wholly_sensitive(self) -> bool:
        """True when this row *alone* would emit C14 (SS2.4's partition table)."""
        return is_sensitive(self.left_path) and is_sensitive(self.right_path)

    @property
    def emits_alone(self) -> str:
        """`C14` if this row alone is wholly sensitive, else `C6` (SS2.4, SS5.5)."""
        return "C14" if self.wholly_sensitive else "C6"


COMPARED_FIELDS: tuple[ComparedField, ...] = (
    ComparedField(
        "name_first",
        "crm.contact.first_name",
        "appdb.student.first_name",
        norm_name,
        norm_name,
        unmapped_reason="unparseable_value",
    ),
    ComparedField(
        "name_last",
        "crm.contact.last_name",
        "appdb.student.last_name",
        norm_name,
        norm_name,
        unmapped_reason="unparseable_value",
    ),
    ComparedField(
        "dob",
        "crm.contact.dob",
        "appdb.student.dob",
        norm_dob,
        norm_dob,
        unmapped_reason="unparseable_value",
    ),
    ComparedField("grade", "crm.contact.grade", "appdb.student.grade", _map_grade, _map_grade),
    ComparedField(
        "stage", "crm.deal.stage", "appdb.enrollment.stage", _map_deal_stage, _map_stage_funnel
    ),
    ComparedField(
        "lifecycle",
        "crm.contact.lifecycle_stage",
        "appdb.student.status",
        _map_lifecycle,
        _map_status,
    ),
)

COMPARED_FIELD_BY_LOGICAL: dict[str, ComparedField] = {
    field.logical: field for field in COMPARED_FIELDS
}

#: Every source-qualified path belongs to exactly one comparison row (SS2.4).
COMPARED_FIELD_BY_PATH: dict[str, ComparedField] = {
    path: field for field in COMPARED_FIELDS for path in field.paths
}

#: Every source-qualified path any comparison can name (SS2.4).
COMPARED_FIELD_PATHS: tuple[str, ...] = tuple(
    sorted({path for field in COMPARED_FIELDS for path in field.paths})
)


@dataclass(frozen=True)
class Comparison:
    """The outcome of one SS2.4 comparison under SS5.1 semantics."""

    logical: str
    verdict: str  # "ok" | "conflict" | "unchecked"
    reason: str | None  # SS5.8 reason, required iff verdict == "unchecked"
    left: str | None
    right: str | None

    @property
    def disagrees(self) -> bool:
        return self.verdict == "conflict"


def compare_field(field: ComparedField | str, left: Any, right: Any) -> Comparison:
    """Evaluate one comparison under SS5.1.

    A comparison is evaluated **only when both sides normalize to a non-`None`
    value**. `None` on either side yields `verdict='unchecked'` and is **never** a
    disagreement. The `None` causes are **three**, disjoint and exhaustive (SS5.1
    ruling 5, SS5.8):

    * `missing_operand`    -- the source value was **NULL**;
    * `unmapped_enum`      -- the value was **present**, the row is enum-mapped
      (`grade`, `stage`, `lifecycle`) and `norm_enum` could not map it;
    * `unparseable_value`  -- the value was **present**, the row is *not* enum-mapped
      (`name_first`, `name_last`, `dob`) and `norm_name`/`norm_dob` returned `None`.

    Which applies is a function of the **row** and of whether the source value was
    NULL -- never of a guess about the value's contents. When both operands are
    `None` the pinned precedence `UNCHECKED_REASON_PRECEDENCE` decides, so one
    comparison always emits exactly one reason.

    The precedence is **read from the constant**, not restated as an `if`. A constant
    that documents behaviour it does not drive is a trap: reordering it would then move
    the contract's claim without moving a single emitted reason.
    """
    row = COMPARED_FIELD_BY_LOGICAL[field] if isinstance(field, str) else field
    left_canon = row.left_mapper(left)
    right_canon = row.right_mapper(right)

    if left_canon is None or right_canon is None:
        # One candidate reason per side that failed to normalize; the pinned precedence
        # picks the one this comparison reports.
        candidates = {
            "missing_operand" if source is None else row.unmapped_reason
            for source, canonical in ((left, left_canon), (right, right_canon))
            if canonical is None
        }
        reason = next(code for code in UNCHECKED_REASON_PRECEDENCE if code in candidates)
        return Comparison(row.logical, "unchecked", reason, left_canon, right_canon)

    verdict = "ok" if left_canon == right_canon else "conflict"
    return Comparison(row.logical, verdict, None, left_canon, right_canon)


def compare_record(
    left_values: Mapping[str, Any], right_values: Mapping[str, Any]
) -> tuple[Comparison, ...]:
    """Run every SS2.4 comparison, keyed by source-qualified path on both sides."""
    return tuple(
        compare_field(row, left_values.get(row.left_path), right_values.get(row.right_path))
        for row in COMPARED_FIELDS
    )


def disagreeing_fields(comparisons: Iterable[Comparison]) -> tuple[str, ...]:
    """SS2.4: the **sorted set of both source-qualified paths of every disagreeing
    comparison**. Only `R-006`/`R-014` populate this."""
    paths: set[str] = set()
    for comparison in comparisons:
        if comparison.disagrees:
            paths.update(COMPARED_FIELD_BY_LOGICAL[comparison.logical].paths)
    return tuple(sorted(paths))


def conflict_type_for_paths(paths: Iterable[str]) -> str | None:
    """SS5.5/SS5.7(1): partition a disagreeing-path set into C14, C6, or nothing.

    Wholly sensitive and non-empty -> `C14`; any non-sensitive path -> `C6`; the
    empty set fires neither (SS5.5 C14: "The empty set never fires C14").
    """
    ordered = set(paths)
    if not ordered:
        return None
    return "C14" if ordered <= SENSITIVE_FIELDS else "C6"


# ======================================================================================
# SS4.1 Refs, persons, `person_key`  --  SS4.8 households
# ======================================================================================

#: SS4.1 -- every source ref class.
REF_CLASSES: tuple[str, ...] = (
    "crm:contact:",
    "crm:deal:",
    "appdb:student:",
    "appdb:enrollment:",
    "payments:payment:",
)

#: SS4.1 -- identity ref classes **in source-preference order**: prefer the earlier
#: class outright, break ties within a class by byte order. `payments:payment:` is an
#: identity ref only for a payment the cascade attributes to no person.
IDENTITY_REF_CLASSES: tuple[str, ...] = (
    "appdb:student:",
    "crm:contact:",
    "payments:payment:",
)

_REF_SOURCE: dict[str, str] = {
    "crm:contact:": "crm",
    "crm:deal:": "crm",
    "appdb:student:": "appdb",
    "appdb:enrollment:": "appdb",
    "payments:payment:": "payments",
}


#: SS5.4 -- the control characters a natural key may never contain. `\x1f` is the
#: fingerprint's intra-section joiner and `\x1e` is `canon_value`'s sequence joiner, so
#: a ref carrying either would be indistinguishable from two refs once the payload is
#: assembled. The whole C0 range is refused rather than just those two: no committed
#: natural key (`crm_id`, uuid, `pi_*`, `external_ref`) can contain one, so anything
#: that does is corrupt input, not data.
_CONTROL_CHARACTERS = frozenset(chr(code) for code in range(0x20))


def make_ref(source: str, entity_type: str, natural_key: Any) -> str:
    """Build a source ref `<source>:<entity_type>:<natural_key>` (SS3, SS4.1).

    A natural key containing a **control character** (`\\x00`-`\\x1f`) is refused. A ref
    is an element of SS5.4's fingerprint section 2, which is joined by `\\x1f`; a ref
    carrying a raw `\\x1f` would make two different conflicts hash to one fingerprint,
    and the fingerprint is the idempotency key the whole proposal pipeline (and R16's
    oscillation dedup) is keyed on. The payload escapes its elements as well, so this is
    the first of two independent guards, not the only one.
    """
    prefix = f"{source}:{entity_type}:"
    if prefix not in _REF_SOURCE:
        raise ValueError(f"unknown ref class {prefix!r}; expected one of {REF_CLASSES}")
    key = str(natural_key)
    if not key:
        raise ValueError(f"empty natural_key for ref class {prefix!r}")
    control = sorted(_CONTROL_CHARACTERS & set(key))
    if control:
        raise ValueError(
            f"natural_key for ref class {prefix!r} contains control character(s) "
            f"{[hex(ord(ch)) for ch in control]!r} (SS5.4); a ref may not carry one"
        )
    return f"{prefix}{key}"


def parse_ref(ref: str) -> tuple[str, str, str]:
    """Split a source ref into `(source, entity_type, natural_key)`."""
    source, _, rest = ref.partition(":")
    entity_type, _, natural_key = rest.partition(":")
    prefix = f"{source}:{entity_type}:"
    if prefix not in _REF_SOURCE or not natural_key:
        raise ValueError(f"malformed source ref {ref!r}")
    return source, entity_type, natural_key


def ref_source(ref: str) -> str:
    """The source id (`crm` | `appdb` | `payments`) a ref belongs to."""
    source, entity_type, _ = parse_ref(ref)
    return _REF_SOURCE[f"{source}:{entity_type}:"]


def is_identity_ref(ref: str, *, payment_attributed: bool = False) -> bool:
    """SS4.1: is `ref` an **identity** ref?

        appdb:student:      -> True   always
        crm:contact:        -> True   always
        payments:payment:   -> True   iff `payment_attributed` is False
        crm:deal:           -> False  always
        appdb:enrollment:   -> False  always

    SS4.1's payment clause is **scoped** -- a payment ref is an identity ref "only for a
    payment that the cascade attributes to **no** person" -- so the scope is an argument
    rather than an assumption baked into the ref string.

    The default is `payment_attributed=False` because the only place a payment ref
    legitimately appears *inside a person's ref set* is SS5.2's "each payment attributed
    to no person": a payment the cascade did attribute contributes its ref to that
    person as **evidence**, never as identity. `anchor_ref` therefore reads the default
    and is correct. Passing `True` is how a caller that knows the payment resolved says
    so -- which is what makes C2's and C11's payment refs (SS5.4) non-identity for the
    clean-sample probe. The flag scopes the payment class **only**; it can never make a
    student or contact ref non-identity.
    """
    if ref.startswith("payments:payment:"):
        return not payment_attributed
    return any(ref.startswith(prefix) for prefix in IDENTITY_REF_CLASSES)


def student_ref(student: Any) -> str:
    """`appdb:student:<id>` for an app-DB student record.

    A record with **no `id`** raises. It is not a student: a CRM contact carries
    `crm_id`, so `_field(contact, "id")` is `None` and `str(None)` would build the
    plausible-looking ref `appdb:student:None`, which sorts before every real student
    ref. That silently makes a *contact* the household anchor (SS4.8) the moment
    `household_members` output -- app-DB students **then** CRM contacts -- is handed to
    `household_anchor_student`, and the anchor decides the household's one `program`
    (`G29`). Failing loudly is the only safe behaviour.
    """
    identifier = _field(student, "id")
    if identifier is None:
        raise ValueError(
            "student_ref requires an app-DB student record with a non-null `id` "
            f"(SS4.8); got {student!r}"
        )
    return make_ref("appdb", "student", identifier)


def anchor_ref(refs: Iterable[str]) -> str:
    """SS4.1: the single lowest-sorted identity ref of a person.

    Source preference `appdb:student: > crm:contact: > payments:payment:` -- prefer
    the earlier class outright, break ties within a class by byte order.
    """
    candidates = [ref for ref in refs if is_identity_ref(ref)]
    if not candidates:
        raise ValueError("a person must carry at least one identity ref (SS4.1)")
    return min(
        candidates,
        key=lambda ref: (
            next(i for i, prefix in enumerate(IDENTITY_REF_CLASSES) if ref.startswith(prefix)),
            ref,
        ),
    )


def person_key(refs: Iterable[str]) -> uuid.UUID:
    """SS4.1: `uuid5(KEYSTONE_NS, anchor_ref(person))`; `canonical_id = person_key`.

    A **pure function of `anchor_ref`**, recomputed from the generation-N snapshot
    and never carried in state. Deliberately **not** a hash of the ref set: the ref
    set changes across generations and would split lineage.
    """
    return uuid.uuid5(KEYSTONE_NS, anchor_ref(refs))


def sources_involved(refs: Iterable[str]) -> tuple[str, ...]:
    """SS8: `sources_involved`, derived mechanically from the `entity_refs` prefixes."""
    return tuple(sorted({ref_source(ref) for ref in refs}))


def _field(record: Any, name: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def household_key(student: Any) -> str | None:
    """SS4.8: `norm_email(student.guardian_email)` -- the **PRIMARY** guardian email only.

    `guardian2_email` is corroborating evidence and is **never** part of the key;
    `appdb.student.household_id` never decides membership. Grouping is by **exact**
    key -- explicitly not transitive closure over shared addresses, never union-find.
    """
    return norm_email(_field(student, "guardian_email"))


def household_members_appdb(students: Iterable[Any]) -> dict[str, tuple[Any, ...]]:
    """SS4.8: app-DB students grouped by exact `household_key`.

    Students whose `household_key` is `None` form no household. Members are ordered
    by their `appdb:student:<id>` ref, so `[0]` is `household_anchor_student`.
    """
    grouped: dict[str, list[Any]] = {}
    for student in students:
        key = household_key(student)
        if key is None:
            continue
        grouped.setdefault(key, []).append(student)
    return {
        key: tuple(sorted(members, key=student_ref)) for key, members in sorted(grouped.items())
    }


def _contact_ref(contact: Any) -> str:
    """`crm:contact:<crm_id>` for a CRM contact record."""
    return make_ref("crm", "contact", _field(contact, "crm_id"))


def household_members(
    students: Iterable[Any], contacts: Iterable[Any] = ()
) -> dict[str, tuple[Any, ...]]:
    """SS4.8: `household_members_appdb(k)` union the CRM contacts whose `norm_email` is `k`.

    **Exported because SS4.8 defines it** (ruling 15). SS0's rule is that neither side
    may re-implement a shared symbol, and a symbol this contract defines but the module
    does not export is precisely the symbol a consumer re-implements -- the R23 drift
    the module exists to prevent.

    * **The key set is exactly the `household_key` values of the supplied STUDENTS.** A
      household is defined by app-DB students; a contact whose `norm_email` matches no
      student's `household_key` is a deal-less lead (SS11.4, `G11`) and belongs to no
      household. It never creates a key.
    * Members are **app-DB students first**, ascending by `appdb:student:<id>` -- so
      `[0]` is `household_anchor_student(k)`, identically to `household_members_appdb`
      -- **then** CRM contacts, ascending by `crm:contact:<crm_id>`. Both orderings are
      total, so the result never depends on input order.
    * One member per **contact record**, so a planted C3 duplicate pair contributes two
      contact members. This is a *record* view: `|household_members_appdb(k)|` -- never
      `|household_members(k)|` -- is what `P3`'s "exactly one child" and C8's "at least
      two children" evaluate (SS4.8), and mixing the two would let a C3 duplicate change
      a household's child count.
    """
    by_key = household_members_appdb(students)
    matched: dict[str, list[Any]] = {}
    for contact in contacts:
        key = norm_email(_field(contact, "email"))
        if key is None or key not in by_key:
            continue
        matched.setdefault(key, []).append(contact)
    return {
        key: (*members, *sorted(matched.get(key, ()), key=_contact_ref))
        for key, members in by_key.items()
    }


def household_anchor_student(members: Iterable[Any]) -> Any:
    """SS4.8: the member with the lexicographically smallest `appdb:student:<id>` ref.

    Ties are impossible -- student ids are unique. The household's **anchor
    enrollment** is this student's enrollment; it is what `G29` and SS1.2 mean by the
    household's one `program`.

    **Takes app-DB students only.** `household_members` returns students *then* CRM
    contacts, and a contact has no `id`; handing that whole tuple here raises (via
    `student_ref`) instead of returning the contact, which is what a `None` id sorting
    first used to do. The app-DB group is `household_members_appdb(k)` -- the same
    grouping SS4.8 says `P3`'s child count and C8's eligibility evaluate.
    """
    ordered = sorted(members, key=student_ref)
    if not ordered:
        raise ValueError("household_anchor_student requires at least one member (SS4.8)")
    return ordered[0]


# ======================================================================================
# SS5.4 entity_refs, observed_values keys, fingerprint
# ======================================================================================


@dataclass(frozen=True)
class RefSpec:
    """Which ref components a conflict type's `entity_refs` carries (SS5.5).

    A count of `None` means "one or more"; an int means exactly that many.
    """

    identity: int | None = 0
    enrollment: int | None = 0
    payment: int | None = 0
    contact: int | None = 0
    student: int | None = 0


#: SS5.5's `entity_refs` column, one row per conflict type.
REF_SPECS: TotalMap = TotalMap(
    "REF_SPECS",
    CONFLICT_TYPES,
    {
        "C1": RefSpec(identity=None),
        "C2": RefSpec(payment=1),
        "C3": RefSpec(contact=2),
        "C4": RefSpec(identity=None),
        "C5": RefSpec(identity=None),
        "C6": RefSpec(identity=None),
        "C7": RefSpec(identity=None, enrollment=1),
        "C8": RefSpec(identity=None),
        "C9": RefSpec(identity=None, enrollment=1),
        "C10": RefSpec(contact=1, student=2),
        "C11": RefSpec(payment=2),
        "C12": RefSpec(identity=None, payment=1),
        "C13": RefSpec(identity=None, payment=1, enrollment=1),
        "C14": RefSpec(identity=None),
    },
)

_COMPONENT_PREFIXES: dict[str, tuple[str, ...]] = {
    "identity": IDENTITY_REF_CLASSES,
    "enrollment": ("appdb:enrollment:",),
    "payment": ("payments:payment:",),
    "contact": ("crm:contact:",),
    "student": ("appdb:student:",),
}


def conflict_refs(
    conflict_type: str,
    *,
    identity_refs: Iterable[str] = (),
    enrollment_refs: Iterable[str] = (),
    payment_refs: Iterable[str] = (),
    contact_refs: Iterable[str] = (),
    student_refs: Iterable[str] = (),
) -> tuple[str, ...]:
    """Build one conflict's `entity_refs` (SS5.4, SS5.5) -- the single shared helper.

    Generator and detector build this list with **this** function: the generator
    never authors an `entity_refs` list (seeding is two-pass, `G31`), and the
    detector materializes conflicts through the same call. Returns the **sorted**
    set of refs; the per-type component spec is enforced, so an under- or
    over-specified conflict raises here instead of silently mismatching the harness
    key `(type, tuple(sorted(entity_refs)))`.
    """
    if conflict_type not in REF_SPECS:
        raise ValueError(f"unknown conflict type {conflict_type!r}")
    spec: RefSpec = REF_SPECS[conflict_type]

    supplied = {
        "identity": tuple(identity_refs),
        "enrollment": tuple(enrollment_refs),
        "payment": tuple(payment_refs),
        "contact": tuple(contact_refs),
        "student": tuple(student_refs),
    }

    collected: list[str] = []
    for component, refs in supplied.items():
        required = getattr(spec, component)
        unique = sorted(set(refs))
        if required == 0:
            if unique:
                raise ValueError(
                    f"{conflict_type} takes no {component} refs (SS5.5), got {unique!r}"
                )
            continue
        if required is None:
            if not unique:
                raise ValueError(f"{conflict_type} requires at least one {component} ref (SS5.5)")
        elif len(unique) != required:
            raise ValueError(
                f"{conflict_type} requires exactly {required} distinct {component} ref(s) "
                f"(SS5.5), got {len(unique)}"
            )
        prefixes = _COMPONENT_PREFIXES[component]
        for ref in unique:
            if not any(ref.startswith(prefix) for prefix in prefixes):
                raise ValueError(f"{ref!r} is not a valid {component} ref (expected {prefixes})")
        collected.extend(unique)

    return tuple(sorted(set(collected)))


#: SS5.4 -- `observed_values` keys are pinned per type. The fingerprint hashes the
#: map, so an unpinned key set is generator/detector drift with no check to catch it.
#: A key absent from a type's row may not be emitted; a key present in it is required.
#: C6/C14 are dynamic: one entry per disagreeing comparison, keyed by the
#: source-qualified path (SS2.4).
OBSERVED_VALUE_KEYS: TotalMap = TotalMap(
    "OBSERVED_VALUE_KEYS",
    CONFLICT_TYPES,
    {
        "C1": frozenset({"paid_payment_refs", "enrollment_ref", "d2_deal_count"}),
        "C2": frozenset({"payer_email_norm", "external_ref", "metadata_name_pair_present"}),
        "C3": frozenset({"email_norm", "first_norm", "last_norm", "dob_norm_a", "dob_norm_b"}),
        "C4": frozenset({"contact_email_norm", "student_guardian_email_norms", "link_method"}),
        "C5": frozenset({"status_funnel", "linked_contact_count", "attributed_payment_count"}),
        "C6": None,  # dynamic: source-qualified paths (SS2.4)
        "C7": frozenset(
            {"enrollment.stage_funnel", "enrollment.deposit_paid_at", "paid_deposit_payment_count"}
        ),
        "C8": frozenset({"household_key", "dropped_source", "eligible_member_count"}),
        "C9": frozenset({"enrollment.crm_deal_id", "deal_present_gen3", "deal_person_refs"}),
        "C10": frozenset(
            {"ext_resolved_ref", "namedob_resolved_ref", "first_norm", "last_norm", "dob_norm"}
        ),
        "C11": frozenset({"payer_email_norm", "amount_cents", "type", "occurred_at_delta_seconds"}),
        "C12": frozenset({"amount_cents", "expected_amount_cents", "program_norm", "type"}),
        "C13": frozenset(
            {"refunded_at", "enrollment.updated_at", "enrollment.stage_funnel", "student.status"}
        ),
        "C14": None,  # dynamic: source-qualified paths (SS2.4)
    },
)


def validate_observed_values(conflict_type: str, observed_values: Mapping[str, Any]) -> None:
    """SS5.4: enforce the pinned `observed_values` key set for a conflict type."""
    if conflict_type not in OBSERVED_VALUE_KEYS:
        raise ValueError(f"unknown conflict type {conflict_type!r}")
    expected = OBSERVED_VALUE_KEYS[conflict_type]
    keys = set(observed_values)
    if expected is None:
        unexpected = sorted(keys - set(COMPARED_FIELD_PATHS))
        if unexpected:
            raise ValueError(
                f"{conflict_type} observed_values keys must be COMPARED_FIELDS paths "
                f"(SS5.4); got {unexpected!r}"
            )
        return
    if keys != set(expected):
        missing = sorted(set(expected) - keys)
        extra = sorted(keys - set(expected))
        raise ValueError(
            f"{conflict_type} observed_values key set is pinned (SS5.4): "
            f"missing={missing!r} extra={extra!r}"
        )


def fingerprint(
    conflict_type: str,
    entity_refs: Iterable[str],
    disagreeing_fields: Iterable[str] = (),
    observed_values: Mapping[str, Any] | None = None,
) -> str:
    """SS5.4's conflict fingerprint -- one callable, no second code path.

        sha256(
            type
          | "\\x1f".join(sorted(canon_value(r) for r in entity_refs))
          | "\\x1f".join(sorted(canon_value(p) for p in disagreeing_fields))
          | "\\x1f".join(f"{k}={canon_value(v)}" for k, v in sorted(observed_values.items()))
        )

    The four sections are joined with a literal `|` (SS5.4 writes them that way and
    pins no other separator). `observed_values` is a **map**, its key set is pinned
    per type (SS5.4), and its values go through `canon_value` (SS2.5) -- so the hash
    contains no Python `repr`, no float, and nothing dependent on `PYTHONHASHSEED`.

    **Sections 2 and 3 go through `canon_value` too, and that is what makes the payload
    INJECTIVE.** Embedding a ref verbatim between `\\x1f` joiners is exactly the defect
    SS2.5 spells out for sequences: without escaping,

        fingerprint("C8", ["appdb:student:a\\x1fappdb:student:b"], ...)
        == fingerprint("C8", ["appdb:student:a", "appdb:student:b"], ...)

    -- one ref carrying the joiner and two separate refs produce the same bytes. Those
    are two different conflicts on two different populations sharing one fingerprint,
    and the fingerprint is the idempotency key R16's oscillation dedup and the whole
    proposal pipeline rest on: a collision there silently suppresses a real proposal.
    `make_ref` refuses a control character in a natural key, so a colliding ref should
    never be constructible in the first place; the escaping is the second, independent
    guard for a ref that reaches the hash without passing through `make_ref`.

    Sorting is over the **escaped** encodings, exactly as SS2.5's sequence case sorts
    over its escaped elements. No committed ref or `COMPARED_FIELDS` path contains a
    backslash, `\\x1f` or `\\x1e`, so escaped and raw order coincide for every value the
    contract can produce -- but only one of the two may be pinned, and it is this one.
    """
    values = dict(observed_values or {})
    validate_observed_values(conflict_type, values)

    payload = "|".join(
        (
            conflict_type,
            _UNIT_SEPARATOR.join(sorted(canon_value(ref) for ref in entity_refs)),
            _UNIT_SEPARATOR.join(sorted(canon_value(path) for path in disagreeing_fields)),
            _UNIT_SEPARATOR.join(
                f"{key}={canon_value(value)}" for key, value in sorted(values.items())
            ),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ======================================================================================
# SS5.7 PRECEDENCE -- ONE function, not a table two sides interpret
# ======================================================================================


@dataclass(frozen=True)
class PrecedenceRule:
    """One SS5.7 rule.

    `kind`:
      * `partition` -- rule 1, the C14/C6 sensitivity partition (co-occurrence only).
      * `suppress`  -- rules 2-8: a surviving `winner` entry suppresses `losers`
        whose `entity_refs` intersect the winner's (optionally filtered) refs.
      * `invariant` -- rules 9-11: properties of the output, not filtering actions.
    `excluded_from_compound` marks the **mechanical** suppressions (rule 2 and rules
    4-8) whose removed pairs never enter `compound_with` (SS5.7(10), `G32`).
    """

    index: int
    kind: str
    winner: str | None
    losers: frozenset[str]
    summary: str
    winner_ref_prefix: str | None = None
    excluded_from_compound: bool = False
    expected_fire_count: int | None = None


PRECEDENCE: tuple[PrecedenceRule, ...] = (
    PrecedenceRule(
        1,
        "partition",
        "C14",
        frozenset({"C6"}),
        "C14 over C6 -- a wholly sensitive disagreeing set is C14 and R-006 must not "
        "also emit C6; a mixed set emits C6 only, sensitive paths still listed.",
    ),
    PrecedenceRule(
        2,
        "suppress",
        "C10",
        frozenset({"C6", "C14", "C4"}),
        "C10 over C6/C14/C4 -- suppressed for any conflict whose entity_refs contain "
        "the collapsed contact ref.",
        winner_ref_prefix="crm:contact:",
        excluded_from_compound=True,
    ),
    PrecedenceRule(
        3,
        "suppress",
        "C2",
        frozenset({"C12", "C11"}),
        "C2 over C12/C11 -- an unattributable payment cannot have a wrong amount or a "
        "duplicate partner.",
    ),
    PrecedenceRule(
        4,
        "suppress",
        "C5",
        frozenset({"C1", "C7"}),
        "C5 over C1/C7 -- a single-source student cannot also be paid-but-no-deal. The "
        "C5-over-C7 half is live (400 plants, G38); the C5-over-C1 half is vacuous.",
        excluded_from_compound=True,
    ),
    PrecedenceRule(
        5,
        "suppress",
        "C13",
        frozenset({"C7"}),
        "C13 over C7 -- a refunded-only payer whose enrollment still reads paid-implying "
        "is C13; R-007 must not also emit C7.",
        excluded_from_compound=True,
    ),
    PrecedenceRule(
        6,
        "suppress",
        "C9",
        frozenset({"C1"}),
        "C9 over C1 -- vacuous under G9 and SS4.5's D2-only link rule; retained for "
        "defence in depth and asserted to fire zero times.",
        excluded_from_compound=True,
        expected_fire_count=0,
    ),
    PrecedenceRule(
        7,
        "suppress",
        "C10",
        frozenset({"C5"}),
        "C10 over C5 -- defensive only; under G21 no student is contact-less, so this "
        "is asserted to fire zero times.",
        excluded_from_compound=True,
        expected_fire_count=0,
    ),
    PrecedenceRule(
        8,
        "suppress",
        "C8",
        frozenset({"C1", "C7"}),
        "C8 over C1/C7 -- the dropped child of a detected C8 is covered by C8; R-001 "
        "and R-007 must not also fire on that child.",
        excluded_from_compound=True,
    ),
    PrecedenceRule(
        9,
        "invariant",
        None,
        frozenset(),
        "C3 does not suppress C6, and C14 does not co-occur with C6 (rule 1). All "
        "remaining ordered pairs co-occur freely.",
    ),
    PrecedenceRule(
        10,
        "invariant",
        None,
        frozenset(),
        "golden/conflicts.json is written through this same filter; the >=10% compound "
        "ratio counts only surviving entries, and pairs removed by rules 2 and 4-8 "
        "never appear in compound_with.",
    ),
    PrecedenceRule(
        11,
        "invariant",
        None,
        frozenset(),
        "(type, tuple(sorted(entity_refs))) is UNIQUE across golden/conflicts.json.",
    ),
)

#: SS5.7(10) -- the mechanical suppressions whose removals never enter `compound_with`.
MECHANICAL_SUPPRESSIONS: tuple[int, ...] = tuple(
    rule.index for rule in PRECEDENCE if rule.excluded_from_compound
)


def _entry_type(entry: Any) -> str:
    value = _field(entry, "type")
    if value is None:
        raise ValueError(f"conflict entry has no `type`: {entry!r}")
    return str(value)


def _entry_refs(entry: Any) -> frozenset[str]:
    refs = _field(entry, "entity_refs")
    if refs is None:
        raise ValueError(f"conflict entry has no `entity_refs`: {entry!r}")
    return frozenset(refs)


def _entry_paths(entry: Any) -> frozenset[str]:
    paths = _field(entry, "disagreeing_fields") or ()
    return frozenset(paths)


def _entry_sort_key(entry: Any) -> tuple[str, tuple[str, ...]]:
    return (_entry_type(entry), tuple(sorted(_entry_refs(entry))))


def conflict_key(entry: Any) -> tuple[str, tuple[str, ...]]:
    """SS5.4/SS5.7(11): `(type, tuple(sorted(entity_refs)))` -- the harness match key."""
    return _entry_sort_key(entry)


def assert_unique_conflict_keys(entries: Sequence[Any]) -> None:
    """SS5.7(11): fail loudly on a duplicate `(type, sorted(entity_refs))` key.

    The manifest self-check calls this rather than letting the harness loader dedupe
    a duplicate silently.
    """
    seen: dict[tuple[str, tuple[str, ...]], int] = {}
    duplicates: list[tuple[str, tuple[str, ...]]] = []
    for entry in entries:
        key = conflict_key(entry)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] == 2:
            duplicates.append(key)
    if duplicates:
        raise ValueError(f"duplicate conflict keys (SS5.7 rule 11): {sorted(duplicates)!r}")


def _apply_partition(entries: list[Any]) -> list[Any]:
    """Rule 1: C14 over C6, on co-occurrence for one person.

    Grouped by the entry's exact ref set, because C6 and C14 both carry the person's
    identity refs (SS5.5). A wholly sensitive disagreeing set keeps the C14 and drops
    the C6; a mixed set keeps the C6 and drops the C14 ("mixed sets emit C6 only").

    Deliberately conservative: a *lone* C6 or C14 is never dropped. Rule 1 says
    R-006 "must not **also** emit C6" -- removing a lone entry would convert a
    detected conflict into a false negative with nothing standing in its place.
    """
    grouped: dict[frozenset[str], list[Any]] = {}
    for entry in entries:
        if _entry_type(entry) in {"C6", "C14"}:
            grouped.setdefault(_entry_refs(entry), []).append(entry)

    dropped: list[int] = []
    for group in grouped.values():
        types = {_entry_type(entry) for entry in group}
        if types != {"C6", "C14"}:
            continue
        paths: set[str] = set()
        for entry in group:
            paths |= _entry_paths(entry)
        keep = conflict_type_for_paths(paths) or "C6"
        dropped.extend(id(entry) for entry in group if _entry_type(entry) != keep)

    dropped_ids = set(dropped)
    return [entry for entry in entries if id(entry) not in dropped_ids]


def _apply_suppression(entries: list[Any], rule: PrecedenceRule) -> tuple[list[Any], int]:
    winner_refs: set[str] = set()
    for entry in entries:
        if _entry_type(entry) != rule.winner:
            continue
        refs = _entry_refs(entry)
        if rule.winner_ref_prefix is not None:
            refs = frozenset(ref for ref in refs if ref.startswith(rule.winner_ref_prefix))
        winner_refs |= refs

    if not winner_refs:
        return entries, 0

    survivors: list[Any] = []
    fired = 0
    for entry in entries:
        if _entry_type(entry) in rule.losers and _entry_refs(entry) & winner_refs:
            fired += 1
            continue
        survivors.append(entry)
    return survivors, fired


def apply_precedence(entries: Iterable[Any], *, report: dict[int, int] | None = None) -> list[Any]:
    """Apply the SS5.7 `PRECEDENCE` filter and return the surviving entries.

    **This is the only implementation.** The generator calls it before writing
    `golden/`, and the detector calls the same function before materialising
    conflicts (SS5.7(10), `G32`). The contract suppresses C7 from a raw population of
    875 down to 300 through three separate rules; two slightly different filters
    would be up to 575 false positives against a golden count of 300.

    Entries may be mappings (`{"type": ..., "entity_refs": [...], ...}`) or objects
    with those attributes; the surviving objects are returned **unmodified and by
    identity**. Output is sorted by `(type, tuple(sorted(entity_refs)))` -- SS8's
    order for `golden/conflicts.json` -- so the result is independent of input order.

    Pass `report` to receive `{rule_index: times_fired}` (used by
    `sc_construction_sweep`, which asserts rules 6 and 7 fire zero times).
    """
    surviving = list(entries)
    for rule in PRECEDENCE:
        if rule.kind == "partition":
            before = len(surviving)
            surviving = _apply_partition(surviving)
            if report is not None:
                report[rule.index] = before - len(surviving)
        elif rule.kind == "suppress":
            surviving, fired = _apply_suppression(surviving, rule)
            if report is not None:
                report[rule.index] = fired
    return sorted(surviving, key=_entry_sort_key)
