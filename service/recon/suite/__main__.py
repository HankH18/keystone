"""`python -m recon.suite` -- the graded verification harness.

Today it is an empty registry: it prints the scorecard header, reports that
there is nothing to run, and exits 0. T-14 registers the real checks (golden
diff, clean-sample, join hash, proposal safety, oscillation, burst cap,
determinism) in `CHECKS`; the CLI surface below does not change.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from recon import __version__

#: Registered checks, keyed by the name accepted by ``--only``.
#: Each value will be a callable returning a check result once T-14 lands.
CHECKS: dict[str, Callable[[], object]] = {}

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


def select_checks(only: Sequence[str] | None) -> dict[str, Callable[[], object]]:
    """Return the checks to run, in registration order, filtered by ``--only``."""
    if not only:
        return dict(CHECKS)
    wanted = set(only)
    return {name: check for name, check in CHECKS.items() if name in wanted}


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

    # T-14: execute `selected` here and exit non-zero on any failure.
    print(EMPTY_MESSAGE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
