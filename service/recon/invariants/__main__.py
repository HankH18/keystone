"""`python -m recon.invariants` -- run the rules and print the golden diff.

    uv run python -m recon.invariants            # run + grade against golden/
    uv run python -m recon.invariants --persist  # also write invariant_results/conflicts

Exit status is 0 only when there are zero false negatives, zero false positives, zero
SS5.4 field-exactness mismatches and zero flagged `golden/clean-sample.json` entities.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import psycopg

from recon.invariants.context import CURRENT_GENERATION
from recon.invariants.grading import grade_clean_sample, grade_run
from recon.invariants.runner import persist_run, run_invariants


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recon.invariants")
    parser.add_argument("--run-id", default="invariants-cli")
    parser.add_argument("--generation", type=int, default=CURRENT_GENERATION)
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--dsn", default=None, help="defaults to $DATABASE_URL")
    args = parser.parse_args(argv)

    dsn = args.dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        parser.error("DATABASE_URL is not set and --dsn was not given")
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://")

    started = time.perf_counter()
    with psycopg.connect(dsn) as conn:
        run = run_invariants(conn, run_id=args.run_id, generation=args.generation)
        if args.persist:
            persist_run(conn, run)
            conn.commit()
    wall = time.perf_counter() - started

    print(f"run {run.run_id}  generation {run.generation}  status {run.status}")
    if run.incomplete:
        print(f"  incomplete loads: {list(run.incomplete)}")
    for outcome in run.outcomes:
        flag = " SKIPPED" if outcome.skipped else ""
        print(
            f"  {outcome.rule_id} {outcome.scope_table:16s} rows={outcome.rows:6d} "
            f"raw={outcome.raw_conflicts:5d} {outcome.elapsed_ms:8.1f}ms"
            f" {outcome.verdicts}{flag}"
        )
    print(f"  stamped {len(run.results)} invariant_results rows")
    print(f"  raw conflicts {len(run.raw_conflicts)} -> surviving {len(run.conflicts)}")
    print(f"  by type: {run.by_type()}")

    diff = grade_run(run.conflicts)
    clean = grade_clean_sample(run.conflicts)
    print(diff.report())
    print(clean.report())
    print(f"wall clock: {wall:.2f}s (engine {run.elapsed_ms / 1000.0:.2f}s)")

    return 0 if (diff.passed and clean.passed) else 1


if __name__ == "__main__":
    sys.exit(main())
