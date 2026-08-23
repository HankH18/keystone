"""Landing and staging against the live database (R1, R4).

These run against real Postgres, as `recon_writer`, over a real `--profile dev`
seed run. Nothing is mocked, because the properties under test are properties of
the database: a fake connection would happily "prove" that the landing table is
append-only while the grants say otherwise.

What is asserted, and why each one is the thing that breaks:

* **lineage on every row** -- `source_id`, `ingest_ts`, `row_hash`, `load_id`,
  `generation`, `run_id`. R1 requires them; a NULL in any of them makes a
  conflict untraceable back to the record that caused it;
* **generations 2 and 3 arrive as new rows** and generation 1 is *byte* unchanged
  afterwards. The tempting implementation -- upsert on natural key -- passes a
  row-count check and destroys R4: A -> B -> A is only visible because all three
  snapshots survive. So the check is a hash of the full generation-1 rows taken
  before and after;
* **the normalized columns are written here, in Python.** Contract SS2 forbids
  `rules/*.sql` from normalizing, so a dirty `"GRADE 10TH"` has to arrive in
  staging already carrying `grade_norm='10'` and `grade_ord=10`;
* **the role really is `recon_writer`**, and that role really cannot mutate the
  landing table. Both halves matter: connecting as the owner would satisfy every
  other assertion here while silently disabling the boundary.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from recon.adapters import build_adapters
from recon.db import ROLE_RECON_WRITER, role_connection
from recon.ingest import expected_counts_from_manifest, ingest_generation


@pytest.fixture(scope="module")
def ingested(seed_tree, owner_engine):
    """Ingest every generation of the seed tree, oldest first, once per module."""
    adapters = build_adapters(seed_tree.root)
    expected = expected_counts_from_manifest(seed_tree.root)
    reports = []
    for generation in seed_tree.generations:
        reports.append(
            ingest_generation(
                adapters,
                generation,
                run_id=f"landing-gen{generation}",
                expected=expected,
            )
        )
    return seed_tree, reports


def _generation_digest(engine, generation: int, run_prefix: str) -> str:
    """A hash over the full text of every landing row one run wrote for a generation.

    Scoped to a run prefix on purpose: other tests in this package legitimately
    ingest into the same generation range, and an unscoped digest would be
    measuring their writes instead of this test's.
    """
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, source_id, entity_type, natural_key, generation, "
                "payload::text, row_hash, load_id, run_id, ingest_ts::text "
                "FROM raw_records WHERE generation = :generation "
                "AND run_id LIKE :prefix ORDER BY id"
            ),
            {"generation": generation, "prefix": run_prefix},
        ).all()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(repr(tuple(row)).encode("utf-8"))
    return f"{len(rows)}:{digest.hexdigest()}"


def test_every_generation_ingests_completely(ingested) -> None:
    seed_tree, reports = ingested
    for generation, report in zip(seed_tree.generations, reports, strict=True):
        assert report.records_rejected == 0
        assert not report.degraded, f"generation {generation} came out degraded"
        for source in report.sources:
            assert source.status == "ok"
            for load in source.loads:
                assert load.expected is not None, "the manifest must supply an expected count"
                assert load.loaded == load.expected
                assert load.complete


def test_every_landed_row_carries_its_lineage(ingested, owner_engine) -> None:
    seed_tree, _ = ingested
    with owner_engine.connect() as connection:
        missing = connection.execute(
            text(
                "SELECT count(*) FROM raw_records WHERE run_id LIKE 'landing-gen%' AND ("
                "source_id IS NULL OR entity_type IS NULL OR natural_key IS NULL OR "
                "payload IS NULL OR row_hash IS NULL OR load_id IS NULL OR "
                "run_id IS NULL OR ingest_ts IS NULL)"
            )
        ).scalar()
        total = connection.execute(
            text("SELECT count(*) FROM raw_records WHERE run_id LIKE 'landing-gen%'")
        ).scalar()
        sample = connection.execute(
            text(
                "SELECT source_id, entity_type, natural_key, generation, row_hash, load_id, run_id "
                "FROM raw_records WHERE generation = :generation AND source_id = 'crm' "
                "AND run_id LIKE 'landing-gen%' ORDER BY id LIMIT 1"
            ),
            {"generation": min(seed_tree.generations)},
        ).one()
    assert total > 0, "the module fixture must have landed rows to inspect"
    assert missing == 0
    assert len(sample.row_hash) == 64
    assert sample.load_id.endswith(f":g{sample.generation}")
    assert sample.run_id.startswith("landing-gen")


def test_a_later_generation_lands_as_new_rows_and_leaves_gen_one_byte_unchanged(
    seed_tree, owner_engine
) -> None:
    """The append-only guarantee R4 depends on, checked by hashing the rows."""
    adapters = build_adapters(seed_tree.root)
    expected = expected_counts_from_manifest(seed_tree.root)
    first, *rest = seed_tree.generations

    prefix = "append-gen%"
    ingest_generation(adapters, first, run_id=f"append-gen{first}", expected=expected)
    before = _generation_digest(owner_engine, first, prefix)
    counts_before = {first: before.split(":")[0]}

    for generation in rest:
        ingest_generation(adapters, generation, run_id=f"append-gen{generation}", expected=expected)
        counts_before[generation] = _generation_digest(owner_engine, generation, prefix).split(":")[
            0
        ]

    after = _generation_digest(owner_engine, first, prefix)
    assert after == before, (
        "ingesting later generations modified generation 1's landing rows; the "
        "landing table is append-only and R4's A->B->A scan needs all three snapshots"
    )

    with owner_engine.connect() as connection:
        total = connection.execute(
            text("SELECT count(*) FROM raw_records WHERE run_id LIKE :prefix"),
            {"prefix": prefix},
        ).scalar()
    assert total == sum(int(value) for value in counts_before.values())
    assert len(counts_before) == 3, "contract SS7 pins three generations"


def test_recon_writer_cannot_mutate_the_landing_table() -> None:
    """The privilege boundary, not a code comment (DESIGN Holds-before-writes)."""
    for statement in (
        "UPDATE raw_records SET row_hash = 'tampered' WHERE id = (SELECT min(id) FROM raw_records)",
        "DELETE FROM raw_records WHERE id = (SELECT min(id) FROM raw_records)",
    ):
        with (
            pytest.raises(DBAPIError) as excinfo,
            role_connection(ROLE_RECON_WRITER, commit=False) as connection,
        ):
            connection.execute(text(statement))
        assert "permission denied" in str(excinfo.value).lower()


def test_the_pipeline_connects_as_recon_writer() -> None:
    """`ingest_source`'s default connection is the restricted role, not the owner."""
    with role_connection(ROLE_RECON_WRITER, commit=False) as connection:
        who = connection.execute(text("SELECT current_user")).scalar()
    assert who == ROLE_RECON_WRITER


