"""The SS5.4 fingerprint -- one callable, no second code path.

The cross-process test is the one that matters: two freshly spawned subprocesses
under different `PYTHONHASHSEED` values must produce the identical digest for the
same logical conflict. A same-process double call proves nothing about set/dict
iteration order.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from recon.reference import (
    COMPARED_FIELD_PATHS,
    CONFLICT_TYPES,
    OBSERVED_VALUE_KEYS,
    Money,
    canon_value,
    fingerprint,
    validate_observed_values,
)

C2_VALUES = {
    "payer_email_norm": "nobody@corp.com",
    "external_ref": None,
    "metadata_name_pair_present": False,
}
C11_VALUES = {
    "payer_email_norm": "parent@corp.com",
    "amount_cents": 10000,
    "type": "fee",
    "occurred_at_delta_seconds": 42,
}


def test_fingerprint_is_a_sha256_hex_digest() -> None:
    digest = fingerprint("C2", ["payments:payment:pi_1"], (), C2_VALUES)
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_ref_and_key_order_cannot_change_the_digest() -> None:
    refs = ["payments:payment:pi_2", "payments:payment:pi_1"]
    shuffled_values = dict(reversed(list(C11_VALUES.items())))
    assert fingerprint("C11", refs, (), C11_VALUES) == fingerprint(
        "C11", list(reversed(refs)), (), shuffled_values
    )


def test_disagreeing_field_order_cannot_change_the_digest() -> None:
    paths = ["crm.contact.grade", "appdb.student.grade"]
    values = {"crm.contact.grade": "4", "appdb.student.grade": "5"}
    assert fingerprint("C6", ["appdb:student:s1"], paths, values) == fingerprint(
        "C6", ["appdb:student:s1"], sorted(paths, reverse=True), values
    )


@pytest.mark.parametrize(
    ("changed", "kwargs"),
    [
        ("type", {"conflict_type": "C12"}),
        ("refs", {"entity_refs": ["payments:payment:pi_9"]}),
        ("values", {"observed_values": {**C11_VALUES, "amount_cents": 10001}}),
    ],
)
def test_every_component_is_hashed(changed: str, kwargs: dict) -> None:
    base = {
        "conflict_type": "C11",
        "entity_refs": ["payments:payment:pi_1", "payments:payment:pi_2"],
        "disagreeing_fields": (),
        "observed_values": C11_VALUES,
    }
    if changed == "type":
        kwargs["observed_values"] = {
            "amount_cents": 10000,
            "expected_amount_cents": 50000,
            "program_norm": "Lower School",
            "type": "fee",
        }
    assert fingerprint(**{**base, **kwargs}) != fingerprint(**base)


def test_observed_values_go_through_canon_value() -> None:
    """No Python `repr`, no float, nothing hash-seed dependent enters the digest."""
    with pytest.raises(ValueError, match="float is FORBIDDEN"):
        fingerprint(
            "C11",
            ["payments:payment:pi_1", "payments:payment:pi_2"],
            (),
            {**C11_VALUES, "amount_cents": 100.0},
        )
    money = fingerprint(
        "C11",
        ["payments:payment:pi_1", "payments:payment:pi_2"],
        (),
        {**C11_VALUES, "amount_cents": Money(10000)},
    )
    assert money == fingerprint(
        "C11", ["payments:payment:pi_1", "payments:payment:pi_2"], (), C11_VALUES
    )
    assert canon_value(Money(10000)) == canon_value(10000)


def test_observed_values_key_set_is_pinned_per_type() -> None:
    """SS5.4: a key absent from a type's row may not be emitted; a key present is required."""
    with pytest.raises(ValueError, match="key set is pinned"):
        fingerprint("C2", ["payments:payment:pi_1"], (), {"payer_email_norm": "a@b.com"})
    with pytest.raises(ValueError, match="key set is pinned"):
        fingerprint("C2", ["payments:payment:pi_1"], (), {**C2_VALUES, "extra": 1})


def test_c6_and_c14_observed_values_must_be_compared_field_paths() -> None:
    ok = {"crm.contact.grade": "4", "appdb.student.grade": "5"}
    fingerprint("C6", ["appdb:student:s1"], sorted(ok), ok)
    with pytest.raises(ValueError, match="must be COMPARED_FIELDS paths"):
        fingerprint("C14", ["appdb:student:s1"], [], {"student.first_name": "a"})


