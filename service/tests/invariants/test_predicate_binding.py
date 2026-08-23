"""Clauses the golden diff cannot grade, bound directly against generation 3.

**Why this file exists.** A mutation sweep over the committed rules found several
predicates that can be DELETED with the golden diff still reporting 3050/3050 matched,
0 FN, 0 FP, 0 mismatches, 0 clean-sample flags:

* the whole of C8's eligibility mask -- SS5.5 names it as the `G22` FP guard -- plus
  the both-sources-dropped NULL branch and the `eligible_count >= 2` gate;
* two thirds of C11's identity triple (`amount_cents`, `type`) and its
  `payer_email_norm` equality, and any window in roughly `[301s, 600000s)`;
* C13's clauses (b) recency-superseding, (c) recency and (d) downstream status, each
  of which survives deletion alone because the 250 non-planted refunds fail all three
  at once.

None of that is a defect in the rules -- the engine's answer is right. It is a
COVERAGE defect: a predicate no fixture can falsify is untested code on a graded path,
and "the golden diff is green" is not evidence about it either way. SS5.6 says so for
C13 in as many words ("Partially-reflected refunds are **not** planted, since the AND
predicate cannot see them"), which is exactly the decision that leaves those clauses
mutation-survivable.

**The method.** Every test below takes a record the engine really classified on the
real generation-3 snapshot, changes ONE thing that one clause reads, and requires the
verdict to move. The edits go into `stg_*` / `er_*` inside a transaction that is rolled
back, the same pattern `test_unchecked_paths.py` established, so the session's ingested
snapshot is untouched for every other test. Each test asserts the BASELINE first --
a test that only checks the mutated state cannot tell a bound clause from a rule that
never fired at all.
"""

from __future__ import annotations

import psycopg
import pytest

from recon.invariants.context import build_context
from recon.invariants.rules import load_rules
from recon.reference import C11_WINDOW_SECONDS

RULES = {spec.rule_id: spec for spec in load_rules()}


@pytest.fixture(scope="module")
def _bound_connection(ingested_dsn: str):
    """One connection with the ER context built and COMMITTED.

    Committed so a test's `rollback()` undoes only that test's edits: a TEMP table
    created inside a transaction a test later rolls back would vanish with it, and
    every following test would fail on a missing `er_*` relation rather than on what
    it was asserting.
    """
    with psycopg.connect(ingested_dsn) as conn:
        build_context(conn)
        conn.commit()
        yield conn
        conn.rollback()


@pytest.fixture
def bound(_bound_connection):
    """The shared connection, rolled back after **every** test, pass or fail.

    Rolling back only on the success path is how one failing test poisons the rest of
    the module with `InFailedSqlTransaction` -- ten red tests reporting a symptom of
    the first one, which is indistinguishable from ten real failures.
    """
    try:
        yield _bound_connection
    finally:
        _bound_connection.rollback()


def _verdicts(conn, rule_id: str) -> dict[str, tuple]:
    with conn.cursor() as cur:
        cur.execute(RULES[rule_id].sql, {"generation": 3})
        return {row[0]: row for row in cur.fetchall()}


def _conflict_payloads(conn, rule_id: str) -> dict[str, list]:
    """`record_ref -> detail.conflicts[]` for every row this rule called a conflict."""
    return {
        ref: row[3]["conflicts"]
        for ref, row in _verdicts(conn, rule_id).items()
        if row[2] == "conflict"
    }


def _fires(conn, rule_id: str, ref: str) -> bool:
    return _verdicts(conn, rule_id)[ref][2] == "conflict"


# ======================================================================================
# C8 -- the eligibility mask (SS5.5's `G22` FP guard) and the household gates
# ======================================================================================


def _a_c8_household(conn) -> tuple[str, str, int, str]:
    """`(dropped student_ref, household_key, eligible_member_count, dropped_source)`."""
    payloads = _conflict_payloads(conn, "R-008")
    assert payloads, "expected R-008 to fire on the committed generation-3 snapshot"
    ref = sorted(payloads)[0]
    observed = payloads[ref][0]["observed_values"]
    return (
        ref,
        observed["household_key"],
        int(observed["eligible_member_count"]),
        observed["dropped_source"],
    )