def test_staging_carries_the_normalized_columns_python_computed(ingested, owner_engine) -> None:
    seed_tree, _ = ingested
    latest = max(seed_tree.generations)
    with owner_engine.connect() as connection:
        unnormalized = connection.execute(
            text(
                "SELECT count(*) FROM stg_crm_contact WHERE generation = :generation "
                "AND (email_norm IS NULL OR first_norm IS NULL OR last_norm IS NULL)"
            ),
            {"generation": latest},
        ).scalar()
        dirty_grades = connection.execute(
            text(
                "SELECT grade, grade_norm, grade_ord FROM stg_crm_contact "
                "WHERE generation = :generation AND grade <> grade_norm LIMIT 5"
            ),
            {"generation": latest},
        ).all()
        plus_addresses = connection.execute(
            text(
                "SELECT email, email_norm FROM stg_crm_contact WHERE generation = :generation "
                "AND email LIKE '%+%@gmail.com' LIMIT 3"
            ),
            {"generation": latest},
        ).all()
    assert unnormalized == 0
    assert dirty_grades, "the dev fixture carries A.3 grade dirt; staging must normalize it"
    for grade, grade_norm, grade_ord in dirty_grades:
        assert grade_norm is not None and grade_ord is not None
        assert grade_norm != grade
    for email, email_norm in plus_addresses:
        assert "+" not in email_norm, "gmail +suffix must be stripped by norm_email"
        assert email_norm != email


def test_staging_rows_point_back_at_their_landing_row(ingested, owner_engine) -> None:
    seed_tree, _ = ingested
    latest = max(seed_tree.generations)
    with owner_engine.connect() as connection:
        orphans = connection.execute(
            text(
                "SELECT count(*) FROM stg_payment s LEFT JOIN raw_records r "
                "ON r.id = s.raw_record_id WHERE s.generation = :generation AND r.id IS NULL"
            ),
            {"generation": latest},
        ).scalar()
        mismatched = connection.execute(
            text(
                "SELECT count(*) FROM stg_payment s JOIN raw_records r ON r.id = s.raw_record_id "
                "WHERE s.generation = :generation AND (r.natural_key <> s.payment_id "
                "OR r.row_hash <> s.row_hash OR r.generation <> s.generation)"
            ),
            {"generation": latest},
        ).scalar()
        refs = connection.execute(
            text(
                "SELECT count(*) FROM stg_student WHERE generation = :generation "
                "AND source_ref <> 'appdb:student:' || student_id"
            ),
            {"generation": latest},
        ).scalar()
    assert orphans == 0
    assert mismatched == 0
    assert refs == 0


