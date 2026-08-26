"""The ``KS006`` cap message states the refusal instead of ordering a halt.

Revision ID: 0017_cap_message_states_refusal
Revises: 0016_price_embedding_models
Create Date: 2026-08-26

The gap this closes
-------------------
Migration 0005 gave ``keystone_budget_charge`` this message for the cap branch::

    budget cap exceeded for scope run:R: spent 900000 + reserve 200000
        > cap 1000000 -- halt the run

The last four words are an instruction, and on the caller that matters most they
instruct the wrong thing. The text does not stay in the database:

* :func:`recon.budget._reserve_once` lifts it verbatim --
  ``detail = str(getattr(exc, "orig", exc)).strip()`` -- onto
  :class:`recon.budget.BudgetCapExceeded`;
* :func:`recon.llm.generate_rationale` returns it as the ``cap_hit`` outcome's
  ``detail``;
* :func:`recon.budget.record_cap_hit` writes it into the ``audit_log`` row that
  the R18 reviewer surface reconciles against.

So the sentence is *persisted operator-facing advice*, and a reviewer reading a
``cap_hit`` row on the reconcile path was being told to halt a run that the code
had deliberately not halted.

What is actually true, and why one word cannot carry it
--------------------------------------------------------
``KS006`` stops the SPEND, always: the trigger refuses the reservation, nothing
is charged, and no provider call happens without a live reservation. What stops
*besides* the spend is the caller's decision, and the two callers differ --
:mod:`recon.budget`'s module docstring, section ``What "stop on cap" actually
stops``, is the long form:

* ``python -m recon.incidents`` **does** halt. :class:`recon.budget.BudgetError`
  propagates to :func:`recon.incidents.main`, which exits ``EXIT_REFUSED``;
* the **reconcile path continues**. :func:`recon.llm.generate_rationale` returns
  ``status="cap_hit"`` with ``text=None``, the proposal lands with
  ``rationale NULL``, and the next conflict makes its own refused reservation.

A trigger cannot see which of those it is running under, so the message must not
guess. It now states the only thing the database knows and every caller shares::

    budget cap exceeded for scope run:R: spent 900000 + reserve 200000
        > cap 1000000 -- this reservation is refused and nothing was charged

Everything a debugger needs -- scope, spent, reserve, cap -- is unchanged and in
the same order, because tests and the persisted ``audit_log`` detail read that
content. Only the trailing clause moves.

Why a new revision rather than an edit to 0005
-----------------------------------------------
0005 is landed history: databases -- including the deployed one -- are migrated
past it, so editing its body would change what a replay produces without
changing any database that already ran it, and the two would silently disagree.
``CREATE OR REPLACE FUNCTION`` restates the body here instead.

This is a MESSAGE change and nothing else
------------------------------------------
The raising condition (``ledger_spent + p_amount > ledger_cap``), the SQLSTATE
(``KS006``), the ``KS007`` depth guard, the ``FOR UPDATE`` ledger lock, the
missing-row branch and the ``UPDATE`` are all reproduced verbatim from 0005, and
:func:`_charge_function` is written so that ``upgrade`` and ``downgrade`` differ
in **exactly one string literal**. The triggers are untouched: neither
``keystone_budget_reserve`` nor ``keystone_budget_settle`` is mentioned here, so
0010's versions of them keep calling this helper unchanged.

**No GRANT or REVOKE appears in this revision, deliberately.** 0005 granted
EXECUTE on ``keystone_budget_charge`` to all three roles and RULING 9 / migration
0006 then revoked it from PUBLIC *and* from every role -- the reserve/settle
triggers became SECURITY DEFINER, so the legitimate path calls this helper as the
owner and no role needs the grant. Re-issuing 0005's grant block here would
re-open the ``pg_temp`` back door 0006 closed. ``CREATE OR REPLACE FUNCTION``
preserves the existing ACL, so the owner-only ``{keystone=X/keystone}`` this
revision inherits is what it leaves behind;
``tests/schema/test_budget_reservations.py::test_the_ledger_mutators_refuse_a_direct_call``
reads that privilege back out of the live database and would fail if it moved.

Downgrade
---------
Restores 0005's body byte for byte, message included -- verified by ``md5(prosrc)``
returning to its pre-0017 value after ``alembic downgrade -1``. It is a real
inverse, not a no-op, and ``tests/schema/test_migrations.py``'s
``test_upgrade_head_on_an_empty_database_then_downgrade_base`` walks the whole
chain back down through it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_cap_message_states_refusal"
down_revision: str | None = "0016_price_embedding_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The SECURITY DEFINER ledger mutator whose body this revision restates. Named
#: once so the two calls below cannot drift onto different functions.
CHARGE_FUNCTION = "keystone_budget_charge"

#: What the cap branch says after the scope/spent/reserve/cap detail. States the
#: refusal the trigger performs -- true on every caller -- instead of naming a
#: run-level consequence only one of the two callers has.
CAP_MESSAGE_TAIL = "-- this reservation is refused and nothing was charged"

#: 0005's wording, restored verbatim by :func:`downgrade`. False on the reconcile
#: path, which continues with ``rationale NULL``.
CAP_MESSAGE_TAIL_AT_0005 = "-- halt the run"


def _charge_function(cap_message_tail: str) -> None:
    """Restate 0005's ``keystone_budget_charge`` with ``cap_message_tail``.

    The body is 0005's, verbatim, with one interpolation. Written as a single
    parameterised function precisely so that the diff between ``upgrade`` and
    ``downgrade`` is a string and cannot quietly become a change of behaviour:
    there is no second copy of the cap check, the depth guard or the ledger
    ``UPDATE`` for the two directions to disagree about.
    """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {CHARGE_FUNCTION}(p_scope text, p_amount bigint)
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        DECLARE
            ledger_cap bigint;
            ledger_spent bigint;
        BEGIN
            IF pg_trigger_depth() = 0 THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'keystone_budget_charge is callable only from the'
                        || ' budget_reservations triggers, never directly';
            END IF;

            SELECT cap_microusd, spent_microusd INTO ledger_cap, ledger_spent
            FROM budget_ledger WHERE scope = p_scope FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS006',
                    MESSAGE = 'no budget_ledger row for scope ' || coalesce(p_scope, 'NULL')
                        || '; ledger rows (scope and cap) are provisioned by migration/config,'
                        || ' never by the capped party';
            END IF;

            IF ledger_spent + p_amount > ledger_cap THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS006',
                    MESSAGE = 'budget cap exceeded for scope ' || p_scope || ': spent '
                        || ledger_spent || ' + reserve ' || p_amount
                        || ' > cap ' || ledger_cap || ' {cap_message_tail}';
            END IF;

            UPDATE budget_ledger
               SET spent_microusd = ledger_spent + p_amount, updated_at = now()
             WHERE scope = p_scope;
        END;
        $$;
        """
    )


def upgrade() -> None:
    _charge_function(CAP_MESSAGE_TAIL)


def downgrade() -> None:
    _charge_function(CAP_MESSAGE_TAIL_AT_0005)
