"""Where the ``KS006`` cap message ends up, and what it says when it gets there.

``tests/schema/test_cap_message.py`` pins the sentence at the layer that writes
it. This module pins it at the two layers that *read* it, which is where the
defect had production reach:

* :func:`recon.budget._reserve_once` lifts the raw error text onto
  :class:`recon.budget.BudgetCapExceeded` as ``detail``
  (``detail = str(getattr(exc, "orig", exc)).strip()``), and
  :func:`recon.llm.generate_rationale` hands that straight back as the
  ``cap_hit`` outcome's ``detail``;
* :func:`recon.budget.record_cap_hit` writes the same string into the
  ``audit_log`` row a reviewer reads on the R18 surface.

Until migration 0017 both of those said ``-- halt the run``. On the reconcile
path -- the one that produces them -- nothing halts: the caller gets
``status="cap_hit"`` with ``text=None`` and the proposal lands with
``rationale NULL``. The instruction was false where it was most visible.

These tests do not re-assert that the cap fires, the ledger stays exact, or the
audit row exists. ``test_ledger.py`` owns all three; this module only asserts
what the surviving text says.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import Engine, text

from recon.budget import (
    AUDIT_CAP_HIT,
    KS_CAP_EXCEEDED,
    BudgetCapExceeded,
    reserve,
)
from tests.budget.support import ScopeFactory, run_id_for, unique

MODEL = "mock-rationale-v1"
RESERVE_INPUT_TOKENS = 100
RESERVE_OUTPUT_TOKENS = 384

#: The committed worst case for the bounds above -- ``test_ledger.py``'s
#: ``test_the_reserve_amount_is_the_committed_worst_case`` derives this number
#: from :func:`recon.budget.worst_case_microusd` and would fail if it moved.
RESERVE_AMOUNT = 10_225

#: Migration 0005's wording. Named, never constructed, so the only way it can
#: appear in a message is if the database really says it.
FORBIDDEN_INSTRUCTION = "halt the run"

#: Migration 0017's replacement.
EXPECTED_TAIL = "-- this reservation is refused and nothing was charged"


def _reserve(scope: str, key: str) -> object:
    return reserve(
        idempotency_key=key,
        model=MODEL,
        max_output_tokens=RESERVE_OUTPUT_TOKENS,
        max_input_tokens=RESERVE_INPUT_TOKENS,
        run_id=run_id_for(scope),
    )


def _breach(scope: str) -> BudgetCapExceeded:
    """Consume the whole cap, then reserve once more and return the refusal."""
    _reserve(scope, unique("res"))  # control: the cap is reached legitimately
    with pytest.raises(BudgetCapExceeded) as excinfo:
        _reserve(scope, unique("res"))
    return excinfo.value


def _assert_states_the_refusal(detail: str, scope: str) -> None:
    """The one shape both surfaces must have: no instruction, all the facts."""
    assert FORBIDDEN_INSTRUCTION not in detail, (
        "a cap refusal is telling the operator to halt a run that the reconcile "
        "path continues. Migration 0017 removed that wording; something has put "
        f"it back. Got: {detail!r}"
    )
    assert EXPECTED_TAIL in detail
    assert f"budget cap exceeded for scope {scope}" in detail
    assert f"spent {RESERVE_AMOUNT}" in detail
    assert f"+ reserve {RESERVE_AMOUNT}" in detail
    assert f"> cap {RESERVE_AMOUNT}" in detail


def test_the_exception_detail_recon_llm_returns_states_the_refusal(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """``BudgetCapExceeded.detail`` is what ``recon.llm`` hands back as ``cap_hit``.

    ``generate_rationale`` returns ``RationaleOutcome(detail=exc.detail, ...)``
    without touching the string, so asserting on the exception here asserts on
    the value the caller receives.
    """
    scope = make_scope(RESERVE_AMOUNT)
    exc = _breach(scope)

    assert exc.sqlstate == KS_CAP_EXCEEDED, "message change only -- the SQLSTATE stands"
    _assert_states_the_refusal(exc.detail, scope)


def test_the_persisted_cap_hit_audit_row_states_the_refusal(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The reviewer-facing copy, read back out of ``audit_log``.

    This is the one that mattered: the row outlives the aborted transaction and
    is what a human sees on the R18 surface, so a false instruction here is
    durable rather than momentary.
    """
    scope = make_scope(RESERVE_AMOUNT)
    _breach(scope)

    with owner_engine.connect() as conn:
        stored = conn.execute(
            text(
                "SELECT detail FROM audit_log "
                "WHERE action = :action AND subject = :subject ORDER BY id DESC LIMIT 1"
            ),
            {"action": AUDIT_CAP_HIT, "subject": scope},
        ).scalar_one()

    payload = json.loads(stored) if isinstance(stored, str) else stored
    _assert_states_the_refusal(payload["body"]["detail"], scope)
