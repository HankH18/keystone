"""SS9.1(b) -- the construction sweep, and the source of every golden entry.

The sweep evaluates each of the fourteen SS5.5 predicates over **every** generation-3
entity -- planted, unsampled and clean alike -- using the shared `recon.normalize`,
`recon.reference` and `recon.er` modules and nothing else. It never executes
`rules/*.sql`, so `G31`'s non-circularity note stands: *which* conflicts exist and of
*what type* remains independent ground truth held by the plant registry, and the sweep
supplies only the **addressing** (`entity_refs`, `observed_values`).

Two consumers, one implementation:

* `selfcheck` compares the raw sweep counts, class by class, against the planted
  population. A surplus of one anywhere is a construction bug and fails the seed run --
  this, and not the 1,000-entity clean sample, is what makes the zero-false-positive
  floor structural.
* `golden` runs the sweep output through the committed `apply_precedence` and writes
  what survives.

Every predicate below cites the SS5.5 row it implements; where SS5.5 pins an
`unchecked` branch (C9's empty person set, C12's missing program) the sweep declines
to emit rather than guessing, exactly as the rule must.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from recon.er import Person, Resolution, Snapshot
from recon.normalize import norm_dob, norm_email, norm_enum, norm_name
from recon.reference import (
    C11_WINDOW_SECONDS,
    ENROLLMENT_GRADE_FLOOR,
    GRADE_ORDER,
    PAID_IMPLYING_STAGES,
    RULE_ID_BY_TYPE,
    STATUS_TO_FUNNEL,
    compare_record,
    conflict_refs,
    conflict_type_for_paths,
    disagreeing_fields,
    fee_amount_cents,
    grade_ord,
    household_members_appdb,
    make_ref,
    sources_involved,
)

from .rng import EPOCH

__all__ = ["ConflictEntry", "SweepResult", "World", "run_sweep"]

_MASK_FLOOR = GRADE_ORDER[ENROLLMENT_GRADE_FLOOR]


def _seconds(timestamp: Any) -> int | None:
    """Whole seconds since the epoch, or `None` for anything that is not a timestamp.

    Tolerant on purpose. C11's window and C13's recency clause both compare whole
    seconds (SS2.5 ruling 4), and a rule that meets a NULL or an unparseable value
    must decline to fire rather than raise -- SS5.8 has no crash in its vocabulary.
    """
    if not isinstance(timestamp, str):
        return None
    from datetime import datetime

    try:
        parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return int((parsed - EPOCH).total_seconds())


@dataclass(frozen=True)
class ConflictEntry:
    """One detected conflict, in `golden/conflicts.json`'s shape (SS8)."""

    type: str
    rule_id: str
    entity_refs: tuple[str, ...]
    sources_involved: tuple[str, ...]
    disagreeing_fields: tuple[str, ...]
    observed_values: dict[str, Any]
    expected_verdict: str = "conflict"
    oscillating: bool = False
    #: Which planted anchor this entry addresses -- sweep-side bookkeeping, never emitted.
    anchor: str | None = None

    def as_json(self, compound_with: Sequence[str]) -> dict[str, Any]:
        return {
            "type": self.type,
            "rule_id": self.rule_id,
            "entity_refs": list(self.entity_refs),
            "sources_involved": list(self.sources_involved),
            "disagreeing_fields": list(self.disagreeing_fields),
            "observed_values": self.observed_values,
            "expected_verdict": self.expected_verdict,
            "compound_with": list(compound_with),
            "oscillating": self.oscillating,
        }


def _entry(
    conflict_type: str,
    refs: tuple[str, ...],
    observed: Mapping[str, Any],
    *,
    paths: Sequence[str] = (),
    anchor: str | None = None,
) -> ConflictEntry:
    return ConflictEntry(
        type=conflict_type,
        rule_id=RULE_ID_BY_TYPE[conflict_type],
        entity_refs=refs,
        sources_involved=sources_involved(refs),
        disagreeing_fields=tuple(paths),
        observed_values=dict(observed),
        anchor=anchor,
    )


