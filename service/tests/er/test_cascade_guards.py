"""The five cascade guards no committed test constrained (R9, SS4.2-SS4.4).

A mutation sweep over `recon/er.py` found five rules that could be **deleted**
with the committed suite green:

=====  ==========================================================  ===============
rule   the guard                                                    what removing it does
=====  ==========================================================  ===============
`P2`   attribute only when **exactly one** household member's       attributes the payment to
       name matches the payment metadata                            the first same-named sibling
`P3`   attribute only when the household has **exactly one** child  attributes an un-named
                                                                    payment to a guessed sibling
`L2`   a shared guardian email links a contact to a student only    links a contact to a child
       when the **names are equal** too                             it only shares an address with
`L3`   name + dob links a contact to a student at all               a real person stops resolving
`E2`   a payment attributed to a person with exactly one            the payment loses its
       enrollment attaches to that enrollment                       enrollment
=====  ==========================================================  ===============

`P2` and `P3` are precisely the guards R9 names -- *distinct records (e.g.
siblings sharing guardian emails) must never collide*. Their apparent red in a
full run came only from `tests/er/dataset.ensure_history_dataset`, which shells
out to the seed generator and fails when the generator's own self-checks fail;
that is incidental coverage of a different module, not a test of these rules.

These tests take the snapshot straight to `recon.er.resolve`, with no database
and no fixture tree, so each one names the trap and the rule that has to catch
it. Every case carries a **positive control in the same shape**: the guarded rule
still fires when its condition is genuinely met, so "the rule was deleted" and
"the rule was tightened into uselessness" are different failures here.
"""

from __future__ import annotations

from typing import Any

import pytest

from recon.er import Resolution, Snapshot, resolve
from recon.reference import make_ref

GEN = 3


# ======================================================================================
# the smallest records each cascade rule reads
# ======================================================================================


def student(
    sid: str,
    first: str,
    last: str,
    guardian: str | None,
    *,
    dob: str = "2014-03-05",
    guardian2: str | None = None,
) -> dict[str, Any]:
    return {
        "id": sid,
        "first_name": first,
        "last_name": last,
        "dob": dob,
        "grade": "5",
        "guardian_email": guardian,
        "guardian2_email": guardian2,
        "status": "active",
        "student_number": sid,
        "observed_ts": None,
    }


def contact(
    cid: str,
    first: str,
    last: str,
    email: str | None,
    *,
    external_id: str | None = None,
    dob: str | None = None,
) -> dict[str, Any]:
    return {
        "crm_id": cid,
        "email": email,
        "first_name": first,
        "last_name": last,
        "lifecycle_stage": "customer",
        "external_id": external_id,
        "dob": dob,
        "grade": None,
        "observed_ts": None,
    }


def payment(
    pid: str,
    payer_email: str | None,
    metadata: dict[str, Any],
    *,
    external_ref: str | None = None,
) -> dict[str, Any]:
    return {
        "payment_id": pid,
        "payer_email": payer_email,
        "external_ref": external_ref,
        "metadata": metadata,
        "type": "tuition",
        "status": "paid",
        "amount_cents": 25_000,
        "occurred_at": None,
    }


def enrollment(eid: str, sid: str, program: str = "Middle School") -> dict[str, Any]:
    return {
        "id": eid,
        "student_id": sid,
        "program": program,
        "stage": "enrolled",
        "crm_deal_id": None,
        "observed_ts": None,
    }


def student_ref(sid: str) -> str:
    return make_ref("appdb", "student", sid)


def payment_ref(pid: str) -> str:
    return make_ref("payments", "payment", pid)


def contact_ref(cid: str) -> str:
    return make_ref("crm", "contact", cid)


def attributed_student(resolved: Resolution, pid: str) -> str | None:
    """The `appdb:student:` ref `pid` was attributed to, or `None`.

    `payment_person` maps to a **person key**, so the assertion goes through the
    person's own refs: "which child is this payment's money against" is the
    question R9 is about, and a person key alone cannot answer it.
    """
    person = resolved.person_for(payment_ref(pid))
    if person is None:
        return None
    return person.student_ref


