"""Pydantic v2 models for the five source entities -- contract SS1, verbatim (R2).

Two rules decide every field here, and both come from the contract rather than
from the fixtures:

**Required means non-nullable in SS1, not "present in the fixtures".** SS1 marks
added fields with `[+]` and marks nullable ones `| null`. A field the contract
allows to be null or absent is optional here even when the committed generator
happens to always emit it, because the port has to accept a *conforming* source,
not only this one. That is also what makes the malformed corpus classify the way
it was built to: `MAL-010` is a `wrong_scalar_type` case, and it would have been
reported as `missing_required_field` if `[+] metadata` had been made mandatory.

**Scalars are strict.** Pydantic's lax mode coerces `"50000"` into `50000`, which
would let `MAL-010` -- a payment whose `amount_cents` arrives as a string --
ingest silently. Every scalar is therefore a `Strict*` type. Strictness is applied
per field rather than through `model_config`, so nested models still accept the
plain dicts a JSON body is made of.

`extra="allow"`: SS1 says "You may add fields; you may not remove these". A source
that grows a column is not malformed, and the landing table keeps the whole
payload regardless -- inventing a rejection the contract does not mandate would be
exactly the "reject a well-formed record" failure SS7 warns about for enums.

**A type this layer accepts but the store cannot hold is a latent 500.** That is
the third rule, and it is the reason the annotated types below exist. `StrictStr`
on `created_at` accepts ``"x"``; the staging column is `timestamptz`, so the
payload validates, lands in the COPY buffer and blows up inside psycopg -- which
reaches the client as a 500 and reaches `ingest_source` as a raw
`InvalidDatetimeFormat`. R2 forbids exactly that: a payload the pipeline cannot
store must be refused *here*, as a structured 4xx, like every other malformed
shape.

So every field is bounded by the type of the column it ends up in
(migrations 0001/0002), never by an invented business rule:

============================  ==========================  =========================
field                         destination column          bound applied here
============================  ==========================  =========================
`created_at` and siblings     `timestamptz`               parses as ISO-8601
`enrollment_year`             `integer` (int4)            fits int4
`payment.amount_cents`        `bigint`, and the stored    fits bigint // 10_000
                              generated `amount_cents
                              * 10000`
`deal.amount`                 `bigint amount_cents` via   finite, and
                              `round(amount * 100)`       `round(amount*100)` fits
============================  ==========================  =========================

Two hostile shapes are *not* fixable per field and are handled in
`recon.adapters.validation` instead, because `extra="allow"` means they can ride
in on a field no model names: a non-finite JSON number (`NaN`, `Infinity`), which
`json.dumps` re-emits as invalid JSON and `jsonb` refuses, and text Postgres
cannot represent (a NUL, or an unpaired surrogate).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
)

__all__ = [
    "ENTITY_MODELS",
    "INT32_MAX",
    "INT32_MIN",
    "MAX_AMOUNT_CENTS",
    "PRIMARY_KEYS",
    "SOURCE_ENTITY_TYPES",
    "TIMESTAMP_FIELDS",
    "AppDbEnrollment",
    "AppDbStudent",
    "CrmContact",
    "CrmDeal",
    "Payment",
    "PaymentMetadata",
    "model_for",
    "primary_key_field",
]

_CONFIG = ConfigDict(extra="allow")

#: Postgres `integer`. `raw_records.generation`, `stg_student.enrollment_year`.
INT32_MIN: int = -(2**31)
INT32_MAX: int = 2**31 - 1

#: Postgres `bigint`. Every `amount_cents` column carries a *stored generated*
#: sibling `amount_microusd = amount_cents * 10000` (migration 0002), so the
#: storable range is the bigint range divided by 10_000 -- a value that fits
#: `amount_cents` but overflows the generated column is still a failed INSERT.
MAX_AMOUNT_CENTS: int = (2**63 - 1) // 10_000
MIN_AMOUNT_CENTS: int = -MAX_AMOUNT_CENTS

#: Per entity, the fields that land in a `timestamptz` column. Named here so a
#: test can enumerate them instead of restating the list and drifting from it.
TIMESTAMP_FIELDS: dict[tuple[str, str], tuple[str, ...]] = {
    ("crm", "contact"): ("created_at", "updated_at"),
    ("crm", "deal"): ("created_at", "updated_at"),
    ("appdb", "student"): ("created_at", "updated_at"),
    ("appdb", "enrollment"): ("deposit_paid_at", "created_at", "updated_at"),
    ("payments", "payment"): ("occurred_at", "refunded_at"),
}


def _iso_timestamp(value: str) -> str:
    """Reject a timestamp string Postgres could not parse.

    SS1 types these fields "ISO-8601 Z", and `datetime.fromisoformat` is the
    stdlib's reader for exactly that grammar (it takes the trailing ``Z`` from
    3.11 on). The value is returned **unchanged** -- staging stores the source's
    own bytes and lets Postgres parse them; this only decides whether it *can* be
    parsed, so no normalization is introduced here and none can drift.
    """
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(
            f"{value!r} is not an ISO-8601 timestamp; this field is stored as "
            "timestamptz, so an unparseable value is a rejected payload, never a "
            "failed write"
        ) from None
    return value


def _int32(value: int) -> int:
    """Reject an integer that does not fit Postgres `integer`."""
    if not INT32_MIN <= value <= INT32_MAX:
        raise ValueError(
            f"{value} does not fit a Postgres integer ({INT32_MIN}..{INT32_MAX}); "
            "an out-of-range value is a rejected payload, never a failed write"
        )
    return value


def _amount_cents(value: int) -> int:
    """Reject cents that overflow `bigint` or its generated `* 10000` sibling."""
    if not MIN_AMOUNT_CENTS <= value <= MAX_AMOUNT_CENTS:
        raise ValueError(
            f"{value} does not fit the storable cents range "
            f"({MIN_AMOUNT_CENTS}..{MAX_AMOUNT_CENTS}); the staging table also "
            "stores amount_cents * 10000 as a bigint"
        )
    return value


def _deal_amount(value: float) -> float:
    """Reject a dollar amount that cannot become storable cents.

    `stg_crm_deal.amount_cents` is `Money.from_dollars(amount).cents`, i.e.
    ``round(amount * 100)``. On an infinity that call raises `OverflowError` and on
    a NaN it raises `ValueError`, both of which surface as a 500 from inside the
    COPY generator; on a merely enormous float it produces an int no bigint can
    hold. All three are the same defect and are refused here.
    """
    if not math.isfinite(value):
        raise ValueError(
            f"{value!r} is not a finite amount; SS1.2's amount becomes integer "
            "cents via round(amount * 100), which is undefined here"
        )
    scaled = value * 100
    # `value` can be finite while `value * 100` overflows to inf, and `round(inf)`
    # raises OverflowError -- which is not a ValueError and would escape pydantic
    # as a 500 rather than a rejection.
    cents = round(scaled) if math.isfinite(scaled) else None
    if cents is None or not MIN_AMOUNT_CENTS <= cents <= MAX_AMOUNT_CENTS:
        raise ValueError(
            f"{value!r} dollars is outside the storable cents range "
            f"({MIN_AMOUNT_CENTS}..{MAX_AMOUNT_CENTS})"
        )
    return value


#: A source timestamp string, verified parseable before it reaches `timestamptz`.
Timestamp = Annotated[StrictStr, AfterValidator(_iso_timestamp)]
#: An integer bound by the Postgres `integer` column it lands in.
Int32 = Annotated[StrictInt, AfterValidator(_int32)]
#: Integer cents bound by `bigint` *and* by the generated `amount_cents * 10000`.
AmountCents = Annotated[StrictInt, AfterValidator(_amount_cents)]
#: SS1.2's dollars-as-float, bound by the cents it is converted into.
DealAmount = Annotated[StrictFloat | StrictInt, AfterValidator(_deal_amount)]


class CrmContact(BaseModel):
    """SS1.1 -- HubSpot-shaped contact. `crm_id` is the PK and the survivorship tiebreak."""

    model_config = _CONFIG

    crm_id: StrictStr
    email: StrictStr
    first_name: StrictStr
    last_name: StrictStr
    lifecycle_stage: StrictStr
    created_at: Timestamp
    updated_at: Timestamp
    external_id: StrictStr | None = None
    dob: StrictStr | None = None
    grade: StrictStr | None = None
    state: StrictStr | None = None
    marketing_consent: StrictBool | None = None


class CrmDeal(BaseModel):
    """SS1.2 -- a deal is per household; `associated_contact_ids` is never empty."""

    model_config = _CONFIG

    deal_id: StrictStr
    name: StrictStr
    pipeline: StrictStr
    stage: StrictStr
    #: SS1.2: the only float-typed field in the contract. It never reaches
    #: `canon_value` as a float -- staging converts it with `Money.from_dollars`.
    amount: DealAmount
    associated_contact_ids: list[StrictStr] = Field(min_length=1)
    created_at: Timestamp
    updated_at: Timestamp


class AppDbStudent(BaseModel):
    """SS1.3 -- app-DB student. `student_number` and `communication_opt_out` are sensitive."""

    model_config = _CONFIG

    id: StrictStr
    first_name: StrictStr
    last_name: StrictStr
    dob: StrictStr
    grade: StrictStr
    guardian_email: StrictStr
    status: StrictStr
    enrollment_year: Int32
    created_at: Timestamp
    updated_at: Timestamp
    guardian2_email: StrictStr | None = None
    student_number: StrictStr | None = None
    household_id: StrictStr | None = None
    communication_opt_out: StrictBool | None = None


class AppDbEnrollment(BaseModel):
    """SS1.4 -- at most one per student; `deposit_paid_at` is never cleared once set."""

    model_config = _CONFIG

    id: StrictStr
    student_id: StrictStr
    program: StrictStr
    stage: StrictStr
    created_at: Timestamp
    updated_at: Timestamp
    deposit_paid_at: Timestamp | None = None
    crm_deal_id: StrictStr | None = None
    billing_owner_email: StrictStr | None = None


class PaymentMetadata(BaseModel):
    """SS1.5 -- `student_first_name` and `student_last_name` are SEPARATE fields.

    They are never joined, split or re-parsed on either side (SS4.3 `P2`).
    """

    model_config = _CONFIG

    student_first_name: StrictStr | None = None
    student_last_name: StrictStr | None = None
    program: StrictStr | None = None


class Payment(BaseModel):
    """SS1.5 -- Stripe-shaped. A refund is this record with `status='refunded'`."""

    model_config = _CONFIG

    payment_id: StrictStr
    payer_email: StrictStr
    payer_name: StrictStr
    amount_cents: AmountCents
    currency: StrictStr
    type: StrictStr
    status: StrictStr
    occurred_at: Timestamp
    external_ref: StrictStr | None = None
    refunded_at: Timestamp | None = None
    metadata: PaymentMetadata | None = None
    created_at: Timestamp | None = None
    updated_at: Timestamp | None = None


#: `(source_id, entity_type) -> model`. The only registry; adapters and the HTTP
#: endpoint both resolve through it, so an unknown pair is one error message.
ENTITY_MODELS: dict[tuple[str, str], type[BaseModel]] = {
    ("crm", "contact"): CrmContact,
    ("crm", "deal"): CrmDeal,
    ("appdb", "student"): AppDbStudent,
    ("appdb", "enrollment"): AppDbEnrollment,
    ("payments", "payment"): Payment,
}

#: The source PK per SS3: "`natural_key` is the source PK of a record".
PRIMARY_KEYS: dict[tuple[str, str], str] = {
    ("crm", "contact"): "crm_id",
    ("crm", "deal"): "deal_id",
    ("appdb", "student"): "id",
    ("appdb", "enrollment"): "id",
    ("payments", "payment"): "payment_id",
}

#: Entity types per source, in the order a full read visits them.
SOURCE_ENTITY_TYPES: dict[str, tuple[str, ...]] = {
    "crm": ("contact", "deal"),
    "appdb": ("student", "enrollment"),
    "payments": ("payment",),
}


def model_for(source_id: str, entity_type: str) -> type[BaseModel]:
    try:
        return ENTITY_MODELS[(source_id, entity_type)]
    except KeyError:
        raise ValueError(
            f"unknown source/entity pair {source_id!r}/{entity_type!r}; "
            f"expected one of {sorted(ENTITY_MODELS)}"
        ) from None


def primary_key_field(source_id: str, entity_type: str) -> str:
    try:
        return PRIMARY_KEYS[(source_id, entity_type)]
    except KeyError:
        raise ValueError(
            f"unknown source/entity pair {source_id!r}/{entity_type!r}; "
            f"expected one of {sorted(PRIMARY_KEYS)}"
        ) from None
