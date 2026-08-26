"""Landing and staging: fixtures -> `raw_records` -> the five `stg_*` tables (R1, R4).

The shape, from contract SS3::

    fixtures/*.jsonl --ReadOnlyAdapter--> raw_records (append-only, lineage stamped)
                                      --> source_generations (completeness ledger)
    raw_records --recon/normalize.py--> stg_crm_contact, stg_crm_deal, stg_student,
                                        stg_enrollment, stg_payment

Five properties are load-bearing rather than incidental, and each one is a place a
plausible implementation goes wrong:

**Landing is append-only, and a later generation never overwrites an earlier one.**
Each snapshot is complete (SS7), so generation 3 re-emits records that have not
changed since generation 1. The obvious "upsert on natural key" would collapse the
history that R4 exists for -- a field that goes A -> B -> A is only visible as
oscillation because all three rows survive -- so the write is a COPY of new rows
and `recon_writer` holds no UPDATE or DELETE on `raw_records` at all. The
privilege is the enforcement; this module just never asks.

**Every normalized column is computed in Python, here, by `recon/normalize.py`.**
Contract SS2 is explicit that `rules/*.sql` may never normalize: the generator and
the detector share one normalization module (R23) and a second, SQL-shaped
implementation of `norm_email` is exactly the drift that makes a golden set
un-gradeable. So the `stg_*` tables carry both the raw and the normalized value,
and SQL only ever reads `*_norm`.

**`complete` is a measurement of the generation, never a claim by a slice.** One
`source_generations` row describes one `(source, entity_type, generation)`
(`LEDGER_SCOPE`), every count on it is measured at that scope by the database
after the write, and `complete` is computed from those counts inside the same
statement (`LEDGER_COMPLETE_RULE`): what a rule can see equals what the manifest
expected. A load contributes to the row; it does not overwrite it, and it cannot
state a verdict about a generation it only holds one slice of. With **no**
expected count the row says `false` -- absence of an expectation is not evidence
of completeness.

A wrongly-`true` flag is not a cosmetic bug: SS5.3 makes the invariant engine skip
every absence rule (C1, C2, C5, C7, C8, C9, C13) on an incomplete generation,
precisely because running an absence test over missing rows manufactures false
positives. SS9.1's raw sweep puts 875 enrollments in C7's population and 575
persons in C1's, so one truncated payments load reported as complete manufactures
thousands of conflicts that the golden set does not contain.

**A rejected record is counted, logged and dropped -- never silently skipped.**
`ingest_source` installs a rejection sink on the adapter, so one malformed line in
a 40,000-line snapshot costs exactly that line and shows up in
`ingest_runs.records_rejected` and in the structured log, with its 4xx.

**Accounting is a structural invariant, not a habit.** For every
`(source, generation, entity_type)`::

    records_read == records_landed + records_rejected

`read` is counted in the read loop, `landed` is what the COPY actually wrote (read
back from `raw_records` by `load_id`), `rejected` is the sink's count -- three
measurements, never derivations of each other -- and `LoadResult.check` /
`_check_source_accounting` raise `IngestAccountingError` when they disagree. This
is enforcement rather than documentation because both ways it has been broken
looked *clean* from the outside: counting `len(records)` as loaded while the write
was gated on a staging table reported a full, `complete` generation over zero
landed rows, and returning only the adapter's *declared* entity types dropped
every record of any other type out of all three counts at once. Either one hands
SS5.3 a generation marked complete that is missing rows, and every absence rule
then fires against it. A record must be impossible to lose without the count
noticing, so any future path that drops one fails loudly instead of under-reporting.

**A sync is bounded as a whole, not just one load at a time.** The per-load
deadline bounds one `(source, generation)`; `ingest_all` runs nine of them in
sequence, so `SYNC_BUDGET_SECONDS` bounds the sequence (R3). Loads past the budget
still return their own structured `source_timeout` and their own ledger row --
skipping them would be a silent skip with a stopwatch.

Timestamps. `ingest_ts` is the database's `now()` rather than a Python clock: it
is a fact about when the row landed, and CLAUDE.md bans `datetime.now()` on graded
paths. `row_hash` and every normalized value are pure functions of the payload, so
nothing here depends on when it ran.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import psycopg
import structlog
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import Connection, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.elements import TextClause

from recon.adapters import (
    ADAPTER_LOAD_DEADLINE_SECONDS,
    ADAPTER_STALL_TIMEOUT_SECONDS,
    IDENTIFIER_RULE,
    INT32_MAX,
    KIND_STATUS,
    MAX_PAYLOAD_BYTES,
    PRIMARY_KEYS,
    SOURCE_ENTITY_TYPES,
    AdapterError,
    IdentifierError,
    RawRecord,
    ReadOnlyAdapter,
    build_adapters,
    canonical_json,
    default_fixtures_root,
    partition,
    read_bounded,
    validate_batch,
    validate_identifier,
)
from recon.api.auth import JOB_SYNC, TRIGGER_SECRET_HEADER, trigger_guard
from recon.db import ROLE_RECON_WRITER, DatabaseNotConfigured, role_connection
from recon.logging import insert_audit_row
from recon.normalize import norm_dob, norm_email, norm_enum, norm_name
from recon.reference import (
    DEAL_STAGE_TO_FUNNEL,
    FUNNEL_VALUES,
    PAYMENT_TYPES,
    STATUS_TO_FUNNEL,
    Money,
    grade_ord,
    make_ref,
)

__all__ = [
    "ACCOUNTING_INVARIANT",
    "AUDIT_ACTOR_INGEST",
    "DEFAULT_MAX_BODY_BYTES",
    "LEDGER_COMPLETE_RULE",
    "LEDGER_SCOPE",
    "MAX_BODY_BYTES_ENV",
    "MAX_RECORDS_PER_BATCH",
    "STAGING_INVARIANT",
    "STAGING_TABLES",
    "SYNC_BUDGET_SECONDS",
    "IngestAccountingError",
    "IngestReport",
    "IngestStagingError",
    "Landing",
    "LedgerVerdict",
    "LoadResult",
    "OversizedBody",
    "RunVerdict",
    "SourceResult",
    "expected_counts_from_manifest",
    "ingest_all",
    "ingest_generation",
    "ingest_source",
    "ledger_complete",
    "load_key",
    "max_body_bytes",
    "oversized_body_problem",
    "raw_request_body",
    "router",
    "stamp_ledger",
    "stamp_run",
]

log = structlog.get_logger("recon.ingest")

#: The accounting invariant, stated once and enforced in code (`LoadResult.check`
#: and `_check_source_accounting`). Every record a source hands over is either
#: landed or rejected; there is no third outcome, and "dropped" is not spellable.
ACCOUNTING_INVARIANT = (
    "for every (source, generation, entity_type): records_read == records_landed + records_rejected"
)
_ACCOUNTING = ACCOUNTING_INVARIANT

#: The staging invariant, stated once and enforced in `_check_staging`. The
#: landing table is the mirror; `stg_*` is what every rule actually reads, so a
#: staging slice that holds fewer records than landed hands the invariant engine
#: a silently truncated dataset it believes is whole.
#:
#: It is `count(DISTINCT natural_key)` rather than `count(*)` because
#: `raw_records` is append-only and a re-ingest of a generation legitimately
#: lands a second copy of every record, while staging is a derived cache that
#: holds the current materialization of each key (contract SS2, migration 0002 --
#: `recon_writer` holds DELETE on `stg_*` and nowhere else, precisely because it
#: is re-materialisable). A repeated primary key inside one generation is a
#: structural rejection (SS7, SS12 D-3) and never lands, so "one staging row per
#: distinct landed key" is exact rather than approximate.
STAGING_INVARIANT = (
    "for every (source, entity_type, generation) with a stg_* table: "
    "count(stg_*) == count(DISTINCT natural_key in raw_records)"
)
_STAGING = STAGING_INVARIANT

#: `audit_log.actor` for an ingestion fault. `^system:` scoped, which migration
#: 0004's `audit_log_actor_scope` trigger (SQLSTATE `KS003`) requires of every row
#: `recon_writer` writes.
AUDIT_ACTOR_INGEST: str = "system:ingest"

#: The whole-sync wall-clock budget (R3, the useful reading). The per-load
#: `ADAPTER_LOAD_DEADLINE_SECONDS` bounds ONE `(source, generation)` load; a full
#: sync runs nine of them in sequence, so a wedged fixture tree would satisfy
#: every per-load bound and still take three quarters of an hour to return a
#: structured error. This is the cumulative bound over all of them: once it is
#: spent, each remaining load is given a zero deadline and fails immediately with
#: the ordinary `source_timeout` -- a structured result per source, never a
#: silently skipped one.
SYNC_BUDGET_SECONDS: float = 600.0

#: Payment statuses SS1.5 permits. Not a `norm_enum` field -- the vocabulary is
#: two values and lives in the contract, not in the enum-variant tables.
PAYMENT_STATUSES: tuple[str, ...] = ("paid", "refunded")

STAGING_TABLES: dict[tuple[str, str], str] = {
    ("crm", "contact"): "stg_crm_contact",
    ("crm", "deal"): "stg_crm_deal",
    ("appdb", "student"): "stg_student",
    ("appdb", "enrollment"): "stg_enrollment",
    ("payments", "payment"): "stg_payment",
}

_COMMON_COLUMNS: tuple[str, ...] = (
    "generation",
    "source_id",
    "source_ref",
    "raw_record_id",
    "run_id",
    "row_hash",
)

_RAW_COLUMNS: tuple[str, ...] = (
    "source_id",
    "entity_type",
    "natural_key",
    "generation",
    "payload",
    "row_hash",
    "load_id",
    "run_id",
)


# ======================================================================================
# value formatting for COPY ... FROM STDIN (text format)
# ======================================================================================


def _text(value: Any) -> str | None:
    """Render one value for text-format COPY. `None` becomes SQL NULL."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, (dict, list)):
        return canonical_json(value)
    if isinstance(value, str):
        return value
    return str(value)


def _str_or_none(value: Any) -> str | None:
    """Keep a source string; anything non-string is not a string value."""
    return value if isinstance(value, str) else None


def _funnel_ord(funnel: str | None) -> int | None:
    return None if funnel is None else FUNNEL_VALUES.index(funnel)


def _note(unchecked: dict[str, str], path: str, raw: Any, normalized: Any, reason: str) -> None:
    """Record that a *present* value failed to normalize (SS5.1 ruling 5, SS5.8).

    Absent/null is not `unchecked` -- it is simply absent. Only a value that was
    there and could not be mapped earns a row here, which is what lets a rule
    report `detail.reason='unmapped_enum'` instead of guessing.
    """
    if raw is not None and normalized is None:
        unchecked[path] = reason


# ======================================================================================
# staging row builders -- one per entity type, all normalization in Python
# ======================================================================================


@dataclass(frozen=True, slots=True)
class StagingSpec:
    table: str
    columns: tuple[str, ...]
    build: Callable[[RawRecord], tuple[Any, ...]]


def _contact_row(record: RawRecord) -> tuple[Any, ...]:
    p = record.payload
    unchecked: dict[str, str] = {}

    email = _str_or_none(p.get("email"))
    first = _str_or_none(p.get("first_name"))
    last = _str_or_none(p.get("last_name"))
    dob = _str_or_none(p.get("dob"))
    grade = _str_or_none(p.get("grade"))
    state = _str_or_none(p.get("state"))
    lifecycle = _str_or_none(p.get("lifecycle_stage"))

    dob_norm = norm_dob(dob)
    grade_norm = norm_enum("grade", grade)
    state_norm = norm_enum("state", state)
    lifecycle_norm = norm_enum("lifecycle_stage", lifecycle)

    _note(unchecked, "crm.contact.dob", dob, dob_norm, "unparseable_value")
    _note(unchecked, "crm.contact.grade", grade, grade_norm, "unmapped_enum")
    _note(unchecked, "crm.contact.state", state, state_norm, "unmapped_enum")
    _note(unchecked, "crm.contact.lifecycle_stage", lifecycle, lifecycle_norm, "unmapped_enum")

    return (
        p.get("crm_id"),
        email,
        first,
        last,
        lifecycle,
        p.get("external_id"),
        dob,
        grade,
        state,
        p.get("marketing_consent"),
        p.get("created_at"),
        p.get("updated_at"),
        norm_email(email),
        norm_name(first),
        norm_name(last),
        dob_norm,
        grade_norm,
        grade_ord(grade),
        state_norm,
        lifecycle_norm,
        unchecked or None,
    )