# ======================================================================================


@dataclass
class World:
    """Indexed generation-3 snapshot plus the cascade output over it."""

    snapshot: Snapshot
    resolution: Resolution
    contacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    deals: dict[str, dict[str, Any]] = field(default_factory=dict)
    students: dict[str, dict[str, Any]] = field(default_factory=dict)
    enrollments: dict[str, dict[str, Any]] = field(default_factory=dict)
    payments: dict[str, dict[str, Any]] = field(default_factory=dict)
    households: dict[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)

    @classmethod
    def build(cls, snapshot: Snapshot, resolution: Resolution) -> World:
        world = cls(snapshot=snapshot, resolution=resolution)
        world.contacts = {
            make_ref("crm", "contact", row["crm_id"]): dict(row) for row in snapshot.contacts
        }
        world.deals = {make_ref("crm", "deal", row["deal_id"]): dict(row) for row in snapshot.deals}
        world.students = {
            make_ref("appdb", "student", row["id"]): dict(row) for row in snapshot.students
        }
        world.enrollments = {
            make_ref("appdb", "enrollment", row["id"]): dict(row) for row in snapshot.enrollments
        }
        world.payments = {
            make_ref("payments", "payment", row["payment_id"]): dict(row)
            for row in snapshot.payments
        }
        world.households = household_members_appdb(snapshot.students)
        return world

    # -- SS4.6 survivorship ------------------------------------------------------------
    def survived_contact(self, person: Person) -> dict[str, Any] | None:
        """The person's CRM contact under SS4.6: **lowest source ref**, i.e. lowest `crm_id`."""
        if not person.contact_refs:
            return None
        return self.contacts[min(person.contact_refs)]

    def survived_deal(self, person: Person) -> dict[str, Any] | None:
        if not person.deal_refs:
            return None
        return self.deals[min(person.deal_refs)]

    def survived_enrollment(self, person: Person) -> dict[str, Any] | None:
        if not person.enrollment_refs:
            return None
        return self.enrollments[min(person.enrollment_refs)]

    def person_payments(self, person: Person) -> list[dict[str, Any]]:
        return [self.payments[ref] for ref in person.payment_refs if ref in self.payments]

    def linked_persons(self) -> list[Person]:
        """Persons with a student **and** at least one linked contact -- C6/C14's domain."""
        return [
            person
            for person in self.resolution.persons
            if person.student_ref is not None and person.contact_refs
        ]

    def person_of_student(self, student_ref: str) -> Person | None:
        key = self.resolution.person_by_ref.get(student_ref)
        return None if key is None else self.resolution.person_by_key[key]


@dataclass
class SweepResult:
    entries: list[ConflictEntry]
    counts: dict[str, int]
    world: World

    def by_type(self, conflict_type: str) -> list[ConflictEntry]:
        return [entry for entry in self.entries if entry.type == conflict_type]


# ======================================================================================
# The fourteen predicates
# ======================================================================================


def _sweep_c1(world: World) -> list[ConflictEntry]:
    """C1 -- person has >=1 `paid` payment **and** >=1 enrollment, but 0 `D2`-linked deals."""
    out: list[ConflictEntry] = []
    for person in world.resolution.persons:
        if person.student_ref is None or not person.enrollment_refs:
            continue
        paid = sorted(
            ref
            for ref in person.payment_refs
            if world.payments.get(ref, {}).get("status") == "paid"
        )
        if not paid or person.deal_refs:
            continue
        out.append(
            _entry(
                "C1",
                conflict_refs("C1", identity_refs=person.identity_refs),
                {
                    "paid_payment_refs": paid,
                    "enrollment_ref": min(person.enrollment_refs),
                    "d2_deal_count": len(person.deal_refs),
                },
                anchor=person.student_ref,
            )
        )
    return out


