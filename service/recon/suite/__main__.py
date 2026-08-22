"""`python -m recon.suite` -- the graded verification harness.

Prints the scorecard and **exits non-zero on any failure**, which is what
DESIGN pins. T-14 registers the remaining checks (golden diff, clean-sample,
join hash, proposal safety, oscillation, burst cap, determinism) in ``CHECKS``;
the CLI surface below does not change.

One check is registered today: ``mirror-unchanged`` (see :mod:`recon.suite
.mirror`). It is the compensating control migration 0006's provenance floor
cites -- the reason "fabrication has to leave a row in the landing table"
means anything is that something reads the landing table. It hashes every
landing and staging table and asserts the reconciler left them byte-identical.

**It currently FAILS**, on purpose, with ``recon.reconciler does not exist yet``.
The digest half is built and exercised; the run it is meant to bracket is T-9
and is not. Hashing an untouched database twice and printing PASS would be a
green that proves the opposite of its own claim, so the runner turns
:class:`~recon.suite.checks.NotYetImplemented` into a loud FAIL and a non-zero
exit instead. That is the intended state of the scorecard until T-9 lands.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from collections.abc import Callable, Sequence

from recon import __version__
from recon.suite.checks import CheckResult, NotYetImplemented
from recon.suite.mirror import CHECK_NAME as MIRROR_UNCHANGED
from recon.suite.mirror import check_mirror_unchanged

#: Registered checks, keyed by the name accepted by ``--only``, in the order
#: they run and print.
CHECKS: dict[str, Callable[[], CheckResult]] = {
    MIRROR_UNCHANGED: check_mirror_unchanged,
}

HEADER_TITLE = f"Keystone reconciliation suite -- scorecard (v{__version__})"
COLUMNS = f"{'CHECK':<40} {'STATUS':<8} DETAIL"
RULE = "=" * len(HEADER_TITLE)
EMPTY_MESSAGE = "no checks yet"


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
        help="Run only the named check (repeatable). Unknown names are ignored for now.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the registered check names and exit.",
    )
    return parser


def select_checks(only: Sequence[str] | None) -> dict[str, Callable[[], CheckResult]]:
    """Return the checks to run, in registration order, filtered by ``--only``."""
    if not only:
        return dict(CHECKS)
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
    """
    try:
        return check()
    except NotYetImplemented as exc:
        return CheckResult.failed(name, f"not yet implemented: {exc}")
    except Exception as exc:  # a crashing check is a failing check, never a silent one
        traceback.print_exc(file=sys.stderr)
        return CheckResult.failed(name, f"check raised {type(exc).__name__}: {exc}")


def main(argv: Sequence[str] | None = None) -> int:
    """Print the scorecard. Returns the process exit code."""
    args = build_parser().parse_args(argv)

    if args.list:
        for name in CHECKS:
            print(name)
        if not CHECKS:
            print(EMPTY_MESSAGE)
        return 0

    print(HEADER_TITLE)
    print(RULE)
    print(COLUMNS)

    selected = select_checks(args.only)
    if not selected:
        print(EMPTY_MESSAGE)
        return 0

    results = [run_check(name, check) for name, check in selected.items()]
    for result in results:
        print(result.row())

    failed = [result for result in results if not result.ok]
    print(RULE)
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
