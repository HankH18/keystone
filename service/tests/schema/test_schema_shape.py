"""The schema actually contains what DESIGN and the invariant contract pin."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

EXPECTED_TABLES = {
    "raw_records",
    "ingest_runs",
    "stg_crm_contact",
    "stg_crm_deal",
    "stg_student",
    "stg_enrollment",
    "stg_payment",
    "entities",
    "entity_links",
    "entity_link_candidates",
    "field_lineage",
    "invariant_results",
    "conflicts",
    "proposals",
    "proposal_events",
    "budget_ledger",
    "budget_reservations",
    "audit_log",
    "api_clients",
    "incidents",
    "conflict_incidents",
}

STAGING_NATURAL_KEY = {
    "stg_crm_contact": "crm_id",
    "stg_crm_deal": "deal_id",
    "stg_student": "student_id",
    "stg_enrollment": "enrollment_id",
    "stg_payment": "payment_id",
}

#: Normalized join keys the ER cascade blocks on, per docs/invariant-contract.md §4.
STAGING_NORMALIZED_INDEXES = {
    "stg_crm_contact": [
        ["generation", "email_norm"],
        ["generation", "first_norm", "last_norm", "dob_norm"],
        ["generation", "external_id"],
    ],
    "stg_student": [
        ["generation", "email_norm"],
        ["generation", "first_norm", "last_norm", "dob_norm"],
        ["generation", "student_number"],
    ],
    "stg_payment": [["generation", "email_norm"], ["generation", "external_ref"]],
    "stg_enrollment": [["generation", "student_id"], ["generation", "crm_deal_id"]],
    "stg_crm_deal": [["generation", "stage_funnel"]],
}


def _index_columns(engine: Engine, table: str) -> list[list[str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT i.relname,
                       array_agg(a.attname ORDER BY k.ord) AS cols
                FROM pg_index x
                JOIN pg_class t ON t.oid = x.indrelid
                JOIN pg_class i ON i.oid = x.indexrelid
                JOIN LATERAL unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
                WHERE t.relname = :table
                GROUP BY i.relname
                """
            ),
            {"table": table},
        ).all()
    return [list(cols) for _, cols in rows]


def test_every_designed_table_exists(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        present = {
            name
            for (name,) in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).all()
        }
    assert present >= EXPECTED_TABLES, f"missing tables: {sorted(EXPECTED_TABLES - present)}"


def test_required_extensions_are_installed(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        extensions = {
            name for (name,) in conn.execute(text("SELECT extname FROM pg_extension")).all()
        }
    assert {"vector", "pgcrypto"} <= extensions, extensions


def test_proposal_status_enum_matches_design(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        labels = (
            conn.execute(
                text(
                    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'proposal_status' ORDER BY e.enumsortorder"
                )
            )
            .scalars()
            .all()
        )
    assert labels == [
        "pending",
        "approved",
        "rejected",
        "applied",
        "rolled_back",
        "sensitive_hold",
    ]


def test_conflicts_fingerprint_has_a_unique_constraint(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        constraints = (
            conn.execute(
                text(
                    "SELECT conname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                    "WHERE t.relname = 'conflicts' AND c.contype = 'u'"
                )
            )
            .scalars()
            .all()
        )
    assert "uq_conflicts_fingerprint" in constraints


@pytest.mark.parametrize(("table", "key"), sorted(STAGING_NATURAL_KEY.items()))
def test_staging_is_indexed_on_generation_and_natural_key(
    owner_engine: Engine, table: str, key: str
) -> None:
    assert ["generation", key] in _index_columns(owner_engine, table)


@pytest.mark.parametrize(("table", "expected"), sorted(STAGING_NORMALIZED_INDEXES.items()))
def test_staging_is_indexed_on_the_normalized_join_keys(
    owner_engine: Engine, table: str, expected: list[list[str]]
) -> None:
    present = _index_columns(owner_engine, table)
    for columns in expected:
        assert columns in present, f"{table} missing index on {columns}; has {present}"


@pytest.mark.parametrize("table", sorted(STAGING_NATURAL_KEY))
def test_staging_allows_duplicate_natural_keys(owner_engine: Engine, table: str) -> None:
    """C11 is "the same payment_id twice in a generation": duplicates must be
    representable, so the natural-key index must NOT be unique."""
    with owner_engine.connect() as conn:
        unique = conn.execute(
            text(
                """
                SELECT bool_or(x.indisunique)
                FROM pg_index x
                JOIN pg_class t ON t.oid = x.indrelid
                JOIN pg_class i ON i.oid = x.indexrelid
                WHERE t.relname = :table AND i.relname LIKE '%_natural'
                """
            ),
            {"table": table},
        ).scalar()
    assert unique is False, f"{table} natural-key index is unique; duplicates unrepresentable"


def test_link_candidates_retain_discarded_resolutions(owner_engine: Engine) -> None:
    """R-010 reads the rows the cascade threw away, so `accepted` must exist and
    the discarded rows must be indexed."""
    with owner_engine.connect() as conn:
        columns = (
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'entity_link_candidates'"
                )
            )
            .scalars()
            .all()
        )
    assert {"source_ref", "key_class", "resolved_ref", "generation", "accepted"} <= set(columns)
    assert ["generation", "source_ref", "key_class"] in _index_columns(
        owner_engine, "entity_link_candidates"
    )


def test_ingest_runs_records_per_source_completeness(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        columns = (
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'ingest_runs'"
                )
            )
            .scalars()
            .all()
        )
    assert {
        "run_id",
        "source_id",
        "generation",
        "status",
        "started_at",
        "finished_at",
        "records_ok",
        "records_rejected",
        "error_detail",
    } <= set(columns)
