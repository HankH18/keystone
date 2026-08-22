"""SS8 -- the emitted golden artifacts.

`golden/conflicts.json` is written **through the same `PRECEDENCE` filter the detector
applies** (`G32`): the generator plants intent, `recon.reference.apply_precedence`
decides which entries survive. There is deliberately no second implementation of the
filter here -- the contract suppresses C7 from a raw 875 down to 300 through three
separate rules, and two slightly different filters would be up to 575 false positives
against a golden count of 300.

`compound_with` is golden-side metadata only (SS8): it is populated **after** the
filter, so pairs removed by the mechanical suppressions never enter it, and A.5's
>=10% ratio is computed over surviving entries alone.

The clean sample takes the strict reading of `G28` and then goes one step further: a
sampled identity ref may not appear in the `entity_refs` of any **raw** sweep entry,
not merely of any surviving one. A ref that only a suppressed entry names is still a
ref some future detector could legitimately flag, and 1,000 sampled entities are the
one place in the grading contract where a false positive is asserted impossible.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from recon.er import Person, Resolution
from recon.normalize import norm_email, norm_enum, norm_name
from recon.reference import (
    CONFLICT_TYPES,
    apply_precedence,
    assert_unique_conflict_keys,
    conflict_key,
    fingerprint,
    household_key,
)

from .build import Dataset
from .rng import Rng
from .sweep import ConflictEntry, SweepResult

__all__ = ["GoldenSet", "build_golden"]

#: SS8 -- `golden/clean-sample.json` carries exactly this many entities.
CLEAN_SAMPLE_SIZE = 1000

#: SS8 -- `golden/expected-views.json` carries at least this many hand-checkable views.
EXPECTED_VIEW_COUNT = 25


@dataclass
class GoldenSet:
    conflicts: list[dict[str, Any]]
    clean_sample: list[dict[str, Any]]
    expected_views: list[dict[str, Any]]
    survivors: list[ConflictEntry]
    suppression_report: dict[int, int]
    compound_ratio: float
    fully_consistent_fraction: float
    entity_count: int
    inconsistent_entity_count: int
    fingerprints: list[str]


def _compound_map(survivors: Sequence[ConflictEntry]) -> dict[int, list[str]]:
    """SS8: for each entry, the sorted keys of the other **surviving** entries it overlaps."""
    by_ref: dict[str, list[int]] = {}
    for index, entry in enumerate(survivors):
        for ref in entry.entity_refs:
            by_ref.setdefault(ref, []).append(index)

    out: dict[int, list[str]] = {}
    for index, entry in enumerate(survivors):
        neighbours: set[int] = set()
        for ref in entry.entity_refs:
            neighbours.update(by_ref.get(ref, ()))
        neighbours.discard(index)
        out[index] = sorted(
            f"{survivors[other].type}|{','.join(sorted(survivors[other].entity_refs))}"
            for other in sorted(neighbours)
        )
    return out


def _oscillating_refs(dataset: Dataset) -> dict[str, set[str]]:
    """`contact_ref -> {field_path}` for every A -> B -> A field (SS7)."""
    out: dict[str, set[str]] = {}
    for row in dataset.oscillations:
        ref = f"crm:contact:{row['crm_id']}"
        out.setdefault(ref, set()).add(str(row["field_path"]))
    return out


def _person_view(
    dataset: Dataset, resolution: Resolution, world: Any, person: Person
) -> dict[str, Any]:
    """One unified cross-source entity view -- R10's join contract, hand-checkable."""
    student = world.students.get(person.student_ref) if person.student_ref else None
    contact = world.survived_contact(person)
    enrollment = world.survived_enrollment(person)
    deal = world.survived_deal(person)
    payments = [world.payments[ref] for ref in person.payment_refs if ref in world.payments]
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
                "type": world.payments[ref].get("type"),
                "status": world.payments[ref].get("status"),
                "amount_cents": world.payments[ref].get("amount_cents"),
            }
            for ref in person.payment_refs
            if ref in world.payments
        ],
        "deal_refs": list(person.deal_refs),
        "link_methods": sorted(
            {link.method for link in resolution.links if link.canonical_id == person.person_key}
        ),
    }


