"""Normalization + match keys -- THE shared spec (R23, contract v2 SS0, SS2.1, SS2.3).

Both the seed generator (`recon.seed`) and the detector (`recon.er`, the invariant
runner, `recon.suite`) import this module. **Neither side may re-implement anything
defined here.** If a value can be computed two ways the build has a silent
divergence and the exact-match grade is lost.

Layering rule (one direction only, no cycles):

    recon.normalize  ->  (nothing in `recon`)
    recon.reference  ->  recon.normalize

So the *canonical vocabularies* and the dirty-variant tables that `norm_enum`
is driven by live here (SS2.1 pins `norm_enum` in this module and says it is
"table-driven from SS2.3"), while the *crosswalks* built on top of those
vocabularies -- `GRADE_ORDER`, `DEAL_STAGE_TO_FUNNEL`, `STATUS_TO_FUNNEL`,
`LIFECYCLE_TO_FUNNEL`, the fee schedule -- live in `recon/reference.py`, which
re-exports the vocabularies rather than restating them.

Normalization is materialized upstream by Python into `stg_*` columns; SQL rules
never normalize (SS2 preamble).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any, NamedTuple

__all__ = [
    "DEAL_STAGE_VALUES",
    "ENUM_FIELDS",
    "GMAIL_DOMAINS",
    "GRADE_VALUES",
    "KEY_CLASSES",
    "LIFECYCLE_VALUES",
    "PROGRAM_VALUES",
    "QUOTE_CHARS",
    "STAGE_FUNNEL_VALUES",
    "STATE_VALUES",
    "STATUS_VALUES",
    "MatchKey",
    "enum_values",
    "match_keys",
    "norm_dob",
    "norm_email",
    "norm_enum",
    "norm_name",
]


# --------------------------------------------------------------------------------------
# SS2.1 primitives
# --------------------------------------------------------------------------------------

#: The only two domains that get local-part canonicalization (SS2.1, `G4`).
#: Universal dot-stripping collapses legitimately distinct addresses and would
#: false-positive against the clean majority, so it is scoped to exactly these.
GMAIL_DOMAINS: frozenset[str] = frozenset({"gmail.com", "googlemail.com"})

#: SS2.1 (ruling 7) -- the committed A.3 quote-dirt set: exactly these **seven**
#: characters, in this committed order. The four curly quotes are part of the
#: committed set, not an implementer's addition: A.3 sprinkles typographic quotes
#: through the CRM export and a three-character set leaves `'Maria'` un-normalized.
QUOTE_CHARS: str = "\"'`‘’“”"  # noqa: RUF001 - curly quotes ARE the A.3 dirt

_WHITESPACE_RUN = re.compile(r"\s+")
_DOB_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
#: Variant key folding for enums: casefold and drop `_`, `-` and whitespace (SS2.3).
_ENUM_SEPARATORS = re.compile(r"[\s_\-]+")


def norm_email(value: str | None) -> str | None:
    """Canonicalize an email address (SS2.1).

    Trim, drop *surrounding* quotes/backticks, casefold. **Only** for `gmail.com`
    and `googlemail.com`: truncate the local part at `+` and remove `.` from it.
    Every other domain is byte-preserved apart from trim/casefold/quote-stripping --
    `a.b@corp.com` and `ab@corp.com` are different people and must stay different.

    **Quote handling is SURROUNDING-ONLY, and is deliberately asymmetric with
    `norm_name` (SS2.1 ruling 6).** `norm_name` removes `QUOTE_CHARS` *anywhere*, so
    `O'Brien` and `OBrien` are one name; `norm_email` strips them from the ends
    *only*, so `o'brien@corp.com` and `obrien@corp.com` stay two mailboxes. Removing
    a quote from the interior of an address is the same false-positive class as
    universal dot-stripping: it collapses distinct mailboxes of distinct people
    against the clean majority. A name is a spelling of one identity; an address is a
    routing key. Do not "make these consistent".

    A value with **no `@`** (SS2.1 ruling 14) gets trim / surrounding-quote strip /
    casefold and **nothing else** -- the gmail local-part rules are never applied to a
    value with no domain to scope them to, and the value is never repaired into an
    address.

    Returns `None` for `None` and for input that is empty once trimmed, so that a
    NULL `guardian2_email` stays NULL rather than becoming `""` and colliding.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"norm_email expects str | None, got {type(value).__name__}")

    text = value.strip().strip(QUOTE_CHARS).strip().casefold()
    if not text:
        return None
    if "@" not in text:
        # Not addressable; preserved verbatim (trim/casefold only) rather than guessed at.
        return text

    local, _, domain = text.rpartition("@")
    if domain in GMAIL_DOMAINS:
        local = local.split("+", 1)[0]
        local = local.replace(".", "")
    return f"{local}@{domain}"