# ======================================================================================
# P3 -- "exactly one child" (R9: siblings sharing a guardian email)
# ======================================================================================

SIBLING_EMAIL = "two-kids@example.test"
ONLY_CHILD_EMAIL = "one-kid@example.test"


def _sibling_snapshot() -> Snapshot:
    """One two-child household, one one-child household, one payment into each.

    Both payments carry **no** usable student name, so `P2` cannot fire and `P3`
    is the only rule left. The single-child payment is the control: `P3` is
    supposed to attribute that one.
    """
    return Snapshot(
        generation=GEN,
        students=[
            student("stu-sib-a", "Ada", "Byron", SIBLING_EMAIL),
            student("stu-sib-b", "Blaise", "Byron", SIBLING_EMAIL),
            student("stu-only", "Dot", "Ellis", ONLY_CHILD_EMAIL),
        ],
        payments=[
            payment("pi_household", SIBLING_EMAIL, {}),
            payment("pi_only_child", ONLY_CHILD_EMAIL, {}),
        ],
    )


def test_p3_refuses_a_household_with_more_than_one_child() -> None:
    """An un-named payment into a two-child household is C2, never a guess (R9).

    Delete `P3`'s `len(members) == 1` and this payment is attributed to whichever
    sibling sorts first -- a real child charged for their sibling's tuition, from
    an address the two legitimately share.
    """
    resolved = resolve(_sibling_snapshot())
    ref = payment_ref("pi_household")

    assert ref not in resolved.payment_method, (
        "an un-named payment into a household with two children was attributed by "
        f"{resolved.payment_method.get(ref)!r}. P3 attributes only when the household "
        "has exactly one child; siblings share a guardian email by construction and "
        "guessing between them is the collision R9 forbids"
    )
    assert attributed_student(resolved, "pi_household") is None
    assert ref in resolved.unattributed_payment_refs, (
        "an unattributable payment is its own entity (SS5.2) and is reported as "
        "unattributed, not silently dropped"
    )


def test_p3_still_attributes_a_single_child_household() -> None:
    """The control: with exactly one child, `P3` is the rule that fires."""
    resolved = resolve(_sibling_snapshot())
    assert resolved.payment_method.get(payment_ref("pi_only_child")) == "P3"
    assert attributed_student(resolved, "pi_only_child") == student_ref("stu-only")


def test_siblings_stay_two_people_and_neither_owns_the_payment() -> None:
    """R9 stated end to end: two children, two persons, no merged household entity."""
    resolved = resolve(_sibling_snapshot())
    keys = {
        ref: resolved.person_by_ref[ref]
        for ref in (student_ref("stu-sib-a"), student_ref("stu-sib-b"))
    }
    assert len(set(keys.values())) == 2, f"the siblings collapsed into one person: {keys}"
    for person in resolved.persons:
        students = [ref for ref in person.refs if ref.startswith("appdb:student:")]
        assert len(students) <= 1, f"person {person.person_key} holds two children: {students}"


# ======================================================================================
# P2 -- "exactly one name match"
# ======================================================================================

SAME_NAME_EMAIL = "same-name@example.test"
DISTINCT_NAME_EMAIL = "distinct-names@example.test"


def _name_match_snapshot() -> Snapshot:
    """A household whose two children normalize to the **same** name.

    Not a contrivance about twins: `norm_name` folds case and spacing, so
    `"CARA"/"byron"` and `"Cara"/"Byron"` are one name key, and any household
    where two records carry the same child's name in different shapes lands here.
    The metadata pair matches both, which is the case `len(matches) == 1` exists
    for -- there is no evidence which record the money belongs to.
    """
    return Snapshot(
        generation=GEN,
        students=[
            student("stu-same-a", "Cara", "Byron", SAME_NAME_EMAIL),
            student("stu-same-b", "CARA", "byron", SAME_NAME_EMAIL),
            student("stu-dist-a", "Enid", "Fry", DISTINCT_NAME_EMAIL),
            student("stu-dist-b", "Gus", "Fry", DISTINCT_NAME_EMAIL),
        ],
        payments=[
            payment(
                "pi_ambiguous",
                SAME_NAME_EMAIL,
                {"student_first_name": "Cara", "student_last_name": "Byron"},
            ),
            payment(
                "pi_named",
                DISTINCT_NAME_EMAIL,
                {"student_first_name": "Gus", "student_last_name": "Fry"},
            ),
        ],
    )


