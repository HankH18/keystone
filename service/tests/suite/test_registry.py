"""Every check SPEC success criterion 1 names is REGISTERED, and `--only` is strict.

The defect this file exists against is the one the ticket was raised for: the
harness registered two checks, printed "2/2 passed", and did not include the
golden diff. A green tally over an incomplete registry is worse than a red one,
because nothing in the output says what is missing. So the registry is asserted
by name here, against SPEC's own list, and a check that is quietly unregistered
to make the suite green turns this file red instead.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from recon.bench.suite import BENCHMARKS
from recon.suite.__main__ import BENCHMARK_NAMES, CHECKS, select_checks

#: SPEC success criterion 1 + DESIGN SS Verification strategy, name by name.
REQUIRED_CHECKS = (
    "golden-diff",
    "clean-sample",
    "join-check",
    "proposal-safety",
    "oscillation-dedup",
    "spend-cap-burst",
    "mirror-unchanged",
    "determinism",
    "manifest",
    "coverage",
)

#: "all six benchmarks green" -- SPEC SS Constraints lists exactly these.
REQUIRED_BENCHMARKS = (
    "bench:cross-source-query-p95",
    "bench:invariant-pass",
    "bench:ingestion-rps",
    "bench:conflict-accuracy",
    "bench:spend-cap-exact",
    "bench:dashboard-load-p95",
)


@pytest.mark.parametrize("name", REQUIRED_CHECKS)
def test_every_spec_named_check_is_registered(name: str) -> None:
    assert name in CHECKS, (
        f"{name!r} is named by SPEC success criterion 1 but is not in the "
        f"registry, so `python -m recon.suite` would print a tally that does not "
        f"include it. Registered: {sorted(CHECKS)}"
    )


@pytest.mark.parametrize("name", REQUIRED_BENCHMARKS)
def test_every_spec_benchmark_is_registered(name: str) -> None:
    assert name in CHECKS and name in BENCHMARK_NAMES, (
        f"{name!r} is one of SPEC's six benchmarks and must be a scorecard row. "
        f"Registered benchmarks: {sorted(BENCHMARKS)}"
    )


def test_there_are_exactly_sixteen_rows() -> None:
    """Ten checks and six benchmarks -- the count is pinned, not merely 'at least'."""
    assert len(CHECKS) == len(REQUIRED_CHECKS) + len(REQUIRED_BENCHMARKS), sorted(CHECKS)


def test_an_unknown_only_name_is_refused_rather_than_running_nothing() -> None:
    """A typo must not select the empty set and report success.

    `--only mirror-unchagned` used to be accepted, run zero checks, print
    "no checks yet" and exit 0 -- a green produced by a spelling mistake.
    """
    with pytest.raises(SystemExit) as raised:
        select_checks(["mirror-unchagned"])
    assert "mirror-unchagned" in str(raised.value)


def test_only_selects_in_registration_order() -> None:
    selected = select_checks(["manifest", "golden-diff"])
    assert list(selected) == ["golden-diff", "manifest"]


def test_list_flag_prints_every_registered_name(service_root_path: Path) -> None:
    """`--list` is the operator's view of the registry and must not be a subset."""
    completed = subprocess.run(
        [sys.executable, "-m", "recon.suite", "--list"],
        cwd=service_root_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    printed = {line.strip() for line in completed.stdout.splitlines() if line.strip()}
    assert set(CHECKS) <= printed, sorted(set(CHECKS) - printed)
