"""`norm_dob` -- `YYYY-MM-DD` or `None`, never a raise (contract SS2.1)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from recon.normalize import norm_dob


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2010-04-05", "2010-04-05"),
        ("  2010-04-05  ", "2010-04-05"),
        ("'2010-04-05'", "2010-04-05"),
        ("`2010-04-05`", "2010-04-05"),
        (date(2010, 4, 5), "2010-04-05"),
        (datetime(2010, 4, 5, 13, 30), "2010-04-05"),
    ],
)
def test_accepted_shapes(raw: object, expected: str) -> None:
    assert norm_dob(raw) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "not-a-date",
        "2010-02-30",  # well-formed shape, impossible day
        "2010-13-01",
        "20100405",  # not the pinned shape
        "04/05/2010",
        "2010-4-5",
        "2010-04-05T00:00:00Z",
        42,
    ],
)
def test_unparseable_is_none_and_never_raises(raw: object) -> None:
    """SS5.1: a `None` operand is `unchecked`, never a disagreement and never a crash."""
    assert norm_dob(raw) is None  # type: ignore[arg-type]
