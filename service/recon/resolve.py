"""Materialization driver: run the committed ER cascade over `stg_*` and persist it.

`recon.er` decides identity; **this module decides nothing**. It loads one
generation's `stg_*` slice into the `Snapshot` shape `recon.er.resolve` already
takes, calls it, and writes the four tables the contract names -- `entity_links`,
`entity_link_candidates`, `entities` and `field_lineage` (SS4.7, SS3). Every rule
(`L1..L3`, `P1..P3`, `E1/E2`, `D2`), every survivorship tiebreak (SS4.6) and every
`person_key` (SS4.1) comes from the committed modules; re-implementing one here is
the R23 drift the shared-module rule exists to prevent.

What the writer is, and why
---------------------------
Writes go through `recon_writer`, which holds INSERT on `entities` and **no**
UPDATE (migration 0004: "the pipeline may APPEND canonical rows, only the guarded
path may MUTATE them"). Two deferred constraint triggers therefore judge every
run, and the write order is arranged so both are satisfied rather than worked
around:

* ``KS008`` -- a canonical row must have an `entity_links` row naming it. **Every**
  person gets at least one link row, including the ones no cascade rule touches (a
  lone app-DB student, a deal-less lead contact, an unattributed payment). Those
  carry `method='anchor'`: the ref is the person's own SS4.1 anchor and no
  `L/P/E/D` rule was involved. Writing `L1` there would be a lie a downstream rule
  reads -- `R-004` takes C4's link method straight off this column.
* ``KS009`` -- an `entity_links` row must name a real `raw_records` row. It does,
  by construction: every ref written here is built by `recon.reference.make_ref`
  from a `stg_*` row, and staging rows exist only for landed records.

One row per source ref, and what that costs
-------------------------------------------
`uq_entity_links_source_generation` is `(generation, source_id, source_key)`, so
the table holds **one winning link per source record per generation** -- its own
comment says so. `recon.er` emits a richer set: a payment carries both its
`P1..P3` person link and its `E1/E2` enrollment link, and a household deal names
two to four siblings. The reduction is deterministic and documented:

* a payment ref keeps its **person** attribution (`P1..P3`); the enrollment
  attribution survives in the canonical view's `link_methods` and is recomputed by
  `recon.er` wherever a rule needs the enrollment itself;
* a deal ref is written against the person the contract already calls the
  household's anchor -- the lowest anchor ref under SS4.1's source preference,
  chosen with `reference.anchor_ref`, never "whichever person came first".

`link_class` has no column here, so it is read off `method`: `L*` is
`contact_student`, `P*` is `payment_person`, `D2` is `deal_person`. `anchor` and
`member` are structural rows, not cascade links, and are never any of those.

The canonical row
-----------------
`entities.current` holds the **unified cross-source view** -- exactly the fields
`golden/expected-views.json` pins (`VIEW_FIELDS`), plus the generation it was
built from and the tenant that owns it. That is what makes `GET /api/entities/{key}`
one indexed primary-key read instead of a nine-table join per request, and it is
why the view a reviewer diffs against the golden file is the *stored* canonical
state rather than something assembled on the way out.

The view is rebuilt here rather than imported from `recon.seed.golden`: the
generator may import detector modules, never the reverse (SS0's layering), so the
two implementations are bound by the committed golden file instead of by an
import. `tests/er/test_expected_views.py` diffs all 25 entries; drift fails there.

Tenancy (R20)
-------------
The fixture org is partitioned into two demo tenants by **household**, so siblings
can never land in different tenants, and the partition is frozen into the canonical
row at materialization time rather than recomputed per request -- adding an API
client later must not silently re-shard rows that already exist. Only
`demo-client` has a committed key (`migrations/0003`), which is what makes
isolation demonstrable: a client key reads its own rows and gets nothing for
`demo-tenant-b`'s, while `admin` reads both.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.engine import Connection

from recon.db import ROLE_RECON_WRITER, role_connection
from recon.er import Person, Resolution, Snapshot, resolve
from recon.logging import get_logger
from recon.normalize import norm_email, norm_enum, norm_name
from recon.reference import (
    anchor_ref,
    canon_value,
    household_key,
    make_ref,
    parse_ref,
    ref_source,
)

__all__ = [
    "CURRENT_GENERATION",
    "LINEAGE_PATHS",
    "LINEAGE_PATH_MAPS",
    "METHOD_ANCHOR",
    "METHOD_MEMBER",
    "SURVIVED_PATHS",
    "TENANT_LABELS",
    "VIEW_FIELDS",
    "MaterializeReport",
    "ResolvedGeneration",
    "candidate_rows",
    "entity_rows",
    "is_materialized",
    "lineage_rows",
    "link_rows",
    "load_snapshot",
    "materialize",
    "person_view",
    "resolve_generation",
    "tenant_for",
]

log = get_logger("recon.resolve")

#: SS3/SS7 -- current state is generation 3; the canonical layer is built from it.
CURRENT_GENERATION: Final = 3

#: The exact key set of one `golden/expected-views.json` entry (SS8, R10). The
#: endpoint's `view` object is this and nothing else, so "matches the golden file"
#: is a dict comparison and not a subset test.
VIEW_FIELDS: Final[tuple[str, ...]] = (
    "anchor_ref",
    "canonical_id",
    "deal_refs",
    "entity_refs",
    "household_key",
    "identity_refs",
    "link_methods",
    "paid",
    "payments",
    "person_key",
    "registered",
    "sources",
    "stage_funnel",
    "survived",
)

#: The nine source-qualified paths a canonical view survives (SS4.6).
SURVIVED_PATHS: Final[tuple[str, ...]] = (
    "appdb.enrollment.program",
    "appdb.enrollment.stage",
    "appdb.student.first_name",
    "appdb.student.grade",
    "appdb.student.last_name",
    "appdb.student.status",
    "crm.contact.email",
    "crm.contact.lifecycle_stage",
    "crm.deal.stage",
)

#: Which record column each lineage path reads, per source record class. These maps
#: are the ONE declaration of field-level lineage: :func:`lineage_rows` writes from
#: them and :data:`LINEAGE_PATHS` is derived from them, so a path cannot be declared
#: without being written and cannot be written without being declared.
_STUDENT_PATHS: Final[dict[str, str]] = {
    "appdb.student.dob": "dob",
    "appdb.student.first_name": "first_name",
    "appdb.student.grade": "grade",
    "appdb.student.last_name": "last_name",
    "appdb.student.status": "status",
}
_CONTACT_PATHS: Final[dict[str, str]] = {
    "crm.contact.dob": "dob",
    "crm.contact.email": "email",
    "crm.contact.first_name": "first_name",
    "crm.contact.grade": "grade",
    "crm.contact.last_name": "last_name",
    "crm.contact.lifecycle_stage": "lifecycle_stage",
}
_ENROLLMENT_PATHS: Final[dict[str, str]] = {
    "appdb.enrollment.program": "program",
    "appdb.enrollment.stage": "stage",
}
_DEAL_PATHS: Final[dict[str, str]] = {"crm.deal.stage": "stage"}
#: R1: "every record carries source id, ingest timestamp, and **field-level
#: lineage**", and payments is one of the three mandated sources -- so the payment
#: record's own reportable fields are named here in the same source-qualified shape
#: the other four maps use. `payment_id` is not among them: it is the record's
#: identity, already carried by `field_lineage.source_ref`, and `metadata` is the
#: source's nested blob rather than a field.
_PAYMENT_PATHS: Final[dict[str, str]] = {
    "payments.payment.amount_cents": "amount_cents",
    "payments.payment.currency": "currency",
    "payments.payment.external_ref": "external_ref",
    "payments.payment.occurred_at": "occurred_at",
    "payments.payment.payer_email": "payer_email",
    "payments.payment.payer_name": "payer_name",
    "payments.payment.status": "status",
    "payments.payment.type": "type",
}

#: Every declared map, keyed by the `(source_id, record_class)` pair a fixture tree
#: names with its `<source>/gen<N>/<record>.jsonl` files. Keying it that way is what
#: lets a test walk the **fixtures** and ask this registry whether each source it
#: finds is covered -- a source with no entry here declares nothing and writes
#: nothing, which is the failure `tests/er/test_materialization.py` reproduces.
LINEAGE_PATH_MAPS: Final[dict[tuple[str, str], dict[str, str]]] = {
    ("appdb", "enrollment"): _ENROLLMENT_PATHS,
    ("appdb", "student"): _STUDENT_PATHS,
    ("crm", "contact"): _CONTACT_PATHS,
    ("crm", "deal"): _DEAL_PATHS,
    ("payments", "payment"): _PAYMENT_PATHS,
}

#: What `field_lineage.field` may hold: exactly the paths the maps above write.
#:
#: **Derived from what is written, never from the comparison vocabulary.** It used
#: to read `set(COMPARED_FIELD_PATHS) | set(SURVIVED_PATHS)`, which tied the reach of
#: lineage to the reach of conflict detection: extending lineage to a new source
#: would have meant extending `COMPARED_FIELD_PATHS`, and that constant is the
#: vocabulary SS2.4's comparisons name, so a new member there would change which
#: conflicts exist and break the committed 0-FP/0-FN golden result. The dependency
#: is severed in the only direction that can do harm -- this module no longer imports
#: `COMPARED_FIELD_PATHS` at all, so nothing here can add a compared field. The two
#: containments that must still hold (`COMPARED_FIELD_PATHS` and `SURVIVED_PATHS` are
#: both covered, so R16's A->B->A scan and the endpoint's `survived` block can name
#: the provenance of every value they carry) are now real assertions in
#: `tests/er/test_materialization.py` rather than a tautology of the definition.
LINEAGE_PATHS: Final[tuple[str, ...]] = tuple(
    sorted({path for paths in LINEAGE_PATH_MAPS.values() for path in paths})
)

#: R20 -- the demo org's tenants. `demo-client` is the label migration 0003 seeded
#: for the committed client key, so a client key's `visible_scope()` names exactly
#: the rows assigned to it. `demo-tenant-b` deliberately has **no** key: it is the
#: other side of the wall, and it is what makes a negative isolation test possible
#: against the committed dataset instead of a hypothetical one.
TENANT_LABELS: Final[tuple[str, ...]] = ("demo-client", "demo-tenant-b")

#: Structural (non-cascade) `entity_links.method` values -- see the module docstring.
METHOD_ANCHOR: Final = "anchor"
METHOD_MEMBER: Final = "member"


# ======================================================================================
# loading one generation out of `stg_*`
# ======================================================================================

#: Every loader is ordered by its natural key. `recon.er` re-sorts everything it
#: walks, so this is belt-and-braces -- but an unordered read is exactly the kind of
#: input-order dependence R9 is graded on, and `tests/er/test_determinism.py`
#: re-resolves a reversed snapshot to prove the cascade does not care either.
_CONTACT_SQL = text(
    """
    SELECT crm_id, email, first_name, last_name, lifecycle_stage, external_id,
           dob, grade, COALESCE(updated_at, created_at) AS observed_ts
      FROM stg_crm_contact
     WHERE generation = :generation
     ORDER BY crm_id
    """
)

_DEAL_SQL = text(
    """
    SELECT deal_id, stage, associated_contact_ids,
           COALESCE(updated_at, created_at) AS observed_ts
      FROM stg_crm_deal
     WHERE generation = :generation
     ORDER BY deal_id
    """
)

_STUDENT_SQL = text(
    """
    SELECT student_id, first_name, last_name, dob, grade, guardian_email,
           guardian2_email, status, student_number,
           COALESCE(updated_at, created_at) AS observed_ts
      FROM stg_student
     WHERE generation = :generation
     ORDER BY student_id
    """
)

_ENROLLMENT_SQL = text(
    """
    SELECT enrollment_id, student_id, program, stage, crm_deal_id,
           COALESCE(updated_at, created_at) AS observed_ts
      FROM stg_enrollment
     WHERE generation = :generation
     ORDER BY enrollment_id
    """
)

#: `observed_ts` is `occurred_at` -- the moment the payment processor says the
#: payment happened, which is the same kind of fact the other four loaders take from
#: `COALESCE(updated_at, created_at)`. `stg_payment` keeps no `updated_at`
#: (migration 0001 gave it `occurred_at`/`refunded_at` instead), so the fallback for
#: a payment that asserts no moment at all is `materialized_at`, the moment it
#: landed. That is the ONE case in `field_lineage` where `observed_ts` is a write
#: time rather than a source time, and it beats the alternative: `_observed` would
#: otherwise refuse the row and the record would carry no lineage, which is the
#: R1 gap this map exists to close.
_PAYMENT_SQL = text(
    """
    SELECT payment_id, payer_email, payer_name, external_ref, payment_metadata,
           type, status, currency, amount_cents, occurred_at,
           COALESCE(occurred_at, materialized_at) AS observed_ts
      FROM stg_payment
     WHERE generation = :generation
     ORDER BY payment_id
    """
)


def _observed(value: Any) -> datetime:
    if not isinstance(value, datetime):  # pragma: no cover - staging pins both columns
        raise ValueError("a staging row reached field_lineage with no usable timestamp")
    return value


def load_snapshot(conn: Connection, generation: int) -> Snapshot:
    """One generation's `stg_*` slice, in the record shape `recon.er` reads.

    The staging columns carry the source's own field names except where 0001
    renamed them (`student_id` for the student's `id`, `payment_metadata` for the
    payment's `metadata`); those are aliased back here, because `recon.er` and
    `recon.normalize.match_keys` are written against the **source** vocabulary and
    must not learn the database's.
    """
    params = {"generation": generation}

    contacts = [
        {
            "crm_id": row.crm_id,
            "email": row.email,
            "first_name": row.first_name,
            "last_name": row.last_name,
            "lifecycle_stage": row.lifecycle_stage,
            "external_id": row.external_id,
            "dob": row.dob,
            "grade": row.grade,
            "observed_ts": row.observed_ts,
        }
        for row in conn.execute(_CONTACT_SQL, params)
    ]
    deals = [
        {
            "deal_id": row.deal_id,
            "stage": row.stage,
            "associated_contact_ids": row.associated_contact_ids or [],
            "observed_ts": row.observed_ts,
        }
        for row in conn.execute(_DEAL_SQL, params)
    ]
    students = [
        {
            "id": row.student_id,
            "first_name": row.first_name,
            "last_name": row.last_name,
            "dob": row.dob,
            "grade": row.grade,
            "guardian_email": row.guardian_email,
            "guardian2_email": row.guardian2_email,
            "status": row.status,
            "student_number": row.student_number,
            "observed_ts": row.observed_ts,
        }
        for row in conn.execute(_STUDENT_SQL, params)
    ]
    enrollments = [
        {
            "id": row.enrollment_id,
            "student_id": row.student_id,
            "program": row.program,
            "stage": row.stage,
            "crm_deal_id": row.crm_deal_id,
            "observed_ts": row.observed_ts,
        }
        for row in conn.execute(_ENROLLMENT_SQL, params)
    ]
    payments = [
        {
            "payment_id": row.payment_id,
            "payer_email": row.payer_email,
            "payer_name": row.payer_name,
            "external_ref": row.external_ref,
            "metadata": row.payment_metadata or {},
            "type": row.type,
            "status": row.status,
            "currency": row.currency,
            "amount_cents": row.amount_cents,
            "occurred_at": row.occurred_at,
            "observed_ts": row.observed_ts,
        }
        for row in conn.execute(_PAYMENT_SQL, params)
    ]

    return Snapshot(
        generation=generation,
        contacts=contacts,
        deals=deals,
        students=students,
        enrollments=enrollments,
        payments=payments,
    )


# ======================================================================================
# the resolved generation: cascade output plus the indexes the view needs
# ======================================================================================


@dataclass(frozen=True)
class ResolvedGeneration:
    """One generation's cascade output, indexed by ref.

    `contacts`/`deals`/`students`/`enrollments`/`payments` mirror what the seed's
    `World` holds: `ref -> record`. Survivorship (SS4.6) reads them by
    `min(refs)`, which is the contract's "lexicographically smallest source ref".
    """

    generation: int
    snapshot: Snapshot
    resolution: Resolution
    contacts: Mapping[str, Mapping[str, Any]]
    deals: Mapping[str, Mapping[str, Any]]
    students: Mapping[str, Mapping[str, Any]]
    enrollments: Mapping[str, Mapping[str, Any]]
    payments: Mapping[str, Mapping[str, Any]]
    methods_by_person: Mapping[str, tuple[str, ...]]

    def survived_contact(self, person: Person) -> Mapping[str, Any] | None:
        return self.contacts[min(person.contact_refs)] if person.contact_refs else None

    def survived_deal(self, person: Person) -> Mapping[str, Any] | None:
        return self.deals[min(person.deal_refs)] if person.deal_refs else None

    def survived_enrollment(self, person: Person) -> Mapping[str, Any] | None:
        return self.enrollments[min(person.enrollment_refs)] if person.enrollment_refs else None


def resolve_generation(conn: Connection, generation: int) -> ResolvedGeneration:
    """Load `stg_*` for `generation` and run the committed cascade over it."""
    snapshot = load_snapshot(conn, generation)
    resolution = resolve(snapshot)

    methods: dict[str, set[str]] = {}
    for link in resolution.links:
        methods.setdefault(link.canonical_id, set()).add(link.method)

    return ResolvedGeneration(
        generation=generation,
        snapshot=snapshot,
        resolution=resolution,
        contacts={make_ref("crm", "contact", row["crm_id"]): row for row in snapshot.contacts},
        deals={make_ref("crm", "deal", row["deal_id"]): row for row in snapshot.deals},
        students={make_ref("appdb", "student", row["id"]): row for row in snapshot.students},
        enrollments={
            make_ref("appdb", "enrollment", row["id"]): row for row in snapshot.enrollments
        },
        payments={
            make_ref("payments", "payment", row["payment_id"]): row for row in snapshot.payments
        },
        methods_by_person={key: tuple(sorted(values)) for key, values in sorted(methods.items())},
    )


# ======================================================================================
# the unified view (R10) -- the join contract `golden/expected-views.json` pins
# ======================================================================================


def person_view(resolved: ResolvedGeneration, person: Person) -> dict[str, Any]:
    """One person's unified cross-source view: registered? paid? what stage?

    Field for field the shape of `golden/expected-views.json` (SS8). `survived`
    applies SS4.6 -- app DB for identity, the lowest source ref within a source --
    and every value is passed through the shared normalizers, never through a
    second spelling of them.
    """
    student = resolved.students.get(person.student_ref) if person.student_ref else None
    contact = resolved.survived_contact(person)
    enrollment = resolved.survived_enrollment(person)
    deal = resolved.survived_deal(person)
    payments = [resolved.payments[ref] for ref in person.payment_refs if ref in resolved.payments]
    stage = None if enrollment is None else norm_enum("stage", enrollment.get("stage"))

    return {
        "person_key": person.person_key,
        "canonical_id": person.person_key,
        "anchor_ref": person.anchor_ref,
        "entity_refs": list(person.refs),
        "identity_refs": list(person.identity_refs),
        "sources": sorted({ref.split(":", 1)[0] for ref in person.refs}),
        "household_key": None if student is None else household_key(student),
        "survived": {
            "appdb.student.first_name": None
            if student is None
            else norm_name(student.get("first_name")),
            "appdb.student.last_name": None
            if student is None
            else norm_name(student.get("last_name")),
            "appdb.student.grade": None
            if student is None
            else norm_enum("grade", student.get("grade")),
            "appdb.student.status": None
            if student is None
            else norm_enum("status", student.get("status")),
            "crm.contact.email": None if contact is None else norm_email(contact.get("email")),
            "crm.contact.lifecycle_stage": None
            if contact is None
            else norm_enum("lifecycle_stage", contact.get("lifecycle_stage")),
            "crm.deal.stage": None if deal is None else norm_enum("deal_stage", deal.get("stage")),
            "appdb.enrollment.program": None
            if enrollment is None
            else norm_enum("program", enrollment.get("program")),
            "appdb.enrollment.stage": stage,
        },
        "registered": bool(person.enrollment_refs),
        "paid": any(payment.get("status") == "paid" for payment in payments),
        "stage_funnel": stage,
        "payments": [
            {
                "ref": ref,
                "type": resolved.payments[ref].get("type"),
                "status": resolved.payments[ref].get("status"),
                "amount_cents": resolved.payments[ref].get("amount_cents"),
            }
            for ref in person.payment_refs
            if ref in resolved.payments
        ],
        "deal_refs": list(person.deal_refs),
        "link_methods": list(resolved.methods_by_person.get(person.person_key, ())),
    }


def tenant_for(person: Person, resolved: ResolvedGeneration) -> str:
    """R20 -- which demo tenant owns this person. Deterministic, household-wide.

    The shard key is the person's `household_key` when it has one, so **siblings
    always land in the same tenant**; a person with no household (a lead contact,
    an unattributed payment) shards on its own anchor ref. sha256 rather than
    `hash()`, because `PYTHONHASHSEED` must not be able to move a tenant boundary.
    """
    student = resolved.students.get(person.student_ref) if person.student_ref else None
    key = (None if student is None else household_key(student)) or person.anchor_ref
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return TENANT_LABELS[int.from_bytes(digest[:8], "big") % len(TENANT_LABELS)]


# ======================================================================================
# rows out
# ======================================================================================


def _method_for_ref(resolved: ResolvedGeneration, person: Person, ref: str) -> str:
    """The `entity_links.method` for one of a person's refs.

    Cascade rules first -- they are what `R-004` and C8's presence predicate read.
    A ref no rule produced is structural: the person's own anchor, or an
    enrollment, which belongs to its student by `enrollment.student_id` (SS1.4)
    and not by any rule in SS4.
    """
    resolution = resolved.resolution
    if ref.startswith("crm:contact:") and ref in resolution.contact_method:
        return resolution.contact_method[ref]
    if ref.startswith("payments:payment:") and ref in resolution.payment_method:
        return resolution.payment_method[ref]
    if ref.startswith("crm:deal:"):
        return "D2"
    if ref.startswith("appdb:enrollment:"):
        return METHOD_MEMBER
    if ref == person.anchor_ref:
        return METHOD_ANCHOR
    return METHOD_ANCHOR


def _deal_owner(resolved: ResolvedGeneration, deal_ref: str) -> str | None:
    """The person a shared deal's single `entity_links` row is written against.

    A household deal names two to four siblings (SS4.5), and the table holds one
    row per source record per generation. The owner is the person whose anchor ref
    wins SS4.1's source preference -- for a household that is the household anchor
    student (SS4.8) -- computed with `reference.anchor_ref` rather than with a
    private re-spelling of the same ordering.
    """
    keys = resolved.resolution.deal_persons.get(deal_ref, ())
    if not keys:
        return None
    by_anchor = {resolved.resolution.person_by_key[key].anchor_ref: key for key in keys}
    return by_anchor[anchor_ref(by_anchor)]


def link_rows(resolved: ResolvedGeneration) -> list[tuple[Any, ...]]:
    """`entity_links` rows: one per source record, `(canonical_id, source_id,
    source_key, source_ref, method, generation)`.

    Every person contributes at least one row (KS008) and every row names a landed
    record (KS009). A ref reachable from more than one person -- only a shared deal
    can be -- is written once, against `_deal_owner`.
    """
    generation = resolved.generation
    rows: dict[tuple[str, str], tuple[Any, ...]] = {}

    for person in sorted(resolved.resolution.persons, key=lambda p: p.anchor_ref):
        for ref in person.refs:
            _, _, natural_key = parse_ref(ref)
            source_id = ref_source(ref)
            slot = (source_id, natural_key)
            if ref.startswith("crm:deal:"):
                owner = _deal_owner(resolved, ref)
                if owner is not None and owner != person.person_key:
                    continue
            elif slot in rows:  # pragma: no cover - only deals are shareable
                continue
            rows[slot] = (
                person.person_key,
                source_id,
                natural_key,
                ref,
                _method_for_ref(resolved, person, ref),
                generation,
            )
    return [rows[key] for key in sorted(rows)]


def candidate_rows(resolved: ResolvedGeneration) -> list[tuple[Any, ...]]:
    """`entity_link_candidates` rows -- **every** match-key resolution (SS4.7).

    Including the ones the cascade threw away: `R-010` (C10, merge-collapsed
    record) is evaluated over the discarded rows, so dropping them would silently
    delete a whole conflict class rather than degrade one.
    """
    return [
        (
            candidate.source_ref,
            candidate.key_class,
            candidate.resolved_ref,
            candidate.generation,
            candidate.reason,
            candidate.decision == "accepted",
        )
        for candidate in resolved.resolution.candidates
    ]


def lineage_rows(resolved: ResolvedGeneration) -> list[tuple[Any, ...]]:
    """`field_lineage` rows: what each source said about each path, and when.

    One row per `(person_key, field_path, generation, source_ref)` (SS3), the
    value serialized by the shared `canon_value`, and `observed_ts` taken from the
    **source record's** `updated_at` -- the moment the source asserted it, not the
    moment this process wrote it down. A row is emitted only when the source
    actually holds that record, so "no row" means "that source never said
    anything", which is a different fact from a null value.

    **Every** payment the person owns is written, not just the survived one.
    Survivorship (SS4.6) picks one record per *identity* source because a person has
    one name and one lifecycle stage; a person has as many payments as it has
    payments, the canonical view lists all of them, and R1 says "every record
    carries ... field-level lineage". `source_ref` is part of a lineage row's
    identity, so the rows stay distinguishable, and `person.payment_refs` is already
    `sorted()` by `recon.er`, so the order is fixed. Every payment belongs to exactly
    one person -- an unattributable one is its own person (SS5.2) -- so the union
    over persons is the whole source and no payment is written twice.
    """
    rows: list[tuple[Any, ...]] = []
    generation = resolved.generation

    for person in resolved.resolution.persons:
        key = person.person_key
        student = resolved.students.get(person.student_ref) if person.student_ref else None
        contact = resolved.survived_contact(person)
        enrollment = resolved.survived_enrollment(person)
        deal = resolved.survived_deal(person)

        for record, source_id, record_class, source_ref in (
            (student, "appdb", "student", person.student_ref or ""),
            (contact, "crm", "contact", min(person.contact_refs, default="")),
            (enrollment, "appdb", "enrollment", min(person.enrollment_refs, default="")),
            (deal, "crm", "deal", min(person.deal_refs, default="")),
            *(
                (resolved.payments.get(ref), "payments", "payment", ref)
                for ref in person.payment_refs
            ),
        ):
            if record is None:
                continue
            # Read through the registry rather than a local name, so a source class
            # dropped from `LINEAGE_PATH_MAPS` stops being written as well as stops
            # being declared -- one declaration, not two that can drift.
            paths = LINEAGE_PATH_MAPS[source_id, record_class]
            observed = _observed(record.get("observed_ts"))
            for path, column in sorted(paths.items()):
                rows.append(
                    (
                        key,
                        path,
                        canon_value(record.get(column)),
                        source_id,
                        source_ref,
                        generation,
                        observed,
                    )
                )
    return rows


def entity_rows(
    resolved: ResolvedGeneration, views: Mapping[str, Mapping[str, Any]]
) -> list[tuple[Any, ...]]:
    """`entities` rows: `(canonical_id, entity_type, current)`.

    `current` is the unified view plus `generation` and `tenant`. `entity_type` is
    `person` for every row -- SS5.2's entity is "one resolved person", and the
    unattributed payment that SS5.2 also calls an entity is represented as the
    person whose only ref is that payment.
    """
    rows: list[tuple[Any, ...]] = []
    for person in resolved.resolution.persons:
        view = dict(views[person.person_key])
        view["generation"] = resolved.generation
        view["tenant"] = tenant_for(person, resolved)
        rows.append((person.person_key, "person", _json(view)))
    return rows


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


# ======================================================================================
# the write
# ======================================================================================


@dataclass(frozen=True)
class MaterializeReport:
    """What one materialization run wrote.

    `elapsed_ms` **includes the commit** when this call owned the transaction, and
    that matters more than it looks: the deferred provenance triggers (`KS008`,
    `KS009`) fire at commit and are the large majority of the wall clock on a
    120,000-record generation. A number that stopped at the last `COPY` would
    report nine seconds for a run that takes minutes. `commit_included` says which
    kind of number this is, so a caller that supplied its own connection cannot
    mistake the compute time for the whole cost.
    """

    generation: int
    lineage_generations: tuple[int, ...]
    persons: int
    links: int
    candidates: int
    entities: int
    lineage: int
    elapsed_ms: float
    persisted: bool
    commit_included: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "lineage_generations": list(self.lineage_generations),
            "persons": self.persons,
            "links": self.links,
            "candidates": self.candidates,
            "entities": self.entities,
            "lineage": self.lineage,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "persisted": self.persisted,
            "commit_included": self.commit_included,
        }


def _copy(
    conn: Connection, table: str, columns: Sequence[str], rows: Iterable[Sequence[Any]]
) -> int:
    """`COPY <table> (<columns>) FROM STDIN`. Returns the number of rows written."""
    column_list = ", ".join(f'"{name}"' for name in columns)
    statement = f'COPY "{table}" ({column_list}) FROM STDIN'
    written = 0
    cursor = conn.connection.driver_connection.cursor()
    with cursor, cursor.copy(statement) as copy_in:
        for row in rows:
            copy_in.write_row(tuple(row))
            written += 1
    return written


def _existing(conn: Connection, table: str, generation: int) -> int:
    statement = text(f"SELECT count(*) FROM {table} WHERE generation = :generation")
    return int(conn.execute(statement, {"generation": generation}).scalar_one())


def is_materialized(
    generation: int = CURRENT_GENERATION, *, conn: Connection | None = None
) -> bool:
    """Does the identity layer already cover `generation`?

    The same question :func:`materialize` asks itself before it writes, exposed
    so a **caller** can ask it *first*. The pipeline driver
    (`recon.api.internal.sync_job`) needs it for the case the append-only rule
    creates: the identity layer cannot be replaced by `recon_writer`, only
    appended to, so "is it already there" decides between doing the work,
    reporting the run as already current, and failing loudly because new records
    landed that the existing layer does not describe.

    `entity_links` is the table asked, not `entities`: `KS008` makes a canonical
    row without a link impossible, so a populated `entity_links` for the
    generation is exactly "this generation has been resolved".
    """
    if conn is not None:
        return _existing(conn, "entity_links", generation) > 0
    with role_connection(ROLE_RECON_WRITER) as owned:
        return _existing(owned, "entity_links", generation) > 0


def materialize(
    *,
    generation: int = CURRENT_GENERATION,
    lineage_generations: Sequence[int] | None = None,
    conn: Connection | None = None,
    persist: bool = True,
) -> MaterializeReport:
    """Resolve `generation` and persist the identity layer.

    `entity_links`, `entity_link_candidates` and `entities` describe **current
    state**, which SS7 defines as generation 3. `field_lineage` is the historical
    table and is written for every generation asked for (1-3 by default), because
    R4/R16's A->B->A scan needs the older snapshots and nothing else does.

    `persist=False` runs the whole cascade and builds every row **without
    writing** -- which is what the determinism check compares, so that two runs are
    two real runs and not one run asserted twice.
    """
    if lineage_generations is None:
        lineage_generations = (1, 2, generation)
    wanted = tuple(sorted({int(value) for value in lineage_generations}))

    if conn is None:
        started = time.monotonic()
        with role_connection(ROLE_RECON_WRITER) as owned:
            report = materialize(
                generation=generation,
                lineage_generations=wanted,
                conn=owned,
                persist=persist,
            )
        # The context manager committed on the way out, and that commit is where
        # the deferred KS008/KS009 triggers ran -- so the elapsed the caller is
        # handed has to be measured from out here.
        elapsed_ms = (time.monotonic() - started) * 1000.0
        report = replace(report, elapsed_ms=elapsed_ms, commit_included=True)
        if persist:
            log.info("resolve.materialized", **report.as_dict())
        return report

    started = time.monotonic()
    resolved = resolve_generation(conn, generation)
    views = {
        person.person_key: person_view(resolved, person) for person in resolved.resolution.persons
    }

    links = link_rows(resolved)
    candidates = candidate_rows(resolved)
    entities = entity_rows(resolved, views)

    lineage: list[tuple[Any, ...]] = []
    for other in wanted:
        step = resolved if other == generation else resolve_generation(conn, other)
        lineage.extend(lineage_rows(step))

    if not persist:
        return MaterializeReport(
            generation=generation,
            lineage_generations=wanted,
            persons=len(resolved.resolution.persons),
            links=len(links),
            candidates=len(candidates),
            entities=len(entities),
            lineage=len(lineage),
            elapsed_ms=(time.monotonic() - started) * 1000.0,
            persisted=False,
        )

    for table in ("entity_links", "entity_link_candidates"):
        already = _existing(conn, table, generation)
        if already:
            raise RuntimeError(
                f"{table} already holds {already} row(s) for generation {generation}: "
                "the identity layer is append-only to recon_writer, so a re-materialization "
                "would duplicate it rather than replace it. Materialize into a fresh "
                "database, or clear the generation as the schema owner first."
            )

    _copy(
        conn,
        "entity_links",
        ("canonical_id", "source_id", "source_key", "source_ref", "method", "generation"),
        links,
    )
    _copy(
        conn,
        "entity_link_candidates",
        ("source_ref", "key_class", "resolved_ref", "generation", "rule", "accepted"),
        candidates,
    )
    _copy(conn, "entities", ("canonical_id", "entity_type", "current"), entities)
    _copy(
        conn,
        "field_lineage",
        (
            "canonical_id",
            "field",
            "value_text",
            "source_id",
            "source_ref",
            "generation",
            "observed_ts",
        ),
        lineage,
    )

    return MaterializeReport(
        generation=generation,
        lineage_generations=wanted,
        persons=len(resolved.resolution.persons),
        links=len(links),
        candidates=len(candidates),
        entities=len(entities),
        lineage=len(lineage),
        elapsed_ms=(time.monotonic() - started) * 1000.0,
        persisted=True,
    )
