"""SS5.8's `unchecked` reasons: every one is reachable, and none is ever a conflict.

    "None of these is ever a crash and none is ever a conflict."

SS7 makes the general rule explicit: "A well-formed record carrying an unrecognised
enum *value* is **never** malformed: it ingests normally, `norm_enum` returns `None`,
and every rule scoping it yields `verdict='unchecked'` with
`detail.reason='unmapped_enum'`."

The unmappable-value cases are injected into `stg_*` inside a transaction that is
rolled back, so the session's ingested snapshot is unchanged for every other test.
The `er_*` tables built for the run are unaffected by these edits -- the records still
exist and still resolve; only an enum *value* changes -- which is precisely the
situation SS7 describes.
"""

from __future__ import annotations

import psycopg
import pytest

from recon.invariants.context import build_context
from recon.invariants.rules import load_rules
from recon.invariants.runner import run_invariants

RULES = {spec.rule_id: spec for spec in load_rules()}


def _run_one(conn, rule_id: str, generation: int = 3):
    spec = RULES[rule_id]
    with conn.cursor() as cur:
        cur.execute(spec.sql, {"generation": generation})
        return cur.fetchall()


@pytest.fixture(scope="module")
def mutable(ingested_dsn: str):
    """A connection with the ER context built, held open for in-transaction edits."""
    with psycopg.connect(ingested_dsn) as conn:
        build_context(conn)
        # Commit the TEMP-table creation so a test's `rollback()` undoes only that
        # test's edits. A temp table created inside the transaction a test later rolls
        # back would vanish with it, and every following test would fail on a missing
        # `er_*` relation rather than on what it was actually asserting.
        conn.commit()
        yield conn
        conn.rollback()


def test_reasons_seen_on_a_clean_run_are_all_in_the_pinned_vocabulary(invariant_run) -> None:
    reasons = {
        (rule_id, reason)
        for rule_id, _v, _ref, _e, verdict, reason in invariant_run.results
        if verdict == "unchecked"
    }
    assert reasons == {
        ("R-000", "no_rule_in_scope"),
        ("R-006", "missing_operand"),
        ("R-014", "missing_operand"),
        ("R-012", "enrollment_unattributed"),
    }


def test_an_unmappable_payment_type_is_unchecked_never_a_conflict(mutable) -> None:
    """SS7: an unrecognised enum value ingests normally and every rule scoping it
    yields `unchecked`. It must never become a wrong-amount conflict just because the
    fee schedule has no cell for it."""
    with mutable.cursor() as cur:
        cur.execute(
            "SELECT source_ref FROM stg_payment WHERE generation = 3 "
            'ORDER BY payment_id COLLATE "C" LIMIT 1'
        )
        ref = cur.fetchone()[0]
        cur.execute(
            "UPDATE stg_payment SET type = 'subscription', type_norm = NULL "
            "WHERE source_ref = %s AND generation = 3",
            (ref,),
        )
    rows = {row[0]: row for row in _run_one(mutable, "R-012")}
    _ref, _entity, verdict, detail = rows[ref]
    assert verdict == "unchecked"
    assert detail == {"reason": "unmapped_enum"}
    mutable.rollback()


def test_an_unmappable_grade_makes_the_comparison_unchecked_not_a_disagreement(
    mutable,
) -> None:
    """SS5.1: a `None` operand yields `unchecked` for that comparison and is **never**
    a disagreement -- so blanking a normalized grade cannot invent a C6."""
    with mutable.cursor() as cur:
        cur.execute(
            "SELECT c.source_ref, c.grade FROM stg_crm_contact c "
            "WHERE c.generation = 3 AND c.grade_norm IS NOT NULL "
            'ORDER BY c.crm_id COLLATE "C" LIMIT 1'
        )
        contact_ref, _grade = cur.fetchone()
        cur.execute(
            "UPDATE stg_crm_contact SET grade = 'Form IV', grade_norm = NULL, grade_ord = NULL "
            "WHERE source_ref = %s AND generation = 3",
            (contact_ref,),
        )
        cur.execute(
            "SELECT student_ref FROM er_contact_student WHERE contact_ref = %s", (contact_ref,)
        )
        row = cur.fetchone()
    assert row is not None, "expected the first contact to be student-linked"
    student_ref = row[0]
    rows = {result[0]: result for result in _run_one(mutable, "R-006")}
    _ref, _entity, verdict, detail = rows[student_ref]
    assert verdict in {"ok", "unchecked"}
    if verdict == "unchecked":
        assert detail["reason"] in {"missing_operand", "unmapped_enum", "unparseable_value"}
    else:
        assert detail is None
    mutable.rollback()


