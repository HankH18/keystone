"""`COMPARED_FIELDS` -- the ONLY producer of `disagreeing_fields` (contract SS2.4).

Also covers SS5.1's comparison semantics, which are what keep an unmappable enum out
of the conflict set: `None` on either side is `unchecked`, never a disagreement.
"""

from __future__ import annotations

import pytest

from recon.reference import (
    COMPARED_FIELD_BY_LOGICAL,
    COMPARED_FIELD_PATHS,
    COMPARED_FIELDS,
    SENSITIVE_FIELDS,
    UNCHECKED_REASON_PRECEDENCE,
    UNCHECKED_REASONS,
    compare_field,
    compare_record,
    conflict_type_for_paths,
    disagreeing_fields,
)


def test_the_six_committed_rows() -> None:
    assert [row.logical for row in COMPARED_FIELDS] == [
        "name_first",
        "name_last",
        "dob",
        "grade",
        "stage",
        "lifecycle",
    ]
    assert [row.paths for row in COMPARED_FIELDS] == [
        ("crm.contact.first_name", "appdb.student.first_name"),
        ("crm.contact.last_name", "appdb.student.last_name"),
        ("crm.contact.dob", "appdb.student.dob"),
        ("crm.contact.grade", "appdb.student.grade"),
        ("crm.deal.stage", "appdb.enrollment.stage"),
        ("crm.contact.lifecycle_stage", "appdb.student.status"),
    ]


def test_every_endpoint_is_a_source_qualified_path() -> None:
    """SS2.4: the same vocabulary as `SENSITIVE_FIELDS`, so SS5.5's subset test is
    well-typed. Never a bare column name and never a logical name."""
    for path in COMPARED_FIELD_PATHS:
        assert path.count(".") >= 2
        assert path.split(".")[0] in {"crm", "appdb", "payments"}
    assert len(COMPARED_FIELD_PATHS) == 12


@pytest.mark.parametrize(
    ("logical", "emits"),
    [
        ("name_first", "C14"),
        ("name_last", "C14"),
        ("dob", "C14"),
        ("stage", "C14"),
        ("grade", "C6"),
        ("lifecycle", "C6"),
    ],
)
def test_the_sensitivity_partition_table(logical: str, emits: str) -> None:
    """SS2.4's partition table, which is what drives the C6/C14 split."""
    row = COMPARED_FIELD_BY_LOGICAL[logical]
    assert row.emits_alone == emits
    assert conflict_type_for_paths(row.paths) == emits


def test_lifecycle_row_has_exactly_one_sensitive_endpoint() -> None:
    row = COMPARED_FIELD_BY_LOGICAL["lifecycle"]
    assert row.left_path not in SENSITIVE_FIELDS
    assert row.right_path in SENSITIVE_FIELDS
    assert not row.wholly_sensitive


def test_grade_row_has_no_sensitive_endpoint() -> None:
    row = COMPARED_FIELD_BY_LOGICAL["grade"]
    assert row.left_path not in SENSITIVE_FIELDS
    assert row.right_path not in SENSITIVE_FIELDS


# ------------------------------------------------------------------- SS5.1 semantics


@pytest.mark.parametrize(
    ("logical", "left", "right", "verdict"),
    [
        # dirt normalizes away -- these agree
        ("name_first", " `José` ", "Jose", "ok"),
        ("name_last", "GARCÍA", "garcia", "ok"),
        ("dob", " 2010-04-05 ", "2010-04-05", "ok"),
        ("grade", "Grade 4", "4th", "ok"),
        ("grade", "Kindergarten", "KG", "ok"),
        ("stage", "Deposit Received", "deposit_paid", "ok"),
        ("stage", "CLOSED_WON", "enrolled", "ok"),
        ("lifecycle", "customer", "active", "ok"),
        ("lifecycle", "MQL", "prospect", "ok"),
        # real disagreements
        ("name_first", "Jon", "John", "conflict"),
        ("dob", "2010-04-05", "2010-04-06", "conflict"),
        ("grade", "Grade 4", "5th", "conflict"),
        ("stage", "Closed Won", "waitlisted", "conflict"),
        ("lifecycle", "customer", "applied", "conflict"),
    ],
)
def test_comparison_verdicts(logical: str, left: str, right: str, verdict: str) -> None:
    assert compare_field(logical, left, right).verdict == verdict


