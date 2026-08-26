"""An INDEPENDENT check of the R10 join: raw fixture JSONL -> hand-written view.

Why this file exists
--------------------
`golden/expected-views.json` is *produced* by the same entity-resolution cascade it
is used to grade: `recon/seed/run.py` runs `recon.er.resolve` and feeds the result
into `recon.seed.golden.build_golden`, whose `_person_view` is a near line-for-line
duplicate of `recon.resolve.person_view`. `tests/er/test_expected_views.py` then
diffs the two. That test is worth having -- it catches drift between the two
assemblers -- but it is **not** evidence that the join is correct, because both
sides descend from one cascade run over one snapshot.

This file supplies the missing arm. It imports **nothing** from the detector's
entity layer: not `recon.er`, not `recon.resolve`, not `recon.reference`, not
`recon.seed`. Its expected values are written out literally, hand-derived from the
raw generation-3 fixture JSONL, and they are additionally re-derived at run time by
a small cascade implemented here **from the normative text of
`docs/invariant-contract.md` SS4** (SS4.1 refs/anchor/person_key, SS4.2 L1/L2/L3,
SS4.3 P1/P2/P3, SS4.4 E1/E2, SS4.5 D2, SS4.6 survivorship, SS4.8 households).

The one shared import is `recon.normalize`. `.claude/CLAUDE.md` pins it as THE
shared spec (R23) that both the generator and the detector legitimately call, and
re-spelling `norm_email`'s gmail rules here would be testing a second normalizer,
not the join. `KEYSTONE_NS` is likewise spelled out as the committed literal the
contract pins in SS2.2 (ruling 1) rather than imported from `recon.reference`.

What the actual side is
-----------------------
`GET /api/entities/{key}` on the real application, over the real materialized
`entities` table -- the full-profile fixture tree ingested and resolved by
`tests.er.dataset.ensure_dataset()`. So a regression anywhere from the cascade to
the stored canonical row to the endpoint turns this suite red.

The four entities are chosen to exercise the parts of the cascade that a
same-code-both-sides comparison cannot vouch for:

1. `08076f0d` Jarrow-Lowe -- an `L1` hard-key join across a **planted CRM identity
   disagreement** (the contact says "Lenaus brant-gray", grade 7; the app DB says
   "Galeol Jarrow-Lowe ", grade 2). SS4.6 survivorship must hand every identity
   field to the app DB and still keep the CRM contact in the person.
2. `00109aca` Darrowson -- an `L2` join that exists **only because of gmail
   folding**: the contact's `daramar-da.rrowson+billing@googlemail.com` reaches the
   student's `daramar-darrowson@googlemail.com` only after the dot and the `+`
   alias are removed. Also `stage: "ENROLLED"` and `lifecycle_stage: "CUSTOMER"`
   through `norm_enum`.
3. `0001e46b` Everton-Dane (Tarael) -- a **three-child household** whose three
   students spell the guardian email three different ways
   (`+family`, `+billing`, bare) that all normalize to one household key, and whose
   payment `pi_0004593` is attributed by `P2` on the metadata name alone.
4. `78a52dc0` Everton-Dane (Finaor) -- the sibling. Its presence is the point: it
   proves the shared household key does **not** collapse the siblings into one
   person and does not pool their payments.
"""

from __future__ import annotations

import io
import json
import sys
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# The ONLY detector-side import (R23: the shared spec both sides call).
from recon.normalize import norm_dob, norm_email, norm_enum, norm_name
from tests.er.dataset import FIXTURES, Dataset

#: `docs/invariant-contract.md` SS2.2 / ruling 1 -- a committed literal, never
#: re-derived. Spelled out here so `person_key` is computed without importing
#: `recon.reference`.
KEYSTONE_NS = uuid.UUID("17733ea0-28dd-5aeb-a266-c62b3689def8")

#: `migrations/versions/0003_seed_api_clients.py`. The admin key sees every tenant,
#: which is what lets one fixture pick entities without caring where R20 sharded them.
ADMIN_HEADERS = {"X-Api-Key": "keystone-demo-admin-8c25e0b71a94f36d"}

GEN = 3


# ======================================================================================
# the raw fixture tree, read as bytes off disk
# ======================================================================================