def build_golden(dataset: Dataset, sweep: SweepResult, resolution: Resolution) -> GoldenSet:
    """Filter, decorate and assemble every `golden/` artifact."""
    report: dict[int, int] = {}
    survivors: list[ConflictEntry] = apply_precedence(sweep.entries, report=report)
    assert_unique_conflict_keys(survivors)

    oscillating = _oscillating_refs(dataset)
    decorated: list[ConflictEntry] = []
    for entry in survivors:
        # SS7: the flag marks the entries "where **the conflict's field** oscillated".
        # An entry with no compared field (C1, C7, ...) has no field that could have
        # oscillated, so it never carries the flag -- the previous
        # `not entry.disagreeing_fields` disjunct handed a `grade` oscillation's flag to
        # every fieldless conflict that happened to share a person with it.
        flag = False
        for ref in entry.entity_refs:
            fields = oscillating.get(ref)
            if fields and entry.disagreeing_fields and fields & set(entry.disagreeing_fields):
                flag = True
        decorated.append(
            ConflictEntry(
                type=entry.type,
                rule_id=entry.rule_id,
                entity_refs=entry.entity_refs,
                sources_involved=entry.sources_involved,
                disagreeing_fields=entry.disagreeing_fields,
                observed_values=entry.observed_values,
                expected_verdict=entry.expected_verdict,
                oscillating=flag,
                anchor=entry.anchor,
            )
        )
    decorated.sort(key=conflict_key)

    compounds = _compound_map(decorated)
    conflicts = [entry.as_json(compounds[index]) for index, entry in enumerate(decorated)]
    fingerprints = [
        fingerprint(entry.type, entry.entity_refs, entry.disagreeing_fields, entry.observed_values)
        for entry in decorated
    ]

    compound_count = sum(1 for row in conflicts if row["compound_with"])
    compound_ratio = compound_count / len(conflicts) if conflicts else 0.0

    world = sweep.world
    conflicted_refs = {ref for entry in sweep.entries for ref in entry.entity_refs}
    surviving_refs = {ref for entry in decorated for ref in entry.entity_refs}

    inconsistent = sum(1 for person in resolution.persons if set(person.refs) & surviving_refs)
    entity_count = len(resolution.persons)
    fully_consistent = (entity_count - inconsistent) / entity_count if entity_count else 1.0

    clean_persons = [
        person
        for person in resolution.persons
        if not (set(person.refs) & conflicted_refs) and person.identity_refs
    ]
    clean_persons.sort(key=lambda person: person.identity_refs)
    if len(clean_persons) < CLEAN_SAMPLE_SIZE:  # pragma: no cover - the clean majority is huge
        raise RuntimeError(
            f"only {len(clean_persons)} conflict-free entities available; "
            f"golden/clean-sample.json needs {CLEAN_SAMPLE_SIZE} (G28)"
        )
    # A.6 says "1,000 **randomly-sampled** clean entities". A stride walk over the
    # sorted list is not a random sample: `crm:contact:CRM-NNNNNNN` sorts in
    # construction order, so ~44% of the sample was an arithmetic progression through
    # the build sequence. The draw is made with the run's own seed through a forked
    # PRNG, so it is still fully deterministic and `G30`-safe (the list it draws from
    # is sorted first), and it now moves with `--seed`.
    sampler = Rng(dataset.seed).fork("clean-sample")
    sampled = sampler.sample(clean_persons, CLEAN_SAMPLE_SIZE)
    sampled.sort(key=lambda person: person.identity_refs)
    clean_sample = [
        {
            "person_key": person.person_key,
            "anchor_ref": person.anchor_ref,
            "identity_refs": list(person.identity_refs),
            "entity_refs": list(person.refs),
            "sources": sorted({ref.split(":", 1)[0] for ref in person.refs}),
        }
        for person in sampled
    ]
    clean_sample.sort(key=lambda row: tuple(row["identity_refs"]))

    views_clean = sampled[:: max(1, len(sampled) // (EXPECTED_VIEW_COUNT - 10))][:15]
    conflicted_persons = [
        person
        for person in resolution.persons
        if person.student_ref is not None and set(person.refs) & surviving_refs
    ]
    conflicted_persons.sort(key=lambda person: person.anchor_ref)
    views_conflicted = conflicted_persons[:: max(1, len(conflicted_persons) // 10)][:10]
    view_persons = {person.person_key: person for person in (*views_clean, *views_conflicted)}
    expected_views = [
        _person_view(dataset, resolution, world, person)
        for _key, person in sorted(view_persons.items())
    ]
    expected_views.sort(key=lambda row: str(row["anchor_ref"]))
    if len(expected_views) < EXPECTED_VIEW_COUNT:  # pragma: no cover
        raise RuntimeError(
            f"golden/expected-views.json has {len(expected_views)} views; "
            f"SS8 requires >= {EXPECTED_VIEW_COUNT}"
        )

    return GoldenSet(
        conflicts=conflicts,
        clean_sample=clean_sample,
        expected_views=expected_views,
        survivors=decorated,
        suppression_report=report,
        compound_ratio=compound_ratio,
        fully_consistent_fraction=fully_consistent,
        entity_count=entity_count,
        inconsistent_entity_count=inconsistent,
        fingerprints=fingerprints,
    )


def counts_by_type(entries: Iterable[ConflictEntry]) -> dict[str, int]:
    """Per-type counts over the committed `CONFLICT_TYPES` order, zeros included."""
    counts = dict.fromkeys(CONFLICT_TYPES, 0)
    for entry in entries:
        counts[entry.type] += 1
    return counts
