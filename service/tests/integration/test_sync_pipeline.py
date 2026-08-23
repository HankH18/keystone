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
    assert result["stages"] == ["ingest", "materialize"], result
    assert result["ingest"]["generations"] == [1, 2, 3], (
        "R4 needs all three snapshots landed: field_lineage's A->B->A history is "
        "read across generations"
    )
    assert result["ingest"]["degraded"] is False
    assert result["materialize"]["persisted"] is True


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
    """
    before = {table: table_count(reader, table) for table in ("raw_records", "stg_crm_contact")}

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
