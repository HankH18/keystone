"""`canon_value` -- the canonical value serializer (contract SS2.5).

The float ban is the load-bearing case: SS2.5 pins `Money(cents)` as the only
money-shaped input, and a bare float must **raise**, not silently format, or the
SS5.4 fingerprint stops being defined for every value a conflict can carry.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from recon.reference import Money, canon_value

PROPERTY = settings(derandomize=True, max_examples=400, deadline=None)

RS = "\x1e"  # SS2.5 sequence element separator
US = "\x1f"  # SS5.4 intra-section joiner

#: SS2.5's pinned string escape set, longest-prefix-first. Written out here rather
#: than imported so the test states the contract instead of reading the code.
_STRING_ESCAPES: tuple[tuple[str, str], ...] = (
    ("\\\\", "\\"),
    ("\\x1f", US),
    ("\\x1e", RS),
)


def _unescape(encoded: str) -> str:
    """Reverse SS2.5's string escaping. A correct decoder, so a round trip proves
    injectivity rather than merely surviving the cases that happen to be simple."""
    out: list[str] = []
    index = 0
    while index < len(encoded):
        if encoded[index] == "\\":
            for token, raw in _STRING_ESCAPES:
                if encoded.startswith(token, index):
                    out.append(raw)
                    index += len(token)
                    break
            else:  # pragma: no cover - a bare backslash would be a serializer bug
                raise AssertionError(f"unescaped backslash at {index} in {encoded!r}")
        else:
            out.append(encoded[index])
            index += 1
    return "".join(out)


def _multiset_normal(value: object) -> tuple:
    """The structural normal form SS2.5 pins for a value: a sequence is a sorted
    multiset of its elements' normal forms, and a scalar is its canonical text.

    Two values with the SAME normal form are the same value under the contract (order
    within a sequence never reaches the digest). Two values with DIFFERENT normal
    forms must have different canonical forms -- that is injectivity, and this function
    is what lets the property test say so without re-implementing the serializer.
    """
    if isinstance(value, list | tuple | set | frozenset):
        return ("seq", tuple(sorted(_multiset_normal(item) for item in value)))
    return ("leaf", canon_value(value))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "\\N"),
        (True, "true"),
        (False, "false"),
        (0, "0"),
        (42, "42"),
        (-7, "-7"),
        (1_000_000, "1000000"),
        (Money(0), "0"),
        (Money(1200000), "1200000"),
        (Money(-50), "-50"),
        (date(2010, 4, 5), "2010-04-05"),
        (datetime(2010, 4, 5, 13, 30, 15, tzinfo=UTC), "2010-04-05T13:30:15Z"),
        ("plain", "plain"),
        ("", ""),
        ("a\\b", "a\\\\b"),
        ("a\x1fb", "a\\x1fb"),
    ],
)
def test_pinned_dispatch_table(value: object, expected: str) -> None:
    assert canon_value(value) == expected


def test_bool_is_dispatched_before_int() -> None:
    """`bool` is a subclass of `int`; the order matters and is pinned."""
    assert canon_value(True) == "true"
    assert canon_value(1) == "1"
    assert canon_value(True) != canon_value(1)


@pytest.mark.parametrize("value", [0.0, 1.5, 12000.0, -0.1, float("inf"), float("nan")])
def test_float_is_forbidden(value: float) -> None:
    """SS2.5: a bare float raises rather than serializing non-deterministically."""
    with pytest.raises(ValueError, match="float is FORBIDDEN"):
        canon_value(value)


def test_money_is_the_only_money_shaped_input() -> None:
    assert canon_value(Money.from_dollars(12000.0)) == "1200000"
    assert Money.from_dollars(500.0) == Money(50000)
    with pytest.raises(TypeError):
        Money(1200.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(True)  # type: ignore[arg-type]


def test_timestamps_are_normalized_to_utc() -> None:
    aware = datetime(2010, 4, 5, 8, 30, 15, tzinfo=timezone(timedelta(hours=-5)))
    assert canon_value(aware) == "2010-04-05T13:30:15Z"
    assert canon_value(aware) == canon_value(aware.astimezone(UTC))


def test_escaping_keeps_the_null_sentinel_unambiguous() -> None:
    """`canon_value(None)` is `\\N`; a literal `"\\N"` must not collide with it."""
    assert canon_value(None) == "\\N"
    assert canon_value("\\N") == "\\\\N"
    assert canon_value(None) != canon_value("\\N")


def test_escaping_keeps_the_unit_separator_out_of_the_payload() -> None:
    """SS5.4 joins with `\\x1f`; an escaped value can never forge a delimiter."""
    assert "\x1f" not in canon_value("a\x1fb")
    assert canon_value("a\x1fb") != canon_value("ab")


def test_multi_valued_observed_values_are_deterministic() -> None:
    """SS5.4 pins three multi-valued keys; the joiner is committed once, here."""
    refs = ["payments:payment:pi_2", "payments:payment:pi_1"]
    assert canon_value(refs) == canon_value(list(reversed(refs)))
    assert canon_value({"b", "a"}) == canon_value(["a", "b"]) == canon_value(("b", "a"))
    # SS2.5 ruling 2: a sequence is self-delimiting, so the EMPTY sequence is the lone
    # leading marker -- deliberately NOT `""`, which is `canon_value("")`. The old
    # `canon_value([]) == ""` was an instance of the non-injectivity this ruling closes.
    assert canon_value([]) == "\x1e"
    assert canon_value([]) != canon_value("")


def test_unsupported_types_raise_rather_than_using_repr() -> None:
    with pytest.raises(TypeError):
        canon_value(object())
    with pytest.raises(TypeError):
        canon_value({"a": 1})


CANONICALIZABLE = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.integers(min_value=-(10**9), max_value=10**9).map(Money),
    st.dates(),
    st.datetimes(timezones=st.just(UTC)),
    st.text(max_size=32),
)


@PROPERTY
@given(CANONICALIZABLE)
def test_canon_value_is_deterministic_and_returns_str(value: object) -> None:
    first = canon_value(value)
    assert isinstance(first, str)
    assert first == canon_value(value)


@PROPERTY
@given(st.text(max_size=32))
def test_string_escaping_round_trips(value: str) -> None:
    """Deterministic *and* reversible: escaping is injective, so distinct values
    can never share a fingerprint through the serializer."""
    encoded = canon_value(value)
    assert _unescape(encoded) == value
    # No canonical form of a string may contain a RAW separator: that is what keeps a
    # value from forging a section boundary (SS5.4) or an element boundary (SS2.5).
    assert US not in encoded
    assert RS not in encoded


@PROPERTY
@given(st.text(max_size=24), st.text(max_size=24))
def test_distinct_strings_have_distinct_canonical_forms(left: str, right: str) -> None:
    if left == right:
        return
    assert canon_value(left) != canon_value(right)


@PROPERTY
@given(st.floats(allow_nan=True, allow_infinity=True))
def test_no_float_ever_serializes(value: float) -> None:
    with pytest.raises(ValueError, match="float is FORBIDDEN"):
        canon_value(value)


def test_canonical_form_is_stable_across_processes(service_root: Path) -> None:
    """`PYTHONHASHSEED` must not be able to change a canonical form."""
    script = (
        "from datetime import UTC, datetime;"
        "from recon.reference import Money, canon_value;"
        "print('|'.join(canon_value(v) for v in ("
        "None, True, 7, Money(1200000), datetime(2010,4,5,1,2,3,tzinfo=UTC),"
        " 'x\\x1fy', ['b','a'])))"
    )
    outputs = []
    for seed in ("0", "1", "random"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=service_root,
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": seed},
            timeout=120,
        )
        outputs.append(result.stdout.strip())
    assert len(set(outputs)) == 1, outputs
    assert outputs[0].startswith("\\N|true|7|1200000|2010-04-05T01:02:03Z|")


# =====================================================================================
# SS2.5 ruling 2 -- the sequence case: pinned wire form, and INJECTIVITY
# =====================================================================================


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([], RS),
        ([""], RS + RS),
        (["a"], f"{RS}a{RS}"),
        (["a", "b"], f"{RS}a{RS}b{RS}"),
        (["b", "a"], f"{RS}a{RS}b{RS}"),
        (("b", "a"), f"{RS}a{RS}b{RS}"),
        (frozenset({"b", "a"}), f"{RS}a{RS}b{RS}"),
        # elements are canonicalized first, then escaped, then sorted, then joined
        ([1, 2], f"{RS}1{RS}2{RS}"),
        ([None], f"{RS}\\\\N{RS}"),  # `\N` escaped for embedding -> `\\N`
        # a raw RS inside a string element is escaped and can never forge a boundary
        (["a\x1eb"], f"{RS}a\\\\x1eb{RS}"),
        # a nested sequence's own separators are re-escaped when it is embedded
        ([["a"]], f"{RS}\\x1ea\\x1e{RS}"),
    ],
)
def test_sequence_encoding_is_the_pinned_wire_form(value: object, expected: str) -> None:
    """SS2.5: `RS + "".join(e + RS for e in sorted(escaped elements))`, restated
    literally from the contract so a re-implementer lands on the same bytes."""
    assert canon_value(value) == expected


@pytest.mark.parametrize(
    ("left", "right", "why"),
    [
        # THE ORIGINAL DEFECT: `\x1e` was the joiner but was not in the escape set.
        (["a\x1eb"], ["a", "b"], "a separator inside an element forged an element boundary"),
        (["a\x1e"], ["a", ""], "trailing separator inside an element"),
        (["\x1e"], ["", ""], "an element that is nothing but a separator"),
        # Nesting: a singleton sequence must not read as its own element.
        (["a"], "a", "a singleton sequence vs its element"),
        ([["a"]], ["a"], "one level of nesting vs none"),
        ([["a"], ["b"]], ["a", "b"], "two singleton children vs two scalar children"),
        ([["a", "b"]], [["a"], ["b"]], "one child of two vs two children of one"),
        # The empty sequence is not the empty string, and not the sequence of one empty.
        ([], "", "the empty sequence vs the empty string"),
        ([], [""], "the empty sequence vs a sequence holding one empty string"),
        ([""], "", "a sequence holding one empty string vs the empty string"),
        # Backslash escaping stays reversible under element escaping.
        (["a\\b"], ["a\\\\b"], "one backslash vs two"),
        (["a\\x1eb"], ["a\x1eb"], "the ESCAPE TEXT vs the raw separator"),
        # The other separator, SS5.4's, is escaped too.
        (["a\x1fb"], ["a\x1f", "b"], "the unit separator inside an element"),
        # Multisets are values: a repeat is not the same as a de-duplication.
        (["a", "a"], ["a"], "a repeated element vs a single one"),
    ],
)
def test_the_sequence_encoding_is_injective_on_every_named_collision(
    left: object, right: object, why: str
) -> None:
    """SS2.5 ruling 2. Each row is a pair of STRUCTURALLY DIFFERENT `observed_values`
    that a non-injective encoding maps to one fingerprint -- and the fingerprint is the
    idempotency key R16's oscillation dedup runs on, so a collision there silently
    suppresses a real second proposal."""
    assert canon_value(left) != canon_value(right), why


NESTED = st.recursive(
    st.one_of(
        st.none(),
        st.integers(min_value=-9, max_value=9),
        # every separator character, plus a backslash and its escape text, as leaves
        st.text(alphabet=st.sampled_from(["a", "b", "\\", RS, US, "x", "1", "e", "f"]), max_size=5),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.tuples(children),
        st.lists(children, max_size=3).map(tuple),
    ),
    max_leaves=6,
)


@settings(derandomize=True, max_examples=1500, deadline=None)
@given(NESTED, NESTED)
def test_canon_value_is_injective_over_nested_structures(left: object, right: object) -> None:
    """SS2.5 ruling 2: distinct values have distinct canonical forms, over nested
    structures whose leaves contain every separator character.

    "Distinct" is measured by SS2.5's own semantics -- a sequence is a sorted multiset,
    so element order is deliberately not distinguishing. Everything else must be.
    """
    if _multiset_normal(left) == _multiset_normal(right):
        return
    assert canon_value(left) != canon_value(right)


#: The same space, but always a sequence at the top level.
NESTED_SEQUENCE = st.lists(NESTED, max_size=3)


@settings(derandomize=True, max_examples=1500, deadline=None)
@given(NESTED_SEQUENCE)
def test_a_sequence_is_never_mistakable_for_a_scalar(value: object) -> None:
    """Every sequence encoding begins with a RAW `\\x1e`; no scalar encoding contains
    one. That is what makes the encoding self-delimiting."""
    encoded = canon_value(value)
    assert encoded.startswith(RS)
    for scalar in (None, True, False, 0, -7, Money(12), date(2010, 4, 5), "", "a", "\x1e", "\\"):
        assert encoded != canon_value(scalar)


# =====================================================================================
# SS2.5 ruling 4 -- timestamps: naive is UTC, second precision
# =====================================================================================


def test_a_naive_datetime_is_utc_never_the_local_zone() -> None:
    """SS2.5 ruling 4. The generator and the detector must emit the same bytes on a
    laptop in `America/Chicago` and in a UTC container, so a naive value is stamped `Z`
    as-is and is never localized."""
    naive = datetime(2010, 4, 5, 13, 30, 15)
    assert canon_value(naive) == "2010-04-05T13:30:15Z"
    assert canon_value(naive) == canon_value(naive.replace(tzinfo=UTC))
    # ...and an AWARE value is converted, not stamped: 08:30-05:00 is 13:30Z.
    assert canon_value(datetime(2010, 4, 5, 8, 30, 15, tzinfo=timezone(timedelta(hours=-5)))) == (
        "2010-04-05T13:30:15Z"
    )


@pytest.mark.parametrize(
    ("microsecond", "expected"),
    [(0, "2010-04-05T01:02:03Z"), (1, "2010-04-05T01:02:03Z"), (999_999, "2010-04-05T01:02:03Z")],
)
def test_microseconds_are_truncated_never_rounded(microsecond: int, expected: str) -> None:
    """SS2.5 ruling 4: second precision. Rounding would let a sub-second difference
    move a value across a second boundary and change a fingerprint."""
    moment = datetime(2010, 4, 5, 1, 2, 3, microsecond, tzinfo=UTC)
    assert canon_value(moment) == expected
    # 03.999999 must NOT become 04 -- that is the mutation this pins.
    assert canon_value(moment) != "2010-04-05T01:02:04Z"


# =====================================================================================
# SS2.5 ruling 13 -- Money.from_dollars uses round(), not int()
# =====================================================================================


@pytest.mark.parametrize(
    ("dollars", "cents", "truncated"),
    [
        # The three values where round() and int() diverge because IEEE-754 puts the
        # product a hair BELOW the integer: 0.29 * 100 == 28.999999999999996.
        (0.29, 29, 28),
        (1.15, 115, 114),
        (8.7, 870, 869),
    ],
)
def test_from_dollars_rounds_and_does_not_truncate(
    dollars: float, cents: int, truncated: int
) -> None:
    """SS1.2/SS2.5 ruling 13. `int()` understates by a cent on a large fraction of the
    15,000 deals; the two functions differ on exactly these inputs, so the test states
    both the right answer and the wrong one."""
    assert Money.from_dollars(dollars) == Money(cents)
    assert Money.from_dollars(dollars).cents == cents
    assert int(dollars * 100) == truncated
    assert cents != truncated


@pytest.mark.parametrize(("dollars", "cents"), [(0.125, 12), (0.375, 38), (0.625, 62)])
def test_round_is_bankers_rounding_at_an_exact_half_cent(dollars: float, cents: int) -> None:
    """SS2.5 ruling 13: `round()` is half-to-EVEN, not half-up -- `0.125 * 100` is
    exactly 12.5 in IEEE-754 and rounds DOWN to 12.

    `G39` forbids the generator from emitting any amount on a half-cent boundary, so
    this tie-break is committed (the function is defined for every input) yet can never
    decide a graded byte. The test pins it so that stays a deliberate choice.
    """
    assert Money.from_dollars(dollars) == Money(cents)
    assert cents % 2 == 0  # half-to-even always lands on an even cent
    assert round(0.5) == 0 and round(1.5) == 2 and round(2.5) == 2
