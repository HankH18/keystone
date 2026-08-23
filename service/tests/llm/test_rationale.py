"""The rationale path: rationale-only, keyless by default, never fatal (R17).

The three properties these tests exist to hold down:

* **rationale-only** -- the module returns text and touches nothing else;
* **keyless** -- the default provider needs no key and no network, and no path
  silently requires one;
* **never fatal** -- a provider failure or a cap hit produces ``text=None``, not
  an exception, because the proposal has to land either way.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from recon.budget import (
    AUDIT_LLM_CALL,
    AUDIT_LLM_CALL_FAILED,
    Usage,
    cost_microusd,
    register_alert_sink,
    unregister_alert_sink,
    worst_case_input_tokens,
    worst_case_microusd,
)
from recon.llm import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    MOCK_MODEL_ID,
    STATUS_BUDGET_ERROR,
    STATUS_CAP_HIT,
    STATUS_INTERNAL_ERROR,
    STATUS_OK,
    STATUS_OVERSPEND,
    STATUS_PROVIDER_ERROR,
    STATUS_REPLAYED,
    STATUS_SCOPE_HALTED,
    STATUS_UNPRICED,
    SYSTEM_PROMPT,
    AnthropicProvider,
    MockProvider,
    ProviderError,
    ProviderNotConfigured,
    ProviderNotSent,
    ProviderResult,
    RationaleRequest,
    build_provider,
    generate_rationale,
)
from tests.budget.support import (
    ScopeFactory,
    env_settings,
    reservations,
    run_id_for,
    spent,
    unique,
)

PROMPT = "crm says status 'active'; sis says 'withdrawn' in a later generation."


def _request(subject: str = "conflict-1", prompt: str = PROMPT) -> RationaleRequest:
    return RationaleRequest(subject=subject, prompt=prompt)


def _reserve_amount() -> int:
    return worst_case_microusd(
        MOCK_MODEL_ID,
        max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        max_input_tokens=worst_case_input_tokens(SYSTEM_PROMPT + PROMPT),
    )


# ===========================================================================
# keyless by default
# ===========================================================================
def test_the_default_provider_is_the_mock_and_needs_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``ANTHROPIC_API_KEY`` in the environment at all -- and it still works.

    Deleted, not blanked: an empty-string key would prove a different and weaker
    thing than the brief asks for.
    """
    env_settings(monkeypatch, ANTHROPIC_API_KEY=None, LLM_PROVIDER=None)
    assert "ANTHROPIC_API_KEY" not in os.environ

    provider = build_provider()

    assert isinstance(provider, MockProvider)
    assert provider.model == MOCK_MODEL_ID


def test_selecting_anthropic_without_a_key_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No silent fallback to the mock.

    A deployment that believes it is calling a model while it serves canned text
    is worse than a visible error -- and a fallback would make "the suite runs
    keyless" a claim about the fallback rather than about the default.
    """
    env_settings(monkeypatch, LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY=None)
    with pytest.raises(ProviderNotConfigured) as excinfo:
        build_provider()
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_an_unknown_provider_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    env_settings(monkeypatch, LLM_PROVIDER="hopes-and-dreams")
    with pytest.raises(ProviderNotConfigured):
        build_provider()


def test_no_module_on_this_path_reads_an_api_key_at_import_time(service_root: Path) -> None:
    """A key must never be required to *load* the code, only to call a live provider."""
    source = (service_root / "recon" / "llm.py").read_text(encoding="utf-8")
    # The only mention is inside `build_provider`'s anthropic branch and its docs.
    assert "os.environ" not in source, "llm.py must read configuration through Settings"


def test_the_mock_is_deterministic() -> None:
    """Same prompt in, byte-identical text and identical usage out.

    That is what lets the burst assert an exact integer spend instead of a range.
    """
    first = MockProvider().complete(_request(), max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS)
    second = MockProvider().complete(_request(), max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS)
    assert first.text == second.text
    assert first.usage == second.usage
    assert first.usage.output_tokens > 0 and first.usage.input_tokens > 0


def test_the_mock_respects_max_output_tokens() -> None:
    """A provider cannot report more output than it was allowed to produce.

    If it could, the settlement would exceed the reservation and the trigger
    would refuse it.
    """
    result = MockProvider().complete(_request(), max_output_tokens=3)
    assert result.usage.output_tokens == 3


# ===========================================================================
# the happy path, end to end against the real ledger
# ===========================================================================
def test_a_rationale_reserves_calls_and_settles(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The whole R17 sequence, with the numbers checked against the price table."""
    reserve_amount = _reserve_amount()
    scope = make_scope(reserve_amount * 4)

    outcome = generate_rationale(
        _request(),
        idempotency_key=unique("happy"),
        provider=MockProvider(),
        run_id=run_id_for(scope),
    )

    assert outcome.status == STATUS_OK
    assert outcome.text
    assert outcome.usage is not None
    expected = cost_microusd(MOCK_MODEL_ID, outcome.usage)
    assert outcome.cost_microusd == expected
    assert spent(owner_engine, scope) == expected
    assert reservations(owner_engine, scope) == [("settled", reserve_amount, expected)]


