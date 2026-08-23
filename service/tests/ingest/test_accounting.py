"""The accounting invariant: a record cannot be lost without the count noticing.

    for every (source, generation, entity_type):
        records_read == records_landed + records_rejected

That equation is enforced in code (`LoadResult.check`, `_check_source_accounting`)
and asserted here against the real database, because two silent skips got past
review by making one of the three numbers a *derivation* of another instead of a
measurement:

* `LoadResult.loaded` was `len(records)` unconditionally while the landing write
  was gated on `(source_id, entity_type) in STAGING`. Any pair outside those five
  reported a full, `complete` generation over **zero landed rows** -- and SS5.3
  lets every absence rule run against a generation marked complete, so one such
  source fabricates thousands of conflicts the golden set does not contain;
* records whose `entity_type` was not in the adapter's declared list were dropped
  entirely: not landed, not in `records_ok`, not in `records_rejected`, not
  logged, while the source still reported `status=ok, complete=true`.

Both are the same bug, so both get the same fix: `loaded` is now what the landing
COPY actually wrote (read back from `raw_records` by `load_id`), `read` is counted
in the read loop itself, and the invariant is checked before anything is reported.
The tests below therefore assert the *property*, not the two call sites -- a third
code path that drops a record fails them too.

The reconciliation test corrupts records at known offsets in **every** file of a
real seed tree and balances the arithmetic per file, including the physical line
numbers the rejections report.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import text

from recon.adapters import FaultInjectingAdapter, build_adapters, stub_records
from recon.db import ROLE_RECON_WRITER, role_connection
from recon.ingest import (
    ACCOUNTING_INVARIANT,
    IngestAccountingError,
    LoadResult,
    expected_counts_from_manifest,
    ingest_generation,
    ingest_source,
)

#: 1-based physical line numbers corrupted in every snapshot file.
CORRUPT_AT: tuple[int, ...] = (1, 4)

#: The generation the corrupted copy is relabelled onto: inside the range this
#: package owns (>= 900, so teardown removes it) and outside the range the seed
#: tree uses, so a deliberately broken load cannot overwrite another module's
#: completeness ledger rows.
CORRUPT_GENERATION = 921


def _landed(engine, load_id: str) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT count(*) FROM raw_records WHERE load_id = :load_id"),
            {"load_id": load_id},
        ).scalar()


# ======================================================================================
# the invariant itself
# ======================================================================================


def test_the_invariant_is_stated_where_it_is_enforced() -> None:
    """The equation is one string, quoted by every violation message."""
    assert ACCOUNTING_INVARIANT == (
        "for every (source, generation, entity_type): "
        "records_read == records_landed + records_rejected"
    )


def test_a_load_that_under_reports_is_refused_rather_than_reported() -> None:
    """The guard is a raise, not a log line.

    A `LoadResult` that does not balance cannot be turned into a ledger row at
    all -- which is the difference between a sync that fails and a generation that
    lies about being complete.
    """
    honest = LoadResult(
        source_id="crm",
        entity_type="contact",
        generation=901,
        read=10,
        loaded=7,
        rejected=3,
        expected=10,
        complete=False,
    )
    honest.check()

    dropped = LoadResult(
        source_id="crm",
        entity_type="contact",
        generation=901,
        read=10,
        loaded=7,
        rejected=0,
        expected=10,
        complete=True,
    )
    with pytest.raises(IngestAccountingError) as excinfo:
        dropped.check()
    assert "unaccounted=3" in str(excinfo.value)
    assert ACCOUNTING_INVARIANT in str(excinfo.value)


def test_every_load_of_a_real_ingest_balances_against_the_database(seed_tree, owner_engine) -> None:
    """Not arithmetic on our own numbers: `loaded` is checked against `raw_records`."""
    adapters = build_adapters(seed_tree.root)
    expected = expected_counts_from_manifest(seed_tree.root)
    reports = [
        ingest_generation(
            adapters, generation, run_id=f"accounting-gen{generation}", expected=expected
        )
        for generation in seed_tree.generations
    ]

    checked = 0
    for generation, report in zip(seed_tree.generations, reports, strict=True):
        for source in report.sources:
            for load in source.loads:
                load.check()
                load_id = (
                    f"accounting-gen{generation}:{load.source_id}:{load.entity_type}:g{generation}"
                )
                assert load.loaded == _landed(owner_engine, load_id), (
                    f"{load.source_id}/{load.entity_type} generation {generation} reported "
                    f"{load.loaded} loaded; the landing table holds a different number"
                )
                checked += 1
    assert checked == 15, "3 sources x 3 generations x their entity types"


# ======================================================================================
# silent skip 1: a pair with no staging table
# ======================================================================================


def test_a_pair_with_no_staging_table_lands_and_is_reported_incomplete(owner_engine) -> None:
    """It may not report a clean, complete generation over zero materialized rows."""
    generation = 951
    records = stub_records(6, source_id="crm", entity_type="ghost", generation=generation)
    adapter = FaultInjectingAdapter(
        source_id="crm", mode="ok", records=records, available_generations=(generation,)
    )
    adapter.entity_types = ("ghost",)

    with role_connection(ROLE_RECON_WRITER) as connection:
        result = ingest_source(
            adapter,
            generation,
            run_id="unstaged-pair",
            conn=connection,
            stall_timeout=2.0,
            deadline_seconds=10.0,
        )

    (load,) = result.loads
    load.check()
    assert load.read == 6
    assert load.loaded == 6, "the records must be landed, not counted and discarded"
    assert load.rejected == 0
    assert load.staged is False
    assert load.complete is False, (
        "no stg_* table means no rule can ever see these rows; reporting the "
        "generation complete makes every absence rule fire against SS5.3"
    )
    assert result.status != "ok"

    assert _landed(owner_engine, "unstaged-pair:crm:ghost:g951") == 6
    with owner_engine.connect() as connection:
        ledger = connection.execute(
            text(
                "SELECT loaded_count, rejected_count, complete FROM source_generations "
                "WHERE source_id = 'crm' AND entity_type = 'ghost' AND generation = :generation"
            ),
            {"generation": generation},
        ).one()
    assert ledger.loaded_count == 6
    assert ledger.rejected_count == 0
    assert ledger.complete is False


# ======================================================================================
# silent skip 2: a record of an undeclared entity type
# ======================================================================================


def test_a_record_of_an_undeclared_entity_type_is_counted_not_dropped(owner_engine) -> None:
    """The type nobody declared still has to be accounted for, and reported loudly."""
    generation = 952
    declared = stub_records(5, source_id="crm", entity_type="contact", generation=generation)
    undeclared = stub_records(3, source_id="crm", entity_type="invoice", generation=generation)
    adapter = FaultInjectingAdapter(
        source_id="crm",
        mode="ok",
        records=(*declared, *undeclared),
        available_generations=(generation,),
    )
    adapter.entity_types = ("contact",)

    with role_connection(ROLE_RECON_WRITER) as connection:
        result = ingest_source(
            adapter,
            generation,
            run_id="undeclared-type",
            conn=connection,
            stall_timeout=2.0,
            deadline_seconds=10.0,
        )

    by_type = {load.entity_type: load for load in result.loads}
    assert set(by_type) == {"contact", "invoice"}, (
        "an undeclared entity type must still produce a load result; dropping it "
        "loses the records from every count at once"
    )
    assert by_type["contact"].loaded == 5
    assert by_type["invoice"].read == 3
    assert by_type["invoice"].loaded == 3
    assert by_type["invoice"].complete is False
    assert result.records_read == 8
    assert result.records_ok == 8
    assert result.status != "ok"
    assert result.complete is False

    assert _landed(owner_engine, "undeclared-type:crm:invoice:g952") == 3


def test_the_source_level_check_catches_a_whole_bucket_going_missing() -> None:
    """The per-load equation cannot see a load that was never built; this can.

    A missing bucket takes its own numbers with it, so the source-level check
    compares the sum of the per-type reads against the counter incremented in the
    read loop itself.
    """
    from recon.ingest import _check_source_accounting

    kept = LoadResult(
        source_id="crm",
        entity_type="contact",
        generation=952,
        read=5,
        loaded=5,
        rejected=0,
        expected=None,
        complete=True,
    )
    kept.check()  # balances on its own terms
    with pytest.raises(IngestAccountingError) as excinfo:
        _check_source_accounting("crm", 952, [kept], read_total=8, rejected_total=0)
    assert "3 record(s) were dropped" in str(excinfo.value)


# ======================================================================================
# reconciliation across every file, at known offsets
# ======================================================================================


@pytest.fixture
def corrupted_tree(seed_tree, tmp_path: Path) -> tuple[Path, int, dict[tuple[str, str], int]]:
    """A copy of the seed tree with `CORRUPT_AT` broken in every snapshot file.

    Returns the root, the generation that was corrupted, and the physical line
    count of each file, so the test can balance the arithmetic per file against
    what is on disk rather than against what the pipeline says it saw.

    The copy is **relabelled onto its own generation**. `source_generations` is
    keyed on `(source_id, generation, entity_type)`, so ingesting a deliberately
    broken snapshot under a generation another module ingests cleanly would
    overwrite that module's ledger rows with `complete = false` -- a test that
    breaks a different test's subject is worse than no test.
    """
    target = tmp_path / "corrupted"
    shutil.copytree(seed_tree.root, target)
    source_generation = min(seed_tree.generations)
    generation = CORRUPT_GENERATION
    line_counts: dict[tuple[str, str], int] = {}

    for source_dir in sorted(target.iterdir()):
        if not source_dir.is_dir():
            continue
        gen_dir = source_dir / f"gen{source_generation}"
        if not gen_dir.is_dir():
            continue
        gen_dir = gen_dir.rename(source_dir / f"gen{generation}")
        for path in sorted(gen_dir.glob("*.jsonl")):
            lines = path.read_text(encoding="utf-8").split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            assert len(lines) > max(CORRUPT_AT), f"{path} is too short to corrupt"
            for offset in CORRUPT_AT:
                lines[offset - 1] = '{"truncated": '
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            line_counts[(source_dir.name, path.stem)] = len(lines)

    return target, generation, line_counts


def test_corrupting_known_offsets_balances_per_file(corrupted_tree, owner_engine) -> None:
    """Per file: lines on disk == landed + rejected, and the offsets are named."""
    root, generation, line_counts = corrupted_tree
    run_id = "reconcile-corrupt"
    adapters = build_adapters(root)

    with role_connection(ROLE_RECON_WRITER) as connection:
        results = [
            ingest_source(
                adapter,
                generation,
                run_id=f"{run_id}-{source_id}",
                conn=connection,
                expected=expected_counts_from_manifest(root),
                stall_timeout=5.0,
                deadline_seconds=60.0,
            )
            for source_id, adapter in sorted(adapters.items())
        ]

    seen: set[tuple[str, str]] = set()
    for result in results:
        rejected_lines: dict[str, list[int]] = {}
        for rejection in result.rejections:
            rejected_lines.setdefault(rejection.entity_type or "unknown", []).append(
                rejection.line_no
            )

        for load in result.loads:
            key = (load.source_id, load.entity_type)
            seen.add(key)
            on_disk = line_counts[key]

            load.check()
            assert load.read == on_disk, (
                f"{key} has {on_disk} physical lines but the pipeline accounted for {load.read}"
            )
            assert load.rejected == len(CORRUPT_AT)
            assert load.loaded == on_disk - len(CORRUPT_AT)
            assert load.complete is False, "a load with rejections is not complete (SS5.3)"

            load_id = f"{run_id}-{load.source_id}:{load.source_id}:{load.entity_type}:g{generation}"
            assert _landed(owner_engine, load_id) == load.loaded

            assert sorted(rejected_lines[load.entity_type]) == sorted(CORRUPT_AT), (
                "a rejection must name the physical line it came from, or an "
                "operator is sent to the wrong line of the snapshot"
            )

    assert seen == set(line_counts), "every file in the tree must have been accounted for"
