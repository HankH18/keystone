"""The six SPEC benchmarks, each **asserting** its threshold.

    "cross-source query <1s p95 on 100k (20 runs); full invariant/reconciliation
     pass <30s on 100k; ingestion >=500 rec/s sustained from stubs; golden-set
     exact; spend cap exact under burst; dashboard <1s p95 on 100k."
                                                        -- SPEC SS Constraints

Every one of them returns a :class:`~recon.suite.checks.CheckResult`, so a missed
threshold is a red row and a non-zero exit, not a number in a paragraph. A
benchmark that only reports is a benchmark nobody notices regressing.

Where the numbers come from
----------------------------
Four of the six read measurements the suite has already taken, because taking
them twice would be slower *and* would let two rows describe two different runs:
``invariant-pass`` and ``conflict-accuracy`` read
:func:`recon.suite.pipeline.pipeline`, ``spend-cap`` reads the one cached burst
(:func:`recon.suite.burst.burst_outcome`). The two latency benchmarks and the
ingestion benchmark do their own measuring, here.

Percentiles
------------
p95 over 20 measured samples by the **nearest-rank** method: sort ascending and
take element ``ceil(0.95 * 20) = 19`` (1-based) -- the 19th of 20. No
interpolation, so the reported number is a request that actually happened. The
first iteration of every latency loop is a discarded warm-up: it pays for
connection setup, prepared-statement planning and first-touch page cache, and
including it would measure the process, not the query.

What the dashboard number is, and what it is not
--------------------------------------------------
``dashboard-load`` replays the **server side of the Overview route** -- the one
``GET /api/scorecard`` plus the fourteen ``GET /api/conflicts?type=..&page_size=1``
count queries that ``dashboard/src/routes/Overview.tsx`` issues -- through an
in-process ``TestClient``, and asserts the p95 of their combined wall time.

It therefore covers: FastAPI routing, auth, the SQL those fifteen endpoints run,
and JSON serialisation, against the 100k dataset. It does **not** cover: network
latency, TLS, uvicorn/worker scheduling, HTTP/2 multiplexing or the browser's own
request concurrency, JS bundle download and parse, React render, or paint. It is
a **service-side floor on a browser page load, not a browser page load** -- an
in-process TestClient number is not a browser number, and this row says so rather
than letting the threshold imply otherwise. The a11y/Playwright suite in
``dashboard/`` is where a real browser drives the page; wiring a k6 or Playwright
timing into this scorecard is future work and is listed as a gap in
``docs/scorecard.txt``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence

from sqlalchemy import text

from recon.adapters.jsonl import build_adapters, default_fixtures_root
from recon.bench.ingest import STAGING_TABLES
from recon.db import get_engine
from recon.ingest import expected_counts_from_manifest, ingest_generation
from recon.invariants.grading import grade_clean_sample, grade_run
from recon.logging import get_logger
from recon.suite.burst import burst_outcome
from recon.suite.checks import CheckResult
from recon.suite.golden import expected_views
from recon.suite.pipeline import pipeline
from recon.suite.probe import admin_headers, probe_client

__all__ = [
    "BENCHMARKS",
    "CONFLICT_TYPES",
    "DASHBOARD_LOAD_P95_SECONDS",
    "INGEST_MIN_RPS",
    "INVARIANT_PASS_SECONDS",
    "LATENCY_RUNS",
    "QUERY_P95_SECONDS",
    "check_conflict_accuracy",
    "check_cross_source_query",
    "check_dashboard_load",
    "check_ingestion",
    "check_invariant_pass",
    "check_spend_cap",
    "percentile",
]

log = get_logger("recon.bench.suite")

#: SPEC thresholds. Named, not inlined, so the row and the assertion cannot drift.
QUERY_P95_SECONDS = 1.0
DASHBOARD_LOAD_P95_SECONDS = 1.0
INVARIANT_PASS_SECONDS = 30.0
INGEST_MIN_RPS = 500.0

#: SPEC pins "20 runs" for the latency benchmarks. One extra is run first and
#: discarded as warm-up, so 20 *measured* samples remain.
LATENCY_RUNS = 20

#: The fourteen committed conflict classes, the same list the Overview route
#: iterates (``dashboard/src/lib/contract.ts``).
CONFLICT_TYPES = tuple(f"C{index}" for index in range(1, 15))

CROSS_SOURCE_QUERY = "bench:cross-source-query-p95"
INVARIANT_PASS = "bench:invariant-pass"
INGESTION = "bench:ingestion-rps"
CONFLICT_ACCURACY = "bench:conflict-accuracy"
SPEND_CAP = "bench:spend-cap-exact"
DASHBOARD_LOAD = "bench:dashboard-load-p95"

#: The landing + staging surface the ingestion benchmark empties **inside its own
#: transaction** and restores by rolling back.
_LANDING_TABLES = ("raw_records", "ingest_runs", "source_generations")


def percentile(samples: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile: a value that was actually measured."""
    if not samples:
        raise ValueError("no samples")
    ordered = sorted(samples)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def _latency_summary(samples: Sequence[float]) -> str:
    return (
        f"n={len(samples)} min={min(samples) * 1000:.1f}ms "
        f"p50={percentile(samples, 0.50) * 1000:.1f}ms "
        f"p95={percentile(samples, 0.95) * 1000:.1f}ms "
        f"max={max(samples) * 1000:.1f}ms"
    )


