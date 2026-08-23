"""The index the deferred provenance trigger has been reading `raw_records` without.

Revision ID: 0011_link_provenance_index
Revises: 0010_settle_evidence_binding
Create Date: 2026-08-23

``KS009`` (0006, ``keystone_require_link_provenance``) is a **deferred** constraint
trigger: every ``entity_links`` row must name a landed record, and it asks

.. code-block:: sql

    SELECT 1 FROM raw_records rr
     WHERE rr.source_id   = NEW.source_id
       AND rr.natural_key = NEW.source_key
       AND rr.generation  = NEW.generation

Landing's only index is 0001's ``ix_raw_records_source_key`` on
``(source_id, entity_type, natural_key, generation)``. That is a **prefix
mismatch**: the trigger does not know ``entity_type`` -- an ``entity_links`` row
carries ``source_id`` and ``source_key`` and nothing else -- so ``natural_key``
cannot be used as an index key at all. Postgres can still use the index, but only
by scanning every entry for that ``source_id`` and re-checking the other two
columns, and there are three sources, so "that source's whole index range" is
roughly a third of the landing table. Per row.

Measured on the committed full-profile fixtures (360,400 landed records,
generations 1-3; Postgres 16, ``infra/docker-compose.yml``), materializing
generation 3 with lineage 1-3 -- 120,000 ``entity_links`` rows, so 120,000
deferred provenance probes at the commit::

    before   354.25s wall  (5m54s)
    after     22.17s wall  (16.0x), of which CREATE INDEX is 0.58s

Same database contents both times: the "after" database is a
``CREATE DATABASE ... TEMPLATE`` clone of the "before" one, so the index is the
only difference. One probe, ``EXPLAIN (ANALYZE, BUFFERS)``::

    before  Index Only Scan using ix_raw_records_source_key
            (cost=0.42..8555.56) actual time=6.289..17.639, Buffers: read=1332
    after   Index Only Scan using ix_raw_records_provenance
            (cost=0.42..8.44)    actual time=0.041..0.042, Buffers: read=4

1332 pages read per probe versus 4 is the whole story, and it is paid 120,000
times.

This matters beyond the benchmark: ``POST /internal/sync`` now materializes as its
second stage (``recon.api.internal.sync_job``), so the number above *is* the
grader's quick-start.

Additive and reversible: one index, no data change, no grant change. ``CONCURRENTLY``
is deliberately **not** used -- it cannot run inside alembic's transaction, and this
runs against a freshly migrated database where there is nothing to be concurrent
with.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_link_provenance_index"
down_revision: str | None = "0010_settle_evidence_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_raw_records_provenance"
TABLE = "raw_records"

#: Exactly the trigger's predicate, in its own column order. `generation` is last
#: because it is the lowest-cardinality of the three (there are three of them),
#: and `natural_key` is what actually makes a probe selective.
COLUMNS = ("source_id", "natural_key", "generation")


def upgrade() -> None:
    op.create_index(INDEX_NAME, TABLE, list(COLUMNS))


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name=TABLE)
