"""The weight audit: ``confidence.yaml``'s numbers, asserted verbatim.

**This file is the reason the model counts as "committed".** R14 says the formula
must be committed and that a hardcoded constant is a failure. A YAML file alone
does not achieve that: a weight could be edited in a one-line commit and every
score in the system would move with nothing turning red. The literals below are
the second copy that makes the first one binding -- changing a weight without
changing this file is a red build, and changing both is a diff a reviewer sees.

The numbers are written as strings on both sides deliberately. The YAML holds
quoted strings so ``Decimal(str)`` parses them exactly; comparing against strings
here means this test would also catch a change that merely *unquoted* a value,
which is the change that would silently reintroduce binary floating point onto a
graded path.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from recon.confidence import load_model, model_path
from recon.normalize import KEY_CLASSES
from recon.reference import CONFLICT_TYPES, OBSERVED_VALUE_KEYS

# ---------------------------------------------------------------------------------
# THE COMMITTED MODEL. Editing confidence.yaml without editing this block is a
# red build; that is the entire point of the block.
# ---------------------------------------------------------------------------------
COMMITTED_VERSION = 1

COMMITTED_BASES = {
    "C1": "0.50",
    "C2": "0.35",
    "C3": "0.45",
    "C4": "0.50",
    "C5": "0.50",
    "C6": "0.40",
    "C7": "0.50",
    "C8": "0.45",
    "C9": "0.55",
    "C10": "0.30",
    "C11": "0.55",
    "C12": "0.55",
    "C13": "0.55",
    "C14": "0.40",
}

#: DESIGN pins these seven weights and their values; the file is where they live.
COMMITTED_WEIGHTS = {
    "hard_external_id_agreement": "0.35",
    "normalized_email_agreement": "0.25",
    "name_dob_exact": "0.20",
    "amount_date_corroboration": "0.10",
    "disagreeing_field": "-0.10",
    "partial_evidence": "-0.15",
    "oscillation_observed": "-0.25",
}

COMMITTED_SIGNAL_ORDER = (
    "hard_external_id_agreement",
    "normalized_email_agreement",
    "name_dob_exact",
    "amount_date_corroboration",
    "disagreeing_field",
    "partial_evidence",
    "oscillation_observed",
)

COMMITTED_KINDS = {
    "hard_external_id_agreement": "boolean",
    "normalized_email_agreement": "boolean",
    "name_dob_exact": "boolean",
    "amount_date_corroboration": "boolean",
    "disagreeing_field": "count",
    "partial_evidence": "boolean",
    "oscillation_observed": "boolean",
}

COMMITTED_KEY_CLASSES = {
    "hard_external_id_agreement": "ext",
    "normalized_email_agreement": "email",
    "name_dob_exact": "namedob",
}

COMMITTED_CORROBORATING_KEYS = {
    "C1": ("paid_payment_refs", "enrollment_ref"),
    "C2": (),
    "C3": (),
    "C4": (),
    "C5": (),
    "C6": (),
    "C7": (),
    "C8": (),
    "C9": (),
    "C10": (),
    "C11": ("amount_cents", "occurred_at_delta_seconds"),
    "C12": ("amount_cents", "expected_amount_cents"),
    "C13": ("refunded_at", "enrollment.updated_at"),
    "C14": (),
}


@pytest.fixture(scope="module")
def raw_document() -> dict:
    return yaml.safe_load(model_path().read_text(encoding="utf-8"))


def test_the_model_file_is_at_the_committed_path() -> None:
    """The repository root, next to `prices.yaml` -- the other committed table."""
    path = model_path()
    assert path.name == "confidence.yaml"
    assert path.is_file(), f"the committed confidence model is missing at {path}"
    assert (path.parent / "prices.yaml").is_file(), (
        "confidence.yaml is expected at the repository root beside prices.yaml; "
        f"resolved to {path.parent}"
    )


def test_model_version_is_the_committed_one() -> None:
    assert load_model().version == COMMITTED_VERSION


def test_every_base_is_the_committed_value(raw_document: dict) -> None:
    """One assertion per conflict type, on the literal string in the file."""
    bases = {key: entry["value"] for key, entry in raw_document["bases"].items()}
    assert bases == COMMITTED_BASES


def test_every_weight_is_the_committed_value(raw_document: dict) -> None:
    weights = {name: entry["weight"] for name, entry in raw_document["signals"].items()}
    assert weights == COMMITTED_WEIGHTS


def test_signal_order_is_the_committed_order(raw_document: dict) -> None:
    assert tuple(raw_document["signal_order"]) == COMMITTED_SIGNAL_ORDER


def test_signal_kinds_are_committed(raw_document: dict) -> None:
    kinds = {name: entry["kind"] for name, entry in raw_document["signals"].items()}
    assert kinds == COMMITTED_KINDS


def test_precision_is_committed(raw_document: dict) -> None:
    """Four places matches `proposals.confidence NUMERIC(5,4)` exactly.

    If the model quantized coarser or finer than the column, the stored value
    would be a database rounding of the computed one and "same conflict + same
    evidence => same score" would be true of the model and false of the record.
    """
    precision = raw_document["precision"]
    assert precision["decimal_places"] == 4
    assert precision["rounding"] == "ROUND_HALF_EVEN"
    assert precision["clamp_min"] == "0.0000"
    assert precision["clamp_max"] == "1.0000"


def test_every_numeric_literal_is_a_quoted_string() -> None:
    """No bare YAML float anywhere in the model.

    ``Decimal(0.35)`` is ``0.34999999999999997779553950749686919152736663818359375``.
    A single unquoted weight would put binary floating point on the graded path
    while every other test still passed, so the file is scanned as text.
    """
    text = model_path().read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"^\s*(value|weight|clamp_min|clamp_max):\s*-?\d", line)
    ]
    assert not offenders, f"unquoted numeric literals in confidence.yaml: {offenders}"


def test_identity_signals_name_real_match_key_classes(raw_document: dict) -> None:
    """The three identity signals must name `normalize.KEY_CLASSES` values.

    This is the seam between the model and the ER layer: the signal is read from
    ``entity_link_candidates.key_class``, whose vocabulary is
    ``normalize.match_keys``' three classes. A typo here -- ``external`` for
    ``ext`` -- would make the signal permanently 0 and every score quietly lower,
    with nothing else in the suite able to notice.
    """
    declared = {
        name: entry.get("key_class")
        for name, entry in raw_document["signals"].items()
        if entry.get("key_class")
    }
    assert declared == COMMITTED_KEY_CLASSES
    assert set(declared.values()) <= set(KEY_CLASSES)
    assert set(declared.values()) == set(KEY_CLASSES), (
        "all three committed match-key classes should carry a signal; "
        f"{sorted(set(KEY_CLASSES) - set(declared.values()))} has none"
    )


def test_corroborating_keys_are_the_committed_table(raw_document: dict) -> None:
    declared = raw_document["signals"]["amount_date_corroboration"]["corroborating_keys"]
    assert {key: tuple(value) for key, value in declared.items()} == COMMITTED_CORROBORATING_KEYS


def test_every_corroborating_key_is_pinned_for_its_type() -> None:
    """A corroborating key must be one contract SS5.4 actually pins for that type.

    Otherwise the signal reads a key no conflict of that type ever carries, and
    scores 0 forever -- a dead weight that looks live in the file.
    """
    for conflict_type, keys in COMMITTED_CORROBORATING_KEYS.items():
        pinned = OBSERVED_VALUE_KEYS[conflict_type]
        if not keys:
            continue
        assert pinned is not None, f"{conflict_type} has a dynamic key set; it cannot corroborate"
        missing = sorted(set(keys) - set(pinned))
        assert not missing, (
            f"{conflict_type} corroborating keys {missing} are not in SS5.4's pinned "
            f"key set {sorted(pinned)}"
        )


def test_bases_cover_exactly_the_committed_conflict_catalogue() -> None:
    assert set(COMMITTED_BASES) == set(CONFLICT_TYPES)
    model = load_model()
    assert set(model.bases) == set(CONFLICT_TYPES)


def test_every_base_and_weight_is_in_range() -> None:
    """A base outside [0,1] or a weight outside [-1,1] is a typo, not a policy."""
    model = load_model()
    for conflict_type, base in model.bases.items():
        assert Decimal("0") <= base <= Decimal("1"), f"{conflict_type} base {base} out of range"
    for name, definition in model.signals.items():
        assert Decimal("-1") <= definition.weight <= Decimal("1"), f"{name} weight out of range"


def test_every_signal_and_base_carries_a_written_derivation(raw_document: dict) -> None:
    """A number with no stated derivation is a hardcoded constant with a home.

    R14's requirement is that the signals be *inspectable*; a weight whose
    meaning is not written down cannot be inspected, only obeyed.
    """
    for name, entry in raw_document["signals"].items():
        assert entry.get("derivation", "").strip(), f"signal {name} has no derivation"
        assert len(entry["derivation"]) > 60, f"signal {name}'s derivation is too thin to check"
    for conflict_type, entry in raw_document["bases"].items():
        assert entry.get("note", "").strip(), f"base {conflict_type} has no note"


def test_the_model_is_not_importable_python() -> None:
    """The model is data, not code: no import of it can execute anything."""
    assert model_path().suffix == ".yaml"
    assert not Path(str(model_path()).replace(".yaml", ".py")).exists()
