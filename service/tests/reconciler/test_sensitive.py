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

import ast
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
from recon.resolve import SURVIVED_PATHS
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


def test_no_code_path_in_the_classifier_compares_anything_to_a_number() -> None:
    """0.95 is R24's auto-apply gate and no arm of the hold decision may consult it.

    CONTRACT STRENGTHENED. This was a scan of the file's CHARACTERS that dropped
    lines starting with ``#``, ``"`` or ``*`` and then applied
    ``.replace('">= 0.95', "")`` before asserting ``"0.95" not in body``. The
    literal it erased is the exact one that appears in ``recon/sensitive.py``
    (inside the eligible branch's ``reason`` string), so the test edited the
    source text to accommodate the code it constrains -- a materially weaker
    guarantee than "the string 0.95 appears nowhere in the hold decision's code
    path", which is what its name claimed.

    The AST is the right instrument: it constrains the CODE rather than the file's
    characters, needs no carve-out, and asserts something stronger than the
    absence of one literal -- that the module's executable body contains no
    numeric constant and no comparison operator at all. A threshold cannot be
    applied without one of those, whatever it is spelled.
    """
    from pathlib import Path

    tree = ast.parse(Path(inspect.getsourcefile(classify) or "").read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, complex)):
            if isinstance(node.value, bool):
                continue  # `True`/`False` are ast.Constant of type bool, not thresholds
            offenders.append(f"numeric constant {node.value!r} at line {node.lineno}")
        elif isinstance(node, ast.Compare):
            ops = "".join(type(op).__name__ for op in node.ops)
            # `==` / `!=` / `in` / `not in` are set and string membership tests, which
            # are the classifier's whole mechanism. An ORDERING comparison is what a
            # threshold needs, and there is none.
            if any(
                isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for op in node.ops
            ):  # pragma: no cover - asserted absent
                offenders.append(f"ordering comparison {ops} at line {node.lineno}")
    assert offenders == [], (
        "recon/sensitive.py's executable body must contain no numeric constant and "
        f"no ordering comparison; a threshold needs one of them. Found: {offenders}"
    )


def test_a_classification_cannot_be_born_already_decided() -> None:
    """The birth vocabulary is enforced by the CODE, not only by the trigger.

    ``Classification.__post_init__`` validated the sensitive/status PAIR but never
    checked that ``status`` was a birth status at all, so
    ``Classification(..., status='approved')`` constructed cleanly and
    ``_insert_proposal`` would have passed it straight into the INSERT. The only
    thing that refused it was ``keystone_proposal_born_pending`` (KS002) -- a
    backstop doing a control's job, which is one migration away from being no
    control at all. A mutation forcing ``status='approved'`` into
    ``_insert_proposal`` was killed 12 failed / 22 errors, and every single
    failure traced to the database; zero code-level assertions fired.
    """
    for status in ("approved", "applied", "rejected", "rolled_back", "", "PENDING"):
        with pytest.raises(ValueError, match="birth status"):
            Classification(
                conflict_type="C6",
                target_path="crm.contact.grade",
                status=status,
                sensitive=False,
                disposition=DISPOSITION_ELIGIBLE,
                sensitive_paths=(),
                reason="hand-built",
                auto_apply_eligible_path=True,
            )


def test_the_birth_vocabulary_is_exactly_the_two_statuses() -> None:
    from recon.sensitive import BIRTH_STATUSES

    assert frozenset({STATUS_PENDING, STATUS_SENSITIVE_HOLD}) == BIRTH_STATUSES


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


