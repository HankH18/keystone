"""The settle is validated by the database, not by the Python that issues it.

Revision ID: 0010_settle_evidence_binding
Revises: 0009_source_generations
Create Date: 2026-08-23

Migration 0005 made *reserving* a property of the database: ``recon_writer``
holds no writable spend column at all, and ``budget_ledger.spent_microusd``
moves only under the ``budget_reservations`` triggers. It left the other
direction to the application. ``recon_writer`` holds
``UPDATE(actual_microusd, state, settled_at)``, and the settle trigger accepted
**any** ``actual`` in ``[0, reserve]`` from any principal that held the grant.
So the only thing standing between a capped party and a full refund of money it
had actually spent was a Python function -- and a fourth red-team pass settled
open reservations at ``actual = 0`` as ``recon_writer`` with three spellings the
project's AST counter does not match::

    UPDATE public.budget_reservations SET actual_microusd = 0, state = 'settled' ...
    UPDATE "budget_reservations"      SET actual_microusd = 0, state = 'settled' ...
    UPDATE ONLY budget_reservations   SET actual_microusd = 0, state = 'settled' ...

Counting release sites in the source is a useful smell test. It is not a
boundary: the grant permits the statement however it is spelled, and a boundary
that depends on how a statement is written is not one. Every other boundary in
this project is a grant plus a trigger. This one is now too.

RULING 1 -- the charged amount is DERIVED, never named
-------------------------------------------------------
Prices move into the database (``budget_model_prices``, seeded from the
committed ``prices.yaml``, readable by every role and writable by none of them).
A reservation may then be **price-bound**: it names a ``model`` and the
``max_input_tokens``/``max_output_tokens`` it was sized against, and the reserve
trigger refuses it unless::

    reserve_microusd = ceil(GREATEST(input, cache_read, cache_write) * max_input_tokens
                            + output * max_output_tokens)

which is exactly ``recon.budget.worst_case_microusd``, re-derived by the
database from rates the capped party cannot write. A caller cannot deflate the
rates to make its settlement cheap, because the same rates size the reservation
it is trying to keep.

The settle side is the mirror. A price-bound reservation settles only against a
``settle_evidence`` value, and each value fixes the amount **from the row**:

``provider_reported_usage``
    ``actual = ceil(input*u_in + output*u_out + cache_read*u_cr + cache_write*u_cw)``
    computed here, from this row's model, with the recorded usage required to be
    non-degenerate (a billed call reads a prompt and emits tokens) and **within
    the bounds the reservation was sized for**. The settle cannot name an amount
    that is not that number.
``cost_exceeded_reservation``
    the provider reported more than the row can hold: ``actual = reserve``,
    releasing nothing, and the recorded usage must genuinely exceed the bound.
``outcome_unknown``
    the absence of evidence: ``actual = reserve``. Releases nothing, ever.
``never_sent``
    the only full release. See RULING 2.

A price-bound reservation with **no** evidence is refused outright (``KS007``),
which is what closes all three spellings above: they name an amount and no
reason, and the amount is no longer the caller's to name.

Rows that carry no price binding are legacy/ops rows -- nothing in ``recon``
creates one. They may still be settled at any amount within their own
reservation, and that is harmless in the only direction that matters: such a row
can refund at most the budget it itself charged, so the ledger can never fall
below the sum of the price-bound reservations that are actually holding money.

RULING 2 -- a full release needs a proof the database recognises
-----------------------------------------------------------------
``never_sent`` charges zero and hands the whole reservation back, so it is the
one value worth forging. It now requires ``settle_proof``, a **closed
vocabulary** (``budget_never_sent_proof``) rather than a free string, and two
role-keyed rules the database can check on its own:

* ``ops_attested_outage`` -- the operator's attestation, the strongest proof --
  is refused to ``recon_writer``. The capped party cannot attest to its own
  outage;
* ``recon_writer`` may only claim a pre-send failure **within
  :data:`NEVER_SENT_WINDOW_SECONDS` (60s) of the reservation's own
  ``created_at``**,
  which is the database's clock and is frozen at insert. A pre-send failure is a
  connect-time failure; it is not discovered a minute later, and a reservation
  resurrected minutes or hours afterwards cannot claim its request never left.

What the database still cannot do is tell a truthful ``connection_refused`` from
a fabricated one. What it can do -- and now does -- is refuse an *arbitrary*
justification, refuse the operator-grade one to the capped party, and refuse a
late one. ``recon.budget.NeverSent`` is the other half: it takes a
:class:`~recon.budget.PreSendProof` member and not a string, so the proof is
classified from the transport's own exception rather than typed by a caller.

What this revision deliberately does NOT change
-----------------------------------------------
``recon_writer`` still holds **no INSERT and no UPDATE on ``budget_ledger``**.
0005 closed "invent a scope with a cap of my choosing" by revoking the grant
outright, and that stays closed: nothing here widens it, and
``tests/schema/test_budget_reservations.py`` still asserts the refusal is
``insufficient_privilege``.

The consequence is that ``recon.budget.provision_scope`` -- which
``/internal/{sync,reconcile}`` calls to open each run's ledger row -- is not
callable by the capped party, by construction. It runs on the **ops** principal,
which ``infra/render.yaml`` now supplies to the web service as a separate
``OPS_DATABASE_URL`` rather than as its ``DATABASE_URL``: the serving path
connects as ``recon_writer`` and one narrow call does not. See that file's
comments for what remains open.

SQLSTATEs
---------
``KS006`` reservation refused: no ledger row, or the cap would be exceeded
          (unchanged from 0005)
``KS007`` illegal budget-reservation lifecycle change -- now including a
          reservation whose reserved amount its own price binding does not give,
          and a settlement whose amount the row being closed does not justify
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
import yaml
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_settle_evidence_binding"
down_revision: str | None = "0009_source_generations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECON_WRITER = "recon_writer"
REVIEW_WRITER = "review_writer"
APPLY_WRITER = "apply_writer"
ALL_ROLES = (RECON_WRITER, REVIEW_WRITER, APPLY_WRITER)

PRICES_TABLE = "budget_model_prices"
RESERVATIONS = "budget_reservations"

#: The settle vocabulary. These strings are ``recon.budget.SpendEvidence.kind``
#: one-for-one: the type in Python and the enum in the database are the same
#: closed set, so a value that exists on one side and not the other is a
#: migration error rather than a silent widening.
SETTLE_EVIDENCE = (
    "provider_reported_usage",
    "cost_exceeded_reservation",
    "outcome_unknown",
    "never_sent",
)

#: The closed vocabulary of pre-send proofs. ``NeverSent("trust me bro")`` used
#: to release 15,850 microusd; the database now has an opinion about what a
#: proof may say. ``ops_attested_outage`` is the operator's own attestation and
#: is refused to the capped party by the settle trigger.
NEVER_SENT_PROOFS = (
    "connection_refused",
    "dns_failure",
    "tls_handshake_failed",
    "client_rejected_request",
    "auth_rejected_at_edge",
    "ops_attested_outage",
)

#: How long after ``created_at`` the capped party may still claim its request
#: never left the process. Mirrors the documented "a pre-send failure is a
#: connect-time failure" rule; ``recon.budget.NEVER_SENT_WINDOW_SECONDS`` holds
#: the same number and ``tests/budget`` asserts the two agree.
NEVER_SENT_WINDOW_SECONDS = 60

#: The ops-seeded row whose cap bounds every run scope the capped party opens.
PER_RUN_TEMPLATE_SCOPE = "run:default"

#: Columns the capped party supplies when reserving, after this revision.
RESERVATION_INSERT_COLUMNS = (
    "scope",
    "idempotency_key",
    "reserve_microusd",
    "model",
    "max_input_tokens",
    "max_output_tokens",
)

#: Columns the capped party supplies when settling. The amount is still in the
#: list -- and is now checked against one the database derives itself.
RESERVATION_UPDATE_COLUMNS = (
    "actual_microusd",
    "state",
    "settled_at",
    "settle_evidence",
    "settle_proof",
    "usage_input_tokens",
    "usage_output_tokens",
    "usage_cache_read_tokens",
    "usage_cache_write_tokens",
)

#: What 0005 granted, restored verbatim by :func:`downgrade`.
RESERVATION_INSERT_COLUMNS_0005 = ("scope", "idempotency_key", "reserve_microusd")
RESERVATION_UPDATE_COLUMNS_0005 = ("actual_microusd", "state", "settled_at")

USAGE_COLUMNS = (
    "usage_input_tokens",
    "usage_output_tokens",
    "usage_cache_read_tokens",
    "usage_cache_write_tokens",
)

RATE_COLUMNS = ("input_rate", "output_rate", "cache_read_rate", "cache_write_rate")

#: ``prices.yaml`` field name -> column name. The YAML names mirror the
#: provider's ``usage`` object; the column names spell out the unit.
RATE_FIELDS = (
    ("input", "input_rate"),
    ("output", "output_rate"),
    ("cache_read", "cache_read_rate"),
    ("cache_write", "cache_write_rate"),
)


def _columns(names: Sequence[str]) -> str:
    return ", ".join(f'"{name}"' for name in names)


def _prices_path() -> Path:
    """``<repo>/prices.yaml`` -- this file is ``<repo>/service/migrations/versions/``."""
    return Path(__file__).resolve().parents[3] / "prices.yaml"


def committed_prices() -> dict[str, dict[str, Decimal]]:
    """Parse the committed price table into exact decimals.

    Deliberately a re-parse rather than an import of ``recon.budget``: a
    migration must not depend on application code that a later revision may
    change out from under it. It reads the same committed file, through
    ``Decimal``, and fails loudly rather than seeding a zero rate -- a model
    priced at zero reserves nothing and would be unmetered.
    """
    path = _prices_path()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    models = raw.get("models") or {}
    if not isinstance(models, dict) or not models:
        raise RuntimeError(f"{path} declares no models; the ledger cannot price a call")
    parsed: dict[str, dict[str, Decimal]] = {}
    for model, rates in sorted(models.items()):
        if not isinstance(rates, dict):
            raise RuntimeError(f"{path}: model {model!r} has no rate mapping")
        row: dict[str, Decimal] = {}
        for field, column in RATE_FIELDS:
            if field not in rates:
                raise RuntimeError(f"{path}: model {model!r} is missing rate {field!r}")
            value = Decimal(str(rates[field]))
            if value < 0:
                raise RuntimeError(f"{path}: model {model!r} has a negative {field!r} rate")
            row[column] = value
        parsed[str(model)] = row
    return parsed


# ---------------------------------------------------------------------------
# upgrade / downgrade
# ---------------------------------------------------------------------------
def upgrade() -> None:
    _create_price_table()
    _add_binding_columns()
    _install_reserve_trigger()
    _install_settle_trigger()
    _rescope_grants()


def downgrade() -> None:
    _restore_0005_grants()
    _restore_0006_settle_trigger()
    _restore_0006_reserve_trigger()
    _drop_binding_columns()
    _drop_price_table()


# ---------------------------------------------------------------------------
# RULING 1 -- ops-owned prices, and a reservation that is bound to them
# ---------------------------------------------------------------------------
def _create_price_table() -> None:
    """The committed price table, in the database, writable by nobody.

    The rates have to be here for the settle trigger to derive an amount, and
    they have to be *unwritable by the capped party* for that derivation to mean
    anything. No role is granted INSERT or UPDATE: rates change by migration,
    which is also what keeps them reviewable and versioned.
    """
    op.create_table(
        PRICES_TABLE,
        sa.Column("model", sa.Text, primary_key=True),
        sa.Column("input_rate", sa.Numeric(24, 10), nullable=False),
        sa.Column("output_rate", sa.Numeric(24, 10), nullable=False),
        sa.Column("cache_read_rate", sa.Numeric(24, 10), nullable=False),
        sa.Column("cache_write_rate", sa.Numeric(24, 10), nullable=False),
        sa.CheckConstraint(
            "input_rate > 0 AND output_rate > 0 AND cache_read_rate >= 0 AND cache_write_rate >= 0",
            name="ck_model_price_positive",
        ),
        comment=(
            "Committed per-token rates, in microusd, mirroring prices.yaml. The "
            "budget_reservations triggers derive both the worst case and the "
            "settled amount from these, so they are owner-only: a capped party "
            "that could write a rate could write itself a refund."
        ),
    )
    prices = committed_prices()
    op.get_bind().execute(
        sa.text(
            f"INSERT INTO {PRICES_TABLE} (model, {', '.join(RATE_COLUMNS)}) "
            "VALUES (:model, :input_rate, :output_rate, "
            ":cache_read_rate, :cache_write_rate)"
        ),
        [{"model": model, **rates} for model, rates in sorted(prices.items())],
    )
    op.execute(f"GRANT SELECT ON {PRICES_TABLE} TO {_columns(ALL_ROLES)}")


def _drop_price_table() -> None:
    op.drop_table(PRICES_TABLE)


def _add_binding_columns() -> None:
    """The price binding, the evidence, and the usage the evidence is priced on.

    Every column is nullable. A row that names no ``model`` is *unbound*: it
    predates this revision or was written by ops directly, and it keeps 0005's
    behaviour. Nothing in ``recon`` creates one, and the burst asserts as much.
    """
    postgresql.ENUM(*SETTLE_EVIDENCE, name="budget_settle_evidence").create(
        op.get_bind(), checkfirst=True
    )
    postgresql.ENUM(*NEVER_SENT_PROOFS, name="budget_never_sent_proof").create(
        op.get_bind(), checkfirst=True
    )

    op.add_column(RESERVATIONS, sa.Column("model", sa.Text, nullable=True))
    op.add_column(RESERVATIONS, sa.Column("max_input_tokens", sa.BigInteger, nullable=True))
    op.add_column(RESERVATIONS, sa.Column("max_output_tokens", sa.BigInteger, nullable=True))
    op.add_column(
        RESERVATIONS,
        sa.Column(
            "settle_evidence",
            postgresql.ENUM(*SETTLE_EVIDENCE, name="budget_settle_evidence", create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        RESERVATIONS,
        sa.Column(
            "settle_proof",
            postgresql.ENUM(*NEVER_SENT_PROOFS, name="budget_never_sent_proof", create_type=False),
            nullable=True,
        ),
    )
    for column in USAGE_COLUMNS:
        op.add_column(RESERVATIONS, sa.Column(column, sa.BigInteger, nullable=True))

    op.create_foreign_key(
        "fk_budget_reservations_model", RESERVATIONS, PRICES_TABLE, ["model"], ["model"]
    )
    op.create_check_constraint(
        "ck_reservation_price_binding_complete",
        RESERVATIONS,
        "num_nulls(model, max_input_tokens, max_output_tokens) IN (0, 3)",
    )
    op.create_check_constraint(
        "ck_reservation_token_bounds_nonneg",
        RESERVATIONS,
        "(max_input_tokens IS NULL OR max_input_tokens >= 0) "
        "AND (max_output_tokens IS NULL OR max_output_tokens >= 0)",
    )
    op.create_check_constraint(
        "ck_reservation_usage_nonneg",
        RESERVATIONS,
        " AND ".join(f"({column} IS NULL OR {column} >= 0)" for column in USAGE_COLUMNS),
    )
    op.create_check_constraint(
        "ck_reservation_proof_is_never_sent_only",
        RESERVATIONS,
        "settle_proof IS NULL OR settle_evidence = 'never_sent'::budget_settle_evidence",
    )
    op.create_check_constraint(
        "ck_reservation_settlement_fields_match_state",
        RESERVATIONS,
        "state = 'settled'::budget_reservation_state OR ("
        "settle_evidence IS NULL AND settle_proof IS NULL AND "
        + " AND ".join(f"{column} IS NULL" for column in USAGE_COLUMNS)
        + ")",
    )


def _drop_binding_columns() -> None:
    for name in (
        "ck_reservation_settlement_fields_match_state",
        "ck_reservation_proof_is_never_sent_only",
        "ck_reservation_usage_nonneg",
        "ck_reservation_token_bounds_nonneg",
        "ck_reservation_price_binding_complete",
    ):
        op.drop_constraint(name, RESERVATIONS, type_="check")
    op.drop_constraint("fk_budget_reservations_model", RESERVATIONS, type_="foreignkey")
    for column in (*USAGE_COLUMNS, "settle_proof", "settle_evidence"):
        op.drop_column(RESERVATIONS, column)
    for column in ("max_output_tokens", "max_input_tokens", "model"):
        op.drop_column(RESERVATIONS, column)
    postgresql.ENUM(name="budget_never_sent_proof").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="budget_settle_evidence").drop(op.get_bind(), checkfirst=True)


def _install_reserve_trigger() -> None:
    """RESERVE, plus: a price-bound reservation must be arithmetic, not a claim.

    0005's version checked that a reservation is born open. This one adds the
    consistency rule that makes the settle side worth anything: when the row
    names a model, ``reserve_microusd`` must be exactly the worst case the
    database computes from the ops-owned rates and the row's own token bounds.

    Without it the rates would still be caller-supplied in effect -- a caller
    could name tiny-but-nonzero rates, reserve a large amount against them and
    then settle at 1 microusd. With it, deflating the rates deflates the
    reservation the caller is trying to keep, so there is nothing to win.

    Input is priced at the dearest input-side rate, exactly as
    ``recon.budget.worst_case_microusd`` does, because a prompt-cached call
    bills its first pass at the cache-write rate.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_budget_reserve() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        DECLARE
            price budget_model_prices%ROWTYPE;
            expected bigint;
        BEGIN
            IF NEW.state IS DISTINCT FROM 'open'::budget_reservation_state
               OR NEW.actual_microusd IS NOT NULL
               OR NEW.settled_at IS NOT NULL
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'a budget reservation is born open with no actual and no'
                        || ' settled_at; got state='
                        || coalesce(NEW.state::text, 'NULL')
                        || ', actual_microusd=' || coalesce(NEW.actual_microusd::text, 'NULL')
                        || ', settled_at=' || coalesce(NEW.settled_at::text, 'NULL');
            END IF;

            IF NEW.settle_evidence IS NOT NULL
               OR NEW.settle_proof IS NOT NULL
               OR NEW.usage_input_tokens IS NOT NULL
               OR NEW.usage_output_tokens IS NOT NULL
               OR NEW.usage_cache_read_tokens IS NOT NULL
               OR NEW.usage_cache_write_tokens IS NOT NULL
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'a budget reservation is born with no settlement evidence:'
                        || ' the evidence and the usage it is priced on are written by the'
                        || ' settle, never by the reserve';
            END IF;

            IF NEW.model IS NOT NULL THEN
                SELECT * INTO price FROM budget_model_prices WHERE model = NEW.model;
                IF NOT FOUND THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'KS007',
                        MESSAGE = 'model ' || NEW.model || ' is not in budget_model_prices;'
                            || ' an unpriced model would reserve nothing';
                END IF;

                expected := ceil(
                    GREATEST(price.input_rate, price.cache_read_rate,
                             price.cache_write_rate) * NEW.max_input_tokens
                    + price.output_rate * NEW.max_output_tokens);

                IF NEW.reserve_microusd IS DISTINCT FROM expected THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'KS007',
                        MESSAGE = 'reservation for model ' || NEW.model || ' must reserve the'
                            || ' worst case the committed rates give for its own token bounds:'
                            || ' expected ' || expected || ', got '
                            || coalesce(NEW.reserve_microusd::text, 'NULL');
                END IF;
            END IF;

            PERFORM keystone_budget_charge(NEW.scope, NEW.reserve_microusd);
            RETURN NEW;
        END;
        $$;
        """
    )


def _install_settle_trigger() -> None:
    """SETTLE: the amount comes from the row, and the release needs a reason.

    Read the branches as one rule each. Only ``never_sent`` releases the whole
    reservation, only ``provider_reported_usage`` releases a part of it, and
    that part is a number this function computes -- the caller's
    ``actual_microusd`` has to *equal* it or the statement is refused. Every
    other outcome charges the reservation in full.

    ``SECURITY DEFINER`` with the role check on ``session_user``, exactly as
    0006's RULING 9 left it: the privileged ledger write is owner-run so no role
    needs EXECUTE on the mutators, and the acting role is still the
    authenticated LOGIN role because ``recon.db`` authenticates as the role and
    never issues ``SET ROLE``.
    """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION keystone_budget_settle() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        DECLARE
            acting_role text := session_user;
            price budget_model_prices%ROWTYPE;
            derived bigint;
            total_input bigint;
            usage_nulls int;
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.scope IS DISTINCT FROM OLD.scope
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.reserve_microusd IS DISTINCT FROM OLD.reserve_microusd
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.model IS DISTINCT FROM OLD.model
               OR NEW.max_input_tokens IS DISTINCT FROM OLD.max_input_tokens
               OR NEW.max_output_tokens IS DISTINCT FROM OLD.max_output_tokens
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'a reservation''s identity, scope, idempotency key, reserved'
                        || ' amount, creation time and price binding are immutable'
                        || ' after insert';
            END IF;

            IF OLD.state <> 'open'::budget_reservation_state THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'reservation ' || OLD.id || ' is already '
                        || OLD.state::text || '; a reservation settles exactly once';
            END IF;

            IF acting_role = '{RECON_WRITER}'
               AND NEW.state IS DISTINCT FROM 'settled'::budget_reservation_state
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'role {RECON_WRITER} may only settle a reservation'
                        || ' (open -> settled); reclaiming a reservation releases spend'
                        || ' in full and belongs to the sweeper, not to the capped party';
            END IF;

            IF NEW.state = 'settled'::budget_reservation_state THEN
                IF NEW.actual_microusd IS NULL
                   OR NEW.actual_microusd < 0
                   OR NEW.actual_microusd > OLD.reserve_microusd
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'KS007',
                        MESSAGE = 'settling reservation ' || OLD.id || ' requires 0 <= actual <='
                            || ' reserve (' || OLD.reserve_microusd || '), got '
                            || coalesce(NEW.actual_microusd::text, 'NULL');
                END IF;

                usage_nulls := num_nulls(NEW.usage_input_tokens, NEW.usage_output_tokens,
                                         NEW.usage_cache_read_tokens,
                                         NEW.usage_cache_write_tokens);

                -- A PRICE-BOUND reservation is the product's own. Its amount is
                -- derivable here, so naming one without a reason is refused --
                -- and that is what closes `UPDATE ... SET actual_microusd = 0`
                -- in every spelling, from every principal that holds the grant.
                IF OLD.model IS NOT NULL AND NEW.settle_evidence IS NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'KS007',
                        MESSAGE = 'reservation ' || OLD.id || ' is price-bound to model '
                            || OLD.model || ', so its settlement must name the evidence that'
                            || ' justifies its amount (settle_evidence); an UPDATE that names'
                            || ' an amount and no reason is refused however it is spelled';
                END IF;

                IF NEW.settle_evidence IS NOT NULL THEN
                    IF NEW.settle_evidence IN (
                        'provider_reported_usage'::budget_settle_evidence,
                        'cost_exceeded_reservation'::budget_settle_evidence)
                    THEN
                        IF OLD.model IS NULL THEN
                            RAISE EXCEPTION USING
                                ERRCODE = 'KS007',
                                MESSAGE = 'settling reservation ' || OLD.id || ' as '
                                    || NEW.settle_evidence::text || ' needs a price binding;'
                                    || ' this row names no model, so no amount is derivable'
                                    || ' from it';
                        END IF;
                        IF usage_nulls <> 0 THEN
                            RAISE EXCEPTION USING
                                ERRCODE = 'KS007',
                                MESSAGE = 'settling reservation ' || OLD.id || ' as '
                                    || NEW.settle_evidence::text || ' requires the'
                                    || ' provider-reported usage it is priced on';
                        END IF;
                        SELECT * INTO price FROM budget_model_prices WHERE model = OLD.model;
                        total_input := NEW.usage_input_tokens + NEW.usage_cache_read_tokens
                                       + NEW.usage_cache_write_tokens;
                        derived := ceil(
                            price.input_rate * NEW.usage_input_tokens
                            + price.output_rate * NEW.usage_output_tokens
                            + price.cache_read_rate * NEW.usage_cache_read_tokens
                            + price.cache_write_rate * NEW.usage_cache_write_tokens);
                    ELSIF usage_nulls <> 4 THEN
                        RAISE EXCEPTION USING
                            ERRCODE = 'KS007',
                            MESSAGE = 'settling reservation ' || OLD.id || ' as '
                                || NEW.settle_evidence::text || ' records no usage: it is'
                                || ' the ABSENCE of a provider report, not a report';
                    END IF;

                    IF NEW.settle_evidence = 'provider_reported_usage'::budget_settle_evidence
                    THEN
                        IF total_input <= 0 OR NEW.usage_output_tokens <= 0 THEN
                            RAISE EXCEPTION USING
                                ERRCODE = 'KS007',
                                MESSAGE = 'reservation ' || OLD.id || ': a billed call reads a'
                                    || ' prompt and emits tokens, so usage of '
                                    || total_input || ' in / ' || NEW.usage_output_tokens
                                    || ' out is not evidence of a cost -- settle it as'
                                    || ' outcome_unknown, which charges the reservation';
                        END IF;
                        IF total_input > OLD.max_input_tokens
                           OR NEW.usage_output_tokens > OLD.max_output_tokens
                        THEN
                            RAISE EXCEPTION USING
                                ERRCODE = 'KS007',
                                MESSAGE = 'reservation ' || OLD.id || ': reported usage of '
                                    || total_input || ' in / ' || NEW.usage_output_tokens
                                    || ' out is outside the bounds this reservation was sized'
                                    || ' for (' || OLD.max_input_tokens || ' / '
                                    || OLD.max_output_tokens || '); that is an overspend, not'
                                    || ' a settlement';
                        END IF;
                        IF NEW.actual_microusd IS DISTINCT FROM derived THEN
                            RAISE EXCEPTION USING
                                ERRCODE = 'KS007',
                                MESSAGE = 'reservation ' || OLD.id || ': the committed rates'
                                    || ' price that usage at ' || derived || ' microusd, not '
                                    || NEW.actual_microusd || '. A settlement does not name'
                                    || ' its own amount';
                        END IF;

                    ELSIF NEW.settle_evidence
                          = 'cost_exceeded_reservation'::budget_settle_evidence
                    THEN
                        IF derived <= OLD.reserve_microusd
                           AND total_input <= OLD.max_input_tokens
                           AND NEW.usage_output_tokens <= OLD.max_output_tokens
                        THEN
                            RAISE EXCEPTION USING
                                ERRCODE = 'KS007',
                                MESSAGE = 'reservation ' || OLD.id || ': ' || derived
                                    || ' microusd fits inside the reservation ('
                                    || OLD.reserve_microusd || '), so this is an ordinary'
                                    || ' provider_reported_usage settlement, not an overspend';
                        END IF;
                        IF NEW.actual_microusd <> OLD.reserve_microusd THEN
                            RAISE EXCEPTION USING
                                ERRCODE = 'KS007',
                                MESSAGE = 'reservation ' || OLD.id || ': a cost that exceeded'
                                    || ' the reservation charges every microusd it can ('
                                    || OLD.reserve_microusd || '), releasing nothing; got '
                                    || NEW.actual_microusd;
                        END IF;

                    ELSIF NEW.settle_evidence = 'outcome_unknown'::budget_settle_evidence THEN
                        IF NEW.actual_microusd <> OLD.reserve_microusd THEN
                            RAISE EXCEPTION USING
                                ERRCODE = 'KS007',
                                MESSAGE = 'reservation ' || OLD.id || ': an unknown outcome'
                                    || ' charges the FULL reservation ('
                                    || OLD.reserve_microusd || ') and releases nothing; got '
                                    || NEW.actual_microusd || '. Guessing low is the leak';
                        END IF;

                    ELSIF NEW.settle_evidence = 'never_sent'::budget_settle_evidence THEN
                        IF NEW.settle_proof IS NULL THEN
                            RAISE EXCEPTION USING
                                ERRCODE = 'KS007',
                                MESSAGE = 'reservation ' || OLD.id || ': releasing a whole'
                                    || ' reservation needs a stated proof from the'
                                    || ' budget_never_sent_proof vocabulary';
                        END IF;
                        IF NEW.actual_microusd <> 0 THEN
                            RAISE EXCEPTION USING
                                ERRCODE = 'KS007',
                                MESSAGE = 'reservation ' || OLD.id || ': a request that never'
                                    || ' left was never billed, so it charges 0, not '
                                    || NEW.actual_microusd;
                        END IF;
                        IF acting_role = '{RECON_WRITER}' THEN
                            IF NEW.settle_proof
                               = 'ops_attested_outage'::budget_never_sent_proof
                            THEN
                                RAISE EXCEPTION USING
                                    ERRCODE = 'KS007',
                                    MESSAGE = 'role {RECON_WRITER} may not attest to its own'
                                        || ' outage; ops_attested_outage is the operator''s'
                                        || ' proof and belongs to the sweeper principal';
                            END IF;
                            IF now() - OLD.created_at
                               > interval '{NEVER_SENT_WINDOW_SECONDS} seconds'
                            THEN
                                RAISE EXCEPTION USING
                                    ERRCODE = 'KS007',
                                    MESSAGE = 'reservation ' || OLD.id || ' was created '
                                        || round(extract(epoch from now() - OLD.created_at))
                                        || 's ago; a pre-send failure is a connect-time'
                                        || ' failure and cannot be discovered more than'
                                        || ' {NEVER_SENT_WINDOW_SECONDS}s later. Settle it as'
                                        || ' outcome_unknown';
                            END IF;
                        END IF;
                    END IF;
                END IF;

                NEW.settled_at := now();
                PERFORM keystone_budget_release(
                    OLD.scope, OLD.reserve_microusd - NEW.actual_microusd);
            ELSIF NEW.state = 'reclaimed'::budget_reservation_state THEN
                IF NEW.actual_microusd IS NOT NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'KS007',
                        MESSAGE = 'a reclaimed reservation records no actual spend';
                END IF;
                NEW.settled_at := now();
                PERFORM keystone_budget_release(OLD.scope, OLD.reserve_microusd);
            ELSE
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'the only legal changes to an open reservation are'
                        || ' open -> settled and open -> reclaimed, got '
                        || coalesce(NEW.state::text, 'NULL');
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )


def _restore_0006_reserve_trigger() -> None:
    """The reserve trigger exactly as 0006 left it: owner-run, no price binding."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_budget_reserve() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        BEGIN
            IF NEW.state IS DISTINCT FROM 'open'::budget_reservation_state
               OR NEW.actual_microusd IS NOT NULL
               OR NEW.settled_at IS NOT NULL
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'a budget reservation is born open with no actual and no'
                        || ' settled_at; got state='
                        || coalesce(NEW.state::text, 'NULL')
                        || ', actual_microusd=' || coalesce(NEW.actual_microusd::text, 'NULL')
                        || ', settled_at=' || coalesce(NEW.settled_at::text, 'NULL');
            END IF;

            PERFORM keystone_budget_charge(NEW.scope, NEW.reserve_microusd);
            RETURN NEW;
        END;
        $$;
        """
    )


def _restore_0006_settle_trigger() -> None:
    """The settle trigger exactly as 0006 left it: any actual in [0, reserve]."""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION keystone_budget_settle() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        DECLARE
            acting_role text := session_user;
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.scope IS DISTINCT FROM OLD.scope
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.reserve_microusd IS DISTINCT FROM OLD.reserve_microusd
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'a reservation''s identity, scope, idempotency key, reserved'
                        || ' amount and creation time are immutable after insert';
            END IF;

            IF OLD.state <> 'open'::budget_reservation_state THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'reservation ' || OLD.id || ' is already '
                        || OLD.state::text || '; a reservation settles exactly once';
            END IF;

            IF acting_role = '{RECON_WRITER}'
               AND NEW.state IS DISTINCT FROM 'settled'::budget_reservation_state
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'role {RECON_WRITER} may only settle a reservation'
                        || ' (open -> settled); reclaiming a reservation releases spend'
                        || ' in full and belongs to the sweeper, not to the capped party';
            END IF;

            IF NEW.state = 'settled'::budget_reservation_state THEN
                IF NEW.actual_microusd IS NULL
                   OR NEW.actual_microusd < 0
                   OR NEW.actual_microusd > OLD.reserve_microusd
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'KS007',
                        MESSAGE = 'settling reservation ' || OLD.id || ' requires 0 <= actual <='
                            || ' reserve (' || OLD.reserve_microusd || '), got '
                            || coalesce(NEW.actual_microusd::text, 'NULL');
                END IF;
                NEW.settled_at := now();
                PERFORM keystone_budget_release(
                    OLD.scope, OLD.reserve_microusd - NEW.actual_microusd);
            ELSIF NEW.state = 'reclaimed'::budget_reservation_state THEN
                IF NEW.actual_microusd IS NOT NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'KS007',
                        MESSAGE = 'a reclaimed reservation records no actual spend';
                END IF;
                NEW.settled_at := now();
                PERFORM keystone_budget_release(OLD.scope, OLD.reserve_microusd);
            ELSE
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'the only legal changes to an open reservation are'
                        || ' open -> settled and open -> reclaimed, got '
                        || coalesce(NEW.state::text, 'NULL');
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )


# ---------------------------------------------------------------------------
# the grant surface
# ---------------------------------------------------------------------------
def _rescope_grants() -> None:
    op.execute(f"REVOKE INSERT ON {RESERVATIONS} FROM {_columns(ALL_ROLES)}")
    op.execute(f"REVOKE UPDATE ON {RESERVATIONS} FROM {_columns(ALL_ROLES)}")
    op.execute(
        f"GRANT INSERT ({_columns(RESERVATION_INSERT_COLUMNS)}) "
        f'ON {RESERVATIONS} TO "{RECON_WRITER}"'
    )
    op.execute(
        f"GRANT UPDATE ({_columns(RESERVATION_UPDATE_COLUMNS)}) "
        f'ON {RESERVATIONS} TO "{RECON_WRITER}"'
    )


def _restore_0005_grants() -> None:
    op.execute(f"REVOKE INSERT ON {RESERVATIONS} FROM {_columns(ALL_ROLES)}")
    op.execute(f"REVOKE UPDATE ON {RESERVATIONS} FROM {_columns(ALL_ROLES)}")
    op.execute(
        f"GRANT INSERT ({_columns(RESERVATION_INSERT_COLUMNS_0005)}) "
        f'ON {RESERVATIONS} TO "{RECON_WRITER}"'
    )
    op.execute(
        f"GRANT UPDATE ({_columns(RESERVATION_UPDATE_COLUMNS_0005)}) "
        f'ON {RESERVATIONS} TO "{RECON_WRITER}"'
    )
