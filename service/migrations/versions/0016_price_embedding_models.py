"""The three embedding models R25 reserves against get a price.

Revision ID: 0016_price_embedding_models
Revises: 0015_escalation_reason_grant
Create Date: 2026-08-24

The gap this closes
-------------------
``recon.incidents`` (R25, stretch #8) meters every embedding call through
:func:`recon.budget.reserve`, on the model id its provider is priced on::

    mock   -> mock-embedding-v1
    voyage -> voyage-3.5
    openai -> text-embedding-3-small

None of the three was in ``prices.yaml``, and ``prices.yaml`` is what migration
0010 seeded ``budget_model_prices`` from. Two independent doors therefore refused
the feature outright:

* :func:`recon.incidents.build_embedding_provider` calls ``_require_priced``
  **before** it branches on the provider name, so even the offline default
  raised::

      EmbeddingProviderNotConfigured: EMBEDDING_PROVIDER='mock' is priced on model
      'mock-embedding-v1', which is not in the committed prices.yaml ...

* and had it not, 0010's reserve trigger would have refused the INSERT a second
  time (``model ... is not in budget_model_prices``).

Both refusals were correct -- an unpriced model reserves nothing, and a
reservation of nothing is an uncapped call. They were fail-*closed* doors, not a
working feature. ``prices.yaml`` version 2 adds the rates; this revision puts
them where the triggers can read them.

Why literals here, and not a re-parse of ``prices.yaml``
--------------------------------------------------------
0010 seeds ``budget_model_prices`` by *parsing the committed file at migration
time*, which means a database migrated today already receives these three rows
from 0010 -- and a database migrated last week did not. A revision is immutable
history, so this one states the rates it was written for as literals and upserts
them. The two paths then converge on any database, in any order:

* fresh database -- 0010 seeds all twelve models from ``prices.yaml`` v2, and
  this revision re-states three of them to the same numbers (a no-op UPDATE);
* database already at 0010..0015 -- 0010 seeded nine, and this revision inserts
  the three that are missing.

``ON CONFLICT (model) DO UPDATE``, never ``DO NOTHING``: ``DO NOTHING`` would
leave a wrong pre-existing rate in place and report success, and a wrong rate is
exactly what the price table exists to make impossible. It also converges a
database where ``tests/incidents/conftest.embedding_prices`` -- the stand-in
fixture this revision retires -- left rows behind.

Drift between the file and the table is NOT re-checked here. It already has one
detector, in the place that can act on it:
``tests/budget/test_prices.py::test_the_seeded_database_rates_are_the_committed_price_table``
compares every rate in ``budget_model_prices`` to every rate in ``prices.yaml``
and says in its own failure message that the fix is a new migration. Repeating
that check inside a migration would make the whole chain unrunnable after the
next legitimate rate change, which is a worse failure than the one it guards.

What the rates are
------------------
``prices.yaml`` carries the vendor, the URL and the capture date for each. In
microusd per token:

===========================  =========  ==========================================
model                        rate       provenance
===========================  =========  ==========================================
``voyage-3.5``               ``0.06``   Voyage AI list, $0.06/1M, captured 2026-08-24
``text-embedding-3-small``   ``0.02``   OpenAI list (standard, not Batch), $0.02/1M
``mock-embedding-v1``        ``0.06``   policy: the higher of the two live rates
===========================  =========  ==========================================

All four rate columns carry the same number per model. An embeddings endpoint
emits no output tokens and has no prompt cache, so ``output_rate``,
``cache_read_rate`` and ``cache_write_rate`` have no vendor meaning -- but
``ck_model_price_positive`` forbids a zero on the first two, and
:func:`recon.budget.worst_case_microusd` prices the input side at the
*cache-write* rate, so cache_write must be the real rate or every embedding
reservation is wrong. ``prices.yaml`` states the same three reasons at the
block.

Downgrade
---------
Deletes the three rows -- and refuses, loudly, if a ``budget_reservations`` row
still names one of them. ``fk_budget_reservations_model`` (0010) would refuse it
anyway with SQLSTATE 23503; catching it first says *why* deleting the rate is
the wrong move: the settle trigger derives a settled amount from these rates, so
a reservation whose model has no rate is a charge nobody can re-derive.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision: str = "0016_price_embedding_models"
down_revision: str | None = "0015_escalation_reason_grant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 0010's table. Owner-only for writes; every role holds SELECT, and a row added
#: here inherits that table grant, so no GRANT is needed.
PRICES_TABLE = "budget_model_prices"
RESERVATIONS = "budget_reservations"

RATE_COLUMNS = ("input_rate", "output_rate", "cache_read_rate", "cache_write_rate")

#: ``prices.yaml`` version 2, in microusd per token, as exact decimals. One rate
#: per model, repeated across all four columns -- see the module docstring.
EMBEDDING_RATES: dict[str, Decimal] = {
    "mock-embedding-v1": Decimal("0.06"),
    "text-embedding-3-small": Decimal("0.02"),
    "voyage-3.5": Decimal("0.06"),
}

#: Stamped nowhere -- it is here so a reader can tell at a glance which version
#: of the committed file these literals were copied from.
PRICES_YAML_VERSION = 2


def _rows() -> list[dict[str, object]]:
    """One bind-parameter row per model, in sorted order.

    Sorted because a migration is reviewed as a diff and read as a log: the
    order rows are seeded in is the order they appear in a ``\\d+`` dump on a
    fresh database, and an arbitrary order there is noise a reviewer has to
    re-derive.
    """
    return [
        {"model": model, **{column: rate for column in RATE_COLUMNS}}
        for model, rate in sorted(EMBEDDING_RATES.items())
    ]


def upgrade() -> None:
    columns = ", ".join(RATE_COLUMNS)
    assignments = ", ".join(f"{column} = EXCLUDED.{column}" for column in RATE_COLUMNS)
    op.get_bind().execute(
        sa.text(
            f"INSERT INTO {PRICES_TABLE} (model, {columns}) "
            "VALUES (:model, :input_rate, :output_rate, :cache_read_rate, :cache_write_rate) "
            f"ON CONFLICT (model) DO UPDATE SET {assignments}"
        ),
        _rows(),
    )


def downgrade() -> None:
    bind = op.get_bind()
    referenced = (
        bind.execute(
            sa.text(
                f"SELECT DISTINCT model FROM {RESERVATIONS} "
                "WHERE model = ANY(:models) ORDER BY model"
            ),
            {"models": sorted(EMBEDDING_RATES)},
        )
        .scalars()
        .all()
    )
    if referenced:
        raise RuntimeError(
            f"cannot unprice {list(referenced)}: {RESERVATIONS} rows still name "
            "them, and migration 0010's settle trigger derives a settled amount "
            "from these rates. Deleting the rate would leave a charge that "
            "nobody can re-derive -- and fk_budget_reservations_model would "
            "refuse the DELETE anyway (SQLSTATE 23503). Reclaim or archive those "
            "reservations first."
        )
    bind.execute(
        sa.text(f"DELETE FROM {PRICES_TABLE} WHERE model = ANY(:models)"),
        {"models": sorted(EMBEDDING_RATES)},
    )