# =====================================================================================
# the fail-open in the committed selector, closed on this side
# =====================================================================================
def test_a_c6_whose_target_is_not_even_in_dispute_is_held() -> None:
    """`reference.fix_target`'s C6 branch fails OPEN; `classify` must not.

    When the disagreeing set holds no wholly-sensitive comparison row AND no
    ``AUTO_APPLY_ELIGIBLE`` path, ``fix_target`` falls back to
    ``FIX_TARGETS["C6"]`` = ``crm.contact.grade`` -- which is *eligible*, and which
    is not in the disagreeing set at all. So before this arm existed,
    ``classify("C6", ["appdb.student.status"])`` returned a NOT-held, auto-apply
    eligible proposal for a conflict whose only disagreeing path is SENSITIVE.
    Contract SS6 pins the opposite direction: "A field path in neither set is not
    auto-applyable: eligibility is an allowlist, not the complement of
    SENSITIVE_FIELDS."

    Not reachable from the committed engine today -- SS2.4 puts BOTH endpoints of
    every disagreeing row into ``disagreeing_fields``, so a single-endpoint C6 is
    never produced -- which is why the real run is correct. It is a latent
    fail-open in the module the classifier trusts, and ``reference.py`` is
    authoritative and shared, so it is closed here and flagged for its owner.
    """
    # The two shapes that reach the selector's fallback row: a SENSITIVE path whose
    # comparison row is not wholly sensitive, and a path outside the vocabulary.
    # Both previously returned target=crm.contact.grade, status=pending,
    # auto_apply_eligible=True.
    for paths in (("appdb.student.status",), ("nonsense.path.x",)):
        result = classify("C6", paths)
        assert result.status == STATUS_SENSITIVE_HOLD, paths
        assert result.auto_apply_eligible_path is False, paths
        assert "not among them" in result.reason, paths

    # A wholly-sensitive path IS selected (it is in the set), so it is held by the
    # ordinary SENSITIVE_FIELDS arm rather than by the fallback guard -- the right
    # answer for the right reason.
    single = classify("C6", ("appdb.student.dob",))
    assert single.status == STATUS_SENSITIVE_HOLD
    assert single.target_path == "appdb.student.dob"
    assert "SENSITIVE_FIELDS" in single.reason


def test_the_fallback_arm_does_not_fire_on_any_shape_the_engine_produces() -> None:
    """The sabotage check: the guard above must not hold a legitimate proposal.

    Contract SS2.4 makes `disagreeing_fields` the sorted set of BOTH endpoints of
    every disagreeing comparison row, so those are the shapes that actually occur.
    If the guard fired on one of them it would convert real eligible C6 proposals
    into holds, and the test above would be proving nothing but over-caution.
    """
    for row in COMPARED_FIELDS:
        result = classify("C6", row.paths)
        assert result.status == (
            STATUS_SENSITIVE_HOLD if row.wholly_sensitive else STATUS_PENDING
        ), row.logical
    # and the empty set (SS6's per-type table, no disagreeing paths to be in)
    assert classify("C6").status == STATUS_PENDING


def test_an_unlisted_target_is_held_and_that_arm_is_reachable(monkeypatch) -> None:
    """SS6: "eligibility is an allowlist, not the complement of SENSITIVE_FIELDS".

    ``reference._fix_target`` raises for a path on neither committed list, so this
    arm of ``classify`` cannot be reached through the real selector -- which is
    itself the reason it was marked ``# pragma: no cover`` and never exercised. It
    is still the arm that decides what happens if a future template writes a path
    nobody classified, so it is driven directly here rather than assumed.
    """
    from recon.reference import FixTarget

    monkeypatch.setattr(
        "recon.sensitive.fix_target",
        lambda conflict_type, paths=(): FixTarget(
            conflict_type, "crm.contact.nickname", "eligible"
        ),
    )
    result = classify("C6", ("crm.contact.nickname", "appdb.student.nickname"))
    assert result.status == STATUS_SENSITIVE_HOLD
    assert result.sensitive is True
    assert result.auto_apply_eligible_path is False
    assert "allowlist" in result.reason


def test_a_lifecycle_only_c6_never_writes_the_sensitive_app_db_path() -> None:
    """The one shape where a sensitive path and a non-held proposal coexist.

    ``appdb.student.status`` IS in ``SENSITIVE_FIELDS``; its CRM counterpart
    ``crm.contact.lifecycle_stage`` is not, and SS6 pins the target as the CRM
    side, "eligible **only** when the proposal writes the CRM side and leaves
    ``appdb.student.status`` untouched". So the proposal is correctly not held --
    but it is the single shape in the whole graded run (120 of 3,050 proposals)
    where "a sensitive path is in the conflict" and "the proposal is not held" are
    both true, and the safety claim should be ENFORCED rather than asserted in
    prose.
    """
    from recon.reconciler import fix_action

    paths = ("crm.contact.lifecycle_stage", "appdb.student.status")
    result = classify("C6", paths)
    assert result.status == STATUS_PENDING
    assert "appdb.student.status" in result.sensitive_paths
    assert result.target_path == "crm.contact.lifecycle_stage"

    action = fix_action(
        "C6",
        disagreeing_fields=paths,
        observed_values={
            "crm.contact.lifecycle_stage": "applicant",
            "appdb.student.status": "enrolled",
        },
    )
    assert "appdb.student.status" not in action.action["set"], (
        "a lifecycle-only C6 writes the CRM side and leaves the sensitive app-DB "
        "path untouched (SS6); writing it would make 120 proposals a sensitive write"
    )
    assert set(action.action["set"]) <= {"crm.contact.lifecycle_stage"}


