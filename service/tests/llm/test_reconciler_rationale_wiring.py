"""The reconciler's production hook, driven against the real budget ledger.

`tests/reconciler/test_rationale_wiring.py` pins *which* hook
`recon.reconciler.rationale_hook_for` hands back. This module pins that the live
one is a real R17 call: it reserves the worst case **before** the provider is
called, calls it, and settles the actual cost **after** -- against real
`budget_ledger` and `budget_reservations` rows, through the real Postgres
trigger that enforces the cap.

Why it matters that this test exists at all: until the hook was wired,
`recon.llm.generate_rationale` had no non-test caller anywhere, so the entire
reserve -> call -> settle chain and the spend cap behind it were exercised only
by tests written against `generate_rationale` directly. The chain was never
reached *from the reconciler*, which is the only thing in the service that has a
reason to produce a rationale. These tests enter through
`rationale_hook_for(...)(packet)` -- the exact call `reconcile()` makes -- and
never call `generate_rationale` themselves.

The provider is `MockProvider`, injected. That is the mock honesty of this
module, stated plainly: **no live model is called here**, so what is proven is
the ledger arithmetic, the ordering, and the failure semantics -- not that
Anthropic returns good prose. The provider seam is the only thing stubbed;
the ledger, the trigger, the grants and the reconciler's own hook are real.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from recon.budget import (
    Usage,
    cost_microusd,
    worst_case_input_tokens,
    worst_case_microusd,
)
from recon.llm import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    MOCK_MODEL_ID,
    SYSTEM_PROMPT,
    MockProvider,
    ProviderError,
    ProviderResult,
)
from recon.reconciler import (
    RATIONALE_KEY_PREFIX,
    ConflictRow,
    build_packet,
    no_rationale,
    rationale_hook_for,
    rationale_prompt,
)
from tests.budget.support import ScopeFactory, env_settings, reservations, run_id_for, spent

FAKE_KEY = "sk-ant-not-a-real-key-0000000000"


@pytest.fixture(autouse=True)
def _settings_isolation() -> Iterator[None]:
    """Per test, not per session: `live_provider` must not outlive its test.

    `tests/llm/conftest.py` re-exports `_settings_cache_isolation`, which clears
    the `get_settings` cache once at session start and once at session end. That
    is not enough here: every test in this module sets `LLM_PROVIDER=anthropic`
    with a fake key, and a cached `Settings` saying so would leak into the rest
    of `tests/llm` and beyond -- `monkeypatch` restores the environment but
    nothing rebuilds the cached object.
    """
    from recon.config import get_settings

    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


def _packet(run_id: str = "ledger-run", fingerprint: str = "fp-ledger"):
    from recon.confidence import load_model
    from recon.reconciler import OSCILLATION_NO_INPUT

    conflict = ConflictRow(
        id=1,
        fingerprint=fingerprint,
        type="C6",
        rule_id="R-006",
        entity_refs=("appdb:student:s1", "crm:contact:c1"),
        sources_involved=("appdb", "crm"),
        disagreeing_fields=("appdb.student.grade", "crm.contact.grade"),
        observed_values={"appdb.student.grade": "7", "crm.contact.grade": "8"},
        oscillating=False,
        status="open",
        escalation_reason=None,
    )
    return build_packet(
        conflict,
        run_id=run_id,
        generation=3,
        model=load_model(),
        candidates={},
        incomplete_sources=(),
        oscillating=False,
        oscillation_source=OSCILLATION_NO_INPUT,
        lineage_rows=0,
        lineage_generations=0,
    )


def _reserve_amount(prompt: str) -> int:
    """What the ledger must have committed before the provider is called."""
    return worst_case_microusd(
        MOCK_MODEL_ID,
        max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        max_input_tokens=worst_case_input_tokens(SYSTEM_PROMPT + prompt),
    )


@pytest.fixture
def live_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """`LLM_PROVIDER=anthropic`, so the hook is the live one.

    The provider object is still injected by each test: the setting is the
    switch, and it has to be the switch, or a test could turn the graded
    `LLM_PROVIDER=mock` path into a spending one by passing an argument.
    """
    env_settings(monkeypatch, LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY=FAKE_KEY)


def test_the_hook_reserves_calls_and_settles_on_the_real_ledger(
    owner_engine: Engine, make_scope: ScopeFactory, live_provider: None
) -> None:
    """One reconciler rationale, end to end, with the numbers checked."""
    packet = _packet()
    reserve_amount = _reserve_amount(rationale_prompt(packet))
    scope = make_scope(reserve_amount * 4)

    hook = rationale_hook_for(run_id_for(scope), provider=MockProvider())
    assert hook is not no_rationale

    text_out = hook(packet)

    assert text_out, "the live hook must return rationale text"
    rows = reservations(owner_engine, scope)
    assert len(rows) == 1, rows
    state, reserved, actual = rows[0]
    assert state == "settled"
    assert reserved == reserve_amount
    assert actual is not None and 0 < actual <= reserved
    assert spent(owner_engine, scope) == actual


def test_the_reservation_is_committed_before_the_provider_is_called(
    make_scope: ScopeFactory, live_provider: None
) -> None:
    """R17's ordering, observed from inside the provider the reconciler drove.

    Post-call accounting is what DESIGN rejects. `tests/llm/test_rationale.py`
    asserts this of `generate_rationale`; this asserts it of the path the
    reconciler actually takes to get there.
    """
    from tests.llm.test_rationale import spent_via_new_connection

    packet = _packet()
    reserve_amount = _reserve_amount(rationale_prompt(packet))
    scope = make_scope(reserve_amount * 4)
    seen: list[int] = []

    def observe(_request: object) -> None:
        seen.append(spent_via_new_connection(scope))

    hook = rationale_hook_for(run_id_for(scope), provider=MockProvider(on_call=observe))
    hook(packet)

    assert seen == [reserve_amount], (
        "the worst case must already be committed to the ledger when the provider "
        "is called from the reconciler's hook"
    )


def test_the_cap_refuses_the_call_and_the_hook_returns_none(
    owner_engine: Engine, make_scope: ScopeFactory, live_provider: None
) -> None:
    """A scope with no room: nothing is called, nothing is charged, no exception.

    This is the brief's absolute at the point that matters -- `reconcile()` gets
    `None` back and the proposal lands with `rationale NULL`.
    """
    packet = _packet()
    scope = make_scope(1)  # 1 microUSD: below any worst case
    called: list[str] = []

    hook = rationale_hook_for(
        run_id_for(scope),
        provider=MockProvider(on_call=lambda _request: called.append("called")),
    )

    # Without this the whole test would pass against the no-op hook, which also
    # returns None and also spends nothing.
    assert hook is not no_rationale
    assert hook(packet) is None
    assert called == [], "the provider was called after the cap refused the reservation"
    assert spent(owner_engine, scope) == 0


def test_a_provider_that_raises_is_swallowed_and_settled(
    owner_engine: Engine, make_scope: ScopeFactory, live_provider: None
) -> None:
    """The documented behaviour: the run continues, and the attempt still costs.

    A failed attempt is not a free attempt -- the reservation is closed through
    the settle path rather than silently released -- so this asserts both halves:
    `None` to the reconciler, and a closed reservation on the ledger.
    """

    class _Broken:
        model = MOCK_MODEL_ID

        def complete(self, request: object, *, max_output_tokens: int) -> ProviderResult:
            raise ProviderError("the provider is down")

    packet = _packet()
    scope = make_scope(_reserve_amount(rationale_prompt(packet)) * 8)

    assert rationale_hook_for(run_id_for(scope), provider=_Broken())(packet) is None

    rows = reservations(owner_engine, scope)
    assert rows, "a failed attempt must still leave its reservation accounted for"
    assert all(state != "open" for state, _, _ in rows), rows


def test_the_same_run_and_fingerprint_replay_instead_of_paying_twice(
    owner_engine: Engine, make_scope: ScopeFactory, live_provider: None
) -> None:
    """The idempotency key is derived, so a re-fired run does not double-charge.

    `RATIONALE_KEY_PREFIX:<run_id>:<fingerprint>` -- no uuid4, no clock. The
    second call collides on the reservation's unique key, `recon.llm` reports it
    as replayed, and the provider is not called again.
    """
    packet = _packet()
    reserve_amount = _reserve_amount(rationale_prompt(packet))
    scope = make_scope(reserve_amount * 8)
    calls: list[str] = []

    hook = rationale_hook_for(
        run_id_for(scope),
        provider=MockProvider(on_call=lambda _request: calls.append("call")),
    )

    assert hook(packet)
    charged = spent(owner_engine, scope)
    assert len(calls) == 1

    second = hook(_packet())
    assert second is None, "a replayed reservation yields no text, and no new call"
    assert len(calls) == 1, "the provider was called again under a replayed key"
    assert spent(owner_engine, scope) == charged
    assert f"{RATIONALE_KEY_PREFIX}:{run_id_for(scope)}:{packet.conflict.fingerprint}"


def test_the_mock_setting_never_reaches_the_ledger(
    owner_engine: Engine, make_scope: ScopeFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The graded path, proven on the ledger rather than argued from the code.

    `LLM_PROVIDER=mock` must leave `budget_ledger.spent_microusd` at zero even
    when a provider is handed in -- the setting is the switch, so no argument can
    turn the deterministic path into a spending one.
    """
    env_settings(monkeypatch, LLM_PROVIDER="mock")
    packet = _packet()
    scope = make_scope(_reserve_amount(rationale_prompt(packet)) * 4)

    hook = rationale_hook_for(run_id_for(scope), provider=MockProvider())

    assert hook is no_rationale
    assert hook(packet) is None
    assert spent(owner_engine, scope) == 0
    assert reservations(owner_engine, scope) == []


def test_the_cost_is_computed_from_provider_reported_usage(
    owner_engine: Engine, make_scope: ScopeFactory, live_provider: None
) -> None:
    """Never estimated. The settled amount equals the price table's answer."""
    packet = _packet()
    scope = make_scope(_reserve_amount(rationale_prompt(packet)) * 4)
    captured: list[Usage] = []

    class _Recording(MockProvider):
        def complete(self, request: object, *, max_output_tokens: int) -> ProviderResult:
            result = super().complete(request, max_output_tokens=max_output_tokens)
            captured.append(result.usage)
            return result

    assert rationale_hook_for(run_id_for(scope), provider=_Recording())(packet)

    assert len(captured) == 1
    assert spent(owner_engine, scope) == cost_microusd(MOCK_MODEL_ID, captured[0])
