"""R15/R24's structural claim: a sensitive proposal never reaches the confidence gate.

The claim is *not* "a sensitive proposal fails the threshold". A threshold test
proves the comparison is written the way it is written today; it says nothing
about the next edit. The claim is that the confidence of a sensitive proposal is
**never read at all**, and that is a property of the call graph, so it is proved
against the call graph:

1. :func:`recon.apply.sensitivity_gate` has no confidence parameter -- asserted
   from its signature, so adding one is a red test rather than a quiet change;
2. the only function that reads :data:`recon.apply.AUTO_APPLY_CONFIDENCE_FLOOR`
   takes an :class:`recon.apply.EligibilityClearance`, which cannot be
   constructed around a sensitive classification -- asserted by trying;
3. handed a proposal whose ``confidence`` attribute **raises on access**,
   :func:`recon.apply.auto_apply_decision` still returns a refusal. A single
   read of that attribute anywhere on the sensitive path turns this test red.

(3) is the one that binds. It is run over every sensitive shape the committed
contract can produce, at a fabricated confidence of 1.0 in the underlying row,
and none of them can consult it.

No database. These are properties of the module.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from decimal import Decimal
from pathlib import Path

import pytest

from recon import apply as apply_module
from recon.apply import (
    AUTO_APPLY_CONFIDENCE_FLOOR,
    EVIDENCE_SCHEMA,
    EligibilityClearance,
    ProposalRecord,
    RollbackPath,
    auto_apply_decision,
    sensitivity_gate,
    write_set_gate,
)
from recon.reference import AUTO_APPLY_ELIGIBLE, SENSITIVE_FIELDS
from recon.sensitive import classify

#: A rollback path that is deliberately WIDE OPEN. If a sensitive proposal is
#: still refused with this, the refusal came from the sensitivity gate and from
#: nothing downstream of it.
OPEN_PATH = RollbackPath(known=True, detail="deliberately open", entity_exists=True)


class ConfidenceIsPoison(ProposalRecord):
    """A proposal whose confidence cannot be read without raising.

    This is the instrument. ``ProposalRecord`` is a plain (non-slotted) dataclass
    precisely so an instance can be re-classed into this one after construction:
    ``property`` is a data descriptor, so it wins over the instance attribute the
    dataclass ``__init__`` already stored, and any expression that touches
    ``.confidence`` on such a record raises with the rule it broke.
    """

    @property  # type: ignore[override]
    def confidence(self) -> Decimal:
        raise AssertionError(
            "the confidence of a SENSITIVE proposal was read. R15: a sensitive-field "
            "proposal can never auto-apply at any confidence, so nothing on the "
            "sensitive path may consult a score -- not even to reject it."
        )


def _poisoned(
    conflict_type: str, disagreeing: tuple[str, ...] = (), **overrides: object
) -> ProposalRecord:
    """A record carrying confidence 1.0 that cannot be read."""
    record = ProposalRecord(
        id=1,
        conflict_id=1,
        conflict_type=conflict_type,
        fingerprint="f",
        disagreeing_fields=disagreeing,
        action={"set": {}},
        confidence=Decimal("1.0"),
        evidence={},
        status="sensitive_hold",
        sensitive=True,
        target_canonical_id="00000000-0000-0000-0000-000000000001",
    )
    for name, value in overrides.items():
        object.__setattr__(record, name, value)
    # The stored value is 1.0 and stays 1.0; only its READABILITY is removed.
    assert record.confidence == Decimal("1.0")
    object.__setattr__(record, "__class__", ConfidenceIsPoison)
    with pytest.raises(AssertionError, match="was read"):
        _ = record.confidence
    return record


# ---------------------------------------------------------------------------
# (1) the gate cannot see a score
# ---------------------------------------------------------------------------


def test_the_sensitivity_gate_takes_no_confidence() -> None:
    """`sensitivity_gate` has no score-shaped parameter, by signature."""
    parameters = set(inspect.signature(sensitivity_gate).parameters)
    forbidden = {name for name in parameters if "confidence" in name or "score" in name}
    assert not forbidden, (
        f"sensitivity_gate takes {sorted(forbidden)}. R15's classifier is a pure function "
        "of the target field path, evaluated BEFORE confidence; a gate that can see a "
        "score is a gate a later edit can make conditional on one."
    )
    assert "confidence" not in set(inspect.signature(apply_module.classify).parameters)


def test_only_one_function_reads_the_confidence_floor() -> None:
    """The 0.95 comparison lives in exactly one place, and that place is gated.

    Parsed from the source rather than grepped: the name of the function that
    reads the floor is what the next assertion is about, so it must be derived
    and not assumed.
    """
    tree = ast.parse(Path(inspect.getfile(apply_module)).read_text())
    readers = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(inner, ast.Name) and inner.id == "AUTO_APPLY_CONFIDENCE_FLOOR"
            for inner in ast.walk(node)
        )
    }
    assert readers == {"_eligibility_checks"}, (
        f"AUTO_APPLY_CONFIDENCE_FLOOR is read by {sorted(readers)}. R24's floor belongs "
        "in one function, and that function must be the one the sensitivity gate guards."
    )
    guarded = inspect.signature(apply_module._eligibility_checks).parameters
    assert next(iter(guarded)) == "clearance"
    assert guarded["clearance"].annotation in ("EligibilityClearance", EligibilityClearance)


# ---------------------------------------------------------------------------
# (2) the clearance token cannot be minted for a sensitive proposal
# ---------------------------------------------------------------------------


#: A write set the gate clears, so a clearance can be minted for the tests that
#: are about the OTHER half. `crm.contact.grade` is on contract SS6's allowlist.
CLEARED_WRITE_SET = write_set_gate({"crm.contact.grade": "7"})


def test_the_clearance_needs_both_verdicts() -> None:
    """The token is minted from BOTH R15 gates, not from the classification alone.

    `EligibilityClearance` used to carry only the classification, which is what
    let a proposal with a clean classification and a hostile ACTION walk into
    `_eligibility_checks` and be admitted at 0.99. Asserted from the signature so
    dropping a field back off it is a red test rather than a quiet widening.
    """
    fields = {field.name for field in dataclasses.fields(EligibilityClearance)}
    assert fields == {"classification", "write_set"}
    assert CLEARED_WRITE_SET.cleared


@pytest.mark.parametrize("path", sorted(SENSITIVE_FIELDS))
def test_no_clearance_can_be_minted_for_a_sensitive_target(path: str) -> None:
    """Every one of contract SS6's sensitive paths is unclearable AS A TARGET."""
    classification = classify("C14", (path,))
    if not classification.sensitive:  # pragma: no cover - C14 holds on type alone
        pytest.fail(f"classify() did not hold the sensitive path {path!r}")
    with pytest.raises(ValueError, match="never reaches the confidence gate"):
        EligibilityClearance(classification, CLEARED_WRITE_SET)


