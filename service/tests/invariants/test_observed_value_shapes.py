"""SS5.4's `observed_values` construction, where the committed golden set cannot bind it.

Two blind spots the golden diff has, both structural rather than accidental:

**1. The multi-valued keys are never exercised beyond one element.** SS5.4 ruling 16
goes to some length to pin how three inherently multi-valued keys are BUILT, because
"pinning a key without pinning how its value is built leaves the two sides free to
hash different bytes for the same conflict". On the committed dataset only one of the
three ever carries more than one element:

    C1.paid_payment_refs              {1: 500}
    C9.deal_person_refs               {0: 50, 1: 50}
    C4.student_guardian_email_norms   {1: 157, 2: 93}   <- the only one that varies

So `jsonb_agg(... ORDER BY ... COLLATE "C")` in `R-001` can be replaced by
`jsonb_build_array(min(...))` and the diff still reports 3050/3050 matched. The tests
below construct the multi-element case directly and assert both the ORDER (byte, not
locale) and that the sequence reaches `canon_value`'s sequence case (SS2.5) rather
than being flattened.

**2. The SQL renders SS2.5's timestamp and date forms a second time.** SS2.5 says
`canon_value` is what both sides use "wherever a value is ... written to
`observed_values`", and SS2.5's closing line says `rules/*.sql` never build
`observed_values` strings -- but four rules build them with `to_char`. `grade_run`
compares `observed_values` BY VALUE, so a divergence would surface as a field-exactness
mismatch on the committed data; nothing pins the two renderings together on a value
the fixtures happen not to contain (a sub-second timestamp, a non-UTC offset). The
last tests pin them.
"""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from recon.invariants.context import build_context
from recon.invariants.rules import load_rules

# `_build_conflict` is the runner's private assembler, and it is deliberately the
# thing under test: it is what turns a rule's component lists into `entity_refs`,
# `observed_values` and the SS5.4 fingerprint. Re-deriving that here would be a second
# implementation of exactly the step these tests exist to pin.
from recon.invariants.runner import _build_conflict
from recon.reference import canon_value, fingerprint

RULES = {spec.rule_id: spec for spec in load_rules()}


@pytest.fixture(scope="module")
def _shape_connection(ingested_dsn: str):
    with psycopg.connect(ingested_dsn) as conn:
        build_context(conn)
        conn.commit()
        yield conn
        conn.rollback()


@pytest.fixture
def shaped(_shape_connection):
    try:
        yield _shape_connection
    finally:
        _shape_connection.rollback()


def _payloads(conn, rule_id: str) -> dict[str, list]:
    with conn.cursor() as cur:
        cur.execute(RULES[rule_id].sql, {"generation": 3})
        return {row[0]: row[3]["conflicts"] for row in cur.fetchall() if row[2] == "conflict"}


# ======================================================================================
# the multi-valued keys, at cardinality > 1
# ======================================================================================


def test_c1_paid_payment_refs_is_a_sorted_multi_element_sequence(shaped) -> None:
    """SS5.4 ruling 16: "the sorted `payments:payment:<id>` refs of the person's `paid`
    payments" -- plural, and sorted by BYTE order.

    Every C1 in `golden/conflicts.json` carries exactly one, so nothing in the graded
    set can tell a sorted aggregate from `min()`. This gives one C1 person a second
    `paid` payment and requires both refs, in byte order.
    """
    payloads = _payloads(shaped, "R-001")
    assert payloads, "expected R-001 to fire on generation 3"
    subject = sorted(payloads)[0]
    original = payloads[subject][0]["observed_values"]["paid_payment_refs"]
    assert len(original) == 1, "baseline: golden's C1 population is single-payment"

    with shaped.cursor() as cur:
        cur.execute(
            "SELECT link.person_key FROM er_person person "
            "JOIN er_payment_person link ON link.person_key = person.person_key "
            "WHERE person.student_ref = %s LIMIT 1",
            (subject,),
        )
        person_key = cur.fetchone()[0]
        # Re-point one other `paid` payment at this person. Its ref must sort BELOW
        # the existing one for the assertion to distinguish sorted-aggregate from
        # "whatever order the join produced", so pick the smallest available.
        cur.execute(
            "SELECT p.source_ref FROM stg_payment p "
            "JOIN er_payment_person l ON l.payment_ref = p.source_ref "
            "WHERE p.generation = 3 AND p.status = 'paid' AND p.source_ref < %s "
            'ORDER BY p.source_ref COLLATE "C" LIMIT 1',
            (original[0],),
        )
        donor = cur.fetchone()[0]
        cur.execute(
            "UPDATE er_payment_person SET person_key = %s WHERE payment_ref = %s",
            (person_key, donor),
        )

    payload = _payloads(shaped, "R-001")[subject][0]
    refs = payload["observed_values"]["paid_payment_refs"]
    assert refs == sorted([donor, *original]), "two refs, ascending by byte order"
    assert refs[0] == donor, "the aggregate is sorted, not `min()`-collapsed"

    # SS2.5: the sequence must reach `canon_value`'s sequence case in the digest, not
    # be flattened -- `canon_value(["a"]) != canon_value("a")` is a graded property.
    conflict = _build_conflict(payload)
    assert conflict.observed_values["paid_payment_refs"] == refs
    assert conflict.fingerprint == fingerprint(
        "C1", conflict.entity_refs, (), conflict.observed_values
    )
    assert canon_value(refs) != canon_value(refs[0])
    shaped.rollback()


