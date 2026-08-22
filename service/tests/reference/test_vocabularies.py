"""Every committed vocabulary and its exact ORDER (contract SS2.3, SS3, SS4.1, SS5.8).

These tuples are not incidental collections: their **order** is committed and is read by
the generator and the detector alike, so a permutation is a silent divergence with no
structural assertion to catch it.

* `STAGE_FUNNEL_VALUES` is the enrollment funnel in funnel order; a permutation makes
  "later stage" mean something else.
* `IDENTITY_REF_CLASSES` is SS4.1's *source preference*, which decides `anchor_ref` and
  therefore every `person_key`.
* `SOURCE_IDS`, `PROGRAM_VALUES` and `PAYMENT_TYPES` index `A1_VOLUMES` and the fee
  schedule's declared domain.
* `ENUM_FIELDS` and `COMPARED_FIELD_PATHS` are `sorted(...)` of a **set**; drop the
  `sorted()` and the value becomes `PYTHONHASHSEED`-dependent, which is the one thing
  `.claude/CLAUDE.md` forbids outright on a graded path.

Every expected value below is restated literally from the contract. A test that reads
the constant it is checking proves nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import recon.normalize as normalize
import recon.reference as reference
from recon.normalize import ENUM_FIELDS, norm_enum
from recon.reference import (
    COMPARED_FIELD_PATHS,
    CONFLICT_TYPES,
    DEAL_STAGE_VALUES,
    FUNNEL_VALUES,
    GRADE_VALUES,
    IDENTITY_REF_CLASSES,
    KEY_CLASSES,
    LIFECYCLE_VALUES,
    PAYMENT_TYPES,
    PROGRAM_VALUES,
    REF_CLASSES,
    SOURCE_IDS,
    STAGE_FUNNEL_VALUES,
    STATE_VALUES,
    STATUS_VALUES,
    UNCHECKED_REASON_PRECEDENCE,
    VERDICTS,
    Money,
    make_ref,
)

# --------------------------------------------------------------------------------------
# SS5.8 -- the closed verdict vocabulary
# --------------------------------------------------------------------------------------


def test_verdicts_is_the_closed_three_member_vocabulary() -> None:
    """SS5.8: "Pinned `verdict` vocabulary, **closed**: `ok`, `conflict`, `unchecked`".

    A fourth member is the failure this pins: SS5.1 says a `None` operand is
    `unchecked` and **never** a disagreement, and SS5.3 says a rule skipped for an
    incomplete source emits `unchecked`, never a conflict. Both statements are only
    decidable because there is no third option to fall into -- a `degraded` or `skipped`
    verdict would let a row that should read `unchecked` read as something the harness
    does not count, and the false-negative category would quietly shrink.
    """
    assert VERDICTS == frozenset({"ok", "conflict", "unchecked"})  # noqa: SIM300 - committed constant on the left reads as the claim
    assert len(VERDICTS) == 3
    for member in ("ok", "conflict", "unchecked"):
        assert member in VERDICTS
    for non_member in ("degraded", "skipped", "error", "pending", "unknown", "OK", ""):
        assert non_member not in VERDICTS


def test_unchecked_reason_precedence_is_the_committed_order() -> None:
    """SS5.1: `missing_operand` > `unparseable_value` > `unmapped_enum`."""
    assert UNCHECKED_REASON_PRECEDENCE == (
        "missing_operand",
        "unparseable_value",
        "unmapped_enum",
    )


# --------------------------------------------------------------------------------------
# SS4.1 -- ref classes
# --------------------------------------------------------------------------------------


def test_ref_classes_are_the_five_committed_classes() -> None:
    """SS4.1: "A **source ref** is one of" -- exactly these five, no more and no fewer.

    Losing a class is not a cosmetic edit: `REF_CLASSES` is what `make_ref` validates
    against, so a missing class makes every ref of that class **unconstructible** and
    the conflict types that carry it (`crm:deal:` is named by no `entity_refs` row but
    `appdb:enrollment:` carries C7, C9 and C13) stop being emittable at all.
    """
    assert REF_CLASSES == (
        "crm:contact:",
        "crm:deal:",
        "appdb:student:",
        "appdb:enrollment:",
        "payments:payment:",
    )
    assert len(REF_CLASSES) == 5
    assert len(set(REF_CLASSES)) == 5


@pytest.mark.parametrize(
    ("source", "entity_type"),
    [
        ("crm", "contact"),
        ("crm", "deal"),
        ("appdb", "student"),
        ("appdb", "enrollment"),
        ("payments", "payment"),
    ],
)
def test_every_committed_ref_class_is_constructible(source: str, entity_type: str) -> None:
    """The behavioural half: each of the five classes really is buildable."""
    assert f"{source}:{entity_type}:" in REF_CLASSES
    assert make_ref(source, entity_type, "k1") == f"{source}:{entity_type}:k1"


def test_identity_ref_classes_are_the_committed_source_preference_order() -> None:
    """SS4.1: `appdb:student: > crm:contact: > payments:payment:`, and the ORDER is the
    preference -- `anchor_ref` reads it positionally, so permuting it re-keys every
    person (`person_key = uuid5(KEYSTONE_NS, anchor_ref)`) and splits every lineage."""
    assert IDENTITY_REF_CLASSES == ("appdb:student:", "crm:contact:", "payments:payment:")
    assert "crm:deal:" not in IDENTITY_REF_CLASSES
    assert "appdb:enrollment:" not in IDENTITY_REF_CLASSES
    assert set(IDENTITY_REF_CLASSES) < set(REF_CLASSES)


def test_source_ids_are_the_three_sources_in_committed_order() -> None:
    """SS3. Also the order `sources_involved` (SS8) reports in."""
    assert SOURCE_IDS == ("appdb", "crm", "payments")
    assert SOURCE_IDS == tuple(sorted(SOURCE_IDS))  # noqa: SIM300 - the committed constant on the left reads as the claim
    assert len(SOURCE_IDS) == 3


# --------------------------------------------------------------------------------------
# SS1.5 / SS2.3 -- value vocabularies
# --------------------------------------------------------------------------------------


def test_payment_types_are_the_committed_three() -> None:
    """SS1.5, and the second axis of the fee schedule's declared domain (SS2.3)."""
    assert PAYMENT_TYPES == ("fee", "deposit", "tuition")


