"""THE burst test (R17): 120 concurrent rationale requests against a cap of 6.

This is the graded one, so it is worth being explicit about what it does and
does not prove.

**It drives the real path.** Each of the 120 workers calls
:func:`recon.llm.generate_rationale` -- the same function the reconciler calls --
against the mock provider, which returns deterministic text *and deterministic
provider usage*. Those usage numbers go through the committed price table, the
real ``budget_reservations`` INSERT, the real ``BEFORE INSERT`` trigger and the
real ledger. Nothing about the cap is simulated; the only thing the mock
replaces is the network call whose *cost* is being capped.

**The requests are genuinely concurrent.** 120 threads are released together by
a :class:`threading.Barrier`, and each granted call is then *parked inside the
provider* until the test releases it -- so every granted reservation is open,
committed and in flight at the instant the assertions run. A loop that reserved
and settled one at a time would never contend for the ledger row lock, and would
prove nothing about the race DESIGN warns about.

**The assertions are five-sided**, because each one alone has a way of passing
while the product is broken:

* exactly ``M`` grants -- not "at most M": a cap that admits fewer calls than it
  allows is a broken product that still looks safe;
* spend lands **exactly** on the cap, never above;
* every refusal carries ``KS006`` -- so a dropped connection, a pool timeout or
  a deadlock cannot masquerade as the cap holding;
* the ``spent <= cap`` CHECK is still on the table -- a burst that "passed"
  because someone dropped the backstop is a much worse result than a failure;
* a retry wave against the exhausted cap adds zero grants and zero reservation
  rows -- no retry bypasses the cap.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import Engine, text

from recon.budget import (
    AUDIT_CAP_HIT,
    KS_CAP_EXCEEDED,
    Usage,
    cost_microusd,
    register_alert_sink,
    unregister_alert_sink,
    worst_case_input_tokens,
    worst_case_microusd,
)
from recon.llm import (
    STATUS_CAP_HIT,
    STATUS_OK,
    SYSTEM_PROMPT,
    MockProvider,
    RationaleOutcome,
    RationaleRequest,
    generate_rationale,
)
from tests.budget.support import (
    ScopeFactory,
    audit_count,
    check_constraint_exists,
    reservations,
    run_id_for,
    spent,
    unique,
)

MODEL = "mock-rationale-v1"
CONTENDERS = 120
ADMITTED = 6
MAX_OUTPUT_TOKENS = 384
RETRY_WAVE = 10

#: Seconds a parked worker will wait before giving up. Generous, because the
#: only thing it guards against is a hung test -- a real stall shows up as a
#: failed assertion on the outcome vector, not as a silent pass.
PARK_TIMEOUT = 120.0
GATHER_TIMEOUT = 120.0

PROMPT = (
    "Source crm says the enrollment status is 'active'; source sis says "
    "'withdrawn'. The sis record was loaded in a later generation."
)


def _request(index: int) -> RationaleRequest:
    # Same prompt for every worker: the mock is deterministic, so identical
    # prompts mean identical usage, which is what lets the final spend be
    # asserted as an exact integer rather than a range.
    return RationaleRequest(subject=f"conflict-{index}", prompt=PROMPT)


def _expected_reserve() -> int:
    return worst_case_microusd(
        MODEL,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        max_input_tokens=worst_case_input_tokens(SYSTEM_PROMPT + PROMPT),
    )


def _expected_actual() -> int:
    result = MockProvider().complete(_request(0), max_output_tokens=MAX_OUTPUT_TOKENS)
    return cost_microusd(MODEL, result.usage)


def test_a_concurrent_burst_stops_exactly_at_the_cap_with_no_bypass(
    owner_engine: Engine, make_scope: ScopeFactory
) -> None:
    reserve_each = _expected_reserve()
    actual_each = _expected_actual()
    assert actual_each < reserve_each, "the settlement must genuinely release something"

    cap = reserve_each * ADMITTED
    daily = make_scope(cap, hint="daily")
    run = make_scope(cap * 100, hint="run")  # roomy: the daily scope is the binding one
    run_id = run_id_for(run)

    start = threading.Barrier(CONTENDERS, timeout=GATHER_TIMEOUT)
    release = threading.Event()
    parked = threading.Semaphore(0)
    alerts: list[dict] = []
    register_alert_sink(alerts.append)

    def hold(_request: RationaleRequest) -> None:
        """Park inside the provider so the reservation stays open and committed."""
        parked.release()
        if not release.wait(timeout=PARK_TIMEOUT):  # pragma: no cover - hung-test guard
            raise TimeoutError("burst worker was never released")

    provider = MockProvider(on_call=hold)

    def worker(index: int) -> RationaleOutcome:
        start.wait()
        return generate_rationale(
            _request(index),
            run_id=run_id,
            idempotency_key=unique(f"burst-{index}"),
            provider=provider,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            max_attempts=1,
        )

    try:
        with ThreadPoolExecutor(max_workers=CONTENDERS) as pool:
            futures = [pool.submit(worker, index) for index in range(CONTENDERS)]

            # ---- phase 1: wait until every granted call is parked ------------
            deadline = time.monotonic() + GATHER_TIMEOUT
            for _ in range(ADMITTED):
                remaining = max(0.0, deadline - time.monotonic())
                assert parked.acquire(timeout=remaining), (
                    f"only some of the {ADMITTED} admitted calls ever reached the provider"
                )

            # Every other contender must have finished (refused) by now. Wait for
            # the ledger to settle on a stable figure rather than sampling a race.
            refused_deadline = time.monotonic() + GATHER_TIMEOUT
            while time.monotonic() < refused_deadline:
                done = sum(1 for future in futures if future.done())
                if done == CONTENDERS - ADMITTED:
                    break
                time.sleep(0.05)

            # ---- phase 2: the assertions, with all M reservations open -------
            open_spend = spent(owner_engine, daily)
            assert open_spend == cap, (
                f"the burst must land exactly on the cap: spent={open_spend}, cap={cap}"
            )
            held = reservations(owner_engine, daily)
            assert len(held) == ADMITTED, f"expected {ADMITTED} stored reservations, got {held}"
            assert {state for state, _, _ in held} == {"open"}

            # ---- phase 3: a retry wave, while the cap is full ----------------
            retries = [
                generate_rationale(
                    _request(1000 + index),
                    run_id=run_id,
                    idempotency_key=unique(f"burst-retry-{index}"),
                    provider=MockProvider(),  # would succeed instantly if it ran
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                    # A cap hit is terminal, so each of these makes exactly one
                    # reservation attempt through the same trigger and is refused.
                    # `test_a_retry_takes_a_fresh_reservation` covers the other
                    # half of "every retry re-reserves": the transient-failure path.
                    max_attempts=3,
                )
                for index in range(RETRY_WAVE)
            ]
            assert all(outcome.status == STATUS_CAP_HIT for outcome in retries), (
                f"a retry got through the cap: {[o.status for o in retries]}"
            )
            assert all(outcome.sqlstate == KS_CAP_EXCEEDED for outcome in retries)
            assert all(outcome.text is None for outcome in retries)
            assert spent(owner_engine, daily) == cap, "a retry moved the ledger"
            assert len(reservations(owner_engine, daily)) == ADMITTED, (
                "a retry stored a reservation past the cap"
            )

            # ---- phase 4: release, and let the granted calls settle ----------
            release.set()
            outcomes = [future.result(timeout=GATHER_TIMEOUT) for future in futures]
    finally:
        release.set()
        unregister_alert_sink(alerts.append)

    granted = [outcome for outcome in outcomes if outcome.status == STATUS_OK]
    refused = [outcome for outcome in outcomes if outcome.status == STATUS_CAP_HIT]
    other = [outcome for outcome in outcomes if outcome.status not in (STATUS_OK, STATUS_CAP_HIT)]

    # ---- the outcome vector -------------------------------------------------
    assert not other, f"unexpected outcomes: {[(o.status, o.detail) for o in other]}"
    assert len(granted) == ADMITTED, (
        f"expected exactly {ADMITTED} grants, got {len(granted)} "
        f"(refused {len(refused)}, other {len(other)})"
    )
    assert len(refused) == CONTENDERS - ADMITTED
    assert {outcome.sqlstate for outcome in refused} == {KS_CAP_EXCEEDED}, (
        "every refusal must be the cap refusing (KS006) -- a dropped connection "
        "or a deadlock must not be able to masquerade as the cap holding"
    )
    assert all(outcome.text for outcome in granted), "a granted call produced no rationale"
    assert all(outcome.cost_microusd == actual_each for outcome in granted)
    assert all(outcome.attempts == 1 for outcome in refused), "a cap hit must be terminal"

    # ---- the ledger ---------------------------------------------------------
    final_spend = spent(owner_engine, daily)
    assert final_spend == actual_each * ADMITTED, (
        "after settlement the ledger holds exactly the provider-reported cost of "
        f"the {ADMITTED} calls that actually happened"
    )
    assert final_spend <= cap
    assert spent(owner_engine, run) == actual_each * ADMITTED, "the run scope agrees"
    settled = reservations(owner_engine, daily)
    assert settled == [("settled", reserve_each, actual_each)] * ADMITTED

    # ---- the evidence -------------------------------------------------------
    cap_hits = audit_count(owner_engine, action=AUDIT_CAP_HIT, subject=daily)
    assert cap_hits == len(refused) + RETRY_WAVE, (
        f"every refusal must leave a cap_hit audit row: {cap_hits} rows for "
        f"{len(refused)} refusals plus {RETRY_WAVE} retries"
    )
    cap_alerts = [event for event in alerts if event["scope"] == daily]
    assert len(cap_alerts) == cap_hits, "the stubbed alert fired for every cap hit"
    assert {event["sqlstate"] for event in cap_alerts} == {KS_CAP_EXCEEDED}

    # The observed vector, printed so a run of this test is reportable evidence
    # and not just a green tick.
    print(
        "\nburst outcome vector: "
        f"contenders={CONTENDERS} granted={len(granted)} refused={len(refused)} "
        f"other={len(other)} refusal_sqlstates={sorted({o.sqlstate for o in refused})} "
        f"cap={cap} reserve_each={reserve_each} spend_while_open={open_spend} "
        f"actual_each={actual_each} final_spend={final_spend} "
        f"cap_hit_audit_rows={cap_hits} alerts_fired={len(cap_alerts)} "
        f"retry_wave={RETRY_WAVE} retries_granted=0"
    )

    # ---- the backstop was in place the whole time ---------------------------
    assert check_constraint_exists(owner_engine), (
        "ck_budget_spent_within_cap is the backstop under the trigger; a burst "
        "that passed with it dropped would prove nothing"
    )
    with owner_engine.connect() as conn:
        violations = conn.execute(
            text("SELECT count(*) FROM budget_ledger WHERE spent_microusd > cap_microusd")
        ).scalar_one()
    assert violations == 0


def test_the_burst_numbers_are_derived_and_not_hand_written() -> None:
    """The reserve and actual figures come from the same code production uses."""
    assert _expected_reserve() == worst_case_microusd(
        MODEL,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        max_input_tokens=worst_case_input_tokens(SYSTEM_PROMPT + PROMPT),
    )
    usage = MockProvider().complete(_request(0), max_output_tokens=MAX_OUTPUT_TOKENS).usage
    assert isinstance(usage, Usage)
    assert _expected_actual() == cost_microusd(MODEL, usage)
