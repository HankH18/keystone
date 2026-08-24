"""The clusterer: deterministic by construction, and measurably not a `GROUP BY`.

No database. These are the properties the module's determinism claim rests on,
asserted directly rather than inferred from a stable-looking output.
"""

from __future__ import annotations

import collections
import math

import pytest

from recon.incidents import (
    DEFAULT_THRESHOLD,
    ConflictRecord,
    MockEmbeddingProvider,
    centroid,
    cluster_vectors,
    cosine_distance,
    descriptor,
)
from tests.incidents.conftest import golden_records


def _records() -> list[ConflictRecord]:
    """The whole committed golden set as `ConflictRecord`s, in fingerprint order.

    Ordered by the same key `recon.incidents.load_conflicts` uses -- the
    fingerprint -- rather than by file order, so the offline harness and the
    database path cluster the same sequence.
    """
    from recon.reference import fingerprint

    records = []
    for raw in golden_records():
        digest = fingerprint(
            str(raw["type"]),
            [str(ref) for ref in raw["entity_refs"]],  # type: ignore[union-attr]
            [str(path) for path in raw["disagreeing_fields"]],  # type: ignore[union-attr]
            raw["observed_values"],  # type: ignore[arg-type]
        )
        records.append(
            ConflictRecord(
                id=0,
                fingerprint=digest,
                type=str(raw["type"]),
                rule_id=str(raw["rule_id"]),
                entity_refs=tuple(str(r) for r in raw["entity_refs"]),  # type: ignore[union-attr]
                sources=tuple(str(s) for s in raw["sources_involved"]),  # type: ignore[union-attr]
                disagreeing_fields=tuple(
                    str(p)
                    for p in raw["disagreeing_fields"]  # type: ignore[union-attr]
                ),
                observed_values=dict(raw["observed_values"]),  # type: ignore[arg-type]
                oscillating=bool(raw["oscillating"]),
            )
        )
    return sorted(records, key=lambda record: record.fingerprint)


@pytest.fixture(scope="module")
def golden_vectors() -> tuple[list[ConflictRecord], tuple[tuple[float, ...], ...]]:
    """Every golden conflict, embedded by the mock. ~3,050 vectors, no network."""
    records = _records()
    provider = MockEmbeddingProvider()
    return records, provider.embed([descriptor(record) for record in records]).vectors


def test_the_mock_needs_no_key_and_no_network() -> None:
    """Constructed and called with nothing configured at all.

    The point of the mock is that the graded suite runs offline and keyless. If
    this ever needed an environment variable, every other test in this package
    would be testing a differently-configured system than the one that ships.
    """
    vectors = MockEmbeddingProvider().embed(["type C1\nrule R-001"]).vectors
    assert len(vectors) == 1
    assert len(vectors[0]) == 256


def test_the_mock_is_byte_identical_across_instances_and_processes() -> None:
    """Two independent providers, the same vector.

    This is what rules out Python's own `hash()`: `hash("x")` is randomised per
    process unless `PYTHONHASHSEED` is pinned, so a hashing-trick embedding
    built on it would be stable within one run and different in the next.
    """
    left = MockEmbeddingProvider().embed(["type C6\nobs grade 11"]).vectors
    right = MockEmbeddingProvider().embed(["type C6\nobs grade 11"]).vectors
    assert left == right


def test_the_mock_returns_unit_vectors() -> None:
    """Cosine distance is only `1 - dot` for unit vectors."""
    for vector in MockEmbeddingProvider().embed(["a b c", "type C1 rule R-001"]).vectors:
        assert math.isclose(math.fsum(x * x for x in vector), 1.0, rel_tol=1e-12)


def test_similar_descriptors_are_closer_than_unrelated_ones() -> None:
    """The lexical claim, stated as an assertion rather than as prose.

    Two C6 grade mismatches differing only in the grades are near each other;
    a C2 payment orphan is far from both. This is what makes the mock a usable
    stand-in and it is also the limit of what it can do -- it measures token
    overlap, nothing more.
    """
    provider = MockEmbeddingProvider()
    grade_a, grade_b, payment = provider.embed(
        [
            "type C6\nfields appdb.student.grade\nobs appdb.student.grade 11",
            "type C6\nfields appdb.student.grade\nobs appdb.student.grade 9",
            "type C2\nfields none\nobs external_ref null",
        ]
    ).vectors
    assert cosine_distance(grade_a, grade_b) < cosine_distance(grade_a, payment)