def test_program_values_are_the_committed_four_in_order() -> None:
    """SS2.3: `Lower School | Middle School | Upper School | Summer Academy`.

    The order indexes `FEE_SCHEDULE`'s declared domain
    (`(program, kind) for program in PROGRAM_VALUES for kind in PAYMENT_TYPES`), and the
    same tuple drives `deal.pipeline` -- "the same four values", one table, never two.
    """
    assert PROGRAM_VALUES == (
        "Lower School",
        "Middle School",
        "Upper School",
        "Summer Academy",
    )
    # the committed order is NOT alphabetical
    assert PROGRAM_VALUES != tuple(sorted(PROGRAM_VALUES))  # noqa: SIM300 - constant-on-the-left reads as the claim


def test_stage_funnel_values_are_the_canonical_funnel_IN_FUNNEL_ORDER() -> None:
    """SS2.3 `enrollment.stage`: `prospect, applied, waitlisted, deposit_paid, enrolled,
    withdrawn, refunded`.

    This is the funnel, not a set: `prospect` precedes `applied` precedes `waitlisted`
    precedes `deposit_paid` precedes `enrolled`. `PAID_IMPLYING_STAGES` names the two
    that assert a payment happened, and both sit after `waitlisted`.
    """
    assert STAGE_FUNNEL_VALUES == (
        "prospect",
        "applied",
        "waitlisted",
        "deposit_paid",
        "enrolled",
        "withdrawn",
        "refunded",
    )
    assert FUNNEL_VALUES == STAGE_FUNNEL_VALUES
    assert STAGE_FUNNEL_VALUES != tuple(sorted(STAGE_FUNNEL_VALUES))  # noqa: SIM300 - the committed constant on the left reads as the claim
    order = {value: index for index, value in enumerate(STAGE_FUNNEL_VALUES)}
    assert order["prospect"] < order["applied"] < order["waitlisted"] < order["deposit_paid"]
    assert order["deposit_paid"] < order["enrolled"] < order["withdrawn"] < order["refunded"]


def test_deal_stage_values_are_the_committed_seven_in_order() -> None:
    """SS2.3 `DEAL_STAGE_TO_FUNNEL`'s domain, in the order the contract lists it -- the
    order that makes the map read as bijective onto the funnel."""
    assert DEAL_STAGE_VALUES == (
        "New Lead",
        "Application Submitted",
        "Waitlisted",
        "Deposit Received",
        "Closed Won",
        "Closed Lost",
        "Refunded",
    )