@pytest.mark.parametrize("conflict_type", CONFLICT_TYPES)
def test_every_type_has_a_pinned_or_dynamic_key_row(conflict_type: str) -> None:
    expected = OBSERVED_VALUE_KEYS[conflict_type]
    if expected is None:
        assert conflict_type in {"C6", "C14"}
        return
    validate_observed_values(conflict_type, dict.fromkeys(expected, 1))
    with pytest.raises(ValueError):
        validate_observed_values(conflict_type, {})


def test_multi_valued_observed_values_are_serializable() -> None:
    """SS5.4 pins three multi-valued keys; the fingerprint must be defined for them."""
    digest = fingerprint(
        "C1",
        ["appdb:student:s1"],
        (),
        {
            "paid_payment_refs": ["payments:payment:pi_2", "payments:payment:pi_1"],
            "enrollment_ref": "appdb:enrollment:e1",
            "d2_deal_count": 0,
        },
    )
    assert digest == fingerprint(
        "C1",
        ["appdb:student:s1"],
        (),
        {
            "d2_deal_count": 0,
            "enrollment_ref": "appdb:enrollment:e1",
            "paid_payment_refs": ["payments:payment:pi_1", "payments:payment:pi_2"],
        },
    )


SUBPROCESS_SCRIPT = """
import json, sys
from recon.reference import fingerprint

payload = json.loads(sys.argv[1])
print(fingerprint(payload["type"], payload["entity_refs"],
                  payload["disagreeing_fields"], payload["observed_values"]))
"""

CASES = [
    {
        "type": "C6",
        "entity_refs": ["crm:contact:CRM-0000001", "appdb:student:s1"],
        "disagreeing_fields": ["crm.contact.grade", "appdb.student.grade"],
        "observed_values": {"crm.contact.grade": "4", "appdb.student.grade": "5"},
    },
    {
        "type": "C11",
        "entity_refs": ["payments:payment:pi_0000002", "payments:payment:pi_0000001"],
        "disagreeing_fields": [],
        "observed_values": C11_VALUES,
    },
    {
        "type": "C8",
        "entity_refs": ["appdb:student:s7"],
        "disagreeing_fields": [],
        "observed_values": {
            "household_key": "parent@corp.com",
            "dropped_source": "crm",
            "eligible_member_count": 3,
        },
    },
]


def _run(service_root: Path, case: dict, hash_seed: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", SUBPROCESS_SCRIPT, json.dumps(case)],
        cwd=service_root,
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": hash_seed},
        timeout=120,
    )
    return result.stdout.strip()


@pytest.mark.parametrize("case", CASES, ids=[case["type"] for case in CASES])
def test_fingerprint_is_equal_across_freshly_spawned_subprocesses(
    service_root: Path, case: dict
) -> None:
    """Two processes, different `PYTHONHASHSEED`s, same logical conflict."""
    digests = {_run(service_root, case, seed) for seed in ("0", "1", "12345", "random")}
    assert len(digests) == 1, digests

    in_process = fingerprint(
        case["type"], case["entity_refs"], case["disagreeing_fields"], case["observed_values"]
    )
    assert digests == {in_process}


def test_shuffled_input_in_a_subprocess_still_matches(service_root: Path) -> None:
    case = CASES[0]
    shuffled = {
        "type": case["type"],
        "entity_refs": list(reversed(case["entity_refs"])),
        "disagreeing_fields": list(reversed(case["disagreeing_fields"])),
        "observed_values": dict(reversed(list(case["observed_values"].items()))),
    }
    assert _run(service_root, case, "0") == _run(service_root, shuffled, "random")


def test_validate_observed_values_refuses_an_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown conflict type"):
        validate_observed_values("C99", {})