_CONTACT_COLUMNS = (
    "crm_id",
    "email",
    "first_name",
    "last_name",
    "lifecycle_stage",
    "external_id",
    "dob",
    "grade",
    "state",
    "marketing_consent",
    "created_at",
    "updated_at",
    "email_norm",
    "first_norm",
    "last_norm",
    "dob_norm",
    "grade_norm",
    "grade_ord",
    "state_norm",
    "lifecycle_norm",
    "unchecked_fields",
)


def _deal_row(record: RawRecord) -> tuple[Any, ...]:
    p = record.payload
    unchecked: dict[str, str] = {}

    stage = _str_or_none(p.get("stage"))
    pipeline = _str_or_none(p.get("pipeline"))
    amount = p.get("amount")

    deal_stage = norm_enum("deal_stage", stage)
    stage_funnel = None if deal_stage is None else DEAL_STAGE_TO_FUNNEL[deal_stage]
    pipeline_norm = norm_enum("pipeline", pipeline)

    _note(unchecked, "crm.deal.stage", stage, stage_funnel, "unmapped_enum")
    _note(unchecked, "crm.deal.pipeline", pipeline, pipeline_norm, "unmapped_enum")

    # SS1.2: `amount` is the only float in the contract. It is kept verbatim as
    # text and converted with the pinned `Money.from_dollars` (banker's rounding),
    # so it never reaches `canon_value` as a float.
    amount_raw = None if amount is None else json.dumps(amount)
    amount_cents = Money.from_dollars(amount).cents if isinstance(amount, (int, float)) else None

    return (
        p.get("deal_id"),
        p.get("name"),
        pipeline,
        stage,
        amount_raw,
        p.get("associated_contact_ids"),
        p.get("created_at"),
        p.get("updated_at"),
        amount_cents,
        stage_funnel,
        _funnel_ord(stage_funnel),
        pipeline_norm,
        unchecked or None,
    )


_DEAL_COLUMNS = (
    "deal_id",
    "name",
    "pipeline",
    "stage",
    "amount_raw",
    "associated_contact_ids",
    "created_at",
    "updated_at",
    "amount_cents",
    "stage_funnel",
    "stage_funnel_ord",
    "pipeline_norm",
    "unchecked_fields",
)


def _student_row(record: RawRecord) -> tuple[Any, ...]:
    p = record.payload
    unchecked: dict[str, str] = {}

    first = _str_or_none(p.get("first_name"))
    last = _str_or_none(p.get("last_name"))
    dob = _str_or_none(p.get("dob"))
    grade = _str_or_none(p.get("grade"))
    status = _str_or_none(p.get("status"))
    guardian = _str_or_none(p.get("guardian_email"))
    guardian2 = _str_or_none(p.get("guardian2_email"))

    dob_norm = norm_dob(dob)
    grade_norm = norm_enum("grade", grade)
    status_norm = norm_enum("status", status)
    status_compare = None if status_norm is None else STATUS_TO_FUNNEL[status_norm]

    _note(unchecked, "appdb.student.dob", dob, dob_norm, "unparseable_value")
    _note(unchecked, "appdb.student.grade", grade, grade_norm, "unmapped_enum")
    _note(unchecked, "appdb.student.status", status, status_norm, "unmapped_enum")

    return (
        p.get("id"),
        first,
        last,
        dob,
        grade,
        guardian,
        guardian2,
        status,
        p.get("enrollment_year"),
        p.get("student_number"),
        p.get("household_id"),
        p.get("communication_opt_out"),
        p.get("created_at"),
        p.get("updated_at"),
        norm_email(guardian),
        norm_email(guardian2),
        norm_name(first),
        norm_name(last),
        dob_norm,
        grade_norm,
        grade_ord(grade),
        status_norm,
        status_compare,
        unchecked or None,
    )


_STUDENT_COLUMNS = (
    "student_id",
    "first_name",
    "last_name",
    "dob",
    "grade",
    "guardian_email",
    "guardian2_email",
    "status",
    "enrollment_year",
    "student_number",
    "household_id",
    "communication_opt_out",
    "created_at",
    "updated_at",
    "email_norm",
    "guardian2_email_norm",
    "first_norm",
    "last_norm",
    "dob_norm",
    "grade_norm",
    "grade_ord",
    "status_norm",
    "status_compare",
    "unchecked_fields",
)


def _enrollment_row(record: RawRecord) -> tuple[Any, ...]:
    p = record.payload
    unchecked: dict[str, str] = {}

    stage = _str_or_none(p.get("stage"))
    program = _str_or_none(p.get("program"))
    billing = _str_or_none(p.get("billing_owner_email"))

    stage_funnel = norm_enum("stage", stage)
    program_norm = norm_enum("program", program)

    _note(unchecked, "appdb.enrollment.stage", stage, stage_funnel, "unmapped_enum")
    _note(unchecked, "appdb.enrollment.program", program, program_norm, "unmapped_enum")

    return (
        p.get("id"),
        p.get("student_id"),
        program,
        stage,
        p.get("deposit_paid_at"),
        p.get("crm_deal_id"),
        billing,
        p.get("created_at"),
        p.get("updated_at"),
        stage_funnel,
        _funnel_ord(stage_funnel),
        program_norm,
        norm_email(billing),
        unchecked or None,
    )


_ENROLLMENT_COLUMNS = (
    "enrollment_id",
    "student_id",
    "program",
    "stage",
    "deposit_paid_at",
    "crm_deal_id",
    "billing_owner_email",
    "created_at",
    "updated_at",
    "stage_funnel",
    "stage_funnel_ord",
    "program_norm",
    "billing_owner_email_norm",
    "unchecked_fields",
)


def _payment_row(record: RawRecord) -> tuple[Any, ...]:
    p = record.payload
    unchecked: dict[str, str] = {}

    payer_email = _str_or_none(p.get("payer_email"))
    payment_type = _str_or_none(p.get("type"))
    status = _str_or_none(p.get("status"))
    metadata = p.get("metadata") if isinstance(p.get("metadata"), Mapping) else None
    meta_program = _str_or_none(metadata.get("program")) if metadata else None
    meta_first = _str_or_none(metadata.get("student_first_name")) if metadata else None
    meta_last = _str_or_none(metadata.get("student_last_name")) if metadata else None

    type_norm = payment_type if payment_type in PAYMENT_TYPES else None
    status_norm = status if status in PAYMENT_STATUSES else None
    program_norm = norm_enum("program", meta_program)

    _note(unchecked, "payments.payment.type", payment_type, type_norm, "unmapped_enum")
    _note(unchecked, "payments.payment.status", status, status_norm, "unmapped_enum")
    _note(
        unchecked,
        "payments.payment.metadata.program",
        meta_program,
        program_norm,
        "unmapped_enum",
    )

    return (
        p.get("payment_id"),
        payer_email,
        p.get("payer_name"),
        p.get("amount_cents"),
        p.get("currency"),
        payment_type,
        status,
        p.get("occurred_at"),
        p.get("refunded_at"),
        p.get("external_ref"),
        metadata,
        norm_email(payer_email),
        # SS1.5 / SS4.3 `P2`: "No name string is ever joined, split or re-parsed on
        # either side." `payer_name` is one opaque string, so splitting it into a
        # first/last pair here would invent the very re-parse the contract forbids
        # -- and `P2` joins on `metadata.student_{first,last}_name`, which ARE two
        # separate source fields. These two columns therefore stay NULL by
        # construction, and the two below them carry the join keys.
        None,
        None,
        norm_name(meta_first),
        norm_name(meta_last),
        program_norm,
        type_norm,
        status_norm,
        unchecked or None,
    )


_PAYMENT_COLUMNS = (
    "payment_id",
    "payer_email",
    "payer_name",
    "amount_cents",
    "currency",
    "type",
    "status",
    "occurred_at",
    "refunded_at",
    "external_ref",
    "payment_metadata",
    "email_norm",
    "payer_first_norm",
    "payer_last_norm",
    "student_name_first_norm",
    "student_name_last_norm",
    "program_norm",
    "type_norm",
    "status_norm",
    "unchecked_fields",
)


STAGING: dict[tuple[str, str], StagingSpec] = {
    ("crm", "contact"): StagingSpec("stg_crm_contact", _CONTACT_COLUMNS, _contact_row),
    ("crm", "deal"): StagingSpec("stg_crm_deal", _DEAL_COLUMNS, _deal_row),
    ("appdb", "student"): StagingSpec("stg_student", _STUDENT_COLUMNS, _student_row),
    ("appdb", "enrollment"): StagingSpec("stg_enrollment", _ENROLLMENT_COLUMNS, _enrollment_row),
    ("payments", "payment"): StagingSpec("stg_payment", _PAYMENT_COLUMNS, _payment_row),
}


# ======================================================================================
# results
# ======================================================================================


class IngestAccountingError(RuntimeError):
    """The accounting invariant did not hold, so the load is not reportable.

    Raised, never logged-and-swallowed: an under-reported load writes
    `complete = true` over a generation that is missing rows, and SS5.3 then lets
    every absence rule run against it. A loud failure costs one sync; a quiet one
    fabricates conflicts the golden set does not contain.
    """


class IngestStagingError(IngestAccountingError):
    """`stg_*` does not hold every record `raw_records` holds for a generation.

    A subclass of :class:`IngestAccountingError` because it is the same failure
    wearing a different table: a count that does not add up, reported loudly
    instead of handed to SS5.3 as a complete generation. `_materialize` used to
    ``DELETE FROM stg_* WHERE source_id = ... AND generation = ...`` and then
    re-insert only the current slice, so a second slice posted into one
    generation left both slices in `raw_records`, only the second in staging, and
    the ledger saying `complete`.
    """


