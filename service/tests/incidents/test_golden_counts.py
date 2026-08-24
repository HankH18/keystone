"""Every number the docs claim about the clustering, pinned to the committed file.

**No database, on purpose, and that purpose is a real failure.** The first cut of
`test_reachability.py` asserted "3,050 conflicts -> 38 incidents" against the
`conflicts` table. It passed alone and failed in a full-suite run, because
`tests/er/scratchdb.use_database` repoints `DATABASE_URL` **process-wide** and
`tests/apply` calls it (through `tests.er.dataset.ensure_dataset`) before
`tests/incidents` is collected. The scratch database it builds materializes one
generation, so `oscillation_observed` is 0 for every conflict and 25 conflicts
carry `oscillating = false` where the graded set has `true`. `descriptor()`
includes that flag, so the clustering there is a correct 33 rather than a wrong
38. Nothing was non-deterministic; the assertion was about *which database the
process was pointed at*, which is not this feature's property to pin.

So the counts live here, computed from `golden/conflicts.json` -- the committed
grading contract -- in memory. No DSN, no fixture, no ambient state. If one of
these moves, the module docstring of `recon.incidents`, the README's
"What `GET /api/incidents` does and does not do" and `AI_USAGE.md` §6 are wrong
and must move with it.

Ordering is by :func:`recon.reference.fingerprint`, not file order, because
`recon.incidents.load_conflicts` reads `ORDER BY fingerprint` and the leader
algorithm's output depends on the input order it is given. Reproducing that
order here is what makes these numbers the same numbers a real run produces.
"""

from __future__ import annotations

import collections
import json
from typing import Any

import pytest

from recon.incidents import (
    DEFAULT_THRESHOLD,
    ConflictRecord,
    MockEmbeddingProvider,
    cluster_vectors,
    descriptor,
)
from recon.reference import fingerprint
from tests.incidents.conftest import GOLDEN_CONFLICTS

#: Cluster count at each threshold. `recon.incidents.DEFAULT_THRESHOLD`'s own
#: docstring cites this sweep as the reason 0.10 was chosen rather than picked,
#: and nothing tested it until now.
SWEEP: dict[float, int] = {
    0.05: 191,
    0.08: 54,
    0.10: 38,
    0.12: 27,
    0.15: 21,
    0.20: 18,
    0.30: 16,
}

#: The lowest threshold at which an incident stops being single-type. The whole
#: argument for 0.10 is that it is comfortably below this.
FIRST_MIXED_THRESHOLD = 0.20

#: Incident sizes at :data:`DEFAULT_THRESHOLD`, descending. Pinned as a vector
#: rather than as a count because "38 incidents" is also true of 38 singletons
#: and 3,012 lost rows.
GOLDEN_SIZES: tuple[int, ...] = (
    500, 400, 300, 239, 204, 200, 110, 100, 96, 76, 75, 75, 75, 68, 67,
    50, 50, 50, 48, 41, 37, 36, 33, 29, 17, 16, 10, 10, 10, 9, 6, 4, 4,
    1, 1, 1, 1, 1,
)  # fmt: skip

#: The widest grouping over the row's own columns that the clustering actually
#: **refines**: type, rule_id, sources, disagreeing_fields and the KEY SET of
#: observed_values. The gap between this and 38 is what the embedding adds over
#: any `GROUP BY` you could write.
#:
#: Adding `oscillating` to that key produces :data:`COLUMNWISE_GROUPS_WITH_FLAG`
#: groups and the clustering is NOT a refinement of it -- see
#: `test_the_oscillating_flag_does_not_always_separate`. That is a limitation,
#: and it is pinned rather than dropped from the key to make the number look
#: better.
COLUMNWISE_GROUPS = 19

#: The same key plus `oscillating`. Not refined by the clustering.
COLUMNWISE_GROUPS_WITH_FLAG = 21

#: `GROUP BY` the raw `observed_values` jsonb -- i.e. "just group on the values"
#: -- is not an available alternative at this cardinality.
RAW_VALUE_GROUPS = 2306


def _columnwise_key(record: ConflictRecord) -> tuple[Any, ...]:
    """Everything about a conflict a `GROUP BY` could name, except the values.

    `oscillating` is deliberately NOT here -- see
    :data:`COLUMNWISE_GROUPS` and `test_the_oscillating_flag_does_not_always_separate`.
    """
    return (
        record.type,
        record.rule_id,
        tuple(sorted(record.sources)),
        tuple(sorted(record.disagreeing_fields)),
        tuple(sorted(record.observed_values)),
    )


