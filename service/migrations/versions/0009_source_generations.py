"""`source_generations`: the per-source completeness ledger DESIGN and the contract require.

Revision ID: 0009_source_generations
Revises: 0008_ledger_write_honesty
Create Date: 2026-08-22

**Why this migration exists at all.** ``docs/DESIGN.md`` (Data models) and
``docs/invariant-contract.md`` SS3 / SS5.3 both name
``source_generations(source_id, generation, entity_type, expected_count,
loaded_count, complete bool)`` as a first-class table, and SS5.3 makes it
*load-bearing rather than logging*: an absence-style rule (C1, C2, C5, C7, C8, C9,
C13) must be **skipped** with ``verdict='unchecked'`` and
``detail.reason='source_incomplete'`` for any source whose generation-3 load did
not complete. Revisions 0001-0008 never created it -- ``ingest_runs`` is
per-``(run_id, source_id)`` and carries no per-entity-type expected/loaded counts,
so it cannot answer "did *this* entity type arrive in full".

That gap is not cosmetic. Without the ledger, a payments source that 5xxs
half-way through looks exactly like a payments source that legitimately has no
record for those students, and every absence rule fires: SS9.1's raw sweep puts
875 enrollments in C7's population and 575 persons in C1's, so one truncated load
turns into thousands of fabricated conflicts against a golden set that has none of
them. The table is how "we did not see it" is told apart from "it is not there".

Nothing in 0001-0008 is modified: this revision only adds a table and its grants.

``complete`` is a plain stored boolean rather than a generated column. It is set
by the ingest path from three facts -- the load ended without a source error, the
loaded count equals the manifest's expected count for that
``(source, entity_type, generation)``, and nothing was rejected -- and the last
two of those are not both columns here (``expected_count`` is nullable, because a
source without a committed manifest has no expected count and is judged only on
"the stream ended cleanly"). Encoding the decision in SQL would put a *second*
definition of completeness next to the Python one, which is the drift the shared
normalization module exists to prevent.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_source_generations"
down_revision: str | None = "0008_ledger_write_honesty"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "source_generations"
RECON_WRITER = "recon_writer"
READERS = ("recon_writer", "review_writer", "apply_writer")

#: Every ``stg_*`` table has ``raw_record_id REFERENCES raw_records(id)`` and 0001
#: created **no index on the referencing column**. Postgres indexes the referenced
#: side automatically and never the referencing side, so deleting one landing row
#: makes it sequentially scan all five staging tables to prove nothing points at
#: it. Measured here on a 400,000-row landing table: deleting ~7,000 rows ran for
#: **13+ minutes** and had to be cancelled. Adding the five indexes is additive --
#: no column, constraint or grant from 0001-0008 changes -- and it is also what
#: every staging-to-landing lineage join (``field_lineage``, the unified entity
#: view) will want anyway.
STAGING_LINEAGE_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_stg_crm_contact_raw_record", "stg_crm_contact"),
    ("ix_stg_crm_deal_raw_record", "stg_crm_deal"),
    ("ix_stg_student_raw_record", "stg_student"),
    ("ix_stg_enrollment_raw_record", "stg_enrollment"),
    ("ix_stg_payment_raw_record", "stg_payment"),
)

TS = sa.TIMESTAMP(timezone=True)
NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("source_id", sa.Text, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("entity_type", sa.Text, nullable=False),
        sa.Column("expected_count", sa.Integer, nullable=True),
        sa.Column("loaded_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("rejected_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("complete", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("run_id", sa.Text, nullable=True),
        sa.Column("error_detail", postgresql.JSONB, nullable=True),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint(
            "source_id", "generation", "entity_type", name="pk_source_generations"
        ),
        sa.CheckConstraint("loaded_count >= 0", name="ck_source_generations_loaded_nonneg"),
        sa.CheckConstraint("rejected_count >= 0", name="ck_source_generations_rejected_nonneg"),
        sa.CheckConstraint("generation >= 1", name="ck_source_generations_generation_positive"),
        comment=(
            "Per-source, per-entity-type, per-generation completeness ledger "
            "(DESIGN Data models; contract SS3, SS5.3). `complete = false` MUST make "
            "every absence-style rule (C1, C2, C5, C7, C8, C9, C13) emit "
            "verdict='unchecked' with detail.reason='source_incomplete' for the whole "
            "run, and marks the run degraded. This is a correctness input, not logging."
        ),
    )
    op.create_index(
        "ix_source_generations_complete",
        TABLE,
        ["generation", "complete"],
    )

    for index_name, table in STAGING_LINEAGE_INDEXES:
        op.create_index(index_name, table, ["raw_record_id"])

    readers = ", ".join(f'"{role}"' for role in READERS)
    op.execute(f"GRANT SELECT ON {TABLE} TO {readers}")
    # The detection path stamps the ledger and re-stamps it when a generation is
    # re-ingested, so it holds INSERT and UPDATE -- and, as everywhere else on this
    # path, no DELETE.
    op.execute(f'GRANT INSERT, UPDATE ON {TABLE} TO "{RECON_WRITER}"')


def downgrade() -> None:
    for index_name, table in STAGING_LINEAGE_INDEXES:
        op.drop_index(index_name, table_name=table)
    op.drop_index("ix_source_generations_complete", table_name=TABLE)
    op.drop_table(TABLE)