def test_status_values_are_the_committed_five_in_order() -> None:
    """SS2.3 `STATUS_TO_FUNNEL`'s domain."""
    assert STATUS_VALUES == ("prospect", "applied", "enrolled", "active", "withdrawn")


def test_lifecycle_values_are_the_committed_ten_and_case_significant() -> None:
    """SS2.3: `MQL` and `marketingqualifiedlead` are TWO committed entries, not one --
    the map is total over this exact domain and a key missing from it raises at import."""
    assert LIFECYCLE_VALUES == (
        "subscriber",
        "lead",
        "marketingqualifiedlead",
        "MQL",
        "salesqualifiedlead",
        "SQL",
        "opportunity",
        "customer",
        "evangelist",
        "other",
    )
    assert "MQL" in LIFECYCLE_VALUES
    assert "SQL" in LIFECYCLE_VALUES
    assert "mql" not in LIFECYCLE_VALUES


def test_grade_values_are_the_committed_fourteen_in_school_order() -> None:
    """SS2.3: `PK` first, then `K`, then 1..12 -- the order `GRADE_ORDER` ordinalizes."""
    assert GRADE_VALUES == (
        "PK",
        "K",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "10",
        "11",
        "12",
    )
    # string order is wrong here: "10" < "2" and "1" < "K"
    assert GRADE_VALUES != tuple(sorted(GRADE_VALUES))  # noqa: SIM300 - constant-on-the-left reads as the claim


def test_key_classes_are_the_committed_match_key_order() -> None:
    """SS2.1: `("ext", "email", "namedob")` -- the order `match_keys` emits them in."""
    assert KEY_CLASSES == ("ext", "email", "namedob")


def test_state_values_are_the_fifty_codes_in_committed_order() -> None:
    """SS2.3 ruling 12. The order is the committed one (roughly alphabetical by state
    NAME, not by code): `IA` follows `IN`, `MD` follows `ME`, `NV` precedes `NH`."""
    assert STATE_VALUES[:6] == ("AL", "AK", "AZ", "AR", "CA", "CO")
    assert STATE_VALUES[-3:] == ("WV", "WI", "WY")
    assert len(STATE_VALUES) == 50
    assert STATE_VALUES != tuple(sorted(STATE_VALUES))  # noqa: SIM300 - the committed constant on the left reads as the claim
    # ordered by state NAME: Indiana before Iowa, Maine before Maryland, Nevada before NH
    positions = {code: index for index, code in enumerate(STATE_VALUES)}
    assert positions["IN"] < positions["IA"]
    assert positions["ME"] < positions["MD"]
    assert positions["NV"] < positions["NH"]


# --------------------------------------------------------------------------------------
# The two `sorted(set(...))` derivations -- determinism, not aesthetics
# --------------------------------------------------------------------------------------


def test_enum_fields_is_every_accepted_field_SORTED() -> None:
    """`norm_enum`'s accepted field names. Derived from a dict, so without the
    `sorted()` the tuple would carry declaration order -- which is stable but is not what
    the error message (`expected one of {ENUM_FIELDS}`) or any consumer iterating it can
    rely on, and it is what the property tests sample from."""
    assert ENUM_FIELDS == (
        "deal_stage",
        "grade",
        "lifecycle_stage",
        "pipeline",
        "program",
        "stage",
        "state",
        "status",
    )
    assert ENUM_FIELDS == tuple(sorted(ENUM_FIELDS))  # noqa: SIM300 - the committed constant on the left reads as the claim
    assert len(ENUM_FIELDS) == 8
    for field in ENUM_FIELDS:
        assert norm_enum(field, None) is None  # every declared field is really accepted


