"""The ingest benchmark measures something, and its threshold is a real gate.

A benchmark nobody can fail is a decoration. So this asserts both directions:

* against a **real ingest** of a real seed tree, the reported rate is a rate --
  positive, computed from records the pipeline actually landed;
* against an **impossible threshold**, the same run reports FAIL and the CLI exits
  non-zero. Without this, "PASS" would be evidence of nothing.

Warm-up handling is asserted too: the first generation is excluded from the rate,
because it pays for the page cache, the connection handshake and Postgres's
shared buffers. Reporting it would understate sustained throughput; silently
*including* it while claiming to discard it would overstate the discipline.

The rate here is measured on the small `dev` profile, so it is not the number the
ticket reports -- that comes from `python -m recon.bench ingest --assert-min-rps
500` against the committed **full** fixture tree. What this test proves is that
the harness and its gate work.
"""

from __future__ import annotations

from recon.bench import run_ingest_bench
from recon.bench.__main__ import main


def test_the_benchmark_measures_a_real_ingest(seed_tree, owner_engine) -> None:
    result = run_ingest_bench(
        root=seed_tree.root,
        generations=list(seed_tree.generations),
        min_rps=500.0,
        truncate=False,
        run_id="bench-test",
    )

    assert len(result.timings) == len(seed_tree.generations)
    assert result.timings[0].warmup is True, "the first generation is the warm-up"
    assert all(timing.warmup is False for timing in result.timings[1:])
    assert len(result.measured) == len(seed_tree.generations) - 1

    assert result.records == sum(timing.records for timing in result.timings[1:])
    assert result.records > 0
    assert result.seconds > 0
    assert result.rps == result.records / result.seconds
    assert all(timing.rejected == 0 for timing in result.timings)
    assert result.passed is True
    assert "SUSTAINED" in result.render() and "PASS" in result.render()


def test_the_threshold_is_a_gate_not_a_label(seed_tree, owner_engine) -> None:
    """The same run, an unreachable threshold: FAIL, and a non-zero exit."""
    result = run_ingest_bench(
        root=seed_tree.root,
        generations=list(seed_tree.generations),
        min_rps=10_000_000.0,
        truncate=False,
        run_id="bench-gate",
    )
    assert result.passed is False
    assert "FAIL" in result.render()

    exit_code = main(
        [
            "ingest",
            "--fixtures",
            str(seed_tree.root),
            "--assert-min-rps",
            "10000000",
            "--no-truncate",
            *[arg for gen in seed_tree.generations for arg in ("--generation", str(gen))],
        ]
    )
    assert exit_code == 1


def test_the_cli_exits_zero_when_the_threshold_is_met(seed_tree, owner_engine) -> None:
    exit_code = main(
        [
            "ingest",
            "--fixtures",
            str(seed_tree.root),
            "--assert-min-rps",
            "500",
            "--no-truncate",
            *[arg for gen in seed_tree.generations for arg in ("--generation", str(gen))],
        ]
    )
    assert exit_code == 0
