"""`python -m recon.suite` -- the graded verification harness.

Prints the scorecard, writes ``docs/scorecard.txt`` and ``docs/scorecard.json``,
and **exits non-zero on any failure**, which is what DESIGN pins.

The registry below is SPEC success criterion 1, item by item::

    golden diff 0 FN / 0 FP; join check passes; proposal-safety check passes
    (N conflicts -> N pending, mirror unchanged, C14 held); burst test halts
    exactly at cap; all six benchmarks green.

Ten checks and six benchmarks, in a fixed order:

===================  ===========================================================
golden-diff          0 FN / 0 FP / 0 field-exactness mismatches, per-type counts
clean-sample         0 of the 1,000 asserted-clean entities flagged (SS8)
join-check           25 unified views from ``GET /api/entities/{key}`` == golden
proposal-safety      N -> N, every C14 and every sensitive target held, mirror
                     byte-unchanged
oscillation-dedup    a second pass proposes zero; oscillating conflicts escalate
spend-cap-burst      the 120-thread burst's whole evidence vector
mirror-unchanged     landing + staging byte-identical across the reconciler run
determinism          two seeded runs: dataset, conflict set, confidence vector
manifest             the generator's 47 self-checks and Appendix A's minimums
coverage             >=80% on the seven core modules, measured by running pytest
===================  ===========================================================

Order is not cosmetic. ``coverage`` runs **first**: it shells out to pytest, and
much of that suite writes to a database, so it has to happen before the pipeline
takes its snapshot rather than under it (point ``KEYSTONE_COVERAGE_DATABASE_URL``
at a second database when they must not share at all -- see
:mod:`recon.suite.coverage`). Everything after it reads the one graded pass in
:mod:`recon.suite.pipeline`.

There is no ``SKIP``
---------------------
A check that cannot run has not passed. :class:`~recon.suite.checks
.NotYetImplemented` becomes a FAIL carrying its reason, and so does any other
exception -- including :class:`~recon.suite.pipeline.PreconditionFailed`, which
is what a run against an empty or half-loaded database gets. The alternative,
"not applicable", eventually gets reported for the one thing that mattered.

Keyless by construction
------------------------
Nothing here needs a provider key. ``LLM_PROVIDER`` defaults to ``mock``, and the
burst drives the real reservation/settlement ledger through
:class:`recon.llm.MockProvider` -- deterministic, offline, and priced from the
committed ``prices.yaml``. ``ANTHROPIC_API_KEY`` unset is the graded path, not a
degraded one.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from recon.bench.suite import BENCHMARKS
from recon.logging import configure_logging_once, console, get_logger
from recon.suite.burst import CHECK_NAME as SPEND_CAP_BURST
from recon.suite.burst import check_spend_cap_burst
from recon.suite.checks import CheckResult, NotYetImplemented
from recon.suite.coverage import COVERAGE, check_coverage
from recon.suite.determinism import DETERMINISM, check_determinism
from recon.suite.golden import (
    CLEAN_SAMPLE,
    GOLDEN_DIFF,
    JOIN_CHECK,
    check_clean_sample,
    check_golden_diff,
    check_join,
)
from recon.suite.manifest import MANIFEST, check_manifest
from recon.suite.mirror import CHECK_NAME as MIRROR_UNCHANGED
from recon.suite.pipeline import cached_pipeline, pipeline
from recon.suite.proposals import (
    OSCILLATION_DEDUP,
    PROPOSAL_SAFETY,
    check_oscillation_dedup,
    check_proposal_safety,
)
from recon.suite.report import write_scorecard

log = get_logger("recon.suite")


def check_mirror_unchanged() -> CheckResult:
    """The mirror digests bracketing the graded ``run_once()`` (see the pipeline).

    Not :func:`recon.suite.mirror.check_mirror_unchanged`, which runs its own
    reconciler pass: by the time the scorecard reaches this row the graded run
    has already happened, and a *third* pass would be bracketed around a run that
    proposes nothing (every fingerprint is open) -- hashing an idle database and
    calling it evidence. The digests compared here are the ones taken either side
    of the pass that wrote every proposal on the scorecard.
    """
    from recon.suite.mirror import compare

    run = pipeline()
    return compare(run.mirror_before, run.mirror_after)


#: Registered checks, keyed by the name accepted by ``--only``, in the order they
#: run and print. ``coverage`` is first; see the module docstring.
CHECKS: dict[str, Callable[[], CheckResult]] = {
    COVERAGE: check_coverage,
    GOLDEN_DIFF: check_golden_diff,
    CLEAN_SAMPLE: check_clean_sample,
    JOIN_CHECK: check_join,
    PROPOSAL_SAFETY: check_proposal_safety,
    OSCILLATION_DEDUP: check_oscillation_dedup,
    MIRROR_UNCHANGED: check_mirror_unchanged,
    DETERMINISM: check_determinism,
    MANIFEST: check_manifest,
    SPEND_CAP_BURST: check_spend_cap_burst,
    **BENCHMARKS,
}

#: The subset rendered under the BENCHMARKS heading.
BENCHMARK_NAMES = tuple(BENCHMARKS)

EMPTY_MESSAGE = "no checks yet"

#: Printed under every scorecard, green or red. These are the limits of what the
#: sixteen rows above actually establish, written next to the rows rather than in
#: a document nobody opens next to them -- a green whose scope is not stated is
#: read as covering more than it does.
SUITE_NOTES: tuple[str, ...] = (
    "SCOPE: every row except `manifest` and `determinism`'s dataset half grades the "
    "committed FULL-profile dataset (~360k landed records over 3 generations, 43,375 "
    "entities) in the database DATABASE_URL names. `determinism` and `manifest` "
    "additionally regenerate that dataset twice from `python -m recon.seed --profile "
    "full` into scratch directories (KEYSTONE_SUITE_SEED_PROFILE overrides the profile; "
    "at anything but `full` the committed-golden cross-checks do not apply and the row "
    "says so).",
    "NOT COVERED: browser-side dashboard timing (`bench:dashboard-load-p95` is "
    "service-side only -- see recon/bench/suite.py), a live Anthropic provider (the "
    "graded path is the offline mock; the burst drives the real ledger), any source "
    "other than the three committed JSONL adapters, the deployed Render/Neon "
    "environment, and the auto-apply/rollback path (`recon.apply`, covered by "
    "tests/apply rather than by a scorecard row).",
    "PRECONDITION: the suite grades a loaded database and asserts that with per-slice "
    "counts against fixtures/manifest.json. It does not ingest and does not materialize "
    "-- run `POST /internal/sync` first. A half-loaded database fails every row rather "
    "than producing a small green.",
)


def build_parser() -> argparse.ArgumentParser:
    """Build the suite argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m recon.suite",
        description="Run the Keystone verification suite and print a scorecard.",
    )
    parser.add_argument(
        "--only",
        metavar="NAME",
        action="append",
        default=None,
        help="Run only the named check (repeatable). An unknown name is an error.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the registered check names and exit.",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print the scorecard without writing docs/scorecard.{txt,json}.",
    )
    return parser