def _records() -> tuple[ConflictRecord, ...]:
    """The committed golden conflicts, in the fingerprint order a run reads."""
    raw: list[dict[str, Any]] = json.loads(GOLDEN_CONFLICTS.read_text(encoding="utf-8"))
    built = [
        ConflictRecord(
            id=index,
            fingerprint=fingerprint(
                str(entry["type"]),
                [str(ref) for ref in entry["entity_refs"]],
                [str(path) for path in entry["disagreeing_fields"]],
                entry["observed_values"],
            ),
            type=str(entry["type"]),
            rule_id=entry["rule_id"],
            entity_refs=tuple(str(ref) for ref in entry["entity_refs"]),
            sources=tuple(str(s) for s in entry["sources_involved"]),
            disagreeing_fields=tuple(str(p) for p in entry["disagreeing_fields"]),
            observed_values=dict(entry["observed_values"]),
            oscillating=bool(entry["oscillating"]),
        )
        for index, entry in enumerate(raw)
    ]
    return tuple(sorted(built, key=lambda record: record.fingerprint))


@pytest.fixture(scope="module")
def golden() -> tuple[ConflictRecord, ...]:
    return _records()


@pytest.fixture(scope="module")
def vectors(golden: tuple[ConflictRecord, ...]) -> tuple[tuple[float, ...], ...]:
    provider = MockEmbeddingProvider()
    return tuple(provider._vector(descriptor(record)) for record in golden)


def test_the_golden_set_is_the_population_the_docs_describe(
    golden: tuple[ConflictRecord, ...],
) -> None:
    assert len(golden) == 3050
    assert len({record.type for record in golden}) == 14
    assert sum(1 for record in golden if record.oscillating) == 25, (
        "the oscillating flag is IN the descriptor, so this count is load-bearing "
        "for every number in this file"
    )


def test_the_golden_set_clusters_into_thirty_eight_single_type_incidents(
    golden: tuple[ConflictRecord, ...], vectors: tuple[tuple[float, ...], ...]
) -> None:
    """The headline number, and the one the README and AI_USAGE §6 both quote."""
    clusters = cluster_vectors(vectors, threshold=DEFAULT_THRESHOLD)
    assert len(clusters) == 38

    sizes = tuple(sorted((group.size for group in clusters), reverse=True))
    assert sizes == GOLDEN_SIZES
    assert sum(sizes) == len(golden), "the incidents must partition the conflicts"
    assert sum(1 for size in sizes if size == 1) == 5

    for group in clusters:
        types = {golden[index].type for index in group.members}
        assert len(types) == 1, f"an incident spans several conflict types: {sorted(types)}"


def test_eight_of_the_fourteen_conflict_types_are_split(
    golden: tuple[ConflictRecord, ...], vectors: tuple[tuple[float, ...], ...]
) -> None:
    """Single-type alone would also be true of `GROUP BY type` itself."""
    clusters = cluster_vectors(vectors, threshold=DEFAULT_THRESHOLD)
    per_type: dict[str, set[int]] = collections.defaultdict(set)
    for position, group in enumerate(clusters):
        for index in group.members:
            per_type[golden[index].type].add(position)
    split = {name: len(ids) for name, ids in per_type.items() if len(ids) > 1}
    assert sorted(split) == ["C11", "C12", "C14", "C3", "C4", "C6", "C8", "C9"]
    assert split["C6"] == 13


def test_the_clustering_refines_the_widest_columnwise_grouping(
    golden: tuple[ConflictRecord, ...], vectors: tuple[tuple[float, ...], ...]
) -> None:
    """**The honest measure of what the embedding adds.**

    Not "finer than `GROUP BY type`" -- 14 vs 38 is a weak claim when a couple of
    extra columns would close most of it. This groups on *every* column a
    `conflicts` row carries that a `GROUP BY` could name, including the KEY SET
    of `observed_values`, and the clustering still strictly refines it. The
    residue is what the values buy.
    """
    clusters = cluster_vectors(vectors, threshold=DEFAULT_THRESHOLD)
    incident_of = {
        index: position for position, group in enumerate(clusters) for index in group.members
    }

    groups: dict[tuple[Any, ...], set[int]] = collections.defaultdict(set)
    per_incident: dict[int, set[tuple[Any, ...]]] = collections.defaultdict(set)
    for index, record in enumerate(golden):
        groups[_columnwise_key(record)].add(incident_of[index])
        per_incident[incident_of[index]].add(_columnwise_key(record))

    assert len(groups) == COLUMNWISE_GROUPS
    # Refinement: every incident sits inside exactly one column-wise group.
    straddling = {i: k for i, k in per_incident.items() if len(k) > 1}
    assert not straddling, f"incidents straddling two column-wise groups: {sorted(straddling)}"
    assert sum(1 for ids in groups.values() if len(ids) > 1) == 10, (
        "how many of the 19 column-wise groups the values split"
    )
    assert len(clusters) - len(groups) == 19, (
        "the number of splits attributable to the observed VALUES; README, "
        "AI_USAGE §6 and recon.incidents' docstring all quote 19 -> 38"
    )


