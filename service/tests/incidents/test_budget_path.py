"""Embedding calls really go through the ledger: reserve before, settle after.

Live database, live triggers, live grants. The acceptance criterion is not "the
code calls `reserve`" -- it is that money moved in `budget_ledger` and that
migration 0010's triggers accepted the shape of both statements. Neither can be
faked from Python: the reserve trigger re-derives the worst case from the
ops-owned rates in `budget_model_prices` and refuses a reservation whose own
arithmetic does not hold, and the settle trigger refuses any amount but the full
reservation for `outcome_unknown`.

The one thing standing in for production here is where the *rates* came from --
see `conftest.embedding_prices`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from recon.budget import (
    BudgetError,
    PriceTable,
    Usage,
    ledger_row,
    worst_case_input_tokens,
    worst_case_microusd,
)
from recon.incidents import (
    AUDIT_EMBEDDING_CALL,
    MOCK_EMBEDDING_MODEL,
    EmbeddingBudgetReplayed,
    EmbeddingResult,
    MockEmbeddingProvider,
    embed_descriptors,
)
from tests.incidents.conftest import run_id_for

DESCRIPTORS = [
    "type C1\nrule R-001\nsources appdb+crm\nfields none\nobs d2_deal_count num.zero",
    "type C6\nrule R-006\nsources appdb+crm\nfields appdb.student.grade\nobs grade 11",
    "type C2\nrule R-002\nsources payments\nfields none\nobs external_ref null",
]


def _reservations(engine: Engine, scope: str) -> list[tuple[str, int, int | None, str | None]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT state::text AS state, reserve_microusd, actual_microusd, "
                "       settle_evidence::text AS evidence, model, "
                "       max_input_tokens, max_output_tokens "
                "  FROM budget_reservations WHERE scope = :s ORDER BY id"
            ),
            {"s": scope},
        ).fetchall()
    return [(r.state, r.reserve_microusd, r.actual_microusd, r.evidence) for r in rows]


def test_a_batch_reserves_before_the_call_and_settles_after(
    ledger_scope: str, embedding_prices: PriceTable, owner_engine: Engine
) -> None:
    """One provider call, one reservation, one settlement, and the ledger moved.

    `spent_microusd` is read before and after. A path that called `reserve` and
    never settled would leave the row `open` and the ledger holding the worst
    case; a path that skipped the ledger entirely would leave it at zero. Both
    are distinguishable from the assertion below, which is the point.
    """
    before = ledger_row(ledger_scope)
    assert before is not None and before.spent_microusd == 0

    vectors, model, dimension = embed_descriptors(
        DESCRIPTORS,
        run_id=run_id_for(ledger_scope),
        provider=MockEmbeddingProvider(),
        table=embedding_prices,
    )
    assert len(vectors) == len(DESCRIPTORS)
    assert model == MOCK_EMBEDDING_MODEL
    assert dimension == 256

    rows = _reservations(owner_engine, ledger_scope)
    assert len(rows) == 1, f"expected exactly one reservation for one batch, got {rows}"
    state, reserved, actual, evidence = rows[0]
    assert state == "settled"
    assert evidence == "outcome_unknown"
    # `outcome_unknown` charges the FULL reservation -- the settle trigger
    # refuses anything else, so this equality is the database's, not Python's.
    assert actual == reserved

    after = ledger_row(ledger_scope)
    assert after is not None
    assert after.spent_microusd == reserved, (
        "the ledger did not move by the reserved amount; the embedding path is not actually metered"
    )


def test_the_reservation_is_priced_on_the_real_worst_case(
    ledger_scope: str, embedding_prices: PriceTable, owner_engine: Engine
) -> None:
    """The amount is the committed rates' worst case, re-derived by the trigger.

    Asserted against `recon.budget`'s own arithmetic rather than a literal, so
    the test cannot be satisfied by a reservation of a plausible-looking size.
    Migration 0010's reserve trigger computes the same number independently and
    refuses the INSERT if the two disagree, so this passing means both agree.
    """
    expected_tokens = sum(worst_case_input_tokens(one) for one in DESCRIPTORS)
    expected_microusd = worst_case_microusd(
        MOCK_EMBEDDING_MODEL,
        max_output_tokens=0,
        max_input_tokens=expected_tokens,
        table=embedding_prices,
    )
    embed_descriptors(
        DESCRIPTORS,
        run_id=run_id_for(ledger_scope),
        provider=MockEmbeddingProvider(),
        table=embedding_prices,
    )
    with owner_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT reserve_microusd, max_input_tokens, max_output_tokens, model "
                "  FROM budget_reservations WHERE scope = :s"
            ),
            {"s": ledger_scope},
        ).one()
    assert row.model == MOCK_EMBEDDING_MODEL
    assert row.max_input_tokens == expected_tokens
    # An embedding response has no generated tokens, and the schema allows the
    # bound to say so (`ck_reservation_token_bounds_nonneg`).
    assert row.max_output_tokens == 0
    assert row.reserve_microusd == expected_microusd


def test_each_batch_is_its_own_reservation(
    ledger_scope: str, embedding_prices: PriceTable, owner_engine: Engine
) -> None:
    """A reservation covers one provider call, so batching is visible in the ledger."""
    embed_descriptors(
        DESCRIPTORS,
        run_id=run_id_for(ledger_scope),
        provider=MockEmbeddingProvider(),
        batch_size=1,
        table=embedding_prices,
    )
    rows = _reservations(owner_engine, ledger_scope)
    assert len(rows) == len(DESCRIPTORS)
    assert {state for state, _, _, _ in rows} == {"settled"}


def test_the_settlement_is_audited_as_an_embedding_call(
    ledger_scope: str, embedding_prices: PriceTable, owner_engine: Engine
) -> None:
    """R18: the spend is in `audit_log`, under its own action, with the reason.

    A distinct action from `llm_call` so embedding spend can be told apart from
    rationale spend, and carrying :data:`recon.incidents.SETTLE_NOTE` so a reader
    who finds `outcome_unknown` next to a call that plainly succeeded is told
    why the priced path could not express it.
    """
    embed_descriptors(
        DESCRIPTORS,
        run_id=run_id_for(ledger_scope),
        provider=MockEmbeddingProvider(),
        table=embedding_prices,
    )
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT actor, detail, cost_microusd FROM audit_log "
                " WHERE action = :action AND subject LIKE :pattern"
            ),
            {"action": AUDIT_EMBEDDING_CALL, "pattern": f"%{ledger_scope.removeprefix('run:')}%"},
        ).fetchall()
    assert len(rows) == 1
    assert rows[0].actor == "system:budget"
    assert rows[0].cost_microusd > 0
    # `recon.logging.audit_detail` wraps the body under `body` and stamps the
    # mode and a digest alongside it, so the members this settlement added are
    # one level down. `embedding_model`, `embedding_dim` and `note` all survive
    # redaction because they are on `recon.privacy.SAFE_KEYS`.
    body = rows[0].detail["body"]
    assert body["embedding_model"] == MOCK_EMBEDDING_MODEL
    assert body["embedding_dim"] == 256
    assert "output tokens" in body["note"]


def test_replaying_a_run_is_refused_rather_than_paying_twice(
    ledger_scope: str, embedding_prices: PriceTable
) -> None:
    """The same `run_id` over the same descriptors meets its own reservation.

    That is what an idempotency key is for: the reservation it names is already
    spent or in flight, so the call must NOT be repeated. Mirrors
    `recon.llm._attempt_rationale`'s replay branch, and it is why every run of
    `cluster_conflicts` needs a fresh `run_id`.
    """
    run_id = run_id_for(ledger_scope)
    embed_descriptors(
        DESCRIPTORS, run_id=run_id, provider=MockEmbeddingProvider(), table=embedding_prices
    )
    with pytest.raises(EmbeddingBudgetReplayed, match="already reserved"):
        embed_descriptors(
            DESCRIPTORS, run_id=run_id, provider=MockEmbeddingProvider(), table=embedding_prices
        )


class _ExplodingProvider:
    """A provider that reaches the transport and then fails."""

    model = MOCK_EMBEDDING_MODEL
    dimension = 256

    def embed(self, texts: list[str]) -> EmbeddingResult:
        raise TimeoutError("read timed out after the request was sent")


class _MiscountingProvider:
    """A provider that returns the wrong number of vectors."""

    model = MOCK_EMBEDDING_MODEL
    dimension = 256

    def embed(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=((1.0,) + (0.0,) * 255,),
            usage=Usage(input_tokens=1),
            model=self.model,
            dimension=self.dimension,
        )


@pytest.mark.parametrize(
    ("provider", "expected"),
    [(_ExplodingProvider(), TimeoutError), (_MiscountingProvider(), Exception)],
)
def test_a_failed_call_still_settles_and_charges_the_full_reservation(
    ledger_scope: str,
    embedding_prices: PriceTable,
    owner_engine: Engine,
    provider: object,
    expected: type[Exception],
) -> None:
    """Failing closed: the provider may have done the work and will bill for it.

    The reservation must not be left `open` (the sweeper would eventually charge
    it anyway, later and less visibly) and must not be released (a timeout storm
    that refunds itself bills unbounded money against a ledger reading zero).
    """
    with pytest.raises(expected):
        embed_descriptors(
            DESCRIPTORS,
            run_id=run_id_for(ledger_scope),
            provider=provider,  # type: ignore[arg-type]
            table=embedding_prices,
        )
    rows = _reservations(owner_engine, ledger_scope)
    assert len(rows) == 1
    state, reserved, actual, evidence = rows[0]
    assert state == "settled"
    assert evidence == "outcome_unknown"
    assert actual == reserved
    after = ledger_row(ledger_scope)
    assert after is not None and after.spent_microusd == reserved


def test_an_empty_run_id_is_refused_before_anything_is_charged(
    embedding_prices: PriceTable,
) -> None:
    """No run id, no per-run ledger scope -- and R17 mandates one."""
    with pytest.raises(ValueError, match="run_id"):
        embed_descriptors(
            DESCRIPTORS, run_id="  ", provider=MockEmbeddingProvider(), table=embedding_prices
        )


def test_a_cap_that_cannot_hold_the_call_stops_it(
    ledger_scope: str, embedding_prices: PriceTable, owner_engine: Engine
) -> None:
    """The cap is the database's, and it refuses the reservation, not the call.

    The ledger row is squeezed to one microusd; `reserve` meets `KS006` inside
    the reserving transaction, nothing is charged, and the embedding never
    happens. This is the acceptance criterion "the real ones must be capped",
    exercised through the mock so it needs no key.
    """
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE budget_ledger SET cap_microusd = 1 WHERE scope = :s"), {"s": ledger_scope}
        )
    with pytest.raises(BudgetError):
        embed_descriptors(
            DESCRIPTORS,
            run_id=run_id_for(ledger_scope),
            provider=MockEmbeddingProvider(),
            table=embedding_prices,
        )
    after = ledger_row(ledger_scope)
    assert after is not None and after.spent_microusd == 0