def select_checks(only: Sequence[str] | None) -> dict[str, Callable[[], CheckResult]]:
    """Return the checks to run, in registration order, filtered by ``--only``.

    An unknown name is a hard error rather than a silent empty selection: a typo
    in ``--only`` used to produce "no checks yet" and exit 0, which is a green
    that ran nothing.
    """
    if not only:
        return dict(CHECKS)
    unknown = sorted(set(only) - set(CHECKS))
    if unknown:
        raise SystemExit(f"unknown check(s) {unknown}; registered: {sorted(CHECKS)}")
    wanted = set(only)
    return {name: check for name, check in CHECKS.items() if name in wanted}


def run_check(name: str, check: Callable[[], CheckResult]) -> CheckResult:
    """Run one check and turn any escape into a FAIL row.

    Three outcomes, no fourth:

    * the check returns a result -- used as is;
    * it raises :class:`NotYetImplemented` -- FAIL, with the reason. Never a
      skip: a check whose subject does not exist has not passed;
    * it raises anything else -- FAIL, with the exception type and message, so
      a broken check cannot vanish from the scorecard by crashing the runner.
      That includes :class:`~recon.suite.pipeline.PreconditionFailed`: a run
      against a database that does not hold the dataset must be red, not absent.

    The traceback used to go out as ``traceback.print_exc(file=sys.stderr)``,
    which is a sink with nothing in front of it: a check that dies holding a
    record prints that record, frame locals' repr included, straight to the
    terminal. It is emitted as a structlog event with ``exc_info`` instead, so
    the same traceback is formatted *inside* the chain and redacted like every
    other event (``recon.logging.SINKS``).
    """
    try:
        return check()
    except NotYetImplemented as exc:
        return CheckResult.failed(name, f"not yet implemented: {exc}")
    except Exception as exc:  # a crashing check is a failing check, never a silent one
        log.error("suite.check_raised", rule=name, error=f"{type(exc).__name__}", exc_info=True)
        return CheckResult.failed(name, f"check raised {type(exc).__name__}: {exc}")