def norm_name(value: str | None) -> str | None:
    """Canonicalize a personal name (SS2.1).

    Casefold, NFKD-fold accents (drop combining marks), remove every `QUOTE_CHARS`
    character **wherever it occurs**, collapse internal whitespace, trim. **Never**
    merges different spellings: `Jon` != `John`.

    Quote removal is deliberately *anywhere*, not surrounding-only (SS2.1 ruling 6):
    `O'Brien` and `OBrien` are one person spelled two ways and must link. The mirror
    rule in `norm_email` is surrounding-only for exactly the opposite reason -- see
    its docstring. The asymmetry is load-bearing.

    The second casefold is not redundant: NFKD of a compatibility character can
    yield upper-case output (`U+3392` -> `MHz`), which would otherwise break
    idempotence.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"norm_name expects str | None, got {type(value).__name__}")

    text = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = text.translate({ord(ch): None for ch in QUOTE_CHARS})
    text = _WHITESPACE_RUN.sub(" ", text).strip()
    return text or None


def norm_dob(value: str | date | datetime | None) -> str | None:
    """Canonicalize a date of birth to `YYYY-MM-DD`, else `None` (SS2.1).

    Never raises: an unparseable value is `None`, which SS5.1 turns into
    `verdict='unchecked'` -- never a disagreement.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None

    text = value.strip().strip(QUOTE_CHARS).strip()
    if not _DOB_SHAPE.match(text):
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# SS2.3 canonical vocabularies + dirty-variant tables (the data `norm_enum` is driven by)
# --------------------------------------------------------------------------------------

GRADE_VALUES: tuple[str, ...] = (
    "PK",
    "K",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "10",
    "11",
    "12",
)

PROGRAM_VALUES: tuple[str, ...] = (
    "Lower School",
    "Middle School",
    "Upper School",
    "Summer Academy",
)

STAGE_FUNNEL_VALUES: tuple[str, ...] = (
    "prospect",
    "applied",
    "waitlisted",
    "deposit_paid",
    "enrolled",
    "withdrawn",
    "refunded",
)

DEAL_STAGE_VALUES: tuple[str, ...] = (
    "New Lead",
    "Application Submitted",
    "Waitlisted",
    "Deposit Received",
    "Closed Won",
    "Closed Lost",
    "Refunded",
)

STATUS_VALUES: tuple[str, ...] = (
    "prospect",
    "applied",
    "enrolled",
    "active",
    "withdrawn",
)

#: SS2.3 `LIFECYCLE_TO_FUNNEL` domain, verbatim and case-significant: `MQL` and
#: `marketingqualifiedlead` are two committed vocabulary entries, not one.
LIFECYCLE_VALUES: tuple[str, ...] = (
    "subscriber",
    "lead",
    "marketingqualifiedlead",
    "MQL",
    "salesqualifiedlead",
    "SQL",
    "opportunity",
    "customer",
    "evangelist",
    "other",
)

#: SS2.3 (ruling 12) -- **exactly the 50 states**, USPS codes. No `DC`, no territory
#: (`PR`/`GU`/`VI`/`AS`/`MP`), no military `AA`/`AE`/`AP`, no Canadian province. The
#: generator must not emit any value outside this set: `crm.contact.state` exists
#: solely to exercise a committed normalizer under unit test (SS1.1 reason (c)), and an
#: emitted `DC` would normalize to `None` and make that its only untestable field.
STATE_VALUES: tuple[str, ...] = (
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
)

_STATE_NAMES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

_GRADE_ORDINAL_SUFFIX: dict[str, str] = {
    "1": "1st",
    "2": "2nd",
    "3": "3rd",
    "4": "4th",
    "5": "5th",
    "6": "6th",
    "7": "7th",
    "8": "8th",
    "9": "9th",
    "10": "10th",
    "11": "11th",
    "12": "12th",
}

_GRADE_ORDINAL_WORD: dict[str, str] = {
    "1": "first",
    "2": "second",
    "3": "third",
    "4": "fourth",
    "5": "fifth",
    "6": "sixth",
    "7": "seventh",
    "8": "eighth",
    "9": "ninth",
    "10": "tenth",
    "11": "eleventh",
    "12": "twelfth",
}