def _read(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


@dataclass(frozen=True)
class RawWorld:
    """Generation 3 exactly as the five JSONL files spell it, plus SS4 blocking keys."""

    students: tuple[dict[str, Any], ...]
    enrollments: tuple[dict[str, Any], ...]
    contacts: tuple[dict[str, Any], ...]
    deals: tuple[dict[str, Any], ...]
    payments: tuple[dict[str, Any], ...]

    student_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    contact_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: SS4.8 -- exact grouping on the PRIMARY guardian email only.
    households: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)
    #: SS4.2 L2 blocking key: (norm_email of either guardian slot, first, last).
    by_email_name: dict[tuple[str, str, str], tuple[dict[str, Any], ...]] = field(
        default_factory=dict
    )
    #: SS4.2 L3 blocking key: (first, last, dob).
    by_namedob: dict[tuple[str, str, str], tuple[dict[str, Any], ...]] = field(default_factory=dict)
    #: Students a contact already owns by hard key -- "hard keys win" (SS4.2).
    l1_students: frozenset[str] = frozenset()
    #: Contacts that carry a hard key to some student.
    l1_contacts: frozenset[str] = frozenset()
    #: SS4.2 outcome: `student_id -> ((contact, method), ...)`, ascending by `crm_id`.
    student_contacts: dict[str, tuple[tuple[dict[str, Any], str], ...]] = field(
        default_factory=dict
    )
    #: SS4.3 outcome: `student_id -> ((payment, method), ...)`, ascending by `payment_id`.
    student_payments: dict[str, tuple[tuple[dict[str, Any], str], ...]] = field(
        default_factory=dict
    )
    #: SS4.5 D2 index: `crm_id -> (deal, ...)`, ascending by `deal_id`.
    deals_by_contact: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)
    #: `student_id -> (enrollment, ...)`; SS1.4 makes this at most one element.
    enrollments_by_student: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)


