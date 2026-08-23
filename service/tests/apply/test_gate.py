"""R24's four conditions, one at a time, with no database.

R24: auto-apply "fires only at confidence >= 0.95, approved case types, complete
evidence, with a recorded rollback path; never touches sensitive fields; applies
only to Keystone's canonical layer -- never to sources."

Each test below removes exactly one condition from an otherwise-passing proposal
and asserts the gate refuses **and names that condition**. A gate whose refusal
reason is vague is a gate whose next failure is unattributable.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from recon.apply import (
    AUTO_APPLY_CASE_TYPES,
    EVIDENCE_SCHEMA,
    ProposalRecord,
    RollbackPath,
    auto_apply_decision,
    write_set_gate,
)
from recon.reference import AUTO_APPLY_ELIGIBLE, FIX_TARGETS, SENSITIVE_FIELDS
from recon.sensitive import classify

OPEN_PATH = RollbackPath(known=True, detail="entity exists, both legs unspent", entity_exists=True)
NO_PATH = RollbackPath(known=False, detail="the citation is already spent")

COMPLETE_EVIDENCE = {
    "schema": EVIDENCE_SCHEMA,
    "completeness": {"incomplete_sources": [], "null_observed_values": []},
    "confidence": {"signals": {"partial_evidence": False, "partial_evidence_reasons": []}},
}


def record(**overrides: object) -> ProposalRecord:
    """A C9 proposal that passes every condition, minus whatever is overridden."""
    base = {
        "id": 7,
        "conflict_id": 3,
        "conflict_type": "C9",
        "fingerprint": "fp",
        "disagreeing_fields": (),
        "action": {"set": {"appdb.enrollment.crm_deal_id": None}},
        "confidence": Decimal("1.0000"),
        "evidence": COMPLETE_EVIDENCE,
        "status": "approved",
        "sensitive": False,
        "target_canonical_id": "0f5f3d3d-0000-4000-8000-000000000001",
    }
    base.update(overrides)
    return ProposalRecord(**base)  # type: ignore[arg-type]


def reasons(decision: object) -> list[str]:
    return [check.name for check in decision.failed]  # type: ignore[attr-defined]


def test_the_baseline_proposal_is_admitted() -> None:
    """Without a passing case the negatives below could all pass vacuously."""
    decision = auto_apply_decision(record(), OPEN_PATH)
    assert decision.allowed, decision.detail
    assert decision.reason == "eligible"
    assert {check.name for check in decision.checks} == {
        "not_sensitive",
        "write_set_eligible",
        "approved_case_type",
        "target_on_allowlist",
        "write_matches_fix_target",
        "confidence_floor",
        "complete_evidence",
        "writes_a_field",
        "rollback_path",
        "status_appliable",
    }


@pytest.mark.parametrize("confidence", ["0.9499", "0.9000", "0.0000", "0.7000"])
def test_below_the_floor_is_refused(confidence: str) -> None:
    decision = auto_apply_decision(record(confidence=Decimal(confidence)), OPEN_PATH)
    assert not decision.allowed
    assert reasons(decision) == ["confidence_floor"]


def test_exactly_the_floor_is_admitted() -> None:
    """0.9500 passes. The boundary is `>=`, and numeric(5,4) can hit it exactly."""
    assert auto_apply_decision(record(confidence=Decimal("0.9500")), OPEN_PATH).allowed


def test_the_approved_case_types_are_derived_from_the_contract() -> None:
    """R24's "approved case types" is contract SS6's eligible column, not a literal."""
    assert {
        conflict_type
        for conflict_type, target in FIX_TARGETS.items()
        if target.classification == "eligible"
    } == AUTO_APPLY_CASE_TYPES
    assert {"C2", "C6", "C9"} == AUTO_APPLY_CASE_TYPES
    for conflict_type in AUTO_APPLY_CASE_TYPES:
        assert FIX_TARGETS[conflict_type].field_path in AUTO_APPLY_ELIGIBLE


@pytest.mark.parametrize(
    "conflict_type", ["C1", "C3", "C5", "C7", "C8", "C10", "C11", "C12", "C13"]
)
def test_an_evidence_only_type_is_refused(conflict_type: str) -> None:
    """Contract SS6's third class: no field write, escalated for human review."""
    decision = auto_apply_decision(
        record(conflict_type=conflict_type, action={"set": {}}), OPEN_PATH
    )
    assert not decision.allowed
    assert "approved_case_type" in reasons(decision)
    assert "writes_a_field" in reasons(decision)


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param(
            {**COMPLETE_EVIDENCE, "completeness": {"incomplete_sources": ["crm"]}},
            id="incomplete_source",
        ),
        pytest.param(
            {**COMPLETE_EVIDENCE, "completeness": {"null_observed_values": ["crm.deal.stage"]}},
            id="null_observed_value",
        ),
        pytest.param(
            {
                **COMPLETE_EVIDENCE,
                "confidence": {
                    "signals": {
                        "partial_evidence": True,
                        "partial_evidence_reasons": ["single_source:crm"],
                    }
                },
            },
            id="partial_evidence_signal",
        ),
        pytest.param({}, id="no_packet_at_all"),
        pytest.param({**COMPLETE_EVIDENCE, "schema": "something.else"}, id="unknown_schema"),
    ],
)
def test_incomplete_evidence_is_refused(evidence: dict[str, object]) -> None:
    decision = auto_apply_decision(record(evidence=evidence), OPEN_PATH)
    assert not decision.allowed
    assert "complete_evidence" in reasons(decision)


