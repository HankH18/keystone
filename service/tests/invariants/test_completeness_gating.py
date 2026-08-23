"""SS5.3 -- correctness under partial-source failure.

    "Any rule whose predicate depends on the **absence** of records from source S --
     C1, C2, C5, C7, C8, C9, C13 -- is skipped for the whole run when S's
     generation-3 load is incomplete, emitting `verdict='unchecked'` with
     `detail.reason='source_incomplete'`, never a conflict. The run is marked
     `degraded`. Presence/agreement tests (C3, C4, C6, C10, C11, C12, C14) still run."

Handing an absence rule an incomplete generation manufactures thousands of false
positives -- this is the whole reason ingest tracks completeness -- so the test does
not merely check a flag: it asserts that **every** absence rule emitted zero
conflicts and stamped every row in its scope, and that the presence rules were
untouched.

The ledger is flipped inside a transaction that is rolled back, so the session's
ingested snapshot survives intact for the rest of the suite.
"""

from __future__ import annotations

import psycopg
import pytest

from recon.invariants.rules import ABSENCE_RULES, load_rules
from recon.invariants.runner import run_invariants

PRESENCE_RULES = {"R-003", "R-004", "R-006", "R-010", "R-011", "R-012", "R-014"}


@pytest.fixture(scope="module")
def baseline_run(ingested_dsn: str):
    """The same run with the ledger untouched -- the control for the row counts."""
    with psycopg.connect(ingested_dsn) as conn:
        return run_invariants(conn, run_id="t6-gating-baseline")


@pytest.fixture(scope="module")
def degraded_run(ingested_dsn: str):
    with psycopg.connect(ingested_dsn) as conn:
        conn.execute(
            "UPDATE source_generations SET complete = false "
            "WHERE generation = 3 AND source_id = 'payments'"
        )
        run = run_invariants(conn, run_id="t6-degraded")
        conn.rollback()
    return run


def test_an_incomplete_load_degrades_the_run(degraded_run) -> None:
    assert degraded_run.status == "degraded"
    assert ("payments", "payment") in degraded_run.incomplete


def test_every_absence_rule_is_skipped_not_fired(degraded_run) -> None:
    outcomes = {outcome.rule_id: outcome for outcome in degraded_run.outcomes}
    for rule_id in sorted(ABSENCE_RULES):
        outcome = outcomes[rule_id]
        assert outcome.skipped, rule_id
        assert outcome.raw_conflicts == 0, rule_id
        assert set(outcome.verdicts) == {"unchecked"}, rule_id


def test_a_skipped_rule_still_stamps_every_row_in_its_scope(degraded_run, baseline_run) -> None:
    """SS5.8's per-record stamping is the grading contract: a silently missing row is
    indistinguishable from a passing one."""
    outcomes = {outcome.rule_id: outcome for outcome in degraded_run.outcomes}
    normal = {outcome.rule_id: outcome for outcome in baseline_run.outcomes}
    for rule_id in sorted(ABSENCE_RULES):
        assert outcomes[rule_id].rows == normal[rule_id].rows


def test_skipped_rules_report_source_incomplete(degraded_run) -> None:
    reasons = {
        (rule_id, reason)
        for rule_id, _version, _ref, _entity, verdict, reason in degraded_run.results
        if rule_id in ABSENCE_RULES and verdict == "unchecked"
    }
    assert reasons == {(rule_id, "source_incomplete") for rule_id in ABSENCE_RULES}


def test_presence_rules_still_run_and_still_fire(degraded_run) -> None:
    outcomes = {outcome.rule_id: outcome for outcome in degraded_run.outcomes}
    for rule_id in sorted(PRESENCE_RULES):
        assert not outcomes[rule_id].skipped, rule_id
    assert outcomes["R-003"].raw_conflicts > 0
    assert outcomes["R-006"].raw_conflicts > 0


def test_the_gate_covers_exactly_the_seven_contract_rules(degraded_run) -> None:
    skipped = {outcome.rule_id for outcome in degraded_run.outcomes if outcome.skipped}
    assert skipped == ABSENCE_RULES
    assert skipped | PRESENCE_RULES | {"R-000"} == {spec.rule_id for spec in load_rules()}