def _sweep_c2(world: World) -> list[ConflictEntry]:
    """C2 -- payment links to no person by `P1..P3` (SS4.3: an unattributable payment
    is C2, never a guess)."""
    out: list[ConflictEntry] = []
    for payment_ref in world.resolution.unattributed_payment_refs:
        payment = world.payments[payment_ref]
        metadata = payment.get("metadata") or {}
        out.append(
            _entry(
                "C2",
                conflict_refs("C2", payment_refs=(payment_ref,)),
                {
                    "payer_email_norm": norm_email(payment.get("payer_email")),
                    "external_ref": payment.get("external_ref"),
                    "metadata_name_pair_present": bool(
                        metadata.get("student_first_name") is not None
                        and metadata.get("student_last_name") is not None
                    ),
                },
                anchor=payment_ref,
            )
        )
    return out


def _sweep_c3(world: World) -> list[ConflictEntry]:
    """C3 -- two gen-3 CRM contacts, equal `email_norm` **and** `(first_norm, last_norm)`,
    `dob_norm` equal or either null. One entry per unordered pair (SS5.2)."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for ref, contact in sorted(world.contacts.items()):
        email = norm_email(contact.get("email"))
        first = norm_name(contact.get("first_name"))
        last = norm_name(contact.get("last_name"))
        if email is None or first is None or last is None:
            continue
        groups.setdefault((email, first, last), []).append(ref)

    out: list[ConflictEntry] = []
    for (email, first, last), refs in sorted(groups.items()):
        if len(refs) < 2:
            continue
        ordered = sorted(refs)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1 :]:
                dob_a = norm_dob(world.contacts[left].get("dob"))
                dob_b = norm_dob(world.contacts[right].get("dob"))
                if dob_a is not None and dob_b is not None and dob_a != dob_b:
                    continue
                out.append(
                    _entry(
                        "C3",
                        conflict_refs("C3", contact_refs=(left, right)),
                        {
                            "email_norm": email,
                            "first_norm": first,
                            "last_norm": last,
                            "dob_norm_a": dob_a,
                            "dob_norm_b": dob_b,
                        },
                        anchor=f"{left}|{right}",
                    )
                )
    return out


def _sweep_c4(world: World) -> list[ConflictEntry]:
    """C4 -- `entity_links.method == 'L3'` **and** the contact's address is not one of the
    student's normalized guardian emails. `method` is read off the link row, never
    re-derived (SS5.5 C4)."""
    out: list[ConflictEntry] = []
    for link in world.resolution.links:
        if link.link_class != "contact_student" or link.method != "L3":
            continue
        contact = world.contacts[link.source_ref]
        student = world.students[link.resolved_ref]
        guardian = sorted(
            {
                value
                for value in (
                    norm_email(student.get("guardian_email")),
                    norm_email(student.get("guardian2_email")),
                )
                if value is not None
            }
        )
        contact_email = norm_email(contact.get("email"))
        if contact_email in guardian:
            continue
        person = world.person_of_student(link.resolved_ref)
        if person is None:  # pragma: no cover - every student is a person
            continue
        out.append(
            _entry(
                "C4",
                conflict_refs("C4", identity_refs=person.identity_refs),
                {
                    "contact_email_norm": contact_email,
                    "student_guardian_email_norms": guardian,
                    "link_method": link.method,
                },
                anchor=link.resolved_ref,
            )
        )
    return out


def _sweep_c5(world: World) -> list[ConflictEntry]:
    """C5 -- `STATUS_TO_FUNNEL(status) == enrolled`, no `entity_links` contact and no
    `P1..P3`-attributed payment."""
    out: list[ConflictEntry] = []
    for person in world.resolution.persons:
        if person.student_ref is None:
            continue
        student = world.students[person.student_ref]
        funnel = norm_enum("status", student.get("status"))
        if funnel is None or STATUS_TO_FUNNEL[funnel] != "enrolled":
            continue
        if person.contact_refs or person.payment_refs:
            continue
        out.append(
            _entry(
                "C5",
                conflict_refs("C5", identity_refs=person.identity_refs),
                {
                    "status_funnel": STATUS_TO_FUNNEL[funnel],
                    "linked_contact_count": len(person.contact_refs),
                    "attributed_payment_count": len(person.payment_refs),
                },
                anchor=person.student_ref,
            )
        )
    return out


def _compare_person(world: World, person: Person) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Run the whole `COMPARED_FIELDS` sweep for one person over **survived** values.

    C6/C14 compare survived values **across sources only** (SS5.2): a disagreement
    between two records of the same source is C3/C10's business, never this.
    """
    contact = world.survived_contact(person)
    deal = world.survived_deal(person)
    student = world.students[person.student_ref] if person.student_ref else None
    enrollment = world.survived_enrollment(person)
    left = {
        "crm.contact.first_name": None if contact is None else contact.get("first_name"),
        "crm.contact.last_name": None if contact is None else contact.get("last_name"),
        "crm.contact.dob": None if contact is None else contact.get("dob"),
        "crm.contact.grade": None if contact is None else contact.get("grade"),
        "crm.contact.lifecycle_stage": None if contact is None else contact.get("lifecycle_stage"),
        "crm.deal.stage": None if deal is None else deal.get("stage"),
    }
    right = {
        "appdb.student.first_name": None if student is None else student.get("first_name"),
        "appdb.student.last_name": None if student is None else student.get("last_name"),
        "appdb.student.dob": None if student is None else student.get("dob"),
        "appdb.student.grade": None if student is None else student.get("grade"),
        "appdb.student.status": None if student is None else student.get("status"),
        "appdb.enrollment.stage": None if enrollment is None else enrollment.get("stage"),
    }
    comparisons = compare_record(left, right)
    paths = disagreeing_fields(comparisons)
    observed: dict[str, Any] = {}
    for comparison in comparisons:
        if not comparison.disagrees:
            continue
        row = _row_for(comparison.logical)
        observed[row[0]] = comparison.left
        observed[row[1]] = comparison.right
    return paths, observed


