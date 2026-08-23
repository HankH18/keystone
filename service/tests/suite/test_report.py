"""The scorecard's two renderings, and the A4 body the dashboard is built against.

`dashboard/src/lib/contract.ts` A4 pins the JSON shape and records the
consequence of getting it wrong: "a different shape leaves the overview reporting
Mismatch for every type, or its error state". These assertions are that contract,
written down on the service side so a rename here is a red test rather than a
broken dashboard.
"""

from __future__ import annotations

import json
from pathlib import Path

from recon.suite.checks import CheckResult
from recon.suite.report import payload, render, write_scorecard

GREEN = CheckResult.passed("golden-diff", "FN=0 FP=0 matched=3050/3050")
RED = CheckResult.failed("join-check", "3 of 25 views disagree with the golden file")
BENCH = CheckResult.passed("bench:invariant-pass", "20.3s total (threshold <30s)")


def test_payload_carries_every_a4_key() -> None:
    body = payload([GREEN, RED, BENCH], benchmarks=("bench:invariant-pass",))

    for key in ("generated_at", "run_id", "conflicts", "proposals", "checks"):
        assert key in body, f"contract.ts A4 requires {key!r}"
    assert set(body["conflicts"]) == {"total", "by_type"}
    assert set(body["proposals"]) == {"total", "by_status"}
    assert body["checks"] == {
        "golden-diff": True,
        "join-check": False,
        "bench:invariant-pass": True,
    }


def test_payload_reports_failure_rather_than_omitting_it() -> None:
    """A red row is `false` in `checks`, not a missing key.

    An absent key reads as "not run" in a `Record<string, boolean>`, and the
    overview would render nothing at all for the check that failed.
    """
    body = payload([GREEN, RED])

    assert body["checks"]["join-check"] is False
    assert body["passed"] is False


def test_payload_of_a_green_run_says_so() -> None:
    assert payload([GREEN, BENCH])["passed"] is True


def test_an_empty_result_list_is_not_a_pass() -> None:
    """Zero checks is not "everything passed"."""
    assert payload([])["passed"] is False


def test_render_shows_the_failing_row_and_names_it_in_the_tally() -> None:
    text = render([GREEN, RED, BENCH], benchmarks=("bench:invariant-pass",))

    assert "golden-diff" in text and "join-check" in text
    assert "2/3 passed" in text
    assert "FAILED: join-check" in text
    assert "BENCHMARKS" in text


def test_render_wraps_long_details_without_losing_them() -> None:
    long_detail = " ".join(f"token{index}" for index in range(80))
    text = render([CheckResult.passed("determinism", long_detail)])

    assert "token0" in text and "token79" in text
    assert all(len(line) <= 120 for line in text.splitlines())


def test_write_scorecard_writes_both_artifacts_even_when_red(tmp_path: Path) -> None:
    """The one run a reviewer most needs to read must not be the one with no file."""
    text_path, json_path = write_scorecard([GREEN, RED], directory=tmp_path)

    assert text_path.exists() and json_path.exists()
    assert "join-check" in text_path.read_text(encoding="utf-8")
    body = json.loads(json_path.read_text(encoding="utf-8"))
    assert body["checks"]["join-check"] is False
