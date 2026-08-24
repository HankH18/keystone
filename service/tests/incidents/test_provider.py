"""Provider selection: keyless by default, loud when misconfigured, never silent.

No database, and -- since `prices.yaml` v2 -- **no `table=` override either**.
Every build here reads the committed price table the way a running service does,
because passing a stand-in table was precisely how this package used to report
green against a repository where the feature could not start. What is under test
is the *refusal* behaviour: the branch a deployment meets when it thinks it
configured a live provider and did not, and the branch that stops an unpriced
model from reserving nothing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from recon.budget import PriceTable, load_price_table
from recon.incidents import (
    EMBEDDING_MODELS,
    EMBEDDING_PROVIDER_ENV,
    MOCK_EMBEDDING_MODEL,
    EmbeddingProviderNotConfigured,
    MockEmbeddingProvider,
    build_embedding_provider,
    embedding_provider_name,
)
from tests.incidents.conftest import COMMITTED_EMBEDDING_RATES

#: The models this repository does NOT price. It is **empty**, and that is the
#: assertion: every model `EMBEDDING_MODELS` can select has a rate. It is kept
#: rather than deleted because it is the mechanism a *fourth* provider would be
#: added through -- mirroring `tests/integration/test_route_table.py`'s
#: `NOT_BUILT_YET`, the list must shrink by the gap being *closed*, never by
#: rotting, and it cannot shrink further than this.
PRICE_GAP: dict[str, str] = {}


def test_the_price_gap_is_closed() -> None:
    """Every embedding model R25 can select is priced in the committed table.

    This test replaced `test_the_price_gap_is_still_open`, which recorded the
    inverse and whose own docstring said to do exactly this: *"it is written to
    go red when the gap closes so it cannot become a graveyard"*, and whose
    failure message said *"Delete its line from PRICE_GAP -- and delete the
    embedding_prices fixture's stand-in insert with it, so the tests use the real
    rates."* `prices.yaml` version 2 and migration `0016_price_embedding_models`
    closed it; the stand-in is gone; this is the assertion left standing.

    The rates are pinned, not just their presence. A model priced at some other
    number is a different reservation and a different cap.
    """
    assert PRICE_GAP == {}, f"an embedding model is still unpriced: {PRICE_GAP}"
    table = load_price_table()
    for model, rate in sorted(COMMITTED_EMBEDDING_RATES.items()):
        price = table.price(model)
        assert price.input == Decimal(rate), model
        # `worst_case_microusd` prices the input side at the cache-write rate, so
        # this one is not decoration: get it wrong and every embedding
        # reservation is wrong.
        assert price.cache_write == Decimal(rate), model


def test_every_embedding_model_is_either_priced_or_named_in_the_gap() -> None:
    """The other direction: a new provider cannot be added silently unpriced."""
    priced = set(load_price_table().models)
    for provider, model in sorted(EMBEDDING_MODELS.items()):
        assert model in priced or model in PRICE_GAP, (
            f"EMBEDDING_PROVIDER={provider!r} is priced on {model!r}, which is neither in "
            "prices.yaml nor recorded in PRICE_GAP. An unpriced model reserves nothing."
        )


def test_the_pinned_rates_cover_exactly_the_models_the_module_can_select() -> None:
    """The pinned-rate table must be the model set, no more and no less.

    Replaces `test_the_gap_and_the_stand_in_fixture_name_the_same_models`, which
    compared the gap to the stand-in. With both of those retired the equivalent
    guard is between `EMBEDDING_MODELS` and the rates the tests pin, so a fourth
    provider cannot be added without a rate being pinned for it here.
    """
    assert set(COMMITTED_EMBEDDING_RATES) == set(EMBEDDING_MODELS.values())


def test_mock_is_the_default_with_nothing_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(EMBEDDING_PROVIDER_ENV, raising=False)
    assert embedding_provider_name() == "mock"


def test_a_whitespace_only_provider_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """`if not value` is False for `"   "`.

    A here-doc or a YAML quoting accident produces exactly that, and the failure
    it caused elsewhere in this service was an endpoint whose secret was three
    spaces. Same reading here.
    """
    monkeypatch.setenv(EMBEDDING_PROVIDER_ENV, "   ")
    assert embedding_provider_name() == "mock"


def test_an_unknown_provider_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EMBEDDING_PROVIDER_ENV, "word2vec")
    with pytest.raises(EmbeddingProviderNotConfigured, match="unknown"):
        build_embedding_provider()


@pytest.mark.parametrize("provider", sorted(EMBEDDING_MODELS))
def test_an_unpriced_model_is_still_refused_at_build_time(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """**The fail-closed door, re-pointed rather than removed.**

    This test used to read "the committed table prices no embedding model, so
    `mock` itself refuses ... it is what a deployment gets today". That premise
    is what `prices.yaml` v2 and migration 0016 deliberately falsified, so it
    could no longer be asserted from the committed table -- and deleting it
    would have propped the door open, because *nothing else* covers the branch
    that stops an unpriced model from reserving zero.

    So the missing rate is now supplied by the test instead of by the state of
    the repository: a real :class:`~recon.budget.PriceTable` with this provider's
    model removed. Same code path (`_require_priced` runs before the provider
    branch), same refusal, and it stays true for every provider and every future
    edit to `prices.yaml`.
    """
    monkeypatch.setenv(EMBEDDING_PROVIDER_ENV, provider)
    committed = load_price_table()
    without = PriceTable(
        version=committed.version,
        units=committed.units,
        models={
            name: price
            for name, price in committed.models.items()
            if name != EMBEDDING_MODELS[provider]
        },
    )
    with pytest.raises(EmbeddingProviderNotConfigured, match=r"prices\.yaml"):
        build_embedding_provider(table=without)


def test_the_mock_builds_from_the_committed_table_with_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mock` builds from `prices.yaml` alone -- **no fixture, no `table=`**.

    Deliberately takes no fixture. Passing `table=` was how the old suite got a
    green out of an unpriced repository, so a test that still did it could not
    tell you whether a deployment can run this. This one reads the committed file
    through `recon.budget.price_table`, exactly as a running service does.

    Every credential is deleted rather than blanked, so the keyless claim is
    proved against variables that are genuinely absent.
    """
    monkeypatch.setenv(EMBEDDING_PROVIDER_ENV, "mock")
    for key in ("VOYAGE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    provider = build_embedding_provider()
    assert isinstance(provider, MockEmbeddingProvider)
    assert provider.model == MOCK_EMBEDDING_MODEL


@pytest.mark.parametrize(
    ("provider", "env_var"),
    [("voyage", "VOYAGE_API_KEY"), ("openai", "OPENAI_API_KEY")],
)
def test_a_live_provider_without_its_key_refuses_and_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, provider: str, env_var: str
) -> None:
    """No silent fallback to the mock. **The live call itself is untested.**

    What this covers is the refusal. `VoyageEmbeddingProvider.embed` and
    `OpenAIEmbeddingProvider.embed` are never executed by this suite -- there is
    no key and no network -- so their request shapes are unverified and the
    module says so.
    """
    monkeypatch.setenv(EMBEDDING_PROVIDER_ENV, provider)
    monkeypatch.delenv(env_var, raising=False)
    with pytest.raises(EmbeddingProviderNotConfigured, match=env_var):
        build_embedding_provider()


def test_a_whitespace_only_key_counts_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EMBEDDING_PROVIDER_ENV, "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "   ")
    with pytest.raises(EmbeddingProviderNotConfigured, match="whitespace-only"):
        build_embedding_provider()
