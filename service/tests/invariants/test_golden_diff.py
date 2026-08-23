"""THE correctness grade: the golden set is caught exactly.

    "the golden set of seeded conflicts is caught exactly -- no false negatives, no
     false positives -- verified by an automated test"

SS5.4 defines the harness key and the two categories; SS8 adds the stricter
clean-sample probe. Both are asserted here against the **committed** full-profile
`golden/` tree -- 3,050 entries over the 43,375-entity generation-3 world, of which
>=85 percent are clean and must never be flagged.

Nothing in this file is a self-comparison: the detector's conflicts come out of
`rules/*.sql` executed against the ingested database, and the expectation comes off
disk. The seed generator's own sweep is never imported here.
"""

from __future__ import annotations

from recon.invariants.grading import (
    grade_clean_sample,
    grade_run,
    load_clean_sample,
    load_golden,
)
from recon.reference import CONFLICT_MINIMUMS


def test_zero_false_negatives_and_zero_false_positives(invariant_run) -> None:
    diff = grade_run(invariant_run.conflicts)
    assert diff.passed, "\n" + diff.report()
    assert diff.false_negatives == []
    assert diff.false_positives == []


def test_field_exactness_on_every_matched_entry(invariant_run) -> None:
    """SS5.4: `disagreeing_fields`, `sources_involved`, `observed_values` keys and
    `expected_verdict` must agree on every matched pair. A mismatch is a detail line,
    **not** a third scorecard category."""
    diff = grade_run(invariant_run.conflicts)
    assert diff.mismatches == [], "\n" + diff.report()


def test_no_clean_sample_entity_is_flagged(invariant_run) -> None:
    """SS8: FLAGGED iff any detected `entity_refs` INTERSECTS the entity's identity refs.

    This is the strict reading -- "nothing hides behind a ref-set-equality
    technicality" -- and every intersection is one false positive.
    """
    result = grade_clean_sample(invariant_run.conflicts)
    assert result.passed, "\n" + result.report()
    assert result.sampled == len(load_clean_sample())


def test_detected_counts_match_the_golden_counts_per_type(invariant_run) -> None:
    golden = load_golden()
    expected: dict[str, int] = {}
    for entry in golden:
        expected[entry["type"]] = expected.get(entry["type"], 0) + 1
    assert invariant_run.by_type() == dict(sorted(expected.items()))


def test_every_a4_minimum_is_met(invariant_run) -> None:
    """SS11.8: all fourteen A.4 conflict minimums, simultaneously."""
    counts = invariant_run.by_type()
    shortfalls = {
        conflict_type: (counts.get(conflict_type, 0), minimum)
        for conflict_type, minimum in CONFLICT_MINIMUMS.items()
        if counts.get(conflict_type, 0) < minimum
    }
    assert shortfalls == {}


def test_the_run_is_not_degraded(invariant_run) -> None:
    """A degraded run skips seven absence rules; the golden diff would then be
    meaningless, so this asserts the gate did not silently fire."""
    assert invariant_run.status == "ok"
    assert invariant_run.incomplete == ()


def test_every_conflict_carries_a_fingerprint_and_they_are_unique(invariant_run) -> None:
    """SS5.4/SS5.7 rule 11: the fingerprint is the idempotency key the whole proposal
    pipeline is keyed on; a collision silently suppresses a real second proposal."""
    fingerprints = [conflict.fingerprint for conflict in invariant_run.conflicts]
    assert all(len(value) == 64 for value in fingerprints)
    assert len(set(fingerprints)) == len(fingerprints)
    keys = [conflict.key for conflict in invariant_run.conflicts]
    assert len(set(keys)) == len(keys)
