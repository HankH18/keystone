"""Deterministic entity resolution -- the L/P/D/E cascades (contract v2 SS4).

This is the third **shared module** (SS0): the seed generator runs it in pass 2 to
derive every `entity_refs` value it writes into `golden/` (`G31`), and the detector
(`recon.suite`, the invariant runner) resolves the ingested snapshot with the same
code. Neither side may re-implement any rule here.

Layering (one direction, no cycles):

    recon.normalize  ->  (nothing in `recon`)
    recon.reference  ->  recon.normalize
    recon.er         ->  recon.normalize, recon.reference

Nothing in this module reads the clock, the environment, `random`, or `uuid4`, and
every iteration over a mapping or a set is explicitly sorted, so the output is a
pure function of the snapshot -- `PYTHONHASHSEED` cannot move a byte.

Fuzzy similarity contributes **evidence signals only** and never a link decision
(SS4 preamble); there is deliberately no fuzzy matcher here at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from recon.normalize import match_keys, norm_email, norm_enum, norm_name
from recon.reference import (
    anchor_ref as _anchor_ref,
)
from recon.reference import (
    household_key,
    household_members_appdb,
    make_ref,
    person_key,
)

__all__ = [
    "CONTACT_STUDENT_METHODS",
    "DEAL_PERSON_METHODS",
    "LINK_CLASSES",
    "LINK_METHODS",
    "PAYMENT_ENROLLMENT_METHODS",
    "PAYMENT_PERSON_METHODS",
    "EntityLink",
    "LinkCandidate",
    "Person",
    "Resolution",
    "Snapshot",
    "resolve",
]

#: SS4.7 -- `entity_links.link_class`, committed vocabulary.
LINK_CLASSES: tuple[str, ...] = (
    "contact_student",
    "payment_person",
    "payment_enrollment",
    "deal_person",
)

CONTACT_STUDENT_METHODS: tuple[str, ...] = ("L1", "L2", "L3")
PAYMENT_PERSON_METHODS: tuple[str, ...] = ("P1", "P2", "P3")
PAYMENT_ENROLLMENT_METHODS: tuple[str, ...] = ("E1", "E2")
DEAL_PERSON_METHODS: tuple[str, ...] = ("D2",)

#: SS4.7 -- `method` is the id of the **first** cascade rule that fired for a pair.
LINK_METHODS: tuple[str, ...] = (
    *CONTACT_STUDENT_METHODS,
    *PAYMENT_PERSON_METHODS,
    *PAYMENT_ENROLLMENT_METHODS,
    *DEAL_PERSON_METHODS,
)


# ======================================================================================
# Records in, links out
# ======================================================================================


@dataclass(frozen=True, slots=True)
class EntityLink:
    """One **accepted** link (SS4.7). `entity_links` holds accepted links only."""

    canonical_id: str
    source_ref: str
    resolved_ref: str
    link_class: str
    method: str
    generation: int

    def as_row(self) -> dict[str, object]:
        return {
            "canonical_id": self.canonical_id,
            "source_ref": self.source_ref,
            "resolved_ref": self.resolved_ref,
            "link_class": self.link_class,
            "method": self.method,
            "generation": self.generation,
        }


@dataclass(frozen=True, slots=True)
class LinkCandidate:
    """One `match_keys` candidate pair (SS4.7) -- persisted regardless of outcome.

    `R-010` (C10) is evaluated over these, **never** over `entity_links`, which is
    why a discarded resolution still has to be written down.
    """

    source_ref: str
    key_class: str
    resolved_ref: str
    generation: int
    decision: str  # "accepted" | "discarded"
    reason: str

    def as_row(self) -> dict[str, object]:
        return {
            "source_ref": self.source_ref,
            "key_class": self.key_class,
            "resolved_ref": self.resolved_ref,
            "generation": self.generation,
            "decision": self.decision,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Person:
    """One resolved person (SS5.2): a `person_key` with >=1 identity ref.

    An **unattributed payment** is also an entity under SS5.2 and is represented
    here as a person whose only ref is its own `payments:payment:` ref.
    """

    person_key: str
    anchor_ref: str
    refs: tuple[str, ...]
    identity_refs: tuple[str, ...]
    student_ref: str | None
    contact_refs: tuple[str, ...]
    enrollment_refs: tuple[str, ...]
    payment_refs: tuple[str, ...]
    deal_refs: tuple[str, ...]

    @property
    def is_unattributed_payment(self) -> bool:
        return self.student_ref is None and not self.contact_refs


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One generation's complete snapshot of all five entity types (SS7)."""

    generation: int
    contacts: Sequence[Mapping[str, object]] = ()
    deals: Sequence[Mapping[str, object]] = ()
    students: Sequence[Mapping[str, object]] = ()
    enrollments: Sequence[Mapping[str, object]] = ()
    payments: Sequence[Mapping[str, object]] = ()