def _row_for(logical: str) -> tuple[str, str]:
    from recon.reference import COMPARED_FIELD_BY_LOGICAL

    row = COMPARED_FIELD_BY_LOGICAL[logical]
    return row.left_path, row.right_path


def _sweep_c6_c14(world: World) -> list[ConflictEntry]:
    """C6 / C14 -- one conflict per person per generation, partitioned by SS2.4's
    sensitivity table: a wholly sensitive disagreeing set is C14, anything else C6."""
    out: list[ConflictEntry] = []
    for person in world.linked_persons():
        paths, observed = _compare_person(world, person)
        conflict_type = conflict_type_for_paths(paths)
        if conflict_type is None:
            continue
        out.append(
            _entry(
                conflict_type,
                conflict_refs(conflict_type, identity_refs=person.identity_refs),
                observed,
                paths=paths,
                anchor=person.student_ref,
            )
        )
    return out


def _sweep_c7(world: World) -> list[ConflictEntry]:
    """C7 -- enrollment at a paid-implying stage with no `E1`/`E2`-attributed `paid`
    `deposit`/`tuition`. `deposit_paid_at` is never a trigger (SS5.5 C7)."""
    attributed: dict[str, list[str]] = {}
    for payment_ref, enrollment_ref in world.resolution.payment_enrollment.items():
        attributed.setdefault(enrollment_ref, []).append(payment_ref)

    out: list[ConflictEntry] = []
    for enrollment_ref, enrollment in sorted(world.enrollments.items()):
        funnel = norm_enum("stage", enrollment.get("stage"))
        if funnel not in PAID_IMPLYING_STAGES:
            continue
        paid = [
            ref
            for ref in sorted(attributed.get(enrollment_ref, ()))
            if world.payments[ref].get("status") == "paid"
            and world.payments[ref].get("type") in {"deposit", "tuition"}
        ]
        if paid:
            continue
        student_ref = make_ref("appdb", "student", enrollment["student_id"])
        person = world.person_of_student(student_ref)
        if person is None:  # pragma: no cover
            continue
        out.append(
            _entry(
                "C7",
                conflict_refs(
                    "C7",
                    identity_refs=person.identity_refs,
                    enrollment_refs=(enrollment_ref,),
                ),
                {
                    "enrollment.stage_funnel": funnel,
                    "enrollment.deposit_paid_at": enrollment.get("deposit_paid_at"),
                    "paid_deposit_payment_count": len(paid),
                },
                anchor=enrollment_ref,
            )
        )
    return out