@dataclass(frozen=True, slots=True)
class Landing:
    """What one landing write actually did.

    `replayed` is not a nicety: the landing table is append-only, so "did this
    load already land?" and "land it" have to be **one** decision. Two concurrent
    requests sharing a `run_id` both read `already == 0` under READ COMMITTED,
    both COPYed, and both answered 200 over a doubled landing table -- which is
    the mirror every downstream count and every absence test rests on.
    """

    #: Rows in `raw_records` carrying this `load_id` once the write is done.
    #: On a replay this is what was **already** there; nothing was added.
    landed: int
    #: True when this load had already landed and nothing was written.
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class LoadResult:
    """One `(source, entity_type, generation)` load.

    `read` / `loaded` / `rejected` are three **separately measured** counts, not
    three views of one number:

    `read`
        records the source handed over for this entity type, counted in the read
        loop itself, plus the lines it rejected.
    `loaded`
        rows the landing COPY actually wrote, read back from `raw_records` by
        `load_id`. It is emphatically not `len(records)`: that is the assumption
        that let a load whose write was skipped report a full, complete
        generation.
    `rejected`
        structural rejections attributed to this entity type.

    `check()` asserts the one thing that ties them together (see `_ACCOUNTING`).
    """

    source_id: str
    entity_type: str
    generation: int
    read: int
    loaded: int
    rejected: int
    expected: int | None
    complete: bool
    #: False when no `stg_*` table exists for `(source_id, entity_type)`. Such a
    #: load can land in `raw_records` but can never be materialized, so no rule
    #: will ever see it -- which makes it incomplete by definition (SS5.3).
    staged: bool = True
    #: Records of this **generation** a rule can see once this load is done:
    #: `count(*)` of the `stg_*` slice, read back from the database. `None` (the
    #: default, and the dry-run case) means "the same as this load's `loaded`".
    #:
    #: Separate from `loaded` because they answer different questions and the
    #: difference is the bug: `loaded` is what THIS write added, `visible` is what
    #: the GENERATION now holds. A generation assembled from two slices has a
    #: `visible` of both and a `loaded` of one.
    staged_count: int | None = None
    #: The ledger row this load contributed to, as the database reported it back
    #: (`stamp_ledger`). `None` on the dry-run path, where there is no row.
    #: `complete` above is this verdict's `complete` whenever it is present.
    ledger: LedgerVerdict | None = None

    @property
    def visible(self) -> int:
        """Records of this generation a rule can see. See `staged_count`.

        **Zero for a pair with no `stg_*` table**, whatever landed: `rules/*.sql`
        read staging and never `raw_records`, so an unmaterialized generation is
        invisible rather than partially visible -- which is what stops
        `LEDGER_COMPLETE_RULE` from calling it complete.
        """
        if not self.staged:
            return 0
        return self.loaded if self.staged_count is None else self.staged_count

    @property
    def known_short(self) -> bool:
        """True when this load can be **shown** to be missing records.

        The mirror image of `complete`, and deliberately not its negation. The
        gap between them is one state -- "there is no expectation to compare
        against" -- and the two questions want opposite answers there:

        `complete`
            *provably whole*. False without an expectation, because SS5.3 uses it
            to decide whether an absence rule may run, and running one over a
            generation nobody has counted is how absence tests manufacture false
            positives.
        `known_short`
            *provably not whole*: nothing was materialized, or the manifest says
            there should be more than a rule can see. False without an
            expectation, because a clean read of a source nobody published a
            count for is not a failed sync -- and `ingest_runs.status` reports
            what the sync did, not what the ledger can prove.
        """
        return not self.staged or (self.expected is not None and self.visible != self.expected)

    def check(self) -> None:
        """Raise unless `read == loaded + rejected` for this load."""
        if self.read != self.loaded + self.rejected:
            raise IngestAccountingError(
                f"{_ACCOUNTING} -- violated by {self.source_id}/{self.entity_type} "
                f"generation {self.generation}: read={self.read} "
                f"landed={self.loaded} rejected={self.rejected} "
                f"(unaccounted={self.read - self.loaded - self.rejected})"
            )


@dataclass(frozen=True, slots=True)
class SourceResult:
    """One source's generation-N ingest."""

    source_id: str
    generation: int
    run_id: str
    status: str
    loads: tuple[LoadResult, ...]
    rejections: tuple[AdapterError, ...]
    error: AdapterError | None
    elapsed_ms: float

    @property
    def records_read(self) -> int:
        return sum(load.read for load in self.loads)

    @property
    def records_ok(self) -> int:
        return sum(load.loaded for load in self.loads)

    @property
    def records_rejected(self) -> int:
        return sum(load.rejected for load in self.loads)

    @property
    def complete(self) -> bool:
        return bool(self.loads) and all(load.complete for load in self.loads)


@dataclass(frozen=True, slots=True)
class IngestReport:
    """Everything one `ingest_generation`/`ingest_all` call did."""

    sources: tuple[SourceResult, ...] = ()
    elapsed_ms: float = 0.0

    @property
    def records_ok(self) -> int:
        return sum(source.records_ok for source in self.sources)

    @property
    def records_rejected(self) -> int:
        return sum(source.records_rejected for source in self.sources)

    @property
    def degraded(self) -> bool:
        """True when any source's generation is incomplete (SS5.3's run marking)."""
        return not all(source.complete for source in self.sources)


# ======================================================================================
# manifest
# ======================================================================================


def expected_counts_from_manifest(
    root: Path | str | None = None,
) -> dict[tuple[str, str, int], int]:
    """`(source, entity_type, generation) -> expected record count`, from SS8's manifest.

    Absent manifest -> empty mapping, and completeness then rests on "the stream
    ended cleanly and nothing was rejected". A *wrong* expected count would be
    worse than none, so this never guesses.

    **A corrupt manifest degrades exactly like an absent one**, loudly. This runs
    inside `/internal/ingest/records`, so before this guard a truncated or
    wrong-shaped `fixtures/manifest.json` -- deployment state, not anything a
    caller sent -- turned *every authenticated POST* into a 500: `json.loads`
    raises `JSONDecodeError`, a manifest whose `expected_counts` is a list raises
    `AttributeError`, and a non-numeric count raises `ValueError`, none of which
    the handler's `except` clause names. The counts are an *optional* strengthening
    of the completeness test, so an unreadable one may not be the thing that fails
    a request. It is logged at error level and per-entry, so the degradation is
    reported rather than silent -- the failure this function must avoid is claiming
    a count it does not have.
    """
    base = Path(root) if root is not None else default_fixtures_root()
    manifest_path = base / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw_counts = manifest.get("expected_counts") or {}
        items = list(raw_counts.items())
    except Exception as exc:
        log.error(
            "ingest.manifest_unreadable",
            path=str(manifest_path),
            error=f"{type(exc).__name__}: {exc}",
            detail=(
                "the manifest could not be read, so no expected counts are applied; "
                "completeness rests on the stream ending cleanly with nothing rejected"
            ),
        )
        return {}

    counts: dict[tuple[str, str, int], int] = {}
    for gen_key, per_entity in items:
        if not isinstance(gen_key, str) or not gen_key.startswith("gen"):
            continue
        if not gen_key[3:].isdigit():
            continue
        generation = int(gen_key[3:])
        if not isinstance(per_entity, Mapping):
            log.error(
                "ingest.manifest_entry_unusable",
                path=str(manifest_path),
                generation=generation,
                detail=f"expected an object of per-entity counts, got {type(per_entity).__name__}",
            )
            continue
        for dotted, expected in per_entity.items():
            source_id, _, entity_type = str(dotted).partition(".")
            try:
                counts[(source_id, entity_type, generation)] = int(expected)
            except (TypeError, ValueError):
                log.error(
                    "ingest.manifest_entry_unusable",
                    path=str(manifest_path),
                    generation=generation,
                    entry=str(dotted),
                    detail=f"expected count is not an integer: {type(expected).__name__}",
                )
    return counts


# ======================================================================================
# the writes
# ======================================================================================


def _driver_cursor(conn: Connection):
    return conn.connection.driver_connection.cursor()


def _copy(
    conn: Connection, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]
) -> int:
    """`COPY <table> (<columns>) FROM STDIN`, text format. Returns rows written."""
    column_list = ", ".join(f'"{name}"' for name in columns)
    statement = f'COPY "{table}" ({column_list}) FROM STDIN'
    written = 0
    with _driver_cursor(conn) as cursor, cursor.copy(statement) as copy_in:
        for row in rows:
            copy_in.write_row(tuple(_text(value) for value in row))
            written += 1
    return written


#: Serialises the whole read-check-write-stamp sequence for one generation
#: slice. Transaction-scoped, so it is released by the commit that makes the
#: write visible -- there is no window between "I checked" and "my rows are
#: visible" for a second transaction to slip through. Under READ COMMITTED
#: neither of two concurrent transactions sees the other's uncommitted rows, so
#: without this both passed the `already == 0` check, both COPYed, and the
#: landing table held two copies of every record.
#:
#: The key is `(source_id, entity_type, generation)` rather than the `load_id`.
#: That serialises the `(source, entity_type, generation, run_id)` load -- two
#: requests sharing a run id necessarily share the slice -- and, because the
#: ledger row and the staging slice are keyed on the generation and not on the
#: run, it also serialises the two *different* run ids that write them.
_LOCK_SLICE = text("SELECT pg_advisory_xact_lock(hashtext(:key))")

_COUNT_LOAD = text("SELECT count(*) FROM raw_records WHERE load_id = :load_id")

_COUNT_LANDED_KEYS = text(
    "SELECT count(DISTINCT natural_key) FROM raw_records "
    "WHERE source_id = :source_id AND entity_type = :entity_type AND generation = :generation"
)


def slice_lock_key(source_id: str, entity_type: str, generation: int) -> str:
    """The advisory-lock key for one generation slice. One spelling, one place."""
    return f"keystone:ingest:{source_id}:{entity_type}:g{generation}"


def _lock_slice(conn: Connection, source_id: str, entity_type: str, generation: int) -> None:
    conn.execute(_LOCK_SLICE, {"key": slice_lock_key(source_id, entity_type, generation)})


def _staged_count(conn: Connection, table: str, source_id: str, generation: int) -> int:
    return int(
        conn.execute(
            text(
                f'SELECT count(*) FROM "{table}" '
                "WHERE source_id = :source_id AND generation = :generation"
            ),
            {"source_id": source_id, "generation": generation},
        ).scalar_one()
    )


def _check_staging(conn: Connection, source_id: str, entity_type: str, generation: int) -> int:
    """Assert `STAGING_INVARIANT` for one slice and return the staged count.

    Two measurements, neither derived from the other and neither derived from
    anything Python counted: `count(*)` of the `stg_*` slice, and
    `count(DISTINCT natural_key)` of the landing rows for the same key. They are
    read back from the database *after* the write, which is the only reading that
    can catch a materialization that silently dropped rows -- the failure mode
    here was a `DELETE` scoped wider than the `INSERT` that followed it, and
    every number Python held said the load was fine.
    """
    spec = STAGING[(source_id, entity_type)]
    staged = _staged_count(conn, spec.table, source_id, generation)
    landed_keys = int(
        conn.execute(
            _COUNT_LANDED_KEYS,
            {"source_id": source_id, "entity_type": entity_type, "generation": generation},
        ).scalar_one()
    )
    if staged != landed_keys:
        raise IngestStagingError(
            f"{_STAGING} -- violated by {source_id}/{entity_type} generation "
            f"{generation}: {spec.table} holds {staged} row(s) but raw_records "
            f"holds {landed_keys} distinct natural key(s) for that generation "
            f"({landed_keys - staged} landed record(s) are invisible to every "
            "rule, and SS5.3 would let every absence rule run against them)"
        )
    return staged


def _land(
    conn: Connection, records: Sequence[RawRecord], *, run_id: str, load_id: str
) -> list[int]:
    """COPY into `raw_records` and return the new ids in insertion order."""
    if not records:
        return []
    _copy(
        conn,
        "raw_records",
        _RAW_COLUMNS,
        (
            (
                record.source_id,
                record.entity_type,
                record.natural_key,
                record.generation,
                record.payload_json,
                record.row_hash,
                load_id,
                run_id,
            )
            for record in records
        ),
    )
    ids = list(
        conn.execute(
            text("SELECT id FROM raw_records WHERE load_id = :load_id ORDER BY id"),
            {"load_id": load_id},
        ).scalars()
    )
    if len(ids) != len(records):
        # `IngestAccountingError`, not a bare `RuntimeError`: this is the landing
        # count disagreeing with itself, which is the one thing the accounting
        # invariant exists to make loud -- and a `RuntimeError` here escaped the
        # HTTP handler as an unhandled 500 rather than a structured fault.
        raise IngestAccountingError(
            f"load_id {load_id!r} matches {len(ids)} landing rows but {len(records)} were "
            "copied; load ids must be unique per (run, source, entity type, generation)"
        )
    return ids


