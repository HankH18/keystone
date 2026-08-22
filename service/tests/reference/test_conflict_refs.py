"""`conflict_refs` -- the single `entity_refs` builder (contract SS5.4, SS5.5).

The generator never authors an `entity_refs` list; pass 2 derives every golden
entry's refs from this helper applied to the detector's own ER output (`G31`). The
harness matches on `(type, tuple(sorted(entity_refs)))`, so a per-type shape drift
between the two sides is simultaneously a false negative and a false positive.
"""

from __future__ import annotations

import pytest

from recon.reference import CONFLICT_TYPES, REF_SPECS, conflict_refs, sources_involved

STUDENT = "appdb:student:s1"
CONTACT = "crm:contact:CRM-0000001"
CONTACT_B = "crm:contact:CRM-0000002"
STUDENT_B = "appdb:student:s2"
ENROLLMENT = "appdb:enrollment:e1"
PAYMENT = "payments:payment:pi_0000001"
PAYMENT_B = "payments:payment:pi_0000002"


@pytest.mark.parametrize(
    ("conflict_type", "kwargs", "expected"),
    [
        # identity refs only
        ("C1", {"identity_refs": [CONTACT, STUDENT]}, (STUDENT, CONTACT)),
        ("C4", {"identity_refs": [CONTACT, STUDENT]}, (STUDENT, CONTACT)),
        ("C5", {"identity_refs": [STUDENT]}, (STUDENT,)),
        ("C6", {"identity_refs": [CONTACT, STUDENT]}, (STUDENT, CONTACT)),
        ("C8", {"identity_refs": [STUDENT]}, (STUDENT,)),
        ("C14", {"identity_refs": [CONTACT, STUDENT]}, (STUDENT, CONTACT)),
        # the payment's own ref
        ("C2", {"payment_refs": [PAYMENT]}, (PAYMENT,)),
        # the two contact refs, sorted
        ("C3", {"contact_refs": [CONTACT_B, CONTACT]}, (CONTACT, CONTACT_B)),
        # the two payment refs, sorted
        ("C11", {"payment_refs": [PAYMENT_B, PAYMENT]}, (PAYMENT, PAYMENT_B)),
        # identity refs + enrollment
        (
            "C7",
            {"identity_refs": [STUDENT], "enrollment_refs": [ENROLLMENT]},
            (ENROLLMENT, STUDENT),
        ),
        (
            "C9",
            {"identity_refs": [STUDENT], "enrollment_refs": [ENROLLMENT]},
            (ENROLLMENT, STUDENT),
        ),
        # exactly three refs, no transitive expansion
        (
            "C10",
            {"contact_refs": [CONTACT], "student_refs": [STUDENT_B, STUDENT]},
            (STUDENT, STUDENT_B, CONTACT),
        ),
        # identity refs + payment
        ("C12", {"identity_refs": [STUDENT], "payment_refs": [PAYMENT]}, (STUDENT, PAYMENT)),
        # identity refs + payment + enrollment
        (
            "C13",
            {
                "identity_refs": [STUDENT],
                "payment_refs": [PAYMENT],
                "enrollment_refs": [ENROLLMENT],
            },
            (ENROLLMENT, STUDENT, PAYMENT),
        ),
    ],
)
def test_per_type_ref_shape(conflict_type: str, kwargs: dict, expected: tuple[str, ...]) -> None:
    assert conflict_refs(conflict_type, **kwargs) == expected


def test_output_is_always_the_sorted_set_and_input_order_cannot_change_it() -> None:
    forward = conflict_refs(
        "C13",
        identity_refs=[STUDENT, CONTACT],
        payment_refs=[PAYMENT],
        enrollment_refs=[ENROLLMENT],
    )
    backward = conflict_refs(
        "C13",
        identity_refs=[CONTACT, STUDENT],
        payment_refs=[PAYMENT],
        enrollment_refs=[ENROLLMENT],
    )
    assert forward == backward == tuple(sorted(forward))
    assert len(set(forward)) == len(forward)


def test_duplicate_identity_refs_collapse() -> None:
    assert conflict_refs("C1", identity_refs=[STUDENT, STUDENT, CONTACT]) == (STUDENT, CONTACT)


def test_c10_names_exactly_three_refs_and_two_distinct_students() -> None:
    with pytest.raises(ValueError, match="exactly 2 distinct student"):
        conflict_refs("C10", contact_refs=[CONTACT], student_refs=[STUDENT, STUDENT])
    with pytest.raises(ValueError, match="exactly 1 distinct contact"):
        conflict_refs("C10", contact_refs=[CONTACT, CONTACT_B], student_refs=[STUDENT, STUDENT_B])


def test_c3_and_c11_are_pairs() -> None:
    with pytest.raises(ValueError, match="exactly 2 distinct contact"):
        conflict_refs("C3", contact_refs=[CONTACT])
    with pytest.raises(ValueError, match="exactly 2 distinct payment"):
        conflict_refs("C11", payment_refs=[PAYMENT, PAYMENT_B, "payments:payment:pi_3"])


def test_a_component_a_type_does_not_take_is_refused() -> None:
    """The mispointed deal appears in `observed_values`, never in `entity_refs` (C9)."""
    with pytest.raises(ValueError, match="takes no payment refs"):
        conflict_refs(
            "C9", identity_refs=[STUDENT], enrollment_refs=[ENROLLMENT], payment_refs=[PAYMENT]
        )
    with pytest.raises(ValueError, match="takes no enrollment refs"):
        conflict_refs("C1", identity_refs=[STUDENT], enrollment_refs=[ENROLLMENT])


def test_a_missing_required_component_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one identity ref"):
        conflict_refs("C6")
    with pytest.raises(ValueError, match="exactly 1 distinct enrollment"):
        conflict_refs("C7", identity_refs=[STUDENT])


def test_refs_must_be_of_the_right_class() -> None:
    with pytest.raises(ValueError, match="not a valid identity ref"):
        conflict_refs("C1", identity_refs=["crm:deal:DEAL-1"])
    with pytest.raises(ValueError, match="not a valid enrollment ref"):
        conflict_refs("C7", identity_refs=[STUDENT], enrollment_refs=[STUDENT])
    with pytest.raises(ValueError, match="not a valid payment ref"):
        conflict_refs("C2", payment_refs=[STUDENT])


def test_unknown_conflict_type_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown conflict type"):
        conflict_refs("C99", identity_refs=[STUDENT])


def test_every_conflict_type_has_a_ref_spec() -> None:
    assert set(REF_SPECS) == set(CONFLICT_TYPES)


def test_sources_involved_derives_from_the_built_refs() -> None:
    refs = conflict_refs(
        "C13",
        identity_refs=[STUDENT, CONTACT],
        payment_refs=[PAYMENT],
        enrollment_refs=[ENROLLMENT],
    )
    assert sources_involved(refs) == ("appdb", "crm", "payments")
    assert sources_involved(conflict_refs("C2", payment_refs=[PAYMENT])) == ("payments",)
