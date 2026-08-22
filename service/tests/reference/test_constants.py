"""SS2.2 committed constants, SS2.3 fee schedule, SS6 field classification.

These are pinned *values*, so the tests restate them literally from the contract
rather than recomputing them from the module -- a test that reads the constant it
is checking proves nothing.
"""

from __future__ import annotations

import uuid

import pytest

from recon.reference import (
    A1_VOLUMES,
    AUTO_APPLY_ELIGIBLE,
    C11_PLANT_MAX_SECONDS,
    C11_WINDOW_SECONDS,
    CONFLICT_MINIMUMS,
    CONFLICT_TYPES,
    ENROLLMENT_GRADE_FLOOR,
    FEE_SCHEDULE,
    FIX_TARGETS,
    GRADE_ORDER,
    KEYSTONE_NS,
    LEGIT_REPEAT_MIN_SECONDS,
    MAX_PAYLOAD_BYTES,
    NAME_CORPUS_MIN,
    PAID_IMPLYING_STAGES,
    RULE_ID_BY_TYPE,
    SENSITIVE_FIELDS,
    fee_amount_cents,
    fix_target,
    is_auto_apply_eligible,
    is_sensitive,
)


def test_keystone_ns_is_a_committed_literal() -> None:
    """Every uuid5 in the system hangs off this namespace; it may never move."""
    assert isinstance(KEYSTONE_NS, uuid.UUID)
    assert str(KEYSTONE_NS) == "17733ea0-28dd-5aeb-a266-c62b3689def8"
    assert KEYSTONE_NS.version == 5


def test_pinned_scalar_constants() -> None:
    assert MAX_PAYLOAD_BYTES == 262144
    assert MAX_PAYLOAD_BYTES == 256 * 1024
    assert PAID_IMPLYING_STAGES == frozenset({"deposit_paid", "enrolled"})  # noqa: SIM300 - committed constant on the left reads as the claim
    assert ENROLLMENT_GRADE_FLOOR == "K"
    assert GRADE_ORDER[ENROLLMENT_GRADE_FLOOR] == 0
    assert C11_WINDOW_SECONDS == 600
    assert C11_PLANT_MAX_SECONDS == 300
    assert LEGIT_REPEAT_MIN_SECONDS == 1200
    assert NAME_CORPUS_MIN == (2000, 1000)


def test_c11_guard_band_leaves_no_overlap() -> None:
    """`G7`: planted pairs <=300s apart, legitimate repeats >=1200s apart."""
    assert C11_PLANT_MAX_SECONDS < C11_WINDOW_SECONDS < LEGIT_REPEAT_MIN_SECONDS


@pytest.mark.parametrize(
    ("program", "kind", "cents"),
    [
        ("Lower School", "fee", 10000),
        ("Middle School", "fee", 10000),
        ("Upper School", "fee", 10000),
        ("Summer Academy", "fee", 10000),
        ("Lower School", "deposit", 50000),
        ("Middle School", "deposit", 60000),
        ("Upper School", "deposit", 75000),
        ("Summer Academy", "deposit", 25000),
        ("Lower School", "tuition", 1200000),
        ("Middle School", "tuition", 1400000),
        ("Upper School", "tuition", 1600000),
        ("Summer Academy", "tuition", 300000),
    ],
)
def test_fee_schedule_cell(program: str, kind: str, cents: int) -> None:
    assert FEE_SCHEDULE[(program, kind)] == cents
    assert fee_amount_cents(program, kind) == cents


def test_fee_schedule_accepts_dirty_program_values() -> None:
    assert fee_amount_cents("  upper_school ", "deposit") == 75000
    assert fee_amount_cents("SUMMER-ACADEMY", "tuition") == 300000


def test_fee_schedule_is_none_for_an_unresolvable_pair() -> None:
    """C12 turns this into `unchecked`, never a conflict (SS4.4, SS5.5)."""
    assert fee_amount_cents(None, "fee") is None
    assert fee_amount_cents("Not A Program", "fee") is None
    assert fee_amount_cents("Lower School", "late_fee") is None


def test_fee_schedule_is_total_over_program_x_type() -> None:
    assert len(FEE_SCHEDULE) == 12


def test_a1_volumes_are_the_five_pinned_numbers() -> None:
    assert A1_VOLUMES == {
        ("crm", "contact"): 40000,
        ("crm", "deal"): 15000,
        ("appdb", "student"): 25000,
        ("appdb", "enrollment"): 22000,
        ("payments", "payment"): 18000,
    }
    assert sum(A1_VOLUMES.values()) == 120000