def test_the_reservation_happens_before_the_call(make_scope: ScopeFactory) -> None:
    """Observed from inside the provider: the money is committed first.

    Post-call accounting is what DESIGN rejects, so "before" is the requirement
    and this asserts it from the one place that can see the ordering.
    """
    reserve_amount = _reserve_amount()
    scope = make_scope(reserve_amount * 4)
    seen: list[int] = []

    def observe(_request: RationaleRequest) -> None:
        seen.append(spent_via_new_connection(scope))

    generate_rationale(
        _request(),
        idempotency_key=unique("ordering"),
        provider=MockProvider(on_call=observe),
        run_id=run_id_for(scope),
    )

    assert seen == [reserve_amount], (
        "the worst case must already be committed to the ledger when the provider is called"
    )


def spent_via_new_connection(scope: str) -> int:
    """Read spend on a fresh connection, so it sees only committed state."""
    from recon.budget import ledger_row

    row = ledger_row(scope)
    assert row is not None
    return row.spent_microusd


# ===========================================================================
# failures are never fatal
# ===========================================================================
def test_a_provider_failure_leaves_the_rationale_null_but_the_money_charged(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The proposal is the product; the rationale is a nicety. **The money is not.**

    A 503 arrives *after* the request went out, so the provider may have
    generated the response and will bill for it. This test used to assert
    ``spent == 0`` -- that a post-send failure costs nothing -- which is the
    refund bug itself written down as a contract: it let a timeout storm bill
    unbounded money against a ledger reading zero. The correct contract is that
    the proposal still lands *and* both attempts stay charged at their full
    reservation.
    """
    reserve_amount = _reserve_amount()
    scope = make_scope(reserve_amount * 4)

    class Broken:
        model = MOCK_MODEL_ID

        def complete(self, request: RationaleRequest, *, max_output_tokens: int):
            raise ProviderError("upstream 503")

    outcome = generate_rationale(
        _request(),
        idempotency_key=unique("broken"),
        provider=Broken(),
        max_attempts=2,
        run_id=run_id_for(scope),
    )

    # Unchanged: a provider failure is never fatal and never raises.
    assert outcome.status == STATUS_PROVIDER_ERROR
    assert outcome.text is None
    assert outcome.attempts == 2

    # Changed, deliberately: a 503 is post-send, so nothing is refunded.
    assert spent(owner_engine, scope) == reserve_amount * 2, (
        "a failure that may have reached the provider keeps its whole reservation; "
        "releasing it is the application refunding money it actually spent"
    )
    rows = reservations(owner_engine, scope)
    assert [state for state, _, _ in rows] == ["settled", "settled"]
    assert [actual for _, _, actual in rows] == [reserve_amount, reserve_amount]


def test_a_failure_before_the_request_leaves_the_ledger_clean(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The other side of the rule: proven pre-send, and only then, is free.

    Without this the fix would be indistinguishable from "charge everything",
    which is not the contract -- evidence buys a refund, and this is what
    evidence looks like.
    """
    scope = make_scope(_reserve_amount() * 4)

    class Refused:
        model = MOCK_MODEL_ID

        def complete(self, request: RationaleRequest, *, max_output_tokens: int):
            raise ProviderNotSent("ConnectionRefusedError: [Errno 61] connection refused")

    outcome = generate_rationale(
        _request(),
        idempotency_key=unique("presend"),
        provider=Refused(),
        max_attempts=2,
        run_id=run_id_for(scope),
    )

    assert outcome.status == STATUS_PROVIDER_ERROR
    assert spent(owner_engine, scope) == 0, "a call that provably never happened is free"
    assert [actual for _, _, actual in reservations(owner_engine, scope)] == [0, 0]


def test_a_retry_takes_a_fresh_reservation(owner_engine: Engine, make_scope: ScopeFactory) -> None:
    """Every retry path re-reserves. This is the transient-failure half of that.

    A retry that reused the first attempt's reservation would be a call the cap
    never saw -- which is the bypass R17 forbids.
    """
    reserve_amount = _reserve_amount()
    scope = make_scope(reserve_amount * 4)

    class FlakyOnce:
        model = MOCK_MODEL_ID

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, request: RationaleRequest, *, max_output_tokens: int):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("transient")
            return MockProvider().complete(request, max_output_tokens=max_output_tokens)

    provider = FlakyOnce()
    outcome = generate_rationale(
        _request(),
        idempotency_key=unique("flaky"),
        provider=provider,
        max_attempts=2,
        run_id=run_id_for(scope),
    )

    assert outcome.status == STATUS_OK
    assert outcome.attempts == 2
    assert provider.calls == 2
    rows = reservations(owner_engine, scope)
    assert len(rows) == 2, "the retry took its own trip through the reserve trigger"
    assert [state for state, _, _ in rows] == ["settled", "settled"]
    # The first attempt's `ProviderError("transient")` says nothing about where
    # it failed, so it is post-send and keeps its full reservation. This used to
    # assert `[0, cost]` -- a free failed attempt -- which is exactly the refund
    # the cap cannot survive. A retry after an unknown-outcome failure costing
    # the worst case twice is the honest price of not knowing.
    assert [actual for _, _, actual in rows] == [reserve_amount, outcome.cost_microusd]
    assert spent(owner_engine, scope) == reserve_amount + outcome.cost_microusd