def _other_eligible_members(conn, household_key: str, exclude: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT h.student_ref FROM er_household h WHERE h.household_key = %s "
            'AND h.student_ref <> %s ORDER BY h.student_ref COLLATE "C"',
            (household_key, exclude),
        )
        return [row[0] for row in cur.fetchall()]


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        # SS5.5 C8: "excluded from the mask when `GRADE_ORDER[grade_norm] <
        # GRADE_ORDER[ENROLLMENT_GRADE_FLOOR]`". PK is -1, the floor K is 0.
        (
            "grade below the enrollment floor",
            "UPDATE stg_student SET grade = 'Pre-K', grade_norm = 'PK', grade_ord = -1 "
            "WHERE source_ref = %s AND generation = 3",
        ),
        # "...or `student.status == 'withdrawn'`"
        (
            "withdrawn student",
            "UPDATE stg_student SET status = 'withdrawn', status_norm = 'withdrawn', "
            "status_compare = 'withdrawn' WHERE source_ref = %s AND generation = 3",
        ),
    ],
)
def test_an_ineligible_dropped_child_is_not_c8(bound, label: str, statement: str) -> None:
    """Each exclusion in SS5.5's C8 mask, bound one at a time.

    The mask is what stops a household from being reported for a child who was never
    expected downstream in the first place. Deleting any of its clauses leaves the
    golden diff green, so the guard has to be bound here or not at all.
    """
    dropped, _household, _count, _source = _a_c8_household(bound)
    assert _fires(bound, "R-008", dropped), f"baseline: {dropped} must be a C8"
    with bound.cursor() as cur:
        cur.execute(statement, (dropped,))
    assert not _fires(bound, "R-008", dropped), f"{label} must remove the child from the mask"
    bound.rollback()


def test_a_dropped_child_whose_enrollment_is_withdrawn_is_not_c8(bound) -> None:
    """ "...or their enrollment `stage_funnel IN {withdrawn, refunded}`"."""
    dropped, _household, _count, _source = _a_c8_household(bound)
    assert _fires(bound, "R-008", dropped)
    with bound.cursor() as cur:
        cur.execute(
            "UPDATE stg_enrollment SET stage = 'withdrawn', stage_funnel = 'withdrawn' "
            "WHERE source_ref = (SELECT survived_enrollment_ref FROM er_person "
            "                     WHERE student_ref = %s) AND generation = 3",
            (dropped,),
        )
        assert cur.rowcount == 1, "expected the dropped child to have one enrollment"
    assert not _fires(bound, "R-008", dropped)
    bound.rollback()


def test_two_candidate_drops_in_one_household_fire_nothing(bound) -> None:
    """SS5.5 C8: "exactly one of the downstream sources".

    When `crm` is missing exactly one eligible child AND `payments` is missing exactly
    one, the household has two candidate drops -- not the C8 shape -- so
    `dropped_source` is NULL and nothing fires. This is the branch a mutation to
    `WHEN false THEN NULL` deletes with no effect on the grade.
    """
    dropped, household, _count, source = _a_c8_household(bound)
    assert _fires(bound, "R-008", dropped)
    others = _other_eligible_members(bound, household, dropped)
    assert others, "expected a multi-child household"
    # `contact_count = 0` IS "absent from crm" and `payment_count = 0` IS "absent from
    # payments" -- SS5.5 C8's two pinned presence predicates, read off the cascade.
    # Knock the sibling out of the OTHER source, so each source is now missing exactly
    # one eligible child and the household has two candidate drops.
    other_column = "payment_count" if source == "crm" else "contact_count"
    with bound.cursor() as cur:
        cur.execute(
            f"UPDATE er_person SET {other_column} = 0 WHERE student_ref = %s",
            (others[0],),
        )
    assert not _fires(bound, "R-008", dropped)
    assert not _fires(bound, "R-008", others[0])
    bound.rollback()


def test_a_household_with_one_eligible_child_fires_nothing(bound) -> None:
    """The `eligible_count >= 2` gate, which SS5.5's C8 row does not state.

    With exactly one eligible child, "all OTHER eligible children are present" is
    vacuously true and the literal contract text would fire -- on the committed
    fixtures that is 45 further households, two of whose children are in
    `golden/clean-sample.json`. The gate is the FP-safe reading and R-008's header
    documents it; this binds it.
    """
    dropped, household, count, _source = _a_c8_household(bound)
    assert _fires(bound, "R-008", dropped)
    others = _other_eligible_members(bound, household, dropped)
    assert len(others) >= count - 1
    with bound.cursor() as cur:
        # Make every other member ineligible: the dropped child stays absent from its
        # source, but is now the household's ONLY eligible member.
        cur.execute(
            "UPDATE stg_student SET grade = 'Pre-K', grade_norm = 'PK', grade_ord = -1 "
            "WHERE source_ref = ANY(%s) AND generation = 3",
            (others,),
        )
    assert not _fires(bound, "R-008", dropped)
    bound.rollback()