def _variant_key(value: str) -> str:
    """Fold a raw enum value to its lookup key: casefold, drop `_`/`-`/whitespace."""
    text = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().translate({ord(ch): None for ch in QUOTE_CHARS})
    return _ENUM_SEPARATORS.sub("", text).strip()


def _build_variants(pairs: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """Build a variant->canonical table, refusing (at import) to be ambiguous."""
    table: dict[str, str] = {}
    for canonical, variants in pairs.items():
        for variant in (canonical, *variants):
            key = _variant_key(variant)
            if not key:
                raise ValueError(f"empty variant key for canonical {canonical!r}")
            existing = table.get(key)
            if existing is not None and existing != canonical:
                raise ValueError(
                    f"ambiguous enum variant {key!r}: maps to both {existing!r} and {canonical!r}"
                )
            table[key] = canonical
    return table


def _grade_variants() -> dict[str, tuple[str, ...]]:
    """The CLOSED committed grade variant families (SS2.3 ruling 11).

    SS2.3's eight examples are examples; **this** is the committed set and the
    generator may draw grade dirt from nowhere else. A well-formed grade outside it
    (`"Yr 4"`, `"Form IV"`) normalizes to `None`, which makes the `grade` comparison
    `unchecked` (SS5.1) rather than a comparison -- a planted C6 would become an
    unchecked non-event and the golden entry a silent false negative.

    Separators are deleted by `_variant_key`, so `Grade 4`, `grade4`, `GRADE-4` and
    `grade_4` are one variant key rather than four rows.
    """
    pairs: dict[str, tuple[str, ...]] = {
        "PK": ("Pre-K", "PreK", "Pre K", "Pre-Kindergarten", "Prekindergarten"),
        "K": ("KG", "Kindergarten", "Grade K"),
    }
    for value in GRADE_VALUES:
        if value in pairs:
            continue
        suffix = _GRADE_ORDINAL_SUFFIX[value]
        word = _GRADE_ORDINAL_WORD[value]
        pairs[value] = (
            f"Grade {value}",
            suffix,
            word,
            f"Grade {suffix}",
            f"Grade {word}",
        )
    return pairs


_ENUM_VARIANTS: dict[str, dict[str, str]] = {
    "grade": _build_variants(_grade_variants()),
    "state": _build_variants({code: (_STATE_NAMES[code],) for code in STATE_VALUES}),
    "program": _build_variants({value: () for value in PROGRAM_VALUES}),
    # SS2.3: `deal.pipeline` is "the same four values with the same dirty-variant
    # tolerance as `program`" -- one table, two field names, never two tables.
    "pipeline": _build_variants({value: () for value in PROGRAM_VALUES}),
    "stage": _build_variants({value: () for value in STAGE_FUNNEL_VALUES}),
    "deal_stage": _build_variants({value: () for value in DEAL_STAGE_VALUES}),
    "status": _build_variants({value: () for value in STATUS_VALUES}),
    "lifecycle_stage": _build_variants({value: () for value in LIFECYCLE_VALUES}),
}

_ENUM_CANONICAL: dict[str, tuple[str, ...]] = {
    "grade": GRADE_VALUES,
    "state": STATE_VALUES,
    "program": PROGRAM_VALUES,
    "pipeline": PROGRAM_VALUES,
    "stage": STAGE_FUNNEL_VALUES,
    "deal_stage": DEAL_STAGE_VALUES,
    "status": STATUS_VALUES,
    "lifecycle_stage": LIFECYCLE_VALUES,
}

#: Every field name `norm_enum` accepts.
ENUM_FIELDS: tuple[str, ...] = tuple(sorted(_ENUM_CANONICAL))


def enum_values(field: str) -> tuple[str, ...]:
    """The canonical vocabulary of `field`, in committed order."""
    try:
        return _ENUM_CANONICAL[field]
    except KeyError:
        raise ValueError(f"unknown enum field {field!r}; expected one of {ENUM_FIELDS}") from None


def norm_enum(field: str, value: str | None) -> str | None:
    """Map a dirty enum value onto its canonical form, or `None` (SS2.1, SS2.3).

    Returns `None` -- **never raises** -- for `None` and for a well-formed but
    unmappable value. That path becomes `verdict='unchecked'` with
    `detail.reason='unmapped_enum'` (SS5.1, SS5.8), never a conflict and never a
    crash. *Structural* breakage is a different thing entirely and is handled at
    the adapter boundary (SS7).

    An unknown `field` **does** raise: that is a programming error in the caller,
    not dirty data.
    """
    variants = _ENUM_VARIANTS.get(field)
    if variants is None:
        raise ValueError(f"unknown enum field {field!r}; expected one of {ENUM_FIELDS}")
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    key = _variant_key(value)
    if not key:
        return None
    return variants.get(key)


# --------------------------------------------------------------------------------------
# SS2.1 match keys
# --------------------------------------------------------------------------------------


class MatchKey(NamedTuple):
    """One blocking key. Candidates only -- **never** an automatic merge (SS2.1)."""

    key_class: str
    value: Any


#: SS4.7 `entity_link_candidates.key_class`, in the order `match_keys` emits them.
#: **Exported** (SS2.1 ruling 15): an unexported shared symbol is one a consumer
#: re-implements, which is the R23 drift this module exists to prevent.
KEY_CLASSES: tuple[str, ...] = ("ext", "email", "namedob")

_ENTITY_TYPES: tuple[str, ...] = ("crm.contact", "appdb.student", "payments.payment")


def _get(entity: Any, name: str) -> Any:
    if isinstance(entity, dict):
        return entity.get(name)
    return getattr(entity, name, None)


def _infer_entity_type(entity: Any) -> str:
    if _get(entity, "crm_id") is not None:
        return "crm.contact"
    if _get(entity, "payment_id") is not None:
        return "payments.payment"
    if _get(entity, "guardian_email") is not None or _get(entity, "student_number") is not None:
        return "appdb.student"
    raise ValueError(
        "cannot infer entity type for match_keys; pass entity_type explicitly "
        f"(one of {_ENTITY_TYPES})"
    )


def match_keys(entity: Any, entity_type: str | None = None) -> tuple[MatchKey, ...]:
    """Ordered, deterministic blocking keys for one record (SS2.1, SS4.7).

    Order is pinned: `("ext", <hard id>)`, then `("email", norm_email(...))`, then
    `("namedob", (first_norm, last_norm, dob_norm))`. A key is omitted when its
    inputs are absent. An app-DB student emits its `guardian_email` key before its
    `guardian2_email` key (`L2` matches a contact against either -- SS4.2); the
    **household** key is the primary address only and lives in `reference.py`.

    **No `namedob` key is emitted unless `first_norm`, `last_norm` AND `dob_norm`
    are all non-`None`** (SS2.1 ruling 10). A `(first, last, None)` key is never
    emitted in any shape on any entity. `L3` requires both DOBs non-null, so a
    partial key could only manufacture candidate pairs no cascade rule is allowed to
    accept -- and `R-010` (C10), which is evaluated over `entity_link_candidates`
    (SS4.7), would then see a `namedob` resolution no link rule could have made.
    The consequence is intended: a record with a missing or unparseable DOB carries
    **no** `key_class='namedob'` row and is reachable only by `ext` or `email`.

    Duplicate keys are dropped, preserving first-seen order.
    """
    kind = entity_type or _infer_entity_type(entity)
    if kind not in _ENTITY_TYPES:
        raise ValueError(f"unknown entity_type {kind!r}; expected one of {_ENTITY_TYPES}")

    keys: list[MatchKey] = []

    if kind == "crm.contact":
        hard_id = _get(entity, "external_id")
        emails = (_get(entity, "email"),)
        first, last = _get(entity, "first_name"), _get(entity, "last_name")
        dob = _get(entity, "dob")
    elif kind == "appdb.student":
        hard_id = _get(entity, "id")
        emails = (_get(entity, "guardian_email"), _get(entity, "guardian2_email"))
        first, last = _get(entity, "first_name"), _get(entity, "last_name")
        dob = _get(entity, "dob")
    else:  # payments.payment
        hard_id = _get(entity, "external_ref")
        emails = (_get(entity, "payer_email"),)
        metadata = _get(entity, "metadata") or {}
        first = _get(metadata, "student_first_name")
        last = _get(metadata, "student_last_name")
        dob = None

    if hard_id is not None and str(hard_id) != "":
        keys.append(MatchKey("ext", str(hard_id)))

    for raw_email in emails:
        normalized = norm_email(raw_email)
        if normalized is not None:
            keys.append(MatchKey("email", normalized))

    first_norm, last_norm, dob_norm = norm_name(first), norm_name(last), norm_dob(dob)
    if first_norm is not None and last_norm is not None and dob_norm is not None:
        keys.append(MatchKey("namedob", (first_norm, last_norm, dob_norm)))

    seen: set[MatchKey] = set()
    ordered: list[MatchKey] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return tuple(ordered)