@pytest.mark.parametrize(
    ("logical", "left", "right", "reason"),
    [
        ("grade", None, "4", "missing_operand"),
        ("grade", "4", None, "missing_operand"),
        ("name_first", None, None, "missing_operand"),
        ("dob", None, "2010-04-05", "missing_operand"),
        ("grade", "Grade 99", "4", "unmapped_enum"),
        ("lifecycle", "not-a-stage", "enrolled", "unmapped_enum"),
        ("stage", "Closed Won", "not-a-stage", "unmapped_enum"),
        # SS5.1 ruling 5: a PRESENT but unparseable **non-enum** operand is neither
        # `missing_operand` (the source value was not NULL) nor `unmapped_enum` (no
        # enum was consulted -- `norm_dob`/`norm_name` are not table-driven).
        ("dob", "not-a-date", "2010-04-05", "unparseable_value"),
        ("dob", "2010-04-05", "not-a-date", "unparseable_value"),
        ("name_first", "'''", "ana", "unparseable_value"),
        ("name_last", "ana", "  ", "unparseable_value"),
        # ...and the precedence when both operands are None: NULL wins.
        ("dob", None, "not-a-date", "missing_operand"),
        ("grade", None, "Grade 99", "missing_operand"),
    ],
)
def test_none_operands_are_unchecked_never_a_disagreement(
    logical: str, left: str | None, right: str | None, reason: str
) -> None:
    """SS5.1/SS5.8: three disjoint, exhaustive `None` causes, each with a pinned reason."""
    result = compare_field(logical, left, right)
    assert result.verdict == "unchecked"
    assert result.reason == reason
    assert not result.disagrees


def test_a_withdrawn_student_is_unchecked_on_the_crm_side() -> None:
    """`G18`: no lifecycle value maps to `withdrawn`, so the `None`-mapping subset
    yields `unchecked` rather than a manufactured disagreement."""
    for lifecycle in ("subscriber", "evangelist", "other"):
        result = compare_field("lifecycle", lifecycle, "withdrawn")
        assert result.verdict == "unchecked"
        assert result.reason == "unmapped_enum"


def test_a_planted_conflict_can_never_be_created_by_nulling_a_field() -> None:
    """`G17`: both sides non-null and both normalizing to distinct non-`None` canonicals."""
    assert compare_field("grade", None, "4").verdict != "conflict"
    assert compare_field("grade", "3", "4").verdict == "conflict"


# ------------------------------------------------------------- disagreeing_fields set

CRM_SIDE = {
    "crm.contact.first_name": "Jon",
    "crm.contact.last_name": "Garcia",
    "crm.contact.dob": "2010-04-05",
    "crm.contact.grade": "Grade 4",
    "crm.deal.stage": "Closed Won",
    "crm.contact.lifecycle_stage": "customer",
}
APPDB_SIDE = {
    "appdb.student.first_name": "Jon",
    "appdb.student.last_name": "Garcia",
    "appdb.student.dob": "2010-04-05",
    "appdb.student.grade": "4",
    "appdb.enrollment.stage": "enrolled",
    "appdb.student.status": "active",
}


def test_a_clean_person_disagrees_on_nothing() -> None:
    comparisons = compare_record(CRM_SIDE, APPDB_SIDE)
    assert [c.verdict for c in comparisons] == ["ok"] * 6
    assert disagreeing_fields(comparisons) == ()
    assert conflict_type_for_paths(()) is None


def test_disagreeing_fields_is_the_sorted_set_of_both_endpoints() -> None:
    comparisons = compare_record({**CRM_SIDE, "crm.contact.grade": "5th"}, APPDB_SIDE)
    assert disagreeing_fields(comparisons) == ("appdb.student.grade", "crm.contact.grade")


def test_a_grade_only_disagreement_is_c6() -> None:
    comparisons = compare_record({**CRM_SIDE, "crm.contact.grade": "5th"}, APPDB_SIDE)
    paths = disagreeing_fields(comparisons)
    assert conflict_type_for_paths(paths) == "C6"


def test_a_name_only_disagreement_is_c14() -> None:
    """SS12 D-2: name spelling surfaces under C14, which forces `sensitive_hold`."""
    comparisons = compare_record({**CRM_SIDE, "crm.contact.first_name": "John"}, APPDB_SIDE)
    paths = disagreeing_fields(comparisons)
    assert paths == ("appdb.student.first_name", "crm.contact.first_name")
    assert set(paths) <= SENSITIVE_FIELDS
    assert conflict_type_for_paths(paths) == "C14"


def test_a_mixed_disagreement_is_c6_with_the_sensitive_paths_listed() -> None:
    """SS5.6 C6's 80 mixed plants: a name/DOB path together with a grade path."""
    comparisons = compare_record(
        {**CRM_SIDE, "crm.contact.first_name": "John", "crm.contact.grade": "5th"}, APPDB_SIDE
    )
    paths = disagreeing_fields(comparisons)
    assert paths == (
        "appdb.student.first_name",
        "appdb.student.grade",
        "crm.contact.first_name",
        "crm.contact.grade",
    )
    assert conflict_type_for_paths(paths) == "C6"
    assert set(paths) & SENSITIVE_FIELDS