def _sweep_c8(world: World) -> list[ConflictEntry]:
    """C8 -- exactly one **eligible** child absent from exactly one downstream source in
    which all the other eligible children are present.

    Presence is *defined*, not assumed (SS5.5 C8): present in `crm` iff the child has an
    `entity_links` `contact_student` row; present in `payments` iff a payment is
    attributed to it by `P1..P3`. `appdb` presence is definitional and the app DB is
    never the dropped source.
    """
    out: list[ConflictEntry] = []
    for key, members in sorted(world.households.items()):
        if len(members) < 2:
            continue
        eligible: list[tuple[str, Person]] = []
        for student in members:
            student_ref = make_ref("appdb", "student", student["id"])
            person = world.person_of_student(student_ref)
            if person is None:  # pragma: no cover
                continue
            ordinal = grade_ord(student.get("grade"))
            # Explicit `is None`: `GRADE_ORDER["K"] == 0`, and `0 or -1` is falsy-truthy
            # sleight of hand that would silently exclude every kindergartener.
            if ordinal is None or ordinal < _MASK_FLOOR:
                continue
            if norm_enum("status", student.get("status")) == "withdrawn":
                continue
            enrollment = world.survived_enrollment(person)
            if enrollment is not None and norm_enum("stage", enrollment.get("stage")) in {
                "withdrawn",
                "refunded",
            }:
                continue
            eligible.append((student_ref, person))
        if len(eligible) < 2:
            continue

        dropped: list[tuple[str, str, Person]] = []
        for source in ("crm", "payments"):
            absent = [
                (ref, person)
                for ref, person in eligible
                if not (person.contact_refs if source == "crm" else person.payment_refs)
            ]
            if len(absent) == 1:
                dropped.append((source, absent[0][0], absent[0][1]))
        if len(dropped) != 1:
            continue
        source, _student_ref, person = dropped[0]
        out.append(
            _entry(
                "C8",
                conflict_refs("C8", identity_refs=person.identity_refs),
                {
                    "household_key": key,
                    "dropped_source": source,
                    "eligible_member_count": len(eligible),
                },
                anchor=person.student_ref,
            )
        )
    return out


def _sweep_c9(world: World) -> list[ConflictEntry]:
    """C9 -- `crm_deal_id` names a deal absent from the gen-3 CRM snapshot, **or** a deal
    whose `D2` person set is non-empty and does not contain the enrollment's person.
    An empty person set is `unchecked` (`deal_unresolved`), never a conflict."""
    out: list[ConflictEntry] = []
    for enrollment_ref, enrollment in sorted(world.enrollments.items()):
        deal_id = enrollment.get("crm_deal_id")
        if deal_id is None:
            continue
        student_ref = make_ref("appdb", "student", enrollment["student_id"])
        person = world.person_of_student(student_ref)
        if person is None:  # pragma: no cover
            continue
        deal_ref = make_ref("crm", "deal", deal_id)
        present = deal_ref in world.deals
        person_keys = world.resolution.deal_persons.get(deal_ref, ())
        if present:
            if not person_keys:
                continue  # verdict='unchecked', detail.reason='deal_unresolved'
            if person.person_key in person_keys:
                continue
        out.append(
            _entry(
                "C9",
                conflict_refs(
                    "C9",
                    identity_refs=person.identity_refs,
                    enrollment_refs=(enrollment_ref,),
                ),
                {
                    "enrollment.crm_deal_id": str(deal_id),
                    "deal_present_gen3": present,
                    "deal_person_refs": sorted(
                        world.resolution.person_by_key[key].anchor_ref for key in person_keys
                    ),
                },
                anchor=enrollment_ref,
            )
        )
    return out