# =====================================================================================
# SS5.4 ruling 3 -- GOLDEN DIGEST LITERALS: the pinned wire format
# =====================================================================================
#
# This is the most load-bearing table in the suite. The fingerprint is the idempotency
# key for the entire proposal pipeline and the key R16's oscillation dedup runs on, and
# EVERY structural assertion above -- order independence, "every component is hashed",
# cross-process stability -- survives a change to the serialization itself. Five
# separate mutations pass all of them:
#
#   1. the "|" section separator changed to anything else
#   2. the "\x1f" intra-section joiner changed to anything else
#   3. the "{k}={v}" item form changed to "{k}:{v}" or to JSON
#   4. the type written lower-cased, or as its "R-0NN" rule id
#   5. sha256 swapped for sha512 or blake2
#
# A committed digest literal kills all five at once, and it is the only thing that lets
# someone re-derive the digest from SS5.4 alone and confirm they landed on the same
# bytes. If one of these literals changes, the wire format changed: that is a contract
# amendment, not a test fix.

GOLDEN_CASES: list[dict] = [
    {
        "type": "C1",
        "entity_refs": ["appdb:student:s-0001"],
        "disagreeing_fields": [],
        # a multi-valued key (SS5.4) -- exercises the canon_value sequence encoding
        "observed_values": {
            "paid_payment_refs": ["payments:payment:pi_0000002", "payments:payment:pi_0000001"],
            "enrollment_ref": "appdb:enrollment:e-0001",
            "d2_deal_count": 0,
        },
        "digest": "6f9b9b29b3981d70994ad8726e45e8e38c9245554744139efde60456ea1a0360",
    },
    {
        "type": "C2",
        "entity_refs": ["payments:payment:pi_0000900"],
        "disagreeing_fields": [],
        # a None value and a bool value -- exercises "\N" and "true"/"false"
        "observed_values": {
            "payer_email_norm": "nobody@corp.com",
            "external_ref": None,
            "metadata_name_pair_present": False,
        },
        "digest": "a812b69665e54b7a5ec0c22f735fe332dbda47c7fbb3b391b67a303ba5d1b0e4",
    },
    {
        "type": "C3",
        "entity_refs": ["crm:contact:CRM-0000002", "crm:contact:CRM-0000001"],
        "disagreeing_fields": [],
        "observed_values": {
            "email_norm": "parent@corp.com",
            "first_norm": "ana",
            "last_norm": "garcia",
            "dob_norm_a": "2010-04-05",
            "dob_norm_b": None,
        },
        "digest": "01b45c6d4448f8ae8e120f248abfb8abae9d465aaf737fade6be36873445a101",
    },
    {
        "type": "C4",
        "entity_refs": ["appdb:student:s-0002", "crm:contact:CRM-0000003"],
        "disagreeing_fields": [],
        "observed_values": {
            "contact_email_norm": "a.parent@corp.com",
            "student_guardian_email_norms": ["parent@corp.com", "other@corp.com"],
            "link_method": "L3",
        },
        "digest": "6c9743176633aa4873a0b4893e90bf6c28e16c1cdcdcbcac36b81cfc62cf6f89",
    },
    {
        "type": "C6",
        "entity_refs": ["appdb:student:s-0003", "crm:contact:CRM-0000004"],
        # the only types with a NON-EMPTY section 3
        "disagreeing_fields": ["crm.contact.grade", "appdb.student.grade"],
        "observed_values": {"crm.contact.grade": "4", "appdb.student.grade": "5"},
        "digest": "5c81403cb45f1f7af170e9cef13c4d03c399528cb82618f0b18b8955863c11cd",
    },
    {
        "type": "C8",
        "entity_refs": ["appdb:student:s7"],
        "disagreeing_fields": [],
        "observed_values": {
            "household_key": "parent@corp.com",
            "dropped_source": "crm",
            "eligible_member_count": 3,
        },
        "digest": "7d78522b6f139d81885a2da16e858d6495f402ac3a54658626c55edb62ecbd42",
    },
    {
        "type": "C11",
        "entity_refs": ["payments:payment:pi_0000002", "payments:payment:pi_0000001"],
        "disagreeing_fields": [],
        "observed_values": C11_VALUES,
        "digest": "fad370789abc0eff1b47cabf53224510a2dfb80d7953f98b5a83b02a4ce68288",
    },
    {
        "type": "C14",
        "entity_refs": ["appdb:student:s-0004", "crm:contact:CRM-0000005"],
        "disagreeing_fields": ["crm.contact.dob", "appdb.student.dob"],
        "observed_values": {"crm.contact.dob": "2010-04-05", "appdb.student.dob": "2011-06-07"},
        "digest": "08329f3b97d5a147539e2388339006584ec8000014b045119c2d11e3b646145a",
    },
]


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=[case["type"] for case in GOLDEN_CASES])
def test_golden_digest_literal(case: dict) -> None:
    """SS5.4 ruling 3: the committed digest for a representative conflict of each shape."""
    digest = fingerprint(
        case["type"], case["entity_refs"], case["disagreeing_fields"], case["observed_values"]
    )
    assert digest == case["digest"], (
        f"{case['type']} fingerprint moved. If the SS5.4 wire format changed on purpose, "
        "amend the contract and this literal together; otherwise a serialization detail "
        "regressed and every idempotency key in the pipeline moved with it."
    )
    assert len(digest) == 64  # sha256, not sha512


