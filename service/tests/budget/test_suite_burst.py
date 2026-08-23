"""The burst as a GRADED SCORECARD ROW (SPEC gate 1), not only as a pytest run.

SPEC success criterion 1 lists *"burst test halts exactly at cap"* among the
things ``recon.suite`` reports. A burst that only ever runs under pytest is not
that: the scorecard is what the grader reads, and a row that is missing from it
cannot be read at all.

Two claims are made here and both need proving separately:

* the check **passes against the real system** -- it runs the real burst through
  :func:`recon.llm.generate_rationale`, real Postgres, the real trigger;
* the check **can fail**. A check that cannot fail is decoration. Its verdict is
  computed entirely by :func:`recon.suite.burst._assess` from the observed
  vector, so feeding ``_assess`` a vector from a broken cap must produce FAIL --
  and the sabotage test below does exactly that, one broken dimension at a time.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text

from recon.llm import (
    STATUS_OK,
    STATUS_OVERSPEND,
    STATUS_PROVIDER_ERROR,
    STATUS_SCOPE_HALTED,
)
from recon.suite.burst import (
    _RAW_RELEASE_SPELLINGS,
    ADMITTED,
    CHECK_NAME,
    CONTENDERS,
    BurstOutcome,
    _assess,
    check_spend_cap_burst,
    expected_actual,
    expected_reserve,
    run_burst,
)
from recon.suite.checks import FAIL, PASS


def _clean_vector() -> BurstOutcome:
    """The vector a healthy cap produces, built from the real price table."""
    reserve_each = expected_reserve()
    actual_each = expected_actual()
    cap = reserve_each * ADMITTED
    return BurstOutcome(
        contenders=CONTENDERS,
        admitted_expected=ADMITTED,
        cap_microusd=cap,
        reserve_each=reserve_each,
        actual_each=actual_each,
        granted=ADMITTED,
        refused=CONTENDERS - ADMITTED,
        other=(),
        refusal_sqlstates=("KS006",) * (CONTENDERS - ADMITTED),
        spend_while_open=cap,
        final_spend=actual_each * ADMITTED,
        open_reservations=ADMITTED,
        retries_granted=0,
        reservations_after_retries=ADMITTED,
        cap_hit_audit_rows=CONTENDERS - ADMITTED + 10,
        alerts_fired=CONTENDERS - ADMITTED + 10,
        backstop_present=True,
        ledger_violations=0,
        # -- the evidence phases: the half that could not fail before ---------
        release_sites=("budget.py:1",),
        post_send_status=STATUS_PROVIDER_ERROR,
        post_send_spend=reserve_each,
        pre_send_status=STATUS_PROVIDER_ERROR,
        pre_send_spend=0,
        silent_usage_status=STATUS_OK,
        silent_usage_spend=reserve_each,
        overspend_status=STATUS_OVERSPEND,
        after_overspend_status=STATUS_SCOPE_HALTED,
        overspend_spend=reserve_each,
        sweeper_charged=reserve_each,
        sweeper_released=0,
        sweeper_spend=reserve_each,
        # -- the boundary phases: the guards a release count cannot see --------
        raw_update_outcomes=tuple(f"{name}:KS007" for name, _ in _RAW_RELEASE_SPELLINGS),
        raw_update_spend=reserve_each,
        raw_update_control_spend=actual_each,
        replay_receipt=True,
        replay_settle_refused=True,
        replay_spend=reserve_each,
        sweep_as_capped_refused=True,
        sweep_as_capped_open=1,
        sweep_as_capped_spend=reserve_each,
        failed_call_priced_refused=True,
        failed_call_priced_spend=reserve_each,
    )


def test_the_registered_check_runs_the_real_burst_and_passes(owner_engine: Engine) -> None:
    """The graded row, against real Postgres. PASS, with the observed vector."""
    result = check_spend_cap_burst()

    assert result.name == CHECK_NAME
    assert result.status == PASS, result.detail
    assert f"contenders={CONTENDERS}" in result.detail
    assert f"granted={ADMITTED}" in result.detail
    assert "refusal_sqlstates=['KS006']" in result.detail
    assert "retries_granted=0" in result.detail
    print(f"\nsuite scorecard row: {result.row()}")


def test_the_check_is_registered_in_the_suite() -> None:
    """A check nobody registered is a check nobody runs."""
    from recon.suite.__main__ import CHECKS

    assert CHECK_NAME in CHECKS
    assert CHECKS[CHECK_NAME] is check_spend_cap_burst


def test_the_burst_cleans_up_the_scopes_it_provisioned(owner_engine: Engine) -> None:
    """It must not leave ledger rows or reservations behind, on any of its names.

    Matched on ``%suite-burst-%`` and not on ``suite-burst-%``: the evidence and
    boundary phases provision ``run:suite-burst-…`` scopes (a ``run:`` name is
    what lets both mandated scopes resolve to one observable ledger row), and an
    anchored pattern silently stopped matching them -- a leak this assertion
    would have reported as clean.
    """
    before = _harness_rows(owner_engine)
    run_burst(contenders=12, admitted=2)
    assert _harness_rows(owner_engine) == before == (0, 0)


def _harness_rows(engine: Engine) -> tuple[int, int]:
    with engine.connect() as conn:
        ledger = conn.execute(
            text("SELECT count(*) FROM budget_ledger WHERE scope LIKE '%%suite-burst-%%'")
        ).scalar_one()
        reservations = conn.execute(
            text("SELECT count(*) FROM budget_reservations WHERE scope LIKE '%%suite-burst-%%'")
        ).scalar_one()
    return int(ledger), int(reservations)


# ===========================================================================
# sabotage: the check must be able to FAIL
# ===========================================================================
@pytest.mark.parametrize(
    ("field", "value", "expected_fragment"),
    [
        # A cap that let one extra call through -- the bypass being graded.
        ("granted", ADMITTED + 1, "expected exactly"),
        # A cap that admitted fewer than it allows: broken product, safe-looking.
        ("granted", ADMITTED - 1, "expected exactly"),
        # Spend past the cap while every grant is in flight.
        ("spend_while_open", expected_reserve() * ADMITTED + 1, "exactly on the cap"),
        # A refusal that was NOT the cap: a dropped connection masquerading.
        ("refusal_sqlstates", ("KS006", "08006"), "not the cap refusing"),
        # A retry that got through the exhausted cap.
        ("retries_granted", 1, "got through the cap"),
        # A retry that stored a reservation past the cap.
        ("reservations_after_retries", ADMITTED + 1, "stored a reservation past the cap"),
        # The ledger not holding what the calls actually reported.
        ("final_spend", 1, "must hold the reported cost"),
        # A refusal with no cap_hit audit row: R17's evidence missing.
        ("cap_hit_audit_rows", 3, "cap_hit audit row"),
        # The stubbed alert not firing for every cap hit.
        ("alerts_fired", 0, "alert must fire"),
        # The `spent <= cap` backstop dropped from under the trigger.
        ("backstop_present", False, "backstop"),
        # A ledger row over its cap: the thing the whole design forbids.
        ("ledger_violations", 1, "over their cap"),
        # An outcome that was neither a grant nor a cap hit.
        ("other", ("provider_error:boom",), "unexpected outcomes"),
        # -- the evidence dimensions, one sabotage each ----------------------
        # A second way to release money appeared in the source.
        ("release_sites", ("budget.py:1", "llm.py:2"), "exactly ONE place"),
        # ...or the chokepoint vanished entirely.
        ("release_sites", (), "exactly ONE place"),
        # BLOCKER: a post-send failure refunded itself.
        ("post_send_spend", 0, "CHARGED IN FULL"),
        # ...and the other side: a pre-send failure that was charged anyway.
        ("pre_send_spend", expected_reserve(), "must be RELEASED"),
        # BLOCKER 1: a text-returning call with no usage settled at zero.
        ("silent_usage_spend", 0, "UNKNOWN actual"),
        # ...or was reported as a failure rather than charged.
        ("silent_usage_status", "provider_error", "must still report ok"),
        # MAJOR 5: the overspend halt that halts nothing.
        ("after_overspend_status", "ok", "must REFUSE further reservations"),
        # ...or an overspend reported as a success.
        ("overspend_status", "ok", "must report overspend"),
        # BLOCKER 2: the sweeper refunded a call that may have happened.
        ("sweeper_released", expected_reserve(), "released"),
        ("sweeper_charged", 0, "must CHARGE an abandoned reservation"),
        ("sweeper_spend", 0, "must still hold the abandoned reservation"),
        # -- the boundary dimensions, one sabotage each -----------------------
        # THE BLOCKER: the database let a hand-written release through, in one
        # of the spellings the source-level release count does not even match.
        (
            "raw_update_outcomes",
            (
                "schema-qualified:ALLOWED",
                *(f"{name}:KS007" for name, _ in _RAW_RELEASE_SPELLINGS[1:]),
            ),
            "refused by the database",
        ),
        # ...or the trigger was dropped and nothing was refused at all.
        ("raw_update_outcomes", (), "refused by the database"),
        # ...or it refused them and the ledger moved anyway.
        ("raw_update_spend", 0, "hand-written settlement moved the ledger"),
        # ...and the other side: a boundary that refuses EVERY settlement is
        # equally green and completely broken.
        ("raw_update_control_spend", expected_reserve(), "legitimate settlement must still"),
        # MAJOR 5(a): settle() stopped refusing a replayed reservation.
        ("replay_settle_refused", False, "replay receipt must be REFUSED"),
        ("replay_spend", 0, "released the reservation somebody else is holding"),
        # ...or the phase never reached the guard it is testing.
        ("replay_receipt", False, "never reached the guard"),
        # MAJOR 5(b): the sweeper's _refuse_capped_principal was removed.
        ("sweep_as_capped_refused", False, "must REFUSE to run as recon_writer"),
        ("sweep_as_capped_open", 0, "must touch no row at all"),
        ("sweep_as_capped_spend", 0, "as the capped party moved the ledger"),
        # MAJOR 5(c): settle_failed_call accepted a borrowed provider report.
        ("failed_call_priced_refused", False, "must REFUSE ProviderReportedUsage"),
        ("failed_call_priced_spend", 30, "borrowed usage released its reservation"),
    ],
)
def test_one_broken_dimension_makes_the_check_fail(
    field: str, value: object, expected_fragment: str
) -> None:
    """Sabotage, one dimension at a time. Every one of them must be fatal.

    This is the proof that the scorecard row is load-bearing: the verdict is
    computed from the observed vector, so a vector that describes a broken cap
    produces FAIL and says which dimension broke. Without this, "the check
    passed" would only mean "the check ran".
    """
    clean = _clean_vector()
    assert clean.ok, f"the control vector must pass: {clean.failures}"

    broken = _clean_vector()
    setattr(broken, field, value)
    _assess(
        broken,
        admitted=ADMITTED,
        contenders=CONTENDERS,
        cap=broken.reserve_each * ADMITTED,
    )

    assert not broken.ok, f"sabotaging {field}={value!r} did not fail the check"
    assert any(expected_fragment in reason for reason in broken.failures), (
        f"failed for the wrong reason: {broken.failures}"
    )


def test_the_green_control_is_green(owner_engine: Engine) -> None:
    """The no-op control: an untouched vector passes, so the sabotage means something.

    Without it, every sabotage case above would also pass if ``_assess`` simply
    failed everything it was handed.
    """
    clean = _clean_vector()
    _assess(clean, admitted=ADMITTED, contenders=CONTENDERS, cap=clean.reserve_each * ADMITTED)
    assert clean.ok, clean.failures
    assert clean.failures == []


def test_a_failed_vector_renders_as_a_fail_row() -> None:
    """And the FAIL reaches the scorecard as a FAIL row carrying the reason."""
    from recon.suite.checks import CheckResult

    broken = _clean_vector()
    broken.granted = 0
    _assess(broken, admitted=ADMITTED, contenders=CONTENDERS, cap=broken.reserve_each * ADMITTED)
    row = CheckResult.failed(CHECK_NAME, f"{'; '.join(broken.failures)} | {broken.vector()}")

    assert row.status == FAIL
    assert row.ok is False
    assert "expected exactly" in row.detail