def test_p2_refuses_when_the_name_matches_more_than_one_member() -> None:
    """Two members match the metadata name: the payment is C2, not a coin toss.

    Relax `len(matches) == 1` to a truthy test and the payment silently lands on
    `matches[0]`. `P3` cannot rescue it either -- the household has two children --
    so the guard is the only thing standing between the two records.
    """
    resolved = resolve(_name_match_snapshot())
    ref = payment_ref("pi_ambiguous")

    assert ref not in resolved.payment_method, (
        "a payment whose metadata name matches TWO household members was attributed "
        f"by {resolved.payment_method.get(ref)!r} to "
        f"{attributed_student(resolved, 'pi_ambiguous')!r}. P2 requires exactly one "
        "name match; two matches is missing evidence, not a tie to break"
    )
    assert attributed_student(resolved, "pi_ambiguous") is None


def test_p2_still_attributes_a_single_name_match() -> None:
    """The control: one matching name in a multi-child household is exactly P2's case."""
    resolved = resolve(_name_match_snapshot())
    assert resolved.payment_method.get(payment_ref("pi_named")) == "P2"
    assert attributed_student(resolved, "pi_named") == student_ref("stu-dist-b")


# ======================================================================================
# L2 -- a shared guardian email is not, on its own, the same person
# ======================================================================================

GUARDIAN_EMAIL = "guardian@example.test"


def _l2_snapshot() -> Snapshot:
    """One student and two contacts on the guardian's address.

    One contact is the child (same name). The other is somebody else on the same
    address -- a parent, a second child's emergency contact, a co-signer. Their dob
    matches no student, so `L3` cannot pick them up and the only rule in play is
    `L2`.
    """
    return Snapshot(
        generation=GEN,
        students=[student("stu-l2", "Fay", "Gale", GUARDIAN_EMAIL, dob="2014-03-05")],
        contacts=[
            contact("CRM-l2-same-name", "Fay", "Gale", GUARDIAN_EMAIL),
            contact("CRM-l2-other-person", "Zed", "Quill", GUARDIAN_EMAIL, dob="1979-01-01"),
        ],
    )


def test_l2_does_not_link_a_different_person_on_the_same_address() -> None:
    """Email equality alone is not identity (SS4.2), and R9 is the reason.

    Drop the name from `L2`'s key -- match on `email` alone -- and this contact is
    linked to a child they merely share an address with, which puts a stranger's
    lifecycle stage and email into that child's canonical view.
    """
    resolved = resolve(_l2_snapshot())
    ref = contact_ref("CRM-l2-other-person")

    assert ref not in resolved.contact_student, (
        "a contact sharing only the guardian email was linked to the child by "
        f"{resolved.contact_method.get(ref)!r}. L2 matches on (email, first, last); "
        "the address is shared by construction and cannot decide identity by itself"
    )


def test_l2_still_links_the_matching_name_on_that_address() -> None:
    """The control: same address **and** same name is exactly what L2 is for."""
    resolved = resolve(_l2_snapshot())
    ref = contact_ref("CRM-l2-same-name")
    assert resolved.contact_method.get(ref) == "L2"
    assert resolved.contact_student.get(ref) == student_ref("stu-l2")


# ======================================================================================
# L3 -- name + dob, the rule that exists for records that share no address
# ======================================================================================


def _l3_snapshot() -> Snapshot:
    """A contact reachable only by name+dob, and a near-miss that must not link.

    The linkable contact has no `external_id` (so no `L1`) and an address the
    student does not carry (so no `L2`). The near-miss agrees on the name and
    disagrees on the dob, which is the whole content of the rule.
    """
    return Snapshot(
        generation=GEN,
        students=[student("stu-l3", "Hal", "Ives", "l3-guardian@example.test", dob="2013-07-09")],
        contacts=[
            contact("CRM-l3", "Hal", "Ives", "hal.personal@example.test", dob="2013-07-09"),
            contact("CRM-l3-other-dob", "Hal", "Ives", "other.hal@example.test", dob="2001-02-03"),
        ],
    )