def test_the_golden_digests_are_the_payload_ss5_4_describes() -> None:
    """Re-derive one digest from SS5.4's prose ALONE -- no import of the serializer.

    This is what makes the table above checkable by a reader rather than merely
    self-consistent: someone with the contract and a sha256 implementation must land on
    the same 64 hex characters. Section 3 is present and EMPTY here, so the payload
    carries three separators, not two.
    """
    payload = (
        "C8"
        + "|"
        + "appdb:student:s7"
        + "|"
        + ""
        + "|"
        + "\x1f".join(
            ["dropped_source=crm", "eligible_member_count=3", "household_key=parent@corp.com"]
        )
    )
    assert payload.count("|") == 3
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert expected == "7d78522b6f139d81885a2da16e858d6495f402ac3a54658626c55edb62ecbd42"
    assert (
        fingerprint("C8", ["appdb:student:s7"], [], GOLDEN_CASES[5]["observed_values"]) == expected
    )


def test_the_digest_is_sha256_of_utf8_not_some_other_hash() -> None:
    """Mutation 5: swapping the hash keeps every structural property and breaks every
    committed key. Pinned by length AND by a direct comparison against sha512."""
    digest = fingerprint("C8", ["appdb:student:s7"], [], GOLDEN_CASES[5]["observed_values"])
    assert len(digest) == 64
    assert len(hashlib.sha512(b"anything").hexdigest()) == 128
    assert digest != hashlib.sha512(b"anything").hexdigest()[:64]


def test_the_conflict_type_enters_the_payload_verbatim() -> None:
    """Mutation 4: `type.lower()` or the `R-0NN` rule id. Both keep every structural
    property (the digest still changes with the type) and both move every key."""
    refs = ["appdb:student:s7"]
    values = GOLDEN_CASES[5]["observed_values"]
    digest = fingerprint("C8", refs, [], values)
    lowered = hashlib.sha256(
        (
            "c8|appdb:student:s7||"
            + "\x1f".join(
                ["dropped_source=crm", "eligible_member_count=3", "household_key=parent@corp.com"]
            )
        ).encode("utf-8")
    ).hexdigest()
    assert digest != lowered
    rule_id = hashlib.sha256(
        (
            "R-008|appdb:student:s7||"
            + "\x1f".join(
                ["dropped_source=crm", "eligible_member_count=3", "household_key=parent@corp.com"]
            )
        ).encode("utf-8")
    ).hexdigest()
    assert digest != rule_id


@pytest.mark.parametrize(
    ("section_separator", "item_joiner", "key_value_separator"),
    [
        ("\x1f", "\x1f", "="),  # mutation 1: section separator
        ("|", ",", "="),  # mutation 2: intra-section joiner
        ("|", "\x1f", ":"),  # mutation 3: k=v item form
        ("", "\x1f", "="),  # mutation 1, degenerate: no separator at all
    ],
)
def test_each_serialization_detail_changes_the_digest(
    section_separator: str, item_joiner: str, key_value_separator: str
) -> None:
    """Mutations 1-3, stated as the payloads they would produce. Every one of these is
    invisible to an order-independence or a "component is hashed" assertion."""
    items = [
        f"dropped_source{key_value_separator}crm",
        f"eligible_member_count{key_value_separator}3",
        f"household_key{key_value_separator}parent@corp.com",
    ]
    mutated_payload = section_separator.join(
        ("C8", "appdb:student:s7", "", item_joiner.join(items))
    )
    mutated = hashlib.sha256(mutated_payload.encode("utf-8")).hexdigest()
    assert mutated != fingerprint(
        "C8", ["appdb:student:s7"], [], GOLDEN_CASES[5]["observed_values"]
    )