@pytest.mark.parametrize("path", sorted(SENSITIVE_FIELDS))
def test_no_clearance_can_be_minted_for_a_sensitive_WRITE(path: str) -> None:
    """...and every one of them is unclearable as a WRITE, on a clean classification.

    The classification handed in here is `C9`'s, which is `eligible`: exactly the
    combination the demonstrated attack used. The token still cannot be minted,
    so `_eligibility_checks` -- the only reader of the 0.95 floor -- is
    unreachable for it.
    """
    clean = classify("C9", ())
    assert not clean.sensitive
    with pytest.raises(ValueError, match="never reaches the confidence gate"):
        EligibilityClearance(clean, write_set_gate({path: "v"}))


@pytest.mark.parametrize("path", ["crm.deal.pipeline", "a.brand.new.field"])
def test_no_clearance_can_be_minted_for_an_unlisted_WRITE(path: str) -> None:
    """Eligibility is an allow-list: a path in neither set cannot be cleared either."""
    assert path not in SENSITIVE_FIELDS and path not in AUTO_APPLY_ELIGIBLE
    with pytest.raises(ValueError, match="never reaches the confidence gate"):
        EligibilityClearance(classify("C9", ()), write_set_gate({path: "v"}))


# ---------------------------------------------------------------------------
# (3) the binding proof: the score is unreadable and the refusal still happens
# ---------------------------------------------------------------------------

#: Every shape contract SS6 classifies `sensitive_hold`: every C14 (held on its
#: type), C4 (`crm.contact.email`), and a mixed C6 (a wholly-sensitive row plus
#: an eligible one).
SENSITIVE_SHAPES = [
    ("C14", ("crm.contact.first_name", "appdb.student.first_name")),
    ("C14", ("crm.contact.dob", "appdb.student.dob")),
    ("C14", ("crm.deal.stage", "appdb.enrollment.stage")),
    ("C4", ()),
    (
        "C6",
        (
            "crm.contact.first_name",
            "appdb.student.first_name",
            "crm.contact.grade",
            "appdb.student.grade",
        ),
    ),
]


@pytest.mark.parametrize(("conflict_type", "disagreeing"), SENSITIVE_SHAPES)
def test_a_sensitive_proposal_is_refused_without_its_score_being_read(
    conflict_type: str, disagreeing: tuple[str, ...]
) -> None:
    """The graded proof. Reading the score at all raises; the refusal still lands."""
    decision = auto_apply_decision(_poisoned(conflict_type, disagreeing), OPEN_PATH)
    assert not decision.allowed
    assert decision.reason == "sensitive_hold"
    assert decision.checks == decision.failed  # the gate stopped at the first check
    assert len(decision.checks) == 1, (
        f"the sensitive path evaluated {[c.name for c in decision.checks]}; it must "
        "return at the sensitivity gate, before any eligibility condition is considered"
    )