def test_a_cap_hit_returns_a_null_rationale_rather_than_raising(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """At the cap the run halts, the proposal still lands, nothing raises."""
    scope = make_scope(1)  # a cap of one microusd admits nothing

    outcome = generate_rationale(
        _request(),
        idempotency_key=unique("capped"),
        provider=MockProvider(),
        max_attempts=3,
        run_id=run_id_for(scope),
    )

    assert outcome.status == STATUS_CAP_HIT
    assert outcome.text is None
    assert outcome.sqlstate == "KS006"
    assert outcome.attempts == 1, "a cap hit is terminal, not retried"
    assert spent(owner_engine, scope) == 0
    assert reservations(owner_engine, scope) == []


def test_an_unpriced_model_is_refused_before_any_call(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """No call is made for a model the committed table cannot price."""
    scope = make_scope(1_000_000)
    called: list[int] = []

    class Unpriced:
        model = "claude-not-in-the-table"

        def complete(self, request: RationaleRequest, *, max_output_tokens: int):
            called.append(1)
            raise AssertionError("must not be called")

    outcome = generate_rationale(
        _request(),
        idempotency_key=unique("unpriced"),
        provider=Unpriced(),
        run_id=run_id_for(scope),
    )

    assert outcome.status == STATUS_UNPRICED
    assert outcome.text is None
    assert not called, "an unpriced model must not reach the provider"
    assert spent(owner_engine, scope) == 0, "and must not reserve anything either"


# ===========================================================================
# rationale-only
# ===========================================================================
def test_the_llm_module_writes_to_nothing_but_the_ledger_and_the_audit_log(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """R17/DESIGN: the LLM never detects, never scores, never writes.

    Structural, not aspirational: a rationale call is made and the tables it is
    forbidden to touch are counted before and after.
    """
    scope = make_scope(_reserve_amount() * 2)
    forbidden = ("conflicts", "proposals", "entities", "invariant_results", "field_lineage")

    def counts() -> dict[str, int]:
        with owner_engine.connect() as conn:
            return {
                table: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
                for table in forbidden
            }

    before = counts()
    generate_rationale(
        _request(),
        idempotency_key=unique("readonly"),
        provider=MockProvider(),
        run_id=run_id_for(scope),
    )
    assert counts() == before


def test_the_llm_source_contains_no_write_to_a_domain_table(service_root: Path) -> None:
    """A grep-level guard against the property drifting back in.

    ``recon/llm.py`` must contain no SQL at all; every statement it needs comes
    from ``recon.budget``, which writes exactly two tables.
    """
    source = (service_root / "recon" / "llm.py").read_text(encoding="utf-8")
    for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert forbidden not in source, f"recon/llm.py must not contain {forbidden!r}"


# ===========================================================================
# PII
# ===========================================================================
def test_a_logged_prompt_is_redacted(
    monkeypatch: pytest.MonkeyPatch, make_scope: ScopeFactory
) -> None:
    """Prompts carry personal data, so nothing logs one verbatim in safe mode."""
    env_settings(monkeypatch, LOG_MODE="safe")
    scope = make_scope(_reserve_amount() * 2)
    secret = "Dorothea Ravensbourne (dorothea.ravensbourne@example.invalid)"
    recorded: list[str] = []

    class Recorder:
        def info(self, event: str, **kwargs: object) -> None:
            recorded.append(json.dumps({"event": event, **kwargs}, default=str))

        warning = info
        error = info

    monkeypatch.setattr("recon.llm.log", Recorder())
    generate_rationale(
        _request(prompt=f"The record says {secret} and the other source disagrees."),
        idempotency_key=unique("pii"),
        provider=MockProvider(),
        run_id=run_id_for(scope),
    )

    assert recorded, "the call logged nothing at all"
    blob = "\n".join(recorded)
    assert secret not in blob
    assert "dorothea.ravensbourne@example.invalid" not in blob
    assert "body_sha256" in blob, "safe mode stores hash + redacted preview"


# ===========================================================================
# the live provider's request shape (exercised without a key)
# ===========================================================================
class _FakeUsage:
    input_tokens = 120
    output_tokens = 45
    cache_read_input_tokens = 300
    cache_creation_input_tokens = 0


class _FakeBlock:
    type = "text"
    text = "because the later generation replaced the value"


class _FakeResponse:
    usage = _FakeUsage()
    content = (_FakeBlock(),)


class _FakeMessages:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    def create(self, **kwargs):
        self.kwargs = kwargs
        return _FakeResponse()


class _FakeClient:
    def __init__(self) -> None:
        self.messages = _FakeMessages()


def _call_with_fake(model: str) -> tuple[dict, ProviderResult]:
    client = _FakeClient()
    provider = AnthropicProvider(model=model, api_key="unused-in-this-test", _client=client)
    result = provider.complete(_request(), max_output_tokens=256)
    return client.messages.kwargs, result


def test_the_live_request_caches_the_frozen_system_prompt() -> None:
    """Caching is a prefix match, so the breakpoint goes on the stable half."""
    kwargs, _ = _call_with_fake("claude-opus-5")
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["max_tokens"] == 256
    system = kwargs["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert system[0]["text"] == SYSTEM_PROMPT
    assert kwargs["messages"] == [{"role": "user", "content": PROMPT}]


def test_temperature_is_omitted_on_models_that_reject_it() -> None:
    """`temperature=0` is a 400 on Opus 5 and the rest of that family.

    Sampling parameters were removed there, so "as deterministic as the API
    allows" means sending none -- and sending one would break every call to the
    configured default model.
    """
    kwargs, _ = _call_with_fake("claude-opus-5")
    assert "temperature" not in kwargs
    assert kwargs["output_config"] == {"effort": "low"}


def test_temperature_zero_is_sent_where_the_model_still_accepts_it() -> None:
    kwargs, _ = _call_with_fake("claude-haiku-4-5")
    assert kwargs["temperature"] == 0
    assert "output_config" not in kwargs


def test_provider_reported_usage_is_mapped_verbatim() -> None:
    """Cost is computed from what the provider reported, never re-estimated."""
    _, result = _call_with_fake("claude-opus-5")
    assert result.usage == Usage(
        input_tokens=120, output_tokens=45, cache_read_tokens=300, cache_write_tokens=0
    )
    assert result.text == "because the later generation replaced the value"
    # 120x5 + 45x25 + 300x0.5 = 600 + 1125 + 150
    assert cost_microusd("claude-opus-5", result.usage) == 1875


def test_a_provider_exception_becomes_a_provider_error() -> None:
    """SDK errors are wrapped, so the caller's one handler covers all of them."""

    class Exploding(_FakeClient):
        def __init__(self) -> None:
            super().__init__()

            def boom(**_kwargs):
                raise RuntimeError("connection reset")

            self.messages.create = boom  # type: ignore[method-assign]

    provider = AnthropicProvider(model="claude-opus-5", api_key="x", _client=Exploding())
    with pytest.raises(ProviderError) as excinfo:
        provider.complete(_request(), max_output_tokens=64)
    assert "connection reset" in str(excinfo.value)


def test_an_unprovisioned_scope_is_not_fatal_and_is_not_called_a_cap_hit(
    owner_engine: Engine,
) -> None:
    """A misconfigured scope still lets the proposal land -- under its own status."""
    outcome = generate_rationale(
        _request(),
        idempotency_key=unique("noscope"),
        provider=MockProvider(),
        run_id=unique("never-provisioned"),
    )
    assert outcome.status == STATUS_BUDGET_ERROR
    assert outcome.text is None


# ===========================================================================
# the refund bug: a call that FAILS AFTER GENERATING must stay charged
# ===========================================================================
class GeneratedThenTimedOut:
    """A provider that does the work and then fails to deliver it.

    This is the shape of the bug, not a stand-in for it: the model produced
    output (so the provider will bill for it) and the failure happened on the
    way back. ``generated`` records that the work really was done, so the test
    is asserting about a call that provably cost money.
    """

    model = MOCK_MODEL_ID

    def __init__(self) -> None:
        self.generated = 0

    def complete(self, request: RationaleRequest, *, max_output_tokens: int):
        MockProvider().complete(request, max_output_tokens=max_output_tokens)
        self.generated += 1
        raise ProviderError("APITimeoutError: request timed out after generation")


def test_a_call_that_failed_after_generating_leaves_its_reservation_charged(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """THE blocker. A post-generation timeout must not be refunded.

    The provider did the work and will bill for it. Settling at zero releases
    the whole reservation, so real spend is charged zero, the loop retries, and
    a timeout storm bills unbounded money against a ledger that reads zero --
    a cap defeated without touching the database at all.
    """
    reserve_amount = _reserve_amount()
    scope = make_scope(reserve_amount * 4)
    provider = GeneratedThenTimedOut()

    outcome = generate_rationale(
        _request(),
        idempotency_key=unique("post-send"),
        provider=provider,
        max_attempts=1,
        run_id=run_id_for(scope),
    )

    assert provider.generated == 1, "the provider really did generate a response"
    assert outcome.status == STATUS_PROVIDER_ERROR
    assert outcome.text is None
    assert spent(owner_engine, scope) == reserve_amount, (
        "the reservation stays CHARGED: absence of evidence that the money was "
        "not spent is not evidence of a refund"
    )
    assert reservations(owner_engine, scope) == [("settled", reserve_amount, reserve_amount)], (
        "settled at the full reservation, releasing nothing"
    )


def test_a_storm_of_post_generation_failures_still_halts_at_the_cap(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The storm the refund bug made unbounded. Now it walks into the cap.

    Ten calls that each generate and then time out, against a cap sized for
    three. With failures refunded, all ten would have proceeded and the ledger
    would still read zero. With failures charged, the cap admits exactly three
    and refuses the rest with ``KS006``.
    """
    reserve_amount = _reserve_amount()
    admitted = 3
    scope = make_scope(reserve_amount * admitted)
    provider = GeneratedThenTimedOut()

    outcomes = [
        generate_rationale(
            _request(subject=f"storm-{index}"),
            idempotency_key=unique(f"storm-{index}"),
            provider=provider,
            max_attempts=1,
            run_id=run_id_for(scope),
        )
        for index in range(10)
    ]

    failed = [item for item in outcomes if item.status == STATUS_PROVIDER_ERROR]
    capped = [item for item in outcomes if item.status == STATUS_CAP_HIT]

    assert len(failed) == admitted, f"the cap admitted {len(failed)}, expected {admitted}"
    assert len(capped) == 10 - admitted, "the rest must be refused by the cap"
    assert {item.sqlstate for item in capped} == {"KS006"}
    assert provider.generated == admitted, "no call happened without a reservation"
    assert spent(owner_engine, scope) == reserve_amount * admitted, (
        "the ledger holds the worst case of every call that may have been billed"
    )
    assert spent(owner_engine, scope) <= reserve_amount * admitted, "never past the cap"


def test_a_failed_call_is_not_audited_as_a_free_successful_one(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """R18: the audit log reconciles with the dashboard.

    1,000 timeouts written as ``llm_call`` with cost 0 and tokens 0/0 read as
    1,000 free calls, which is both wrong and the exact opposite of what
    happened.
    """
    scope = make_scope(_reserve_amount() * 4)
    key = unique("auditfail")

    generate_rationale(
        _request(),
        idempotency_key=key,
        provider=GeneratedThenTimedOut(),
        max_attempts=1,
        run_id=run_id_for(scope),
    )

    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT action, cost_microusd, detail FROM audit_log "
                "WHERE subject LIKE :p ORDER BY id"
            ),
            {"p": f"{key}%"},
        ).fetchall()

    actions = [row.action for row in rows]
    assert AUDIT_LLM_CALL not in actions, "a failed call must not look like a success"
    assert actions == [AUDIT_LLM_CALL_FAILED]
    assert rows[0].cost_microusd == _reserve_amount()
    reason = rows[0].detail["body"]["reason"]
    assert "ProviderError: APITimeoutError" in reason, "the audit row says WHAT failed"
    assert "unknown" in reason, "and that the outcome -- and therefore the cost -- was unknown"


# ===========================================================================
# an overspend halts the run and does not report ok
# ===========================================================================
def test_a_settlement_above_the_reservation_does_not_report_ok(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """A run that spent more than it reserved must not come back ``ok``.

    The reported cost is settled at the reservation because the database
    refuses more -- but the shortfall is money the ledger will never see, so
    the cap's arithmetic is now known to under-count and the run halts.
    """
    reserve_amount = _reserve_amount()
    scope = make_scope(reserve_amount * 4)

    class OverReporting:
        """Returns text, then reports far more usage than was reserved."""

        model = MOCK_MODEL_ID

        def complete(self, request: RationaleRequest, *, max_output_tokens: int):
            result = MockProvider().complete(request, max_output_tokens=max_output_tokens)
            return ProviderResult(
                text=result.text,
                usage=Usage(input_tokens=10**7, output_tokens=10**7),
                model=self.model,
            )

    key = unique("overspend")
    alerts: list[dict] = []
    register_alert_sink(alerts.append)
    try:
        outcome = generate_rationale(
            _request(),
            idempotency_key=key,
            provider=OverReporting(),
            max_attempts=2,
            run_id=run_id_for(scope),
        )
    finally:
        unregister_alert_sink(alerts.append)

    assert outcome.status == STATUS_OVERSPEND
    assert outcome.status != STATUS_OK
    assert outcome.text is None, "an overspending call does not deliver a rationale"
    assert outcome.attempts == 1, "an overspend is terminal, not retried"
    assert outcome.cost_microusd == reserve_amount
    assert spent(owner_engine, scope) == reserve_amount, "every microusd it can hold"

    # Audited and alerted the same way a cap hit is, so the shortfall is
    # visible rather than dropped.
    with owner_engine.connect() as conn:
        overflow = conn.execute(
            text(
                "SELECT detail FROM audit_log WHERE action = 'budget_settle_overflow' "
                "AND subject = :s"
            ),
            {"s": f"{key}#attempt1"},
        ).one()
    assert overflow.detail["body"]["shortfall_microusd"] > 0
    assert [event["event"] for event in alerts] == [
        "budget.settle_overflow",
        "budget.scope_halted",
    ]
    assert alerts[0]["shortfall_microusd"] == overflow.detail["body"]["shortfall_microusd"]

    # MAJOR 5: and the halt HALTS. The previous version returned this status and
    # nothing consumed it -- 20 consecutive calls, each overspending by
    # ~30,000,000 microusd, all proceeded. The scope itself now refuses.
    after = generate_rationale(
        _request(),
        idempotency_key=unique("after-overspend"),
        provider=MockProvider(),  # would succeed instantly if it were allowed to run
        max_attempts=3,
        run_id=run_id_for(scope),
    )
    assert after.status == STATUS_SCOPE_HALTED
    assert after.text is None
    assert after.attempts == 1, "a halted scope is terminal, not retried"
    assert spent(owner_engine, scope) == reserve_amount, "and nothing more was charged"


# ===========================================================================
# a replayed idempotency key
# ===========================================================================
def test_a_replayed_idempotency_key_does_not_raise_and_does_not_recall(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """``generate_rationale`` says "never raises". A repeated key used to raise.

    ``reserve`` re-raised every non-KS006 ``DBAPIError``, so a replayed key
    surfaced as a raw ``UniqueViolation`` straight through a function documented
    never to raise. It is an idempotent no-op: nothing is charged again, and the
    paid call is NOT repeated -- repeating it would be a call the cap never saw.
    """
    reserve_amount = _reserve_amount()
    scope = make_scope(reserve_amount * 6)
    key = unique("replay")

    class Counting:
        model = MOCK_MODEL_ID

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, request: RationaleRequest, *, max_output_tokens: int):
            self.calls += 1
            return MockProvider().complete(request, max_output_tokens=max_output_tokens)

    provider = Counting()
    first = generate_rationale(
        _request(),
        idempotency_key=key,
        provider=provider,
        max_attempts=1,
        run_id=run_id_for(scope),
    )
    assert first.status == STATUS_OK
    after_first = spent(owner_engine, scope)

    second = generate_rationale(
        _request(),
        idempotency_key=key,
        provider=provider,
        max_attempts=1,
        run_id=run_id_for(scope),
    )

    assert second.status == STATUS_REPLAYED, "a replay is a documented outcome, not an error"
    assert second.text is None
    assert provider.calls == 1, "the paid call is not repeated"
    assert spent(owner_engine, scope) == after_first, "and nothing is charged again"
    assert len(reservations(owner_engine, scope)) == 1


# ===========================================================================
# the whitespace key
# ===========================================================================
def test_a_whitespace_api_key_is_treated_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """``if not key`` is False for ``"   "``, so a blank key built a live provider.

    That is the silent-misconfiguration failure ``build_provider`` exists to
    prevent: a mis-pasted dashboard secret produced a provider that looked
    configured and failed later as an opaque 401 at the first call.
    """
    env_settings(monkeypatch, LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="   ")
    with pytest.raises(ProviderNotConfigured) as excinfo:
        build_provider()
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


def test_a_real_key_is_stripped_before_use(monkeypatch: pytest.MonkeyPatch) -> None:
    """And a key with stray whitespace around it still works, stripped."""
    env_settings(monkeypatch, LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="  sk-test-value \n")
    provider = build_provider()
    assert isinstance(provider, AnthropicProvider)
    assert provider.api_key == "sk-test-value"


# ===========================================================================
# where a failure happened -- the classifier itself
# ===========================================================================
def test_an_unrecognised_failure_is_treated_as_post_send() -> None:
    """Fail closed: the whitelist is the only way to a refund."""
    from recon.llm import _reached_provider

    assert _reached_provider(RuntimeError("who knows")) is True
    assert _reached_provider(ProviderError("upstream 503")) is True
    assert _reached_provider(TimeoutError("read timed out")) is True
    assert _reached_provider(ConnectionResetError("reset after send")) is True


def test_a_proven_pre_send_failure_is_recognised_through_the_wrapper() -> None:
    """The SDK's exception survives as ``__cause__``, so the wrapper is transparent."""
    from recon.llm import _reached_provider

    assert _reached_provider(ProviderNotSent("no route")) is False
    assert _reached_provider(ConnectionRefusedError("[Errno 61]")) is False

    class AuthenticationError(Exception):
        status_code = 401

    try:
        try:
            raise AuthenticationError("invalid x-api-key")
        except AuthenticationError as inner:
            raise ProviderError("AuthenticationError: invalid x-api-key") from inner
    except ProviderError as wrapped:
        assert _reached_provider(wrapped) is False


# ===========================================================================
# BLOCKER 1: a successful call that reports no usage is not a free call
# ===========================================================================
def test_a_success_that_reports_no_usage_is_charged_the_full_reservation(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """A provider that returns real text and no usage BILLS you. Charge it.

    ``cost_microusd(model, Usage())`` is 0 and settlement used to accept it, so a
    provider whose usage block is absent, zeroed, or omits ``output_tokens``
    produced 100 successful, text-returning, billed calls at a cost of zero --
    with no database access required to arrange it.

    Text with no usage is not a cost of zero. It is an UNKNOWN, and unknown
    charges the worst case. The rationale still comes back, because the text is
    real and the proposal is the product: what changes is the money.
    """
    reserve_amount = _reserve_amount()

    class SilentUsage:
        """Returns genuine text and reports nothing about what it billed."""

        model = MOCK_MODEL_ID

        def __init__(self, usage: Usage) -> None:
            self.usage = usage

        def complete(self, request: RationaleRequest, *, max_output_tokens: int):
            result = MockProvider().complete(request, max_output_tokens=max_output_tokens)
            return ProviderResult(text=result.text, usage=self.usage, model=self.model)

    for index, usage in enumerate(
        (
            Usage(),
            Usage(input_tokens=500, output_tokens=0),
            Usage(input_tokens=0, output_tokens=500),
        )
    ):
        scope = make_scope(reserve_amount * 2)
        outcome = generate_rationale(
            _request(),
            idempotency_key=unique(f"silent{index}"),
            provider=SilentUsage(usage),
            max_attempts=1,
            run_id=run_id_for(scope),
        )

        assert outcome.status == STATUS_OK, "the text is real; it is the cost that is unknown"
        assert outcome.text is not None
        assert outcome.cost_microusd == reserve_amount, (
            f"usage {usage} is not evidence of a cost, so the FULL reservation is charged"
        )
        assert spent(owner_engine, scope) == reserve_amount
        assert reservations(owner_engine, scope) == [("settled", reserve_amount, reserve_amount)], (
            "nothing was released for a call whose actual cost is unknown"
        )


def test_a_success_that_reports_real_usage_still_releases_the_difference(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The other side of BLOCKER 1, so the rule is not merely "charge everything"."""
    reserve_amount = _reserve_amount()
    scope = make_scope(reserve_amount * 2)

    outcome = generate_rationale(
        _request(),
        idempotency_key=unique("honest"),
        provider=MockProvider(),
        max_attempts=1,
        run_id=run_id_for(scope),
    )

    assert outcome.status == STATUS_OK
    assert 0 < outcome.cost_microusd < reserve_amount, "reported usage really is priced"
    assert spent(owner_engine, scope) == outcome.cost_microusd


# ===========================================================================
# MAJOR 4: "never raises" means never
# ===========================================================================
def test_an_empty_run_id_does_not_raise(make_scope: ScopeFactory) -> None:
    """``/internal/reconcile`` supplies ``run_id``, and ``run_scope("")`` raised.

    A caller written against "never raises" has no ``except`` clause, so a blank
    run id took down the run that "the rationale is a nicety" exists to keep
    alive. It is now an outcome with a reason.
    """
    outcome = generate_rationale(
        _request(),
        run_id="",
        idempotency_key=unique("blank-run"),
        provider=MockProvider(),
        max_attempts=1,
    )
    assert outcome.status == STATUS_BUDGET_ERROR
    assert outcome.text is None
    assert "run_id" in (outcome.detail or "")


def test_a_database_failure_inside_reserve_does_not_raise(make_scope: ScopeFactory) -> None:
    """Any DBAPIError that is not KS006/23505 used to propagate.

    A pool timeout, a reset connection, a deadlock, a statement timeout: every
    one of them escaped a function documented never to raise. The SQLSTATE is
    carried on the outcome so "the cap refused this" stays a claim about the cap.
    """
    from sqlalchemy.exc import DBAPIError

    import recon.llm as llm_module

    class Orig(Exception):
        sqlstate = "57014"  # query_canceled: a statement timeout

    def exploding_reserve(**kwargs: object):
        raise DBAPIError("SELECT 1", {}, Orig("canceling statement due to statement timeout"))

    original = llm_module.reserve
    llm_module.reserve = exploding_reserve  # type: ignore[assignment]
    try:
        outcome = generate_rationale(
            _request(),
            run_id="run-1",
            idempotency_key=unique("dbapi"),
            provider=MockProvider(),
            max_attempts=1,
        )
    finally:
        llm_module.reserve = original  # type: ignore[assignment]

    assert outcome.status == STATUS_BUDGET_ERROR
    assert outcome.sqlstate == "57014"
    assert outcome.sqlstate != "KS006", "a statement timeout must not read as the cap holding"


def test_an_unanticipated_failure_is_an_outcome_and_not_a_traceback() -> None:
    """The totality backstop. A function documented never to raise, never raises."""
    import recon.llm as llm_module

    def exploding_reserve(**kwargs: object):
        raise MemoryError("something nobody wrote a branch for")

    original = llm_module.reserve
    llm_module.reserve = exploding_reserve  # type: ignore[assignment]
    try:
        outcome = generate_rationale(
            _request(),
            run_id="run-1",
            idempotency_key=unique("boom"),
            provider=MockProvider(),
            max_attempts=1,
        )
    finally:
        llm_module.reserve = original  # type: ignore[assignment]

    assert outcome.status == STATUS_INTERNAL_ERROR
    assert outcome.text is None
    assert "MemoryError" in (outcome.detail or "")


def test_generate_rationale_declares_no_raising_path(service_root: Path) -> None:
    """Every ``return`` from the entry point is an outcome; the wrapper is total."""
    import ast

    source = (service_root / "recon" / "llm.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    entry = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "generate_rationale"
    )
    handlers = [
        handler
        for node in ast.walk(entry)
        if isinstance(node, ast.Try)
        for handler in node.handlers
    ]
    assert handlers, "the entry point must catch something"
    assert any(
        isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
        for handler in handlers
    ), "generate_rationale is documented never to raise, so it catches Exception"
    assert not [node for node in ast.walk(entry) if isinstance(node, ast.Raise)], (
        "and it raises nothing itself"
    )