def test_an_empty_section_is_the_empty_string_not_an_omitted_section() -> None:
    """SS5.4: four sections, three separators, always. A serializer that DROPS an empty
    section instead of emitting `""` produces a different payload for every conflict
    that carries no `disagreeing_fields` -- which is twelve of the fourteen types."""
    values = GOLDEN_CASES[5]["observed_values"]
    items = "\x1f".join(
        ["dropped_source=crm", "eligible_member_count=3", "household_key=parent@corp.com"]
    )
    dropped = hashlib.sha256(f"C8|appdb:student:s7|{items}".encode()).hexdigest()
    assert dropped != fingerprint("C8", ["appdb:student:s7"], [], values)


# =====================================================================================
# SS5.4 -- the pinned `observed_values` key set, restated from the contract table
# =====================================================================================
#
# The fingerprint HASHES this map, so the key set is the shape of the digest. Nothing
# above binds the committed keys themselves: `OBSERVED_VALUE_KEYS['C5']` can be renamed
# wholesale, and the dynamic C6/C14 row can be frozen into a fixed set, with every
# assertion so far still green -- and every fingerprint in the system moves with it.

COMMITTED_OBSERVED_VALUE_KEYS: dict[str, frozenset[str] | None] = {
    "C1": frozenset({"paid_payment_refs", "enrollment_ref", "d2_deal_count"}),
    "C2": frozenset({"payer_email_norm", "external_ref", "metadata_name_pair_present"}),
    "C3": frozenset({"email_norm", "first_norm", "last_norm", "dob_norm_a", "dob_norm_b"}),
    "C4": frozenset({"contact_email_norm", "student_guardian_email_norms", "link_method"}),
    "C5": frozenset({"status_funnel", "linked_contact_count", "attributed_payment_count"}),
    # SS5.4: "one entry per disagreeing comparison, keyed by the source-qualified path"
    "C6": None,
    "C7": frozenset(
        {"enrollment.stage_funnel", "enrollment.deposit_paid_at", "paid_deposit_payment_count"}
    ),
    "C8": frozenset({"household_key", "dropped_source", "eligible_member_count"}),
    "C9": frozenset({"enrollment.crm_deal_id", "deal_present_gen3", "deal_person_refs"}),
    "C10": frozenset(
        {"ext_resolved_ref", "namedob_resolved_ref", "first_norm", "last_norm", "dob_norm"}
    ),
    "C11": frozenset({"payer_email_norm", "amount_cents", "type", "occurred_at_delta_seconds"}),
    "C12": frozenset({"amount_cents", "expected_amount_cents", "program_norm", "type"}),
    "C13": frozenset(
        {"refunded_at", "enrollment.updated_at", "enrollment.stage_funnel", "student.status"}
    ),
    "C14": None,
}


def test_the_committed_observed_value_key_mapping_is_exact() -> None:
    """SS5.4's key table, verbatim -- all fourteen rows, every key spelled out.

    A renamed key is not a refactor. `observed_values` enters the payload as
    `f"{k}={canon_value(v)}"` with the key **verbatim**, so renaming one moves every
    fingerprint of that type; and because `validate_observed_values` enforces the row,
    the generator and the detector would agree with each other while disagreeing with
    every digest already committed to `golden/`.
    """
    assert dict(OBSERVED_VALUE_KEYS) == COMMITTED_OBSERVED_VALUE_KEYS
    assert set(OBSERVED_VALUE_KEYS) == set(CONFLICT_TYPES)


@pytest.mark.parametrize(
    ("conflict_type", "expected"),
    sorted(COMMITTED_OBSERVED_VALUE_KEYS.items(), key=lambda item: int(item[0][1:])),
    ids=lambda x: x if isinstance(x, str) else "",
)
def test_each_row_of_the_observed_values_table(
    conflict_type: str, expected: frozenset[str] | None
) -> None:
    assert OBSERVED_VALUE_KEYS[conflict_type] == expected
    if expected is not None:
        assert sorted(OBSERVED_VALUE_KEYS[conflict_type]) == sorted(expected)


