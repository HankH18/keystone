"""``coverage`` -- the >=80% gate on core logic, measured by running the tests.

    ">=80% test coverage on core logic (adapters, normalization, invariants,
     joins, proposal-gating, spend cap)"                       -- SPEC Constraints
    "coverage gate >=80% on recon/{adapters,normalize,er,invariants,confidence,
     reconciler,budget}"                                -- DESIGN SS Verification

Why this runs pytest instead of reading a coverage file
---------------------------------------------------------
A committed ``.coverage`` (or a ``coverage.json`` an earlier CI job left behind)
is a *file claiming a number*. Nothing in it says which revision produced it,
whether the tests passed while producing it, or whether it predates the module it
reports 94% on. A scorecard row backed by such a file is green for as long as
nobody deletes the file -- which is the exact failure class this package exists
to refuse. So the check shells out and measures, and if the measurement cannot be
taken the row is RED with the reason.

The subprocess and its database
---------------------------------
``pytest`` runs as a child process, and much of the suite needs Postgres. It gets
``KEYSTONE_COVERAGE_DATABASE_URL`` when that is set and ``DATABASE_URL``
otherwise, and the environment variable exists because the tests and the harness
want different things from a database: the harness is mid-pipeline with 3,050
proposals on the table, and a test that truncates or re-seeds under it would make
the other nine rows describe a database that no longer exists. **Point
``KEYSTONE_COVERAGE_DATABASE_URL`` at a second database** (a template copy is
enough) whenever the two must not share.

Both numbers are reported and both gate
-----------------------------------------
The row carries the coverage percentage *and* the test outcome. A coverage figure
harvested from a run with failing tests is not evidence that the covered lines
work -- coverage counts lines executed, and a line executed by a failing
assertion is still counted -- so a non-zero pytest exit fails this row even when
the percentage clears 80.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from recon.suite.checks import CheckResult

__all__ = [
    "COVERAGE",
    "COVERAGE_DSN_ENV",
    "COVERED_MODULES",
    "MINIMUM_PERCENT",
    "check_coverage",
    "run_coverage",
]

COVERAGE = "coverage"

#: DESIGN's seven core modules, verbatim.
COVERED_MODULES = (
    "recon.adapters",
    "recon.normalize",
    "recon.er",
    "recon.invariants",
    "recon.confidence",
    "recon.reconciler",
    "recon.budget",
)

MINIMUM_PERCENT = 80.0

#: Point this at a SECOND database so the tests never mutate the one the rest of
#: the scorecard is grading. Falls back to ``DATABASE_URL``.
COVERAGE_DSN_ENV = "KEYSTONE_COVERAGE_DATABASE_URL"

#: Tests that drive this package. Deselected so the coverage child cannot start
#: the suite that started it.
EXCLUDED_TESTS = ("tests/suite",)

_SERVICE_ROOT = Path(__file__).resolve().parents[2]

#: Generous, but finite: a hung test run must become a red row, not a hung suite.
_TIMEOUT_SECONDS = float(os.environ.get("KEYSTONE_COVERAGE_TIMEOUT", "2400"))


class CoverageResult:
    """What the child process reported."""

    __slots__ = ("failures", "per_module", "percent", "returncode", "seconds", "summary", "tail")

    def __init__(
        self,
        percent: float,
        per_module: dict[str, float],
        returncode: int,
        seconds: float,
        summary: str,
        tail: str,
        failures: tuple[str, ...] = (),
    ) -> None:
        self.percent = percent
        self.per_module = per_module
        self.returncode = returncode
        self.seconds = seconds
        self.summary = summary
        self.tail = tail
        #: The node ids from pytest's `short test summary info` block. Carried so
        #: a red row NAMES the tests rather than sending the reader back to a log
        #: the harness already read and threw away.
        self.failures = failures


#: pytest's own tally line, e.g. ``5 failed, 3068 passed, 1 skipped in 843.12s``.
_TALLY = re.compile(r"^=*\s*(?:\x1b\[[0-9;]*m)*[\d].*\b(?:passed|failed|error)\b.*$")

#: One node id out of the `short test summary info` block.
_NODE = re.compile(r"^(FAILED|ERROR)\s+(\S+)")


def _parse_pytest_output(stdout: str) -> tuple[str, tuple[str, ...]]:
    """pytest's tally line and the node ids it named as failed.

    Both are read from the child's captured stdout because the harness already
    has it: a row that says only "pytest exited 1" sends its reader back to a log
    this process read and discarded.
    """
    lines = [line.strip() for line in stdout.splitlines()]
    tally = ""
    for line in reversed(lines):
        stripped = line.strip("= ").strip()
        if not stripped or stripped.startswith(("FAILED", "ERROR")):
            continue
        if re.search(r"\d+\s+(passed|failed|error)", stripped):
            tally = stripped
            break
    failed = tuple(dict.fromkeys(match.group(2) for line in lines if (match := _NODE.match(line))))
    return tally or "(pytest printed no tally line)", failed


def _module_path(module: str) -> str:
    return module.replace(".", "/")


def _percent_from_json(report: dict, module: str) -> float | None:
    """Combined statement+branch coverage for one module, from ``coverage json``."""
    prefix = _module_path(module)
    covered = 0
    total = 0
    for filename, payload in (report.get("files") or {}).items():
        normalized = filename.replace("\\", "/")
        if not (normalized == f"{prefix}.py" or normalized.startswith(f"{prefix}/")):
            continue
        summary = payload.get("summary") or {}
        covered += int(summary.get("covered_lines", 0)) + int(summary.get("covered_branches", 0))
        total += int(summary.get("num_statements", 0)) + int(summary.get("num_branches", 0))
    if total == 0:
        return None
    return 100.0 * covered / total


def run_coverage() -> CoverageResult:
    """Run pytest under coverage in a child process and parse the JSON report."""
    env = dict(os.environ)
    dsn = env.get(COVERAGE_DSN_ENV) or env.get("DATABASE_URL")
    if dsn:
        env["DATABASE_URL"] = dsn
    # A skipped DB test is not coverage of the DB path: make a missing database a
    # failure in the child too, the way CI runs it.
    env.setdefault("KEYSTONE_REQUIRE_DB", "1")

    with tempfile.TemporaryDirectory(prefix="keystone-coverage-") as workspace:
        data_file = Path(workspace) / ".coverage"
        json_report = Path(workspace) / "coverage.json"
        env["COVERAGE_FILE"] = str(data_file)

        # NO extra `-q`: `pyproject.toml`'s `addopts` already carries one, and a
        # second turns it into `-qq`, which suppresses pytest's own tally line --
        # so the row reported "(pytest printed no tally line)" for a run that had
        # simply been told twice to be quiet.
        argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]
        for module in COVERED_MODULES:
            argv += [f"--cov={module}"]
        argv += [f"--cov-report=json:{json_report}", "--cov-report="]
        for path in EXCLUDED_TESTS:
            argv += ["--ignore", path]

        started = time.perf_counter()
        try:
            completed = subprocess.run(  # fixed argv, no shell
                argv,
                cwd=str(_SERVICE_ROOT),
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"the coverage run did not finish within {_TIMEOUT_SECONDS:.0f}s. A "
                "harness that waits forever reports nothing, so this is a failure."
            ) from exc
        seconds = time.perf_counter() - started

        stdout = completed.stdout or ""
        summary, failed_ids = _parse_pytest_output(stdout)
        if not json_report.exists():
            raise RuntimeError(
                "the coverage run produced no JSON report, so there is no number to "
                f"gate on. pytest exited {completed.returncode}: "
                f"{(completed.stderr or stdout)[-600:]}"
            )
        report = json.loads(json_report.read_text(encoding="utf-8"))

    per_module: dict[str, float] = {}
    for module in COVERED_MODULES:
        value = _percent_from_json(report, module)
        per_module[module] = -1.0 if value is None else value

    totals = report.get("totals") or {}
    covered = int(totals.get("covered_lines", 0)) + int(totals.get("covered_branches", 0))
    total = int(totals.get("num_statements", 0)) + int(totals.get("num_branches", 0))
    percent = 100.0 * covered / total if total else 0.0

    return CoverageResult(
        failures=failed_ids,
        percent=percent,
        per_module=per_module,
        returncode=completed.returncode,
        seconds=seconds,
        summary=summary.strip(),
        tail=(completed.stdout or "")[-1500:],
    )


def check_coverage() -> CheckResult:
    """>=80% combined statement+branch coverage on the seven core modules."""
    result = run_coverage()
    failures: list[str] = []

    unmeasured = sorted(name for name, value in result.per_module.items() if value < 0)
    if unmeasured:
        failures.append(
            f"no coverage data at all for {unmeasured}: a module the run never "
            "imported is 0% measured, not 100% covered"
        )
    below = sorted(
        f"{name} {value:.1f}%"
        for name, value in result.per_module.items()
        if 0 <= value < MINIMUM_PERCENT
    )
    if below:
        failures.append(f"below the {MINIMUM_PERCENT:.0f}% floor: {below}")
    if result.percent < MINIMUM_PERCENT:
        failures.append(f"combined {result.percent:.1f}% < {MINIMUM_PERCENT:.0f}%")
    if result.returncode != 0:
        named = ", ".join(result.failures[:6]) or "(pytest named no node ids)"
        more = f" (+{len(result.failures) - 6} more)" if len(result.failures) > 6 else ""
        failures.append(
            f"pytest exited {result.returncode} [{result.summary}]; coverage counts "
            "lines executed, including lines executed by a failing assertion, so a "
            f"red suite's percentage is not evidence. Failed: {named}{more}"
        )

    per_module = " ".join(
        f"{name.split('.')[-1]}:{'n/a' if value < 0 else f'{value:.1f}%'}"
        for name, value in sorted(result.per_module.items())
    )
    detail = (
        f"combined {result.percent:.1f}% (floor {MINIMUM_PERCENT:.0f}%) over "
        f"{len(COVERED_MODULES)} core modules [{per_module}]; {result.summary}; "
        f"measured by a real pytest run in {result.seconds:.0f}s"
    )
    if failures:
        return CheckResult.failed(COVERAGE, f"{detail} | " + " | ".join(failures))
    return CheckResult.passed(COVERAGE, detail)
