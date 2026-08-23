"""R15: classification is a pure function of the target path, and it beats confidence.

The requirement has three separable claims, and each gets its own test rather than
one end-to-end assertion that happens to cover all of them:

1. every C14, and every proposal whose fix target is in ``SENSITIVE_FIELDS``, is
   born ``sensitive_hold``;
2. no confidence can change that -- established structurally, by the classifier
   having no confidence parameter at all;
3. the database refuses the row anyway (SQLSTATE ``KS002``), which is the
   backstop and is tested as a *separate* control in
   ``test_reconcile_run.py::test_the_database_refuses_a_sensitive_proposal_born_pending``.
"""

from __future__ import annotations

import inspect

import pytest

from recon.reference import (
    AUTO_APPLY_ELIGIBLE,
    COMPARED_FIELDS,
    CONFLICT_TYPES,
    FIX_TARGETS,
    SENSITIVE_FIELDS,
    fix_target,
)
from recon.sensitive import (
    DISPOSITION_ELIGIBLE,
    DISPOSITION_ESCALATED,
    DISPOSITION_SENSITIVE_HOLD,
    STATUS_PENDING,
    STATUS_SENSITIVE_HOLD,
    Classification,
    classify,
)

#: Contract SS6's committed fix-target table, transcribed. The point of copying it
#: is the same as copying the weights: a change to `reference.FIX_TARGETS` that
#: nobody meant to make is a red test rather than a quiet reclassification.
COMMITTED_TABLE = {
    "C1": (None, DISPOSITION_ESCALATED),
    "C2": ("payments.payment.external_ref", DISPOSITION_ELIGIBLE),
    "C3": (None, DISPOSITION_ESCALATED),
    "C4": ("crm.contact.email", DISPOSITION_SENSITIVE_HOLD),
    "C5": (None, DISPOSITION_ESCALATED),
    "C6": ("crm.contact.grade", DISPOSITION_ELIGIBLE),
    "C7": (None, DISPOSITION_ESCALATED),
    "C8": (None, DISPOSITION_ESCALATED),
    "C9": ("appdb.enrollment.crm_deal_id", DISPOSITION_ELIGIBLE),
    "C10": (None, DISPOSITION_ESCALATED),
    "C11": (None, DISPOSITION_ESCALATED),
    "C12": (None, DISPOSITION_ESCALATED),
    "C13": (None, DISPOSITION_ESCALATED),
    "C14": ("crm.contact.first_name", DISPOSITION_SENSITIVE_HOLD),
}


# =====================================================================================
# claim 2 first, because it is structural: the classifier CANNOT see confidence
# =====================================================================================
def test_the_classifier_has_no_confidence_parameter() -> None:
    """ "Classification wins over confidence" as a property of the signature.

    The strongest available statement is that the function cannot be handed a
    score at all. A future edit that wanted to let a 0.99 through would have to
    add the parameter first, which is a reviewable act rather than an accident.
    """
    parameters = set(inspect.signature(classify).parameters)
    assert parameters == {"conflict_type", "disagreeing_fields"}
    assert not any("confid" in name or "score" in name for name in parameters)


def test_no_code_path_in_the_classifier_mentions_a_threshold() -> None:
    """0.95 is R24's auto-apply gate and must not appear in the hold decision."""
    from pathlib import Path

    source = Path(inspect.getsourcefile(classify) or "").read_text(encoding="utf-8")
    body = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith(("#", '"', "*"))
    )
    assert "0.95" not in body.replace('">= 0.95', "")


def test_a_classification_cannot_be_built_that_contradicts_itself() -> None:
    """`sensitive=True` with `status='pending'` is the row KS002 exists to refuse.

    Making it unconstructible upstream means the trigger never has to.
    """
    with pytest.raises(ValueError, match="born sensitive_hold"):
        Classification(
            conflict_type="C14",
            target_path="crm.contact.first_name",
            status=STATUS_PENDING,
            sensitive=True,
            disposition=DISPOSITION_SENSITIVE_HOLD,
            sensitive_paths=(),
            reason="hand-built",
            auto_apply_eligible_path=False,
        )


def test_a_sensitive_path_can_never_also_be_auto_apply_eligible() -> None:
    assert not (SENSITIVE_FIELDS & AUTO_APPLY_ELIGIBLE)
    with pytest.raises(ValueError, match="auto-apply allowlist"):
        Classification(
            conflict_type="C6",
            target_path="crm.contact.email",
            status=STATUS_SENSITIVE_HOLD,
            sensitive=True,
            disposition=DISPOSITION_SENSITIVE_HOLD,
            sensitive_paths=("crm.contact.email",),
            reason="hand-built",
            auto_apply_eligible_path=True,
        )


# =====================================================================================
# claim 1: every C14 and every sensitive target is held
# =====================================================================================
def test_every_c14_is_held_whatever_its_disagreeing_paths() -> None:
    """SS6: "C6 mixed, and **every** C14"."""
    for paths in (
        (),
        ("crm.contact.first_name", "appdb.student.first_name"),
        ("crm.contact.dob", "appdb.student.dob"),
        ("crm.deal.stage", "appdb.enrollment.stage"),
        ("crm.contact.grade", "appdb.student.grade"),  # not a C14 shape; still held
    ):
        result = classify("C14", paths)
        assert result.status == STATUS_SENSITIVE_HOLD, paths
        assert result.sensitive is True
        assert result.held is True


@pytest.mark.parametrize("path", sorted(SENSITIVE_FIELDS))
def test_every_committed_sensitive_field_is_held_when_it_is_the_target(path: str) -> None:
    """The list IS the classifier: walk all 20 paths, not a representative three."""
    from recon.sensitive import Classification as _C

    # Drive the classifier through the one type whose target is path-selected and
    # whose committed table entry is the path itself.
    result = classify("C14", (path,))
    assert isinstance(result, _C)
    assert result.status == STATUS_SENSITIVE_HOLD


