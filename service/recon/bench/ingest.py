"""The ingestion benchmark: measured records/second, end to end (SPEC Benchmarks).

SPEC requires "ingestion >=500 rec/s sustained from stubs". This measures the whole
path a real sync takes -- adapter read, Pydantic validation, `COPY` into
`raw_records`, Python normalization, `COPY` into the five `stg_*` tables, and the
`source_generations` / `ingest_runs` stamps. Timing only the parser, or only the
`COPY`, would produce a bigger number that means nothing: the throughput that
matters is the one the pipeline actually achieves.

**Warm-up is measured and discarded.** The first generation pays for the OS page
cache, the connection handshake, Postgres's shared buffers and every import; it is
ingested exactly like the rest and then left out of the rate. What is reported is
the sustained rate over the remaining generations. The warm-up's own rate is
printed too, so "we discarded the slow one" is visible rather than implied.

**The tables are truncated first by default.** A benchmark that appends to
whatever the previous run left behind measures index maintenance on a table of
unknown size, so consecutive runs would report falling throughput for no reason.
`--no-truncate` keeps the data if you want to measure the append-onto-existing
case deliberately.

The threshold is an assertion, not a suggestion: below `--assert-min-rps` the
command exits non-zero.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

from recon.adapters import build_adapters, default_fixtures_root
from recon.db import get_engine
from recon.ingest import STAGING_TABLES, expected_counts_from_manifest, ingest_generation

__all__ = ["BenchResult", "GenerationTiming", "run_ingest_bench", "truncate_landing"]


@dataclass(frozen=True, slots=True)
class GenerationTiming:
    generation: int
    records: int
    rejected: int
    seconds: float
    warmup: bool

    @property
    def rps(self) -> float:
        return self.records / self.seconds if self.seconds > 0 else 0.0


@dataclass(frozen=True, slots=True)
class BenchResult:
    timings: tuple[GenerationTiming, ...]
    min_rps: float

    @property
    def measured(self) -> tuple[GenerationTiming, ...]:
        return tuple(timing for timing in self.timings if not timing.warmup)

    @property
    def records(self) -> int:
        return sum(timing.records for timing in self.measured)

    @property
    def seconds(self) -> float:
        return sum(timing.seconds for timing in self.measured)

    @property
    def rps(self) -> float:
        return self.records / self.seconds if self.seconds > 0 else 0.0

    @property
    def passed(self) -> bool:
        return self.rps >= self.min_rps

    def render(self) -> str:
        lines = [
            "ingest benchmark",
            f"  fixtures  : {self.records} records over "
            f"{len(self.measured)} measured generation(s)",
            "  generation      records   seconds        rec/s",
        ]
        for timing in self.timings:
            tag = "  (warm-up, discarded)" if timing.warmup else ""
            lines.append(
                f"  gen {timing.generation:<10d} {timing.records:>7d} "
                f"{timing.seconds:>9.3f} {timing.rps:>12.1f}{tag}"
            )
        lines.append(f"  SUSTAINED {self.rps:.1f} rec/s (threshold {self.min_rps:.0f})")
        lines.append(f"  {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


def truncate_landing() -> None:
    """Empty the landing + staging tables so a run starts from a known size.

    Runs as the `DATABASE_URL` principal (the schema owner), not as
    `recon_writer` -- which holds no DELETE on `raw_records` and must not, since
    the landing table is append-only to the pipeline (R1/R4). Wiping a benchmark's
    workspace is an operator action, and it uses an operator's credentials.
    """
    tables = ", ".join(sorted({"raw_records", *STAGING_TABLES.values()}))
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


def run_ingest_bench(
    *,
    root: Path | str | None = None,
    generations: Sequence[int] | None = None,
    min_rps: float = 500.0,
    truncate: bool = True,
    run_id: str | None = None,
) -> BenchResult:
    """Ingest every generation, discard the first as warm-up, return the rate."""
    root = Path(root) if root is not None else default_fixtures_root()
    adapters = build_adapters(root)
    expected = expected_counts_from_manifest(root)

    if generations is None:
        available: set[int] = set()
        for adapter in adapters.values():
            available.update(adapter.generations())
        generations = sorted(available)
    if not generations:
        raise RuntimeError(f"no generations found under {root}")

    if truncate:
        truncate_landing()

    run_id = run_id or f"bench-{int(time.time())}"
    timings: list[GenerationTiming] = []
    for index, generation in enumerate(generations):
        started = time.monotonic()
        report = ingest_generation(
            adapters,
            generation,
            run_id=f"{run_id}-gen{generation}",
            expected=expected,
        )
        elapsed = time.monotonic() - started
        timings.append(
            GenerationTiming(
                generation=generation,
                records=report.records_ok,
                rejected=report.records_rejected,
                seconds=elapsed,
                warmup=index == 0 and len(generations) > 1,
            )
        )

    return BenchResult(tuple(timings), min_rps)
