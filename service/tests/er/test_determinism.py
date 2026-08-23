"""R9: two materialization runs over identical input produce identical links.

Proved by **comparing**, not by asserting a property of the code. Four
comparisons, each of which fails differently:

1. two independent runs of load + cascade + row building produce equal
   `entity_links` rows;
2. the rows actually **persisted** equal those runs, so what is in the database is
   what a fresh run would write and not a lucky historical artefact;
3. a run over the *same records in reversed order* produces the same links,
   persons and candidates -- because the realistic way determinism dies here is a
   query without `ORDER BY`, not a random number;
4. `entities.current` matches a freshly computed view for every entity: the
   canonical view is what the API serves, and a stable link set with a wobbling
   view would still be a non-deterministic pipeline.
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from recon.er import Snapshot, resolve
from recon.resolve import (
    candidate_rows,
    entity_rows,
    lineage_rows,
    link_rows,
    load_snapshot,
    person_view,
    resolve_generation,
    tenant_for,
)
from tests.er.dataset import Dataset

_PERSISTED_LINKS = text(
    """
    SELECT canonical_id::text AS canonical_id, source_id, source_key, source_ref,
           method, generation
      FROM entity_links
     ORDER BY source_id, source_key
    """
)

_PERSISTED_ENTITIES = text(
    "SELECT canonical_id::text AS canonical_id, entity_type, current FROM entities"
)


def test_two_runs_produce_identical_links(reader: Engine, dataset: Dataset) -> None:
    """Run the whole thing twice from the database and diff the rows."""
    with reader.connect() as conn:
        first = link_rows(resolve_generation(conn, dataset.generation))
    with reader.connect() as conn:
        second = link_rows(resolve_generation(conn, dataset.generation))

    assert first == second
    assert len(first) == dataset.report.links

    differing = [(a, b) for a, b in zip(first, second, strict=True) if a != b]
    assert not differing, differing[:3]


def test_persisted_links_equal_a_fresh_run(reader: Engine, dataset: Dataset) -> None:
    """What is in `entity_links` is exactly what a fresh cascade produces now."""
    with reader.connect() as conn:
        computed = link_rows(resolve_generation(conn, dataset.generation))
        stored = [
            (
                row.canonical_id,
                row.source_id,
                row.source_key,
                row.source_ref,
                row.method,
                row.generation,
            )
            for row in conn.execute(_PERSISTED_LINKS)
        ]

    assert len(stored) == len(computed)
    mismatches = [(a, b) for a, b in zip(stored, computed, strict=True) if a != b]
    assert not mismatches, f"{len(mismatches)} persisted link(s) differ, e.g. {mismatches[:3]}"


def test_candidates_and_lineage_are_reproducible(reader: Engine, dataset: Dataset) -> None:
    """The other two derived tables are pure functions of the snapshot too."""
    with reader.connect() as conn:
        first = resolve_generation(conn, dataset.generation)
        second = resolve_generation(conn, dataset.generation)

    assert candidate_rows(first) == candidate_rows(second)
    assert lineage_rows(first) == lineage_rows(second)


def test_input_order_does_not_change_the_cascade(reader: Engine, dataset: Dataset) -> None:
    """Reverse every input sequence; the cascade must not notice.

    This is the failure R9 actually meets in practice: a `SELECT` without an
    `ORDER BY` hands the cascade its rows in whatever order the heap felt like,
    and a rule that reads the *first* bucket entry then resolves a different
    student -- moving a `person_key`, and with it a golden `entity_refs`.
    """
    with reader.connect() as conn:
        snapshot = load_snapshot(conn, dataset.generation)

    reversed_snapshot = Snapshot(
        generation=snapshot.generation,
        contacts=list(reversed(list(snapshot.contacts))),
        deals=list(reversed(list(snapshot.deals))),
        students=list(reversed(list(snapshot.students))),
        enrollments=list(reversed(list(snapshot.enrollments))),
        payments=list(reversed(list(snapshot.payments))),
    )

    forward = resolve(snapshot)
    backward = resolve(reversed_snapshot)

    assert forward.links == backward.links
    assert forward.candidates == backward.candidates
    assert [person.person_key for person in forward.persons] == [
        person.person_key for person in backward.persons
    ]
    assert [person.refs for person in forward.persons] == [
        person.refs for person in backward.persons
    ]


def test_persisted_canonical_views_equal_a_fresh_computation(
    reader: Engine, dataset: Dataset
) -> None:
    """Every stored `entities.current` equals what the driver would build today."""
    with reader.connect() as conn:
        resolved = resolve_generation(conn, dataset.generation)
        stored = {
            row.canonical_id: (row.entity_type, row.current)
            for row in conn.execute(_PERSISTED_ENTITIES)
        }

    assert len(stored) == len(resolved.resolution.persons)

    differences: list[str] = []
    for person in resolved.resolution.persons:
        entry = stored.get(person.person_key)
        if entry is None:
            differences.append(f"{person.anchor_ref}: no stored canonical row")
            continue
        entity_type, current = entry
        expected = dict(person_view(resolved, person))
        expected["generation"] = dataset.generation
        expected["tenant"] = tenant_for(person, resolved)
        if entity_type != "person" or current != expected:
            differences.append(f"{person.anchor_ref}: stored canonical row differs")
        if len(differences) > 3:
            break

    assert not differences, differences


def test_entity_rows_are_stable_across_two_builds(reader: Engine, dataset: Dataset) -> None:
    """The serialized canonical rows are byte-identical between two runs.

    `entity_rows` renders `current` with `json.dumps(sort_keys=True)`, so this
    compares the bytes that reach `COPY` -- the level at which a dict-ordering
    regression would actually show up.
    """
    with reader.connect() as conn:
        first = resolve_generation(conn, dataset.generation)
        second = resolve_generation(conn, dataset.generation)

    left = entity_rows(
        first, {p.person_key: person_view(first, p) for p in first.resolution.persons}
    )
    right = entity_rows(
        second, {p.person_key: person_view(second, p) for p in second.resolution.persons}
    )
    assert left == right