def test_a_stage_only_disagreement_is_c14() -> None:
    """SS6/SS12 D-8: `crm.deal.stage` is sensitive, so stage-only plants are C14."""
    comparisons = compare_record({**CRM_SIDE, "crm.deal.stage": "Waitlisted"}, APPDB_SIDE)
    paths = disagreeing_fields(comparisons)
    assert paths == ("appdb.enrollment.stage", "crm.deal.stage")
    assert conflict_type_for_paths(paths) == "C14"


def test_a_lifecycle_only_disagreement_is_c6() -> None:
    comparisons = compare_record({**CRM_SIDE, "crm.contact.lifecycle_stage": "lead"}, APPDB_SIDE)
    paths = disagreeing_fields(comparisons)
    assert paths == ("appdb.student.status", "crm.contact.lifecycle_stage")
    assert conflict_type_for_paths(paths) == "C6"


def test_unchecked_comparisons_never_reach_disagreeing_fields() -> None:
    comparisons = compare_record(
        {**CRM_SIDE, "crm.contact.grade": None, "crm.contact.lifecycle_stage": "bogus"},
        APPDB_SIDE,
    )
    assert disagreeing_fields(comparisons) == ()
    assert {c.reason for c in comparisons if c.verdict == "unchecked"} == {
        "missing_operand",
        "unmapped_enum",
    }


def test_compare_field_accepts_a_row_object_as_well_as_its_logical_name() -> None:
    row = COMPARED_FIELD_BY_LOGICAL["grade"]
    assert compare_field(row, "4", "5") == compare_field("grade", "4", "5")


# =====================================================================================
# SS5.1 / SS5.8 ruling 5 -- the `unparseable_value` reason
# =====================================================================================


def test_the_unchecked_reason_vocabulary_is_the_committed_six() -> None:
    """SS5.8, restated literally from the contract."""
    assert UNCHECKED_REASONS == frozenset(  # noqa: SIM300 - committed constant on the left reads as the claim
        {
            "no_rule_in_scope",
            "missing_operand",
            "unmapped_enum",
            "unparseable_value",
            "enrollment_unattributed",
            "deal_unresolved",
            "source_incomplete",
        }
    )
    assert "unparseable_value" in UNCHECKED_REASONS


def test_the_three_none_causes_are_precedence_ordered() -> None:
    """SS5.1 ruling 5: one comparison emits ONE reason, so the causes are ordered and a
    NULL operand -- the most specific statement available -- always wins."""
    assert UNCHECKED_REASON_PRECEDENCE == ("missing_operand", "unparseable_value", "unmapped_enum")
    assert set(UNCHECKED_REASON_PRECEDENCE) <= UNCHECKED_REASONS


@pytest.mark.parametrize(
    ("logical", "present_but_unnormalizable", "expected_reason"),
    [
        # enum-mapped rows report `unmapped_enum`...
        ("grade", "Grade 99", "unmapped_enum"),
        ("stage", "not-a-stage", "unmapped_enum"),
        ("lifecycle", "not-a-stage", "unmapped_enum"),
        # ...and NON-enum rows report `unparseable_value` (ruling 5). A name that is
        # nothing but quote characters, and a date in the wrong shape, are both PRESENT.
        ("name_first", "'''", "unparseable_value"),
        ("name_last", '`` "" ', "unparseable_value"),
        ("dob", "04/05/2010", "unparseable_value"),
    ],
)
def test_the_reason_is_a_property_of_the_comparison_row(
    logical: str, present_but_unnormalizable: str, expected_reason: str
) -> None:
    """SS5.1 ruling 5: which of the three applies is a function of the ROW and of whether
    the source value was NULL -- never of a guess about the value's contents.

    An unparseable `crm.contact.dob` is neither `missing_operand` (the source value was
    not NULL) nor `unmapped_enum` (no enum was consulted -- `norm_dob` and `norm_name`
    are not table-driven). Forcing it into either puts a false statement into
    `detail.reason`, and the generator and the detector would force it differently.
    """
    assert COMPARED_FIELD_BY_LOGICAL[logical].unmapped_reason == expected_reason
    result = compare_field(logical, present_but_unnormalizable, present_but_unnormalizable)
    assert present_but_unnormalizable is not None  # PRESENT: never `missing_operand`
    assert result.verdict == "unchecked"
    assert result.reason == expected_reason
    assert not result.disagrees