# ======================================================================================
# C11 -- the identity triple and the strict `< 600s` window
# ======================================================================================


def _a_c11_pair(conn) -> tuple[str, str]:
    payloads = _conflict_payloads(conn, "R-011")
    assert payloads, "expected R-011 to fire on the committed generation-3 snapshot"
    refs = payloads[sorted(payloads)[0]][0]["payment_refs"]
    return refs[0], refs[1]


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        (
            "different amount_cents",
            "UPDATE stg_payment SET amount_cents = amount_cents + 1 "
            "WHERE source_ref = %s AND generation = 3",
        ),
        (
            "different type",
            "UPDATE stg_payment SET type = CASE WHEN type = 'fee' THEN 'tuition' ELSE 'fee' END, "
            " type_norm = CASE WHEN type = 'fee' THEN 'tuition' ELSE 'fee' END "
            "WHERE source_ref = %s AND generation = 3",
        ),
        (
            # `payer_email_norm` is a stored generated column mirroring `email_norm`
            # (`GENERATED ALWAYS AS (email_norm)`), so the normalized value is written
            # once by Python and aliased -- SS2's "normalization is materialized
            # upstream", not recomputed in SQL. Edit the column it mirrors.
            "different payer_email_norm",
            "UPDATE stg_payment SET email_norm = email_norm || '.invalid' "
            "WHERE source_ref = %s AND generation = 3",
        ),
    ],
)
def test_each_leg_of_c11s_identity_triple_is_required(bound, label: str, statement: str) -> None:
    """SS5.5 C11: "equal `(payer_email_norm, amount_cents, type)`".

    Golden plants no near-miss decoys, so on the committed dataset the same-person +
    same-window join alone already selects exactly the 50 planted pairs and all three
    equality legs can be dropped with a green diff. Each is bound here by breaking it
    on one member of a real pair.
    """
    left, right = _a_c11_pair(bound)
    assert _fires(bound, "R-011", left) and _fires(bound, "R-011", right)
    with bound.cursor() as cur:
        cur.execute(statement, (right,))
    assert not _fires(bound, "R-011", left), f"{label} must break the pair"
    assert not _fires(bound, "R-011", right)
    bound.rollback()


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (C11_WINDOW_SECONDS - 1, True),  # 599s -- inside
        (C11_WINDOW_SECONDS, False),  # 600s -- SS5.2 pins the window STRICT
        (C11_WINDOW_SECONDS + 1, False),
    ],
)
def test_the_c11_window_is_strictly_less_than_600s(bound, delta: int, expected: bool) -> None:
    """SS5.2: "C11's window is `abs(occurred_at delta) < C11_WINDOW_SECONDS` (600s),
    strictly". The boundary is untestable from golden -- planted pairs are <=300s
    apart and legitimate repeats >=1200s, so any window in `[301, 600000)` grades
    green -- and a `<=` here would be a silent widening.
    """
    left, right = _a_c11_pair(bound)
    assert _fires(bound, "R-011", left)
    with bound.cursor() as cur:
        cur.execute(
            "UPDATE stg_payment SET occurred_at = "
            "  (SELECT occurred_at FROM stg_payment WHERE source_ref = %s AND generation = 3)"
            "  + make_interval(secs => %s) "
            "WHERE source_ref = %s AND generation = 3",
            (left, delta, right),
        )
    assert _fires(bound, "R-011", left) is expected
    bound.rollback()


# ======================================================================================
# C13 -- clauses (b), (c) and (d), each of which survives deletion alone
# ======================================================================================


def _a_c13_payment(conn) -> tuple[str, str, str]:
    """`(payment_ref, enrollment_ref, student_ref)` of one real C13."""
    payloads = _conflict_payloads(conn, "R-013")
    assert payloads, "expected R-013 to fire on the committed generation-3 snapshot"
    ref = sorted(payloads)[0]
    payload = payloads[ref][0]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT person.student_ref FROM er_payment_person link "
            "JOIN er_person person ON person.person_key = link.person_key "
            "WHERE link.payment_ref = %s",
            (ref,),
        )
        student_ref = cur.fetchone()[0]
    return ref, payload["enrollment_refs"][0], student_ref