def _sweep_c10(world: World) -> list[ConflictEntry]:
    """C10 -- one contact whose `ext` and `namedob` candidates resolve to two different,
    non-null students in `entity_link_candidates` (never over `entity_links`)."""
    out: list[ConflictEntry] = []
    for contact_ref, per_class in sorted(world.resolution.candidates_by_contact.items()):
        ext = per_class.get("ext", ())
        namedob = per_class.get("namedob", ())
        if len(ext) != 1 or len(namedob) != 1 or ext[0] == namedob[0]:
            continue
        contact = world.contacts[contact_ref]
        out.append(
            _entry(
                "C10",
                conflict_refs(
                    "C10", contact_refs=(contact_ref,), student_refs=(ext[0], namedob[0])
                ),
                {
                    "ext_resolved_ref": ext[0],
                    "namedob_resolved_ref": namedob[0],
                    "first_norm": norm_name(contact.get("first_name")),
                    "last_norm": norm_name(contact.get("last_name")),
                    "dob_norm": norm_dob(contact.get("dob")),
                },
                anchor=contact_ref,
            )
        )
    return out


def _sweep_c11(world: World) -> list[ConflictEntry]:
    """C11 -- two payments, equal `(payer_email_norm, amount_cents, type)`, `occurred_at`
    strictly within 600s, **both** resolving by `P1..P3` to the same person.

    Siblings share a payer address and a flat `fee`, so the same-person clause is what
    stops a legitimate household from firing this; a sibling pair resolves to two
    different persons and is never C11 (SS5.5 C11).
    """
    groups: dict[tuple[str, int, str], list[str]] = {}
    for ref, payment in sorted(world.payments.items()):
        key = (
            norm_email(payment.get("payer_email")) or "",
            int(payment.get("amount_cents", 0)),
            str(payment.get("type")),
        )
        groups.setdefault(key, []).append(ref)

    out: list[ConflictEntry] = []
    for key, refs in sorted(groups.items()):
        if len(refs) < 2:
            continue
        ordered = sorted(refs)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1 :]:
                left_person = world.resolution.payment_person.get(left)
                right_person = world.resolution.payment_person.get(right)
                if left_person is None or left_person != right_person:
                    continue
                left_at = _seconds(world.payments[left].get("occurred_at"))
                right_at = _seconds(world.payments[right].get("occurred_at"))
                if left_at is None or right_at is None:  # pragma: no cover
                    continue
                delta = abs(left_at - right_at)
                if delta >= C11_WINDOW_SECONDS:
                    continue
                out.append(
                    _entry(
                        "C11",
                        conflict_refs("C11", payment_refs=(left, right)),
                        {
                            "payer_email_norm": key[0],
                            "amount_cents": key[1],
                            "type": key[2],
                            "occurred_at_delta_seconds": delta,
                        },
                        anchor=f"{left}|{right}",
                    )
                )
    return out


def _sweep_c12(world: World) -> list[ConflictEntry]:
    """C12 -- `amount_cents` != the fee-schedule amount for the resolved `(program, type)`.

    Program comes from the `E1`/`E2`-attributed enrollment; failing that from
    `metadata.program`; failing that the rule is `unchecked` (SS4.4), so the sweep
    declines to emit.
    """
    out: list[ConflictEntry] = []
    for payment_ref, payment in sorted(world.payments.items()):
        person_key = world.resolution.payment_person.get(payment_ref)
        enrollment_ref = world.resolution.payment_enrollment.get(payment_ref)
        program = None
        if enrollment_ref is not None:
            program = norm_enum("program", world.enrollments[enrollment_ref].get("program"))
        if program is None:
            metadata = payment.get("metadata") or {}
            program = norm_enum("program", metadata.get("program"))
        if program is None:
            continue  # unchecked: enrollment_unattributed
        expected = fee_amount_cents(program, str(payment.get("type")))
        if expected is None or int(payment["amount_cents"]) == expected:
            continue
        if person_key is None:
            continue  # C2 territory; PRECEDENCE 3 would remove it anyway
        person = world.resolution.person_by_key[person_key]
        out.append(
            _entry(
                "C12",
                conflict_refs(
                    "C12", identity_refs=person.identity_refs, payment_refs=(payment_ref,)
                ),
                {
                    "amount_cents": int(payment["amount_cents"]),
                    "expected_amount_cents": expected,
                    "program_norm": program,
                    "type": str(payment.get("type")),
                },
                anchor=payment_ref,
            )
        )
    return out