def test_an_unparseable_dob_is_never_reported_as_an_unmapped_enum() -> None:
    """The mutation stated directly: collapsing ruling 5 back into two codes."""
    result = compare_field("dob", "not-a-date", "2010-04-05")
    assert result.reason == "unparseable_value"
    assert result.reason != "unmapped_enum"
    assert result.reason != "missing_operand"


def test_every_row_reports_a_reason_from_the_committed_vocabulary() -> None:
    for row in COMPARED_FIELDS:
        assert row.unmapped_reason in UNCHECKED_REASONS
        assert row.unmapped_reason in UNCHECKED_REASON_PRECEDENCE


# =====================================================================================
# SS5.1 -- `UNCHECKED_REASON_PRECEDENCE` must DRIVE the behaviour, not merely describe it
# =====================================================================================
#
# The constant was exported and asserted, but `compare_field` hard-coded
# `missing_operand` and never read it. A constant that documents behaviour it does not
# drive is a trap: the contract's claim can be re-ordered without a single emitted
# reason moving, and the next reader believes the tuple is authoritative.


@pytest.mark.parametrize(
    ("logical", "left", "right", "expected"),
    [
        # NULL on one side, present-but-unparseable on the other -> the NULL wins
        ("dob", None, "04/05/2010", "missing_operand"),
        ("dob", "04/05/2010", None, "missing_operand"),
        ("name_first", None, "'''", "missing_operand"),
        # NULL on one side, present-but-unmappable enum on the other -> the NULL wins
        ("grade", None, "Grade 99", "missing_operand"),
        ("grade", "Grade 99", None, "missing_operand"),
        ("lifecycle", None, "not-a-stage", "missing_operand"),
        # both NULL -> still one reason, and it is the NULL one
        ("dob", None, None, "missing_operand"),
        ("grade", None, None, "missing_operand"),
    ],
)
def test_a_null_operand_outranks_a_present_but_unnormalizable_one(
    logical: str, left: object, right: object, expected: str
) -> None:
    """SS5.1: "A NULL operand is the most specific and least ambiguous statement
    available, so it is reported whenever either side is NULL"."""
    result = compare_field(logical, left, right)
    assert result.verdict == "unchecked"
    assert result.reason == expected
    assert not result.disagrees


def test_compare_field_READS_the_precedence_constant() -> None:
    """The binding test: re-order the committed tuple and the emitted reason follows.

    With the precedence hard-coded as an `if`, this test fails -- which is the whole
    point. It is the only assertion that can tell "the behaviour matches the constant"
    apart from "the behaviour and the constant happen to agree".
    """
    import recon.reference as reference

    # one NULL operand and one present-but-unparseable operand: both causes are live
    baseline = compare_field("dob", None, "04/05/2010")
    assert baseline.reason == "missing_operand"

    reordered = ("unparseable_value", "missing_operand", "unmapped_enum")
    original = reference.UNCHECKED_REASON_PRECEDENCE
    try:
        reference.UNCHECKED_REASON_PRECEDENCE = reordered
        flipped = compare_field("dob", None, "04/05/2010")
        assert flipped.reason == "unparseable_value", (
            "compare_field ignored UNCHECKED_REASON_PRECEDENCE -- the constant documents "
            "behaviour it does not drive"
        )
        # the enum row follows the SAME constant: `unmapped_enum` is still last in the
        # reordered tuple, so the NULL operand still wins on that row
        assert compare_field("grade", None, "Grade 99").reason == "missing_operand"
    finally:
        reference.UNCHECKED_REASON_PRECEDENCE = original

    assert compare_field("dob", None, "04/05/2010").reason == "missing_operand"


def test_only_the_causes_actually_present_are_candidates() -> None:
    """The precedence picks among the reasons this comparison really has: a row whose
    only failing operand is a NULL never reports the row's `unmapped_reason`, and a row
    with no NULL operand never reports `missing_operand` however the tuple is ordered."""
    import recon.reference as reference

    original = reference.UNCHECKED_REASON_PRECEDENCE
    try:
        reference.UNCHECKED_REASON_PRECEDENCE = (
            "unparseable_value",
            "unmapped_enum",
            "missing_operand",
        )
        # both operands NULL: `missing_operand` is the ONLY candidate, so it still wins
        assert compare_field("dob", None, None).reason == "missing_operand"
        assert compare_field("grade", None, None).reason == "missing_operand"
        # neither operand NULL: `missing_operand` is not a candidate at all
        assert compare_field("dob", "04/05/2010", "nope").reason == "unparseable_value"
        assert compare_field("grade", "Grade 99", "Grade 98").reason == "unmapped_enum"
    finally:
        reference.UNCHECKED_REASON_PRECEDENCE = original