def test_a_later_paid_payment_of_the_same_type_suppresses_c13(bound) -> None:
    """SS5.5 C13 clause (b): "**no** later `paid` payment of the same `type` exists
    for that person"."""
    payment, _enrollment, _student = _a_c13_payment(bound)
    assert _fires(bound, "R-013", payment)
    with bound.cursor() as cur:
        # Re-point one arbitrary paid payment of the same type at this person, later.
        cur.execute(
            "SELECT p.source_ref FROM stg_payment p WHERE p.generation = 3 "
            "AND p.status = 'paid' AND p.source_ref <> %s "
            "AND p.type = (SELECT type FROM stg_payment WHERE source_ref = %s AND generation = 3)"
            ' ORDER BY p.payment_id COLLATE "C" LIMIT 1',
            (payment, payment),
        )
        donor = cur.fetchone()[0]
        cur.execute(
            "UPDATE stg_payment SET occurred_at = "
            " (SELECT occurred_at FROM stg_payment WHERE source_ref = %s AND generation = 3)"
            " + interval '1 day' WHERE source_ref = %s AND generation = 3",
            (payment, donor),
        )
        cur.execute(
            "UPDATE er_payment_person SET person_key = "
            " (SELECT person_key FROM er_payment_person WHERE payment_ref = %s), "
            " student_ref = (SELECT student_ref FROM er_payment_person WHERE payment_ref = %s) "
            "WHERE payment_ref = %s",
            (payment, payment, donor),
        )
    assert not _fires(bound, "R-013", payment)
    bound.rollback()


def test_a_later_REFUNDED_payment_of_the_same_type_does_not_suppress_c13(bound) -> None:
    """The other half of clause (b), and the reason `superseded` filters on
    `status = 'paid'`.

    "no later **paid** payment" is what SS5.5 says. Without the status filter a later
    *refunded* payment of the same type also suppresses C13 -- unobservable on the
    committed dataset (no refund is superseded by a later refund of the same type),
    which is precisely why it needs an assertion of its own.
    """
    payment, _enrollment, _student = _a_c13_payment(bound)
    assert _fires(bound, "R-013", payment)
    with bound.cursor() as cur:
        cur.execute(
            "SELECT p.source_ref FROM stg_payment p WHERE p.generation = 3 "
            "AND p.source_ref <> %s "
            "AND p.type = (SELECT type FROM stg_payment WHERE source_ref = %s AND generation = 3)"
            ' ORDER BY p.payment_id COLLATE "C" LIMIT 1',
            (payment, payment),
        )
        donor = cur.fetchone()[0]
        cur.execute(
            "UPDATE stg_payment SET status = 'refunded', status_norm = 'refunded', "
            " refunded_at = occurred_at, occurred_at = "
            " (SELECT occurred_at FROM stg_payment WHERE source_ref = %s AND generation = 3)"
            " + interval '1 day' WHERE source_ref = %s AND generation = 3",
            (payment, donor),
        )
        cur.execute(
            "UPDATE er_payment_person SET person_key = "
            " (SELECT person_key FROM er_payment_person WHERE payment_ref = %s), "
            " student_ref = (SELECT student_ref FROM er_payment_person WHERE payment_ref = %s) "
            "WHERE payment_ref = %s",
            (payment, payment, donor),
        )
    assert _fires(bound, "R-013", payment), "a later REFUND is not a later PAID payment"
    bound.rollback()


def test_a_refund_predating_the_enrollment_update_is_not_c13(bound) -> None:
    """SS5.5 C13 clause (c): "`refunded_at` post-dates the enrollment row's
    `updated_at`".

    This is the SINGLE read of `updated_at` any rule is permitted (SS1, `G26`) and it
    is the clause with zero discriminating power in the graded dataset: over all 425
    refunded payments the `(b AND d AND NOT c)` cell is empty, so deleting it changes
    nothing the golden diff can see.
    """
    payment, enrollment, _student = _a_c13_payment(bound)
    assert _fires(bound, "R-013", payment)
    with bound.cursor() as cur:
        cur.execute(
            "UPDATE stg_enrollment SET updated_at = "
            " (SELECT refunded_at FROM stg_payment WHERE source_ref = %s AND generation = 3)"
            " + interval '1 second' WHERE source_ref = %s AND generation = 3",
            (payment, enrollment),
        )
        assert cur.rowcount == 1
    assert not _fires(bound, "R-013", payment)
    bound.rollback()


def test_a_refund_whose_enrollment_moved_off_a_paid_stage_is_not_c13(bound) -> None:
    """SS5.5 C13 clause (d), enrollment half: "the enrollment `stage_funnel IN
    PAID_IMPLYING_STAGES`". A correctly-reflected refund is not a conflict."""
    payment, enrollment, _student = _a_c13_payment(bound)
    assert _fires(bound, "R-013", payment)
    with bound.cursor() as cur:
        cur.execute(
            "UPDATE stg_enrollment SET stage = 'refunded', stage_funnel = 'refunded' "
            "WHERE source_ref = %s AND generation = 3",
            (enrollment,),
        )
    assert not _fires(bound, "R-013", payment)
    bound.rollback()


