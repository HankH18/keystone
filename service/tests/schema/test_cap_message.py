"""The ``KS006`` cap message is a statement of fact, not an instruction.

Migration 0005 ended the cap message with ``-- halt the run``. That sentence
does not stay in the database: :func:`recon.budget._reserve_once` lifts it
verbatim onto :class:`recon.budget.BudgetCapExceeded`, :mod:`recon.llm` returns
it as the ``cap_hit`` outcome's ``detail``, and
:func:`recon.budget.record_cap_hit` writes it into the ``audit_log`` row the R18
reviewer surface reconciles against. So it is persisted operator-facing advice
-- and on the reconcile path, which continues with ``rationale NULL``, it was
advice to do something the code deliberately does not do.

Migration 0017 replaces the message with what the trigger actually performs.
This module pins that at the layer the sentence is *written* in, and
``tests/budget/test_cap_message.py`` pins it at the two layers it is *read* in.

What is deliberately NOT re-asserted here: the SQLSTATE, the raising condition,
the grants and the trigger behaviour are unchanged by 0017 and already have
their own tests (``test_constraints.py``,
``test_budget_reservations.py::test_the_ledger_mutators_refuse_a_direct_call``).
The positive control below asserts ``KS006`` anyway, because a message assertion
against a *different* error would otherwise pass by accident.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from recon.db import ROLE_RECON_WRITER
from tests.schema.conftest import TEST_TAG, RoleTxn, assert_sqlstate

BUDGET_CAP_EXCEEDED = "KS006"

#: Migration 0005's wording. The point of this module is that it is gone from
#: every surface, so it is spelled out once and asserted against, never built.
FORBIDDEN_INSTRUCTION = "halt the run"

#: Migration 0017's wording, as ``CAP_MESSAGE_TAIL`` states it.
EXPECTED_TAIL = "-- this reservation is refused and nothing was charged"

CAP = 1_000_000

RESERVE = (
    "INSERT INTO budget_reservations (scope, idempotency_key, reserve_microusd) "
    "VALUES (:scope, :key, :reserve) RETURNING id"
)


@pytest.fixture
def capped_scope(owner_engine: Engine) -> Iterator[str]:
    """A ledger row provisioned by **ops** with a cap of :data:`CAP`.

    The capped party holds no INSERT on ``budget_ledger`` at all -- asserted
    directly in ``test_budget_reservations.py`` -- so the row cannot come from
    the role that then breaches it.
    """
    scope = f"run:{TEST_TAG}-capmsg-{uuid.uuid4()}"
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) "
                "VALUES (:scope, :cap, 0)"
            ),
            {"scope": scope, "cap": CAP},
        )
    yield scope
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": scope})
        conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": scope})


def test_a_real_cap_breach_reports_the_refusal_and_orders_nothing(
    role_txn: RoleTxn, capped_scope: str
) -> None:
    """Drive the trigger over the cap and read the message it actually raises.

    Not a read of the catalogue and not a string built by the test: a real
    ``recon_writer`` connection, a real INSERT, the real reserve trigger. The
    first reservation is a positive control -- it consumes the whole cap, so the
    second is refused by the cap and not by something else.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text(RESERVE), {"scope": capped_scope, "key": "capmsg-1", "reserve": CAP})
        conn.execute(text(RESERVE), {"scope": capped_scope, "key": "capmsg-2", "reserve": 1})

    assert_sqlstate(excinfo.value, BUDGET_CAP_EXCEEDED)
    message = str(excinfo.value.orig)

    assert FORBIDDEN_INSTRUCTION not in message, (
        "the cap message is telling the operator to halt a run the reconcile "
        "path does not halt; migration 0017 replaced that wording and something "
        f"has put it back. Got: {message!r}"
    )
    assert EXPECTED_TAIL in message

    # Every fact a debugger (and the persisted audit row) depends on survives.
    assert f"budget cap exceeded for scope {capped_scope}" in message
    assert f"spent {CAP}" in message
    assert "+ reserve 1" in message
    assert f"> cap {CAP}" in message


def test_the_charge_functions_body_carries_the_corrected_message(owner_engine: Engine) -> None:
    """The migrated database's function source, read out of ``pg_proc``.

    The behavioural test above proves the message a breach produces *today*.
    This one proves the source it came from, so a future revision that restates
    ``keystone_budget_charge`` -- the way 0017 itself does -- cannot reintroduce
    the instruction on a branch no test happens to drive.
    """
    with owner_engine.connect() as conn:
        body = conn.execute(
            text("SELECT prosrc FROM pg_proc WHERE proname = 'keystone_budget_charge'")
        ).scalar_one()

    assert FORBIDDEN_INSTRUCTION not in body
    assert EXPECTED_TAIL in body
    # The raising condition is 0017's business to leave alone, so pin it here:
    # a "fix" that removed the instruction by removing the check would pass
    # every assertion above.
    assert "IF ledger_spent + p_amount > ledger_cap THEN" in body
    assert "ERRCODE = 'KS006'" in body
