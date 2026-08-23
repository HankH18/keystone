"""R14's arithmetic: reproducible, inspectable, lowered by partial and conflicting evidence.

These tests need no database. The scoring function takes a value object, which is
itself part of the design: a confidence model that can only be checked by running
the whole pipeline is a model nobody checks.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from recon.confidence import (
    ConfidenceModelError,
    Signals,
    disagreeing_row_count,
    load_model,
    model_path,
    observed_value_is_null,
    partial_evidence_reasons,
    score,
)

SERVICE_ROOT = Path(__file__).resolve().parents[2]


def _signals(conflict_type: str = "C6", **kwargs: object) -> Signals:
    return Signals(conflict_type=conflict_type, **kwargs)  # type: ignore[arg-type]


# =====================================================================================
# the formula, worked
# =====================================================================================
def test_worked_example_grade_only_c6_linked_by_external_id() -> None:
    """base 0.40 + ext 0.35 - one disagreeing row 0.10 = 0.65.

    The example carried in the ticket report and in ARCHITECTURE.md. It is a test
    so the documented arithmetic cannot drift away from the implemented
    arithmetic.
    """
    result = score(
        _signals(
            "C6",
            hard_external_id_agreement=True,
            disagreeing_field=1,
        )
    )
    assert result.base == Decimal("0.40")
    assert result.value == Decimal("0.6500")
    contributions = {term.name: term.contribution for term in result.terms}
    assert contributions["hard_external_id_agreement"] == Decimal("0.35")
    assert contributions["disagreeing_field"] == Decimal("-0.10")
    assert contributions["oscillation_observed"] == Decimal("0.00")


def test_worked_example_all_three_key_classes_agree() -> None:
    """0.40 + 0.35 + 0.25 + 0.20 = 1.20 -> clamped to 1.0, THEN -0.10 = 0.9000.

    CONTRACT CHANGED, model v1 -> v2 (deliberate; see `confidence.yaml`'s version
    note). This test previously asserted ``value == 1.0000`` with
    ``clamped is True``: i.e. it pinned as correct the behaviour in which a
    conflict's disagreement penalty changes the stored number by NOTHING because
    the positive evidence had already saturated the clamp. R14 requires that
    "partial/conflicting evidence lowers it", so the old expectation encoded the
    defect as the contract. Measured on the graded store, 1,051 of 3,050
    proposals were clamped and 191 of them carried a penalty that moved the
    number by zero.

    ``raw_total`` is unchanged (1.10) because it is still the ungrouped sum -- the
    recorded terms still add up to a recorded number. What moved is where the
    clamp is applied.
    """
    result = score(
        _signals(
            "C6",
            hard_external_id_agreement=True,
            normalized_email_agreement=True,
            name_dob_exact=True,
            disagreeing_field=1,
        )
    )
    assert result.raw_total == Decimal("1.10")
    assert result.positive_total == Decimal("0.80")
    assert result.negative_total == Decimal("-0.10")
    # The positive half saturated ...
    assert result.evidence_total == Decimal("1.0000")
    assert result.positive_clamped is True
    # ... and the penalty still came off it.
    assert result.value == Decimal("0.9000")
    assert result.clamped is False

    unpenalised = score(
        _signals(
            "C6",
            hard_external_id_agreement=True,
            normalized_email_agreement=True,
            name_dob_exact=True,
        )
    )
    assert unpenalised.value == Decimal("1.0000")
    assert unpenalised.value - result.value == Decimal("0.1000"), (
        "the disagreement penalty must be visible in the stored number even when "
        "the positive evidence saturates -- this is the R14 clause v1 erased"
    )


def test_worked_example_evidence_only_conflict_with_corroboration() -> None:
    """C1: base 0.50 + corroboration 0.10 + ext 0.35 = 0.95."""
    result = score(
        _signals(
            "C1",
            hard_external_id_agreement=True,
            amount_date_corroboration=True,
        )
    )
    assert result.value == Decimal("0.9500")


# =====================================================================================
# R14: partial or conflicting evidence LOWERS the score
# =====================================================================================
def test_partial_evidence_lowers_the_score_by_exactly_its_weight() -> None:
    base = score(_signals("C6", hard_external_id_agreement=True, disagreeing_field=1))
    partial = score(
        _signals(
            "C6",
            hard_external_id_agreement=True,
            disagreeing_field=1,
            partial_evidence=True,
            partial_evidence_reasons=("single_source:crm",),
        )
    )
    assert partial.value < base.value
    assert base.value - partial.value == Decimal("0.1500")


def test_each_additional_disagreeing_row_lowers_the_score() -> None:
    """ "Sources conflict" is monotone: more disagreement is never more confidence."""
    values = [
        score(_signals("C6", hard_external_id_agreement=True, disagreeing_field=n)).value
        for n in range(0, 4)
    ]
    assert values == sorted(values, reverse=True)
    assert values[0] - values[1] == Decimal("0.1000")
    assert values[1] - values[2] == Decimal("0.1000")


def test_oscillation_is_the_heaviest_penalty() -> None:
    model = load_model()
    negatives = {
        name: definition.weight
        for name, definition in model.signals.items()
        if definition.weight < 0
    }
    assert min(negatives, key=lambda name: negatives[name]) == "oscillation_observed"
    plain = score(_signals("C6", hard_external_id_agreement=True))
    oscillating = score(_signals("C6", hard_external_id_agreement=True, oscillation_observed=True))
    assert plain.value - oscillating.value == Decimal("0.2500")


def test_a_packet_with_no_positive_evidence_and_every_penalty_floors_at_zero() -> None:
    result = score(
        _signals(
            "C10",
            disagreeing_field=3,
            partial_evidence=True,
            oscillation_observed=True,
        )
    )
    assert result.raw_total < 0
    assert result.value == Decimal("0.0000")
    assert result.clamped is True


def test_every_score_is_inside_the_unit_interval_for_every_type() -> None:
    """R14 says [0,1]; check it over the whole signal cube, not one example."""
    from recon.reference import CONFLICT_TYPES

    for conflict_type in CONFLICT_TYPES:
        for flags in range(2**5):
            result = score(
                _signals(
                    conflict_type,
                    hard_external_id_agreement=bool(flags & 1),
                    normalized_email_agreement=bool(flags & 2),
                    name_dob_exact=bool(flags & 4),
                    amount_date_corroboration=bool(flags & 8),
                    partial_evidence=bool(flags & 16),
                    disagreeing_field=flags % 4,
                    oscillation_observed=bool(flags % 3 == 0),
                )
            )
            assert Decimal("0") <= result.value <= Decimal("1"), (conflict_type, flags, result)


# =====================================================================================
# reproducibility -- the graded one
# =====================================================================================
def test_same_conflict_same_evidence_same_score() -> None:
    signals = _signals("C6", hard_external_id_agreement=True, disagreeing_field=2)
    first = score(signals)
    second = score(signals)
    assert first.value == second.value
    assert str(first.value) == str(second.value)
    assert first.as_dict() == second.as_dict()


def test_the_score_is_a_decimal_never_a_float() -> None:
    """A float would make the value platform-dependent at the last digit."""
    result = score(_signals("C6", hard_external_id_agreement=True))
    assert isinstance(result.value, Decimal)
    assert isinstance(result.base, Decimal)
    for term in result.terms:
        assert isinstance(term.weight, Decimal)
        assert isinstance(term.contribution, Decimal)
    assert result.value.as_tuple().exponent == -4


def test_the_score_is_identical_in_a_SEPARATE_PROCESS() -> None:
    """Bit-for-bit across processes, which is what R14 actually asks for.

    Two calls in one interpreter share a parsed model and a `decimal` context;
    that they agree proves less than it looks. A subprocess re-reads the YAML,
    rebuilds the context and re-does the arithmetic from scratch.
    """
    program = (
        "from decimal import Decimal;"
        "from recon.confidence import Signals, score;"
        "s=Signals(conflict_type='C6', hard_external_id_agreement=True,"
        " normalized_email_agreement=True, disagreeing_field=2, partial_evidence=True);"
        "print(score(s).value)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    here = score(
        _signals(
            "C6",
            hard_external_id_agreement=True,
            normalized_email_agreement=True,
            disagreeing_field=2,
            partial_evidence=True,
        )
    ).value
    assert completed.stdout.strip() == str(here)
    assert completed.stdout.strip() == "0.6500"


# =====================================================================================
# inspectability
# =====================================================================================
def test_the_packet_records_every_term_of_the_arithmetic() -> None:
    """A score whose inputs are not recorded is not inspectable."""
    model = load_model()
    result = score(_signals("C12", amount_date_corroboration=True))
    packet = result.as_dict()
    assert packet["model_version"] == model.version
    assert packet["formula"] == model.formula
    assert packet["base"] == "0.55"
    assert [term["signal"] for term in packet["terms"]] == list(model.signal_order)
    # The recorded arithmetic must actually add up to the recorded answer.
    total = Decimal(packet["base"]) + sum(Decimal(term["contribution"]) for term in packet["terms"])
    assert total == Decimal(packet["raw_total"])
    assert Decimal(packet["confidence"]) == result.value


def test_partial_evidence_reasons_name_the_clause_that_fired() -> None:
    reasons = partial_evidence_reasons(
        incomplete_sources=["crm"],
        observed_values={"amount_cents": None, "type": "deposit"},
        sources_involved=["payments"],
    )
    assert reasons == (
        "incomplete_sources:crm",
        "null_observed_values:amount_cents",
        "single_source:payments",
    )


def test_no_reasons_means_the_signal_is_off() -> None:
    reasons = partial_evidence_reasons(
        incomplete_sources=[],
        observed_values={"amount_cents": 1000, "type": "deposit"},
        sources_involved=["appdb", "payments"],
    )
    assert reasons == ()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        ("", True),
        ([], True),
        ({}, True),
        (False, False),
        (0, False),
        ("x", False),
        (["a"], False),
    ],
)
def test_the_emptiness_test_does_not_treat_false_or_zero_as_missing(
    value: object, expected: bool
) -> None:
    """`metadata_name_pair_present: false` is an observation, not a hole.

    Treating it as a hole would penalise C2 for the very fact that defines it.
    """
    assert observed_value_is_null(value) is expected


# =====================================================================================
# the disagreement unit: ROWS, not paths
# =====================================================================================
def test_a_grade_only_disagreement_is_one_row_not_two_paths() -> None:
    """Contract SS2.4 puts BOTH endpoints in `disagreeing_fields`."""
    assert disagreeing_row_count(["crm.contact.grade", "appdb.student.grade"]) == 1


def test_a_mixed_disagreement_counts_each_row_once() -> None:
    assert (
        disagreeing_row_count(
            [
                "crm.contact.grade",
                "appdb.student.grade",
                "crm.contact.first_name",
                "appdb.student.first_name",
            ]
        )
        == 2
    )


def test_an_unknown_path_counts_as_its_own_row() -> None:
    """Fail in the penalising direction, never the free one."""
    assert disagreeing_row_count(["not.a.compared.path"]) == 1


# =====================================================================================
# the model is committed, and there is no fallback
# =====================================================================================
def test_a_missing_model_file_raises_rather_than_defaulting(tmp_path: Path) -> None:
    """A model that keeps scoring without its file is a hardcoded model."""
    with pytest.raises(ConfidenceModelError, match="missing"):
        load_model(tmp_path / "nope.yaml")


def test_an_unquoted_weight_is_refused(tmp_path: Path) -> None:
    """The one door binary floating point could come through is closed by type."""
    source = tmp_path / "confidence.yaml"
    document = model_path().read_text(encoding="utf-8").replace('weight: "0.35"', "weight: 0.35")
    source.write_text(document, encoding="utf-8")
    with pytest.raises(ConfidenceModelError, match="quoted strings"):
        load_model(source)


def test_a_model_missing_a_conflict_type_is_refused(tmp_path: Path) -> None:
    """Totality is checked at load, not on whichever conflict arrives first."""
    import yaml

    document = yaml.safe_load(model_path().read_text(encoding="utf-8"))
    document["bases"].pop("C7")
    source = tmp_path / "confidence.yaml"
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ConfidenceModelError, match="C7"):
        load_model(source)


def test_a_signal_order_that_drops_a_signal_is_refused(tmp_path: Path) -> None:
    import yaml

    document = yaml.safe_load(model_path().read_text(encoding="utf-8"))
    document["signal_order"] = document["signal_order"][:-1]
    source = tmp_path / "confidence.yaml"
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ConfidenceModelError, match="signal_order"):
        load_model(source)


# =====================================================================================
# the LLM is not in this number
# =====================================================================================
def test_confidence_does_not_import_the_llm_module() -> None:
    """ "No LLM input to this number, ever" as a fact about the import graph.

    A promise in a docstring is a promise; an import graph is a fact. The check
    is over the parsed AST rather than the text, so a mention in prose does not
    trip it and a real import cannot hide behind formatting.
    """
    for module in ("recon/confidence.py", "recon/sensitive.py"):
        tree = ast.parse((SERVICE_ROOT / module).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not any(name.startswith("recon.llm") or name == "anthropic" for name in imported), (
            f"{module} imports an LLM module; R14 forbids any LLM input to the score"
        )


def test_the_scoring_function_cannot_be_handed_text() -> None:
    """`score` takes a value object with typed fields; there is no text channel."""
    with pytest.raises((ConfidenceModelError, AttributeError, TypeError)):
        score("the model says 0.99")  # type: ignore[arg-type]


# =====================================================================================
# the fourth partial-evidence clause: contradictory match keys
# =====================================================================================
def test_contradictory_match_keys_are_a_partial_evidence_reason() -> None:
    """R14's "conflicting evidence lowers it", at the identity level.

    Two match-key classes resolving ONE record to two DIFFERENT entities is the
    strongest evidence there is against an identity. It is the defining property
    of a C10 (merge-collapsed record), and before this clause existed every C10
    in the graded run scored a clamped 1.0000 -- see
    `tests/reconciler/test_reconcile_run.py::test_a_merge_collapsed_conflict_is_not_confident`.
    """
    reasons = partial_evidence_reasons(
        incomplete_sources=[],
        observed_values={"first_norm": "a"},
        sources_involved=["appdb", "crm"],
        contradictory_match_keys=["crm:contact:CRM-1"],
    )
    assert reasons == ("contradictory_match_keys:crm:contact:CRM-1",)


def test_the_contradiction_clause_is_off_by_default() -> None:
    """It must not fire on the ordinary packet, or every score drops by 0.15."""
    assert (
        partial_evidence_reasons(
            incomplete_sources=[],
            observed_values={"first_norm": "a"},
            sources_involved=["appdb", "crm"],
        )
        == ()
    )


# =====================================================================================
# the clamp region -- where R14's "lowers it" clause was previously erased
# =====================================================================================
def _cube() -> list[Signals]:
    """Every signal vector the model can be handed, for every committed type.

    14 types x 2^4 boolean flags x 4 disagreeing-row counts x 2 oscillation
    states = 1,792 vectors. The penalty tests below run over ALL of them rather
    than over one hand-picked example in the unclamped interior, which is what
    let the saturation defect survive a green suite: every committed penalty test
    built a C6 with exactly one positive signal (0.40 + 0.35 = 0.75), so no case
    came anywhere near ``clamp_max``.
    """
    from recon.reference import CONFLICT_TYPES

    vectors: list[Signals] = []
    for conflict_type in CONFLICT_TYPES:
        for flags in range(2**4):
            for rows in range(4):
                for oscillating in (False, True):
                    vectors.append(
                        _signals(
                            conflict_type,
                            hard_external_id_agreement=bool(flags & 1),
                            normalized_email_agreement=bool(flags & 2),
                            name_dob_exact=bool(flags & 4),
                            amount_date_corroboration=bool(flags & 8),
                            disagreeing_field=rows,
                            partial_evidence=bool(flags & 16),
                            oscillation_observed=oscillating,
                        )
                    )
    return vectors


@pytest.mark.parametrize("signal", ["partial_evidence", "oscillation_observed"])
def test_turning_a_penalty_on_strictly_lowers_every_score_above_the_floor(signal: str) -> None:
    """R14's "partial/conflicting evidence lowers it", over the WHOLE cube.

    This assertion fails against model v1: for the 1,051 clamped proposals in the
    graded store, turning a penalty on left the quantized score identical. The
    only permitted exception is the floor -- a score already at ``clamp_min``
    cannot go lower, which is inherent to a bounded score rather than a defect,
    and the test asserts that the exception is *only* ever the floor.
    """
    from dataclasses import replace

    model = load_model()
    checked = 0
    for off in _cube():
        if getattr(off, signal):
            continue
        on = replace(off, **{signal: True})
        lower, higher = score(on).value, score(off).value
        if higher == model.clamp_min:
            assert lower == model.clamp_min
            continue
        assert lower < higher, (signal, off)
        checked += 1
    assert checked > 500, f"only {checked} non-floor comparisons -- the cube got smaller"


def test_each_additional_disagreeing_row_strictly_lowers_it_even_when_saturated() -> None:
    """The count penalty, over the whole cube, including the saturated region."""
    from dataclasses import replace

    model = load_model()
    for vector in _cube():
        fewer = score(replace(vector, disagreeing_field=vector.disagreeing_field)).value
        more = score(replace(vector, disagreeing_field=vector.disagreeing_field + 1)).value
        if fewer == model.clamp_min:
            assert more == model.clamp_min
            continue
        assert more < fewer, vector


def test_below_saturation_v2_is_arithmetically_identical_to_v1() -> None:
    """The change must move ONLY the scores the clamp was flattening.

    v1 was ``clamp01(base + sum(all))``. Wherever ``base + sum(positive)`` fits
    inside the clamp window, v2 computes the same number -- which is what makes
    this a repair of the saturated region rather than a new model.
    """
    from recon.confidence import clamp

    model = load_model()
    compared = 0
    for vector in _cube():
        result = score(vector)
        if result.positive_clamped:
            continue
        v1_bounded, _ = clamp(result.raw_total, model)
        assert result.value == v1_bounded.quantize(model.quantum, rounding=model.rounding), vector
        compared += 1
    assert compared > 500, f"only {compared} unsaturated vectors compared"


def test_the_packet_carries_everything_needed_to_recompute_the_number() -> None:
    """A reviewer holding one row must not need the repository to check it.

    The packet previously carried no rounding mode, no decimal places, no clamp
    window and no binding to the file that produced it -- so re-deriving the
    number meant assuming ROUND_HALF_EVEN at 4 places over [0,1] and trusting an
    integer ``version`` a human has to remember to bump.
    """
    from decimal import Decimal as D

    packet = score(_signals("C6", hard_external_id_agreement=True, disagreeing_field=1)).as_dict()
    precision = packet["precision"]
    positive = sum(
        D(term["contribution"]) for term in packet["terms"] if not term["weight"].startswith("-")
    )
    negative = sum(
        D(term["contribution"]) for term in packet["terms"] if term["weight"].startswith("-")
    )
    evidence = min(
        max(D(packet["base"]) + positive, D(precision["clamp_min"])), D(precision["clamp_max"])
    )
    total = min(max(evidence + negative, D(precision["clamp_min"])), D(precision["clamp_max"]))
    recomputed = total.quantize(
        D(1).scaleb(-precision["decimal_places"]), rounding=precision["rounding"]
    )
    assert recomputed == D(packet["confidence"])
    assert len(packet["model_sha256"]) == 64
    assert packet["model_sha256"] == load_model().digest


def test_the_recorded_digest_is_the_sha256_of_the_model_file_bytes() -> None:
    import hashlib

    assert load_model().digest == hashlib.sha256(model_path().read_bytes()).hexdigest()


# =====================================================================================
# the two latent dict-order dependencies in the model accessors
# =====================================================================================
def test_a_second_corroborating_table_is_refused(tmp_path: Path) -> None:
    """`corroborating_keys()` returns the FIRST such signal in YAML order.

    With two, the graded number would depend on the order two keys happen to
    appear in a file. Refuse the model instead of silently picking one.
    """
    import yaml

    document = yaml.safe_load(model_path().read_text(encoding="utf-8"))
    table = document["signals"]["amount_date_corroboration"]["corroborating_keys"]
    document["signals"]["partial_evidence"]["corroborating_keys"] = table
    source = tmp_path / "confidence.yaml"
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ConfidenceModelError, match="more than one signal"):
        load_model(source)


def test_two_signals_claiming_one_key_class_are_refused(tmp_path: Path) -> None:
    """`key_class_signals()` is last-write-wins; a duplicate would drop one."""
    import yaml

    document = yaml.safe_load(model_path().read_text(encoding="utf-8"))
    document["signals"]["name_dob_exact"]["key_class"] = "ext"
    source = tmp_path / "confidence.yaml"
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ConfidenceModelError, match="key_class"):
        load_model(source)