def _link_contacts(
    contacts: Sequence[dict[str, Any]],
    l1_contacts: frozenset[str],
    l1_students: frozenset[str],
    by_email_name: Mapping[tuple[str, str, str], Sequence[dict[str, Any]]],
    by_namedob: Mapping[tuple[str, str, str], Sequence[dict[str, Any]]],
) -> dict[str, tuple[dict[str, Any], str]]:
    """SS4.2 -- `crm_id -> (student, method)` for every contact the cascade links.

    `L1` is settled globally before `L2`/`L3` is considered, because SS4.2 rejects a
    candidate pair when either side is already `L1`-linked to a different record.
    Buckets are read in ascending student id, which is the only thing that makes an
    `L2` hit on a bucket of several students a function of the data and not of the
    order the file happened to be written in.
    """
    linked: dict[str, tuple[dict[str, Any], str]] = {}

    def _first_free(bucket: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
        for student in bucket:
            if student["id"] not in l1_students:
                return student
        return None

    for contact in sorted(contacts, key=lambda c: c["crm_id"]):
        if contact["crm_id"] in l1_contacts:
            continue
        first = norm_name(contact.get("first_name"))
        last = norm_name(contact.get("last_name"))
        mail = norm_email(contact.get("email"))
        born = norm_dob(contact.get("dob"))
        hit = None
        if mail is not None and first is not None and last is not None:
            hit = _first_free(by_email_name.get((mail, first, last), ()))
            if hit is not None:
                linked[contact["crm_id"]] = (hit, "L2")
                continue
        if first is not None and last is not None and born is not None:
            hit = _first_free(by_namedob.get((first, last, born), ()))
            if hit is not None:
                linked[contact["crm_id"]] = (hit, "L3")
    return linked


def _attribute_payments(
    payments: Sequence[dict[str, Any]],
    student_by_id: Mapping[str, dict[str, Any]],
    households: Mapping[str, Sequence[dict[str, Any]]],
) -> dict[str, tuple[dict[str, Any], str]]:
    """SS4.3 -- `payment_id -> (student, method)`; an unattributable payment is absent.

    `P2`/`P3` consult the household keys only -- the set of PRIMARY `guardian_email`
    values (SS4.8). `guardian2_email` participates in `L2` and nowhere else.
    """
    attributed: dict[str, tuple[dict[str, Any], str]] = {}
    for payment in sorted(payments, key=lambda p: p["payment_id"]):
        external = payment.get("external_ref")
        if external is not None and external in student_by_id:
            attributed[payment["payment_id"]] = (student_by_id[external], "P1")
            continue
        key = norm_email(payment.get("payer_email"))
        members = households.get(key, ()) if key is not None else ()
        if not members:
            continue
        meta = payment.get("metadata") or {}
        first = norm_name(meta.get("student_first_name"))
        last = norm_name(meta.get("student_last_name"))
        if first is not None and last is not None:
            hits = [
                member
                for member in members
                if norm_name(member.get("first_name")) == first
                and norm_name(member.get("last_name")) == last
            ]
            if len(hits) == 1:
                attributed[payment["payment_id"]] = (hits[0], "P2")
                continue
        if len(members) == 1:
            attributed[payment["payment_id"]] = (members[0], "P3")
    return attributed


def _load_world() -> RawWorld:
    students = tuple(_read(FIXTURES / "appdb" / f"gen{GEN}" / "student.jsonl"))
    enrollments = tuple(_read(FIXTURES / "appdb" / f"gen{GEN}" / "enrollment.jsonl"))
    contacts = tuple(_read(FIXTURES / "crm" / f"gen{GEN}" / "contact.jsonl"))
    deals = tuple(_read(FIXTURES / "crm" / f"gen{GEN}" / "deal.jsonl"))
    payments = tuple(_read(FIXTURES / "payments" / f"gen{GEN}" / "payment.jsonl"))

    student_by_id = {row["id"]: row for row in students}
    contact_by_id = {row["crm_id"]: row for row in contacts}

    households: dict[str, list[dict[str, Any]]] = {}
    email_name: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    namedob: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for student in sorted(students, key=lambda row: row["id"]):
        key = norm_email(student.get("guardian_email"))
        if key is not None:
            households.setdefault(key, []).append(student)
        first = norm_name(student.get("first_name"))
        last = norm_name(student.get("last_name"))
        if first is not None and last is not None:
            for slot in ("guardian_email", "guardian2_email"):
                mail = norm_email(student.get(slot))
                if mail is not None:
                    email_name.setdefault((mail, first, last), []).append(student)
            born = norm_dob(student.get("dob"))
            if born is not None:
                namedob.setdefault((first, last, born), []).append(student)

    l1_students: set[str] = set()
    l1_contacts: set[str] = set()
    for contact in contacts:
        external = contact.get("external_id")
        if external is not None and external in student_by_id:
            l1_students.add(external)
            l1_contacts.add(contact["crm_id"])

    frozen_households = {k: tuple(v) for k, v in households.items()}
    frozen_email_name = {k: tuple(v) for k, v in email_name.items()}
    frozen_namedob = {k: tuple(v) for k, v in namedob.items()}

    soft = _link_contacts(
        contacts,
        frozenset(l1_contacts),
        frozenset(l1_students),
        frozen_email_name,
        frozen_namedob,
    )
    student_contacts: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for contact in sorted(contacts, key=lambda c: c["crm_id"]):
        external = contact.get("external_id")
        if external is not None and external in student_by_id:
            student_contacts.setdefault(external, []).append((contact, "L1"))
        elif contact["crm_id"] in soft:
            student, method = soft[contact["crm_id"]]
            student_contacts.setdefault(student["id"], []).append((contact, method))

    attributed = _attribute_payments(payments, student_by_id, frozen_households)
    student_payments: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for payment in sorted(payments, key=lambda p: p["payment_id"]):
        hit = attributed.get(payment["payment_id"])
        if hit is not None:
            student_payments.setdefault(hit[0]["id"], []).append((payment, hit[1]))

    deals_by_contact: dict[str, list[dict[str, Any]]] = {}
    for deal in sorted(deals, key=lambda d: d["deal_id"]):
        for crm_id in deal.get("associated_contact_ids") or ():
            deals_by_contact.setdefault(str(crm_id), []).append(deal)

    by_student: dict[str, list[dict[str, Any]]] = {}
    for enrollment in sorted(enrollments, key=lambda e: e["id"]):
        by_student.setdefault(str(enrollment.get("student_id")), []).append(enrollment)

    return RawWorld(
        students=students,
        enrollments=enrollments,
        contacts=contacts,
        deals=deals,
        payments=payments,
        student_by_id=student_by_id,
        contact_by_id=contact_by_id,
        households=frozen_households,
        by_email_name=frozen_email_name,
        by_namedob=frozen_namedob,
        l1_students=frozenset(l1_students),
        l1_contacts=frozenset(l1_contacts),
        student_contacts={k: tuple(v) for k, v in student_contacts.items()},
        student_payments={k: tuple(v) for k, v in student_payments.items()},
        deals_by_contact={k: tuple(v) for k, v in deals_by_contact.items()},
        enrollments_by_student={k: tuple(v) for k, v in by_student.items()},
    )


_WORLD: RawWorld | None = None


def load_world() -> RawWorld:
    """The raw fixture tree, parsed once per process."""
    global _WORLD
    if _WORLD is None:
        if not (FIXTURES / "manifest.json").is_file():
            pytest.fail(f"no committed fixture tree at {FIXTURES}: run `make seed` first.")
        _WORLD = _load_world()
    return _WORLD


# ======================================================================================
# the cascade, re-implemented here from `docs/invariant-contract.md` SS4
# ======================================================================================


def contacts_of(world: RawWorld, student_id: str) -> list[tuple[dict[str, Any], str]]:
    """SS4.2 -- every CRM contact that resolves to `student_id`, with its method."""
    return list(world.student_contacts.get(student_id, ()))


def payments_of(world: RawWorld, student_id: str) -> list[tuple[dict[str, Any], str]]:
    """SS4.3 -- every payment attributed to `student_id`, with its method."""
    return list(world.student_payments.get(student_id, ()))


def deals_of(world: RawWorld, crm_ids: Iterable[str]) -> list[dict[str, Any]]:
    """SS4.5 D2 -- every deal naming one of these contacts, ascending by `deal_id`."""
    hits = {
        deal["deal_id"]: deal
        for crm_id in crm_ids
        for deal in world.deals_by_contact.get(crm_id, ())
    }
    return [hits[key] for key in sorted(hits)]


def enrollment_method(payment: Mapping[str, Any], enrollments: Sequence[Mapping[str, Any]]) -> str:
    """SS4.4 -- `E1` when the metadata program picks exactly one, else `E2`."""
    meta = payment.get("metadata") or {}
    program = norm_enum("program", meta.get("program"))
    if program is not None:
        hits = [e for e in enrollments if norm_enum("program", e.get("program")) == program]
        if len(hits) == 1:
            return "E1"
    return "E2"


def _anchor_ref(refs: Sequence[str]) -> str:
    """SS4.1 -- lowest identity ref under `appdb:student: > crm:contact: > payments:payment:`."""
    order = ("appdb:student:", "crm:contact:", "payments:payment:")
    identity = [r for r in refs if r.startswith(order)]
    return min(
        identity,
        key=lambda r: (next(i for i, p in enumerate(order) if r.startswith(p)), r),
    )


def independent_view(world: RawWorld, student_id: str) -> dict[str, Any]:
    """The whole R10 unified view for one student-anchored person, from raw records."""
    student = world.student_by_id[student_id]
    student_ref = f"appdb:student:{student_id}"

    linked_contacts = contacts_of(world, student_id)
    linked_payments = payments_of(world, student_id)
    enrollments = list(world.enrollments_by_student.get(student_id, ()))
    deals = deals_of(world, (c["crm_id"] for c, _ in linked_contacts))

    refs = sorted(
        {student_ref}
        | {f"appdb:enrollment:{e['id']}" for e in enrollments}
        | {f"crm:contact:{c['crm_id']}" for c, _ in linked_contacts}
        | {f"crm:deal:{d['deal_id']}" for d in deals}
        | {f"payments:payment:{p['payment_id']}" for p, _ in linked_payments}
    )
    anchor = _anchor_ref(refs)
    key = str(uuid.uuid5(KEYSTONE_NS, anchor))

    # SS4.6: within one source, the survivor is the lexicographically smallest ref.
    contact = None
    if linked_contacts:
        contact = min(linked_contacts, key=lambda pair: pair[0]["crm_id"])[0]
    enrollment = enrollments[0] if enrollments else None
    deal = deals[0] if deals else None
    stage = None if enrollment is None else norm_enum("stage", enrollment.get("stage"))

    methods = {method for _, method in linked_contacts}
    methods |= {method for _, method in linked_payments}
    for payment, _ in linked_payments:
        if enrollments:
            methods.add(enrollment_method(payment, enrollments))
    if deals:
        methods.add("D2")

    payment_refs = sorted(f"payments:payment:{p['payment_id']}" for p, _ in linked_payments)
    by_ref = {f"payments:payment:{p['payment_id']}": p for p, _ in linked_payments}

    return {
        "person_key": key,
        "canonical_id": key,
        "anchor_ref": anchor,
        "entity_refs": refs,
        "identity_refs": [r for r in refs if r.startswith(("appdb:student:", "crm:contact:"))],
        "sources": sorted({r.split(":", 1)[0] for r in refs}),
        "household_key": norm_email(student.get("guardian_email")),
        "survived": {
            "appdb.student.first_name": norm_name(student.get("first_name")),
            "appdb.student.last_name": norm_name(student.get("last_name")),
            "appdb.student.grade": norm_enum("grade", student.get("grade")),
            "appdb.student.status": norm_enum("status", student.get("status")),
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
        "registered": bool(enrollments),
        "paid": any(by_ref[r].get("status") == "paid" for r in payment_refs),
        "stage_funnel": stage,
        "payments": [
            {
                "ref": r,
                "type": by_ref[r].get("type"),
                "status": by_ref[r].get("status"),
                "amount_cents": by_ref[r].get("amount_cents"),
            }
            for r in payment_refs
        ],
        "deal_refs": [f"crm:deal:{d['deal_id']}" for d in deals],
        "link_methods": sorted(methods),
    }


# ======================================================================================
# the four hand-derived entities
# ======================================================================================


@dataclass(frozen=True)
class Case:
    label: str
    student_id: str
    #: A second, non-uuid key the same entity must answer to (SS4.7 `entity_links`).
    natural_key: str
    view: dict[str, Any]


CASES: tuple[Case, ...] = (
    Case(
        label="L1 hard key holds across a planted CRM identity disagreement",
        student_id="08076f0d-6287-5d8e-b329-5ee5518dc53a",
        natural_key="CRM-0015897",
        view={
            "anchor_ref": "appdb:student:08076f0d-6287-5d8e-b329-5ee5518dc53a",
            "canonical_id": "ed9ace12-db92-5136-a730-2ace98d67eee",
            "deal_refs": ["crm:deal:DEAL-0011461"],
            "entity_refs": [
                "appdb:enrollment:3cf5c140-6e60-58f6-a229-ace2a6b5e361",
                "appdb:student:08076f0d-6287-5d8e-b329-5ee5518dc53a",
                "crm:contact:CRM-0015897",
                "crm:deal:DEAL-0011461",
                "payments:payment:pi_0015362",
            ],
            "household_key": "neriwen-jarrow-lowe@googlemail.com",
            "identity_refs": [
                "appdb:student:08076f0d-6287-5d8e-b329-5ee5518dc53a",
                "crm:contact:CRM-0015897",
            ],
            "link_methods": ["D2", "E1", "L1", "P1"],
            "paid": True,
            "payments": [
                {
                    "amount_cents": 300000,
                    "ref": "payments:payment:pi_0015362",
                    "status": "paid",
                    "type": "tuition",
                }
            ],
            "person_key": "ed9ace12-db92-5136-a730-2ace98d67eee",
            "registered": True,
            "sources": ["appdb", "crm", "payments"],
            "stage_funnel": "enrolled",
            "survived": {
                "appdb.enrollment.program": "Summer Academy",
                "appdb.enrollment.stage": "enrolled",
                # The CRM contact on this person says "Lenaus" / "brant-gray" /
                # grade 7. SS4.6 gives identity to the app DB, and every one of
                # these four is the app DB's value.
                "appdb.student.first_name": "galeol",
                "appdb.student.grade": "2",
                "appdb.student.last_name": "jarrow-lowe",
                "appdb.student.status": "enrolled",
                "crm.contact.email": "neriwen-jarrow-lowe@googlemail.com",
                "crm.contact.lifecycle_stage": "customer",
                "crm.deal.stage": "Closed Won",
            },
        },
    ),
    Case(
        label="L2 fires only because gmail folding strips a dot and a +alias",
        student_id="00109aca-b448-56b3-83d5-828fed48f0da",
        natural_key="CRM-0011266",
        view={
            "anchor_ref": "appdb:student:00109aca-b448-56b3-83d5-828fed48f0da",
            "canonical_id": "da46219d-da05-55ce-ac8b-2a598d29a2a6",
            "deal_refs": ["crm:deal:DEAL-0007037"],
            "entity_refs": [
                "appdb:enrollment:7559f5b4-fb57-501b-9de6-6415da368ead",
                "appdb:student:00109aca-b448-56b3-83d5-828fed48f0da",
                "crm:contact:CRM-0011266",
                "crm:deal:DEAL-0007037",
                "payments:payment:pi_0010497",
            ],
            "household_key": "daramar-darrowson@googlemail.com",
            "identity_refs": [
                "appdb:student:00109aca-b448-56b3-83d5-828fed48f0da",
                "crm:contact:CRM-0011266",
            ],
            "link_methods": ["D2", "E1", "L2", "P2"],
            "paid": True,
            "payments": [
                {
                    "amount_cents": 1600000,
                    "ref": "payments:payment:pi_0010497",
                    "status": "paid",
                    "type": "tuition",
                }
            ],
            "person_key": "da46219d-da05-55ce-ac8b-2a598d29a2a6",
            "registered": True,
            "sources": ["appdb", "crm", "payments"],
            "stage_funnel": "enrolled",
            "survived": {
                "appdb.enrollment.program": "Upper School",
                # the enrollment row spells this "ENROLLED"
                "appdb.enrollment.stage": "enrolled",
                "appdb.student.first_name": "neriis",
                "appdb.student.grade": "9",
                "appdb.student.last_name": "darrowson",
                "appdb.student.status": "active",
                # raw: "daramar-da.rrowson+billing@googlemail.com"
                "crm.contact.email": "daramar-darrowson@googlemail.com",
                # raw: "CUSTOMER"
                "crm.contact.lifecycle_stage": "customer",
                "crm.deal.stage": "Closed Won",
            },
        },
    ),
    Case(
        label="three-child household, one guardian email, P2 picks Tarael by name",
        student_id="0001e46b-096a-563a-afe4-49d5fefb2756",
        natural_key="pi_0004593",
        view={
            "anchor_ref": "appdb:student:0001e46b-096a-563a-afe4-49d5fefb2756",
            "canonical_id": "8e4221af-1f38-5b87-bc97-cb85b9c6f3bc",
            "deal_refs": ["crm:deal:DEAL-0002124"],
            "entity_refs": [
                "appdb:enrollment:cbd3cc92-0b05-5178-83de-30a1c7767b7e",
                "appdb:student:0001e46b-096a-563a-afe4-49d5fefb2756",
                "crm:contact:CRM-0004542",
                "crm:deal:DEAL-0002124",
                "payments:payment:pi_0004593",
            ],
            # raw guardian_email: "galewen-everton-dane+family@googlemail.com"
            "household_key": "galewen-everton-dane@googlemail.com",
            "identity_refs": [
                "appdb:student:0001e46b-096a-563a-afe4-49d5fefb2756",
                "crm:contact:CRM-0004542",
            ],
            "link_methods": ["D2", "E1", "L1", "P2"],
            "paid": True,
            "payments": [
                {
                    "amount_cents": 25000,
                    "ref": "payments:payment:pi_0004593",
                    "status": "paid",
                    "type": "deposit",
                }
            ],
            "person_key": "8e4221af-1f38-5b87-bc97-cb85b9c6f3bc",
            "registered": True,
            "sources": ["appdb", "crm", "payments"],
            "stage_funnel": "enrolled",
            "survived": {
                "appdb.enrollment.program": "Summer Academy",
                "appdb.enrollment.stage": "enrolled",
                "appdb.student.first_name": "tarael",
                "appdb.student.grade": "3",
                "appdb.student.last_name": "everton-dane",
                "appdb.student.status": "active",
                # raw: "Galewen-everton-dane@googlemail.com"
                "crm.contact.email": "galewen-everton-dane@googlemail.com",
                "crm.contact.lifecycle_stage": "customer",
                "crm.deal.stage": "Closed Won",
            },
        },
    ),
    Case(
        label="the sibling: a shared household key must not pool the two children",
        student_id="78a52dc0-2392-56d3-961f-53ee249a540d",
        natural_key="CRM-0004540",
        view={
            "anchor_ref": "appdb:student:78a52dc0-2392-56d3-961f-53ee249a540d",
            "canonical_id": "ab0316aa-d07b-5a7d-bd7d-527e01ff4176",
            "deal_refs": ["crm:deal:DEAL-0002124"],
            "entity_refs": [
                "appdb:enrollment:fdb215bf-4fd9-55ac-b876-1b5f9e7912e5",
                "appdb:student:78a52dc0-2392-56d3-961f-53ee249a540d",
                "crm:contact:CRM-0004540",
                "crm:deal:DEAL-0002124",
                "payments:payment:pi_0004591",
            ],
            "household_key": "galewen-everton-dane@googlemail.com",
            "identity_refs": [
                "appdb:student:78a52dc0-2392-56d3-961f-53ee249a540d",
                "crm:contact:CRM-0004540",
            ],
            "link_methods": ["D2", "E1", "L1", "P2"],
            "paid": True,
            "payments": [
                {
                    "amount_cents": 300000,
                    "ref": "payments:payment:pi_0004591",
                    "status": "paid",
                    "type": "tuition",
                }
            ],
            "person_key": "ab0316aa-d07b-5a7d-bd7d-527e01ff4176",
            "registered": True,
            "sources": ["appdb", "crm", "payments"],
            "stage_funnel": "enrolled",
            "survived": {
                "appdb.enrollment.program": "Summer Academy",
                "appdb.enrollment.stage": "enrolled",
                "appdb.student.first_name": "finaor",
                "appdb.student.grade": "PK",
                "appdb.student.status": "enrolled",
                "appdb.student.last_name": "everton-dane",
                # raw: "galewen-everton.-dane+school@googlemail.com"
                "crm.contact.email": "galewen-everton-dane@googlemail.com",
                # raw: "CUSTOMER"
                "crm.contact.lifecycle_stage": "customer",
                "crm.deal.stage": "Closed Won",
            },
        },
    ),
)

IDS = [case.student_id[:8] for case in CASES]


# ======================================================================================
# harness: structlog has to be bound to a stream that is still open
# ======================================================================================

#: Reached only when the interpreter has no usable `sys.__stderr__` (a frozen or
#: GUI runner). A `StringIO` is never closed, so a write to it cannot raise -- which
#: is the only property being bought here.
_FALLBACK_LOG_SINK = io.StringIO()


def _durable_log_stream() -> Any:
    """The process's own stderr: pytest redirects it by file descriptor, never closes it."""
    stream = sys.__stderr__
    if stream is None or stream.closed:
        return _FALLBACK_LOG_SINK
    return stream


def _uncache_recon_loggers() -> None:
    """Forget the bound logger structlog memoised on every `recon.*` lazy proxy.

    `configure_logging(cache=True)` -- the production setting -- makes
    `structlog.get_logger(...)` memoise the assembled bound logger on the proxy the
    first time it is used, by rebinding `proxy.bind` in the instance dict. A
    module-level `log = structlog.get_logger("recon.ingest")` is therefore pinned to
    whatever stream the FIRST test in the session to touch it configured, and
    re-configuring structlog afterwards does **not** move it -- so re-installing the
    chain without this does nothing at all for `recon.ingest`, which is the module
    that raises. The proxies are found by walking the already-imported `recon.*`
    modules rather than by listing them, so a module that grows a logger tomorrow is
    covered without anyone remembering to add it.
    """
    import structlog

    for name, module in list(sys.modules.items()):
        if not name.startswith("recon"):
            continue
        for value in vars(module).values():
            if isinstance(value, structlog._config.BoundLoggerLazyProxy):
                proxy: Any = value
                proxy.__dict__.pop("bind", None)


def rebind_logging_to_a_durable_stream() -> None:
    """Re-install the production log chain on a stream nothing is going to close.

    **The defect this closes is in the harness, not in the service.**
    `recon.logging.configure_logging` builds
    `structlog.WriteLoggerFactory(file=stream or sys.stderr)`, which captures the
    *object* `sys.stderr` names at configure time. Every Keystone entry point calls
    `configure_logging_once()`, so a test that drives one in-process while pytest's
    per-test capture is installed leaves the `WriteLogger` holding pytest's capture
    object; pytest closes that object when the test ends, and every later
    `log.info(...)` **in the same process** raises
    ``ValueError: I/O operation on closed file`` from inside the logging call.

    That is not a logging nuisance. `tests/invariants/test_cli.py` calls
    `recon.invariants.__main__.main()` with `capsys`, and the very next module to
    build the materialized dataset dies at `recon/ingest.py`'s
    ``log.info("ingest.source_done", ...)`` -- in *fixture setup*, so every
    database-backed test downstream of it ERRORs rather than fails. Measured:
    `pytest tests/invariants` errored 45 tests, and
    `pytest tests/invariants/test_cli.py tests/er/test_independent_join.py` errored 9.

    The repair is the one `tests/privacy/conftest.py` and `tests/triggers/conftest.py`
    already settled on: configure against `sys.__stderr__`, which pytest redirects at
    the file-descriptor level but never closes, so the chain outlives any single
    test's capture. `reset_logging_configuration()` + `configure_logging_once()` --
    rather than `configure_logging(stream=...)` -- because that pair also leaves
    `recon.logging._CONFIGURED` set, so a later `create_app()` cannot re-bind the
    chain onto whatever capture object happens to be installed at that moment.

    Nothing is silenced and no assertion moves: the full production chain, redaction
    processor included, is what gets installed, and every log call the suite drives
    still really runs. Only the destination is ours.
    """
    from recon.logging import configure_logging_once, reset_logging_configuration

    saved = sys.stderr
    sys.stderr = _durable_log_stream()
    try:
        reset_logging_configuration()
        configure_logging_once()
    finally:
        sys.stderr = saved
    _uncache_recon_loggers()


# ======================================================================================
# fixtures
# ======================================================================================


@pytest.fixture(scope="session", autouse=True)
def _logging_survives_the_dataset_build() -> None:
    """Repair the log chain before `dataset` builds, not after it has already died.

    Autouse and session-scoped so that pytest sets it up ahead of the explicitly
    requested session fixtures of this module -- `dataset` (from
    `tests/er/conftest.py`), whose `ingest_generation` call is the one that raises
    when the chain is bound to a closed stream. Asserted by measurement rather than
    by reading the ordering rules: without this fixture,
    `pytest tests/invariants/test_cli.py tests/er/test_independent_join.py` ERRORs
    the nine database-backed tests in this file; with it, all 21 pass.
    """
    rebind_logging_to_a_durable_stream()


@pytest.fixture(scope="session")
def world() -> RawWorld:
    return load_world()


@pytest.fixture(scope="session")
def joined(dataset: Dataset) -> Iterator[TestClient]:
    """The real app over the real materialized dataset."""
    from recon.app import create_app

    with TestClient(create_app()) as client:
        yield client


def _fetch(api: TestClient, key: str) -> dict[str, Any]:
    response = api.get(f"/api/entities/{key}", params={"lineage": "false"}, headers=ADMIN_HEADERS)
    assert response.status_code == 200, f"GET /api/entities/{key} -> {response.status_code}"
    return response.json()


# ======================================================================================
# 1. the literal expectation vs. the raw fixture bytes
# ======================================================================================


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_hand_written_view_is_what_the_raw_records_say(world: RawWorld, case: Case) -> None:
    """The literal view above, re-derived from JSONL by this file's own SS4 cascade.

    This is what stops the literal blocks from being a transcript of the endpoint:
    they have to survive a second, independent assembly from the fixture bytes.
    """
    assert independent_view(world, case.student_id) == case.view, case.label


# ======================================================================================
# 2. the literal expectation vs. what the service actually serves
# ======================================================================================


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_api_serves_the_hand_written_view(joined: TestClient, case: Case) -> None:
    """`GET /api/entities/{person_key}` returns exactly the hand-derived view."""
    body = _fetch(joined, case.view["person_key"])
    assert body["key"]["canonical_id"] == case.view["person_key"]
    assert body["view"] == case.view, case.label
    assert body["answer"] == {
        "registered": case.view["registered"],
        "paid": case.view["paid"],
        "stage": case.view["stage_funnel"],
        "sources": case.view["sources"],
    }


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_the_natural_key_reaches_the_same_person(joined: TestClient, case: Case) -> None:
    """A CRM id / payment id the person absorbed resolves to the same canonical row.

    The uuid lookup is a primary-key read and would still pass if `entity_links`
    were empty; this one goes through `entity_links.source_ref`, so it fails when a
    record stops being attached to the person the join says owns it.
    """
    body = _fetch(joined, case.natural_key)
    assert body["key"]["form"] == "natural_key"
    assert body["key"]["canonical_id"] == case.view["person_key"], case.label
    assert body["view"] == case.view


# ======================================================================================
# 3. the cases are still the hard cases (a fixture reroll must not quietly soften them)
# ======================================================================================


def test_case_one_really_carries_a_crm_identity_disagreement(world: RawWorld) -> None:
    """Case 1 is only interesting while the CRM contradicts the app DB on identity."""
    student = world.student_by_id["08076f0d-6287-5d8e-b329-5ee5518dc53a"]
    contact = world.contact_by_id["CRM-0015897"]

    assert contact["external_id"] == student["id"], "the L1 hard key is what joins these two"
    assert norm_name(contact["first_name"]) != norm_name(student["first_name"])
    assert norm_name(contact["last_name"]) != norm_name(student["last_name"])
    assert norm_enum("grade", contact["grade"]) != norm_enum("grade", student["grade"])

    survived = CASES[0].view["survived"]
    assert survived["appdb.student.first_name"] == norm_name(student["first_name"])
    assert survived["appdb.student.last_name"] == norm_name(student["last_name"])
    assert survived["appdb.student.grade"] == norm_enum("grade", student["grade"])


def test_case_two_needs_gmail_folding_to_link_at_all(world: RawWorld) -> None:
    """Strip the folding and the L2 blocking key stops matching -- no link, no view."""
    student = world.student_by_id["00109aca-b448-56b3-83d5-828fed48f0da"]
    contact = world.contact_by_id["CRM-0011266"]

    raw_contact_email = contact["email"]
    raw_guardian_email = student["guardian_email"]
    assert "." in raw_contact_email.split("@")[0], raw_contact_email
    assert "+" in raw_contact_email.split("@")[0], raw_contact_email
    assert raw_contact_email.casefold() != raw_guardian_email.casefold()
    assert norm_email(raw_contact_email) == norm_email(raw_guardian_email)

    assert contact["external_id"] is None, "no hard key -- this person exists because of L2"
    linked = {row["crm_id"]: method for row, method in contacts_of(world, student["id"])}
    assert linked == {contact["crm_id"]: "L2"}


def test_case_three_and_four_are_one_household_of_three_children(world: RawWorld) -> None:
    """Three students, three spellings of one guardian email, three distinct persons."""
    key = "galewen-everton-dane@googlemail.com"
    members = world.households[key]
    assert len(members) == 3, [m["id"] for m in members]

    spellings = {m["guardian_email"] for m in members}
    assert len(spellings) == 3, spellings
    assert {norm_email(s) for s in spellings} == {key}

    # ... and the cascade keeps them apart.
    keys = {m["id"]: independent_view(world, m["id"])["person_key"] for m in members}
    assert len(set(keys.values())) == 3, keys

    # Each child's payment is attributed to that child, by metadata name (P2/P1).
    owned = {m["id"]: [p["payment_id"] for p, _ in payments_of(world, m["id"])] for m in members}
    assert owned == {
        "0001e46b-096a-563a-afe4-49d5fefb2756": ["pi_0004593"],
        "69ed710b-5656-5a8e-9eb1-8730a138b0b9": ["pi_0004592"],
        "78a52dc0-2392-56d3-961f-53ee249a540d": ["pi_0004591"],
    }, owned

    # The household deal is shared by all three, and by nobody else.
    named = {
        d["deal_id"]
        for d in world.deals
        if {"CRM-0004540", "CRM-0004541", "CRM-0004542"} & set(d["associated_contact_ids"] or ())
    }
    assert named == {"DEAL-0002124"}


def test_payer_email_of_case_three_is_a_dotted_alias(world: RawWorld) -> None:
    """P2 reached the household key only after the payer address was folded."""
    payment = next(p for p in world.payments if p["payment_id"] == "pi_0004593")
    raw = payment["payer_email"]
    assert raw == "galewe.n-everton-dane+billing@googlemail.com"
    assert norm_email(raw) == "galewen-everton-dane@googlemail.com"
    assert payment["external_ref"] is None, "no P1 hard key -- this is a P2 attribution"


# ======================================================================================
# 4. the join is not self-referential: this file never imports the entity layer
# ======================================================================================


def test_this_module_imports_no_detector_entity_code() -> None:
    """The whole point of the file, asserted rather than promised.

    Scope, stated exactly: **nothing that produces an expected value here comes from
    the entity layer.** `recon.normalize` is allowed (R23, the shared spec). `recon.er`,
    `recon.resolve`, `recon.reference` and `recon.seed` are the code under test and
    must not appear in this module's source.

    `tests.er.dataset` is imported, and it *does* call `recon.resolve.materialize` --
    that is the actual side, the pipeline being graded. What it never supplies is an
    expectation: every asserted value above is either a literal or a product of this
    file's own SS4 cascade over the fixture JSONL.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    lines = [line.strip() for line in source.splitlines()]
    imports = [line for line in lines if line.startswith(("import ", "from "))]
    banned = ("recon.er", "recon.resolve", "recon.reference", "recon.seed", "recon.invariants")
    offenders = [line for line in imports for name in banned if name in line]
    assert offenders == [], offenders


def test_the_dataset_under_test_is_the_full_tree(dataset: Dataset, world: RawWorld) -> None:
    """A dev-profile stand-in would make every assertion above vacuous."""
    assert dataset.generation == GEN
    assert len(world.students) == 25000
    assert len(world.contacts) == 40000
    assert len(world.payments) == 18000
