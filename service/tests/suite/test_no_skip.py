"""A check that cannot run is a FAIL. There is no third outcome.

Driven as a subprocess with ``DATABASE_URL`` removed, because that is the
cheapest way to make every graded check genuinely unable to run: the pipeline
cannot open a connection, so each row has to say so rather than disappear. The
assertion is on the exit code AND on the rows, because either one alone can be
satisfied by the wrong thing -- exit 1 with no rows printed, or green rows with a
non-zero exit from something else.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CHECKS_UNDER_TEST = ("golden-diff", "clean-sample", "join-check", "proposal-safety")


def _run_without_database(service_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env.pop("KEYSTONE_REQUIRE_DB", None)
    # Never let a failing run overwrite the committed artifacts.
    env["KEYSTONE_SCORECARD_DIR"] = str(service_root / ".pytest_cache" / "t14-noskip")
    return subprocess.run(
        [sys.executable, "-m", "recon.suite", "--no-write", *args],
        cwd=service_root,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=env,
    )


def test_checks_that_cannot_run_are_failed_not_skipped(service_root_path: Path) -> None:
    argv: list[str] = []
    for name in CHECKS_UNDER_TEST:
        argv += ["--only", name]

    completed = _run_without_database(service_root_path, *argv)

    assert completed.returncode == 1, (
        "a run in which no check could reach the database exited "
        f"{completed.returncode}; the harness must exit non-zero on any failure.\n"
        f"{completed.stdout[-2000:]}"
    )
    stdout = completed.stdout
    assert "SKIP" not in stdout, "the scorecard has no SKIP status; a skip is a FAIL"
    for name in CHECKS_UNDER_TEST:
        row = next((line for line in stdout.splitlines() if line.startswith(name)), None)
        assert row is not None, f"{name} printed no row at all:\n{stdout[-2000:]}"
        assert "FAIL" in row, row
    assert "DATABASE_URL" in stdout, (
        "the failure rows must name the real cause; a row that only says 'check "
        "raised' sends a reader looking in the wrong place"
    )


def test_the_partial_run_is_labelled(service_root_path: Path) -> None:
    """`--only` must announce that the tally covers a subset.

    "4/4 passed" from a four-check subset reads exactly like "4/4 passed" from a
    complete run unless the output says otherwise.
    """
    completed = _run_without_database(service_root_path, "--only", "golden-diff")

    assert "PARTIAL RUN" in completed.stdout, completed.stdout[-1500:]