def test_compared_field_paths_is_the_twelve_paths_SORTED() -> None:
    """SS2.4: every source-qualified path any comparison can name, sorted.

    It is built from a **set comprehension**. Without the `sorted()` the tuple's order
    is set-iteration order, i.e. `PYTHONHASHSEED`-dependent -- the exact class of
    non-determinism `.claude/CLAUDE.md` forbids on a graded path, and it feeds
    `validate_observed_values`' C6/C14 error message and every consumer that iterates it.
    """
    assert COMPARED_FIELD_PATHS == (
        "appdb.enrollment.stage",
        "appdb.student.dob",
        "appdb.student.first_name",
        "appdb.student.grade",
        "appdb.student.last_name",
        "appdb.student.status",
        "crm.contact.dob",
        "crm.contact.first_name",
        "crm.contact.grade",
        "crm.contact.last_name",
        "crm.contact.lifecycle_stage",
        "crm.deal.stage",
    )
    assert COMPARED_FIELD_PATHS == tuple(sorted(COMPARED_FIELD_PATHS))  # noqa: SIM300 - the committed constant on the left reads as the claim
    assert len(COMPARED_FIELD_PATHS) == 12


_ORDER_PROBE = """
import json
from recon.normalize import ENUM_FIELDS
from recon.reference import COMPARED_FIELD_PATHS, CONFLICT_TYPES, SOURCE_IDS
print(json.dumps({
    "ENUM_FIELDS": list(ENUM_FIELDS),
    "COMPARED_FIELD_PATHS": list(COMPARED_FIELD_PATHS),
    "CONFLICT_TYPES": list(CONFLICT_TYPES),
    "SOURCE_IDS": list(SOURCE_IDS),
}))
"""


def test_the_derived_orders_are_identical_across_hash_seeds(service_root: Path) -> None:
    """The determinism claim, measured rather than reasoned about.

    A same-process assertion cannot see a `PYTHONHASHSEED` dependency: the seed is fixed
    for the life of the interpreter. These are freshly spawned processes under four
    different seeds, which is the only way a `tuple(set(...))` shows itself.
    """
    observed = set()
    for seed in ("0", "1", "12345", "random"):
        result = subprocess.run(
            [sys.executable, "-c", _ORDER_PROBE],
            cwd=service_root,
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": seed},
            timeout=120,
        )
        observed.add(json.dumps(json.loads(result.stdout), sort_keys=True))
    assert len(observed) == 1, observed
    payload = json.loads(observed.pop())
    assert payload["ENUM_FIELDS"] == list(ENUM_FIELDS)
    assert payload["COMPARED_FIELD_PATHS"] == list(COMPARED_FIELD_PATHS)
    assert payload["CONFLICT_TYPES"] == list(CONFLICT_TYPES)
    assert payload["SOURCE_IDS"] == list(SOURCE_IDS)


# --------------------------------------------------------------------------------------
# SS2.5 -- `Money` is ORDERED
# --------------------------------------------------------------------------------------


def test_money_is_ordered_so_amounts_can_be_compared_and_sorted() -> None:
    """`Money` is `@dataclass(frozen=True, order=True)`. Without `order=True` every
    comparison raises `TypeError` at runtime instead of failing a test: C12 compares an
    `amount_cents` against the fee-schedule amount and C13 picks the person's **most
    recent** payment, and `canon_value`'s sequence case **sorts** its elements -- a
    `Money` inside a multi-valued `observed_values` entry would crash the fingerprint.
    """
    assert Money(1) < Money(2)
    assert Money(2) > Money(1)
    assert Money(1) <= Money(1)
    assert Money(1) >= Money(1)
    assert sorted([Money(30), Money(10), Money(20)]) == [Money(10), Money(20), Money(30)]
    assert min(Money(500), Money(50000)) == Money(500)
    assert max([Money(-1), Money(0)]) == Money(0)


def test_money_ordering_is_by_cents_and_equality_still_works() -> None:
    assert Money(10000) == Money(10000)
    assert Money(10000) != Money(10001)
    assert not Money(10000) < Money(10000)


# --------------------------------------------------------------------------------------
# The vocabularies are RE-EXPORTED, never restated (SS0 layering)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "GRADE_VALUES",
        "PROGRAM_VALUES",
        "STAGE_FUNNEL_VALUES",
        "DEAL_STAGE_VALUES",
        "STATUS_VALUES",
        "LIFECYCLE_VALUES",
        "STATE_VALUES",
        "KEY_CLASSES",
    ],
)
def test_reference_reexports_the_normalize_vocabulary_object_itself(name: str) -> None:
    """SS0: `recon.reference` imports `recon.normalize` and **re-exports** the
    vocabularies rather than restating them. Identity, not equality: two equal tuples
    would drift the moment one side is edited."""
    assert getattr(reference, name) is getattr(normalize, name)
    assert name in reference.__all__
    assert name in normalize.__all__
