"""The committed price table: exact arithmetic, and no zero-cost default (R17).

These are the only tests in the package that need no database. They are here
because the price table is the input to every reservation: if a model can be
priced at zero, the cap is unreachable for that model and everything the burst
test proves stops applying to it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from recon.budget import (
    MICROUSD_PER_USD,
    UnknownModelError,
    Usage,
    cap_microusd_from_env,
    cost_microusd,
    load_price_table,
    price_table,
    worst_case_input_tokens,
    worst_case_microusd,
)

MOCK_MODEL = "mock-rationale-v1"


def test_every_committed_model_is_priced_above_zero() -> None:
    """No model may be free. A free model is an uncapped model.

    Not a style check: the reservation is ``price x tokens``, so a zero rate
    reserves zero, the ledger never moves, and the cap that the whole burst test
    exists to prove never fires for that model.
    """
    table = price_table()
    assert table.models, "the committed price table is empty"
    for model, price in table.models.items():
        assert price.input > 0, f"{model} has a zero input rate"
        assert price.output > 0, f"{model} has a zero output rate"
        assert price.cache_read > 0, f"{model} has a zero cache-read rate"
        assert price.cache_write > 0, f"{model} has a zero cache-write rate"


def test_an_unknown_model_fails_loudly_and_is_never_free() -> None:
    """An unpriced model raises. It does not cost 0, and it does not warn."""
    with pytest.raises(UnknownModelError) as excinfo:
        cost_microusd("claude-not-a-real-model", Usage(input_tokens=1_000_000))
    assert "not in the committed price table" in str(excinfo.value)

    with pytest.raises(UnknownModelError):
        worst_case_microusd(
            "claude-not-a-real-model", max_output_tokens=1000, max_input_tokens=1000
        )


def test_the_worked_example_from_the_ticket() -> None:
    """tokens -> price table -> microusd, computed the way production computes it.

    Opus 5 rates are 5 / 25 / 0.5 / 6.25 microusd per token::

        1,200 uncached input   x 5      =  6,000
          800 cached-read      x 0.5    =    400
          400 cache-write      x 6.25   =  2,500
          512 output           x 25     = 12,800
                                          ------
                                          21,700 microusd  ($0.0217)
    """
    usage = Usage(
        input_tokens=1200, output_tokens=512, cache_read_tokens=800, cache_write_tokens=400
    )
    assert cost_microusd("claude-opus-5", usage) == 21_700


def test_rounding_is_always_up() -> None:
    """A fractional microusd rounds up, so rounding can only over-charge.

    Rounding down a fraction on every call is a slow leak past a cap that is
    otherwise exact -- the direction matters more than the magnitude.
    """
    # 1 cache-read token on Opus 5 costs 0.5 microusd.
    assert cost_microusd("claude-opus-5", Usage(cache_read_tokens=1)) == 1
    assert cost_microusd("claude-opus-5", Usage(cache_read_tokens=3)) == 2  # 1.5 -> 2


def test_worst_case_prices_input_at_the_dearest_rate() -> None:
    """The reservation must bound the call, so input is priced at cache-write.

    A prompt-cached call bills its first pass at 1.25x input. Reserving at the
    plain input rate would leave a settlement the reservation cannot absorb, and
    the database would refuse it.
    """
    reserve = worst_case_microusd("claude-opus-5", max_output_tokens=512, max_input_tokens=1000)
    assert reserve == 19_050  # 1000 x 6.25 + 512 x 25


def test_worst_case_input_tokens_is_a_real_upper_bound() -> None:
    """One token per UTF-8 byte, plus framing -- a ceiling, not an estimate.

    A byte-level BPE tokenizer merges bytes into tokens and never splits one, so
    it cannot emit more tokens than the input has bytes. The multibyte case is
    the one a "4 characters per token" rule of thumb gets wrong, so it is
    asserted explicitly.
    """
    assert worst_case_input_tokens("", framing=0) == 0
    assert worst_case_input_tokens("hello", framing=0) == 5
    # 4 characters, 12 UTF-8 bytes: the bound follows the bytes.
    assert worst_case_input_tokens("日本語だ", framing=0) == 12
    assert worst_case_input_tokens("hello") == 5 + 64  # default framing


def test_worst_case_bounds_the_mock_providers_real_usage() -> None:
    """The bound must hold for the provider the graded test actually runs.

    If it did not, the settlement would exceed the reservation and the trigger
    would refuse it -- so this is the assertion that keeps the burst honest.
    """
    from recon.llm import SYSTEM_PROMPT, MockProvider, RationaleRequest

    request = RationaleRequest(subject="c-1", prompt="Two systems disagree about a value.")
    result = MockProvider().complete(request, max_output_tokens=384)
    reserved = worst_case_microusd(
        MOCK_MODEL,
        max_output_tokens=384,
        max_input_tokens=worst_case_input_tokens(SYSTEM_PROMPT + request.prompt),
    )
    assert cost_microusd(MOCK_MODEL, result.usage) <= reserved


def test_cap_parsing_matches_the_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Money is parsed through Decimal and never a float, exactly as 0005 does."""
    monkeypatch.setenv("KEYSTONE_TEST_CAP", "5.00")
    assert cap_microusd_from_env("KEYSTONE_TEST_CAP", "1.00") == 5 * MICROUSD_PER_USD
    monkeypatch.setenv("KEYSTONE_TEST_CAP", "0.10")
    assert cap_microusd_from_env("KEYSTONE_TEST_CAP", "1.00") == 100_000
    # Malformed and negative fall back to the documented default, never to zero
    # and never to something enormous.
    monkeypatch.setenv("KEYSTONE_TEST_CAP", "not-money")
    assert cap_microusd_from_env("KEYSTONE_TEST_CAP", "1.00") == MICROUSD_PER_USD
    monkeypatch.setenv("KEYSTONE_TEST_CAP", "-9")
    assert cap_microusd_from_env("KEYSTONE_TEST_CAP", "1.00") == MICROUSD_PER_USD


