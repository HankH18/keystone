"""Fixtures for the invariant suite.

Every test in this package runs against a database this session created and will
drop (:mod:`tests.invariants.scratchdb`). The generation-3 fixture tree is ingested
once per session and the invariant run is executed once per session, because both are
deterministic pure functions of that tree -- so re-running them per test would buy
nothing but wall clock.

``KEYSTONE_REQUIRE_DB`` follows the convention `tests/schema/conftest.py` set: a
missing ``DATABASE_URL`` is a skip on a laptop without docker and a hard **error** in
CI, because a green that proves nothing is worse than a red.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import psycopg
import pytest

from tests.invariants.scratchdb import scratch_database, use_database

REQUIRE_DB_ENV = "KEYSTONE_REQUIRE_DB"

SKIP_REASON = (
    "DATABASE_URL is not set. The invariant suite needs a Postgres server "
    "(infra/docker-compose.yml, host port 55432); it creates and drops its own "
    "database on that server and never touches the one DATABASE_URL names."
)

REQUIRE_DB_REASON = (
    f"{REQUIRE_DB_ENV} is set, so the invariant suite must actually run -- but "
    "DATABASE_URL is not configured, so every test would have skipped and the run "
    "would have reported a green that proves nothing."
)


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


@pytest.fixture(scope="session")
def scratch_dsn() -> Iterator[str]:
    """A migrated, empty database owned by this pytest session."""
    if not _database_url():
        if os.environ.get(REQUIRE_DB_ENV):
            pytest.fail(REQUIRE_DB_REASON)
        pytest.skip(SKIP_REASON)
    with scratch_database("t6") as dsn, use_database(dsn):
        yield dsn


@pytest.fixture(scope="session")
def ingested_dsn(scratch_dsn: str) -> str:
    """The scratch database with the generation-3 fixture tree loaded.

    SS7: current state is generation 3 and invariants read it only, so generations 1
    and 2 are deliberately not loaded -- they belong to `field_lineage` and R4/R16's
    oscillation scan, which are a different ticket's path. `source_generations` is
    stamped for generation 3, which is what SS5.3's completeness gate reads.
    """
    from recon.adapters import build_adapters
    from recon.ingest import expected_counts_from_manifest, ingest_generation

    report = ingest_generation(
        build_adapters(None),
        3,
        run_id="t6-suite-gen3",
        expected=expected_counts_from_manifest(None),
    )
    failed = [result for result in report.sources if result.status != "ok"]
    assert not failed, f"generation-3 ingest did not complete: {failed}"
    return scratch_dsn


@pytest.fixture(scope="session")
def invariant_run(ingested_dsn: str):
    """One full invariant pass over generation 3, plus its wall clock.

    The transaction is **committed and the connection closed before the run is
    yielded**, and that is not tidiness. `build_context` ANALYZEs the `stg_*` tables,
    `ANALYZE` takes a `SHARE UPDATE EXCLUSIVE` lock, and inside a transaction block
    that lock is held until the transaction ends. A session-scoped fixture that
    yielded while still inside its transaction would pin all five staging tables for
    the whole pytest session -- every later module's `build_context` would block on
    it, and the suite would hang with no error message rather than fail. Committing
    here also makes the statistics visible to the other connections that assert on
    them (`test_performance.test_every_staging_table_has_planner_statistics`).
    """
    from recon.invariants.runner import run_invariants

    with psycopg.connect(ingested_dsn) as conn:
        run = run_invariants(conn, run_id="t6-suite")
        conn.commit()
    return run


@pytest.fixture
def conn(ingested_dsn: str) -> Iterator[psycopg.Connection]:
    """A fresh connection on the ingested scratch database, rolled back at the end."""
    with psycopg.connect(ingested_dsn) as connection:
        yield connection
        connection.rollback()
