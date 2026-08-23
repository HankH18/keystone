"""Benchmarks reported by the suite scorecard.

`python -m recon.bench ingest --assert-min-rps 500` measures the ingest path end to
end and exits non-zero below the threshold (SPEC Benchmarks; T-4 acceptance 2).
"""

from recon.bench.ingest import BenchResult, GenerationTiming, run_ingest_bench

__all__ = ["BenchResult", "GenerationTiming", "run_ingest_bench"]
