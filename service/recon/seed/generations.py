"""SS7 generations: three **complete snapshots** per source, not deltas.

Each `fixtures/{source}/gen{N}/*.jsonl` is the whole source at generation N; records
unchanged since N-1 are re-emitted verbatim. **Absence of a `natural_key` from the
gen-3 snapshot IS a deletion** (SS12 D-9), and it is the only way C8's dropped sibling
and C9's non-existent deal are representable at all.

Two things vary across generations and nothing else:

1. **Deletions.** The 75 C8 `crm` contacts, the 50 C9 branch-1 deals and the 75 C8
   `payments` records are present in generations 1-2 and absent from generation 3, which
   is what makes SS9.1(a)'s gen-1 counts 40,075 / 15,050 / 18,075 against A.1's gen-3
   40,000 / 15,000 / 18,000 (SS12 D-12).
2. **The A -> B -> A oscillation.** >=25 `(person, field)` pairs carry a value the app DB
   disagrees with in gen 1, the corrected value in gen 2, and the disagreeing value
   again in gen 3 -- A.4's "integration that re-asserts stale data after correction".

**Flagged as SS12 D-14.** A.4 also says gen 2 "adds records", but SS9.1(a) pins the gen-1
counts at exactly gen-3-plus-deletions (40,075 / 15,050 / 18,075), which leaves no room
for a record that first appears in gen 2 and survives into gen 3, and a record present
only in gen 2 would change nothing observable because invariants read generation 3 only.
The conservative reading -- taken here -- is that the gen-1 record set is complete and
generation 2 changes values only. The consequence is recorded in the contract's
divergence table rather than left as a comment: the per-generation append path and the
`source_generations` ledger are exercised with zero arrivals, and a revision that wants
arrivals has to move SS9.1(a)'s pinned gen-1 numbers first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from recon.er import Snapshot

from .build import Dataset

__all__ = ["GENERATIONS", "SOURCE_FILES", "snapshot", "snapshot_records"]

GENERATIONS: tuple[int, ...] = (1, 2, 3)

#: The emitted fixture layout: `fixtures/{source}/gen{N}/{entity}.jsonl`.
SOURCE_FILES: tuple[tuple[str, str], ...] = (
    ("appdb", "student"),
    ("appdb", "enrollment"),
    ("crm", "contact"),
    ("crm", "deal"),
    ("payments", "payment"),
)

_NATURAL_KEY: dict[str, str] = {
    "contact": "crm_id",
    "deal": "deal_id",
    "student": "id",
    "enrollment": "id",
    "payment": "payment_id",
}


def _copy(record: Mapping[str, Any]) -> dict[str, Any]:
    """A copy deep enough that a per-generation edit cannot reach another generation."""
    clone = dict(record)
    metadata = clone.get("metadata")
    if isinstance(metadata, Mapping):
        clone["metadata"] = dict(metadata)
    associated = clone.get("associated_contact_ids")
    if isinstance(associated, list):
        clone["associated_contact_ids"] = list(associated)
    return clone


def _oscillated_value(dataset: Dataset, generation: int, crm_id: str, field_path: str) -> Any:
    for row in dataset.oscillations:
        if row["crm_id"] == crm_id and row["field_path"] == field_path:
            return row[f"gen{generation}"]
    return _MISSING


_MISSING = object()


def snapshot_records(
    dataset: Dataset, source: str, entity_type: str, generation: int
) -> list[dict[str, Any]]:
    """The complete `(source, entity_type)` snapshot at `generation`, sorted by PK."""
    if generation not in GENERATIONS:
        raise ValueError(f"unknown generation {generation!r}; expected one of {GENERATIONS}")

    if (source, entity_type) == ("crm", "contact"):
        rows, deleted = dataset.contacts, dataset.deleted_contact_ids
    elif (source, entity_type) == ("crm", "deal"):
        rows, deleted = dataset.deals, dataset.deleted_deal_ids
    elif (source, entity_type) == ("appdb", "student"):
        rows, deleted = dataset.students, set()
    elif (source, entity_type) == ("appdb", "enrollment"):
        rows, deleted = dataset.enrollments, set()
    elif (source, entity_type) == ("payments", "payment"):
        rows, deleted = dataset.payments, dataset.deleted_payment_ids
    else:  # pragma: no cover - the five pairs are the whole vocabulary
        raise ValueError(f"unknown source/entity {source!r}/{entity_type!r}")

    key = _NATURAL_KEY[entity_type]
    oscillating = {(str(row["crm_id"]), str(row["field_path"])) for row in dataset.oscillations}

    out: list[dict[str, Any]] = []
    for record in rows:
        natural_key = str(record[key])
        if generation == 3 and natural_key in deleted:
            continue
        clone = _copy(record)
        if entity_type == "contact" and generation != 3:
            for field_path, column in (
                ("crm.contact.grade", "grade"),
                ("crm.contact.lifecycle_stage", "lifecycle_stage"),
            ):
                if (natural_key, field_path) in oscillating:
                    value = _oscillated_value(dataset, generation, natural_key, field_path)
                    if value is not _MISSING:
                        clone[column] = value
        out.append(clone)

    out.sort(key=lambda row: str(row[key]))
    return out


def snapshot(dataset: Dataset, generation: int) -> Snapshot:
    """The whole-world snapshot at `generation`, ready for `recon.er.resolve`."""
    return Snapshot(
        generation=generation,
        contacts=snapshot_records(dataset, "crm", "contact", generation),
        deals=snapshot_records(dataset, "crm", "deal", generation),
        students=snapshot_records(dataset, "appdb", "student", generation),
        enrollments=snapshot_records(dataset, "appdb", "enrollment", generation),
        payments=snapshot_records(dataset, "payments", "payment", generation),
    )


def expected_counts(dataset: Dataset) -> dict[str, dict[str, int]]:
    """SS5.3's completeness ledger input: expected gen-N record count per pair."""
    counts: dict[str, dict[str, int]] = {}
    for generation in GENERATIONS:
        bucket: dict[str, int] = {}
        for source, entity_type in SOURCE_FILES:
            bucket[f"{source}.{entity_type}"] = len(
                snapshot_records(dataset, source, entity_type, generation)
            )
        counts[f"gen{generation}"] = bucket
    return counts


def sorted_by_key(records: Sequence[Mapping[str, Any]], entity_type: str) -> list[dict[str, Any]]:
    """Records in the fixed sorted order SS8 pins for every emitted file."""
    key = _NATURAL_KEY[entity_type]
    return sorted((dict(record) for record in records), key=lambda row: str(row[key]))