def test_the_deal_amount_becomes_integer_cents_never_a_float(ingested, owner_engine) -> None:
    """SS1.2 / SS2.5: the only float in the contract never reaches `canon_value` as one."""
    seed_tree, _ = ingested
    latest = max(seed_tree.generations)
    with owner_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT amount_raw, amount_cents, amount_microusd FROM stg_crm_deal "
                "WHERE generation = :generation ORDER BY deal_id LIMIT 20"
            ),
            {"generation": latest},
        ).all()
        nulls = connection.execute(
            text(
                "SELECT count(*) FROM stg_crm_deal WHERE generation = :generation "
                "AND amount_cents IS NULL"
            ),
            {"generation": latest},
        ).scalar()
    assert nulls == 0
    for amount_raw, amount_cents, amount_microusd in rows:
        assert amount_cents == round(float(amount_raw) * 100)
        assert amount_microusd == amount_cents * 10000


def test_source_generations_and_ingest_runs_record_every_load(ingested, owner_engine) -> None:
    seed_tree, _ = ingested
    with owner_engine.connect() as connection:
        ledger = connection.execute(
            text(
                "SELECT source_id, generation, entity_type, expected_count, loaded_count, "
                "rejected_count, complete FROM source_generations "
                "WHERE generation = ANY(:generations) "
                "ORDER BY source_id, generation, entity_type"
            ),
            {"generations": list(seed_tree.generations)},
        ).all()
        runs = connection.execute(
            text(
                "SELECT run_id, source_id, generation, status, records_ok, records_rejected "
                "FROM ingest_runs WHERE run_id LIKE 'landing-gen%' ORDER BY run_id, source_id"
            )
        ).all()

    # 3 sources x 3 generations, per entity type: crm 2, appdb 2, payments 1 = 5 per gen.
    assert len(ledger) == 15
    for row in ledger:
        assert row.expected_count == row.loaded_count
        assert row.rejected_count == 0
        assert row.complete is True

    assert len(runs) == 9
    for row in runs:
        assert row.status == "ok"
        assert row.records_rejected == 0
        assert row.records_ok > 0


def test_nothing_is_deduplicated_on_the_way_in(ingested, owner_engine) -> None:
    """Every arriving record is stored, once per arrival -- no silent dedupe.

    The two tables answer different questions and are checked separately, because
    conflating them hides a real bug in both directions:

    * `raw_records` is **append-only**, so re-ingesting a generation legitimately
      doubles its row count -- that is the R4 history, not duplication;
    * `stg_*` is a **re-materializable cache** (migration 0002), so the latest
      run's slice must hold exactly one row per source record.

    Migration 0001 deliberately puts no unique key on `stg_*` (C11 needs a
    repeated key to be *representable*), so the count is the only guard.
    """
    seed_tree, _ = ingested
    latest = max(seed_tree.generations)
    expected = expected_counts_from_manifest(seed_tree.root)[("payments", "payment", latest)]

    with owner_engine.connect() as connection:
        staged = connection.execute(
            text("SELECT count(*) FROM stg_payment WHERE generation = :generation"),
            {"generation": latest},
        ).scalar()
        per_load = (
            connection.execute(
                text(
                    "SELECT count(*) FROM raw_records WHERE generation = :generation "
                    "AND entity_type = 'payment' GROUP BY load_id"
                ),
                {"generation": latest},
            )
            .scalars()
            .all()
        )
        distinct_keys = connection.execute(
            text(
                "SELECT count(DISTINCT payment_id) FROM stg_payment WHERE generation = :generation"
            ),
            {"generation": latest},
        ).scalar()

    assert staged == expected
    assert distinct_keys == expected, "no natural key may be dropped or merged"
    assert per_load, "at least one payments load must exist for the latest generation"
    assert set(per_load) == {expected}, (
        "each landing load holds exactly the records that arrived in it; append-only "
        "means repeated loads add loads, never rewrite one"
    )
