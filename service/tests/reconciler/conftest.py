"""Fixtures for the reconciler suite.

Isolation follows the convention `tests/invariants/conftest.py` set and reuses its
helper verbatim: ``DATABASE_URL`` supplies the *server coordinates* only, and this
session creates, migrates, uses and drops a database of its own
(``keystone_t7_<pid>_<token>``). Nothing here reads or writes the database
``DATABASE_URL`` names, so this suite and another agent's cannot see each other's
rows -- which matters more here than anywhere, because the graded claim is a
*count* of proposals.

``KEYSTONE_REQUIRE_DB`` follows the same convention: a missing ``DATABASE_URL``
skips on a laptop with no docker and **fails** in CI, because a green that proves
nothing is worse than a red.

What the session fixture builds, and why all three generations
--------------------------------------------------------------
**All three generations** are ingested and the committed invariant engine is run
over them, so ``conflicts`` holds the real, graded conflict set -- these tests
count real proposals against real detections, not fixtures.

``recon.resolve.materialize`` is then run with ``lineage_generations=(1, 2, 3)``,
for two separate reasons:

* the three heaviest-weighted confidence signals are read from
  ``entity_link_candidates``, and without materialization that table is empty --
  every identity signal would score 0, every score would still be produced, and
  every assertion here would still pass. That is the definition of an untested
  green;
* contract SS7's A -> B -> A pattern needs **three ascending generations** of one
  ``(person_key, field)``. Lineage covering one generation cannot contain it, so
  a suite built that way can only test R16 against hand-planted rows.

**This fixture previously ingested generation 3 alone**, and this docstring
previously said that ingesting generations 1-2 "changes the detected conflict set
away from the graded one (4,759 conflicts instead of golden's 3,050)". That is
false, and it was load-bearing: it is the stated reason R16's oscillation half was
never exercised end to end. Measured in a scratch database with generations 1, 2
and 3 ingested, ``run_invariants`` returns **3,050** conflicts in exactly the
golden per-type distribution -- byte-for-byte the graded set -- because
invariants read generation 3 only (SS7). :func:`_assert_store_matches_golden`
asserts that on every run, so the claim is enforced rather than believed.

With all three generations, ``field_lineage`` holds ~1,279,575 rows across
generations 1-3, the committed A -> B -> A scan finds **25** oscillating pairs
unaided, and those 25 are exactly the entries ``golden/conflicts.json`` marks
``"oscillating": true``. The whole build costs about a minute.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection

from tests.invariants.scratchdb import scratch_database, use_database

REQUIRE_DB_ENV = "KEYSTONE_REQUIRE_DB"

SKIP_REASON = (
    "DATABASE_URL is not set. The reconciler suite needs a Postgres server "
    "(infra/docker-compose.yml, host port 55432); it creates and drops its own "
    "database on that server and never touches the one DATABASE_URL names."
)

REQUIRE_DB_REASON = (
    f"{REQUIRE_DB_ENV} is set, so the reconciler suite must actually run -- but "
    "DATABASE_URL is not configured, so every database test would have skipped and "
    "the run would have reported a green that proves nothing."
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
    with scratch_database("t7") as dsn, use_database(dsn):
        yield dsn


#: SS7: gen 1 baseline, gen 2 changes, gen 3 = current state with >= 25 fields
#: re-asserting their gen-1 value. All three are needed for an A -> B -> A window.
INGESTED_GENERATIONS = (1, 2, 3)

#: SS8/SS7: `golden/conflicts.json` carries `"oscillating": true` on exactly this
#: many entries. The A -> B -> A scan over generations 1-3 must find the same set.
GOLDEN_OSCILLATING = 25


@pytest.fixture(scope="session")
def conflict_store(scratch_dsn: str) -> str:
    """The scratch database with generations 1-3 ingested and the invariants run.

    Committed once, because everything downstream reads it and nothing downstream
    is allowed to change it: every test that writes does so inside a transaction
    it rolls back (see :func:`writer`).
    """
    import psycopg

    from recon.adapters import build_adapters
    from recon.ingest import expected_counts_from_manifest, ingest_generation
    from recon.invariants.runner import persist_run, run_invariants

    for generation in INGESTED_GENERATIONS:
        report = ingest_generation(
            build_adapters(None),
            generation,
            run_id=f"t7-gen{generation}",
            expected=expected_counts_from_manifest(None),
        )
        failed = [result for result in report.sources if result.status != "ok"]
        assert not failed, f"generation-{generation} ingest did not complete: {failed}"

    with psycopg.connect(scratch_dsn) as conn:
        run = run_invariants(conn, run_id="t7-invariants")
        persist_run(conn, run)
        conn.commit()

    _assert_store_matches_golden(run)

    # The identity layer (so the three identity signals have something to read) AND
    # the lineage history (so SS7's A -> B -> A scan has three generations to scan).
    from recon.resolve import materialize

    materialized = materialize(generation=3, lineage_generations=INGESTED_GENERATIONS)
    assert materialized.candidates > 0, (
        "entity_link_candidates is empty, so every identity signal would score 0 "
        "and the three heaviest weights in the model would be untested"
    )

    with psycopg.connect(scratch_dsn) as conn:
        after = conn.execute("SELECT count(*) FROM conflicts").fetchone()[0]
        generations = conn.execute(
            "SELECT count(DISTINCT generation) FROM field_lineage"
        ).fetchone()[0]
    assert after == len(run.conflicts), (
        "materialization changed the conflict store; the counts these tests assert "
        f"would no longer be the graded ones ({len(run.conflicts)} -> {after})"
    )
    # Without this the oscillation suite silently degrades to "scanned nothing".
    assert generations == len(INGESTED_GENERATIONS), (
        f"field_lineage covers {generations} generation(s), not "
        f"{len(INGESTED_GENERATIONS)}. SS7's A -> B -> A window needs three, so "
        "every oscillation assertion below would be vacuous"
    )

    from recon.invariants.oscillation import scan_field_lineage

    with psycopg.connect(scratch_dsn) as conn:
        scan = scan_field_lineage(conn)
    assert len(scan.pairs) == GOLDEN_OSCILLATING, (
        "the committed A -> B -> A scan found "
        f"{len(scan.pairs)} oscillating pairs over real lineage, not golden's "
        f"{GOLDEN_OSCILLATING}. R16's oscillation half would be testing nothing."
    )
    return scratch_dsn


def _assert_store_matches_golden(run: object) -> None:
    """Fail the fixture unless the store is the GRADED conflict set.

    Not paranoia -- this caught a real one. An earlier run of this suite built a
    store holding 2,948 conflicts with **zero C4** instead of the committed
    3,050 with 250. Every downstream assertion still passed except the one that
    counted C4 proposals, because "every C4 is held" is trivially true of no
    C4s. A fixture that can silently produce a weaker world makes every count in
    this file unfalsifiable, so the fixture asserts its own world first and the
    whole suite errors out loudly if the store is not the graded one.
    """
    import json
    from collections import Counter
    from pathlib import Path

    golden = json.loads(
        (Path(__file__).resolve().parents[3] / "golden" / "conflicts.json").read_text()
    )
    expected = dict(sorted(Counter(entry["type"] for entry in golden).items()))
    detected = run.by_type()  # type: ignore[attr-defined]
    assert detected == expected, (
        "the conflict store is not the graded set, so every proposal count in this "
        "suite would be counting the wrong world.\n"
        f"  detected: {detected}\n"
        f"  golden  : {expected}\n"
        f"  run status={run.status!r} incomplete={run.incomplete!r}"  # type: ignore[attr-defined]
    )


@pytest.fixture
def writer(conflict_store: str) -> Iterator[Connection]:
    """A ``recon_writer`` connection whose transaction is ROLLED BACK at the end.

    Two properties, both load-bearing:

    * it authenticates **as** ``recon_writer``, so the grants and the KS00x
      triggers are the ones actually judging these writes. Running as the schema
      owner would leave the boundary untested while every assertion still passed;
    * ``commit=False`` means every test gets the same pristine conflict store,
      and a test that writes 2,600 proposals leaves none behind.
    """
    from recon.db import ROLE_RECON_WRITER, role_connection

    with role_connection(ROLE_RECON_WRITER, commit=False) as conn:
        yield conn