def test_clustering_the_same_input_twice_gives_identical_clusters(
    golden_vectors: tuple[list[ConflictRecord], tuple[tuple[float, ...], ...]],
) -> None:
    """R25's determinism requirement, over the real grading contract."""
    _, vectors = golden_vectors
    first = cluster_vectors(vectors, threshold=DEFAULT_THRESHOLD)
    second = cluster_vectors(vectors, threshold=DEFAULT_THRESHOLD)
    assert first == second


def test_the_assignment_is_a_function_of_content_not_of_scan_luck(
    golden_vectors: tuple[list[ConflictRecord], tuple[tuple[float, ...], ...]],
) -> None:
    """Every member really is within the threshold of its own leader.

    The leader algorithm's guarantee, checked rather than assumed: a member that
    sat further than `threshold` from its leader would mean the assignment came
    from scan order rather than from distance.
    """
    _, vectors = golden_vectors
    for group in cluster_vectors(vectors, threshold=DEFAULT_THRESHOLD):
        for member in group.members:
            assert cosine_distance(vectors[member], vectors[group.leader]) <= DEFAULT_THRESHOLD


def test_every_conflict_lands_in_exactly_one_cluster(
    golden_vectors: tuple[list[ConflictRecord], tuple[tuple[float, ...], ...]],
) -> None:
    """A partition, not a covering: no conflict is dropped and none is duplicated."""
    _, vectors = golden_vectors
    groups = cluster_vectors(vectors, threshold=DEFAULT_THRESHOLD)
    seen = [index for group in groups for index in group.members]
    assert sorted(seen) == list(range(len(vectors)))


def test_the_centroid_does_not_depend_on_member_order() -> None:
    """`math.fsum` is exact, so a centroid is order-independent.

    With plain `sum` this fails: floating-point addition is not associative, and
    the centroid is what lands in the `vector` column.
    """
    provider = MockEmbeddingProvider()
    vectors = list(
        provider.embed([f"type C6\nobs appdb.student.grade {n}" for n in range(12)]).vectors
    )
    assert centroid(vectors) == centroid(list(reversed(vectors)))


def test_the_clusters_refine_conflict_type_and_do_not_merge_types(
    golden_vectors: tuple[list[ConflictRecord], tuple[tuple[float, ...], ...]],
) -> None:
    """The honest measurement, pinned so the claim in the docstring cannot rot.

    Two facts, and the second is the limitation:

    * the partition **refines** `GROUP BY type`: every cluster is single-type;
    * it therefore **never merges** two conflict types into one incident.

    If a future change makes clusters span types, this test goes red -- and that
    would be an improvement worth re-reading the module docstring for, not a
    regression to paper over.
    """
    records, vectors = golden_vectors
    groups = cluster_vectors(vectors, threshold=DEFAULT_THRESHOLD)
    for group in groups:
        types = {records[index].type for index in group.members}
        assert len(types) == 1, f"cluster led by {group.leader} spans {sorted(types)}"


def test_the_clusters_are_strictly_finer_than_any_group_by_over_the_columns(
    golden_vectors: tuple[list[ConflictRecord], tuple[tuple[float, ...], ...]],
) -> None:
    """The other half: this is not a `GROUP BY` in disguise.

    Baseline: `(type, disagreeing_fields, sources)` -- everything a plain SQL
    grouping could use without opening `observed_values`. The clustering must
    produce strictly more groups than that, by splitting on the *values* inside
    the disagreement, or it has added nothing.
    """
    records, vectors = golden_vectors
    groups = cluster_vectors(vectors, threshold=DEFAULT_THRESHOLD)
    baseline = {
        (r.type, tuple(sorted(r.disagreeing_fields)), tuple(sorted(r.sources))) for r in records
    }
    split = collections.Counter(
        (
            records[group.leader].type,
            tuple(sorted(records[group.leader].disagreeing_fields)),
            tuple(sorted(records[group.leader].sources)),
        )
        for group in groups
    )
    assert len(groups) > len(baseline), (
        f"{len(groups)} clusters for {len(baseline)} column groups: this is a GROUP BY "
        "with extra steps"
    )
    assert sum(1 for count in split.values() if count > 1) >= 5, (
        "fewer than five column groups were split by the values inside them; the "
        f"clustering is barely finer than the columns: {split}"
    )


def test_the_threshold_is_rejected_outside_the_cosine_range() -> None:
    with pytest.raises(ValueError, match=r"\[0, 2\]"):
        cluster_vectors([(1.0, 0.0)], threshold=3.0)