@dataclass(frozen=True, slots=True)
class Resolution:
    """Everything the cascade decided for one generation."""

    generation: int
    links: tuple[EntityLink, ...]
    candidates: tuple[LinkCandidate, ...]
    persons: tuple[Person, ...]
    person_by_key: Mapping[str, Person]
    person_by_ref: Mapping[str, str]
    contact_student: Mapping[str, str]
    contact_method: Mapping[str, str]
    student_contacts: Mapping[str, tuple[str, ...]]
    payment_person: Mapping[str, str]
    payment_method: Mapping[str, str]
    payment_enrollment: Mapping[str, str]
    payment_enrollment_method: Mapping[str, str]
    deal_persons: Mapping[str, tuple[str, ...]]
    unattributed_payment_refs: tuple[str, ...]
    candidates_by_contact: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)

    def person_for(self, ref: str) -> Person | None:
        key = self.person_by_ref.get(ref)
        return None if key is None else self.person_by_key[key]


def _get(record: Mapping[str, object], name: str) -> object:
    return record.get(name)


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


# ======================================================================================
# SS4.2 contact <-> student
# ======================================================================================


def _link_contacts_students(
    contacts: Sequence[Mapping[str, object]],
    students: Sequence[Mapping[str, object]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Run `L1`/`L2`/`L3` and return `(contact_ref -> student_ref, contact_ref -> method)`.

    Link on the **first rule that fires** (SS4.2). A candidate pair is rejected when
    either side is already `L1`-linked to a *different* record -- hard keys win.
    """
    student_by_id: dict[str, Mapping[str, object]] = {}
    by_email_name: dict[tuple[str, str, str], list[Mapping[str, object]]] = {}
    by_namedob: dict[tuple[str, str, str], list[Mapping[str, object]]] = {}

    # Sorted, like every other input this module walks: the `by_email_name` /
    # `by_namedob` bucket lists below are read with a `break` on the FIRST entry, so
    # the caller's row order would otherwise decide which student an L2/L3 link
    # resolves to -- and through it a golden `entity_refs` value -- the moment G5's
    # name-key uniqueness is relaxed or a query arrives without an ORDER BY.
    for student in sorted(students, key=lambda row: str(_get(row, "id"))):
        sid = _str_or_none(_get(student, "id"))
        if sid is None:
            continue
        student_by_id[sid] = student
        first = norm_name(_get(student, "first_name"))
        last = norm_name(_get(student, "last_name"))
        for raw_email in (_get(student, "guardian_email"), _get(student, "guardian2_email")):
            email = norm_email(raw_email)
            if email is not None and first is not None and last is not None:
                by_email_name.setdefault((email, first, last), []).append(student)
        dob = _str_or_none(_get(student, "dob"))
        dob_norm = None if dob is None else _norm_dob(dob)
        if first is not None and last is not None and dob_norm is not None:
            by_namedob.setdefault((first, last, dob_norm), []).append(student)

    ordered_contacts = sorted(contacts, key=lambda c: str(_get(c, "crm_id")))

    linked: dict[str, str] = {}
    method: dict[str, str] = {}
    l1_student_contacts: dict[str, set[str]] = {}

    # -- L1 first, globally: hard keys win, so they must all be known before L2/L3 runs.
    for contact in ordered_contacts:
        contact_ref = make_ref("crm", "contact", _get(contact, "crm_id"))
        external_id = _str_or_none(_get(contact, "external_id"))
        if external_id is None or external_id not in student_by_id:
            continue
        student_ref = make_ref("appdb", "student", external_id)
        linked[contact_ref] = student_ref
        method[contact_ref] = "L1"
        l1_student_contacts.setdefault(student_ref, set()).add(contact_ref)

    for contact in ordered_contacts:
        contact_ref = make_ref("crm", "contact", _get(contact, "crm_id"))
        if contact_ref in linked:
            continue
        first = norm_name(_get(contact, "first_name"))
        last = norm_name(_get(contact, "last_name"))
        email = norm_email(_get(contact, "email"))
        dob_norm = _norm_dob(_str_or_none(_get(contact, "dob")))

        chosen: tuple[str, str] | None = None
        if email is not None and first is not None and last is not None:
            for student in by_email_name.get((email, first, last), ()):
                student_ref = make_ref("appdb", "student", _get(student, "id"))
                if l1_student_contacts.get(student_ref):
                    continue  # hard key already owns this student
                chosen = (student_ref, "L2")
                break
        if chosen is None and first is not None and last is not None and dob_norm is not None:
            for student in by_namedob.get((first, last, dob_norm), ()):
                student_ref = make_ref("appdb", "student", _get(student, "id"))
                if l1_student_contacts.get(student_ref):
                    continue
                chosen = (student_ref, "L3")
                break
        if chosen is not None:
            linked[contact_ref] = chosen[0]
            method[contact_ref] = chosen[1]

    return linked, method


def _norm_dob(value: object) -> str | None:
    from recon.normalize import norm_dob

    return norm_dob(value)  # type: ignore[arg-type]


# ======================================================================================
# SS4.3 / SS4.4 payment <-> person, payment <-> enrollment
# ======================================================================================


def _attribute_payments(
    payments: Sequence[Mapping[str, object]],
    students: Sequence[Mapping[str, object]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Run `P1`/`P2`/`P3`; return `(payment_ref -> student_ref, payment_ref -> method)`.

    `P2`/`P3` consult **only** the set of `household_key` values -- the *primary*
    `guardian_email` values (SS4.8). `guardian2_email` participates in `L2` and
    nowhere else. No name splitting happens on either side; both sides call the same
    `norm_name`. An unattributable payment is C2, never a guess (SS4.3).
    """
    households = household_members_appdb(students)
    student_by_id = {
        sid: student
        for student in students
        if (sid := _str_or_none(_get(student, "id"))) is not None
    }

    attributed: dict[str, str] = {}
    method: dict[str, str] = {}

    for payment in sorted(payments, key=lambda p: str(_get(p, "payment_id"))):
        payment_ref = make_ref("payments", "payment", _get(payment, "payment_id"))
        external_ref = _str_or_none(_get(payment, "external_ref"))
        if external_ref is not None and external_ref in student_by_id:
            attributed[payment_ref] = make_ref("appdb", "student", external_ref)
            method[payment_ref] = "P1"
            continue

        key = norm_email(_get(payment, "payer_email"))
        members = households.get(key) if key is not None else None
        if not members:
            continue

        metadata = _get(payment, "metadata")
        meta: Mapping[str, object] = metadata if isinstance(metadata, Mapping) else {}
        first = norm_name(meta.get("student_first_name"))
        last = norm_name(meta.get("student_last_name"))
        if first is not None and last is not None:
            matches = [
                student
                for student in members
                if norm_name(_get(student, "first_name")) == first
                and norm_name(_get(student, "last_name")) == last
            ]
            if len(matches) == 1:
                attributed[payment_ref] = make_ref("appdb", "student", _get(matches[0], "id"))
                method[payment_ref] = "P2"
                continue

        if len(members) == 1:
            attributed[payment_ref] = make_ref("appdb", "student", _get(members[0], "id"))
            method[payment_ref] = "P3"

    return attributed, method


def _attribute_enrollments(
    payments: Sequence[Mapping[str, object]],
    enrollments_by_student: Mapping[str, tuple[Mapping[str, object], ...]],
    payment_student: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """`E1`/`E2` (SS4.4) for payments already attributed to a person by `P1..P3`.

    Under `G12` a person has at most one enrollment, so `E1` and `E2` can never
    disagree; `E1` is retained as the documented attribution semantics and as the
    branch that survives a relaxation of the 1:1 rule.
    """
    attributed: dict[str, str] = {}
    method: dict[str, str] = {}

    for payment in sorted(payments, key=lambda p: str(_get(p, "payment_id"))):
        payment_ref = make_ref("payments", "payment", _get(payment, "payment_id"))
        student_ref = payment_student.get(payment_ref)
        if student_ref is None:
            continue
        candidates = enrollments_by_student.get(student_ref, ())
        if not candidates:
            continue

        metadata = _get(payment, "metadata")
        meta: Mapping[str, object] = metadata if isinstance(metadata, Mapping) else {}
        program = norm_enum("program", meta.get("program"))  # type: ignore[arg-type]
        if program is not None:
            matches = [
                enrollment
                for enrollment in candidates
                if norm_enum("program", _get(enrollment, "program")) == program  # type: ignore[arg-type]
            ]
            if len(matches) == 1:
                attributed[payment_ref] = make_ref("appdb", "enrollment", _get(matches[0], "id"))
                method[payment_ref] = "E1"
                continue

        if len(candidates) == 1:
            attributed[payment_ref] = make_ref("appdb", "enrollment", _get(candidates[0], "id"))
            method[payment_ref] = "E2"

    return attributed, method


# ======================================================================================
# SS4.7 candidates
# ======================================================================================


def _build_candidates(
    contacts: Sequence[Mapping[str, object]],
    payments: Sequence[Mapping[str, object]],
    students: Sequence[Mapping[str, object]],
    generation: int,
    contact_student: Mapping[str, str],
) -> tuple[list[LinkCandidate], dict[str, dict[str, tuple[str, ...]]]]:
    """Persist **every** `match_keys` candidate pair, accepted or discarded (SS4.7).

    Keys are resolved against app-DB students, which is the only target `R-010`
    consults: C10 asks whether one contact's `ext` and `namedob` keys reach two
    *different* students.
    """
    index: dict[tuple[str, object], list[str]] = {}
    for student in students:
        student_ref = make_ref("appdb", "student", _get(student, "id"))
        for key in match_keys(student, "appdb.student"):
            index.setdefault((key.key_class, key.value), []).append(student_ref)
    resolved_index = {key: tuple(sorted(set(refs))) for key, refs in sorted(index.items())}

    rows: list[LinkCandidate] = []
    by_contact: dict[str, dict[str, tuple[str, ...]]] = {}

    for contact in sorted(contacts, key=lambda c: str(_get(c, "crm_id"))):
        contact_ref = make_ref("crm", "contact", _get(contact, "crm_id"))
        accepted = contact_student.get(contact_ref)
        per_class: dict[str, list[str]] = {}
        for key in match_keys(contact, "crm.contact"):
            for student_ref in resolved_index.get((key.key_class, key.value), ()):
                per_class.setdefault(key.key_class, []).append(student_ref)
                rows.append(
                    LinkCandidate(
                        source_ref=contact_ref,
                        key_class=key.key_class,
                        resolved_ref=student_ref,
                        generation=generation,
                        decision="accepted" if student_ref == accepted else "discarded",
                        reason="cascade_link" if student_ref == accepted else "not_first_rule",
                    )
                )
        if per_class:
            by_contact[contact_ref] = {
                cls: tuple(sorted(set(refs))) for cls, refs in sorted(per_class.items())
            }

    for payment in sorted(payments, key=lambda p: str(_get(p, "payment_id"))):
        payment_ref = make_ref("payments", "payment", _get(payment, "payment_id"))
        for key in match_keys(payment, "payments.payment"):
            for student_ref in resolved_index.get((key.key_class, key.value), ()):
                rows.append(
                    LinkCandidate(
                        source_ref=payment_ref,
                        key_class=key.key_class,
                        resolved_ref=student_ref,
                        generation=generation,
                        decision="discarded",
                        reason="candidate_only",
                    )
                )

    rows.sort(key=lambda r: (r.source_ref, r.key_class, r.resolved_ref))
    return rows, by_contact


# ======================================================================================
# The cascade
# ======================================================================================


def resolve(snapshot: Snapshot) -> Resolution:
    """Run the whole SS4 cascade over one generation snapshot.

    Returns accepted links, every candidate pair, and the assembled person set.
    Deterministic and order-independent: every input sequence is re-sorted by its
    source ref before it is walked.
    """
    generation = snapshot.generation
    contacts = list(snapshot.contacts)
    deals = list(snapshot.deals)
    students = list(snapshot.students)
    enrollments = list(snapshot.enrollments)
    payments = list(snapshot.payments)

    contact_student, contact_method = _link_contacts_students(contacts, students)

    student_contacts: dict[str, list[str]] = {}
    for contact_ref, student_ref in sorted(contact_student.items()):
        student_contacts.setdefault(student_ref, []).append(contact_ref)

    enrollments_by_student: dict[str, list[Mapping[str, object]]] = {}
    for enrollment in sorted(enrollments, key=lambda e: str(_get(e, "id"))):
        sid = _str_or_none(_get(enrollment, "student_id"))
        if sid is None:
            continue
        enrollments_by_student.setdefault(make_ref("appdb", "student", sid), []).append(enrollment)
    frozen_enrollments = {ref: tuple(rows) for ref, rows in sorted(enrollments_by_student.items())}

    payment_student, payment_method = _attribute_payments(payments, students)
    payment_enrollment, payment_enrollment_method = _attribute_enrollments(
        payments, frozen_enrollments, payment_student
    )

    candidates, candidates_by_contact = _build_candidates(
        contacts, payments, students, generation, contact_student
    )

    # ---- person assembly -------------------------------------------------------------
    refs_by_student: dict[str, set[str]] = {}
    for student in students:
        student_ref = make_ref("appdb", "student", _get(student, "id"))
        refs_by_student[student_ref] = {student_ref}
    for student_ref, rows in frozen_enrollments.items():
        bucket = refs_by_student.get(student_ref)
        if bucket is None:
            continue
        for enrollment in rows:
            bucket.add(make_ref("appdb", "enrollment", _get(enrollment, "id")))
    for contact_ref, student_ref in sorted(contact_student.items()):
        bucket = refs_by_student.get(student_ref)
        if bucket is not None:
            bucket.add(contact_ref)
    for payment_ref, student_ref in sorted(payment_student.items()):
        bucket = refs_by_student.get(student_ref)
        if bucket is not None:
            bucket.add(payment_ref)

    contact_ids: dict[str, str] = {}
    for contact in contacts:
        crm_id = _str_or_none(_get(contact, "crm_id"))
        if crm_id is not None:
            contact_ids[crm_id] = make_ref("crm", "contact", crm_id)

    # A lead contact -- one with no student link -- is its own person (SS11.4, `G11`).
    lead_refs: dict[str, set[str]] = {
        contact_ref: {contact_ref}
        for contact_ref in sorted(contact_ids.values())
        if contact_ref not in contact_student
    }

    owner_of_contact: dict[str, str] = {}
    for student_ref, refs in sorted(refs_by_student.items()):
        for ref in refs:
            if ref.startswith("crm:contact:"):
                owner_of_contact[ref] = student_ref

    deal_persons_by_seed: dict[str, list[str]] = {}
    for deal in sorted(deals, key=lambda d: str(_get(d, "deal_id"))):
        deal_ref = make_ref("crm", "deal", _get(deal, "deal_id"))
        associated = _get(deal, "associated_contact_ids")
        seeds: list[str] = []
        if isinstance(associated, Sequence) and not isinstance(associated, str | bytes):
            for raw in associated:
                crm_id = _str_or_none(raw)
                if crm_id is None:
                    continue
                contact_ref = contact_ids.get(crm_id)
                if contact_ref is None:
                    continue
                seed = owner_of_contact.get(contact_ref) or (
                    contact_ref if contact_ref in lead_refs else None
                )
                if seed is None:
                    continue
                if seed not in seeds:
                    seeds.append(seed)
                bucket = refs_by_student.get(seed) or lead_refs.get(seed)
                if bucket is not None:
                    bucket.add(deal_ref)
        deal_persons_by_seed[deal_ref] = sorted(seeds)

    unattributed_payment_refs: list[str] = []
    for payment in sorted(payments, key=lambda p: str(_get(p, "payment_id"))):
        payment_ref = make_ref("payments", "payment", _get(payment, "payment_id"))
        if payment_ref not in payment_student:
            unattributed_payment_refs.append(payment_ref)

    persons: list[Person] = []
    person_by_ref: dict[str, str] = {}
    seed_to_key: dict[str, str] = {}

    def _build(seed: str, refs: Iterable[str], student_ref: str | None) -> Person:
        ordered = tuple(sorted(set(refs)))
        anchor = _anchor_ref(ordered)
        key = str(person_key(ordered))
        person = Person(
            person_key=key,
            anchor_ref=anchor,
            refs=ordered,
            identity_refs=tuple(
                ref
                for ref in ordered
                if ref.startswith(("appdb:student:", "crm:contact:"))
                or (ref.startswith("payments:payment:") and student_ref is None)
            ),
            student_ref=student_ref,
            contact_refs=tuple(r for r in ordered if r.startswith("crm:contact:")),
            enrollment_refs=tuple(r for r in ordered if r.startswith("appdb:enrollment:")),
            payment_refs=tuple(r for r in ordered if r.startswith("payments:payment:")),
            deal_refs=tuple(r for r in ordered if r.startswith("crm:deal:")),
        )
        seed_to_key[seed] = key
        for ref in ordered:
            person_by_ref.setdefault(ref, key)
        return person

    for student_ref, refs in sorted(refs_by_student.items()):
        persons.append(_build(student_ref, refs, student_ref))
    for contact_ref, refs in sorted(lead_refs.items()):
        persons.append(_build(contact_ref, refs, None))
    for payment_ref in unattributed_payment_refs:
        persons.append(_build(payment_ref, {payment_ref}, None))

    person_by_key = {person.person_key: person for person in persons}

    links: list[EntityLink] = []
    for contact_ref, student_ref in sorted(contact_student.items()):
        links.append(
            EntityLink(
                canonical_id=seed_to_key[student_ref],
                source_ref=contact_ref,
                resolved_ref=student_ref,
                link_class="contact_student",
                method=contact_method[contact_ref],
                generation=generation,
            )
        )
    for payment_ref, student_ref in sorted(payment_student.items()):
        if student_ref not in seed_to_key:
            continue
        links.append(
            EntityLink(
                canonical_id=seed_to_key[student_ref],
                source_ref=payment_ref,
                resolved_ref=student_ref,
                link_class="payment_person",
                method=payment_method[payment_ref],
                generation=generation,
            )
        )
    for payment_ref, enrollment_ref in sorted(payment_enrollment.items()):
        student_ref = payment_student[payment_ref]
        if student_ref not in seed_to_key:
            continue
        links.append(
            EntityLink(
                canonical_id=seed_to_key[student_ref],
                source_ref=payment_ref,
                resolved_ref=enrollment_ref,
                link_class="payment_enrollment",
                method=payment_enrollment_method[payment_ref],
                generation=generation,
            )
        )
    deal_persons: dict[str, tuple[str, ...]] = {}
    for deal_ref, seeds in sorted(deal_persons_by_seed.items()):
        keys = tuple(sorted({seed_to_key[seed] for seed in seeds if seed in seed_to_key}))
        deal_persons[deal_ref] = keys
        for seed in seeds:
            if seed not in seed_to_key:
                continue
            links.append(
                EntityLink(
                    canonical_id=seed_to_key[seed],
                    source_ref=deal_ref,
                    resolved_ref=seed,
                    link_class="deal_person",
                    method="D2",
                    generation=generation,
                )
            )

    links.sort(key=lambda link: (link.link_class, link.source_ref, link.resolved_ref))

    return Resolution(
        generation=generation,
        links=tuple(links),
        candidates=tuple(candidates),
        persons=tuple(persons),
        person_by_key=person_by_key,
        person_by_ref=person_by_ref,
        contact_student=dict(sorted(contact_student.items())),
        contact_method=dict(sorted(contact_method.items())),
        student_contacts={ref: tuple(v) for ref, v in sorted(student_contacts.items())},
        payment_person={
            ref: seed_to_key[student_ref]
            for ref, student_ref in sorted(payment_student.items())
            if student_ref in seed_to_key
        },
        payment_method=dict(sorted(payment_method.items())),
        payment_enrollment=dict(sorted(payment_enrollment.items())),
        payment_enrollment_method=dict(sorted(payment_enrollment_method.items())),
        deal_persons=deal_persons,
        unattributed_payment_refs=tuple(unattributed_payment_refs),
        candidates_by_contact=candidates_by_contact,
    )


def household_keys(students: Iterable[Mapping[str, object]]) -> tuple[str, ...]:
    """Sorted `household_key` values of `students` (SS4.8) -- the only set `P2`/`P3` read."""
    return tuple(sorted({k for s in students if (k := household_key(s)) is not None}))