def test_all_fourteen_a4_minimums() -> None:
    assert CONFLICT_MINIMUMS == {
        "C1": 500,
        "C2": 200,
        "C3": 300,
        "C4": 250,
        "C5": 400,
        "C6": 500,
        "C7": 300,
        "C8": 150,
        "C9": 100,
        "C10": 50,
        "C11": 50,
        "C12": 100,
        "C13": 100,
        "C14": 50,
    }
    assert sum(CONFLICT_MINIMUMS.values()) == 3050


def test_conflict_types_and_rule_ids() -> None:
    assert CONFLICT_TYPES == tuple(f"C{n}" for n in range(1, 15))  # noqa: SIM300 - committed constant on the left reads as the claim
    assert RULE_ID_BY_TYPE["C1"] == "R-001"
    assert RULE_ID_BY_TYPE["C10"] == "R-010"
    assert RULE_ID_BY_TYPE["C14"] == "R-014"
    assert set(RULE_ID_BY_TYPE) == set(CONFLICT_TYPES)


def test_sensitive_fields_is_the_committed_list_verbatim() -> None:
    assert SENSITIVE_FIELDS == frozenset(  # noqa: SIM300 - committed constant on the left reads as the claim
        {
            "crm.contact.first_name",
            "crm.contact.last_name",
            "crm.contact.dob",
            "appdb.student.first_name",
            "appdb.student.last_name",
            "appdb.student.dob",
            "appdb.student.student_number",
            "payments.payment.payer_email",
            "payments.payment.payer_name",
            "appdb.enrollment.billing_owner_email",
            "crm.contact.email",
            "appdb.student.guardian_email",
            "appdb.student.guardian2_email",
            "appdb.enrollment.stage",
            "appdb.enrollment.deposit_paid_at",
            "appdb.student.status",
            "payments.payment.status",
            "crm.deal.stage",
            "crm.contact.marketing_consent",
            "appdb.student.communication_opt_out",
        }
    )
    assert len(SENSITIVE_FIELDS) == 20


def test_auto_apply_eligible_is_an_allowlist_not_a_complement() -> None:
    assert AUTO_APPLY_ELIGIBLE == frozenset(  # noqa: SIM300 - committed constant on the left reads as the claim
        {
            "appdb.enrollment.crm_deal_id",
            "payments.payment.external_ref",
            "crm.contact.external_id",
            "crm.contact.grade",
            "crm.contact.lifecycle_stage",
        }
    )
    assert not SENSITIVE_FIELDS & AUTO_APPLY_ELIGIBLE
    # A path in neither set is not auto-applyable (SS6).
    assert not is_auto_apply_eligible("crm.deal.pipeline")
    assert not is_auto_apply_eligible("crm.contact.state")
    assert not is_sensitive("crm.deal.pipeline")


@pytest.mark.parametrize(
    "path",
    ["crm.deal.stage", "appdb.enrollment.deposit_paid_at", "appdb.student.guardian2_email"],
)
def test_flagged_divergences_are_classified_sensitive(path: str) -> None:
    """SS12 D-7 / D-8: over-classifying is safe, under-classifying is a graded failure."""
    assert is_sensitive(path)
    assert not is_auto_apply_eligible(path)


def test_fix_target_table_is_total_and_matches_the_committed_rows() -> None:
    assert set(FIX_TARGETS) == set(CONFLICT_TYPES)
    assert FIX_TARGETS["C2"].field_path == "payments.payment.external_ref"
    assert FIX_TARGETS["C2"].classification == "eligible"
    assert FIX_TARGETS["C9"].field_path == "appdb.enrollment.crm_deal_id"
    assert FIX_TARGETS["C9"].classification == "eligible"
    for evidence_only in ("C1", "C3", "C5", "C7", "C8", "C10", "C11", "C12", "C13"):
        assert FIX_TARGETS[evidence_only].field_path is None
        assert FIX_TARGETS[evidence_only].classification == "escalated"


def test_c4_writes_the_disagreeing_email_and_can_never_escape_the_classifier() -> None:
    """SS6 / SS12 D-7: all 250 C4 proposals are `sensitive_hold`, by construction."""
    target = fix_target("C4")
    assert target.field_path == "crm.contact.email"
    assert target.classification == "sensitive_hold"
    assert fix_target("C4", ["crm.contact.external_id"]).field_path == "crm.contact.email"