def main(argv: Sequence[str] | None = None) -> int:
    """Print the scorecard. Returns the process exit code.

    Every line goes out through :func:`recon.logging.console`, not ``print``: a
    check's ``detail`` string quotes what the check compared, and the scorecard
    is written to a terminal an operator and a grader read. ``console`` scrubs
    it in the default `safe` mode and leaves the column layout alone.
    """
    # `recon.logging.ENTRY_POINTS`: install the redaction processor first.
    configure_logging_once()
    args = build_parser().parse_args(argv)

    if args.list:
        for name in CHECKS:
            console(name)
        if not CHECKS:
            console(EMPTY_MESSAGE)
        return 0

    selected = select_checks(args.only)
    if not selected:  # pragma: no cover - select_checks refuses an empty selection
        console(EMPTY_MESSAGE)
        return 1

    notes: list[str] = list(SUITE_NOTES)
    if args.only:
        notes.append(
            f"PARTIAL RUN: --only {sorted(set(args.only))}; "
            f"{len(CHECKS) - len(selected)} registered check(s) did not run"
        )

    # Checks first, then the artifacts, then the benchmarks -- in that order, and
    # not for tidiness. ``bench:dashboard-load-p95`` replays the Overview route,
    # which begins with ``GET /api/scorecard``, and that endpoint serves the
    # artifact written here. Running the benchmarks before the write would
    # measure the endpoint's 503 path on any deployment whose scorecard has not
    # been generated yet -- a benchmark passing or failing on the presence of a
    # file rather than on the service's speed. The artifacts are written again
    # afterwards so the committed copy carries the benchmark rows too.
    check_names = [name for name in selected if name not in BENCHMARK_NAMES]
    bench_names = [name for name in selected if name in BENCHMARK_NAMES]

    results = [run_check(name, selected[name]) for name in check_names]
    run = cached_pipeline()

    wrote: tuple[str, str] | None = None
    if not args.no_write and bench_names:
        try:
            interim = write_scorecard(results, run=run, benchmarks=BENCHMARK_NAMES, notes=notes)
            wrote = (str(interim[0]), str(interim[1]))
        except OSError as exc:
            console(f"could not write the interim scorecard artifacts: {exc}")
            return 1

    results += [run_check(name, selected[name]) for name in bench_names]
    run = cached_pipeline() or run

    from recon.suite.report import render

    console(render(results, run=run, benchmarks=BENCHMARK_NAMES, notes=notes))

    if not args.no_write:
        try:
            text_path, json_path = write_scorecard(
                results, run=run, benchmarks=BENCHMARK_NAMES, notes=notes
            )
            wrote = (str(text_path), str(json_path))
        except OSError as exc:
            console(f"could not write the scorecard artifacts: {exc}")
            return 1
    if wrote:
        console(f"wrote {wrote[0]} and {wrote[1]}")

    return 1 if any(not result.ok for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