def test_an_unknown_rollback_path_is_refused() -> None:
    decision = auto_apply_decision(record(), NO_PATH)
    assert not decision.allowed
    assert reasons(decision) == ["rollback_path"]
    assert "spent" in decision.detail


@pytest.mark.parametrize("status", ["pending", "rejected", "applied", "rolled_back"])
def test_only_an_approved_proposal_is_admitted(status: str) -> None:
    """apply_writer may move `approved -> applied` and nothing else (KS004)."""
    decision = auto_apply_decision(record(status=status), OPEN_PATH)
    assert not decision.allowed
    assert "status_appliable" in reasons(decision)


def test_a_target_outside_the_allowlist_is_refused() -> None:
    """Contract SS6: "eligibility is an allowlist, not the complement of SENSITIVE_FIELDS".

    A C6 whose disagreeing set contains neither an eligible nor a wholly-sensitive
    path makes `reference.fix_target` fall back to its default row, which is not
    in dispute -- and `recon.sensitive.classify` holds it for exactly that reason.
    """
    decision = auto_apply_decision(
        record(conflict_type="C6", disagreeing_fields=("crm.contact.state",)), OPEN_PATH
    )
    assert not decision.allowed
    assert decision.reason == "sensitive_hold"


def test_the_decision_serialises_every_condition() -> None:
    """The audit row and the API both carry the whole verdict, not just a boolean."""
    body = auto_apply_decision(record(confidence=Decimal("0.5")), OPEN_PATH).as_dict()
    assert body["allowed"] is False
    assert body["reason"] == "confidence_floor"
    assert [check["check"] for check in body["checks"]].count("confidence_floor") == 1
    assert all({"check", "passed", "detail"} == set(check) for check in body["checks"])


# =====================================================================================
# The write set: what will this statement WRITE?  (the blocker T-11 shipped)
# =====================================================================================
#
# Everything below is STRUCTURAL. Each test CONSTRUCTS the hostile proposal rather
# than surveying the store, because a survey of 380 real proposals proves a property
# of the current DATA and not of the gate. The previous round's own red team named
# that: "a population property presented as a structural one". A seed change must not
# be able to make these vacuous.


def test_the_demonstrated_attack_is_refused() -> None:
    """The exact proposal that was auto-applied: C2 writing `crm.contact.email` at 0.99.

    `C2` is an approved case type whose committed template writes
    `payments.payment.external_ref`, so the CLASSIFICATION of this conflict is
    "not sensitive" -- asserted below, because if it were sensitive this test
    would be re-proving the gate that already worked. The proposal is refused
    anyway, on the strength of what its ACTION writes.
    """
    hostile = record(
        conflict_type="C2",
        action={"set": {"crm.contact.email": "attacker@evil.test"}},
        confidence=Decimal("0.9900"),
    )
    # The two questions, and the fact that they disagree on this row.
    assert not classify("C2", ()).sensitive, (
        "C2's classification is sensitive, so this proposal would be held by the "
        "classification gate and this test would not be about the write set at all"
    )
    assert "crm.contact.email" in SENSITIVE_FIELDS

    decision = auto_apply_decision(hostile, OPEN_PATH)
    assert not decision.allowed, decision.detail
    assert decision.reason == "sensitive_write"
    assert reasons(decision) == ["write_set_eligible"]
    assert "crm.contact.email" in decision.detail