def test_the_c6_and_c14_rows_are_DYNAMIC_not_a_frozen_key_set() -> None:
    """SS5.4: C6/C14 carry "one entry per **disagreeing** comparison, keyed by the
    source-qualified path" -- both endpoints of every disagreeing row, and only those.

    Freezing the row into a fixed key set (say all twelve `COMPARED_FIELD_PATHS`) keeps
    `validate_observed_values` looking sane and passes every other assertion in this
    file, but it makes the common case -- a grade-only C6, two keys -- **invalid**, and
    the 500 C6 / 50 C14 entries stop being emittable at all.
    """
    assert OBSERVED_VALUE_KEYS["C6"] is None
    assert OBSERVED_VALUE_KEYS["C14"] is None

    for conflict_type in ("C6", "C14"):
        # a two-key map (one disagreeing row) is valid...
        validate_observed_values(
            conflict_type, {"crm.contact.grade": "4", "appdb.student.grade": "5"}
        )
        # ...and so is a four-key map (a mixed set), and so is the full twelve
        validate_observed_values(
            conflict_type,
            {
                "crm.contact.grade": "4",
                "appdb.student.grade": "5",
                "crm.contact.first_name": "ana",
                "appdb.student.first_name": "anna",
            },
        )
        validate_observed_values(conflict_type, dict.fromkeys(COMPARED_FIELD_PATHS, "x"))
        # ...and the empty map is accepted by the validator (the PREDICATE, not the key
        # row, is what forbids an empty disagreeing set -- SS5.5's C14 clause)
        validate_observed_values(conflict_type, {})
        # ...but a key that is not a COMPARED_FIELDS path never is
        with pytest.raises(ValueError, match="must be COMPARED_FIELDS paths"):
            validate_observed_values(conflict_type, {"student.first_name": "a"})


def test_a_key_set_pinned_type_rejects_a_dynamic_path_key() -> None:
    """The mirror of the above: the twelve non-dynamic rows are CLOSED, so a
    source-qualified path is not a legal key for them."""
    with pytest.raises(ValueError, match="key set is pinned"):
        validate_observed_values("C8", {"crm.contact.grade": "4"})


# =====================================================================================
# SS5.4 -- sections 2 and 3 are ESCAPED, so the payload is INJECTIVE
# =====================================================================================
#
# `canon_value` is injective, but sections 2 and 3 used to embed their elements VERBATIM
# between `\x1f` joiners -- exactly the defect SS2.5 spells out for sequences. One ref
# carrying the joiner and two separate refs produced the same digest, and the digest is
# the dedup key the whole proposal pipeline rests on.

C8_VALUES = {
    "household_key": "parent@corp.com",
    "dropped_source": "crm",
    "eligible_member_count": 3,
}
C6_GRADE_VALUES = {"crm.contact.grade": "4", "appdb.student.grade": "5"}


def test_one_ref_carrying_the_joiner_is_not_the_same_digest_as_two_refs() -> None:
    """The original hole, stated as the collision it produced.

    `fingerprint("C8", ["appdb:student:a\\x1fappdb:student:b"], ...)` and
    `fingerprint("C8", ["appdb:student:a", "appdb:student:b"], ...)` are two different
    conflicts -- one dropped child versus two -- on two different populations. Sharing a
    fingerprint means R16's oscillation dedup silently swallows the second proposal.
    """
    joined = fingerprint("C8", ["appdb:student:a\x1fappdb:student:b"], [], C8_VALUES)
    two_refs = fingerprint("C8", ["appdb:student:a", "appdb:student:b"], [], C8_VALUES)
    assert joined != two_refs


def test_one_disagreeing_path_carrying_the_joiner_is_not_the_same_as_two_paths() -> None:
    """Section 3 has the identical hole and the identical fix."""
    joined = fingerprint(
        "C6",
        ["appdb:student:s1"],
        ["crm.contact.grade\x1fappdb.student.grade"],
        C6_GRADE_VALUES,
    )
    two_paths = fingerprint(
        "C6", ["appdb:student:s1"], ["crm.contact.grade", "appdb.student.grade"], C6_GRADE_VALUES
    )
    assert joined != two_paths


