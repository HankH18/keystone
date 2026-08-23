"""The coverage row's decision, without paying for a fourteen-minute test run.

:func:`recon.suite.coverage.run_coverage` really shells out to pytest -- that is
the point of the row and it is exercised end to end by `python -m recon.suite`.
What is asserted here is the part that decides, fed a measurement directly: the
80% floor, the per-module floor, the "no data is not full coverage" rule, and the
rule that a red test run invalidates the percentage no matter how high it is.
"""

from __future__ import annotations

import pytest

from recon.suite import coverage as cov


def _result(**overrides) -> cov.CoverageResult:
    defaults = {
        "percent": 91.2,
        "per_module": dict.fromkeys(cov.COVERED_MODULES, 91.2),
        "returncode": 0,
        "seconds": 843.0,
        "summary": "3073 passed in 843.12s",
        "tail": "",
        "failures": (),
    }
    defaults.update(overrides)
    return cov.CoverageResult(**defaults)


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch):
    def install(result: cov.CoverageResult) -> None:
        monkeypatch.setattr(cov, "run_coverage", lambda: result)

    return install


def test_a_green_run_over_the_floor_passes(stub) -> None:
    stub(_result())

    assert cov.check_coverage().ok


def test_the_combined_floor_gates(stub) -> None:
    stub(_result(percent=79.9, per_module=dict.fromkeys(cov.COVERED_MODULES, 79.9)))

    result = cov.check_coverage()

    assert not result.ok
    assert "combined 79.9% < 80%" in result.detail


def test_one_module_below_the_floor_fails_the_row(stub) -> None:
    """The combined number can clear 80 while a module the SPEC names is at 40."""
    per_module = dict.fromkeys(cov.COVERED_MODULES, 95.0)
    per_module["recon.budget"] = 40.0
    stub(_result(percent=91.0, per_module=per_module))

    result = cov.check_coverage()

    assert not result.ok
    assert "recon.budget 40.0%" in result.detail


def test_a_module_with_no_data_is_not_full_coverage(stub) -> None:
    per_module = dict.fromkeys(cov.COVERED_MODULES, 95.0)
    per_module["recon.er"] = -1.0
    stub(_result(percent=95.0, per_module=per_module))

    result = cov.check_coverage()

    assert not result.ok
    assert "no coverage data at all" in result.detail
    assert "recon.er" in result.detail


def test_a_red_test_run_fails_the_row_even_at_high_coverage(stub) -> None:
    """Coverage counts lines executed -- including by a failing assertion."""
    stub(
        _result(
            returncode=1,
            summary="5 failed, 3068 passed in 843.12s",
            failures=("tests/privacy/test_sinks.py::test_a", "tests/apply/test_b.py::test_c"),
        )
    )

    result = cov.check_coverage()

    assert not result.ok
    assert "91.2%" in result.detail, "the measured number is still reported"
    assert "pytest exited 1" in result.detail
    assert "tests/privacy/test_sinks.py::test_a" in result.detail, (
        "a red row must NAME the tests; 'pytest exited 1' sends the reader nowhere"
    )


def test_the_tally_and_the_failed_ids_are_read_out_of_pytest_output() -> None:
    stdout = (
        "..F...F                                             [100%]\n"
        "=========================== short test summary info ===========================\n"
        "FAILED tests/privacy/test_sinks.py::test_every_terminal_writer - AssertionError\n"
        "ERROR tests/apply/test_x.py::test_y - sqlalchemy.exc.ProgrammingError\n"
        "======================= 1 failed, 5 passed, 1 error in 12.34s ==================\n"
    )

    tally, failed = cov._parse_pytest_output(stdout)

    assert "1 failed" in tally and "5 passed" in tally
    assert failed == (
        "tests/privacy/test_sinks.py::test_every_terminal_writer",
        "tests/apply/test_x.py::test_y",
    )


def test_output_with_no_tally_says_so_rather_than_inventing_one() -> None:
    tally, failed = cov._parse_pytest_output("INTERNALERROR> boom\n")

    assert "no tally" in tally
    assert failed == ()
