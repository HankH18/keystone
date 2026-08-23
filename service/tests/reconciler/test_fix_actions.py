"""Contract SS6's fix templates, resolved to concrete ``{"set": {...}}`` actions.

SS6 pins the target **path** per conflict type. It does not pin the **value**, so
the value derivation is a committed decision of the reconciler, and these tests
are where each one is written down and held:

* an evidence-only type produces ``{"set": {}}`` and *says* it is evidence-only,
  rather than being skipped -- R13 wants one proposal per conflict;
* C6/C14 take the value from the **app-DB endpoint of the same comparison row**,
  because SS4.6 makes the app DB authoritative for identity fields and SS6 ruling
  8 makes the CRM path the target. The proposal therefore always corrects the
  less-authoritative copy towards the more-authoritative one;
* C9 clears the stale pointer;
* C2 and an ambiguous C4 admit that no value is derivable and carry an empty set.

The action shape itself is not optional: migration 0007's
``ck_proposals_action_vocabulary`` admits exactly ``{"set": {<path>: <value>}}``
and nothing else, so an action of any other shape is a rejected INSERT rather
than a bad proposal.
"""

from __future__ import annotations

import pytest

from recon.reconciler import fix_action
from recon.reference import CONFLICT_TYPES, fix_target

EVIDENCE_ONLY = ("C1", "C3", "C5", "C7", "C8", "C10", "C11", "C12", "C13")


@pytest.mark.parametrize("conflict_type", CONFLICT_TYPES)
def test_every_action_has_the_only_shape_the_database_admits(conflict_type: str) -> None:
    action = fix_action(conflict_type).action
    assert set(action) == {"set"}
    assert isinstance(action["set"], dict)
    assert all(isinstance(key, str) for key in action["set"])


@pytest.mark.parametrize("conflict_type", EVIDENCE_ONLY)
def test_evidence_only_types_carry_an_empty_set_and_say_why(conflict_type: str) -> None:
    """SS6: "no field write -- evidence-only proposal"."""
    resolved = fix_action(conflict_type)
    assert resolved.action == {"set": {}}
    assert resolved.target_path is None
    assert resolved.derivable is False
    assert "no field write" in resolved.derivation


def test_c6_grade_only_writes_the_authoritative_app_db_value_onto_the_crm_path() -> None:
    resolved = fix_action(
        "C6",
        disagreeing_fields=("crm.contact.grade", "appdb.student.grade"),
        observed_values={"crm.contact.grade": "7", "appdb.student.grade": "8"},
    )
    assert resolved.target_path == "crm.contact.grade"
    assert resolved.value == "8"
    assert resolved.action == {"set": {"crm.contact.grade": "8"}}
    assert resolved.derivable is True
    assert "SS4.6" in resolved.derivation


def test_c6_lifecycle_only_writes_the_crm_side_from_the_app_db_status() -> None:
    resolved = fix_action(
        "C6",
        disagreeing_fields=("crm.contact.lifecycle_stage", "appdb.student.status"),
        observed_values={
            "crm.contact.lifecycle_stage": "applied",
            "appdb.student.status": "enrolled",
        },
    )
    assert resolved.action == {"set": {"crm.contact.lifecycle_stage": "enrolled"}}


def test_c14_writes_the_disagreeing_sensitive_path_on_the_crm_side() -> None:
    """SS6 ruling 8: byte order would have picked `appdb.*`; the convention is CRM."""
    resolved = fix_action(
        "C14",
        disagreeing_fields=("crm.contact.first_name", "appdb.student.first_name"),
        observed_values={
            "crm.contact.first_name": "jon",
            "appdb.student.first_name": "john",
        },
    )
    assert resolved.target_path == "crm.contact.first_name"
    assert resolved.action == {"set": {"crm.contact.first_name": "john"}}


def test_a_mixed_c6_writes_the_sensitive_row_not_the_grade_row() -> None:
    resolved = fix_action(
        "C6",
        disagreeing_fields=(
            "crm.contact.grade",
            "appdb.student.grade",
            "crm.contact.dob",
            "appdb.student.dob",
        ),
        observed_values={
            "crm.contact.grade": "7",
            "appdb.student.grade": "8",
            "crm.contact.dob": "2012-01-01",
            "appdb.student.dob": "2012-02-02",
        },
    )
    assert resolved.target_path == "crm.contact.dob"
    assert resolved.action == {"set": {"crm.contact.dob": "2012-02-02"}}