def _land_records(
    conn: Connection | None,
    records: Sequence[RawRecord],
    *,
    source_id: str,
    entity_type: str,
    generation: int,
    run_id: str,
    persist: bool,
    staged: bool,
) -> Landing:
    """Land one entity type's records **atomically and idempotently**.

    One function, because the count and the write have to be one decision. The
    bug this replaces was two: the landing write was gated on `(source_id,
    entity_type) in STAGING` while the reported count was `len(records)` taken
    unconditionally, so any pair outside those five reported a full, `complete`
    generation over zero landed rows.

    The second bug it replaces was one decision split across two transactions.
    `raw_records` is append-only, so "has this load already landed?" and "land
    it" are a check and a write that must not interleave -- and under READ
    COMMITTED they interleaved perfectly: twelve concurrent requests sharing a
    `run_id` each saw an empty load, each COPYed, and the landing table held
    twelve copies. The advisory lock is taken **first**, is transaction-scoped,
    and is therefore still held when the commit publishes the rows, so the second
    caller's check runs after the first caller's write is visible and finds it.

    A replay is then a **no-op that reports what already landed**: nothing is
    written, nothing is materialized, and `Landing.replayed` says so. Re-landing
    would double the mirror; re-materializing would double the staging slice.

    `raw_records` is generic -- source, entity type, natural key, payload -- so it
    can and does hold every pair. Only `stg_*` is per-entity, so a pair with no
    staging spec lands and is *not* materialized; the caller marks that load
    incomplete, because no rule reads `raw_records` and an absence test over an
    unmaterialized generation would fire on every row it cannot see.

    With `persist=False` (a dry run: no database, `--no-persist`, the read-only
    port tests) nothing is written and the accepted count is returned, which keeps
    the invariant meaningful in the only sense available without a store.
    """
    if not persist or conn is None:
        return Landing(landed=len(records))
    if not staged:
        log.warning(
            "ingest.unstaged_entity_type",
            run_id=run_id,
            source=source_id,
            entity_type=entity_type,
            generation=generation,
            records=len(records),
            detail=(
                f"no stg_* table is registered for {source_id}/{entity_type}; the "
                "records land in raw_records but are not materialized, so the "
                "generation is reported incomplete rather than silently empty"
            ),
        )
    load_id = load_key(run_id, source_id, entity_type, generation)
    _lock_slice(conn, source_id, entity_type, generation)
    already = int(conn.execute(_COUNT_LOAD, {"load_id": load_id}).scalar_one())
    if already:
        log.warning(
            "ingest.load_replayed",
            run_id=run_id,
            source=source_id,
            entity_type=entity_type,
            generation=generation,
            load_id=load_id,
            landed_count=already,
            records_read=len(records),
            detail=(
                "this load id already holds landed rows; landing is append-only, "
                "so the replay is a no-op and reports what is already there"
            ),
        )
        return Landing(landed=already, replayed=True)
    raw_ids = _land(conn, records, run_id=run_id, load_id=load_id)
    if staged:
        _materialize(conn, source_id, entity_type, generation, records, raw_ids, run_id=run_id)
    return Landing(landed=len(raw_ids))


def _materialize(
    conn: Connection,
    source_id: str,
    entity_type: str,
    generation: int,
    records: Sequence[RawRecord],
    raw_ids: Sequence[int],
    *,
    run_id: str,
) -> int:
    """Materialize the records just landed into `stg_*`, **additively**.

    Staging is a derived cache (migration 0002 says so, and `recon_writer` holds
    DELETE here and nowhere else), so a re-ingest of a record replaces that
    record's staging row rather than doubling it. `raw_records` still only ever
    gains rows.

    **The DELETE is scoped to what this slice supersedes, and to nothing else.**
    It used to be scoped to the whole `(source_id, generation)` while the INSERT
    that followed carried only the current slice, and those two scopes are not
    the same thing. Posting two slices into one generation therefore left both in
    `raw_records`, only the second in staging, and the ledger reporting the
    generation `complete` -- the invariant engine reads `stg_*` and trusts
    `complete`, so it was handed a silently truncated dataset it believed was
    whole, and every absence rule fired against the half that was missing.

    Scoping the DELETE by `source_ref` (which is `make_ref(source, entity_type,
    natural_key)`, and is the one key every `stg_*` table carries under the same
    name) makes the two scopes identical: a slice replaces exactly the records it
    re-asserts, and leaves every other record of the generation alone. Both
    readings survive -- a re-ingest of a whole snapshot still replaces it,
    one-for-one, and two disjoint slices now add up.

    The DELETE is skipped entirely when the generation has nothing staged yet,
    which is the overwhelmingly common case and the one the ingestion benchmark
    measures.
    """
    spec = STAGING[(source_id, entity_type)]
    refs = [
        make_ref(record.source_id, record.entity_type, record.natural_key) for record in records
    ]
    if refs and _staged_count(conn, spec.table, source_id, generation):
        conn.execute(
            text(
                f'DELETE FROM "{spec.table}" WHERE source_id = :source_id '
                "AND generation = :generation AND source_ref = ANY(:refs)"
            ),
            {"source_id": source_id, "generation": generation, "refs": refs},
        )
    if not records:
        return 0
    columns = (*_COMMON_COLUMNS, *spec.columns)

    def rows() -> Iterable[Sequence[Any]]:
        for record, raw_id in zip(records, raw_ids, strict=True):
            yield (
                record.generation,
                record.source_id,
                make_ref(record.source_id, record.entity_type, record.natural_key),
                raw_id,
                run_id,
                record.row_hash,
                *spec.build(record),
            )

    return _copy(conn, spec.table, columns, rows())


# ======================================================================================
# the completeness ledger and the run row: one scope per row, measured at that scope
# ======================================================================================

#: **The scope of one `source_generations` row, pinned.** The table's primary key
#: is `(source_id, generation, entity_type)` (migration 0009) and contract SS3
#: spells the row the same way, so the row describes ONE
#: `(source, entity_type, generation)` -- never one load, one slice or one run.
#:
#: This is stated here because the defect it replaces was a *scope mismatch* and
#: nothing else: `loaded_count` was measured over the generation while
#: `complete` and `rejected_count` were measured over the slice in hand, and the
#: upsert then wrote all three with `DO UPDATE SET ... = EXCLUDED. ...`. Whichever
#: slice committed last therefore published its own verdict as the generation's:
#: slice A landing 20 payments and rejecting 5 was overwritten by slice B, and
#: the generation came out `complete = true, rejected_count = 0` over a dataset
#: missing five records.
#:
#: SS5.3 makes that the worst reachable bug in this module rather than a
#: bookkeeping slip. An absence rule (C1, C2, C5, C7, C8, C9, C13) is *skipped*
#: on an incomplete generation precisely because running an absence test over
#: missing rows manufactures false positives; a `complete` that is true while
#: rows are missing hands the detector a truncated dataset it believes is whole,
#: and the correctness grade is zero false positives against the clean majority.
LEDGER_SCOPE: str = (
    "one source_generations row describes one (source_id, entity_type, generation): "
    "expected_count, loaded_count, rejected_count and complete are all measured at "
    "that scope, and a slice contributes to the row rather than overwriting it"
)

#: **`complete` is a function of the row's counts, not a flag a writer sets.**
#: It is computed by the database, in `_reconcile_ledger_statement`, out of two
#: numbers the database measured (`loaded_count`, and the `stg_*` count a rule
#: can actually see) and one number the manifest expected. `ledger_complete` is
#: the same function in Python for the dry-run path, and `stamp_ledger` refuses
#: to return a verdict the two disagree on, so the two spellings cannot drift.
#:
#: **Absence of an expectation is not evidence of completeness** (the fail-safe
#: half). With no manifest `expected_count` -- which is every generation
#: `/internal/ingest/records` is normally driven with -- the row cannot state
#: that what is visible is everything there is, so it says `false`. The cost is
#: that absence rules skip; the cost of the other choice is that a one-record
#: POST marks a whole generation complete and every absence rule runs against it.
LEDGER_COMPLETE_RULE: str = (
    "complete = expected_count IS NOT NULL AND loaded_count = expected_count "
    "AND visible = expected_count"
)


def load_key(run_id: str, source_id: str, entity_type: str, generation: int) -> str:
    """The id of one load: `(run, source, entity type, generation)`. One spelling.

    It names the landing rows a load wrote (`raw_records.load_id`) **and** the
    slice's entry in the ledger row's contribution map, so the ledger's
    arithmetic can be joined back to the rows it is about.
    """
    return f"{run_id}:{source_id}:{entity_type}:g{generation}"


def ledger_complete(expected: int | None, loaded: int, visible: int) -> bool:
    """SS5.3's verdict for one `(source, entity_type, generation)`. See `LEDGER_COMPLETE_RULE`.

    `visible` is what a rule can see -- the `stg_*` count -- and is `0` for a pair
    with no staging table, because `rules/*.sql` never read `raw_records`.
    """
    return expected is not None and loaded == expected and visible == expected


@dataclass(frozen=True, slots=True)
class LedgerVerdict:
    """The `source_generations` row as the database reports it after a load.

    Every field is read back from the row, so a caller reporting these numbers is
    quoting the ledger rather than its own expectations of it.
    """

    expected: int | None
    loaded: int
    rejected: int
    visible: int
    complete: bool


@dataclass(frozen=True, slots=True)
class RunVerdict:
    """The `ingest_runs` row as the database reports it after a load."""

    records_ok: int
    records_rejected: int
    status: str


#: One slice's contribution to the ledger row, merged into `error_detail` under
#: `slices` and keyed by `load_key`.
#:
#: `rejected` has to live somewhere durable and per-slice. Rejections are the one
#: quantity on the row with no rows of its own to be counted from -- a rejected
#: record is by definition not in `raw_records` -- so a second slice's
#: `EXCLUDED.rejected_count` silently erased the first slice's. Keying each
#: slice's count by its `load_id` makes the aggregate *combine* (two disjoint
#: slices add up), makes it *idempotent* (a replayed load overwrites its own key,
#: never appends), and keeps the evidence: the map says which load could not read
#: what.
#:
#: The key names are drawn from `recon.privacy`'s allow-list (`run_id`,
#: `records_read`, `records_ok`, `rejected`) so the retention sweep's 90-day
#: anonymisation of `error_detail` leaves the arithmetic legible.
_REJECTED_AT_LEDGER_SCOPE = """
        (SELECT COALESCE(sum(CASE
                    WHEN jsonb_typeof(contrib.value -> 'rejected') = 'number'
                    THEN (contrib.value ->> 'rejected')::bigint
                    ELSE 1 END), 0)
           FROM source_generations AS ledger
           CROSS JOIN LATERAL
                jsonb_each(COALESCE(ledger.error_detail -> 'slices', '{}'::jsonb)) AS contrib
          WHERE ledger.source_id = :source_id
            AND ledger.entity_type = :entity_type
            AND ledger.generation = :generation)
"""

#: Records this slice's contribution and **nothing else**. `loaded_count`,
#: `rejected_count` and `complete` are deliberately absent from the `DO UPDATE`
#: list: they are measured, not asserted, and `_reconcile_ledger_statement` is
#: the only statement in the module allowed to write them.
_LEDGER_CONTRIBUTION = text(
    """
    INSERT INTO source_generations
        (source_id, generation, entity_type, expected_count, loaded_count,
         rejected_count, complete, run_id, error_detail, updated_at)
    VALUES
        (:source_id, :generation, :entity_type, :expected_count, 0, 0, false,
         :run_id, CAST(:error_detail AS jsonb), now())
    ON CONFLICT (source_id, generation, entity_type) DO UPDATE SET
        -- An expectation, once recorded, is not erased by a writer that did not
        -- look one up: the HTTP path carries no manifest, and letting it null the
        -- file path's expected_count would make a completed generation unprovable.
        expected_count = COALESCE(EXCLUDED.expected_count, source_generations.expected_count),
        run_id = EXCLUDED.run_id,
        -- The latest load's structured error at the top level (SS5.3 readers and
        -- `tests/ingest/test_bounded_failure.py` read `error_detail['status']`),
        -- with every contributing load's own detail preserved under `slices`.
        error_detail = jsonb_set(
            COALESCE(EXCLUDED.error_detail, '{}'::jsonb) - 'slices',
            '{slices}',
            COALESCE(source_generations.error_detail -> 'slices', '{}'::jsonb)
                || COALESCE(EXCLUDED.error_detail -> 'slices', '{}'::jsonb)
        ),
        updated_at = now()
    """
)


