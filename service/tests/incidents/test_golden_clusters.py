"""End to end over the committed grading contract: 3,050 real conflicts, clustered.

Live database. The conflicts are the committed `golden/conflicts.json` rows,
fingerprinted with `recon.reference.fingerprint`, so this is the same population
every other gate runs against -- not a fixture shaped to make clustering look
good.

The writes go through `recon_writer` (`recon.incidents.cluster_conflicts` uses
`recon.db.role_connection`), so migration 0002's INSERT grants on `incidents`
and `conflict_incidents` are exercised for real. A test that wrote as the owner
would bypass the grants it is meant to depend on.
"""

from __future__ import annotations

import collections

from sqlalchemy import Engine, text

from recon.budget import PriceTable
from recon.incidents import (
    DEFAULT_THRESHOLD,
    MOCK_EMBEDDING_MODEL,
    IncidentRun,
    MockEmbeddingProvider,
    cluster_conflicts,
    load_conflicts,
)
from tests.incidents.conftest import ScopeFactory, run_id_for


def _cluster(scope: str, table: PriceTable) -> IncidentRun:
    """One clustering pass against the ledger row `scope` names.

    Every pass needs its **own** scope, because the reservation idempotency key
    is `embed:<run_id>:<batch>:<digest>` and the run scope is `run:<run_id>`:
    re-running with the same run id over the same conflicts is a *replay*, which
    the ledger refuses on purpose, and inventing a suffixed run id would name a
    scope with no ledger row at all. `make_ledger_scope` provisions each one.
    """
    return cluster_conflicts(
        run_id=run_id_for(scope),
        provider=MockEmbeddingProvider(),
        table=table,
    )


def test_the_golden_set_clusters_into_incidents_and_they_are_written(
    golden_conflict_ids: list[int],
    ledger_scope: str,
    embedding_prices: PriceTable,
    owner_engine: Engine,
) -> None:
    """3,050 conflicts become a handful of incidents, and the rows are really there.

    Every assertion below is about the database after the call, not about the
    return value, because the return value is what the code believes and the
    rows are what the service will serve.
    """
    run = cluster_conflicts(
        run_id=run_id_for(ledger_scope),
        provider=MockEmbeddingProvider(),
        table=embedding_prices,
    )
    assert run.conflicts == len(golden_conflict_ids) == 3050
    assert run.incidents > 14, (
        "fewer incidents than there are conflict types would mean the clustering is "
        "coarser than a GROUP BY type"
    )
    assert run.model == MOCK_EMBEDDING_MODEL
    assert run.threshold == DEFAULT_THRESHOLD

    with owner_engine.connect() as conn:
        written = conn.execute(
            text(
                "SELECT count(*) FROM incidents WHERE id = ANY(:ids)",
            ),
            {"ids": list(run.incident_ids)},
        ).scalar_one()
        members = conn.execute(
            text(
                "SELECT count(*) AS n, count(DISTINCT conflict_id) AS distinct_conflicts "
                "  FROM conflict_incidents WHERE incident_id = ANY(:ids)"
            ),
            {"ids": list(run.incident_ids)},
        ).one()
        centroids = conn.execute(
            text(
                "SELECT count(*) FROM incidents "
                " WHERE id = ANY(:ids) AND centroid IS NOT NULL AND embedding_dim = 256"
            ),
            {"ids": list(run.incident_ids)},
        ).scalar_one()

    assert written == run.incidents
    assert centroids == run.incidents, "a pgvector centroid is missing"
    # A partition: every conflict is a member of exactly one incident.
    assert members.n == 3050
    assert members.distinct_conflicts == 3050


def test_two_runs_over_the_same_conflicts_produce_identical_clusters(
    golden_conflict_ids: list[int],
    make_ledger_scope: ScopeFactory,
    embedding_prices: PriceTable,
) -> None:
    """**The graded determinism property.** Same conflicts in, same incidents out.

    Compared on labels and sizes, in order, because those are what the API
    serves and what a dashboard would render. The incident *ids* differ -- they
    are database identities allocated per run, and `incidents` is append-only to
    `recon_writer` (no DELETE grant), so runs accumulate rather than replacing
    one another.
    """
    first = _cluster(make_ledger_scope(), embedding_prices)
    second = _cluster(make_ledger_scope(), embedding_prices)
    assert first.labels == second.labels
    assert first.sizes == second.sizes
    assert first.incident_ids != second.incident_ids


def test_the_incident_labels_are_unique_and_carry_no_value(
    golden_conflict_ids: list[int],
    ledger_scope: str,
    embedding_prices: PriceTable,
) -> None:
    """Two incidents that read identically are two incidents a reviewer confuses.

    The ordinal in `label_for` is what separates the NINE distinct grade
    mismatches the golden set contains under one base label (re-measured
    2026-08-24; the docstring said eight).
    """
    run = _cluster(ledger_scope, embedding_prices)
    labels = list(run.labels)
    assert len(set(labels)) == len(labels)
    for label in labels:
        assert "@" not in label
        assert "pii" not in label


def test_the_clustering_is_finer_than_group_by_type_on_the_real_data(
    golden_conflict_ids: list[int],
    ledger_scope: str,
    embedding_prices: PriceTable,
    owner_engine: Engine,
) -> None:
    """The honest claim, measured against the rows in the database.

    Every incident is single-type (the clustering **refines** `GROUP BY type`
    and never merges types), and at least one conflict type is split into
    several incidents (so it is strictly finer than that grouping, rather than
    equal to it). Both halves are asserted because only the pair distinguishes
    "semantic refinement" from "a `GROUP BY` with extra steps".
    """
    run = _cluster(ledger_scope, embedding_prices)
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT ci.incident_id, c.type "
                "  FROM conflict_incidents ci JOIN conflicts c ON c.id = ci.conflict_id "
                " WHERE ci.incident_id = ANY(:ids)"
            ),
            {"ids": list(run.incident_ids)},
        ).fetchall()

    per_incident: dict[int, set[str]] = collections.defaultdict(set)
    per_type: dict[str, set[int]] = collections.defaultdict(set)
    for row in rows:
        per_incident[int(row.incident_id)].add(row.type)
        per_type[row.type].add(int(row.incident_id))

    mixed = {incident: types for incident, types in per_incident.items() if len(types) > 1}
    assert not mixed, f"clusters spanning several conflict types: {mixed}"
    split = {name: len(ids) for name, ids in per_type.items() if len(ids) > 1}
    assert len(split) >= 4, (
        "at most three conflict types were split into several incidents, so this is "
        f"barely more than GROUP BY type: {split}"
    )


def test_clustering_an_empty_conflict_table_is_an_empty_run(
    ledger_scope: str, embedding_prices: PriceTable, owner_engine: Engine
) -> None:
    """No conflicts is a fact, not a fault -- and it must not reserve any money.

    Deliberately does NOT depend on `golden_conflict_ids`, so it runs against a
    conflicts table this package has not filled. A service that has detected
    nothing has no incidents.
    """
    with owner_engine.connect() as conn:
        if conn.execute(text("SELECT count(*) FROM conflicts")).scalar_one():
            # Another test in this session committed the golden rows; the
            # status filter gives the same empty population without deleting
            # anything another suite may be reading.
            conflicts = load_conflicts(conn, status="dismissed")
            assert conflicts == ()

    run = cluster_conflicts(
        run_id=run_id_for(ledger_scope),
        status="dismissed",
        provider=MockEmbeddingProvider(),
        table=embedding_prices,
    )
    assert run.conflicts == 0
    assert run.incident_ids == ()