def _sweep_c13(world: World) -> list[ConflictEntry]:
    """C13 -- a `refunded` payment whose four clauses all hold (SS5.5 C13).

    (a) it is the person's most recent payment of that type on the attributed
    enrollment; (b) no later `paid` payment of the same type exists for that person;
    (c) `refunded_at` post-dates the enrollment row's `updated_at`; (d) the enrollment
    is paid-implying **and** the student's status maps to `enrolled`.
    """
    out: list[ConflictEntry] = []
    for payment_ref, payment in sorted(world.payments.items()):
        if payment.get("status") != "refunded":
            continue
        person_key = world.resolution.payment_person.get(payment_ref)
        enrollment_ref = world.resolution.payment_enrollment.get(payment_ref)
        if person_key is None or enrollment_ref is None:
            continue  # unchecked: enrollment_unattributed
        person = world.resolution.person_by_key[person_key]
        payment_type = str(payment.get("type"))
        occurred = _seconds(payment.get("occurred_at"))
        same_type = [
            world.payments[ref]
            for ref in person.payment_refs
            if ref in world.payments and world.payments[ref].get("type") == payment_type
        ]
        if any((_seconds(other.get("occurred_at")) or 0) > (occurred or 0) for other in same_type):
            continue  # (a) not the most recent of its type
        if any(
            other.get("status") == "paid"
            and (_seconds(other.get("occurred_at")) or 0) > (occurred or 0)
            for other in same_type
        ):
            continue  # (b) superseded
        enrollment = world.enrollments[enrollment_ref]
        refunded_at = _seconds(payment.get("refunded_at"))
        updated_at = _seconds(enrollment.get("updated_at"))
        if refunded_at is None or updated_at is None or refunded_at <= updated_at:
            continue  # (c)
        funnel = norm_enum("stage", enrollment.get("stage"))
        student = world.students[person.student_ref] if person.student_ref else None
        status = None if student is None else norm_enum("status", student.get("status"))
        if funnel not in PAID_IMPLYING_STAGES:
            continue  # (d)
        if status is None or STATUS_TO_FUNNEL[status] != "enrolled":
            continue  # (d)
        out.append(
            _entry(
                "C13",
                conflict_refs(
                    "C13",
                    identity_refs=person.identity_refs,
                    payment_refs=(payment_ref,),
                    enrollment_refs=(enrollment_ref,),
                ),
                {
                    "refunded_at": payment.get("refunded_at"),
                    "enrollment.updated_at": enrollment.get("updated_at"),
                    "enrollment.stage_funnel": funnel,
                    "student.status": str(student.get("status")) if student else None,
                },
                anchor=payment_ref,
            )
        )
    return out


_SWEEPS = (
    ("C1", _sweep_c1),
    ("C2", _sweep_c2),
    ("C3", _sweep_c3),
    ("C4", _sweep_c4),
    ("C5", _sweep_c5),
    ("C6/C14", _sweep_c6_c14),
    ("C7", _sweep_c7),
    ("C8", _sweep_c8),
    ("C9", _sweep_c9),
    ("C10", _sweep_c10),
    ("C11", _sweep_c11),
    ("C12", _sweep_c12),
    ("C13", _sweep_c13),
)


def run_sweep(snapshot: Snapshot, resolution: Resolution) -> SweepResult:
    """Evaluate all fourteen predicates over the generation-3 world."""
    world = World.build(snapshot, resolution)
    entries: list[ConflictEntry] = []
    for _label, sweep in _SWEEPS:
        entries.extend(sweep(world))
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.type] = counts.get(entry.type, 0) + 1
    return SweepResult(entries=entries, counts=counts, world=world)