@lru_cache(maxsize=16)
def _reconcile_ledger_statement(table: str | None) -> TextClause:
    """Re-measure the whole row from the database and recompute `complete`.

    Three measurements, all taken at the row's own scope and none of them a
    number Python carried in:

    `loaded_count`
        `count(DISTINCT natural_key)` of the generation's landing rows. Distinct
        keys rather than rows because `raw_records` is append-only: re-asserting a
        slice legitimately lands a second copy of every record, and the ledger
        describes how much of the generation exists, not how often it arrived.
    `visible`
        `count(*)` of the `stg_*` slice -- what `rules/*.sql` can actually see --
        or the literal `0` for a pair with no staging table, since no rule reads
        `raw_records`. `_check_staging` has already asserted that this equals the
        distinct landed keys for every staged pair, so a row where they disagree
        never reaches this statement.
    `rejected_count`
        the sum over the contribution map (see `_REJECTED_AT_LEDGER_SCOPE`).

    `complete` is then computed **in this statement** from those numbers and the
    row's own `expected_count`. There is no parameter for it, which is the point:
    a writer cannot state a verdict, only supply a measurement.
    """
    visible = (
        "0"
        if table is None
        else (
            f'(SELECT count(*) FROM "{table}" '
            "WHERE source_id = :source_id AND generation = :generation)"
        )
    )
    return text(
        f"""
        WITH measured AS (
            SELECT
                (SELECT count(DISTINCT natural_key) FROM raw_records
                  WHERE source_id = :source_id
                    AND entity_type = :entity_type
                    AND generation = :generation) AS loaded,
                {visible} AS visible,
                {_REJECTED_AT_LEDGER_SCOPE} AS rejected
        )
        UPDATE source_generations AS sg
           SET loaded_count = m.loaded,
               rejected_count = m.rejected,
               complete = (sg.expected_count IS NOT NULL
                           AND m.loaded = sg.expected_count
                           AND m.visible = sg.expected_count),
               updated_at = now()
          FROM measured AS m
         WHERE sg.source_id = :source_id
           AND sg.entity_type = :entity_type
           AND sg.generation = :generation
        RETURNING sg.expected_count, sg.loaded_count, sg.rejected_count,
                  sg.complete, m.visible AS visible
        """
    )


def stamp_ledger(
    conn: Connection, load: LoadResult, *, run_id: str, error: AdapterError | None = None
) -> LedgerVerdict:
    """Contribute this load to its generation's ledger row and return the row.

    Two statements, in this order and inside the caller's transaction: record
    what this slice did, then re-measure the whole row and recompute `complete`
    from the measurements. A slice never writes a count that describes only
    itself, and never writes `complete` at all.
    """
    key = load_key(run_id, load.source_id, load.entity_type, load.generation)
    problem = error.problem() if error is not None else {}
    detail = {
        **problem,
        "slices": {
            key: {
                "run_id": run_id,
                "records_read": load.read,
                "records_ok": load.loaded,
                "rejected": load.rejected,
                "error": problem or None,
            }
        },
    }
    scope = {
        "source_id": load.source_id,
        "entity_type": load.entity_type,
        "generation": load.generation,
    }
    conn.execute(
        _LEDGER_CONTRIBUTION,
        {
            **scope,
            "expected_count": load.expected,
            "run_id": run_id,
            "error_detail": json.dumps(detail),
        },
    )
    spec = STAGING.get((load.source_id, load.entity_type))
    row = conn.execute(
        _reconcile_ledger_statement(None if spec is None else spec.table), scope
    ).one()
    verdict = LedgerVerdict(
        expected=row.expected_count,
        loaded=row.loaded_count,
        rejected=row.rejected_count,
        visible=row.visible,
        complete=row.complete,
    )
    twin = ledger_complete(verdict.expected, verdict.loaded, verdict.visible)
    if twin != verdict.complete:
        # The SQL and the Python spelling of `LEDGER_COMPLETE_RULE` disagree.
        # Loud, because a silent disagreement is exactly the drift that lets a
        # generation be called complete on one path and incomplete on the other.
        raise IngestAccountingError(
            f"{LEDGER_COMPLETE_RULE} -- the database and `ledger_complete` disagree for "
            f"{load.source_id}/{load.entity_type} generation {load.generation}: "
            f"database={verdict.complete} python={twin} "
            f"(expected={verdict.expected} loaded={verdict.loaded} visible={verdict.visible})"
        )
    return verdict


#: The same shape one table down: `ingest_runs` is keyed `(run_id, source_id)`,
#: so its counts are measured over that pair. `records_ok = EXCLUDED.records_ok`
#: discarded the first entity type's count whenever two of them shared a run id --
#: the blocker's scope mismatch, one table over.
_RUN_CONTRIBUTION = text(
    """
    INSERT INTO ingest_runs
        (run_id, source_id, generation, status, started_at, finished_at,
         records_ok, records_rejected, error_detail)
    VALUES
        (:run_id, :source_id, :generation, CAST(:status AS ingest_status), now(), now(),
         0, 0, CAST(:error_detail AS jsonb))
    ON CONFLICT (run_id, source_id) DO UPDATE SET
        generation = EXCLUDED.generation,
        -- The worst status any load of this run reported, never the last one:
        -- a second entity type finishing cleanly does not un-fail the first.
        status = (CASE
            WHEN 'failed' IN (ingest_runs.status::text, EXCLUDED.status::text) THEN 'failed'
            WHEN 'partial' IN (ingest_runs.status::text, EXCLUDED.status::text) THEN 'partial'
            WHEN 'running' IN (ingest_runs.status::text, EXCLUDED.status::text) THEN 'running'
            ELSE 'ok' END)::ingest_status,
        finished_at = now(),
        error_detail = jsonb_set(
            COALESCE(EXCLUDED.error_detail, '{}'::jsonb) - 'loads',
            '{loads}',
            COALESCE(ingest_runs.error_detail -> 'loads', '{}'::jsonb)
                || COALESCE(EXCLUDED.error_detail -> 'loads', '{}'::jsonb)
        )
    """
)

_RUN_RECONCILE = text(
    """
    WITH measured AS (
        SELECT
            (SELECT count(*) FROM raw_records
              WHERE run_id = :run_id AND source_id = :source_id) AS records_ok,
            (SELECT COALESCE(sum(CASE
                        WHEN jsonb_typeof(contrib.value -> 'rejected') = 'number'
                        THEN (contrib.value ->> 'rejected')::bigint
                        ELSE 1 END), 0)
               FROM ingest_runs AS run
               CROSS JOIN LATERAL
                    jsonb_each(COALESCE(run.error_detail -> 'loads', '{}'::jsonb)) AS contrib
              WHERE run.run_id = :run_id AND run.source_id = :source_id) AS records_rejected
    )
    UPDATE ingest_runs AS r
       SET records_ok = m.records_ok,
           records_rejected = m.records_rejected
      FROM measured AS m
     WHERE r.run_id = :run_id AND r.source_id = :source_id
    RETURNING r.records_ok, r.records_rejected, r.status
    """
)


def stamp_run(
    conn: Connection,
    *,
    run_id: str,
    source_id: str,
    generation: int,
    status: str,
    loads: Sequence[LoadResult],
    detail: Mapping[str, Any] | None = None,
) -> RunVerdict:
    """Contribute this call to the `(run_id, source_id)` run row and return the row.

    `records_ok` is `count(*)` of the landing rows carrying this run id and
    source -- the row's own scope, read back from the database. It is therefore
    additive across entity types by construction and idempotent under a replay
    (a replayed load writes no rows, so the count does not move), where summing
    `len(accepted)` in Python was neither.
    """
    contributions = {
        load_key(run_id, load.source_id, load.entity_type, load.generation): {
            "records_read": load.read,
            "records_ok": load.loaded,
            "rejected": load.rejected,
        }
        for load in loads
    }
    document = {**(dict(detail) if detail else {}), "loads": contributions}
    conn.execute(
        _RUN_CONTRIBUTION,
        {
            "run_id": run_id,
            "source_id": source_id,
            "generation": generation,
            "status": status,
            "error_detail": json.dumps(document),
        },
    )
    row = conn.execute(_RUN_RECONCILE, {"run_id": run_id, "source_id": source_id}).one()
    return RunVerdict(
        records_ok=row.records_ok, records_rejected=row.records_rejected, status=row.status
    )


# ======================================================================================
# the pipeline
# ======================================================================================


def _entity_types_of(
    adapter: ReadOnlyAdapter, seen: Iterable[str], rejected: Iterable[str] = ()
) -> tuple[str, ...]:
    """Every entity type this load must account for, in a deterministic order.

    The declared types come first (so a type that produced nothing still gets a
    ledger row saying so -- an empty snapshot is a fact, not an absence), then any
    type that actually turned up and was not declared.

    That second half is the fix for a real silent skip: returning only the
    *declared* types drops every record of an undeclared type on the floor -- not
    landed, not counted as ok, not counted as rejected, not logged, while the
    source still reported `status=ok, complete=true`. A type nobody declared is a
    reason to report loudly, never a reason to stop counting.
    """
    declared = getattr(adapter, "entity_types", None) or SOURCE_ENTITY_TYPES.get(
        getattr(adapter, "source_id", ""), ()
    )
    ordered = list(declared)
    for entity_type in (*seen, *rejected):
        if entity_type not in ordered:
            ordered.append(entity_type)
    return tuple(ordered)


def _check_source_accounting(
    source_id: str,
    generation: int,
    loads: Sequence[LoadResult],
    *,
    read_total: int,
    rejected_total: int,
) -> None:
    """Assert the invariant per load *and* over the source as a whole.

    The per-load check catches a load whose write was skipped. It cannot catch a
    whole *bucket* that never became a load at all -- an entity type that fell out
    of the list -- because the missing bucket takes its own numbers with it. So the
    source-level check compares the sum of the per-type reads against the counter
    incremented in the read loop itself: a record cannot be lost without the
    totals disagreeing.
    """
    for load in loads:
        load.check()
    counted = sum(load.read for load in loads)
    if counted != read_total + rejected_total:
        accounted = ", ".join(f"{load.entity_type}={load.read}" for load in loads) or "<none>"
        raise IngestAccountingError(
            f"{_ACCOUNTING} -- violated by {source_id} generation {generation}: the "
            f"read loop produced {read_total} records and {rejected_total} rejections "
            f"({read_total + rejected_total} in total) but only {counted} are "
            f"accounted for across the entity types [{accounted}]; "
            f"{read_total + rejected_total - counted} record(s) were dropped"
        )