def test_c6_targets_by_disagreeing_path_set() -> None:
    grade_only = fix_target("C6", ["crm.contact.grade", "appdb.student.grade"])
    assert grade_only.field_path == "crm.contact.grade"
    assert grade_only.classification == "eligible"

    lifecycle_only = fix_target("C6", ["crm.contact.lifecycle_stage", "appdb.student.status"])
    assert lifecycle_only.field_path == "crm.contact.lifecycle_stage"
    assert lifecycle_only.classification == "eligible"

    mixed = fix_target(
        "C6",
        [
            "crm.contact.grade",
            "appdb.student.grade",
            "crm.contact.first_name",
            "appdb.student.first_name",
        ],
    )
    assert mixed.classification == "sensitive_hold"
    assert is_sensitive(mixed.field_path or "")


def test_every_c14_is_sensitive_hold() -> None:
    target = fix_target("C14", ["crm.contact.dob", "appdb.student.dob"])
    assert target.classification == "sensitive_hold"
    assert target.field_path in {"crm.contact.dob", "appdb.student.dob"}
    assert fix_target("C14").classification == "sensitive_hold"


def test_fix_target_rejects_an_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown conflict type"):
        fix_target("C99")


def test_c6_falls_back_to_the_committed_row_when_no_path_is_actionable() -> None:
    """A C6 with no disagreeing path is not constructible (SS5.5), but the table row
    must still be defined -- the classifier is a pure function, never a guess."""
    assert fix_target("C6").field_path == "crm.contact.grade"
    assert fix_target("C6", ["appdb.student.grade"]).field_path == "crm.contact.grade"


# =====================================================================================
# SS2.2 ruling 1 -- KEYSTONE_NS is a committed literal AND its derivation is recorded
# =====================================================================================


def test_keystone_ns_matches_its_recorded_derivation() -> None:
    """SS2.2 ruling 1. The literal is what the code uses; this asserts the provenance
    note in the contract still reproduces it, so the note can never rot into a false
    claim about where the namespace came from.

    It is a CONSTANT and not an expression because it determines every `person_key`
    (SS4.1) and every `appdb.student.id` (SS1.3): were it re-derived, the seed string
    would be the real constant, and a `v2`->`v3` rename or a stray space in either the
    generator or the detector would silently re-key the whole dataset -- every student
    PK, every `person_key`, every `field_lineage` row, and with them R16's dedup.
    """
    assert uuid.uuid5(uuid.NAMESPACE_DNS, "keystone.invariant-contract.v2") == KEYSTONE_NS
    assert str(KEYSTONE_NS) == "17733ea0-28dd-5aeb-a266-c62b3689def8"
    # A near-miss seed must NOT reproduce it -- that is why the literal is committed.
    for near_miss in (
        "keystone.invariant-contract.v3",
        "keystone.invariant-contract.v2 ",
        "keystone.invariant_contract.v2",
    ):
        assert uuid.uuid5(uuid.NAMESPACE_DNS, near_miss) != KEYSTONE_NS


# =====================================================================================
# SS6 -- `is_sensitive` is EXACT SET MEMBERSHIP, never a prefix test
# =====================================================================================


@pytest.mark.parametrize(
    "path",
    [
        # a longer path that has a sensitive path as a PREFIX
        "crm.contact.first_name_suffix",
        "crm.contact.first_name.middle",
        "appdb.student.dob_verified",
        "appdb.student.status_reason",
        "crm.deal.stage_history",
        "payments.payment.status_code",
        # a shorter path that IS a prefix of a sensitive path
        "crm.contact.",
        "crm.contact",
        "appdb.student",
        "appdb",
        "",
        # near misses that share no prefix relationship
        "crm.contact.firstname",
        "CRM.CONTACT.FIRST_NAME",
    ],
)
def test_is_sensitive_is_exact_membership_not_prefix_matching(path: str) -> None:
    """SS6: `SENSITIVE_FIELDS` "**is** the whole classifier", so a path is sensitive iff
    it is literally in the set.

    A forward prefix test (`path.startswith(sensitive)`) would classify
    `crm.contact.first_name_suffix`; a reverse one (`sensitive.startswith(path)`) would
    classify the entire `crm.contact.` namespace. Either way the classifier stops being
    decidable from the committed list and starts depending on how a future field happens
    to be named -- and SS6 is the thing that has to be decidable, because it is what
    forces all 250 C4 proposals onto `sensitive_hold` (SS12 D-7).
    """
    assert path not in SENSITIVE_FIELDS
    assert not is_sensitive(path)


def test_is_sensitive_is_true_for_every_committed_path_and_only_those() -> None:
    for path in SENSITIVE_FIELDS:
        assert is_sensitive(path)
    assert sum(is_sensitive(p) for p in SENSITIVE_FIELDS) == 20