# ======================================================================================
# 1. cross-source query p95
# ======================================================================================
def check_cross_source_query() -> CheckResult:
    """``GET /api/entities/{key}`` p95 under 1s, 20 measured runs on 100k."""
    run = pipeline()
    keys = [str(entry["person_key"]) for entry in expected_views()]
    if not keys:
        return CheckResult.failed(
            CROSS_SOURCE_QUERY, "no golden expected-view keys to query against"
        )
    # LATENCY_RUNS measured + one warm-up, cycling the golden keys so the loop is
    # not 20 repeats of one cached row.
    plan = [keys[index % len(keys)] for index in range(LATENCY_RUNS + 1)]

    headers = admin_headers()
    samples: list[float] = []
    with probe_client() as client:
        for index, key in enumerate(plan):
            started = time.perf_counter()
            response = client.get(f"/api/entities/{key}", headers=headers)
            elapsed = time.perf_counter() - started
            if response.status_code != 200:
                return CheckResult.failed(
                    CROSS_SOURCE_QUERY,
                    f"GET /api/entities/{key} answered {response.status_code}",
                )
            if index:  # index 0 is the discarded warm-up
                samples.append(elapsed)

    p95 = percentile(samples, 0.95)
    detail = (
        f"{_latency_summary(samples)} threshold<{QUERY_P95_SECONDS:.0f}s "
        f"(warm-up discarded; {run.precondition.entities} entities, "
        f"{sum(run.precondition.landing.values())} landed records; in-process ASGI)"
    )
    if p95 >= QUERY_P95_SECONDS:
        return CheckResult.failed(
            CROSS_SOURCE_QUERY, f"p95 {p95 * 1000:.1f}ms exceeds the 1s budget | {detail}"
        )
    return CheckResult.passed(CROSS_SOURCE_QUERY, detail)


# ======================================================================================
# 2. full invariant / reconciliation pass
# ======================================================================================
def check_invariant_pass() -> CheckResult:
    """Detection + persistence + proposal generation, under 30s on 100k."""
    run = pipeline()
    total = run.full_pass_seconds
    detail = (
        f"{total:.2f}s total = invariants {run.invariants_seconds:.2f}s + persist "
        f"{run.persist_seconds:.2f}s + reconcile {run.reconcile_seconds:.2f}s "
        f"(threshold <{INVARIANT_PASS_SECONDS:.0f}s); "
        f"{len(run.run_a.conflicts)} conflicts over "
        f"{sum(run.precondition.landing.values())} landed records"
    )
    if total >= INVARIANT_PASS_SECONDS:
        return CheckResult.failed(INVARIANT_PASS, f"pass took {total:.2f}s | {detail}")
    return CheckResult.passed(INVARIANT_PASS, detail)


