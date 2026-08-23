"""Reserve / settle / sweep against the live ledger (R17).

Everything here runs against real Postgres and, where the boundary is the point,
as the real restricted role. The cap is a trigger and a grant; a test that
faked either would prove nothing about the thing being graded.
"""

from __future__ import annotations

import inspect
import os
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from recon.budget import (
    AUDIT_CAP_HIT,
    AUDIT_LLM_CALL_FAILED,
    AUDIT_SWEEP_CHARGED,
    DAILY_SCOPE,
    DAILY_SCOPE_ENV,
    KS_CAP_EXCEEDED,
    KS_RESERVATION_LIFECYCLE,
    MAX_LEASE_SECONDS,
    BudgetCapExceeded,
    BudgetError,
    BudgetOverspend,
    BudgetScopeHalted,
    DegenerateUsage,
    LedgerScopeMissing,
    NeverSent,
    OutcomeUnknown,
    PreSendProof,
    ProviderReportedUsage,
    RealDailyScopeRefused,
    Reservation,
    SettlementRefused,
    Usage,
    ZeroReservationRefused,
    cost_microusd,
    fire_alert,
    halted_scopes,
    ledger_row,
    provision_run_scope,
    register_alert_sink,
    reserve,
    resume_scope,
    run_scope,
    settle,
    settle_capped,
    settle_failed_call,
    sweep_expired_reservations,
    unregister_alert_sink,
    worst_case_microusd,
)
from recon.db import ROLE_RECON_WRITER, role_connection
from tests.budget.support import (
    ScopeFactory,
    audit_count,
    env_settings,
    reservations,
    run_id_for,
    spent,
    unique,
)

MODEL = "mock-rationale-v1"

#: 100 input tokens + 384 output on the mock model. Chosen so the arithmetic in
#: every assertion below is checkable by hand: 100 x 6.25 + 384 x 25 = 10,225.
RESERVE_INPUT_TOKENS = 100
RESERVE_OUTPUT_TOKENS = 384
RESERVE_AMOUNT = 10_225


def _reserve(scope: str, key: str, **kwargs) -> object:
    return reserve(
        idempotency_key=key,
        model=MODEL,
        max_output_tokens=RESERVE_OUTPUT_TOKENS,
        max_input_tokens=RESERVE_INPUT_TOKENS,
        run_id=run_id_for(scope),
        **kwargs,
    )


def test_the_reserve_amount_is_the_committed_worst_case() -> None:
    """The number this file asserts on is the one production computes."""
    assert (
        worst_case_microusd(
            MODEL,
            max_output_tokens=RESERVE_OUTPUT_TOKENS,
            max_input_tokens=RESERVE_INPUT_TOKENS,
        )
        == RESERVE_AMOUNT
    )