# =====================================================================================
# SS6 ruling 8 -- C6/C14 fix templates write the CRM side
# =====================================================================================

# Byte order puts `appdb.*` before `crm.*` on EVERY wholly-sensitive comparison row, so
# a plain `sorted(sensitive_paths)[0]` silently always selects the app-DB side.
_WHOLLY_SENSITIVE_ROWS: list[tuple[str, str, str]] = [
    ("name_first", "crm.contact.first_name", "appdb.student.first_name"),
    ("name_last", "crm.contact.last_name", "appdb.student.last_name"),
    ("dob", "crm.contact.dob", "appdb.student.dob"),
    ("stage", "crm.deal.stage", "appdb.enrollment.stage"),
]


@pytest.mark.parametrize(
    ("logical", "crm_path", "appdb_path"), _WHOLLY_SENSITIVE_ROWS, ids=lambda x: x
)
def test_c14_writes_the_crm_side_of_the_disagreeing_row(
    logical: str, crm_path: str, appdb_path: str
) -> None:
    """SS6 ruling 8, resolving MINOR-8. Every other committed template writes the CRM
    record (`crm.contact.grade`, `crm.contact.lifecycle_stage` "CRM side only",
    `crm.contact.email` for C4), and SS4.6 survivorship is `app DB > CRM > payments` for
    identity fields -- so writing the app-DB endpoint proposes overwriting the
    authoritative record with the less authoritative one.
    """
    target = fix_target("C14", [appdb_path, crm_path])
    assert target.field_path == crm_path
    assert target.field_path != appdb_path
    assert target.field_path != sorted([appdb_path, crm_path])[0]  # the old selector
    assert target.classification == "sensitive_hold"


@pytest.mark.parametrize(
    ("logical", "crm_path", "appdb_path"), _WHOLLY_SENSITIVE_ROWS, ids=lambda x: x
)
def test_a_mixed_c6_writes_the_crm_side_of_its_sensitive_row(
    logical: str, crm_path: str, appdb_path: str
) -> None:
    """SS6 ruling 8: the sensitive half of a MIXED set decides both the classification
    and the target, and the target is the CRM endpoint of that row."""
    mixed = [appdb_path, crm_path, "crm.contact.grade", "appdb.student.grade"]
    target = fix_target("C6", mixed)
    assert target.field_path == crm_path
    assert target.classification == "sensitive_hold"
    # ...and the grade half must NOT win: a mixed C6 is never auto-appliable.
    assert target.field_path != "crm.contact.grade"


def test_the_committed_c14_default_row_is_the_crm_side() -> None:
    """The table row a C14 falls back to when no paths are supplied follows the same
    convention -- a default that contradicted ruling 8 would reintroduce it."""
    assert FIX_TARGETS["C14"].field_path == "crm.contact.first_name"
    assert FIX_TARGETS["C14"].classification == "sensitive_hold"
    assert fix_target("C14").field_path == "crm.contact.first_name"


def test_every_c6_and_c14_target_is_a_crm_path() -> None:
    """The invariant behind ruling 8, stated once over every reachable disagreeing set."""
    for _logical, crm_path, appdb_path in _WHOLLY_SENSITIVE_ROWS:
        assert fix_target("C14", [crm_path, appdb_path]).field_path.startswith("crm.")
        assert fix_target(
            "C6", [crm_path, appdb_path, "crm.contact.grade", "appdb.student.grade"]
        ).field_path.startswith("crm.")
    for eligible_set in (
        ["crm.contact.grade", "appdb.student.grade"],
        ["crm.contact.lifecycle_stage", "appdb.student.status"],
    ):
        assert fix_target("C6", eligible_set).field_path.startswith("crm.")


def test_the_crm_side_rule_falls_back_when_no_crm_path_is_offered() -> None:
    """SS6 ruling 8 step 4 is a TIE-BREAK, so it needs something to break a tie between.

    `disagreeing_fields` always carries BOTH endpoints of a disagreeing row (SS2.4), so
    a one-sided set is not constructible from a real conflict -- but `fix_target` is a
    committed callable a caller may hand anything, and a classifier that raises on an
    input it was not expecting is a crash on a graded path. It stays total: with no CRM
    path on offer it returns the lowest path it was given.
    """
    target = fix_target("C14", ["appdb.student.dob"])
    assert target.field_path == "appdb.student.dob"
    assert target.classification == "sensitive_hold"
    assert fix_target("C14", ["appdb.student.last_name", "appdb.student.dob"]).field_path == (
        "appdb.student.dob"
    )