# ======================================================================================
# 3. ingestion throughput
# ======================================================================================
def check_ingestion() -> CheckResult:
    """>=500 rec/s sustained through the real ingest path, measured and restored.

    The whole benchmark runs inside ONE transaction that is rolled back: the
    landing and staging tables are emptied (``TRUNCATE`` is transactional in
    Postgres), every generation is ingested through :func:`recon.ingest
    .ingest_generation` exactly as production does it, the rate is measured, and
    the rollback puts the mirror back byte for byte.

    That is not a convenience either. ``recon.bench.ingest.truncate_landing``
    commits its truncation, which is correct for a standalone benchmark against a
    scratch database and catastrophic here: the suite's other nine checks need
    the identity layer, and the identity layer's provenance floor points at rows
    in ``raw_records``. Measuring throughput must not cost the grader a six-minute
    re-materialization.
    """
    run = pipeline()
    root = default_fixtures_root()
    adapters = build_adapters(root)
    expected = expected_counts_from_manifest(root)

    generations: set[int] = set()
    for adapter in adapters.values():
        generations.update(adapter.generations())
    ordered = sorted(generations)
    if len(ordered) < 2:
        return CheckResult.failed(
            INGESTION,
            f"only {len(ordered)} generation(s) under {root}: with no warm-up to "
            "discard the sustained rate would be a cold-start rate",
        )

    tables = ", ".join(sorted({*_LANDING_TABLES, *STAGING_TABLES.values()}))
    timings: list[tuple[int, int, float]] = []
    engine = get_engine()
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
            for generation in ordered:
                started = time.monotonic()
                report = ingest_generation(
                    adapters,
                    generation,
                    run_id=f"suite-bench-gen{generation}",
                    expected=expected,
                    conn=conn,
                )
                timings.append((generation, report.records_ok, time.monotonic() - started))
        finally:
            transaction.rollback()

    # The rollback is a claim; verify it against the digest the pipeline took.
    from recon.suite.mirror import mirror_digest

    with engine.connect() as conn:
        after = mirror_digest(conn)
    restored = not run.mirror_after.changed_tables(after)

    measured = timings[1:]
    records = sum(count for _, count, _ in measured)
    seconds = sum(elapsed for _, _, elapsed in measured)
    rps = records / seconds if seconds else 0.0

    per_gen = " ".join(
        f"gen{generation}:{count}rec/{elapsed:.2f}s"
        + ("(warm-up)" if index == 0 else f"={count / elapsed:.0f}rps")
        for index, (generation, count, elapsed) in enumerate(timings)
    )
    detail = (
        f"{rps:.0f} rec/s sustained over {records} records in {seconds:.2f}s "
        f"(threshold >={INGEST_MIN_RPS:.0f}); {per_gen}; "
        f"measured inside a rolled-back transaction, mirror restored={restored}"
    )
    failures = []
    if rps < INGEST_MIN_RPS:
        failures.append(f"{rps:.0f} rec/s is below the {INGEST_MIN_RPS:.0f} rec/s floor")
    if not restored:
        failures.append(
            "the benchmark's rollback did NOT restore the mirror: "
            f"{run.mirror_after.changed_tables(after)}"
        )
    if not records:
        failures.append("zero records were ingested, so the rate grades nothing")
    if failures:
        return CheckResult.failed(INGESTION, f"{detail} | " + " | ".join(failures))
    return CheckResult.passed(INGESTION, detail)


# ======================================================================================
# 4. conflict-detection accuracy
# ======================================================================================
def check_conflict_accuracy() -> CheckResult:
    """Exact on the golden set: 0 FN, 0 FP, 0 field mismatches, 0 clean flagged."""
    run = pipeline()
    diff = grade_run(run.run_a.conflicts)
    clean = grade_clean_sample(run.run_a.conflicts)

    total = diff.golden_total or 1
    precision = diff.matched / (diff.matched + len(diff.false_positives) or 1)
    recall = diff.matched / total
    detail = (
        f"precision {precision:.6f} recall {recall:.6f} on {diff.golden_total} golden "
        f"entries (FN={len(diff.false_negatives)} FP={len(diff.false_positives)} "
        f"field-mismatches={len(diff.mismatches)}); clean sample "
        f"{clean.sampled - len(clean.flagged)}/{clean.sampled} unflagged; "
        f"threshold: EXACT"
    )
    if diff.passed and clean.passed and diff.golden_total:
        return CheckResult.passed(CONFLICT_ACCURACY, detail)
    return CheckResult.failed(CONFLICT_ACCURACY, f"detection is not exact | {detail}")