# =====================================================================================
# the second, independent control: R15 re-derived from the ACTION's own keys
# =====================================================================================
def _classification(**kwargs: object) -> Classification:
    defaults: dict[str, object] = {
        "conflict_type": "C6",
        "target_path": "crm.contact.grade",
        "status": STATUS_PENDING,
        "sensitive": False,
        "disposition": DISPOSITION_ELIGIBLE,
        "sensitive_paths": (),
        "reason": "test",
        "auto_apply_eligible_path": True,
    }
    defaults.update(kwargs)
    return Classification(**defaults)  # type: ignore[arg-type]


def _conflict(**kwargs: object):
    from recon.reconciler import ConflictRow

    defaults: dict[str, object] = {
        "id": 1,
        "fingerprint": "fp",
        "type": "C6",
        "rule_id": "R-006",
        "entity_refs": ("appdb:student:s1", "crm:contact:c1"),
        "sources_involved": ("appdb", "crm"),
        "disagreeing_fields": ("appdb.student.grade", "crm.contact.grade"),
        "observed_values": {},
        "oscillating": False,
        "status": "open",
        "escalation_reason": None,
    }
    defaults.update(kwargs)
    return ConflictRow(**defaults)  # type: ignore[arg-type]


def _action(assignments: dict[str, object]):
    from recon.reconciler import FixAction

    return FixAction(
        conflict_type="C6",
        target_path=next(iter(assignments), None),
        value=None,
        derivable=True,
        derivation="test",
        action={"set": dict(assignments)},
    )


def test_a_non_held_proposal_writing_a_sensitive_path_is_refused() -> None:
    """The row the DATABASE accepts, refused by a component that did not classify.

    KS002 binds `sensitive` to the birth STATUS only. Nothing in the schema binds
    `sensitive` to the paths the action writes, so this exact row -- non-sensitive,
    pending, writing `crm.contact.dob` -- is accepted by every committed
    constraint and would be auto-appliable under R24 at >= 0.95. It is what a
    re-targeting or classifier bug produces.
    """
    from recon.reconciler import _assert_action_matches_classification

    with pytest.raises(ValueError, match="SENSITIVE path"):
        _assert_action_matches_classification(
            _conflict(),
            _action({"crm.contact.dob": "2010-01-01"}),
            _classification(),
        )


def test_a_non_held_proposal_writing_an_unlisted_path_is_refused() -> None:
    """SS6: "eligibility is an allowlist, not the complement of SENSITIVE_FIELDS"."""
    from recon.reconciler import _assert_action_matches_classification

    with pytest.raises(ValueError, match="AUTO_APPLY_ELIGIBLE"):
        _assert_action_matches_classification(
            _conflict(),
            _action({"crm.contact.nickname": "x"}),
            _classification(),
        )


def test_a_held_proposal_may_write_only_its_own_target() -> None:
    from recon.reconciler import _assert_action_matches_classification

    held = _classification(
        target_path="crm.contact.dob",
        status=STATUS_SENSITIVE_HOLD,
        sensitive=True,
        disposition=DISPOSITION_SENSITIVE_HOLD,
        auto_apply_eligible_path=False,
    )
    # its own target: fine
    _assert_action_matches_classification(
        _conflict(), _action({"crm.contact.dob": "2010-01-01"}), held
    )
    with pytest.raises(ValueError, match="beyond its own target"):
        _assert_action_matches_classification(
            _conflict(),
            _action({"crm.contact.dob": "2010-01-01", "crm.contact.last_name": "x"}),
            held,
        )