def test_a_backslash_in_a_ref_cannot_forge_an_escape_sequence() -> None:
    """The backslash pass runs FIRST (SS2.5), so a ref that literally contains the four
    characters `\\x1f` is distinguishable from one containing the control character."""
    literal_text = fingerprint("C8", ["appdb:student:a\\x1fb"], [], C8_VALUES)
    control_char = fingerprint("C8", ["appdb:student:a\x1fb"], [], C8_VALUES)
    assert literal_text != control_char


def test_ordinary_refs_and_paths_are_unchanged_by_the_escaping() -> None:
    """No committed ref or `COMPARED_FIELDS` path contains a backslash, `\\x1f` or
    `\\x1e`, so the escaping is the identity on every value the contract can produce --
    which is why none of the committed digest literals above moved when it was added."""
    for value in [
        "appdb:student:s7",
        "crm:contact:CRM-0000001",
        "payments:payment:pi_0000001",
        "appdb:enrollment:e-0001",
        "crm.contact.lifecycle_stage",
        "appdb.enrollment.stage",
    ]:
        assert canon_value(value) == value


ESCAPED_GOLDEN_CASES: list[dict] = [
    {
        "id": "ref-with-US",
        "type": "C8",
        "entity_refs": ["appdb:student:a\x1fappdb:student:b"],
        "disagreeing_fields": [],
        "observed_values": C8_VALUES,
        "digest": "f5e8f509b023f966cef0a772fac30aed8f73cd1c7c9f9951478a04a6204ff3a6",
    },
    {
        "id": "two-refs",
        "type": "C8",
        "entity_refs": ["appdb:student:a", "appdb:student:b"],
        "disagreeing_fields": [],
        "observed_values": C8_VALUES,
        "digest": "f9cdf90a0aa708ad753c9c9c34b0dd6c47d39556118db7e63c52021fba21ed02",
    },
    {
        "id": "path-with-US",
        "type": "C6",
        "entity_refs": ["appdb:student:s1"],
        "disagreeing_fields": ["crm.contact.grade\x1fappdb.student.grade"],
        "observed_values": C6_GRADE_VALUES,
        "digest": "c626bb8b2e4292ea88f563f90a8ccba6e26c9038209832a5a2a7ffdcb1682c47",
    },
    {
        "id": "two-paths",
        "type": "C6",
        "entity_refs": ["appdb:student:s1"],
        "disagreeing_fields": ["crm.contact.grade", "appdb.student.grade"],
        "observed_values": C6_GRADE_VALUES,
        "digest": "937cb397242efc8d28e0610ddd2a647cfeae332afde495ef41a8f0ed7af3dda1",
    },
]


@pytest.mark.parametrize(
    "case", ESCAPED_GOLDEN_CASES, ids=[case["id"] for case in ESCAPED_GOLDEN_CASES]
)
def test_escaped_golden_digest_literal(case: dict) -> None:
    """SS5.4 ruling 3, extended to the escaped sections: the committed bytes.

    Without these four literals the escaping is bound only by an inequality, which a
    "sort differently" or "join differently" change would also satisfy. With them, the
    exact payload is pinned.
    """
    assert (
        fingerprint(
            case["type"], case["entity_refs"], case["disagreeing_fields"], case["observed_values"]
        )
        == case["digest"]
    )


def test_the_escaped_payload_is_the_one_ss5_4_describes() -> None:
    """Re-derived from the contract's prose alone: each element is `canon_value`-escaped,
    the escaped encodings are sorted, then joined with `\\x1f`."""
    escaped_ref = "appdb:student:a\\x1fappdb:student:b"  # the four TEXT characters
    payload = (
        "C8"
        + "|"
        + escaped_ref
        + "|"
        + ""
        + "|"
        + "\x1f".join(
            ["dropped_source=crm", "eligible_member_count=3", "household_key=parent@corp.com"]
        )
    )
    assert "\x1f" not in escaped_ref
    assert payload.count("\x1f") == 2  # the two section-4 joiners ONLY
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert expected == ESCAPED_GOLDEN_CASES[0]["digest"]
    assert fingerprint("C8", ["appdb:student:a\x1fappdb:student:b"], [], C8_VALUES) == expected
