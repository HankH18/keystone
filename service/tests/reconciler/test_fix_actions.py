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

from recon.apply import effective_write_paths
from recon.normalize import norm_enum
from recon.reconciler import _FIELD_VOCABULARY, NESTED_FIX_TARGETS, _preimages, fix_action
from recon.reference import (
    CONFLICT_TYPES,
    DEAL_STAGE_TO_FUNNEL,
    LIFECYCLE_TO_FUNNEL,
    SENSITIVE_FIELDS,
    fix_target,
)
from recon.resolve import SURVIVED_PATHS

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


def test_c6_lifecycle_only_writes_the_crm_side_in_the_CRM_S_OWN_VOCABULARY() -> None:
    """The value is the CRM lifecycle token, not the funnel token it compares as.

    **This assertion used to read `{"crm.contact.lifecycle_stage": "enrolled"}`
    and it was wrong.** Contract SS2.4's `lifecycle` row compares
    `LIFECYCLE_TO_FUNNEL(crm.contact.lifecycle_stage)` against
    `STATUS_TO_FUNNEL(appdb.student.status)`, and SS5.4 records the COMPARISON's
    values in `observed_values` -- so the authoritative endpoint there is a
    **funnel** token (`enrolled`) while the field being written holds a CRM one
    (`customer`, `SQL`, `opportunity`, ...). `norm_enum('lifecycle_stage',
    'enrolled')` is `None`: writing the funnel token onto the field makes the
    NEXT comparison of it `unchecked` (SS5.1), so the conflict would disappear by
    becoming unreadable rather than by being fixed.

    It was undetectable for exactly as long as the write was invisible -- the
    top-level form lands beside `recon.resolve.VIEW_FIELDS`, where nothing reads
    it. Written where the canonical layer actually keeps the field (inside
    `survived`, which the entity endpoints project) it is a visible corruption of
    the canonical view. Measured on the graded store: every one of the 120
    lifecycle-only C6s carries `appdb.student.status = 'enrolled'`, whose
    preimage under `LIFECYCLE_TO_FUNNEL` is the singleton `{'customer'}`.
    """
    resolved = fix_action(
        "C6",
        disagreeing_fields=("crm.contact.lifecycle_stage", "appdb.student.status"),
        observed_values={
            "crm.contact.lifecycle_stage": "applied",
            "appdb.student.status": "enrolled",
        },
    )
    assert resolved.action == {"set": {"crm.contact.lifecycle_stage": "customer"}}
    assert resolved.value == "customer"
    assert resolved.derivable is True
    # ...and the value it writes is one the field's own normalizer accepts, which
    # is the property the old assertion violated.
    assert norm_enum("lifecycle_stage", resolved.value) == resolved.value
    assert norm_enum("lifecycle_stage", "enrolled") is None
    # ...and it still MEANS the authoritative funnel value.
    assert LIFECYCLE_TO_FUNNEL[resolved.value] == "enrolled"


@pytest.mark.parametrize("funnel", ["prospect", "applied"])
def test_an_ambiguous_lifecycle_preimage_derives_no_value(funnel: str) -> None:
    """Three CRM tokens map to `prospect` and three to `applied` (SS2.3).

    Which one the contact should carry is not determined by the evidence, so the
    template refuses to choose -- the same shape of refusal an ambiguous C4
    makes. A guessed value is worse than none: it would be applied, and it would
    be wrong in a field a human reads.
    """
    resolved = fix_action(
        "C6",
        disagreeing_fields=("crm.contact.lifecycle_stage", "appdb.student.status"),
        observed_values={
            "crm.contact.lifecycle_stage": "enrolled",
            "appdb.student.status": funnel,
        },
    )
    assert resolved.target_path == "crm.contact.lifecycle_stage"
    assert resolved.action == {"set": {}}
    assert resolved.derivable is False
    assert "preimages" in resolved.derivation
    assert len(_preimages(LIFECYCLE_TO_FUNNEL)[funnel]) == 3


@pytest.mark.parametrize("funnel", ["waitlisted", "deposit_paid", "refunded"])
def test_a_funnel_value_no_lifecycle_token_reaches_derives_no_value(funnel: str) -> None:
    """SS10 G18: no `lifecycle_stage` value maps to these, so there is nothing to write."""
    resolved = fix_action(
        "C6",
        disagreeing_fields=("crm.contact.lifecycle_stage", "appdb.student.status"),
        observed_values={
            "crm.contact.lifecycle_stage": "enrolled",
            "appdb.student.status": funnel,
        },
    )
    assert resolved.action == {"set": {}}
    assert resolved.derivable is False
    assert funnel not in _preimages(LIFECYCLE_TO_FUNNEL)


def test_a_c14_stage_row_writes_the_deal_s_own_vocabulary() -> None:
    """The same mismatch on the other non-identity mapper, and it is total.

    `DEAL_STAGE_TO_FUNNEL` is bijective onto the funnel (SS2.3), so the preimage
    is always a singleton and no C14 stage proposal loses its value. The write is
    held at every confidence either way -- `crm.deal.stage` is in
    `SENSITIVE_FIELDS` -- so this is about the value being RIGHT, not about it
    being applied.
    """
    resolved = fix_action(
        "C14",
        disagreeing_fields=("crm.deal.stage", "appdb.enrollment.stage"),
        observed_values={"crm.deal.stage": "applied", "appdb.enrollment.stage": "enrolled"},
    )
    assert resolved.target_path == "crm.deal.stage"
    assert resolved.action == {"set": {"crm.deal.stage": "Closed Won"}}
    assert norm_enum("deal_stage", resolved.value) == resolved.value
    assert DEAL_STAGE_TO_FUNNEL[resolved.value] == "enrolled"


