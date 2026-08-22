"""`match_keys` -- ordered, deterministic blocking keys (contract SS2.1, SS4.7).

Candidates only. The sibling case is the negative that matters: siblings share the
guardian email by construction (`G1`), so an email key alone must never be taken
for identity -- which is exactly why `L2` also requires the name to match (SS4.2).
"""

from __future__ import annotations

import pytest

from recon.normalize import KEY_CLASSES, MatchKey, match_keys

CONTACT = {
    "crm_id": "CRM-0000001",
    "external_id": "student-1",
    "email": " `Parent.Guardian+school@Gmail.com` ",
    "first_name": "  José ",
    "last_name": "García",
    "dob": "2010-04-05",
}

STUDENT = {
    "id": "student-1",
    "first_name": "Jose",
    "last_name": "Garcia",
    "dob": "2010-04-05",
    "guardian_email": "parentguardian@gmail.com",
    "guardian2_email": "other.parent@corp.com",
    "student_number": "S-000123",
}

PAYMENT = {
    "payment_id": "pi_0000001",
    "external_ref": "student-1",
    "payer_email": "ParentGuardian@gmail.com",
    "metadata": {"student_first_name": "Jose", "student_last_name": "Garcia", "program": None},
}


def test_contact_key_order_is_pinned() -> None:
    assert match_keys(CONTACT) == (
        MatchKey("ext", "student-1"),
        MatchKey("email", "parentguardian@gmail.com"),
        MatchKey("namedob", ("jose", "garcia", "2010-04-05")),
    )


def test_student_key_order_is_pinned_and_primary_email_comes_first() -> None:
    """`L2` matches a contact against either guardian address (SS4.2)."""
    assert match_keys(STUDENT) == (
        MatchKey("ext", "student-1"),
        MatchKey("email", "parentguardian@gmail.com"),
        MatchKey("email", "other.parent@corp.com"),
        MatchKey("namedob", ("jose", "garcia", "2010-04-05")),
    )


def test_payment_keys() -> None:
    """A payment has no DOB, so it emits no `namedob` key (`P2` is a cascade rule)."""
    assert match_keys(PAYMENT) == (
        MatchKey("ext", "student-1"),
        MatchKey("email", "parentguardian@gmail.com"),
    )


def test_contact_and_student_of_one_person_agree_on_every_key_class() -> None:
    contact_keys = dict(match_keys(CONTACT))
    student_keys = match_keys(STUDENT)
    assert contact_keys["ext"] == student_keys[0].value
    assert contact_keys["email"] == student_keys[1].value
    assert contact_keys["namedob"] == student_keys[3].value


def test_siblings_sharing_a_guardian_email_do_not_produce_equal_match_keys() -> None:
    """NEGATIVE case: shared guardian email + different names => different key sets."""
    older = {
        "id": "student-1",
        "first_name": "Ana",
        "last_name": "Garcia",
        "dob": "2010-04-05",
        "guardian_email": "P.Guardian@gmail.com",
        "guardian2_email": None,
    }
    younger = {
        "id": "student-2",
        "first_name": "Beto",
        "last_name": "Garcia",
        "dob": "2013-09-01",
        "guardian_email": "pguardian@gmail.com",
        "guardian2_email": None,
    }

    older_keys, younger_keys = match_keys(older), match_keys(younger)

    # The household email normalizes equal -- that is `G1` working as designed...
    assert dict(older_keys)["email"] == dict(younger_keys)["email"]
    # ...but the key SETS differ, so no rule can mistake the pair for one person.
    assert set(older_keys) != set(younger_keys)
    assert dict(older_keys)["namedob"] != dict(younger_keys)["namedob"]
    assert dict(older_keys)["ext"] != dict(younger_keys)["ext"]


def test_twins_sharing_email_and_dob_still_differ_on_the_namedob_key() -> None:
    """`G5`(a): within a shared-email group, `(first_norm, last_norm)` is unique."""
    twin_a = {
        "id": "student-1",
        "first_name": "Ana",
        "last_name": "Garcia",
        "dob": "2010-04-05",
        "guardian_email": "pg@corp.com",
    }
    twin_b = dict(twin_a, id="student-2", first_name="Bea")
    assert dict(match_keys(twin_a))["namedob"] != dict(match_keys(twin_b))["namedob"]


@pytest.mark.parametrize(
    "entity",
    [
        {"crm_id": "CRM-1", "external_id": None, "email": None, "first_name": None},
        {"crm_id": "CRM-1", "external_id": "", "email": "  ", "first_name": "A", "last_name": "B"},
    ],
)
def test_absent_inputs_emit_no_key(entity: dict) -> None:
    assert match_keys(entity) == ()