def test_the_oscillating_flag_does_not_always_separate(
    golden: tuple[ConflictRecord, ...], vectors: tuple[tuple[float, ...], ...]
) -> None:
    """**A limitation, pinned rather than tuned away.**

    `descriptor()` emits an `oscillating true|false` line, so adding the flag to
    the column-wise key ought to be something the clustering also refines. It is
    not: at :data:`DEFAULT_THRESHOLD` exactly two incidents mix the two values --
    both C6 status/lifecycle families whose members agree on every other column
    and on every observed value. One token out of a few dozen does not move two
    unit vectors 0.10 apart.

    The first draft of this file asserted refinement of the 21-group key and went
    red here, which is why the README, `AI_USAGE.md` §6 and
    `recon.incidents`'s docstring all quote **19 -> 38** rather than 21 -> 38.
    Widening the claim by dropping `oscillating` from the key would have been the
    easy fix and the dishonest one; the key without it is the widest grouping
    the clustering genuinely refines, and this test says what the other one is.
    """
    clusters = cluster_vectors(vectors, threshold=DEFAULT_THRESHOLD)
    incident_of = {
        index: position for position, group in enumerate(clusters) for index in group.members
    }
    keyed: dict[tuple[Any, ...], set[int]] = collections.defaultdict(set)
    per_incident: dict[int, set[bool]] = collections.defaultdict(set)
    for index, record in enumerate(golden):
        keyed[(*_columnwise_key(record), record.oscillating)].add(incident_of[index])
        per_incident[incident_of[index]].add(record.oscillating)

    assert len(keyed) == COLUMNWISE_GROUPS_WITH_FLAG
    mixed = sorted(i for i, flags in per_incident.items() if len(flags) > 1)
    assert len(mixed) == 2, f"expected exactly two flag-mixing incidents, got {mixed}"
    for position in mixed:
        types = {golden[i].type for i in clusters[position].members}
        assert types == {"C6"}, f"incident {position} mixes the flag AND spans {sorted(types)}"


def test_grouping_on_the_raw_values_instead_is_not_an_alternative(
    golden: tuple[ConflictRecord, ...],
) -> None:
    """2,306 groups over 3,050 conflicts: amounts, emails and names are near-unique.

    This is why the descriptor reduces values to *shapes*. Quoted in the README
    as the reason "just `GROUP BY` the values" is not the simpler thing that
    would have done the job.
    """
    raw = {
        json.dumps(record.observed_values, sort_keys=True, ensure_ascii=True, default=str)
        for record in golden
    }
    assert len(raw) == RAW_VALUE_GROUPS


def test_the_threshold_sweep_is_the_one_default_threshold_cites(
    golden: tuple[ConflictRecord, ...], vectors: tuple[tuple[float, ...], ...]
) -> None:
    """`DEFAULT_THRESHOLD`'s docstring pins 191/54/38/27/21/18/16. Nothing tested it.

    Both halves of its argument are asserted: the counts, and the claim that the
    first threshold at which an incident stops being single-type is 0.20 -- which
    is what makes 0.10 a measured choice rather than a taste.
    """
    mixed_at: list[float] = []
    for threshold, expected in sorted(SWEEP.items()):
        clusters = cluster_vectors(vectors, threshold=threshold)
        assert len(clusters) == expected, f"threshold {threshold}"
        if any(len({golden[i].type for i in group.members}) > 1 for group in clusters):
            mixed_at.append(threshold)
    assert mixed_at and min(mixed_at) == FIRST_MIXED_THRESHOLD
    assert DEFAULT_THRESHOLD < FIRST_MIXED_THRESHOLD


def test_dropping_the_type_line_changes_the_count_but_not_the_conclusion(
    golden: tuple[ConflictRecord, ...],
) -> None:
    """The ablation behind "it never merges types", measured rather than asserted.

    An earlier revision of `recon.incidents`'s docstring claimed removing `type`
    from the descriptor "does not change the outcome". It does -- 49 incidents,
    not 38. What survives is the conclusion: still **zero** multi-type incidents,
    because `recon.reference.OBSERVED_VALUE_KEYS` pins a distinct key set per
    type, so the key names carry the type whether or not it is spelled out.
    """
    provider = MockEmbeddingProvider()
    stripped = tuple(
        provider._vector(
            "\n".join(
                line for line in descriptor(record).split("\n") if not line.startswith("type ")
            )
        )
        for record in golden
    )
    clusters = cluster_vectors(stripped, threshold=DEFAULT_THRESHOLD)
    assert len(clusters) == 49
    assert not [group for group in clusters if len({golden[i].type for i in group.members}) > 1], (
        "removing the type token DID let two conflict types merge"
    )
