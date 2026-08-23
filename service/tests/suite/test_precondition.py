"""The suite refuses to grade a database that does not hold the dataset.

Success criterion 1 is a statement about a 100k dataset. Run against an empty
schema the same code would find zero conflicts, diff them against a golden file
it never loaded rows for, and report "0 false negatives" -- a green produced by
absence. :func:`recon.suite.pipeline.assert_loaded` is what makes that outcome
impossible, so it is asserted here against a real, migrated, EMPTY database
rather than against a mock.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from recon.db import get_engine
from recon.suite.pipeline import GRADED_TABLES, PreconditionFailed, assert_loaded

MANIFEST = {
    "expected_counts": {
        "gen3": {"crm.contact": 40000, "appdb.student": 25000},
    }
}


@pytest.fixture
def fixtures_root(tmp_path: Path) -> Path:
    (tmp_path / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    return tmp_path


def test_an_empty_landing_table_is_refused(scratch_database: str, fixtures_root: Path) -> None:
    with get_engine().connect() as conn, pytest.raises(PreconditionFailed) as raised:
        assert_loaded(conn, fixtures_root)

    message = str(raised.value)
    assert "missing slices" in message
    assert "crm.contact@gen3" in message, message
    assert "/internal/sync" in message, "the row must say how to fix it"


def test_a_wrong_slice_count_is_refused(scratch_database: str, tmp_path: Path) -> None:
    """A source that timed out mid-load lands SOME rows. That is not loaded."""
    root = tmp_path / "fx"
    root.mkdir()
    root.joinpath("manifest.json").write_text(
        json.dumps({"expected_counts": {"gen3": {"crm.contact": 3}}}), encoding="utf-8"
    )
    engine = get_engine()
    with engine.begin() as conn:
        for index in range(2):  # two of the three expected rows
            conn.execute(
                text(
                    "INSERT INTO raw_records (source_id, entity_type, natural_key, "
                    "generation, payload, row_hash, load_id, run_id) VALUES "
                    "('crm', 'contact', :key, 3, '{}'::jsonb, :hash, 'l', 'r')"
                ),
                {"key": f"CRM-{index}", "hash": f"h{index}"},
            )
    try:
        with engine.connect() as conn, pytest.raises(PreconditionFailed) as raised:
            assert_loaded(conn, root)
        assert "wrong counts" in str(raised.value)
        assert "expected 3 landed 2" in str(raised.value)
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM raw_records WHERE run_id = 'r'"))


def test_a_missing_fixtures_manifest_is_refused(scratch_database: str, tmp_path: Path) -> None:
    with get_engine().connect() as conn, pytest.raises(PreconditionFailed) as raised:
        assert_loaded(conn, tmp_path)

    assert "no fixtures manifest" in str(raised.value)
    assert "recon.seed" in str(raised.value)


def test_the_graded_layer_excludes_the_mirror_and_the_identity_layer() -> None:
    """The reset must never be able to empty what the suite grades against.

    A truncation list is a one-line edit away from taking `raw_records` with it,
    and the consequence would not be a red row -- it would be a six-minute
    re-materialization and a precondition failure on the NEXT run.
    """
    forbidden = {
        "raw_records",
        "ingest_runs",
        "source_generations",
        "entities",
        "entity_links",
        "entity_link_candidates",
        "field_lineage",
        "api_clients",
        "budget_ledger",
    }

    assert not forbidden & set(GRADED_TABLES), sorted(forbidden & set(GRADED_TABLES))
