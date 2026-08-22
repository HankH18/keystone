"""Every index DESIGN and the migrations pin, asserted by name AND by columns.

The gap this closes: nothing asserted that the designed indexes exist, so
``DROP INDEX ix_field_lineage_scan`` -- the index the oscillation window scan is
built on -- left the whole suite green. An index is a performance contract, and
a performance contract nothing checks is a comment.

Two layers, deliberately:

* :data:`DESIGN_INDEXES` -- the indexes ``docs/DESIGN.md`` §"Data models" names
  *in prose*. These are requirements, quoted at each entry.
* :data:`MIGRATION_INDEXES` -- every remaining index the migrations create.
  These are the blocking/join keys the ER cascade and the invariant SQL depend
  on; dropping any of them is a silent performance regression.

Both are asserted by index **name** and by exact **column list in order**, so
neither renaming an index nor quietly reordering its columns can slip through.
Column order is the whole point of a composite index: ``(canonical_id, field,
generation)`` supports the oscillation scan and ``(generation, field,
canonical_id)`` does not, while both would satisfy a set comparison.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

#: name -> (table, ordered columns). Quoted requirement per entry.
DESIGN_INDEXES: dict[str, tuple[str, list[str]]] = {
    # "raw_records(...) -- append-only landing. Index (source_id, entity_type,
    #  natural_key, generation)."
    "ix_raw_records_source_key": (
        "raw_records",
        ["source_id", "entity_type", "natural_key", "generation"],
    ),
    # "field_lineage(...) -- index (canonical_id, field, generation);
    #  oscillation = window scan for value A,B,A."
    "ix_field_lineage_scan": ("field_lineage", ["canonical_id", "field", "generation"]),
    # "conflicts(id, fingerprint unique, ...)"
    "uq_conflicts_fingerprint": ("conflicts", ["fingerprint"]),
    # "entities(canonical_id uuid, ...)" -- the canonical primary key.
    "entities_pkey": ("entities", ["canonical_id"]),
    # "budget_ledger(scope pk, ...)"
    "budget_ledger_pkey": ("budget_ledger", ["scope"]),
    # "api_clients(key_hash, scope, label)" -- key_hash is the lookup key.
    "api_clients_pkey": ("api_clients", ["key_hash"]),
}

#: Everything else the migrations create, by name and ordered column list.
MIGRATION_INDEXES: dict[str, tuple[str, list[str]]] = {
    "ix_ingest_runs_source_generation": ("ingest_runs", ["source_id", "generation", "status"]),
    "pk_ingest_runs": ("ingest_runs", ["run_id", "source_id"]),
    "ix_raw_records_generation": ("raw_records", ["generation"]),
    "ix_raw_records_row_hash": ("raw_records", ["row_hash"]),
    "ix_stg_crm_contact_natural": ("stg_crm_contact", ["generation", "crm_id"]),
    "ix_stg_crm_contact_email": ("stg_crm_contact", ["generation", "email_norm"]),
    "ix_stg_crm_contact_namedob": (
        "stg_crm_contact",
        ["generation", "first_norm", "last_norm", "dob_norm"],
    ),
    "ix_stg_crm_contact_ext": ("stg_crm_contact", ["generation", "external_id"]),
    "ix_stg_crm_deal_natural": ("stg_crm_deal", ["generation", "deal_id"]),
    "ix_stg_crm_deal_stage": ("stg_crm_deal", ["generation", "stage_funnel"]),
    "ix_stg_crm_deal_contacts": ("stg_crm_deal", ["associated_contact_ids"]),
    "ix_stg_student_natural": ("stg_student", ["generation", "student_id"]),
    "ix_stg_student_email": ("stg_student", ["generation", "email_norm"]),
    "ix_stg_student_guardian2": ("stg_student", ["generation", "guardian2_email_norm"]),
    "ix_stg_student_namedob": (
        "stg_student",
        ["generation", "first_norm", "last_norm", "dob_norm"],
    ),
    "ix_stg_student_number": ("stg_student", ["generation", "student_number"]),
    "ix_stg_student_household": ("stg_student", ["generation", "household_id"]),
    "ix_stg_enrollment_natural": ("stg_enrollment", ["generation", "enrollment_id"]),
    "ix_stg_enrollment_student": ("stg_enrollment", ["generation", "student_id"]),
    "ix_stg_enrollment_deal": ("stg_enrollment", ["generation", "crm_deal_id"]),
    "ix_stg_enrollment_stage": ("stg_enrollment", ["generation", "stage_funnel"]),
    "ix_stg_payment_natural": ("stg_payment", ["generation", "payment_id"]),
    "ix_stg_payment_email": ("stg_payment", ["generation", "email_norm"]),
    "ix_stg_payment_ext": ("stg_payment", ["generation", "external_ref"]),
    "ix_stg_payment_dupe": (
        "stg_payment",
        ["generation", "email_norm", "amount_cents", "type_norm", "occurred_at"],
    ),
    "ix_entities_type": ("entities", ["entity_type"]),
    "ix_entity_links_canonical": ("entity_links", ["canonical_id", "generation"]),
    "ix_entity_links_ref": ("entity_links", ["generation", "source_ref"]),
    "uq_entity_links_source_generation": (
        "entity_links",
        ["generation", "source_id", "source_key"],
    ),
    "ix_link_candidates_source": ("entity_link_candidates", ["generation", "source_ref"]),
    "ix_link_candidates_resolved": (
        "entity_link_candidates",
        ["generation", "key_class", "resolved_ref"],
    ),
    "ix_link_candidates_accepted": (
        "entity_link_candidates",
        ["generation", "source_ref", "key_class"],
    ),
    "ix_invariant_results_run": ("invariant_results", ["run_id", "rule_id"]),
    "ix_invariant_results_ref": ("invariant_results", ["record_ref", "run_id"]),
    "ix_invariant_results_verdict": ("invariant_results", ["run_id", "verdict"]),
    "ix_conflicts_type_status": ("conflicts", ["type", "status"]),
    "ix_conflicts_last_seen": ("conflicts", ["last_seen_run"]),
    "uq_proposals_open_fingerprint": ("proposals", ["fingerprint"]),
    "ix_proposals_status": ("proposals", ["status", "created_at"]),
    "ix_proposals_conflict": ("proposals", ["conflict_id"]),
    "ix_proposal_events_proposal": ("proposal_events", ["proposal_id", "ts"]),
    "ix_proposal_events_txid": ("proposal_events", ["txid"]),
    # Added by 0004: the correlation lookup the entities trigger performs on
    # every canonical UPDATE.
    "ix_proposal_events_canonical": ("proposal_events", ["canonical_id", "txid"]),
    "ix_conflict_incidents_conflict": ("conflict_incidents", ["conflict_id"]),
    "pk_conflict_incidents": ("conflict_incidents", ["incident_id", "conflict_id"]),
}

ALL_INDEXES = {**DESIGN_INDEXES, **MIGRATION_INDEXES}

#: Indexes that must additionally be UNIQUE. A non-unique
#: `uq_conflicts_fingerprint` would not make re-detection idempotent.
UNIQUE_INDEXES = {
    "uq_conflicts_fingerprint",
    "uq_proposals_open_fingerprint",
    "uq_entity_links_source_generation",
    "entities_pkey",
    "budget_ledger_pkey",
    "api_clients_pkey",
    "pk_ingest_runs",
    "pk_conflict_incidents",
}

_INDEX_QUERY = text(
    """
    SELECT t.relname AS table_name,
           array_agg(a.attname ORDER BY k.ord) AS columns,
           x.indisunique AS is_unique
    FROM pg_index x
    JOIN pg_class i ON i.oid = x.indexrelid
    JOIN pg_class t ON t.oid = x.indrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = 'public'
    JOIN LATERAL unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
    WHERE i.relname = :name
    GROUP BY t.relname, x.indisunique
    """
)


@pytest.mark.parametrize(
    ("name", "table", "columns"),
    [(name, table, columns) for name, (table, columns) in sorted(DESIGN_INDEXES.items())],
    ids=sorted(DESIGN_INDEXES),
)
def test_every_index_design_names_exists_with_the_designed_columns(
    owner_engine: Engine, name: str, table: str, columns: list[str]
) -> None:
    """docs/DESIGN.md §"Data models" names these; the database must have them."""
    with owner_engine.connect() as conn:
        row = conn.execute(_INDEX_QUERY, {"name": name}).one_or_none()

    assert row is not None, (
        f"index {name} on {table} is missing. docs/DESIGN.md 'Data models' names it; "
        f"dropping it silently degrades the query it exists for."
    )
    assert row.table_name == table, f"{name} is on {row.table_name}, expected {table}"
    assert list(row.columns) == columns, (
        f"{name} indexes {list(row.columns)}, expected {columns} in that exact order"
    )


@pytest.mark.parametrize(
    ("name", "table", "columns"),
    [(name, table, columns) for name, (table, columns) in sorted(MIGRATION_INDEXES.items())],
    ids=sorted(MIGRATION_INDEXES),
)
def test_every_index_the_migrations_create_still_exists(
    owner_engine: Engine, name: str, table: str, columns: list[str]
) -> None:
    """The blocking and join keys the ER cascade and invariant SQL run on."""
    with owner_engine.connect() as conn:
        row = conn.execute(_INDEX_QUERY, {"name": name}).one_or_none()

    assert row is not None, f"index {name} on {table} is missing"
    assert row.table_name == table, f"{name} is on {row.table_name}, expected {table}"
    assert list(row.columns) == columns, (
        f"{name} indexes {list(row.columns)}, expected {columns} in that exact order"
    )


@pytest.mark.parametrize("name", sorted(UNIQUE_INDEXES))
def test_the_uniqueness_carrying_indexes_are_actually_unique(
    owner_engine: Engine, name: str
) -> None:
    """A "unique" index that is not unique enforces nothing."""
    with owner_engine.connect() as conn:
        row = conn.execute(_INDEX_QUERY, {"name": name}).one_or_none()
    assert row is not None, f"index {name} is missing"
    assert row.is_unique is True, f"{name} exists but is not unique"


def test_no_designed_index_was_dropped_wholesale(owner_engine: Engine) -> None:
    """Set-level backstop, so a whole table's indexes going missing is one clear
    failure rather than a wall of parametrized ones."""
    with owner_engine.connect() as conn:
        present = set(
            conn.execute(
                text(
                    "SELECT i.relname FROM pg_index x "
                    "JOIN pg_class i ON i.oid = x.indexrelid "
                    "JOIN pg_class t ON t.oid = x.indrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = 'public' "
                    "WHERE t.relkind = 'r'"
                )
            )
            .scalars()
            .all()
        )
    missing = sorted(set(ALL_INDEXES) - present)
    assert not missing, f"designed indexes missing from the database: {missing}"
