"""`POST /internal/sync` ingests **and then materializes** (R1, R4, R10).

The defect this module exists for: `recon.resolve.materialize` had no caller
outside its own module. Ingestion was wired, the trigger was wired, and nothing
built the canonical layer -- so on the documented grader path `entities`,
`entity_links`, `entity_link_candidates` and `field_lineage` were all empty, the
unified query endpoint answered 404 for every key, and the identity layer existed
only inside `tests/er`'s fixture, which materialized by hand.

Everything here is driven through the real service: the trigger endpoint with the
real secret, the real adapters over the committed fixture tree, the real
`recon_writer` privilege boundary and the real deferred provenance triggers. No
fixture in this package writes a canonical row.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.integration.conftest import CANONICAL_TABLES, SYNC_SECRET, table_count


def test_sync_runs_the_handler_and_reports_both_stages(synced: dict[str, Any]) -> None:
    """The trigger is bound to a real pipeline, not to `"handler": "unbound"`."""
    assert synced["status"] == "started", synced
    assert synced["handler"] == "ran", (
        "the sync trigger reported "
        f"{synced.get('handler')!r}: `create_app()` must bind `sync_job` to the "
        "sync trigger, or POST /internal/sync authenticates and then does nothing"
    )
    result = synced["result"]
    assert result["stages"] == ["ingest", "materialize", "invariants"], result
    assert result["ingest"]["generations"] == [1, 2, 3], (
        "R4 needs all three snapshots landed: field_lineage's A->B->A history is "
        "read across generations"
    )
    assert result["ingest"]["degraded"] is False
    assert result["materialize"]["persisted"] is True
    assert result["invariants"]["status"] == "ok", result["invariants"]


@pytest.mark.parametrize("table", CANONICAL_TABLES)
def test_the_canonical_layer_is_populated(
    reader: Engine, synced: dict[str, Any], table: str
) -> None:
    """The assertion the whole ticket is about: all four tables have rows.

    Every one of them was empty after a sync before `sync_job` existed.
    """
    rows = table_count(reader, table)
    assert rows > 0, (
        f"{table} is empty after a real POST /internal/sync. The sync trigger must "
        "materialize the canonical layer after ingesting; without it the identity "
        "layer, the unified query endpoint and every downstream conflict exist only "
        "in test fixtures"
    )


def test_the_reported_counts_are_the_rows_that_are_actually_there(
    reader: Engine, synced: dict[str, Any]
) -> None:
    """The response is checked against the database, not trusted.

    A summary is a claim; these four numbers are the only thing that makes it a
    report. (`entity_links` and `entity_link_candidates` hold the current
    generation only; `field_lineage` holds 1-3, which is why it is not compared
    to a single generation's count.)
    """
    report = synced["result"]["materialize"]
    assert table_count(reader, "entities") == report["entities"]
    assert table_count(reader, "entity_links") == report["links"]
    assert table_count(reader, "entity_link_candidates") == report["candidates"]
    assert table_count(reader, "field_lineage") == report["lineage"]


def test_every_link_names_a_record_this_sync_actually_landed(reader: Engine, synced: dict) -> None:
    """Ingest-then-materialize, proven by the join rather than by the ordering.

    `KS009` already refuses a link that names no landed record, so this cannot
    fail while the trigger is deferred and the transaction commits -- which is the
    point: it states what that trigger buys, and it fails loudly if the constraint
    is ever dropped and the two stages are reordered.
    """
    orphans = text(
        """
        SELECT count(*) FROM entity_links el
         WHERE NOT EXISTS (
            SELECT 1 FROM raw_records rr
             WHERE rr.source_id = el.source_id
               AND rr.natural_key = el.source_key
               AND rr.generation = el.generation
         )
        """
    )
    with reader.connect() as conn:
        assert int(conn.execute(orphans).scalar_one()) == 0


# ======================================================================================
# R5 -- a completed sync runs the committed rule set
# ======================================================================================
def test_the_invariant_results_table_is_populated_by_the_sync(
    reader: Engine, synced: dict[str, Any]
) -> None:
    """R5, and the assertion the whole third stage exists for.

    `SYNC_STAGES` read `("ingest", "materialize")`, so a grader who followed the
    README -- migrate, `POST /internal/sync`, open the dashboard -- got a fully
    loaded canonical layer, **zero** `invariant_results`, **zero** `conflicts`
    and therefore zero proposals. The rule set ran only from
    `python -m recon.invariants --persist` and from the offline grading harness,
    neither of which is on the HTTP path.
    """
    stamped = table_count(reader, "invariant_results")
    assert stamped > 0, (
        "invariant_results is empty after a real POST /internal/sync. R5 requires a "
        "completed sync to run the committed, versioned rule set and record "
        "pass/fail per record in a queryable results table"
    )
    assert table_count(reader, "conflicts") > 0, (
        "conflicts is empty after a real sync, so the reviewer surface, the "
        "reconciler and the dashboard all have nothing to work from"
    )


def test_the_reported_invariant_summary_is_the_rows_that_are_actually_there(
    reader: Engine, synced: dict[str, Any]
) -> None:
    """The summary is a claim; these counts are what make it a report.

    Same discipline as the materialize stage above: every number the response
    prints is compared against the table it describes, for **this run id**, so a
    stage that reported plausible numbers without writing them would fail here.
    """
    reported = synced["result"]["invariants"]
    run_id = reported["run_id"]

    with reader.connect() as conn:
        stamped = int(
            conn.execute(
                text("SELECT count(*) FROM invariant_results WHERE run_id = :run"),
                {"run": run_id},
            ).scalar_one()
        )
        rules = int(
            conn.execute(
                text("SELECT count(DISTINCT rule_id) FROM invariant_results WHERE run_id = :run"),
                {"run": run_id},
            ).scalar_one()
        )
        first_seen = int(
            conn.execute(
                text("SELECT count(*) FROM conflicts WHERE first_seen_run = :run"),
                {"run": run_id},
            ).scalar_one()
        )

    assert run_id == "t14-integration-sync", (
        f"the invariant stage recorded run id {run_id!r}; it must be the trigger's "
        "run id, so invariant_results ties back to the audit_log trigger claim"
    )
    assert stamped == reported["results"]
    assert rules == reported["rules"]
    assert first_seen == reported["conflicts"]
    assert reported["oscillating"] > 0, (
        "no conflict was flagged oscillating, so R16's A->B->A half is not being "
        "exercised by the wired pipeline even though field_lineage covers 1-3"
    )


def test_every_stamped_record_carries_a_verdict_from_the_closed_set(
    reader: Engine, synced: dict[str, Any]
) -> None:
    """SS5.8's vocabulary, at the `pass`/`fail`/`unchecked` DB spelling."""
    with reader.connect() as conn:
        verdicts = {
            str(row[0])
            for row in conn.execute(text("SELECT DISTINCT verdict::text FROM invariant_results"))
        }
    assert verdicts, "no verdicts at all were recorded"
    assert verdicts <= {"pass", "fail", "unchecked"}, verdicts


def test_lineage_covers_all_three_generations(reader: Engine, synced: dict[str, Any]) -> None:
    """R4/R16: the history table is the one thing that is not current-state only."""
    with reader.connect() as conn:
        generations = [
            row[0]
            for row in conn.execute(
                text("SELECT DISTINCT generation FROM field_lineage ORDER BY generation")
            )
        ]
    assert generations == [1, 2, 3], (
        f"field_lineage holds generations {generations}; the A->B->A oscillation scan "
        "needs all three snapshots"
    )


# ======================================================================================
# re-firing the cron -- run last, because these two mutate the landed state
# ======================================================================================


def _sync(service: TestClient, run_id: str) -> dict[str, Any]:
    response = service.post(
        "/internal/sync", json={"run_id": run_id}, headers={"X-Trigger-Secret": SYNC_SECRET}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_re_firing_the_cron_lands_nothing_twice(
    service: TestClient, reader: Engine, synced: dict[str, Any]
) -> None:
    """A second sync is `already_current`, not a second copy of the landing table.

    `raw_records` is append-only, so a sync that re-lands a snapshot the database
    already holds doubles it -- 360,400 rows per firing, and an A->B->A history
    that now has every value twice. The completeness ledger
    (`source_generations`, migration 0009) is what makes "already landed"
    answerable, and this is the test that it is actually consulted.

    **Stage 3 still runs**, and that is deliberate rather than an oversight: R5 is
    about a *completed* sync, and "nothing new to land" is one. Re-detection is
    what advances `conflicts.last_seen_run`, so the conflict set is refreshed
    without being duplicated -- `persist_run`'s
    `ON CONFLICT (fingerprint) DO UPDATE` is what makes those two compatible.
    """
    before = {table: table_count(reader, table) for table in ("raw_records", "stg_crm_contact")}
    conflicts_before = table_count(reader, "conflicts")

    body = _sync(service, "t14-integration-sync-second")
    assert body["status"] == "started", body
    assert body["handler"] == "ran", body
    result = body["result"]
    assert result["ingest"]["generations"] == [], (
        f"a re-fired sync landed generations {result['ingest']['generations']}; "
        "every one of them is already complete in the ledger"
    )
    assert result["ingest"]["already_landed"] == [1, 2, 3], result
    assert result["materialize"] == {"already_current": True, "generation": 3}, result
    assert result["invariants"]["run_id"] == "t14-integration-sync-second", result["invariants"]
    assert result["invariants"]["conflicts"] == conflicts_before, (
        "re-detection on an unchanged database must find the same conflict set"
    )

    with reader.connect() as conn:
        last_seen = {
            str(row[0])
            for row in conn.execute(text("SELECT DISTINCT last_seen_run FROM conflicts"))
        }
    assert table_count(reader, "conflicts") == conflicts_before, (
        "the second detection pass duplicated conflicts instead of advancing them"
    )
    assert last_seen == {"t14-integration-sync-second"}, (
        f"conflicts carry last_seen_run {last_seen}; re-detection must advance every "
        "row it re-detects, which is the whole reason the second pass is worth running"
    )

    after = {table: table_count(reader, table) for table in ("raw_records", "stg_crm_contact")}
    assert after == before, f"a no-op sync still wrote rows: {before} -> {after}"


def test_a_sync_that_cannot_materialize_does_not_report_success(
    service: TestClient, reader: Engine, synced: dict[str, Any]
) -> None:
    """ "Ingested but not materialized" is a failed sync, and says so.

    Driven by a **real** refusal rather than a patched one. The setup is one
    honest edit to the completeness ledger -- one slice marked incomplete, as the
    schema owner -- which is exactly the state a truncated load leaves behind. The
    sync then re-lands that generation and finds the identity layer already there;
    `recon_writer` may append to the canonical tables and never update them
    (migration 0004), so the layer cannot be rebuilt to describe what just landed.

    Three things are asserted, and the last two matter as much as the first: the
    response does not say `"started"`, it names the stage, and the canonical layer
    is exactly the one the first sync built -- a failed run must not half-write it.
    """
    slice_key = {"source_id": "payments", "entity_type": "payment", "generation": 3}
    with reader.begin() as conn:
        conn.execute(
            text(
                "UPDATE source_generations SET complete = false "
                "WHERE source_id = :source_id AND entity_type = :entity_type "
                "AND generation = :generation"
            ),
            slice_key,
        )

    before = {table: table_count(reader, table) for table in CANONICAL_TABLES}
    try:
        body = _sync(service, "t14-integration-sync-stale")
    finally:
        with reader.begin() as conn:
            conn.execute(
                text(
                    "UPDATE source_generations SET complete = true "
                    "WHERE source_id = :source_id AND entity_type = :entity_type "
                    "AND generation = :generation"
                ),
                slice_key,
            )

    assert body["handler"] == "failed", body
    assert body["status"] == "failed", (
        f"a sync that could not materialize reported status {body['status']!r}. "
        "Ingesting without rebuilding the canonical layer is not a successful sync"
    )
    assert body.get("stage") == "materialize", body
    assert "append-only" in body["error"] or "stale" in body["error"], body

    after = {table: table_count(reader, table) for table in CANONICAL_TABLES}
    assert after == before, (
        f"the failed sync changed the canonical layer: {before} -> {after}. A run "
        "that cannot materialize must leave the previous layer exactly as it was"
    )