def ingest_source(
    adapter: ReadOnlyAdapter,
    generation: int,
    *,
    run_id: str,
    conn: Connection | None = None,
    expected: Mapping[tuple[str, str, int], int] | None = None,
    stall_timeout: float = ADAPTER_STALL_TIMEOUT_SECONDS,
    deadline_seconds: float | None = ADAPTER_LOAD_DEADLINE_SECONDS,
    persist: bool = True,
) -> SourceResult:
    """Ingest one source's generation-N snapshot: read, land, stage, stamp the ledgers.

    Returns a `SourceResult` even when the source failed -- a failed read is a
    *result*, not an exception to the caller, because the completeness ledger and
    the run row have to be written either way.
    """
    if conn is None and persist:
        with role_connection(ROLE_RECON_WRITER) as owned:
            return ingest_source(
                adapter,
                generation,
                run_id=run_id,
                conn=owned,
                expected=expected,
                stall_timeout=stall_timeout,
                deadline_seconds=deadline_seconds,
                persist=persist,
            )

    source_id = getattr(adapter, "source_id", "unknown")
    expected = expected or {}
    started = time.monotonic()

    rejections: list[AdapterError] = []
    has_sink = hasattr(adapter, "on_reject")
    previous_sink = getattr(adapter, "on_reject", None)
    if has_sink:
        adapter.on_reject = rejections.append  # type: ignore[attr-defined]

    by_type: dict[str, list[RawRecord]] = {}
    #: Counted here, in the loop, and never re-derived from `by_type`. It is the
    #: independent side of the accounting invariant: if a record goes into no
    #: bucket, this number still saw it.
    read_total = 0
    error: AdapterError | None = None
    try:
        for record in read_bounded(
            adapter,
            generation,
            stall_timeout=stall_timeout,
            deadline_seconds=deadline_seconds,
        ):
            read_total += 1
            by_type.setdefault(record.entity_type, []).append(record)
    except AdapterError as exc:
        error = exc
        log.error("ingest.source_failed", run_id=run_id, **exc.log_fields())
    finally:
        if has_sink:
            adapter.on_reject = previous_sink  # type: ignore[attr-defined]

    for rejection in rejections:
        log.warning("ingest.record_rejected", run_id=run_id, **rejection.log_fields())

    rejected_by_type: dict[str, int] = {}
    for rejection in rejections:
        key = rejection.entity_type or "unknown"
        rejected_by_type[key] = rejected_by_type.get(key, 0) + 1

    loads: list[LoadResult] = []
    for entity_type in _entity_types_of(adapter, by_type, rejected_by_type):
        records = by_type.get(entity_type, [])
        rejected = rejected_by_type.get(entity_type, 0)
        expected_count = expected.get((source_id, entity_type, generation))
        staged = (source_id, entity_type) in STAGING

        # The landing write comes FIRST and its own row count is what gets
        # reported. `len(records)` is what the source offered; `landed` is what the
        # database confirms it holds, and only the second one is evidence.
        landing = _land_records(
            conn,
            records,
            source_id=source_id,
            entity_type=entity_type,
            generation=generation,
            run_id=run_id,
            persist=persist,
            staged=staged,
        )
        landed = landing.landed

        # What a rule can actually SEE of this generation, measured in the
        # database after the write and checked against the landing table. The
        # completeness ledger describes the generation, so this -- not one
        # slice's row count -- is what `expected_count` is compared against.
        staged_count: int | None = None
        if persist and conn is not None and staged:
            staged_count = _check_staging(conn, source_id, entity_type, generation)

        load = LoadResult(
            source_id=source_id,
            entity_type=entity_type,
            generation=generation,
            read=len(records) + rejected,
            loaded=landed,
            rejected=rejected,
            expected=expected_count,
            # Provisional, and only ever final on the dry-run path: when there is
            # a database the ledger row decides, and `stamp_ledger` below replaces
            # this with the verdict the database computed. `LEDGER_COMPLETE_RULE`
            # is one rule with two spellings and `stamp_ledger` refuses a
            # disagreement, so the provisional value is the same value.
            complete=False,
            staged=staged,
            staged_count=staged_count,
        )
        loads.append(replace(load, complete=ledger_complete(expected_count, landed, load.visible)))

    # Before anything is reported: every record the source produced is either in
    # the landing table or in the rejection count. This runs on the success path,
    # not only when something looks wrong, because the failure it guards against
    # is precisely the one that looks fine.
    _check_source_accounting(
        source_id,
        generation,
        loads,
        read_total=read_total,
        rejected_total=len(rejections),
    )

    # The ledger is stamped BEFORE the status is decided, because the status is a
    # statement about the ledger: `complete` is measured in the database out of
    # what the generation now holds, and a slice that only added half of it must
    # not be able to call the source `ok`.
    if persist and conn is not None:
        loads = [
            replace(load, complete=verdict.complete, ledger=verdict)
            for load, verdict in (
                (load, stamp_ledger(conn, load, run_id=run_id, error=error)) for load in loads
            )
        ]

    if error is not None:
        status = "failed" if not any(load.loaded for load in loads) else "partial"
    elif rejections or any(load.known_short for load in loads):
        status = "partial"
    else:
        status = "ok"

    result = SourceResult(
        source_id=source_id,
        generation=generation,
        run_id=run_id,
        status=status,
        loads=tuple(loads),
        rejections=tuple(rejections),
        error=error,
        elapsed_ms=(time.monotonic() - started) * 1000.0,
    )

    if persist and conn is not None:
        stamp_run(
            conn,
            run_id=run_id,
            source_id=source_id,
            generation=generation,
            status=status,
            loads=loads,
            detail={
                "error": error.problem() if error is not None else None,
                "rejections": [rejection.problem() for rejection in rejections[:50]],
            }
            if (error is not None or rejections)
            else None,
        )

    log.info(
        "ingest.source_done",
        run_id=run_id,
        source=source_id,
        generation=generation,
        status=status,
        records_read=result.records_read,
        records_ok=result.records_ok,
        records_rejected=result.records_rejected,
        complete=result.complete,
        elapsed_ms=round(result.elapsed_ms, 3),
    )
    return result


def _remaining_deadline(
    deadline_seconds: float | None,
    sync_deadline_at: float | None,
    *,
    source_id: str,
    generation: int,
    run_id: str,
) -> float | None:
    """The deadline for the next load: its own bound, capped by the sync budget.

    A spent budget yields `0.0`, not a skip. `read_bounded` turns a zero deadline
    into the ordinary `source_timeout` `AdapterError`, so the source still gets a
    `SourceResult`, a ledger row with `complete = false` and a structured error
    saying how long we waited -- which is what R3 asks for. Dropping the source
    from the loop instead would be a silent skip wearing a timeout's clothes.
    """
    if sync_deadline_at is None:
        return deadline_seconds
    remaining = max(sync_deadline_at - time.monotonic(), 0.0)
    if remaining <= 0.0:
        log.error(
            "ingest.sync_budget_exhausted",
            run_id=run_id,
            source=source_id,
            generation=generation,
            detail=(
                "the cumulative sync budget is spent; this load is given a zero "
                "deadline and fails with a structured source_timeout"
            ),
        )
    return remaining if deadline_seconds is None else min(deadline_seconds, remaining)


def ingest_generation(
    adapters: Mapping[str, ReadOnlyAdapter],
    generation: int,
    *,
    run_id: str,
    expected: Mapping[tuple[str, str, int], int] | None = None,
    conn: Connection | None = None,
    persist: bool = True,
    stall_timeout: float = ADAPTER_STALL_TIMEOUT_SECONDS,
    deadline_seconds: float | None = ADAPTER_LOAD_DEADLINE_SECONDS,
    sync_deadline_at: float | None = None,
) -> IngestReport:
    """Ingest generation N from every adapter. One `ingest_runs` row per source.

    `sync_deadline_at` is an absolute `time.monotonic()` instant: the moment the
    whole sync's budget runs out. Each source is given whichever is smaller, its
    own per-load deadline or what is left of the budget, so the sequence cannot
    outrun the budget by a factor of however many sources there happen to be.
    """
    started = time.monotonic()
    results: list[SourceResult] = []
    for _, adapter in sorted(adapters.items()):
        results.append(
            ingest_source(
                adapter,
                generation,
                run_id=run_id,
                conn=conn,
                expected=expected,
                persist=persist,
                stall_timeout=stall_timeout,
                deadline_seconds=_remaining_deadline(
                    deadline_seconds,
                    sync_deadline_at,
                    source_id=getattr(adapter, "source_id", "unknown"),
                    generation=generation,
                    run_id=run_id,
                ),
            )
        )
    return IngestReport(tuple(results), (time.monotonic() - started) * 1000.0)


def ingest_all(
    adapters: Mapping[str, ReadOnlyAdapter] | None = None,
    generations: Sequence[int] | None = None,
    *,
    run_id: str,
    root: Path | str | None = None,
    persist: bool = True,
    conn: Connection | None = None,
    stall_timeout: float = ADAPTER_STALL_TIMEOUT_SECONDS,
    deadline_seconds: float | None = ADAPTER_LOAD_DEADLINE_SECONDS,
    sync_budget_seconds: float | None = SYNC_BUDGET_SECONDS,
) -> IngestReport:
    """Ingest every generation, oldest first, inside one cumulative time budget.

    Order matters: SS7's snapshots are cumulative history and R4's A -> B -> A scan
    reads `generation` ascending, so a later snapshot must land *after* the one it
    supersedes -- as new rows, never over them.

    `sync_budget_seconds` bounds the **whole** sequence (R3). The per-load deadline
    alone bounds one load, and this runs three sources x three generations of them
    back to back: nine wedged loads at the 300s per-load default is 45 minutes
    before a caller hears anything, which satisfies "never hang a sync" only in
    the letter. Every load past the budget still returns its own structured
    `source_timeout` result and its own ledger row. `None` disables the cap (the
    benchmark, which is measuring, not syncing).
    """
    adapters = adapters if adapters is not None else build_adapters(root)
    expected = expected_counts_from_manifest(root)
    if generations is None:
        available: set[int] = set()
        for adapter in adapters.values():
            available.update(adapter.generations())
        generations = sorted(available)

    started = time.monotonic()
    sync_deadline_at = None if sync_budget_seconds is None else started + sync_budget_seconds
    results: list[SourceResult] = []
    for generation in generations:
        report = ingest_generation(
            adapters,
            generation,
            run_id=f"{run_id}-gen{generation}",
            expected=expected,
            conn=conn,
            persist=persist,
            stall_timeout=stall_timeout,
            deadline_seconds=deadline_seconds,
            sync_deadline_at=sync_deadline_at,
        )
        results.extend(report.sources)
    return IngestReport(tuple(results), (time.monotonic() - started) * 1000.0)


# ======================================================================================
# HTTP: validated ingestion of a literal payload batch (R2)
# ======================================================================================

router = APIRouter(prefix="/internal/ingest", tags=["ingest"])

#: `/internal/ingest/*` is driven by the scheduled **sync** job, so it is the sync
#: job's secret that authenticates it (DESIGN pins one secret per job).
#:
#: This module used to answer "is a configured secret usable?" itself, in a second
#: copy of the check that lived next to the endpoint. The copies diverged: this one
#: treated a whitespace-only value as unconfigured (correct -- deny), while
#: `recon.api.auth` asked `if not configured`, which is `False` for `"   "`, and so
#: authenticated any caller who presented three spaces. Both endpoints of the same
#: job now resolve through `recon.api.auth`, which is the only module that reads a
#: trigger-secret setting or compares one.
_TRIGGER_JOB: str = JOB_SYNC


# ======================================================================================
# the REQUEST is bounded, not only the records inside it
# ======================================================================================

#: Environment variable that overrides :data:`DEFAULT_MAX_BODY_BYTES`. Read at
#: call time (:func:`max_body_bytes`), so a deploy can tighten the bound without a
#: rebuild and a test can move it without reaching into module state.
MAX_BODY_BYTES_ENV: str = "KEYSTONE_MAX_BODY_BYTES"

#: The largest request body `/internal/*` will read, in bytes: **16 MiB**, which
#: is `64 * MAX_PAYLOAD_BYTES`.
#:
#: Sized against real traffic rather than chosen as a round number. This endpoint
#: takes *a snapshot slice as literal payload strings*, and the largest slice the
#: committed manifest expects is `crm.contact` at 40,075 records --
#: `fixtures/crm/gen1/contact.jsonl`, 13,052,723 bytes on disk. A cap under that
#: would refuse a legitimate load, which is an outage dressed as a control;
#: `tests/ingest/test_request_size_bound.py` measures both numbers off the
#: committed tree so a grown dataset fails the test instead of the deploy.
DEFAULT_MAX_BODY_BYTES: int = 64 * MAX_PAYLOAD_BYTES