def test_namedob_key_requires_all_three_components() -> None:
    """`L3` requires both DOBs non-null; a partial key could only manufacture
    candidate pairs no cascade rule can accept."""
    without_dob = {"crm_id": "CRM-1", "first_name": "Ana", "last_name": "Garcia", "dob": None}
    assert [key.key_class for key in match_keys(without_dob)] == []

    with_dob = dict(without_dob, dob="2010-04-05")
    assert [key.key_class for key in match_keys(with_dob)] == ["namedob"]


def test_duplicate_keys_are_dropped_preserving_first_seen_order() -> None:
    student = {
        "id": "student-1",
        "first_name": "Ana",
        "last_name": "Garcia",
        "dob": "2010-04-05",
        "guardian_email": "pg@corp.com",
        "guardian2_email": " PG@Corp.com ",
    }
    emails = [key for key in match_keys(student) if key.key_class == "email"]
    assert emails == [MatchKey("email", "pg@corp.com")]


def test_ordering_is_stable_across_repeated_calls_and_dict_orderings() -> None:
    reordered = dict(reversed(list(CONTACT.items())))
    assert match_keys(CONTACT) == match_keys(reordered)
    assert [match_keys(CONTACT) for _ in range(5)].count(match_keys(CONTACT)) == 5


def test_entity_type_can_be_given_explicitly_and_is_validated() -> None:
    assert match_keys(CONTACT, "crm.contact") == match_keys(CONTACT)
    with pytest.raises(ValueError, match="unknown entity_type"):
        match_keys(CONTACT, "crm.deal")


def test_uninferable_entity_raises() -> None:
    with pytest.raises(ValueError, match="cannot infer entity type"):
        match_keys({"first_name": "Ana"})


def test_records_may_be_objects_as_well_as_dicts() -> None:
    class Contact:
        crm_id = "CRM-0000001"
        external_id = "student-1"
        email = "parent@corp.com"
        first_name = "Ana"
        last_name = "Garcia"
        dob = "2010-04-05"

    assert match_keys(Contact()) == (
        MatchKey("ext", "student-1"),
        MatchKey("email", "parent@corp.com"),
        MatchKey("namedob", ("ana", "garcia", "2010-04-05")),
    )


# =====================================================================================
# SS2.1 ruling 15 -- KEY_CLASSES is exported;  ruling 10 -- the null-DOB consequence
# =====================================================================================


def test_key_classes_is_exported_and_is_the_order_match_keys_emits() -> None:
    """SS4.7's `entity_link_candidates.key_class` vocabulary. Exported (ruling 15)
    because SS0 forbids either side re-implementing a shared symbol, and a symbol the
    contract names but the module hides is the one a consumer retypes."""
    import recon.normalize as normalize

    assert KEY_CLASSES == ("ext", "email", "namedob")
    assert "KEY_CLASSES" in normalize.__all__
    assert [key.key_class for key in match_keys(CONTACT)] == list(KEY_CLASSES)


@pytest.mark.parametrize("dob", [None, "", "   ", "not-a-date", "2010-13-45", "04/05/2010", "2010"])
def test_no_namedob_key_is_emitted_for_a_missing_or_unparseable_dob(dob: object) -> None:
    """SS2.1 ruling 10: no `namedob` key unless first, last AND dob are all present.

    Consequence for SS4.7, which is the point: `entity_link_candidates` carries NO
    `key_class='namedob'` row for such a record, so it is reachable only by `ext` or
    `email`. `L3` requires both DOBs non-null, so a partial key could only manufacture
    candidate pairs no cascade rule may accept -- and `R-010` (C10) is evaluated over
    `entity_link_candidates`, so it would see a `namedob` resolution that no link rule
    could ever have made, i.e. a fabricated merge-collapse.
    """
    record = {"crm_id": "CRM-1", "first_name": "Ana", "last_name": "Garcia", "dob": dob}
    keys = match_keys(record)
    assert all(key.key_class != "namedob" for key in keys)
    assert not any(key.value == ("ana", "garcia", None) for key in keys)


@pytest.mark.parametrize(
    ("first", "last"), [(None, "Garcia"), ("Ana", None), (None, None), ("  ", "Garcia")]
)
def test_no_namedob_key_is_emitted_for_a_missing_name_half_either(
    first: object, last: object
) -> None:
    record = {"crm_id": "CRM-1", "first_name": first, "last_name": last, "dob": "2010-04-05"}
    assert all(key.key_class != "namedob" for key in match_keys(record))


def test_all_three_present_does_emit_the_key() -> None:
    record = {"crm_id": "CRM-1", "first_name": "Ana", "last_name": "Garcia", "dob": "2010-04-05"}
    assert match_keys(record) == (MatchKey("namedob", ("ana", "garcia", "2010-04-05")),)
