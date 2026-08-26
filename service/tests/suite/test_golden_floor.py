"""``golden-diff`` must not report PASS on an empty comparison.

Its two siblings already refuse to: ``check_clean_sample`` fails when
``golden/clean-sample.json`` is empty ("zero flagged out of zero sampled is not
evidence of anything") and ``check_join`` fails when ``golden/expected-views
.json`` is ("0 of 0 views matching is a green that grades nothing").
``check_golden_diff`` had no such floor, and it is the row the whole grading
contract hangs on.

The hole was structural, not hypothetical. :attr:`~recon.invariants.grading
.GoldenDiff.passed` is ``not (false_negatives or false_positives or
mismatches)``, so a golden set of zero entries diffed against a detection of zero
conflicts satisfies it perfectly: no FN, no FP, no field mismatch. A truncated,
mis-pathed or unreadable ``golden/conflicts.json`` -- ``KEYSTONE_GOLDEN_DIR``
pointed at the wrong tree, a fixtures regeneration that wrote an empty file, a
deploy that shipped without the golden tree -- would print

    golden-diff  PASS  FN=0 FP=0 field-mismatches=0 matched=0/0 ...

which is the exact vacuous green :mod:`recon.suite.checks` exists to prevent.

The comparison itself is NOT stubbed here. The real
:func:`recon.invariants.grading.grade_run` runs, over a real
:class:`~recon.invariants.runner.InvariantRun`; what is injected is the *content*
of the golden tree (empty, or one entry) and the pipeline run, because the real
pipeline needs the fully loaded graded database.
"""

from __future__ import annotations

from typing import Any

import pytest

from recon.suite import golden as golden_check
from tests.suite.benchfakes import conflict, fake_pipeline_run


def _golden_entry(index: int, conflict_type: str = "C1") -> dict[str, Any]:
    """The committed-file shape of :func:`tests.suite.benchfakes.conflict`."""
    detected = conflict(index, conflict_type)
    return {
        "type": detected.type,
        "entity_refs": list(detected.entity_refs),
        "sources_involved": list(detected.sources_involved),
        "disagreeing_fields": list(detected.disagreeing_fields),
        "observed_values": dict(detected.observed_values),
        "expected_verdict": detected.expected_verdict,
    }


@pytest.fixture
def golden_tree(monkeypatch: pytest.MonkeyPatch):
    """Point BOTH readers of the golden tree at an injected list of entries.

    ``check_golden_diff`` reads it once itself (for the per-type tally) and once
    through ``grade_run``, which resolves ``load_golden`` in its own module.
    """

    def install(entries: list[dict[str, Any]]) -> None:
        monkeypatch.setattr(golden_check, "load_golden", lambda *_, **__: list(entries))
        monkeypatch.setattr("recon.invariants.grading.load_golden", lambda *_, **__: list(entries))

    return install


def test_an_empty_golden_set_is_not_a_pass(golden_tree, monkeypatch: pytest.MonkeyPatch) -> None:
    """0 detected vs 0 golden satisfies `GoldenDiff.passed`. It must still be FAIL."""
    golden_tree([])
    monkeypatch.setattr(golden_check, "pipeline", lambda: fake_pipeline_run(conflicts=()))

    result = golden_check.check_golden_diff()

    assert not result.ok, result.detail
    assert "0 of 0" in result.detail or "empty" in result.detail
    assert "matched=0/0" in result.detail


def test_a_non_empty_golden_set_that_matches_exactly_still_passes(
    golden_tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor must gate emptiness only -- a real exact run is still green."""
    entries = [_golden_entry(index) for index in range(3)]
    golden_tree(entries)
    monkeypatch.setattr(
        golden_check,
        "pipeline",
        lambda: fake_pipeline_run(conflicts=[conflict(index) for index in range(3)]),
    )

    result = golden_check.check_golden_diff()

    assert result.ok, result.detail
    assert "matched=3/3" in result.detail


def test_a_non_empty_golden_set_with_a_false_negative_fails(
    golden_tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-existing assertion is untouched by the floor."""
    golden_tree([_golden_entry(index) for index in range(3)])
    monkeypatch.setattr(
        golden_check,
        "pipeline",
        lambda: fake_pipeline_run(conflicts=[conflict(index) for index in range(2)]),
    )

    result = golden_check.check_golden_diff()

    assert not result.ok
    assert "FN=1" in result.detail
