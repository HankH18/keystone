"""The comparators the scorecard rows are built out of, exercised on their edges.

Small, pure, and worth pinning: every one of them is a place where a check could
be made to pass by comparing less than it claims to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recon.bench.suite import percentile
from recon.suite.determinism import tree_digest
from recon.suite.golden import view_mismatches


def test_percentile_is_nearest_rank_not_interpolated() -> None:
    """p95 of 20 samples is the 19th, and it is a number that was measured."""
    samples = [float(index) for index in range(1, 21)]

    assert percentile(samples, 0.95) == 19.0
    assert percentile(samples, 0.50) == 10.0
    assert percentile(samples, 1.0) == 20.0
    assert percentile(samples, 0.0) == 1.0


def test_percentile_refuses_an_empty_sample() -> None:
    with pytest.raises(ValueError, match="no samples"):
        percentile([], 0.95)


def test_view_mismatches_catches_a_dropped_key() -> None:
    """A subset test would pass a view that lost `payments` entirely."""
    expected = {"person_key": "p1", "paid": True, "payments": [{"ref": "x"}]}
    served = {"person_key": "p1", "paid": True}

    problems = view_mismatches(expected, served)

    assert problems and "missing keys ['payments']" in problems[0]


def test_view_mismatches_catches_an_extra_key() -> None:
    problems = view_mismatches({"a": 1}, {"a": 1, "b": 2})

    assert problems and "unexpected keys ['b']" in problems[0]


def test_view_mismatches_reports_the_two_values() -> None:
    problems = view_mismatches({"stage_funnel": "won"}, {"stage_funnel": "lost"})

    assert len(problems) == 1
    assert "golden='won'" in problems[0] and "served='lost'" in problems[0]


def test_view_mismatches_is_empty_on_an_exact_match() -> None:
    view = {"person_key": "p1", "sources": ["appdb", "crm"], "paid": False}

    assert view_mismatches(view, dict(view)) == []


def test_tree_digest_hashes_the_path_as_well_as_the_bytes(tmp_path: Path) -> None:
    """Two trees with the same bytes under different names are not the same dataset."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    (left / "a").mkdir(parents=True)
    (right / "a").mkdir(parents=True)
    (left / "a" / "one.jsonl").write_text("x", encoding="utf-8")
    (right / "a" / "two.jsonl").write_text("x", encoding="utf-8")

    assert tree_digest(left)[0] != tree_digest(right)[0]


def test_tree_digest_is_stable_for_identical_trees(tmp_path: Path) -> None:
    left = tmp_path / "left"
    right = tmp_path / "right"
    for root in (left, right):
        (root / "golden").mkdir(parents=True)
        (root / "golden" / "conflicts.json").write_text('[{"type":"C1"}]', encoding="utf-8")

    digest_left, files_left = tree_digest(left)
    digest_right, files_right = tree_digest(right)

    assert digest_left == digest_right
    assert set(files_left) == set(files_right) == {"golden/conflicts.json"}


def test_tree_digest_notices_a_one_byte_change(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    target = root / "fixtures.jsonl"
    target.write_text("aaa", encoding="utf-8")
    before = tree_digest(root)[0]
    target.write_text("aab", encoding="utf-8")

    assert tree_digest(root)[0] != before