def test_the_identity_mapped_rows_are_not_translated() -> None:
    """Only the two rows whose mapper changes vocabulary are carried back.

    `name_first`, `name_last`, `dob` and `grade` compare through the same
    normalizer the field itself is stored under, so the observed value IS the
    field's value and a translation table for them would be a fiction.
    """
    assert set(_FIELD_VOCABULARY) == {"crm.contact.lifecycle_stage", "crm.deal.stage"}
    grade = fix_action(
        "C6",
        disagreeing_fields=("crm.contact.grade", "appdb.student.grade"),
        observed_values={"crm.contact.grade": "7", "appdb.student.grade": "8"},
    )
    assert grade.action == {"set": {"crm.contact.grade": "8"}}


# =====================================================================================
# the SHAPE: an eligible target that lives inside `survived` is written THERE
# =====================================================================================


def test_the_lifecycle_target_is_written_into_survived_when_the_map_is_in_hand() -> None:
    """The observable form, emitted by the committed template rather than by a test.

    `crm.contact.lifecycle_stage` is the ONE contract SS6 eligible path that is a
    member of `recon.resolve.SURVIVED_PATHS`, and `survived` is projected by
    `recon.resolve.VIEW_FIELDS`. Written as a top-level key it lands where no
    reader looks; written into `survived` it moves the value the entity endpoints
    and the R10 join check show. Nothing about the target, the allow-list or the
    classification changes -- only the shape.
    """
    survived = {path: f"current-{path}" for path in SURVIVED_PATHS}
    resolved = fix_action(
        "C6",
        disagreeing_fields=("crm.contact.lifecycle_stage", "appdb.student.status"),
        observed_values={
            "crm.contact.lifecycle_stage": "applied",
            "appdb.student.status": "enrolled",
        },
        survived=survived,
    )
    assert resolved.target_path == "crm.contact.lifecycle_stage"
    assert resolved.container == "survived"
    written = resolved.action["set"]
    assert set(written) == {"survived"}
    # the WHOLE map, per contract SS5: `||` replaces a nested object wholesale.
    assert set(written["survived"]) == set(survived)
    assert written["survived"]["crm.contact.lifecycle_stage"] == "customer"
    for path in SURVIVED_PATHS:
        if path != "crm.contact.lifecycle_stage":
            assert written["survived"][path] == survived[path]
    # ...and what it EFFECTIVELY writes is the one eligible leaf and nothing else.
    paths = effective_write_paths(written, {"survived": survived})
    assert [path.display for path in paths] == ["survived->crm.contact.lifecycle_stage"]
    assert [path.leaf for path in paths] == ["crm.contact.lifecycle_stage"]


def test_without_the_map_the_template_falls_back_to_the_top_level_form() -> None:
    """A template that cannot see the map cannot carry it, and must not guess.

    Guessing the other eight members would author an erasure -- `||` replaces the
    object -- which `apply_proposal` refuses as `shallow_merge_would_erase` and
    which the database refuses as `KS013` for the six sensitive members among
    them. The top-level form is inert rather than destructive, so it is the safe
    fallback.
    """
    resolved = fix_action(
        "C6",
        disagreeing_fields=("crm.contact.lifecycle_stage", "appdb.student.status"),
        observed_values={
            "crm.contact.lifecycle_stage": "applied",
            "appdb.student.status": "enrolled",
        },
    )
    assert resolved.container is None
    assert resolved.action == {"set": {"crm.contact.lifecycle_stage": "customer"}}


def test_a_map_that_does_not_carry_the_member_is_not_extended() -> None:
    """Adding a member is the look-alike hole; a template may never author one.

    `survived`'s membership is the closed set `SURVIVED_PATHS`. If the stored map
    somehow lacks the target, the fix falls back to the top-level form rather
    than introducing a member -- which `recon.apply.merge_preview` refuses on
    both apply paths (`nested_member_introduced`).
    """
    partial = {path: "v" for path in SURVIVED_PATHS if path != "crm.contact.lifecycle_stage"}
    resolved = fix_action(
        "C6",
        disagreeing_fields=("crm.contact.lifecycle_stage", "appdb.student.status"),
        observed_values={
            "crm.contact.lifecycle_stage": "applied",
            "appdb.student.status": "enrolled",
        },
        survived=partial,
    )
    assert resolved.container is None
    assert resolved.action == {"set": {"crm.contact.lifecycle_stage": "customer"}}


def test_only_an_eligible_survived_member_is_ever_nested() -> None:
    """A HELD target that also lives in `survived` keeps the top-level form.

    `appdb.enrollment.stage`, `appdb.student.status` and `crm.deal.stage` are
    members of `survived` too -- and all three are in `SENSITIVE_FIELDS`, so no
    proposal targeting them can auto-apply at any confidence. Re-shaping them
    would buy no observability R15 lets the machine use while moving the shape of
    380 committed `sensitive_hold` rows, so `NESTED_FIX_TARGETS` is the
    INTERSECTION of `SURVIVED_PATHS` with the eligible set and nothing else.
    """
    assert frozenset({"crm.contact.lifecycle_stage"}) == NESTED_FIX_TARGETS
    assert set(SURVIVED_PATHS) & set(SENSITIVE_FIELDS) - NESTED_FIX_TARGETS
    survived = {path: f"current-{path}" for path in SURVIVED_PATHS}
    held = fix_action(
        "C14",
        disagreeing_fields=("crm.deal.stage", "appdb.enrollment.stage"),
        observed_values={"crm.deal.stage": "applied", "appdb.enrollment.stage": "enrolled"},
        survived=survived,
    )
    assert held.container is None
    assert set(held.action["set"]) == {"crm.deal.stage"}


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