# ===========================================================================
# reserve
# ===========================================================================
def test_reserve_charges_the_ledger_before_the_call(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """Worst-case spend lands on the ledger at reservation time, not after.

    DESIGN's whole reason for reserve-then-settle: post-call accounting loses
    the concurrent-burst race, so the money must be committed *before* anyone
    calls a provider.
    """
    scope = make_scope(RESERVE_AMOUNT * 3)
    assert spent(owner_engine, scope) == 0

    _reserve(scope, unique("res"))

    assert spent(owner_engine, scope) == RESERVE_AMOUNT
    assert reservations(owner_engine, scope) == [("open", RESERVE_AMOUNT, None)]


def test_at_the_cap_reserve_raises_ks006_and_leaves_the_ledger_exact(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The refusal is the database's, and it lands the ledger exactly on the cap."""
    scope = make_scope(RESERVE_AMOUNT * 2)
    _reserve(scope, unique("res"))
    _reserve(scope, unique("res"))
    assert spent(owner_engine, scope) == RESERVE_AMOUNT * 2

    with pytest.raises(BudgetCapExceeded) as excinfo:
        _reserve(scope, unique("res"))

    assert excinfo.value.sqlstate == KS_CAP_EXCEEDED
    assert excinfo.value.scope == scope
    assert spent(owner_engine, scope) == RESERVE_AMOUNT * 2, "never past the cap"
    assert len(reservations(owner_engine, scope)) == 2, "the refused row was not stored"


def test_a_cap_hit_writes_an_audit_row_and_fires_the_stubbed_alert(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """R17: at cap -> stop + log + alert. All three, and the audit row survives.

    The audit row is written on a *fresh* connection on purpose: ``KS006``
    aborts the transaction that hit the cap, so a row written inside it would
    roll back with it and the evidence of the cap firing would vanish.
    """
    scope = make_scope(RESERVE_AMOUNT)
    fired: list[dict] = []
    register_alert_sink(fired.append)
    try:
        _reserve(scope, unique("res"))
        with pytest.raises(BudgetCapExceeded):
            _reserve(scope, unique("res"))
    finally:
        unregister_alert_sink(fired.append)

    assert audit_count(owner_engine, action=AUDIT_CAP_HIT, subject=scope) == 1
    assert len(fired) == 1
    assert fired[0]["event"] == "budget.cap_hit"
    assert fired[0]["scope"] == scope
    assert fired[0]["sqlstate"] == KS_CAP_EXCEEDED


def test_a_broken_alert_sink_does_not_break_the_cap() -> None:
    """A failing pager must not turn "the cap held" into "the process died"."""

    def explode(_event: dict) -> None:
        raise RuntimeError("pager is down")

    received: list[dict] = []
    register_alert_sink(explode)
    register_alert_sink(received.append)
    try:
        delivered = fire_alert("budget.cap_hit", {"scope": "x"})
    finally:
        unregister_alert_sink(explode)
        unregister_alert_sink(received.append)
    assert delivered == 1
    assert received and received[0]["scope"] == "x"


# ===========================================================================
# both scopes
# ===========================================================================
def test_both_scopes_are_charged_by_one_reservation(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """R17 mandates per-run AND daily. One call charges both."""
    daily = make_scope(RESERVE_AMOUNT * 10, hint="daily")
    run = make_scope(RESERVE_AMOUNT * 10, hint="run")

    reservation = reserve(
        idempotency_key=unique("both"),
        model=MODEL,
        max_output_tokens=RESERVE_OUTPUT_TOKENS,
        max_input_tokens=RESERVE_INPUT_TOKENS,
        run_id=run_id_for(run),
    )

    assert reservation.scopes == tuple(sorted((daily, run)))
    assert spent(owner_engine, daily) == RESERVE_AMOUNT
    assert spent(owner_engine, run) == RESERVE_AMOUNT


def test_a_run_inside_its_own_cap_is_still_refused_by_the_daily_cap(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The case a per-run-only cap misses: N runs spending N times the day.

    The run scope has room for ten calls. The daily scope has room for one. The
    second call must be refused -- and refused **atomically**, leaving the run
    ledger unmoved, or the run would be charged for a call that never happened.
    """
    daily = make_scope(RESERVE_AMOUNT, hint="daily")
    run = make_scope(RESERVE_AMOUNT * 10, hint="run")
    run_id = run_id_for(run)

    reserve(
        idempotency_key=unique("first"),
        model=MODEL,
        max_output_tokens=RESERVE_OUTPUT_TOKENS,
        max_input_tokens=RESERVE_INPUT_TOKENS,
        run_id=run_id,
    )
    assert spent(owner_engine, daily) == RESERVE_AMOUNT
    assert spent(owner_engine, run) == RESERVE_AMOUNT

    with pytest.raises(BudgetCapExceeded) as excinfo:
        reserve(
            idempotency_key=unique("second"),
            model=MODEL,
            max_output_tokens=RESERVE_OUTPUT_TOKENS,
            max_input_tokens=RESERVE_INPUT_TOKENS,
            run_id=run_id,
        )

    assert excinfo.value.scope == daily, "the daily scope is the one that refused"
    assert spent(owner_engine, daily) == RESERVE_AMOUNT
    assert spent(owner_engine, run) == RESERVE_AMOUNT, (
        "the run ledger must be rolled back with the refused transaction -- a run "
        "charged for a call the daily cap refused is spend with nothing to show"
    )
    assert len(reservations(owner_engine, run)) == 1


def test_a_daily_scope_with_room_does_not_rescue_an_exhausted_run(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The mirror image: the run cap binds while the day has room."""
    daily = make_scope(RESERVE_AMOUNT * 10, hint="daily")
    run = make_scope(RESERVE_AMOUNT, hint="run")
    run_id = run_id_for(run)

    reserve(
        idempotency_key=unique("first"),
        model=MODEL,
        max_output_tokens=RESERVE_OUTPUT_TOKENS,
        max_input_tokens=RESERVE_INPUT_TOKENS,
        run_id=run_id,
    )
    with pytest.raises(BudgetCapExceeded) as excinfo:
        reserve(
            idempotency_key=unique("second"),
            model=MODEL,
            max_output_tokens=RESERVE_OUTPUT_TOKENS,
            max_input_tokens=RESERVE_INPUT_TOKENS,
            run_id=run_id,
        )
    assert excinfo.value.scope == run
    assert spent(owner_engine, daily) == RESERVE_AMOUNT, "the daily charge rolled back too"


# ===========================================================================
# settle
# ===========================================================================
def test_settle_releases_exactly_the_difference(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """Actuals come from provider-reported usage; the rest goes back. Exactly."""
    scope = make_scope(RESERVE_AMOUNT * 2)
    reservation = _reserve(scope, unique("settle"))
    usage = Usage(input_tokens=40, output_tokens=30, cache_read_tokens=20, cache_write_tokens=0)
    expected_actual = cost_microusd(MODEL, usage)  # 40x5 + 30x25 + 20x0.5 = 960

    settlement = settle(reservation, ProviderReportedUsage(usage))

    assert expected_actual == 960
    assert settlement.actual_microusd == expected_actual
    assert settlement.released_microusd == RESERVE_AMOUNT - expected_actual
    assert spent(owner_engine, scope) == expected_actual
    assert reservations(owner_engine, scope) == [("settled", RESERVE_AMOUNT, expected_actual)]


def test_a_zeroed_usage_block_is_not_evidence_and_cannot_settle_at_zero(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """BLOCKER 1: a SUCCESSFUL call that reports no usage must not be free.

    ``cost_microusd(model, Usage())`` is 0, and settlement used to accept it. A
    provider that returns real text with an absent or zeroed usage block bills
    you and was charged nothing: 100 successful, text-returning, billed calls at
    a cost of 0, with no database access required to arrange it.

    Usage is evidence only when it is present and non-degenerate, so the
    degenerate block cannot even be *spelled* as evidence -- and the reservation
    is left untouched, still fully charged, rather than quietly released.
    """
    scope = make_scope(RESERVE_AMOUNT * 2)
    reservation = _reserve(scope, unique("zero"))

    for degenerate in (
        Usage(),
        Usage(input_tokens=100, output_tokens=0),
        Usage(input_tokens=0, output_tokens=100),
        Usage(cache_read_tokens=100, output_tokens=0),
    ):
        with pytest.raises(DegenerateUsage):
            ProviderReportedUsage(degenerate)
        with pytest.raises(DegenerateUsage):
            settle(reservation, ProviderReportedUsage(degenerate))

    assert spent(owner_engine, scope) == RESERVE_AMOUNT, "nothing was released"
    assert reservations(owner_engine, scope) == [("open", RESERVE_AMOUNT, None)]

    # And the rule the caller is left with: unknown charges the worst case.
    settlement = settle(
        reservation, OutcomeUnknown("the provider returned text and reported no usage")
    )
    assert settlement.actual_microusd == RESERVE_AMOUNT
    assert settlement.released_microusd == 0
    assert spent(owner_engine, scope) == RESERVE_AMOUNT


def test_settling_more_than_was_reserved_is_refused_by_the_database(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """``actual <= reserve`` is a trigger, and the refusal is left to it.

    Pre-empting this in Python would mean the test proved a Python branch while
    the database's rule went unexercised -- the exact shape of green that the
    reservation design exists to stop.
    """
    scope = make_scope(RESERVE_AMOUNT * 2)
    reservation = _reserve(scope, unique("over"))
    # Non-degenerate on purpose: this is a real, priceable, believable usage
    # block whose PRICE is above the reservation. A zeroed one would be refused
    # as evidence long before it reached the overspend path.
    enormous = Usage(input_tokens=10, output_tokens=RESERVE_OUTPUT_TOKENS * 100)
    assert cost_microusd(MODEL, enormous) > reservation.reserve_microusd

    with pytest.raises(SettlementRefused) as excinfo:
        settle(reservation, ProviderReportedUsage(enormous))

    assert excinfo.value.sqlstate == KS_RESERVATION_LIFECYCLE
    assert spent(owner_engine, scope) == RESERVE_AMOUNT, "still fully reserved"
    assert reservations(owner_engine, scope) == [("open", RESERVE_AMOUNT, None)]


def test_settle_capped_halts_the_run_instead_of_absorbing_the_overspend(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """An actual above the reservation is a CAP-RELEVANT EVENT, not a rounding detail.

    ``worst_case_input_tokens`` is a hard bound, so this should never fire. When
    it does, two things are true and both matter: the ledger *cannot* hold the
    reported cost (``actual <= reserve`` is a trigger, and leaving the row open
    would under-report a call that cost more than expected), and the difference
    is real money the ledger will never see.

    Absorbing that difference and returning a successful settlement is the
    behaviour a red-team run used to charge 13,925 microusd against 30,000,000
    reported -- 29,986,075 uncharged, status ``ok``. So the settlement lands at
    the reservation, the shortfall is audited **and alerted**, and the run halts
    with :class:`BudgetOverspend`.
    """
    scope = make_scope(RESERVE_AMOUNT * 2)
    reservation = _reserve(scope, unique("overflow"))
    # Non-degenerate on purpose: this is a real, priceable, believable usage
    # block whose PRICE is above the reservation. A zeroed one would be refused
    # as evidence long before it reached the overspend path.
    enormous = Usage(input_tokens=10, output_tokens=RESERVE_OUTPUT_TOKENS * 100)
    reported = cost_microusd(MODEL, enormous)
    alerts: list[dict] = []
    register_alert_sink(alerts.append)

    try:
        with pytest.raises(BudgetOverspend) as excinfo:
            settle_capped(reservation, ProviderReportedUsage(enormous))
    finally:
        unregister_alert_sink(alerts.append)

    settlement = excinfo.value.settlement
    assert settlement.overflowed is True
    assert settlement.actual_microusd == RESERVE_AMOUNT, "settled at the reservation"
    assert settlement.reported_microusd == reported
    assert settlement.shortfall_microusd == reported - RESERVE_AMOUNT
    assert excinfo.value.shortfall_microusd == reported - RESERVE_AMOUNT

    # The ledger holds every microusd it structurally can, and the shortfall is
    # recorded rather than dropped.
    assert spent(owner_engine, scope) == RESERVE_AMOUNT
    assert (
        audit_count(
            owner_engine,
            action="budget_settle_overflow",
            subject=reservation.idempotency_key,
        )
        == 1
    )
    overspend_alerts = [event for event in alerts if event["event"] == "budget.settle_overflow"]
    assert len(overspend_alerts) == 1, "an overspend pages exactly as a cap hit does"
    assert overspend_alerts[0]["shortfall_microusd"] == reported - RESERVE_AMOUNT
    assert [event for event in alerts if event["event"] == "budget.scope_halted"], (
        "an overspend also pages that the scope is now halted"
    )

    # MAJOR 5: THE HALT HALTS SOMETHING. The previous version raised, the caller
    # turned it into `status="overspend"`, and nothing consumed that value -- 20
    # consecutive calls each overspending by ~30,000,000 microusd all proceeded.
    # The halt now lives in the ledger's own audit log, so the NEXT reservation
    # on this scope is refused whoever makes it and whatever they read.
    assert halted_scopes([scope]) == (scope,)
    with pytest.raises(BudgetScopeHalted):
        _reserve(scope, unique("after-overspend"))
    assert spent(owner_engine, scope) == RESERVE_AMOUNT, "a halted scope charges nothing more"

    # Only ops lifts it, and then reserving works again.
    resume_scope(scope, reason="ledger reconciled by hand in this test")
    assert halted_scopes([scope]) == ()
    _reserve(scope, unique("after-resume"))


def test_a_reservation_settles_exactly_once(owner_engine: Engine, make_scope: ScopeFactory) -> None:
    """Double settle is refused: a second release would be free money."""
    scope = make_scope(RESERVE_AMOUNT * 2)
    reservation = _reserve(scope, unique("twice"))
    usage = Usage(input_tokens=10, output_tokens=10)
    settle(reservation, ProviderReportedUsage(usage))
    after_first = spent(owner_engine, scope)

    with pytest.raises(SettlementRefused) as excinfo:
        settle(reservation, ProviderReportedUsage(usage))

    assert excinfo.value.sqlstate == KS_RESERVATION_LIFECYCLE
    assert spent(owner_engine, scope) == after_first, "the ledger did not move twice"


def test_the_audit_row_records_tokens_and_cost(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """R18: tokens and cost land in the audit log, from reported usage."""
    scope = make_scope(RESERVE_AMOUNT * 2)
    reservation = _reserve(scope, unique("audit"))
    usage = Usage(input_tokens=40, output_tokens=30, cache_read_tokens=20)
    settle(reservation, ProviderReportedUsage(usage))

    with owner_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT tokens_in, tokens_out, cost_microusd, detail FROM audit_log "
                "WHERE action = 'llm_call' AND subject = :s"
            ),
            {"s": reservation.idempotency_key},
        ).one()
    assert row.tokens_in == 60  # 40 uncached + 20 cache-read
    assert row.tokens_out == 30
    assert row.cost_microusd == cost_microusd(MODEL, usage)
    assert row.detail["mode"] == "safe", "audit detail is redacted by default (R21)"


# ===========================================================================
# TTL sweeper -- the ops principal, and only genuinely dead rows
# ===========================================================================
def test_the_capped_party_may_not_reclaim_its_own_reservation(
    make_scope: ScopeFactory,
) -> None:
    """``open -> reclaimed`` is the sweeper's, not the capped party's.

    A party that can reclaim a reservation it actually consumed has re-invented
    "zero the spend" -- which is why the sweeper below runs as ops.
    """
    scope = make_scope(RESERVE_AMOUNT * 2)
    reservation = _reserve(scope, unique("reclaim"))
    key = reservation.scope_keys[scope]

    with pytest.raises(DBAPIError) as excinfo, role_connection(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text("UPDATE budget_reservations SET state = 'reclaimed' WHERE idempotency_key = :k"),
            {"k": key},
        )
    assert getattr(excinfo.value.orig, "sqlstate", None) == KS_RESERVATION_LIFECYCLE


def test_the_sweeper_closes_a_dead_lease_by_CHARGING_it_in_full(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """BLOCKER 2: an expired lease is evidence the HOLDER died, not the CALL.

    The sweeper this replaces *reclaimed* the row, which released its whole
    reservation. So a child process that completed a paid call and was then
    ``SIGKILL``ed got a 100% refund, and a crash loop was a way to spend without
    limit -- the same "refund money you actually spent" bypass as the timeout
    path, wearing a janitor's uniform.

    A dead lease is therefore :class:`OutcomeUnknown`: the row is closed at its
    **full reserved amount** and nothing is released. The boundary is still the
    *lease* and not age -- both reservations below are created within
    microseconds of each other, so only the lease can distinguish them -- but
    crossing it now costs the holder its budget instead of refunding it.
    """
    scope = make_scope(RESERVE_AMOUNT * 10)

    settled = _reserve(scope, unique("settled"))
    settle(settled, ProviderReportedUsage(Usage(input_tokens=10, output_tokens=10)))
    settled_actual = cost_microusd(MODEL, Usage(input_tokens=10, output_tokens=10))

    dead = _reserve(scope, unique("dead"), lease_seconds=1)
    live = _reserve(scope, unique("live"), lease_seconds=3600)
    before = spent(owner_engine, scope)
    assert before == settled_actual + RESERVE_AMOUNT * 2

    # Strictly after `dead`'s lease plus the grace, and long before `live`'s.
    swept = sweep_expired_reservations(
        grace_seconds=0, now=dead.lease_expires_at + timedelta(seconds=1)
    )

    swept_keys = {item.idempotency_key for item in swept}
    assert dead.scope_keys[scope] in swept_keys, "an expired lease is evidence the holder died"
    assert live.scope_keys[scope] not in swept_keys, "a live lease is not dead"
    assert settled.scope_keys[scope] not in swept_keys, "a settled reservation is not open"

    charged = {item.idempotency_key: item for item in swept}[dead.scope_keys[scope]]
    assert charged.charged_microusd == RESERVE_AMOUNT
    assert charged.released_microusd == 0, "a dead lease releases NOTHING"
    assert spent(owner_engine, scope) == before, (
        "the sweep charged the abandoned reservation in full: the ledger did not move"
    )
    states = {state for state, _, _ in reservations(owner_engine, scope)}
    assert states == {"settled", "open"}
    assert (
        audit_count(owner_engine, action=AUDIT_SWEEP_CHARGED, subject=dead.scope_keys[scope]) == 1
    ), "the sweep leaves an audit row saying what it charged (R18)"


def test_the_sweeper_never_reclaims_a_live_reservation_however_old_it_is(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The bug this lease exists to close: age alone must not reclaim anything.

    A long-running call is old *and* alive. Under the old age-only rule its
    budget was handed to another caller and its own settlement was then refused
    ``KS007`` -- the call's real cost lost from the ledger permanently, which is
    a cap bypass built out of a janitor. Here the reservation is a full hour
    older than any plausible TTL and still holds its budget, because its lease
    has not expired.
    """
    scope = make_scope(RESERVE_AMOUNT * 2)
    long_call = _reserve(scope, unique("slow"), lease_seconds=MAX_LEASE_SECONDS)
    before = spent(owner_engine, scope)

    # Fifty minutes old -- far older than any age-based cutoff the sweeper this
    # replaced used -- and still inside its lease.
    swept = sweep_expired_reservations(
        grace_seconds=0, now=datetime.now(tz=UTC) + timedelta(minutes=50)
    )

    assert all(item.scope != scope for item in swept), "a live call was reclaimed"
    assert spent(owner_engine, scope) == before

    # And the proof that it is still settleable: an early reclaim is precisely
    # what makes this settle fail with KS007 and lose the money.
    settlement = settle(long_call, ProviderReportedUsage(Usage(input_tokens=10, output_tokens=10)))
    assert settlement.actual_microusd == cost_microusd(
        MODEL, Usage(input_tokens=10, output_tokens=10)
    )


def test_only_stated_evidence_makes_a_sweep_release_anything(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The other half of BLOCKER 2, so the sweeper is not merely "charge always".

    Ops can release a swept reservation, but only by presenting the evidence
    that the call never went out -- a provider incident report, an
    outbound-connection log. There is no flag that means "release anyway".
    """
    scope = make_scope(RESERVE_AMOUNT * 3)
    dead = _reserve(scope, unique("outage"), lease_seconds=1)
    assert spent(owner_engine, scope) == RESERVE_AMOUNT

    with pytest.raises(TypeError):
        sweep_expired_reservations(
            grace_seconds=0,
            now=dead.lease_expires_at + timedelta(seconds=1),
            release_evidence=OutcomeUnknown("we would rather not pay for this"),  # type: ignore[arg-type]
        )
    # MINOR 6: the one construction that grants a 100% refund takes a member of
    # the closed vocabulary the database also holds, never a sentence.
    with pytest.raises(TypeError):
        NeverSent("trust me bro")  # type: ignore[arg-type]

    swept = sweep_expired_reservations(
        grace_seconds=0,
        now=dead.lease_expires_at + timedelta(seconds=1),
        release_evidence=NeverSent(
            PreSendProof.OPS_ATTESTED_OUTAGE,
            "provider incident 4711: no request reached the edge",
        ),
    )
    assert {item.idempotency_key for item in swept} == {dead.scope_keys[scope]}
    assert swept[0].released_microusd == RESERVE_AMOUNT
    assert spent(owner_engine, scope) == 0


def test_a_reservation_with_no_lease_signal_is_left_alone(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """Absence of a liveness signal is not evidence of death.

    A row whose key carries no lease -- one written before leases existed, or by
    something outside this module -- cannot be told apart from a call still in
    flight, so the sweeper leaves it. Ops can still close it deliberately.
    """
    scope = make_scope(RESERVE_AMOUNT * 2)
    unleased = f"{unique('legacy')}#{scope}"  # no `#lease<seconds>` suffix
    with role_connection(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(
                "INSERT INTO budget_reservations (scope, idempotency_key, reserve_microusd) "
                "VALUES (:s, :k, :r)"
            ),
            {"s": scope, "k": unleased, "r": RESERVE_AMOUNT},
        )
    before = spent(owner_engine, scope)

    later = datetime.now(tz=UTC) + timedelta(days=7)
    assert all(item.idempotency_key != unleased for item in sweep_expired_reservations(now=later))
    assert spent(owner_engine, scope) == before, "an unleased row keeps its budget"

    swept = sweep_expired_reservations(now=later, sweep_unleased=True)
    assert unleased in {item.idempotency_key for item in swept}
    assert spent(owner_engine, scope) == before, (
        "and when ops does sweep it, it is CHARGED in full like any dead lease"
    )


def test_the_sweeper_refuses_to_run_as_the_capped_party(
    monkeypatch: pytest.MonkeyPatch, configured_url: str
) -> None:
    """Reclaiming belongs to ops. Connected as ``recon_writer``, it refuses.

    Migration 0005 already refuses ``open -> reclaimed`` to that role, so this
    is not the enforcement point and does not pretend to be one -- it is here so
    a misconfigured ``DATABASE_URL`` fails the ops cron with a legible message
    instead of a ``KS007`` from somewhere in the middle of a sweep.
    """
    from recon.db import reset_engine_cache, role_url

    env_settings(
        monkeypatch,
        DATABASE_URL=role_url(ROLE_RECON_WRITER).render_as_string(hide_password=False),
    )
    reset_engine_cache()
    try:
        with pytest.raises(BudgetError) as excinfo:
            sweep_expired_reservations()
    finally:
        env_settings(monkeypatch, DATABASE_URL=configured_url)
        reset_engine_cache()
    assert ROLE_RECON_WRITER in str(excinfo.value)


def test_the_sweeper_is_a_no_op_when_nothing_has_expired(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """A sweeper that reclaimed live work would be worse than none at all."""
    scope = make_scope(RESERVE_AMOUNT * 2)
    _reserve(scope, unique("fresh"))
    before = spent(owner_engine, scope)

    swept = sweep_expired_reservations(now=datetime.now(tz=UTC))

    assert all(item.scope != scope for item in swept)
    assert spent(owner_engine, scope) == before


# ===========================================================================
# provisioning
# ===========================================================================
def test_provisioning_a_run_scope_is_idempotent_and_never_widens_a_cap(
    owner_engine: Engine,
) -> None:
    """Starting a run twice must not hand it a second budget."""
    run_id = unique("provision")
    scope = run_scope(run_id)
    try:
        assert provision_run_scope(run_id, cap_microusd=5_000) is True
        assert provision_run_scope(run_id, cap_microusd=9_999_999) is False
        row = ledger_row(scope)
        assert row is not None
        assert row.cap_microusd == 5_000, "an existing cap is never widened by a re-trigger"
    finally:
        with owner_engine.begin() as conn:
            conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": scope})
            conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": scope})


def test_an_unprovisioned_scope_is_not_reported_as_a_cap_hit(owner_engine: Engine) -> None:
    """The trigger reuses ``KS006`` for "no ledger row". That is not a cap hit.

    Recording it as one would put false ``cap_hit`` rows into the audit log the
    dashboard reconciles against (R18) and page someone about a budget that was
    never reached. It is still a halt -- just an honestly labelled one.
    """
    scope = f"run:{unique('never-provisioned')}"
    fired: list[dict] = []
    register_alert_sink(fired.append)
    try:
        # Both mandated scopes resolve to this one row, and nobody provisioned it.
        with (
            mock.patch.dict(os.environ, {DAILY_SCOPE_ENV: scope}),
            pytest.raises(LedgerScopeMissing) as excinfo,
        ):
            _reserve(scope, unique("missing"))
    finally:
        unregister_alert_sink(fired.append)

    assert excinfo.value.scope == scope
    assert not fired, "an unprovisioned scope must not fire a cap alert"
    assert audit_count(owner_engine, action=AUDIT_CAP_HIT, subject=scope) == 0


# ===========================================================================
# fail closed financially -- the class of bug a red team found without the DB
# ===========================================================================
def test_a_failed_call_that_may_have_reached_the_provider_is_charged_in_full(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """THE refund rule. A post-send failure keeps the whole reservation.

    A provider timeout after generation is work the provider did and will bill
    for. Settling it at zero is the application refunding money it actually
    spent -- a cap bypass that touches no database at all, and the one that made
    a timeout storm bill unbounded money against a ledger reading zero.
    """
    scope = make_scope(RESERVE_AMOUNT * 3)
    reservation = _reserve(scope, unique("timeout"))
    assert spent(owner_engine, scope) == RESERVE_AMOUNT

    settlement = settle_failed_call(reservation, OutcomeUnknown("APITimeoutError: read timed out"))

    assert settlement.actual_microusd == RESERVE_AMOUNT
    assert settlement.released_microusd == 0, "nothing is released on an unknown outcome"
    assert spent(owner_engine, scope) == RESERVE_AMOUNT, "the money stays charged"
    assert reservations(owner_engine, scope) == [("settled", RESERVE_AMOUNT, RESERVE_AMOUNT)]


def test_a_failure_that_provably_never_reached_the_provider_is_released(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The other side of the rule, so it is not merely "charge everything".

    Evidence that the request never left buys a refund, and only evidence does.
    """
    scope = make_scope(RESERVE_AMOUNT * 3)
    reservation = _reserve(scope, unique("refused"))

    settlement = settle_failed_call(
        reservation,
        NeverSent(PreSendProof.CONNECTION_REFUSED, "ConnectionRefusedError: [Errno 61]"),
    )

    assert settlement.actual_microusd == 0
    assert settlement.released_microusd == RESERVE_AMOUNT
    assert spent(owner_engine, scope) == 0, "a call that provably never happened is free"


def test_a_failed_call_is_audited_as_a_failure_and_not_as_a_free_success(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """R18: the audit log must reconcile with the dashboard.

    A failure written as ``llm_call`` with cost 0 and tokens 0/0 is
    indistinguishable from a cheap success, so a thousand timeouts read as a
    thousand free calls. Failures get their own action and carry the reason.
    """
    scope = make_scope(RESERVE_AMOUNT * 3)
    reservation = _reserve(scope, unique("auditfail"))

    settle_failed_call(reservation, OutcomeUnknown("APITimeoutError: read timed out"))

    assert audit_count(owner_engine, action="llm_call", subject=reservation.idempotency_key) == 0, (
        "a failed call must never be recorded as a successful one"
    )
    assert (
        audit_count(owner_engine, action=AUDIT_LLM_CALL_FAILED, subject=reservation.idempotency_key)
        == 1
    )
    with owner_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT tokens_in, tokens_out, cost_microusd, detail FROM audit_log "
                "WHERE action = :a AND subject = :s"
            ),
            {"a": AUDIT_LLM_CALL_FAILED, "s": reservation.idempotency_key},
        ).one()
    assert row.cost_microusd == RESERVE_AMOUNT, "the audit row carries what was charged"
    assert row.tokens_in == 0 and row.tokens_out == 0
    body = row.detail["body"]
    assert body["outcome"] == "failed"
    assert body["reached_provider"] is True
    assert "APITimeoutError: read timed out" in body["reason"], (
        "the audit row records WHY the call failed, legibly: `reason` is on the "
        "committed privacy allow-list, so it survives safe mode intact"
    )
    assert "unknown" in body["reason"], "and records that the outcome was UNKNOWN"
    with pytest.raises(TypeError):
        # A failed call has no provider-reported usage, so it cannot be priced
        # into a discount: the type refuses before any statement runs.
        settle_failed_call(
            _reserve(scope, unique("noprice")),
            ProviderReportedUsage(Usage(input_tokens=1, output_tokens=1)),
        )


# ===========================================================================
# a zero reservation is not a reservation
# ===========================================================================
def test_a_zero_reservation_is_refused(owner_engine: Engine, make_scope: ScopeFactory) -> None:
    """A live reservation that reserves nothing is a call the cap cannot see.

    It is admitted whatever the ledger says. Combined with a settlement that
    absorbed overflow it was an unmetered call, which is why it is refused
    before any statement runs rather than merely discouraged.
    """
    scope = make_scope(RESERVE_AMOUNT)

    with pytest.raises(ZeroReservationRefused):
        reserve(
            idempotency_key=unique("zero"),
            model=MODEL,
            max_output_tokens=0,
            max_input_tokens=0,
            run_id=run_id_for(scope),
        )

    assert spent(owner_engine, scope) == 0
    assert reservations(owner_engine, scope) == [], "no row was stored for a zero reservation"


# ===========================================================================
# a replayed idempotency key is a no-op, not an exception
# ===========================================================================
def test_a_replayed_idempotency_key_is_an_idempotent_no_op(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """The documented contract: an idempotency key that repeats charges nothing.

    It used to raise a raw ``UniqueViolation`` straight through
    ``generate_rationale``, whose docstring says it never raises. A replayed key
    is a normal thing for a retried cron firing to produce.
    """
    scope = make_scope(RESERVE_AMOUNT * 3)
    key = unique("replay")
    first = _reserve(scope, key)
    assert first.replayed is False
    after_first = spent(owner_engine, scope)

    second = _reserve(scope, key, now=first.lease_expires_at - timedelta(seconds=300))

    assert second.replayed is True
    assert second.reserve_microusd == RESERVE_AMOUNT
    assert spent(owner_engine, scope) == after_first, "a replay charges nothing"
    assert len(reservations(owner_engine, scope)) == 1, "and stores no second row"


def test_a_replay_a_second_and_a_half_later_is_still_a_replay(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """MAJOR 6: idempotency that expired after one wall-clock second was not idempotency.

    ``scope_key`` used to bake ``int(lease_expires_at.timestamp())`` into the
    UNIQUE key. A retried cron firing, a redelivered webhook, an operator
    re-running a command -- anything replaying the same logical key more than a
    second later produced a *different* key, missed the constraint, was granted a
    fresh reservation, and MADE THE PAID CALL AGAIN.

    Real elapsed time here, not a `now=` argument: the point is that the clock
    moving is exactly what used to break it.
    """
    import time

    scope = make_scope(RESERVE_AMOUNT * 5)
    key = unique("replay-clock")
    first = _reserve(scope, key)
    assert first.replayed is False
    after_first = spent(owner_engine, scope)

    time.sleep(1.6)
    second = _reserve(scope, key)

    assert second.replayed is True, "the same logical call must collide, however late"
    assert spent(owner_engine, scope) == after_first, "a replay charges nothing"
    assert len(reservations(owner_engine, scope)) == 1, "and stores no second row"


def test_settling_a_replay_receipt_is_refused(make_scope: ScopeFactory) -> None:
    """A replay receipt is not a grant, so it cannot release anything."""
    scope = make_scope(RESERVE_AMOUNT * 3)
    key = unique("replay-settle")
    first = _reserve(scope, key)
    replay = _reserve(scope, key, now=first.lease_expires_at - timedelta(seconds=300))

    with pytest.raises(SettlementRefused):
        settle(replay, ProviderReportedUsage(Usage(input_tokens=1, output_tokens=1)))


# ===========================================================================
# the daily cap cannot be dropped by naming scopes
# ===========================================================================
def test_a_test_process_cannot_touch_the_real_daily_scope(owner_engine: Engine) -> None:
    """MINOR 8: the real ``daily`` scope is unreachable from a test, structurally.

    The old test proved this by making a real charge against the production
    ``daily`` ledger and then trying to give it back; two reclaimed reservations
    from an earlier run were found still sitting there, because the fixture
    cleans up by scope and never provisions ``daily``. So a test process is
    REFUSED the real daily scope outright: the property is proved by the refusal,
    and the day's budget is never touched to prove that the day's budget works.

    Note that this test deliberately does **not** take ``make_scope``: that
    fixture points the mandated cap at a stand-in row, and what is under test
    here is what happens when nothing has.
    """
    before = spent(owner_engine, DAILY_SCOPE)

    with pytest.raises(RealDailyScopeRefused) as excinfo:
        reserve(
            idempotency_key=unique("nodaily"),
            model=MODEL,
            max_output_tokens=RESERVE_OUTPUT_TOKENS,
            max_input_tokens=RESERVE_INPUT_TOKENS,
            run_id="whatever",
        )
    assert DAILY_SCOPE in excinfo.value.scopes

    assert spent(owner_engine, DAILY_SCOPE) == before, "the day's budget was not touched"
    with owner_engine.connect() as conn:
        stray = conn.execute(
            text("SELECT count(*) FROM budget_reservations WHERE scope = :s"),
            {"s": DAILY_SCOPE},
        ).scalar_one()
    assert stray == 0, (
        "no test may leave a row on the production daily scope; two reclaimed "
        "rows from an earlier run were found there before this guard existed"
    )


def test_dropping_the_daily_cap_cannot_BE_EXPRESSED_by_any_caller() -> None:
    """BLOCKER 3: the argument that dropped the mandated daily cap is gone.

    The previous control was a type, ``IsolatedScopes``, whose constructor walked
    the stack and matched ``frame.f_code.co_filename`` by path SUFFIX. A red team
    built one with

        exec(compile(src, "/anywhere/service/tests/x.py", "exec"))

    with no file edits at all, and every caller of ``reserve`` or
    ``generate_rationale`` was then one keyword away from a real billed call that
    never touched the daily cap. Stack inspection is not a security boundary.

    So the parameter is gone and applying the mandated scope is the reserving
    code's job. This asserts the absence -- the only form in which "you cannot
    express it" can be tested -- three ways: the signatures do not take it, the
    type no longer exists to be imported, and the resolver produces both mandated
    scopes from a ``run_id`` alone.
    """
    import recon.budget as budget_module
    import recon.llm as llm_module
    from recon.budget import _resolve_scopes

    for function in (budget_module.reserve, llm_module.generate_rationale):
        parameters = inspect.signature(function).parameters
        assert "scopes" not in parameters, (
            f"{function.__qualname__} takes a scopes argument again; R17's daily cap "
            "must not be something a caller can decline"
        )
    assert "run_id" in inspect.signature(budget_module.reserve).parameters
    assert not hasattr(budget_module, "IsolatedScopes"), (
        "the stack-guarded escape hatch is back; there is no safe version of it"
    )

    stand_in = "run:daily-stand-in-for-this-assertion"
    with mock.patch.dict(os.environ, {DAILY_SCOPE_ENV: stand_in}):
        assert _resolve_scopes("abc") == tuple(sorted({stand_in, "run:abc"}))


def test_the_daily_stand_in_is_deployment_configuration_not_an_argument() -> None:
    """And an unset override in a NON-test process still means the real daily row.

    ``daily_scope()`` is the whole surface: a process either takes the mandated
    ``daily`` row or is pointed at a stand-in by the environment. Nothing a
    caller passes reaches it, and a stand-in is still an ops-provisioned, capped
    ledger row -- so redirecting it cannot buy budget nobody provisioned.
    """
    from recon.budget import daily_scope

    assert "scope" not in inspect.signature(daily_scope).parameters
    assert inspect.signature(daily_scope).parameters == {}

    with mock.patch.dict(os.environ, {DAILY_SCOPE_ENV: "run:stand-in"}):
        assert daily_scope() == "run:stand-in"
    # An override equal to the real row is treated as unset, so it cannot be used
    # to smuggle the production daily scope into a test process.
    with (
        mock.patch.dict(os.environ, {DAILY_SCOPE_ENV: DAILY_SCOPE}),
        pytest.raises(RealDailyScopeRefused),
    ):
        daily_scope()


def test_a_reservation_key_carries_an_unforgeable_lease_and_no_clock(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    """MAJOR 6: the key carries a lease DURATION, and no wall clock at all.

    The deadline used to be ``int(lease_expires_at.timestamp())``, baked into the
    UNIQUE key. So the same logical idempotency key replayed 1.2 seconds later
    produced a *different* key, missed the constraint, and MADE THE PAID CALL
    AGAIN -- an idempotency key that is only idempotent within one wall-clock
    second is not one. A duration is a property of the caller's identity for the
    work; a clock never is.

    ``idempotency_key`` and ``created_at`` are both immutable after insert
    (migration 0005's settle trigger), so the deadline the sweeper computes from
    them cannot be extended by the holder either.
    """
    from recon.budget import lease_deadline, lease_seconds_from_key

    scope = make_scope(RESERVE_AMOUNT * 2)
    reservation = _reserve(scope, unique("lease"), lease_seconds=1234)
    assert isinstance(reservation, Reservation)

    key = reservation.scope_keys[scope]
    assert lease_seconds_from_key(key) == 1234
    assert key == f"{reservation.idempotency_key}#{scope}#lease1234", (
        "the key is <idempotency_key>#<scope>#lease<duration> and nothing else: "
        "no timestamp appears in it"
    )
    with owner_engine.connect() as conn:
        created_at = conn.execute(
            text("SELECT created_at FROM budget_reservations WHERE idempotency_key = :k"),
            {"k": key},
        ).scalar_one()
    deadline = lease_deadline(key, created_at)
    assert deadline is not None
    assert abs((deadline - reservation.lease_expires_at).total_seconds()) < 5

    # And a holder cannot buy itself an unbounded lease: the duration is clamped,
    # so "pin this budget for a decade" is not spellable.
    greedy = _reserve(scope, unique("greedy"), lease_seconds=10**9)
    assert greedy.lease_seconds == MAX_LEASE_SECONDS
    assert lease_seconds_from_key(greedy.scope_keys[scope]) == MAX_LEASE_SECONDS

    # And the holder cannot rewrite it to buy itself more time. Two independent
    # refusals stand in the way and the grant is the outer one: `idempotency_key`
    # is not even in `recon_writer`'s column-scoped UPDATE (42501,
    # insufficient_privilege), and migration 0005's settle trigger refuses the
    # change as well (KS007) for any principal that does hold the column.
    with pytest.raises(DBAPIError) as excinfo, role_connection(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(
                "UPDATE budget_reservations SET idempotency_key = :new, "
                "actual_microusd = 0, state = 'settled' WHERE idempotency_key = :old"
            ),
            {"new": f"{reservation.scope_keys[scope]}9", "old": reservation.scope_keys[scope]},
        )
    assert getattr(excinfo.value.orig, "sqlstate", None) in {
        "42501",
        KS_RESERVATION_LIFECYCLE,
    }
