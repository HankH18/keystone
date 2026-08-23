"""`python -m recon.bench <benchmark>` -- the measured numbers, not estimated ones.

Exits non-zero when a benchmark misses its asserted threshold, so it can be a CI
gate rather than a report nobody reads.
"""

from __future__ import annotations

import argparse
import sys

from recon.bench.ingest import run_ingest_bench
from recon.logging import configure_logging_once


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m recon.bench")
    sub = parser.add_subparsers(dest="benchmark", required=True)

    ingest = sub.add_parser("ingest", help="records/second through the full ingest path")
    ingest.add_argument(
        "--assert-min-rps",
        type=float,
        default=500.0,
        help="fail when sustained throughput is below this (SPEC: 500)",
    )
    ingest.add_argument(
        "--fixtures",
        default=None,
        help="fixture tree to ingest (default: the committed fixtures/ tree)",
    )
    ingest.add_argument(
        "--generation",
        type=int,
        action="append",
        dest="generations",
        default=None,
        help="restrict to these generations (repeatable); the first is the warm-up",
    )
    ingest.add_argument(
        "--no-truncate",
        action="store_true",
        help="append to whatever is already in raw_records instead of starting clean",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # `recon.logging.ENTRY_POINTS`: the bench drives the real ingest path, which
    # logs rejected records; install the redaction processor before it can.
    configure_logging_once()
    args = build_parser().parse_args(argv)
    if args.benchmark == "ingest":
        result = run_ingest_bench(
            root=args.fixtures,
            generations=args.generations,
            min_rps=args.assert_min_rps,
            truncate=not args.no_truncate,
        )
        print(result.render())
        return 0 if result.passed else 1
    raise SystemExit(f"unknown benchmark {args.benchmark!r}")


if __name__ == "__main__":
    sys.exit(main())