@pytest.mark.parametrize("path", sorted(SENSITIVE_FIELDS))
@pytest.mark.parametrize("conflict_type", sorted(AUTO_APPLY_CASE_TYPES))
def test_no_approved_case_type_may_write_any_sensitive_path(conflict_type: str, path: str) -> None:
    """Every (approved type x sensitive path) pair, constructed, at confidence 1.0.

    The cross product is the structural statement: not "no proposal in the store
    does this" but "no proposal that did this could be admitted". 3 types x 20
    paths = 60 constructed attacks, none of which exists in any fixture.
    """
    decision = auto_apply_decision(
        record(conflict_type=conflict_type, action={"set": {path: "whatever"}}),
        OPEN_PATH,
    )
    assert not decision.allowed, f"{conflict_type} writing {path} was admitted"
    assert decision.reason == "sensitive_write"
    assert path in decision.detail


def test_a_sensitive_path_smuggled_alongside_an_eligible_one_is_refused() -> None:
    """One bad key in a multi-key action is enough. The gate reads EVERY key."""
    decision = auto_apply_decision(
        record(
            action={
                "set": {
                    "appdb.enrollment.crm_deal_id": "d-1",
                    "crm.contact.dob": "2010-01-01",
                }
            }
        ),
        OPEN_PATH,
    )
    assert not decision.allowed
    assert decision.reason == "sensitive_write"


@pytest.mark.parametrize(
    "path",
    [
        "crm.deal.pipeline",  # contract SS12 D-4: deliberately not eligible
        "crm.contact.state",  # SS6: no longer compared by anything
        "survived",  # the nested map, which no template writes
        "a.brand.new.field",  # the field that does not exist yet
        "",  # the empty key
    ],
)
def test_a_path_on_neither_list_is_refused_never_admitted_by_default(path: str) -> None:
    """Contract SS6: "eligibility is an allowlist, not the complement of SENSITIVE_FIELDS".

    This is the arm that matters for the NEXT field rather than for today's: a
    path nobody has classified must be refused, because the alternative is that
    every new field is auto-appliable from the moment it is named.
    """
    assert path not in SENSITIVE_FIELDS
    assert path not in AUTO_APPLY_ELIGIBLE
    decision = auto_apply_decision(record(action={"set": {path: "v"}}), OPEN_PATH)
    assert not decision.allowed, f"{path!r} was admitted by default"
    assert decision.reason == "write_off_allowlist"


@pytest.mark.parametrize("path", sorted(AUTO_APPLY_ELIGIBLE))
def test_every_allowlisted_path_still_passes_the_write_set_gate(path: str) -> None:
    """The control. Without it the tests above could pass by refusing everything."""
    verdict = write_set_gate({path: "v"})
    assert verdict.cleared, verdict.detail
    assert verdict.sensitive_paths == ()
    assert verdict.unlisted_paths == ()


def test_the_write_set_gate_takes_no_confidence() -> None:
    """R15 holds at every score, so the gate that enforces it cannot express one."""
    import inspect

    parameters = set(inspect.signature(write_set_gate).parameters)
    forbidden = {name for name in parameters if "confidence" in name or "score" in name}
    assert not forbidden, f"write_set_gate takes {sorted(forbidden)}"


def test_an_empty_write_set_clears_this_gate_and_is_refused_by_the_next_one() -> None:
    """Naming the right refusal: an evidence-only proposal writes no forbidden path.

    It writes no path at all, which is `writes_a_field`'s subject and not this
    gate's. Stated as a test so the two are not merged into one vague refusal.
    """
    assert write_set_gate({}).cleared
    decision = auto_apply_decision(record(conflict_type="C1", action={"set": {}}), OPEN_PATH)
    assert not decision.allowed
    assert "writes_a_field" in reasons(decision)
    assert "write_set_eligible" not in reasons(decision)