def test_the_real_committed_templates_all_pass_the_recheck() -> None:
    """The control: the guard must not reject anything the engine actually builds.

    Driven over every COMPARED_FIELDS shape for every conflict type, using the
    real `fix_action` and the real `classify` -- so a guard that rejected
    legitimate work would fail here rather than in production.
    """
    from recon.reconciler import _assert_action_matches_classification, fix_action

    checked = 0
    for conflict_type in CONFLICT_TYPES:
        for paths in [(), *[row.paths for row in COMPARED_FIELDS]]:
            classification = classify(conflict_type, paths)
            action = fix_action(conflict_type, disagreeing_fields=paths)
            if action.target_path != classification.target_path:
                continue  # build_packet's own guard covers this; not this test's job
            _assert_action_matches_classification(
                _conflict(type=conflict_type, disagreeing_fields=paths),
                action,
                classification,
            )
            checked += 1
    assert checked >= 98, f"only {checked} shapes checked"


# =====================================================================================
# the recheck judges the PATHS the action writes, not the keys it names
# =====================================================================================

_SURVIVED_NOW = {path: f"current-{path}" for path in SURVIVED_PATHS}


def test_the_recheck_admits_the_committed_nested_form() -> None:
    """The observable shape must not be rejected by the guard that judges it.

    `recon.reconciler` writes an eligible `survived` member INTO the map, so the
    action names ONE key -- `survived` -- and writes one path. A key-level
    recheck refuses it (`survived` is on neither committed list) and would have
    deleted the only observable auto-apply the contract has. The recheck asks
    `recon.apply.effective_write_paths` against the entity row instead.
    """
    from recon.reconciler import _assert_action_matches_classification, fix_action

    paths = ("crm.contact.lifecycle_stage", "appdb.student.status")
    classification = classify("C6", paths)
    assert classification.target_path == "crm.contact.lifecycle_stage"
    action = fix_action(
        "C6",
        disagreeing_fields=paths,
        observed_values={
            "crm.contact.lifecycle_stage": "applied",
            "appdb.student.status": "enrolled",
        },
        survived=_SURVIVED_NOW,
    )
    assert set(action.action["set"]) == {"survived"}
    _assert_action_matches_classification(
        _conflict(type="C6", disagreeing_fields=paths),
        action,
        classification,
        {"survived": _SURVIVED_NOW},
    )


def test_the_recheck_refuses_the_same_shape_carrying_a_sensitive_change() -> None:
    """...and it is not admitting the SHAPE, it is judging the write.

    The identical container with a `SENSITIVE_FIELDS` member replaced writes a
    path R15 forbids, and the recheck raises on it -- the control that keeps the
    test above from being "the guard ignores nested actions".
    """
    from recon.reconciler import FixAction, _assert_action_matches_classification

    paths = ("crm.contact.lifecycle_stage", "appdb.student.status")
    hostile = FixAction(
        conflict_type="C6",
        target_path="crm.contact.lifecycle_stage",
        value="customer",
        derivable=True,
        derivation="hand-built for this test",
        action={"set": {"survived": {**_SURVIVED_NOW, "crm.contact.email": "attacker@evil.test"}}},
        container="survived",
    )
    with pytest.raises(ValueError, match="SENSITIVE path"):
        _assert_action_matches_classification(
            _conflict(type="C6", disagreeing_fields=paths),
            hostile,
            classify("C6", paths),
            {"survived": _SURVIVED_NOW},
        )


def test_the_recheck_without_the_row_refuses_a_nested_action() -> None:
    """No row to diff against is the CONSERVATIVE input, never the permissive one.

    Every member the action carries then counts as written, six of them
    sensitive, so a caller that authored a nested action and forgot to pass the
    row it was authored against fails loudly instead of being waved through.
    """
    from recon.reconciler import _assert_action_matches_classification, fix_action

    paths = ("crm.contact.lifecycle_stage", "appdb.student.status")
    action = fix_action(
        "C6",
        disagreeing_fields=paths,
        observed_values={
            "crm.contact.lifecycle_stage": "applied",
            "appdb.student.status": "enrolled",
        },
        survived=_SURVIVED_NOW,
    )
    with pytest.raises(ValueError, match="SENSITIVE path"):
        _assert_action_matches_classification(
            _conflict(type="C6", disagreeing_fields=paths), action, classify("C6", paths)
        )