def test_a_refund_whose_student_is_no_longer_enrolled_is_not_c13(bound) -> None:
    """SS5.5 C13 clause (d), student half: "`STATUS_TO_FUNNEL(student.status) ==
    enrolled`". SS5.6 requires BOTH downstream fields left stale, so either one being
    updated is a reflected refund."""
    payment, _enrollment, student = _a_c13_payment(bound)
    assert _fires(bound, "R-013", payment)
    with bound.cursor() as cur:
        cur.execute(
            "UPDATE stg_student SET status = 'withdrawn', status_norm = 'withdrawn', "
            "status_compare = 'withdrawn' WHERE source_ref = %s AND generation = 3",
            (student,),
        )
        assert cur.rowcount == 1
    assert not _fires(bound, "R-013", payment)
    bound.rollback()


# ======================================================================================
# C12 -- the unattributable-payment path SS5.7 rule 3 exists to suppress
# ======================================================================================


def test_an_unattributable_payment_with_a_wrong_amount_is_c12_suppressed_by_c2(bound) -> None:
    """SS5.7 rule 3: "C2 over C12/C11 -- an unattributable payment cannot have a wrong
    amount or a duplicate partner."

    That rule can only do its job if `R-012` FIRES on such a payment in the first
    place. The population is empty on the committed dataset -- 0 unattributed payments
    carry a resolvable `metadata.program` -- so nothing in `golden/` exercises it, and
    the rule previously carried an `identity_refs IS NOT NULL` clause that SS5.5's C12
    predicate does not state, which made PRECEDENCE rule 3 dead code and would
    under-count SS9.1(b)'s RAW C12 column on a reseed.

    The fix is not simply deleting that clause: SS5.5 pins C12's `entity_refs` as
    "identity refs + payment ref" and `conflict_refs` REQUIRES at least one identity
    ref, so a bare deletion raises instead of firing. SS4.1 supplies the right answer
    -- for a payment attributed to no person, that payment's own ref IS its identity
    ref -- and this test is what holds the rule to it end to end: it fires, its
    `entity_refs` are exactly the payment ref, and rule 3 removes it.
    """
    from recon.invariants.runner import _build_conflict
    from recon.reference import apply_precedence

    with bound.cursor() as cur:
        cur.execute(
            "SELECT p.source_ref, p.type FROM stg_payment p "
            "LEFT JOIN er_payment_person l ON l.payment_ref = p.source_ref "
            "WHERE p.generation = 3 AND l.payment_ref IS NULL "
            'ORDER BY p.payment_id COLLATE "C" LIMIT 1'
        )
        row = cur.fetchone()
        assert row is not None, "expected the 200 planted C2 payments to be unattributed"
        payment_ref, payment_type = row
        # Give it a resolvable program and an amount off the fee schedule.
        cur.execute(
            "UPDATE stg_payment SET program_norm = 'Lower School', amount_cents = "
            "  (SELECT amount_cents + 1 FROM ref_fee_schedule "
            "    WHERE program_norm = 'Lower School' AND payment_type = %s) "
            "WHERE source_ref = %s AND generation = 3",
            (payment_type, payment_ref),
        )
        assert cur.rowcount == 1

    raw = [
        _build_conflict(payload)
        for rule_id in ("R-002", "R-012")
        for payloads in _conflict_payloads(bound, rule_id).values()
        for payload in payloads
    ]
    by_type: dict[str, list] = {}
    for conflict in raw:
        by_type.setdefault(conflict.type, []).append(conflict)

    c12 = [c for c in by_type.get("C12", ()) if payment_ref in c.entity_refs]
    assert len(c12) == 1, "R-012 must fire on an unattributable payment with a wrong amount"
    assert c12[0].entity_refs == (payment_ref,), (
        "SS4.1: an unattributed payment's own ref IS its identity ref, so C12's "
        "'identity refs + payment ref' collapses to the one ref"
    )
    assert any(payment_ref in c.entity_refs for c in by_type["C2"])

    surviving = apply_precedence(raw)
    assert not [c for c in surviving if c.type == "C12" and payment_ref in c.entity_refs], (
        "SS5.7 rule 3 (C2 over C12) must remove it -- that is the suppression the "
        "rule declines to do for itself"
    )
    bound.rollback()