# ======================================================================================
# 5. spend cap under burst
# ======================================================================================
def check_spend_cap() -> CheckResult:
    """The cap halts exactly at the cap: granted == admitted, spend == cap."""
    outcome = burst_outcome()
    exact = (
        outcome.granted == outcome.admitted_expected
        and outcome.final_spend == outcome.actual_each * outcome.admitted_expected
        and outcome.spend_while_open == outcome.cap_microusd
        and outcome.ledger_violations == 0
        and not outcome.over_admitted
    )
    detail = (
        f"cap={outcome.cap_microusd}uUSD granted={outcome.granted}/"
        f"{outcome.admitted_expected} of {outcome.contenders} contenders; "
        f"refused={outcome.refused}; reserved-while-open={outcome.spend_while_open} "
        f"(== cap); settled spend={outcome.final_spend} "
        f"(== {outcome.actual_each} x {outcome.admitted_expected}); "
        f"ledger violations={outcome.ledger_violations}; over-admitted="
        f"{outcome.over_admitted}; threshold: EXACT"
    )
    if exact and outcome.ok:
        return CheckResult.passed(SPEND_CAP, detail)
    reasons = "; ".join(outcome.failures) or "the observed vector is not exact"
    return CheckResult.failed(SPEND_CAP, f"{reasons} | {detail}")


# ======================================================================================
# 6. dashboard load p95 (service side)
# ======================================================================================
def check_dashboard_load() -> CheckResult:
    """The Overview route's fifteen server calls, p95 under 1s. Service side only."""
    run = pipeline()
    headers = admin_headers()
    samples: list[float] = []
    calls_per_iteration = 1 + len(CONFLICT_TYPES)

    with probe_client() as client:
        for index in range(LATENCY_RUNS + 1):
            started = time.perf_counter()
            responses = [client.get("/api/scorecard", headers=headers)]
            responses += [
                client.get(
                    "/api/conflicts",
                    params={"type": conflict_type, "page": 1, "page_size": 1},
                    headers=headers,
                )
                for conflict_type in CONFLICT_TYPES
            ]
            elapsed = time.perf_counter() - started
            bad = [
                f"{response.request.url.path}?{response.request.url.query.decode()}"
                f" -> {response.status_code}"
                for response in responses
                if response.status_code != 200
            ]
            if bad:
                return CheckResult.failed(
                    DASHBOARD_LOAD, f"the overview route's calls did not all answer 200: {bad[:3]}"
                )
            if index:
                samples.append(elapsed)

    p95 = percentile(samples, 0.95)
    detail = (
        f"{_latency_summary(samples)} threshold<{DASHBOARD_LOAD_P95_SECONDS:.0f}s; "
        f"{calls_per_iteration} server calls per load (GET /api/scorecard + "
        f"{len(CONFLICT_TYPES)} x GET /api/conflicts?type=..&page_size=1), "
        f"{run.precondition.entities} entities / {len(run.proposals)} proposals; "
        "SERVICE SIDE ONLY -- in-process ASGI, no network, no TLS, no browser, "
        "no JS parse and no render; this is a floor on a page load, not a page load"
    )
    if p95 >= DASHBOARD_LOAD_P95_SECONDS:
        return CheckResult.failed(
            DASHBOARD_LOAD, f"p95 {p95 * 1000:.1f}ms exceeds the 1s budget | {detail}"
        )
    return CheckResult.passed(DASHBOARD_LOAD, detail)


#: Registered in SPEC's order.
BENCHMARKS = {
    CROSS_SOURCE_QUERY: check_cross_source_query,
    INVARIANT_PASS: check_invariant_pass,
    INGESTION: check_ingestion,
    CONFLICT_ACCURACY: check_conflict_accuracy,
    SPEND_CAP: check_spend_cap,
    DASHBOARD_LOAD: check_dashboard_load,
}
