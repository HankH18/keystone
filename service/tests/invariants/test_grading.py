"""The golden diff's own behaviour: it must be able to FAIL, and say why.

A harness that reports 0/0 because it cannot see a divergence is worse than one that
reports 47/12. Every path is exercised against a synthetic golden set: a missed entry
becomes a false negative, a spurious detection becomes a false positive, and a
disagreement on a MATCHED entry becomes a field-exactness detail line -- SS5.4's
explicit "not a third category".
"""

from __future__ import annotations

import pytest

from recon.invariants.grading import (
    golden_dir,
    grade_clean_sample,
    grade_run,
    load_clean_sample,
    load_golden,
)
from recon.invariants.runner import DetectedConflict
from recon.reference import conflict_refs, fingerprint

REFS_A = conflict_refs("C1", identity_refs=("appdb:student:a", "crm:contact:CRM-1"))
REFS_B = conflict_refs("C1", identity_refs=("appdb:student:b", "crm:contact:CRM-2"))

OBSERVED = {
    "paid_payment_refs": ["payments:payment:pi_1"],
    "enrollment_ref": "appdb:enrollment:e1",
    "d2_deal_count": 0,
}


def _detected(refs, observed=None) -> DetectedConflict:
    values = dict(OBSERVED if observed is None else observed)
    return DetectedConflict(
        type="C1",
        rule_id="R-001",
        entity_refs=refs,
        sources_involved=("appdb", "crm"),
        disagreeing_fields=(),
        observed_values=values,
        fingerprint=fingerprint("C1", refs, (), values),
    )


def _golden(refs, observed=None) -> dict:
    return {
        "type": "C1",
        "rule_id": "R-001",
        "entity_refs": list(refs),
        "sources_involved": ["appdb", "crm"],
        "disagreeing_fields": [],
        "observed_values": dict(OBSERVED if observed is None else observed),
        "expected_verdict": "conflict",
    }


def test_a_perfect_match_passes() -> None:
    diff = grade_run([_detected(REFS_A)], [_golden(REFS_A)])
    assert diff.passed
    assert diff.matched == 1
    assert "FALSE NEGATIVES: 0" in diff.report()


def test_a_missed_golden_entry_is_a_false_negative() -> None:
    diff = grade_run([], [_golden(REFS_A)])
    assert not diff.passed
    assert diff.false_negatives == [("C1", tuple(REFS_A))]
    assert diff.false_positives == []
    assert diff.counts_by_type(diff.false_negatives) == {"C1": 1}
    assert "FN C1" in diff.report()


def test_a_spurious_detection_is_a_false_positive() -> None:
    diff = grade_run([_detected(REFS_B)], [_golden(REFS_A)])
    assert diff.false_negatives == [("C1", tuple(REFS_A))]
    assert diff.false_positives == [("C1", tuple(REFS_B))]
    assert "FP C1" in diff.report()


def test_a_disagreement_on_a_matched_entry_is_a_detail_line_not_a_category() -> None:
    """SS5.4: field exactness "is **not** a third scorecard category -- the harness
    reports exactly the brief's two categories"."""
    detected = _detected(REFS_A, {**OBSERVED, "d2_deal_count": 3})
    diff = grade_run([detected], [_golden(REFS_A)])
    assert diff.false_negatives == []
    assert diff.false_positives == []
    assert not diff.passed
    assert [mismatch.field_name for mismatch in diff.mismatches] == ["observed_values"]
    assert "field-exactness" in diff.report()


def test_the_report_truncates_a_long_list() -> None:
    golden = [
        _golden(conflict_refs("C1", identity_refs=(f"appdb:student:{n}",))) for n in range(40)
    ]
    diff = grade_run([], golden)
    assert len(diff.false_negatives) == 40
    assert "... 20 more FN" in diff.report()


def test_a_duplicate_golden_key_is_refused() -> None:
    """SS5.7 rule 11: the loader fails on a duplicate rather than deduping silently."""
    with pytest.raises(ValueError, match="duplicate golden key"):
        grade_run([], [_golden(REFS_A), _golden(REFS_A)])


def test_a_duplicate_detection_key_is_refused() -> None:
    with pytest.raises(ValueError, match="duplicate detected key"):
        grade_run([_detected(REFS_A), _detected(REFS_A)], [_golden(REFS_A)])


def test_clean_sample_flags_any_intersection() -> None:
    """SS8's predicate is INTERSECTION, not equality: a conflict naming one of an
    entity's identity refs flags it even though the ref sets differ."""
    sample = [{"identity_refs": ["appdb:student:a"]}]
    result = grade_clean_sample([_detected(REFS_A)], sample)
    assert not result.passed
    assert result.flagged == [(("appdb:student:a",), ("C1", tuple(REFS_A)))]
    assert "FLAGGED" in result.report()


def test_clean_sample_passes_when_nothing_intersects() -> None:
    sample = [{"identity_refs": ["appdb:student:z"]}]
    result = grade_clean_sample([_detected(REFS_A)], sample)
    assert result.passed
    assert result.report().startswith("clean sample: 1 entities")


def test_clean_sample_accepts_a_bare_ref_sequence() -> None:
    result = grade_clean_sample([_detected(REFS_A)], [("appdb:student:a",)])
    assert not result.passed


def test_clean_sample_refuses_an_entity_with_no_refs() -> None:
    with pytest.raises(ValueError, match="no identity refs"):
        grade_clean_sample([], [{"anchor_ref": "appdb:student:a"}])


def test_the_committed_golden_tree_is_the_full_profile() -> None:
    """SS9: "All gates, benchmarks, and the committed `golden/` files are `full`."
    A dev-profile golden set would make a 0/0 diff meaningless."""
    assert golden_dir().name == "golden"
    assert len(load_golden()) == 3050
    assert len(load_clean_sample()) == 1000