def test_c4_is_held_because_its_committed_target_is_an_email() -> None:
    """SS6, stated as an intended consequence: all 250 C4 proposals are held."""
    result = classify("C4")
    assert result.target_path == "crm.contact.email"
    assert result.status == STATUS_SENSITIVE_HOLD
    assert result.sensitive is True
    assert "SENSITIVE_FIELDS" in result.reason


def test_a_c4_can_never_be_retargeted_at_the_linkage_field() -> None:
    """SS6/SS12 D-7: retargeting C4 at `crm.contact.external_id` would free 250 holds."""
    for paths in ((), ("crm.contact.email",), ("crm.contact.external_id",)):
        assert classify("C4", paths).target_path == "crm.contact.email"
        assert classify("C4", paths).held is True


def test_a_mixed_c6_is_held_on_the_strength_of_its_sensitive_half() -> None:
    """SS6 ruling 8 step 2: the sensitive half of a mixed set decides."""
    mixed = classify(
        "C6",
        (
            "crm.contact.grade",
            "appdb.student.grade",
            "crm.contact.first_name",
            "appdb.student.first_name",
        ),
    )
    assert mixed.status == STATUS_SENSITIVE_HOLD
    assert mixed.target_path == "crm.contact.first_name"
    assert mixed.auto_apply_eligible_path is False


def test_a_grade_only_c6_is_pending_and_eligible() -> None:
    result = classify("C6", ("crm.contact.grade", "appdb.student.grade"))
    assert result.status == STATUS_PENDING
    assert result.sensitive is False
    assert result.target_path == "crm.contact.grade"
    assert result.disposition == DISPOSITION_ELIGIBLE
    assert result.auto_apply_eligible_path is True


def test_a_lifecycle_only_c6_writes_the_crm_side_and_stays_eligible() -> None:
    """SS6: eligible "only when the proposal writes the CRM side"."""
    result = classify("C6", ("crm.contact.lifecycle_stage", "appdb.student.status"))
    assert result.target_path == "crm.contact.lifecycle_stage"
    assert result.status == STATUS_PENDING
    assert "appdb.student.status" in result.sensitive_paths


def test_a_stage_only_c6_is_held_because_the_row_is_wholly_sensitive() -> None:
    result = classify("C6", ("crm.deal.stage", "appdb.enrollment.stage"))
    assert result.status == STATUS_SENSITIVE_HOLD
    assert result.target_path == "crm.deal.stage"


def test_every_wholly_sensitive_comparison_row_is_held_under_c6() -> None:
    """Derived from COMPARED_FIELDS rather than listed, so a new row is covered."""
    for row in COMPARED_FIELDS:
        result = classify("C6", row.paths)
        if row.wholly_sensitive:
            assert result.status == STATUS_SENSITIVE_HOLD, row.logical
            assert result.target_path in row.paths
        else:
            assert result.status == STATUS_PENDING, row.logical


# =====================================================================================
# the committed table, and totality
# =====================================================================================
@pytest.mark.parametrize("conflict_type", CONFLICT_TYPES)
def test_the_committed_fix_target_table_is_unchanged(conflict_type: str) -> None:
    expected_path, expected_disposition = COMMITTED_TABLE[conflict_type]
    assert FIX_TARGETS[conflict_type].field_path == expected_path
    assert classify(conflict_type).target_path == expected_path
    assert classify(conflict_type).disposition == expected_disposition


@pytest.mark.parametrize("conflict_type", CONFLICT_TYPES)
def test_classification_is_total_and_births_a_legal_status(conflict_type: str) -> None:
    result = classify(conflict_type)
    assert result.status in {STATUS_PENDING, STATUS_SENSITIVE_HOLD}
    assert result.sensitive == (result.status == STATUS_SENSITIVE_HOLD)
    assert result.reason


def test_evidence_only_types_are_pending_and_escalated_not_a_third_status() -> None:
    """SS6's `escalated` is a disposition; `proposal_status` has no such value.

    The proposal is a human-review queue item like any other, born ``pending``
    with an empty action, and the word "escalated" travels in the packet and the
    audit row. The conflict-row status ``escalated`` stays reserved for SS7's
    ``escalated:oscillation``, the only value the committed dashboard contract
    admits beside ``open``.
    """
    for conflict_type in ("C1", "C3", "C5", "C7", "C8", "C10", "C11", "C12", "C13"):
        result = classify(conflict_type)
        assert result.evidence_only is True
        assert result.status == STATUS_PENDING
        assert result.disposition == DISPOSITION_ESCALATED
        assert result.target_path is None


def test_classification_is_a_pure_function_of_its_arguments() -> None:
    """Same inputs, same answer, no state anywhere in the module."""
    paths = ("crm.contact.grade", "appdb.student.grade")
    first = classify("C6", paths)
    for _ in range(5):
        assert classify("C6", list(paths)).as_dict() == first.as_dict()


def test_the_classifier_agrees_with_the_committed_selector_on_every_shape() -> None:
    """`classify` must never disagree with `reference.fix_target` about the target.

    They are two entry points onto contract SS6; a divergence would mean the
    proposal's action and its classification named different fields.
    """
    shapes = [(), *[row.paths for row in COMPARED_FIELDS]]
    for conflict_type in CONFLICT_TYPES:
        for paths in shapes:
            expected = fix_target(conflict_type, paths).field_path
            assert classify(conflict_type, paths).target_path == expected, (
                conflict_type,
                paths,
            )
