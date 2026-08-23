"""R3 / the ticket's budget: a full invariant pass over the 100k dataset under 30s.

The number reported is the real wall clock of `run_invariants` over the committed
`full` profile -- 120,000 generation-3 records, 43,375 entities, fifteen rules --
measured on the ingested scratch database. It covers loading the snapshot, running
the SS4 cascade, materializing it, executing every rule file, de-duplicating,
applying `PRECEDENCE` and fingerprinting.

The budget is deliberately generous against the measured time: a threshold that sits
one millisecond above the observed value is a flake generator, not a guard.

**The measured number has to be reproducible, or the budget is not a gate.** The five
`stg_*` tables are freshly `COPY`ed by ingest and carry no statistics of their own, so
before `build_context` started ANALYZEing them the planner ran `R-013`'s correlated
sibling scan as a nested loop (~4.7s instead of ~1.7s) -- and, worse, WHICH tables had
statistics depended on how far autovacuum happened to get before the run started.
Byte-identical input measured 8.4s to 19.5s on one machine that way. The
:func:`test_every_staging_table_has_planner_statistics` assertion below is what keeps
that fixed; without it the timing assertions are measuring the background worker.
"""

from __future__ import annotations

import time

import psycopg

from recon.invariants.context import STAGING_TABLES

BUDGET_SECONDS = 30.0

A1_VOLUMES = {
    "stg_crm_contact": 40_000,
    "stg_crm_deal": 15_000,
    "stg_student": 25_000,
    "stg_enrollment": 22_000,
    "stg_payment": 18_000,
}


def test_the_dataset_under_test_is_the_full_profile(conn) -> None:
    """A pass under budget on the dev profile would prove nothing about the graded run."""
    with conn.cursor() as cur:
        for table, expected in sorted(A1_VOLUMES.items()):
            cur.execute(f"SELECT count(*) FROM {table} WHERE generation = 3")
            assert cur.fetchone()[0] == expected, table


def test_a_full_pass_completes_under_thirty_seconds(invariant_run) -> None:
    elapsed = invariant_run.elapsed_ms / 1000.0
    slowest = sorted(invariant_run.outcomes, key=lambda o: -o.elapsed_ms)[:3]
    detail = ", ".join(f"{o.rule_id}={o.elapsed_ms:.0f}ms" for o in slowest)
    assert elapsed < BUDGET_SECONDS, f"full pass took {elapsed:.2f}s (slowest: {detail})"


def test_every_rule_reports_its_own_wall_clock(invariant_run) -> None:
    """A per-rule timing is what turns "the pass got slower" into "this rule did"."""
    assert all(outcome.elapsed_ms >= 0 for outcome in invariant_run.outcomes)
    assert sum(o.elapsed_ms for o in invariant_run.outcomes) <= invariant_run.elapsed_ms


def test_every_staging_table_has_planner_statistics(invariant_run, conn) -> None:
    """`build_context` must have ANALYZEd every `stg_*` table the rules scan.

    `pg_class.reltuples` is `-1` for a table that has never been analyzed or
    vacuumed. Asserting on it rather than on `pg_stat_user_tables.last_analyze` keeps
    the test about the property that matters -- the planner has real row counts --
    instead of about which of ANALYZE or autovacuum supplied them.
    """
    with conn.cursor() as cur:
        for table in STAGING_TABLES:
            cur.execute("SELECT reltuples FROM pg_class WHERE relname = %s", (table,))
            reltuples = cur.fetchone()[0]
            assert reltuples > 0, (
                f"{table} has no planner statistics (reltuples={reltuples}); the "
                "invariant pass is planning blind and its timing is not reproducible"
            )


def test_a_second_pass_is_also_under_budget(ingested_dsn) -> None:
    """One fast sample must not be able to carry the gate.

    `invariant_run` is session-scoped, so `test_a_full_pass_completes_under_thirty_seconds`
    grades exactly one measurement. This takes a second, independent one on its own
    connection -- fresh `TEMP` tables, fresh cascade, same data -- so a fluke plan has
    to happen twice to pass.
    """
    from recon.invariants.runner import run_invariants

    with psycopg.connect(ingested_dsn) as connection:
        started = time.perf_counter()
        run = run_invariants(connection, run_id="t6-perf-second")
        elapsed = time.perf_counter() - started
    slowest = sorted(run.outcomes, key=lambda o: -o.elapsed_ms)[:3]
    detail = ", ".join(f"{o.rule_id}={o.elapsed_ms:.0f}ms" for o in slowest)
    assert elapsed < BUDGET_SECONDS, f"second pass took {elapsed:.2f}s (slowest: {detail})"


def test_analyze_staging_is_a_no_op_once_the_tables_have_statistics(invariant_run, conn) -> None:
    """The steady state must take no lock at all.

    `ANALYZE` holds a `SHARE UPDATE EXCLUSIVE` lock until its transaction ends, so an
    unconditional ANALYZE on every `build_context` lets one long-lived connection pin
    all five staging tables and block every other connection's `build_context` --
    a hang, which is worse than a slow plan because it has no error message.
    """
    from recon.invariants.context import analyze_staging

    assert analyze_staging(conn) == ()


def test_analyze_staging_skips_a_locked_table_instead_of_waiting_on_it(
    invariant_run, ingested_dsn
) -> None:
    """A table another session is holding is left alone, bounded by `lock_timeout`.

    Asserted with a real conflicting lock rather than by reading the code: `SHARE`
    mode conflicts with `SHARE UPDATE EXCLUSIVE`, so this is exactly the contention
    the timeout exists for. The caller's transaction must survive it -- the ANALYZE
    runs in its own savepoint precisely so a timeout aborts the ANALYZE and not the
    invariant run around it.
    """
    import psycopg

    from recon.invariants.context import ANALYZE_LOCK_TIMEOUT_MS, analyze_staging

    with psycopg.connect(ingested_dsn) as setup:
        try:
            setup.execute("UPDATE pg_class SET reltuples = -1 WHERE relname = 'stg_payment'")
            setup.commit()
        except psycopg.errors.InsufficientPrivilege:  # pragma: no cover - CI role dependent
            setup.rollback()
            import pytest

            pytest.skip("resetting pg_class.reltuples needs a superuser role")

    with psycopg.connect(ingested_dsn) as blocker:
        blocker.execute("LOCK TABLE stg_payment IN SHARE MODE")
        try:
            with psycopg.connect(ingested_dsn) as connection:
                started = time.perf_counter()
                analyzed = analyze_staging(connection)
                elapsed = time.perf_counter() - started
                assert "stg_payment" not in analyzed, "a locked table must be skipped"
                # The caller's transaction is still usable, and its `lock_timeout` is
                # the one it had on the way in.
                assert connection.execute("SELECT 1").fetchone() == (1,)
                assert connection.execute("SHOW lock_timeout").fetchone() == ("0",)
                connection.commit()
            assert elapsed < 10 * ANALYZE_LOCK_TIMEOUT_MS / 1000.0, (
                f"analyze_staging waited {elapsed:.2f}s on a locked table; it must give "
                f"up after ~{ANALYZE_LOCK_TIMEOUT_MS}ms rather than block the run"
            )
        finally:
            blocker.rollback()

    with psycopg.connect(ingested_dsn) as restore:
        restore.execute("ANALYZE stg_payment")
        restore.commit()