def test_a_deal_whose_person_set_is_empty_is_unchecked(mutable) -> None:
    """SS5.5 C9: "An empty person set yields `verdict='unchecked'`,
    `detail.reason='deal_unresolved'`" -- never a conflict."""
    with mutable.cursor() as cur:
        cur.execute(
            "SELECT e.source_ref, e.crm_deal_id FROM stg_enrollment e "
            "JOIN er_deal_person d ON d.deal_id = e.crm_deal_id "
            "WHERE e.generation = 3 AND e.crm_deal_id IS NOT NULL "
            'ORDER BY e.enrollment_id COLLATE "C" LIMIT 1'
        )
        enrollment_ref, deal_id = cur.fetchone()
        cur.execute("DELETE FROM er_deal_person WHERE deal_id = %s", (deal_id,))
    rows = {row[0]: row for row in _run_one(mutable, "R-009")}
    _ref, _entity, verdict, detail = rows[enrollment_ref]
    assert verdict == "unchecked"
    assert detail == {"reason": "deal_unresolved"}
    mutable.rollback()


def test_an_unattributed_payment_never_produces_a_refund_conflict(mutable) -> None:
    """SS4.4: `R-013` yields `unchecked` with `enrollment_unattributed` -- never a
    conflict -- when the payment resolves to no enrollment."""
    with mutable.cursor() as cur:
        cur.execute(
            "SELECT p.source_ref FROM stg_payment p "
            "JOIN er_payment_enrollment a ON a.payment_ref = p.source_ref "
            "WHERE p.generation = 3 AND p.status = 'refunded' "
            'ORDER BY p.payment_id COLLATE "C" LIMIT 1'
        )
        payment_ref = cur.fetchone()[0]
        cur.execute("DELETE FROM er_payment_enrollment WHERE payment_ref = %s", (payment_ref,))
    rows = {row[0]: row for row in _run_one(mutable, "R-013")}
    _ref, _entity, verdict, detail = rows[payment_ref]
    assert verdict == "unchecked"
    assert detail == {"reason": "enrollment_unattributed"}
    mutable.rollback()


def test_no_rule_raises_on_a_null_heavy_snapshot(ingested_dsn) -> None:
    """SS5.8 has no crash in its vocabulary. Every rule must return a verdict for a
    record whose optional fields are all NULL rather than raising."""
    with psycopg.connect(ingested_dsn) as conn:
        context = build_context(conn)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE stg_crm_contact SET email = NULL, email_norm = NULL, "
                "first_name = NULL, first_norm = NULL, last_name = NULL, last_norm = NULL, "
                "dob = NULL, dob_norm = NULL, grade = NULL, grade_norm = NULL, "
                "grade_ord = NULL, lifecycle_stage = NULL, lifecycle_norm = NULL, "
                "external_id = NULL WHERE generation = 3"
            )
            cur.execute(
                "UPDATE stg_payment SET payer_email = NULL, email_norm = NULL, "
                "external_ref = NULL, payment_metadata = NULL, amount_cents = NULL, "
                "occurred_at = NULL, refunded_at = NULL WHERE generation = 3"
            )
        run = run_invariants(conn, run_id="t6-nulls", context=context)
        conn.rollback()
    assert len(run.results) > 0
    for _rule, _version, _ref, _entity, verdict, reason in run.results:
        assert verdict in {"ok", "conflict", "unchecked"}
        assert (reason is not None) == (verdict == "unchecked")