def test_rates_are_exact_decimals_not_floats() -> None:
    """0.1 + 0.2 problems have no place in a ledger."""
    price = price_table().price("claude-opus-5")
    assert isinstance(price.cache_write, Decimal)
    assert price.cache_write == Decimal("6.25")


def test_a_malformed_table_is_refused(tmp_path) -> None:
    """A table missing a rate is an error, not a partially-priced model."""
    broken = tmp_path / "prices.yaml"
    broken.write_text('version: 9\nmodels:\n  m:\n    input: "1"\n', encoding="utf-8")
    with pytest.raises(UnknownModelError) as excinfo:
        load_price_table(broken)
    assert "missing rate" in str(excinfo.value)

    missing = tmp_path / "absent.yaml"
    with pytest.raises(UnknownModelError):
        load_price_table(missing)


# ===========================================================================
# the rates the DATABASE prices a settlement with must be the committed ones
# ===========================================================================
def test_the_seeded_database_rates_are_the_committed_price_table(owner_engine) -> None:
    """Migration 0010's ``budget_model_prices`` must not drift from ``prices.yaml``.

    Since 0010 the *settled amount* is computed by the settle trigger from
    ``budget_model_prices``, and ``reported_microusd`` -- the figure that decides
    whether a call overspent -- is still computed in Python from the committed
    file. Two sources for one number is exactly the drift the shared price table
    exists to prevent, so this asserts they are the same set of rates, model for
    model and rate for rate.

    If this fails, the fix is a NEW migration that re-seeds the table. 0010 is
    immutable history and edits to ``prices.yaml`` do not reach the database on
    their own -- which is deliberate (a rate change should be reviewed and
    versioned) and is the whole reason this test exists to say so out loud.
    """
    from sqlalchemy import text

    committed = price_table()
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT model, input_rate, output_rate, cache_read_rate, cache_write_rate "
                "FROM budget_model_prices ORDER BY model"
            )
        ).all()

    seeded = {row.model: row for row in rows}
    assert set(seeded) == set(committed.models), (
        "budget_model_prices and prices.yaml name different models: "
        f"only in the database {sorted(set(seeded) - set(committed.models))}, "
        f"only in the file {sorted(set(committed.models) - set(seeded))}"
    )
    for model, price in sorted(committed.models.items()):
        row = seeded[model]
        assert Decimal(row.input_rate) == price.input, model
        assert Decimal(row.output_rate) == price.output, model
        assert Decimal(row.cache_read_rate) == price.cache_read, model
        assert Decimal(row.cache_write_rate) == price.cache_write, model


def test_the_database_prices_a_settlement_exactly_as_cost_microusd_does(owner_engine) -> None:
    """Same tokens, same money -- computed by Postgres and by Python separately.

    ``cost_microusd`` sums all four terms and rounds UP once; the settle trigger
    does ``ceil(...)`` over the same sum. A rounding difference of one microusd
    per call is a slow leak in whichever direction it goes, so it is asserted
    against the database's own arithmetic rather than assumed from the shape of
    the expression.
    """
    from sqlalchemy import text

    cases = (
        Usage(input_tokens=40, output_tokens=30),
        Usage(input_tokens=1, output_tokens=1),
        Usage(input_tokens=7, output_tokens=3, cache_read_tokens=11, cache_write_tokens=13),
        Usage(input_tokens=999_999, output_tokens=1, cache_read_tokens=1),
    )
    with owner_engine.connect() as conn:
        for usage in cases:
            derived = conn.execute(
                text(
                    "SELECT ceil(input_rate * :ui + output_rate * :uo "
                    "+ cache_read_rate * :ucr + cache_write_rate * :ucw)::bigint "
                    "FROM budget_model_prices WHERE model = :m"
                ),
                {
                    "ui": usage.input_tokens,
                    "uo": usage.output_tokens,
                    "ucr": usage.cache_read_tokens,
                    "ucw": usage.cache_write_tokens,
                    "m": MOCK_MODEL,
                },
            ).scalar_one()
            assert derived == cost_microusd(MOCK_MODEL, usage), usage