@pytest.mark.parametrize(("conflict_type", "disagreeing"), SENSITIVE_SHAPES)
def test_the_stored_flag_alone_also_holds_it(
    conflict_type: str, disagreeing: tuple[str, ...]
) -> None:
    """Three independent facts hold a proposal; each is sufficient on its own.

    Here the classifier is fed a shape it would clear (`C9`), and the hold comes
    from the stored `sensitive` column and from the `sensitive_hold` status. A
    corrupted classifier is therefore not enough to unlock a write.
    """
    record = _poisoned("C9")
    decision = auto_apply_decision(record, OPEN_PATH)
    assert not decision.allowed
    assert decision.reason == "sensitive_hold"
    assert "proposals.sensitive is true" in decision.detail
    assert "status is sensitive_hold" in decision.detail


def test_the_floor_itself_is_exact() -> None:
    """0.95 as a Decimal, so a score of exactly 0.95 passes deterministically."""
    assert Decimal("0.95") == AUTO_APPLY_CONFIDENCE_FLOOR
    assert Decimal("0.9500") >= AUTO_APPLY_CONFIDENCE_FLOOR
    assert Decimal("0.9499") < AUTO_APPLY_CONFIDENCE_FLOOR


# ---------------------------------------------------------------------------
# (4) the same binding proof for the WRITE SET, on a clean classification
# ---------------------------------------------------------------------------

#: Write sets R24 must refuse, each on a conflict type whose CLASSIFICATION is
#: eligible -- so the refusal can only have come from reading the action.
FORBIDDEN_WRITE_SETS = [
    pytest.param({"crm.contact.email": "attacker@evil.test"}, id="the_demonstrated_attack"),
    pytest.param({"crm.contact.dob": "2010-01-01"}, id="dob"),
    pytest.param({"appdb.student.student_number": "S-1"}, id="student_number"),
    pytest.param({"crm.deal.stage": "Closed Won"}, id="financial_status"),
    pytest.param({"crm.contact.marketing_consent": False}, id="consent_flag"),
    pytest.param(
        {"crm.contact.grade": "7", "appdb.student.dob": "2010-01-01"},
        id="smuggled_beside_an_eligible_key",
    ),
    pytest.param({"crm.deal.pipeline": "x"}, id="unlisted_path"),
]


@pytest.mark.parametrize("assignments", FORBIDDEN_WRITE_SETS)
def test_a_forbidden_write_is_refused_without_its_score_being_read(
    assignments: dict[str, object],
) -> None:
    """The binding proof for the write set, in the same instrument as (3).

    The record's conflict type is `C9` and its stored `sensitive` flag is False
    and its status is `approved`, so NOTHING here is held by the classification
    -- the control below asserts that by admitting the same record with an
    allowlisted action. Its confidence is 1.0 and unreadable. The refusal still
    lands, so R24's floor was never consulted: a proposal R15 forbids cannot
    reach the confidence gate at any score, including 1.0.
    """
    record = _poisoned("C9", (), action={"set": assignments}, status="approved", sensitive=False)
    decision = auto_apply_decision(record, OPEN_PATH)
    assert not decision.allowed
    assert decision.reason in {"sensitive_write", "write_off_allowlist"}
    assert [check.name for check in decision.checks] == ["not_sensitive", "write_set_eligible"]


def test_the_control_the_forbidden_writes_are_measured_against() -> None:
    """A no-op control: the SAME record with an allowlisted action must be ADMITTED.

    Without this the parametrized test above could pass because the record shape
    is refused for some unrelated reason -- the "green that proves nothing"
    failure. Here confidence IS read (it is the eligibility gate's job), so the
    record is a plain one rather than a poisoned one.
    """
    admitted = ProposalRecord(
        id=1,
        conflict_id=1,
        conflict_type="C9",
        fingerprint="f",
        disagreeing_fields=(),
        action={"set": {"appdb.enrollment.crm_deal_id": "d-1"}},
        confidence=Decimal("1.0"),
        evidence={
            "schema": EVIDENCE_SCHEMA,
            "completeness": {"incomplete_sources": [], "null_observed_values": []},
            "confidence": {"signals": {"partial_evidence": False}},
        },
        status="approved",
        sensitive=False,
        target_canonical_id="00000000-0000-0000-0000-000000000001",
    )
    decision = auto_apply_decision(admitted, OPEN_PATH)
    assert decision.allowed, decision.detail