def test_c9_deal_person_refs_is_one_sorted_anchor_ref_per_person(shaped) -> None:
    """SS5.4 ruling 16: "**one `anchor_ref` (SS4.1) per person** in the mispointed
    deal's `D2`-resolved person set, sorted -- **not** each person's identity-ref set,
    and not a `person_key`".

    The household-deal case ruling 16 is written for -- 2-4 sibling persons, one
    anchor ref each -- never appears in `golden/conflicts.json` (the 50 branch-1
    entries carry `[]` and the 50 branch-2 entries carry one element), so the
    cardinality this key exists to express is unbound by the grade.
    """
    payloads = _payloads(shaped, "R-009")
    subject = next(
        ref
        for ref in sorted(payloads)
        if len(payloads[ref][0]["observed_values"]["deal_person_refs"]) == 1
    )
    payload = payloads[subject][0]
    deal_id = payload["observed_values"]["enrollment.crm_deal_id"]
    original = payload["observed_values"]["deal_person_refs"]

    with shaped.cursor() as cur:
        cur.execute(
            "SELECT person.person_key, person.anchor_ref FROM er_person person "
            "WHERE person.anchor_ref < %s "
            "AND person.person_key NOT IN (SELECT person_key FROM er_deal_person "
            "                               WHERE deal_id = %s) "
            'ORDER BY person.anchor_ref COLLATE "C" LIMIT 1',
            (original[0], deal_id),
        )
        extra_key, extra_anchor = cur.fetchone()
        cur.execute(
            "INSERT INTO er_deal_person (deal_ref, deal_id, person_key) "
            "SELECT deal_ref, deal_id, %s FROM er_deal_person WHERE deal_id = %s LIMIT 1",
            (extra_key, deal_id),
        )

    payload = _payloads(shaped, "R-009")[subject][0]
    refs = payload["observed_values"]["deal_person_refs"]
    assert refs == sorted([extra_anchor, *original]), "one anchor ref per person, sorted"
    identity_prefixes = ("appdb:student:", "crm:contact:", "payments:payment:")
    assert all(ref.startswith(identity_prefixes) for ref in refs)
    assert not any(len(ref) == 36 and ref.count("-") == 4 for ref in refs), (
        "anchor refs, never `person_key` UUIDs (SS5.4 ruling 16)"
    )
    conflict = _build_conflict(payload)
    assert conflict.observed_values["deal_person_refs"] == refs
    shaped.rollback()


def test_c4_student_guardian_email_norms_already_varies_in_golden(shaped) -> None:
    """The one multi-valued key the committed set DOES bind, kept as the control.

    A cardinality assertion that passes on a key which is single-valued everywhere
    proves nothing about the other two; this shows the assertion has teeth where the
    data exercises it.
    """
    payloads = _payloads(shaped, "R-004")
    sizes = {
        len(payload[0]["observed_values"]["student_guardian_email_norms"])
        for payload in payloads.values()
    }
    assert sizes == {1, 2}
    for ref in sorted(payloads):
        emails = payloads[ref][0]["observed_values"]["student_guardian_email_norms"]
        assert emails == sorted(emails), "SS5.4 ruling 16: sorted, NULLs dropped"
        assert None not in emails


# ======================================================================================
# SS2.5's renderings, pinned against the committed serializer
# ======================================================================================


def test_the_sql_timestamp_rendering_equals_canon_value(shaped) -> None:
    """SS2.5 ruling 4: `"%Y-%m-%dT%H:%M:%SZ"`, normalized to UTC, microseconds
    TRUNCATED rather than rounded.

    `R-007` and `R-013` build that string with `to_char(... AT TIME ZONE 'UTC', ...)`
    rather than delegating to `canon_value`, which is the SS0 drift this contract
    exists to prevent. `grade_run` compares `observed_values` by value, so a
    divergence on a value the fixtures CONTAIN would be caught -- this pins the two
    renderings on values they do not contain: a sub-second timestamp, and one whose
    stored offset is not UTC.
    """
    samples = [
        dt.datetime(2010, 4, 5, 1, 2, 3, tzinfo=dt.UTC),
        dt.datetime(2010, 4, 5, 1, 2, 3, 999_999, tzinfo=dt.UTC),
        dt.datetime(2010, 12, 31, 23, 59, 59, 500_000, tzinfo=dt.UTC),
        dt.datetime(2010, 4, 5, 1, 2, 3, tzinfo=dt.timezone(dt.timedelta(hours=-5))),
    ]
    with shaped.cursor() as cur:
        for value in samples:
            cur.execute(
                "SELECT to_char(%s::timestamptz AT TIME ZONE 'UTC', "
                '\'YYYY-MM-DD"T"HH24:MI:SS"Z"\')',
                (value,),
            )
            assert cur.fetchone()[0] == canon_value(value), value


def test_the_sql_date_rendering_equals_canon_value(shaped) -> None:
    """SS2.5: `date -> "YYYY-MM-DD"`. `R-003` and `R-010` build it with
    `to_char(<date>, 'YYYY-MM-DD')`."""
    samples = [dt.date(2010, 4, 5), dt.date(1999, 12, 31), dt.date(2000, 1, 1)]
    with shaped.cursor() as cur:
        for value in samples:
            cur.execute("SELECT to_char(%s::date, 'YYYY-MM-DD')", (value,))
            assert cur.fetchone()[0] == canon_value(value), value
