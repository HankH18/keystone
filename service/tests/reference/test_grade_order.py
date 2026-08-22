"""`GRADE_ORDER` -- an INTEGER ordinal, never a string comparison (contract SS2.3).

SS2.3 pins the ordinal precisely because string comparison is wrong here, and it
names the exact pairs that go wrong. A test that only checked that the ordinals
ascend would pass against a broken string sort, so each named pair is asserted
BOTH ways: the ordinal answer, and the string answer it must not agree with.
"""

from __future__ import annotations

import pytest

from recon.reference import ENROLLMENT_GRADE_FLOOR, GRADE_ORDER, GRADE_VALUES, grade_ord


def test_grade_order_is_the_committed_table() -> None:
    assert dict(GRADE_ORDER) == {
        "PK": -1,
        "K": 0,
        "1": 1,
        "2": 2,
        "3": 3,
        "4": 4,
        "5": 5,
        "6": 6,
        "7": 7,
        "8": 8,
        "9": 9,
        "10": 10,
        "11": 11,
        "12": 12,
    }


@pytest.mark.parametrize(
    ("left", "right", "ordinal_lt", "string_lt"),
    [
        # SS2.3 verbatim: 'PK' < 'K' is FALSE...
        ("PK", "K", True, False),
        # ...and '1' < 'K', '10' < 'K', '12' < 'K' are all TRUE.
        ("1", "K", False, True),
        ("10", "K", False, True),
        ("12", "K", False, True),
    ],
)
def test_ordinal_ordering_contradicts_string_ordering_on_the_named_pairs(
    left: str, right: str, ordinal_lt: bool, string_lt: bool
) -> None:
    assert (GRADE_ORDER[left] < GRADE_ORDER[right]) is ordinal_lt
    assert (left < right) is string_lt
    # The point of the constant: the two answers disagree on every named pair.
    assert (GRADE_ORDER[left] < GRADE_ORDER[right]) != (left < right)


def test_ordinals_ascend_in_committed_order() -> None:
    ordinals = [GRADE_ORDER[value] for value in GRADE_VALUES]
    assert ordinals == sorted(ordinals)
    assert ordinals == list(range(-1, 13))


def test_grade_order_is_injective_and_total() -> None:
    assert len(set(GRADE_ORDER.values())) == len(GRADE_ORDER) == len(GRADE_VALUES)
    for value in GRADE_VALUES:
        assert isinstance(GRADE_ORDER[value], int)


def test_sorting_grades_by_ordinal_differs_from_sorting_them_as_strings() -> None:
    by_ordinal = sorted(GRADE_VALUES, key=lambda value: GRADE_ORDER[value])
    by_string = sorted(GRADE_VALUES)
    assert by_ordinal == ["PK", "K", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"]
    assert by_string != by_ordinal
    assert by_string[0] == "1"  # the broken answer, pinned so a regression is visible


def test_enrollment_grade_floor_is_the_c8_mask_eligibility_test() -> None:
    """`G22`: a child is excluded from the C8 mask when its grade is below the floor."""
    floor = GRADE_ORDER[ENROLLMENT_GRADE_FLOOR]
    assert floor == 0
    assert GRADE_ORDER["PK"] < floor
    for value in GRADE_VALUES:
        if value != "PK":
            assert GRADE_ORDER[value] >= floor


def test_grade_ord_accepts_dirty_values_and_returns_none_when_unmappable() -> None:
    assert grade_ord("Grade 4") == 4
    assert grade_ord("4th") == 4
    assert grade_ord("Fourth") == 4
    assert grade_ord("Kindergarten") == 0
    assert grade_ord("Pre-K") == -1
    assert grade_ord(None) is None
    assert grade_ord("thirteenth") is None


def test_grade_ord_is_an_int_comparison_not_a_string_one() -> None:
    assert grade_ord("Pre-K") < grade_ord("Kindergarten") < grade_ord("1st")  # type: ignore[operator]
    assert grade_ord("9th") < grade_ord("10th")  # type: ignore[operator]
    assert "9" > "10"  # the string answer, which is why the ordinal exists


def test_undeclared_grade_key_raises() -> None:
    with pytest.raises(KeyError):
        GRADE_ORDER["13"]