#: The most records one batch may carry. Above the largest committed slice
#: (40,075) for the same reason, and below "however many fit" -- see
#: `RecordsRequest.records`.
MAX_RECORDS_PER_BATCH: int = 50_000


def max_body_bytes() -> int:
    """The configured request-body cap, in bytes.

    Read from the environment on every call rather than frozen at import: the
    process that serves and the process that seeds are the same image, and a cap
    baked in at import cannot be tightened by a deploy or moved by a test.

    **An unusable value is the default, never "no limit".** `""`, `"   "`,
    `"many"`, `"0"` and `"-1"` are all operator errors of one kind, and the way
    this fails must not be the way the bound disappears -- a control that a typo
    silently removes is worse than no control, because the route table still
    shows it.
    """
    raw = (os.environ.get(MAX_BODY_BYTES_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_BODY_BYTES
    try:
        configured = int(raw)
    except ValueError:
        log.warning("ingest.max_body_bytes_unusable", value_length=len(raw), status="default")
        return DEFAULT_MAX_BODY_BYTES
    if configured <= 0:
        log.warning("ingest.max_body_bytes_unusable", value_length=len(raw), status="default")
        return DEFAULT_MAX_BODY_BYTES
    return configured


class OversizedBody(bytes):
    """What :func:`raw_request_body` returns instead of a body it refused to read.

    **A `bytes` subclass, deliberately empty.** The dependency's contract stays
    exactly `bytes`, so the three handlers that take it keep their signatures and
    a handler that forgets to ask sees an *empty* body -- 422 on an absent
    envelope -- rather than a truncated one it might parse as if it were whole.
    Silently parsing a prefix of a refused request is the failure mode a
    `bytes`-typed sentinel of any other shape would invite.

    `limit` and `bytes_read` are carried for the problem document: "how big may
    it be" is the actionable half of a 413, and "how much did we take before
    stopping" is what says the read was abandoned rather than completed.
    """

    limit: int
    bytes_read: int
    declared: int | None

    def __new__(cls, *, limit: int, bytes_read: int, declared: int | None) -> OversizedBody:
        self = super().__new__(cls)
        self.limit = limit
        self.bytes_read = bytes_read
        self.declared = declared
        return self


def _declared_length(raw: str | None) -> int | None:
    """`Content-Length` as an int, or `None` when it is absent or unusable.

    Unusable is `None` and not a refusal: a header this service cannot parse is
    a fact about the header, and the stream bound below is what actually decides.
    """
    if raw is None:
        return None
    try:
        declared = int(raw.strip())
    except ValueError:
        return None
    return declared if declared >= 0 else None


class RecordsRequest(BaseModel):
    """A snapshot slice as **literal payload strings**.

    Strings, not objects: contract SS7's corpus stores `raw` as the literal line so
    truncated JSON and non-object lines are representable, and an endpoint that
    only accepted parsed objects could not be driven by half of that corpus.

    The envelope is bounded by the same rule as the payloads: every field is
    checked against the column it lands in. `generation` becomes
    `raw_records.generation`, a Postgres `integer`; `run_id` becomes `run_id` and
    `load_id`, both `text`, which cannot hold a NUL. Unbounded, either one is a
    well-formed request that 500s at the write.
    """

    source: str
    entity_type: str
    generation: int = Field(ge=1, le=INT32_MAX)
    #: Bounded by :data:`MAX_RECORDS_PER_BATCH`, in `_records_within_the_batch_cap`
    #: below. An unbounded `list[str]` accepted a 2,000,000-element batch without
    #: complaint -- measured -- and every one of those elements is then validated,
    #: held, and potentially reported in `problems`.
    records: list[str]
    #: Validated by `recon.adapters.identifiers`, the ONE identifier rule -- not
    #: by a copy of it living here. Three modules used to answer this question
    #: three different ways, and the divergence produced two 5xx and a
    #: silently-accepted control character; see that module's table.
    run_id: str | None = None
    persist: bool = True

    @field_validator("records")
    @classmethod
    def _records_within_the_batch_cap(cls, value: list[str]) -> list[str]:
        """Refuse a batch of more than :data:`MAX_RECORDS_PER_BATCH` records.

        **A validator rather than a `Field` constraint**, for two unrelated
        reasons, and the first one is the load-bearing one:
        `tests/triggers/test_identifier_rule.py` refuses any module outside
        `recon.adapters.identifiers` that declares a length bound on a model
        field, because `run_id` once carried half the identifier rule in two
        envelopes that then disagreed about the other half. A list-length cap on
        `records` is not an identifier rule, but the check that keeps that
        property true reads the source, and a rule enforced by reading the source
        is one you do not get to argue with -- so this is spelled as a check on
        the value instead of as a constraint on the field. The second reason is
        that it can say what the bound *is*, which a bare constraint cannot.

        **What this bounds, honestly.** Not peak parse memory: `parse_body` has
        already run `json.loads` over the whole body by the time any validator
        fires, so the objects exist. :func:`max_body_bytes` is the bound on
        *that*, and it is the one to tighten if the concern is the interpreter's
        heap. This bounds what the endpoint then goes on to **do and retain** --
        `validate_batch` over every element, a rejection object per failure, and
        a `problems` array in the response -- which is the term that scales with
        record count rather than with bytes.
        """
        if len(value) > MAX_RECORDS_PER_BATCH:
            raise ValueError(
                f"a batch carries at most {MAX_RECORDS_PER_BATCH} records "
                f"(this one carries {len(value)}); post the slice in several batches"
            )
        return value


def problem_response(
    problem: Mapping[str, Any], status: int, extra: Mapping[str, Any] | None = None
) -> JSONResponse:
    """An RFC7807 body with the problem media type. Public: `recon.api.internal`
    renders the same document shape, and two spellings of one contract is how the
    two endpoints of one job came to disagree in the first place."""
    body = dict(problem)
    body["status"] = status
    if extra:
        body.update(extra)
    return JSONResponse(status_code=status, content=body, media_type="application/problem+json")


_problem_response = problem_response


async def raw_request_body(request: Request) -> bytes:
    """The request body, unparsed and **bounded**. A dependency, so the handler
    stays sync.

    This is what makes "401 before 422" true rather than approximately true. When
    a route declares a Pydantic body parameter, FastAPI reads and decodes the body
    *before* it solves any dependency, and a JSON decode error is raised on the
    spot -- so an unauthenticated caller sending `{` got a 422 describing the
    envelope, on every one of the three mutating endpoints, and R19 says a request
    without the header is 401. Taking the body as bytes leaves the route with no
    body field at all: nothing is parsed until the handler has already
    authenticated. Reading the bytes leaks nothing; parsing them does.

    **The cap is enforced while reading, not after.** This used to be
    `await request.body()`, which buffers whatever arrives: a 256 KiB bound
    existed per *record* and nothing at all bounded the *request*. Measured
    against the unbounded version, one 64 MiB POST answered `200` and cost 192
    MiB of Python heap and 404 MiB RSS -- and because the body is read here,
    before the handler authenticates, an anonymous caller paid that cost and was
    then told 401. A check written after `await request.body()` returns the same
    413 and bounds nothing, so the read itself is what stops: the stream is
    consumed chunk by chunk and abandoned the moment the total exceeds the cap.
    A truthful `Content-Length` over the cap is refused before a single chunk is
    pulled; a lying one is caught by the same running total, so the header is an
    optimisation and never the decision.

    The verdict is *carried*, not raised: FastAPI renders an `HTTPException` as
    `{"detail": ...}`, and DESIGN pins RFC7807 for every refusal. The handler
    turns an :class:`OversizedBody` into that document with
    :func:`oversized_body_problem`, **after** the 401 check, so the cap is not an
    oracle an unauthenticated caller can probe.

    Async, while the handler stays `def`: the handler runs in the threadpool
    (every write below is blocking), and only this coroutine touches the loop.
    """
    limit = max_body_bytes()
    declared = _declared_length(request.headers.get("content-length"))
    if declared is not None and declared > limit:
        log.warning(
            "ingest.body_too_large", limit_bytes=limit, declared_bytes=declared, bytes_read=0
        )
        return OversizedBody(limit=limit, bytes_read=0, declared=declared)

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            log.warning(
                "ingest.body_too_large",
                limit_bytes=limit,
                declared_bytes=declared,
                bytes_read=size,
            )
            # `chunks` is dropped on return; nothing over the cap is retained,
            # and the rest of the stream is never pulled.
            return OversizedBody(limit=limit, bytes_read=size, declared=declared)
        chunks.append(chunk)
    return b"".join(chunks)


def oversized_body_problem(body: bytes) -> JSONResponse | None:
    """The 413 an over-cap request earns, or `None` when the body is fine.

    413 rather than a new status: `oversized_body` is already the committed name
    for this breakage and already carries 413 in
    `recon.seed.malformed.EXPECT_CODES`, which the adapter imports rather than
    restates. One vocabulary covers a 256 KiB record and a 16 MiB request, so an
    operator reading the log does not have to learn two.

    The body is never echoed and never even parsed -- a refused request is
    exactly the place a secret gets pasted by accident, and this document is also
    what gets logged. Only the two numbers that make the refusal actionable go
    out.
    """
    if not isinstance(body, OversizedBody):
        return None
    return problem_response(
        {
            "type": "https://keystone.invalid/problems/oversized_body",
            "title": "request body too large",
            "detail": (
                f"the request body exceeds the {body.limit}-byte cap and was not read; "
                "post the slice in smaller batches "
                f"(the cap is configurable as {MAX_BODY_BYTES_ENV})"
            ),
        },
        KIND_STATUS["oversized_body"],
        {"limit_bytes": body.limit},
    )


def parse_body[M: BaseModel](body: bytes, model: type[M]) -> tuple[M | None, JSONResponse | None]:
    """`(parsed, None)` or `(None, 422)`. Runs only after authentication.

    Renders the same RFC7807 document FastAPI's own validation handler renders,
    and -- like it -- never echoes the rejected values: the body of a refused
    request is exactly the place a secret gets pasted by accident.
    """
    if not body.strip():
        payload: Any = {}
    else:
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError) as exc:
            return None, problem_response(
                {
                    "type": "https://keystone.invalid/problems/invalid_request",
                    "title": "invalid request",
                    "detail": f"the request body is not JSON: {type(exc).__name__}",
                },
                422,
            )
    if not isinstance(payload, dict):
        return None, problem_response(
            {
                "type": "https://keystone.invalid/problems/invalid_request",
                "title": "invalid request",
                "detail": f"the request body is a JSON {type(payload).__name__}, not an object",
            },
            422,
        )
    try:
        return model.model_validate(payload), None
    except ValidationError as exc:
        errors = [
            {"loc": [str(part) for part in error.get("loc", ())], "type": error.get("type", "")}
            for error in exc.errors()[:10]
        ]
        return None, problem_response(
            {
                "type": "https://keystone.invalid/problems/invalid_request",
                "title": "invalid request",
                "detail": (
                    f"the request body failed validation in {len(exc.errors())} place(s); "
                    "the rejected values are deliberately not echoed"
                ),
                "errors": errors,
            },
            422,
        )


def identifier_problem(exc: IdentifierError) -> JSONResponse:
    """The 4xx an unusable identifier earns, rendered identically everywhere.

    Same status, same problem type, same rule text, whichever endpoint refused
    it -- which is the whole point of there being one validator. The offending
    value is not echoed: it may be the very string the store cannot hold, and
    this document is also what gets logged.
    """
    return problem_response(
        {
            "type": "https://keystone.invalid/problems/invalid_identifier",
            "title": "invalid identifier",
            "detail": str(exc),
            "field": exc.field,
            "rule": IDENTIFIER_RULE,
        },
        422,
    )


def _authorize(secret: str | None) -> JSONResponse | None:
    """`None` when the caller is the scheduler, else a 401 problem (R19).

    **A delegation, not an implementation.** The whole decision -- which settings
    field holds the sync job's secret, whether a configured value is usable, and
    the constant-time comparison itself -- belongs to `recon.api.auth`, and this
    module deliberately holds no copy of any of it. See `_TRIGGER_JOB` for the
    divergence that made that non-negotiable.

    **Fail closed.** This endpoint writes to an append-only landing table, and it
    once skipped the comparison entirely when no secret was configured -- so the
    default configuration (every secret `None`) let an anonymous caller write. An
    endpoint whose authentication disappears when it is misconfigured is worse
    than one with none, because it looks protected in code review and in the route
    table, and the failure is invisible until it is used. Unset, empty,
    whitespace-only, absent header and wrong value are all 401, and neither the
    configured secret nor the presented one is ever logged or echoed.
    """
    return trigger_guard(_TRIGGER_JOB, secret)


def audit_internal_fault(run_id: str, *, action: str, detail: Mapping[str, Any]) -> bool:
    """Record an internal ingestion fault in `audit_log` (R18). Never raises.

    The accounting and staging invariants exist because both ways they have been
    broken looked *clean* from the outside, so a violation has to leave a durable
    trace and not only a log line -- a log line is gone with the container. It
    goes through `recon.logging.insert_audit_row`, the redacting chokepoint, on
    its own connection: the load's transaction has already been rolled back by
    the time this runs, which is the point (the fault must not be able to commit
    the load it condemns).

    A failure to audit is itself logged and swallowed. The caller is already
    returning a structured error; turning the audit write into a second exception
    would replace it with the unhandled 500 this exists to prevent.
    """
    try:
        with role_connection(ROLE_RECON_WRITER) as conn:
            insert_audit_row(
                conn,
                actor=AUDIT_ACTOR_INGEST,
                action=action,
                subject=run_id,
                body=dict(detail),
            )
        return True
    except Exception as exc:
        # Auditing must not raise a second fault on top of the first one.
        log.error(
            "ingest.audit_failed",
            run_id=run_id,
            action=action,
            error=f"{type(exc).__name__}: {exc}",
        )
        return False


@router.post("/records")
def ingest_records(
    request: Request,
    body: bytes = Depends(raw_request_body),
    x_trigger_secret: str | None = Header(default=None, alias=TRIGGER_SECRET_HEADER),
) -> JSONResponse:
    """Validate a batch of literal payloads; land the survivors; report the rejects.

    Never 500 and never a silent skip: every input line produces exactly one
    outcome, each rejection is an RFC7807 problem with its own 4xx, and the run row
    counts them. When every rejection carries the same status the response uses it,
    so a single malformed record answers with the status that record earned;
    a mixed batch answers 422 and the per-record statuses live in `problems`.

    **Authentication runs first, before the body is looked at.** R19 says a
    request without the header is 401, and it was 422 for anything with a
    malformed envelope -- which also told an unauthenticated caller the shape of
    the envelope. The size verdict sits in the same place and for the same
    reason: the *bytes* are already refused by :func:`raw_request_body`, which
    stopped reading, but the *answer* to an unauthenticated caller stays 401 so
    the cap cannot be probed without the header.
    """
    unauthorized = _authorize(x_trigger_secret)
    if unauthorized is not None:
        return unauthorized

    too_large = oversized_body_problem(body)
    if too_large is not None:
        return too_large

    payload, invalid = parse_body(body, RecordsRequest)
    if invalid is not None:
        return invalid
    assert payload is not None

    try:
        if payload.run_id is not None:
            run_id = validate_identifier(payload.run_id, field="run_id")
        else:
            # The fallback is client-supplied too -- it is a request header -- and
            # it lands in the same text columns, so it is judged by the same rule.
            run_id = validate_identifier(
                f"http-{request.headers.get('x-request-id', 'anon')}", field="x-request-id"
            )
    except IdentifierError as exc:
        log.warning("ingest.invalid_identifier", field=exc.field, reason=exc.reason, status=422)
        return identifier_problem(exc)

    if (payload.source, payload.entity_type) not in PRIMARY_KEYS:
        return problem_response(
            {
                "type": "https://keystone.invalid/problems/unknown_source",
                "title": "unknown source",
                "detail": (
                    f"{payload.source!r}/{payload.entity_type!r} is not one of "
                    f"{sorted(PRIMARY_KEYS)}"
                ),
            },
            400,
        )

    results = validate_batch(
        payload.source, payload.entity_type, payload.generation, payload.records
    )
    accepted, rejected = partition(results)

    for rejection in rejected:
        log.warning("ingest.record_rejected", run_id=run_id, **rejection.log_fields())

    persisted = False
    storage_error: Exception | None = None
    internal_fault: IngestAccountingError | None = None
    ledger: LedgerVerdict | None = None
    load_id = load_key(run_id, payload.source, payload.entity_type, payload.generation)
    staged = (payload.source, payload.entity_type) in STAGING
    if payload.persist:
        try:
            with role_connection(ROLE_RECON_WRITER) as conn:
                # The advisory lock, the duplicate check and the COPY are ONE
                # decision, taken inside one transaction. Two concurrent requests
                # sharing a run id used to pass this check together -- neither sees
                # the other's uncommitted rows under READ COMMITTED -- and both
                # landed, doubling the append-only mirror every downstream count
                # and every absence test rests on.
                landing = _land_records(
                    conn,
                    accepted,
                    source_id=payload.source,
                    entity_type=payload.entity_type,
                    generation=payload.generation,
                    run_id=run_id,
                    persist=True,
                    staged=staged,
                )
                if landing.replayed:
                    # A run id is client-supplied, so re-using one is a client
                    # error and gets its own 4xx -- and, crucially, is a **no-op**
                    # that reports what already landed. Landing on top of an
                    # existing load would append a second copy of every record
                    # and mis-pair the returned ids with the staging rows built
                    # from them.
                    return problem_response(
                        {
                            "type": "https://keystone.invalid/problems/duplicate_load",
                            "title": "duplicate load",
                            "detail": (
                                f"load {load_id!r} already holds {landing.landed} landed "
                                "records; nothing was written. Use a fresh run_id "
                                "(landing is append-only)"
                            ),
                        },
                        409,
                        {
                            "run_id": run_id,
                            "accepted": 0,
                            "rejected": 0,
                            "persisted": False,
                            "already_landed": landing.landed,
                        },
                    )
                staged_count = (
                    _check_staging(conn, payload.source, payload.entity_type, payload.generation)
                    if staged
                    else None
                )
                expected = expected_counts_from_manifest()
                expected_count = expected.get(
                    (payload.source, payload.entity_type, payload.generation)
                )
                load = LoadResult(
                    source_id=payload.source,
                    entity_type=payload.entity_type,
                    generation=payload.generation,
                    read=len(payload.records),
                    loaded=landing.landed,
                    rejected=len(rejected),
                    expected=expected_count,
                    # Decided by the ledger row below, never here: this endpoint
                    # posts one slice of a generation and a slice is not entitled
                    # to a verdict about the generation.
                    complete=False,
                    staged=staged,
                    staged_count=staged_count,
                )
                # Same invariant as the file path, on the same terms: every line of
                # the request body is landed or rejected. A 200 that quietly landed
                # fewer rows than it accepted is the silent skip R2 forbids.
                load.check()
                ledger = stamp_ledger(conn, load, run_id=run_id)
                stamp_run(
                    conn,
                    run_id=run_id,
                    source_id=payload.source,
                    generation=payload.generation,
                    status="partial" if rejected else "ok",
                    loads=(load,),
                    detail={"rejections": [item.problem() for item in rejected[:50]]}
                    if rejected
                    else None,
                )
            persisted = True
        except DatabaseNotConfigured:
            log.warning("ingest.not_persisted", run_id=run_id, reason="no DATABASE_URL")
        except IngestAccountingError as exc:
            # The accounting/staging invariant did not hold. It is an INTERNAL
            # fault -- no payload can cause it -- so it is logged, audited and
            # returned as a structured error. It used to be an unhandled
            # exception, which made the guard against a silent skip produce the
            # bare 500 it exists to prevent; and the `with` block above has
            # already rolled the load back, so nothing it half-wrote survives.
            internal_fault = exc
            log.error(
                "ingest.accounting_violation",
                run_id=run_id,
                source=payload.source,
                entity_type=payload.entity_type,
                generation=payload.generation,
                # `rule`, not `invariant`: `recon.privacy`'s committed key
                # vocabulary is default-deny, and a key it has not met is emitted
                # as an opaque token -- an unreadable log line is what invites
                # somebody to widen the allow-list under pressure.
                rule=_STAGING if isinstance(exc, IngestStagingError) else _ACCOUNTING,
                detail=str(exc),
            )
            audit_internal_fault(
                run_id,
                action="ingest.accounting_violation",
                detail={
                    "rule": _STAGING if isinstance(exc, IngestStagingError) else _ACCOUNTING,
                    "source": payload.source,
                    "entity_type": payload.entity_type,
                    "generation": payload.generation,
                    "detail": str(exc),
                },
            )
        except (SQLAlchemyError, psycopg.Error) as exc:
            # `psycopg.Error` as well as `SQLAlchemyError`: the landing COPY runs on
            # the raw driver cursor, so a write it refuses raises the driver's own
            # exception, which SQLAlchemy never wraps. Catching only the wrapped
            # kind is why a payload the database could not store escaped as a 500
            # instead of being reported. Validation is what keeps a *payload* out of
            # this branch at all; this is the net under a genuine storage failure.
            #
            # Storage failing is a *server* problem and is reported as one -- but
            # only after the payload verdict. A malformed payload's 4xx is a fact
            # about the payload and must not be replaced by our outage (R2).
            storage_error = exc
            log.error(
                "ingest.storage_failed",
                run_id=run_id,
                detail=f"{type(exc).__name__}: {exc}",
            )

    summary: dict[str, Any] = {
        "run_id": run_id,
        "source": payload.source,
        "entity_type": payload.entity_type,
        "generation": payload.generation,
        "accepted": len(accepted),
        "rejected": len(rejected),
        "persisted": persisted,
    }
    if ledger is not None:
        # The generation this slice joined, as the ledger reports it -- not as
        # this request would like to describe it. A caller assembling a
        # generation out of slices can see whether it is whole yet, and on what
        # arithmetic (`LEDGER_COMPLETE_RULE`).
        summary["generation_loaded"] = ledger.loaded
        summary["generation_rejected"] = ledger.rejected
        summary["generation_expected"] = ledger.expected
        summary["generation_complete"] = ledger.complete
    if not rejected:
        if internal_fault is not None:
            return problem_response(
                {
                    "type": "https://keystone.invalid/problems/accounting_violation",
                    "title": "accounting violation",
                    "detail": (
                        "every payload validated, but the load did not balance and "
                        "was rolled back rather than reported: "
                        f"{internal_fault}"
                    ),
                    "invariant": _STAGING
                    if isinstance(internal_fault, IngestStagingError)
                    else _ACCOUNTING,
                },
                500,
                summary,
            )
        if storage_error is not None:
            return problem_response(
                {
                    "type": "https://keystone.invalid/problems/storage_unavailable",
                    "title": "storage unavailable",
                    "detail": (
                        "every payload validated, but the landing write failed: "
                        f"{type(storage_error).__name__}"
                    ),
                },
                503,
                summary,
            )
        return JSONResponse(status_code=200, content=summary)

    problems = [item.problem() for item in rejected]
    statuses = {item.status for item in rejected}
    if len(statuses) == 1:
        status = statuses.pop()
        head = dict(problems[0])
    else:
        status = 422
        head = {
            "type": "https://keystone.invalid/problems/multiple_rejections",
            "title": "multiple rejections",
            "detail": (
                f"{len(rejected)} of {len(payload.records)} payloads were rejected, "
                f"with statuses {sorted(statuses)}"
            ),
        }
    return problem_response(head, status, {**summary, "problems": problems})