def test_a_c6_whose_authoritative_endpoint_was_not_observed_derives_no_value() -> None:
    """No value is better than a guessed one; the proposal still lands."""
    resolved = fix_action(
        "C6",
        disagreeing_fields=("crm.contact.grade", "appdb.student.grade"),
        observed_values={"crm.contact.grade": "7"},
    )
    assert resolved.target_path == "crm.contact.grade"
    assert resolved.derivable is False
    assert resolved.action == {"set": {}}
    assert "absent from observed_values" in resolved.derivation


def test_a_c6_whose_authoritative_endpoint_is_null_derives_no_value() -> None:
    resolved = fix_action(
        "C6",
        disagreeing_fields=("crm.contact.grade", "appdb.student.grade"),
        observed_values={"crm.contact.grade": "7", "appdb.student.grade": None},
    )
    assert resolved.derivable is False
    assert resolved.action == {"set": {}}


def test_c9_clears_the_stale_pointer() -> None:
    """SS5.5 makes a null `crm_deal_id` a clean state, so clearing is conservative."""
    resolved = fix_action(
        "C9",
        observed_values={
            "enrollment.crm_deal_id": "DEAL-9",
            "deal_present_gen3": False,
            "deal_person_refs": [],
        },
    )
    assert resolved.target_path == "appdb.enrollment.crm_deal_id"
    assert resolved.value is None
    assert resolved.action == {"set": {"appdb.enrollment.crm_deal_id": None}}
    assert resolved.derivable is True


def test_c2_names_its_target_but_derives_no_value() -> None:
    """The target is what makes SS6's classification decidable; the value does not exist.

    SS5.6 makes the C2 population the only payments omitting both `external_ref`
    and the metadata name pair, with a `payer_email` used by no student and no
    contact -- so there is no candidate person anywhere in the dataset to point
    the linkage at. Inventing one would be the single worst thing an unattended
    fixer could do to a payments record.
    """
    resolved = fix_action(
        "C2",
        observed_values={
            "payer_email_norm": "nobody@example.test",
            "external_ref": None,
            "metadata_name_pair_present": False,
        },
    )
    assert resolved.target_path == "payments.payment.external_ref"
    assert resolved.derivable is False
    assert resolved.action == {"set": {}}
    assert "no candidate person" in resolved.derivation


def test_c4_derives_the_single_guardian_address() -> None:
    resolved = fix_action(
        "C4",
        observed_values={
            "contact_email_norm": "variant@example.test",
            "student_guardian_email_norms": ["guardian@example.test"],
            "link_method": "L3",
        },
    )
    assert resolved.target_path == "crm.contact.email"
    assert resolved.action == {"set": {"crm.contact.email": "guardian@example.test"}}


def test_c4_with_two_guardian_addresses_refuses_to_choose() -> None:
    resolved = fix_action(
        "C4",
        observed_values={
            "contact_email_norm": "variant@example.test",
            "student_guardian_email_norms": ["a@example.test", "b@example.test"],
            "link_method": "L3",
        },
    )
    assert resolved.derivable is False
    assert resolved.action == {"set": {}}
    assert "human decision" in resolved.derivation


def test_a_derived_action_never_writes_a_path_the_template_did_not_name() -> None:
    """The action's keys are a subset of {target_path}, always.

    This is the property the classifier depends on: R15 rules on ONE path, so an
    action touching a second one would be a write nobody classified.
    """
    shapes = [
        ((), {}),
        (
            ("crm.contact.grade", "appdb.student.grade"),
            {"crm.contact.grade": "7", "appdb.student.grade": "8"},
        ),
        (
            ("crm.contact.dob", "appdb.student.dob"),
            {"crm.contact.dob": "a", "appdb.student.dob": "b"},
        ),
    ]
    for conflict_type in CONFLICT_TYPES:
        for paths, observed in shapes:
            resolved = fix_action(conflict_type, disagreeing_fields=paths, observed_values=observed)
            target = fix_target(conflict_type, paths).field_path
            assert set(resolved.action["set"]) <= ({target} if target else set()), (
                conflict_type,
                paths,
            )
            assert resolved.target_path == target