def test_l3_links_on_name_and_dob() -> None:
    """Delete `L3` and this person stops resolving at all.

    That is not a smaller graph, it is a wrong one: the contact becomes a lead
    with no student, and the student loses the CRM half of every field the
    canonical view survives.
    """
    resolved = resolve(_l3_snapshot())
    ref = contact_ref("CRM-l3")

    assert resolved.contact_method.get(ref) == "L3", (
        "a contact matching a student on name+dob, with no hard key and no shared "
        f"address, resolved as {resolved.contact_method.get(ref)!r}. L3 is the rule "
        "that links it (SS4.2), and without it the person is split in two"
    )
    assert resolved.contact_student.get(ref) == student_ref("stu-l3")


def test_l3_does_not_link_on_the_name_alone() -> None:
    """The control: same name, different dob, no link -- L3 is not a name matcher."""
    resolved = resolve(_l3_snapshot())
    assert contact_ref("CRM-l3-other-dob") not in resolved.contact_student


# ======================================================================================
# E2 -- the single-enrollment fallback
# ======================================================================================


def _e2_snapshot() -> Snapshot:
    """Two attributed payments: one whose metadata names a program, one that does not.

    Both payers are attributed by `P1` (a hard `external_ref`), so payment->person
    is settled and the only question left is payment->enrollment.
    """
    return Snapshot(
        generation=GEN,
        students=[
            student("stu-e1", "Ivy", "Jones", "e1@example.test"),
            student("stu-e2", "Kit", "Lowe", "e2@example.test"),
        ],
        enrollments=[
            enrollment("enr-e1", "stu-e1", program="Middle School"),
            enrollment("enr-e2", "stu-e2", program="Middle School"),
        ],
        payments=[
            payment(
                "pi_e1", "e1@example.test", {"program": "Middle School"}, external_ref="stu-e1"
            ),
            payment("pi_e2", "e2@example.test", {}, external_ref="stu-e2"),
        ],
    )


def test_e2_attaches_a_program_less_payment_to_the_only_enrollment() -> None:
    """Delete `E2` and a paid enrolment silently loses its payment.

    A payment with no `program` in its metadata is ordinary -- most of them have
    none -- and the person has exactly one enrollment, so there is nothing to be
    ambiguous about. Without the rule the payment attaches to no enrollment and
    every "paid?" answer built on that join goes quiet.
    """
    resolved = resolve(_e2_snapshot())
    ref = payment_ref("pi_e2")

    assert resolved.payment_enrollment_method.get(ref) == "E2", (
        "a payment attributed to a person with exactly one enrollment resolved to "
        f"{resolved.payment_enrollment_method.get(ref)!r}; E2 is the fallback that "
        "attaches it (SS4.4)"
    )
    assert resolved.payment_enrollment.get(ref) == make_ref("appdb", "enrollment", "enr-e2")


def test_e1_still_wins_when_the_program_is_named() -> None:
    """The control: named program takes `E1`, so `E2` has not swallowed the first rule."""
    resolved = resolve(_e2_snapshot())
    assert resolved.payment_enrollment_method.get(payment_ref("pi_e1")) == "E1"


# ======================================================================================
# the sweep's own control
# ======================================================================================


@pytest.mark.parametrize(
    ("builder", "expected"),
    [
        (_sibling_snapshot, {"pi_only_child": "P3"}),
        (_name_match_snapshot, {"pi_named": "P2"}),
        (_e2_snapshot, {"pi_e1": "P1", "pi_e2": "P1"}),
    ],
)
def test_the_control_every_snapshot_resolves_at_all(builder: Any, expected: dict[str, str]) -> None:
    """Nothing mutated: each snapshot still runs the cascade end to end.

    The green no-op control for the mutation sweep. A snapshot that stopped
    resolving for an unrelated reason would make every negative assertion above
    pass vacuously -- "not attributed" is also what a crash-free no-op looks like.
    """
    resolved = resolve(builder())
    assert resolved.persons, "the cascade produced no persons at all"
    for pid, method in expected.items():
        assert resolved.payment_method.get(payment_ref(pid)) == method
