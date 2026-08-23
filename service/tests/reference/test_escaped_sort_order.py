"""ONE rule, three normative sites: **sort over the ESCAPED encodings**.

The contract states it three times -- SS2.5's sequence case ("sorted = ascending by code
point, over the ESCAPED element encodings"), SS5.4 section 2 (`entity_refs`) and SS5.4
section 3 (`disagreeing_fields`) -- and `recon.reference` restates it in all three
docstrings. The contract also says, in the same breath, that "escaped and raw order
coincide for every value this contract can produce ... but only one of the two orders
may be pinned, and it is this one."

That last clause is exactly why this module exists. Every other test in `tests/reference`
feeds values the contract can produce, and for those values `sorted(escape(x))` and
`escape(sorted(x))` are the same list -- so swapping any of the three sites to
sort-raw-then-escape changes nothing the rest of the suite can see, golden digest
literals included. 100% branch coverage does not help: both orders execute the same
branches.

**Why ordinary values cannot distinguish the two orders.** The backslash pass alone is
order-preserving. It is a prefix-free code (`\\` -> `\\\\`, every other character -> itself,
and no code is a prefix of another) whose codes sort in the same order as their inputs,
and a prefix-free monotone code preserves lexicographic order under concatenation. Only
the *separator* escapes break monotonicity: `\\x1e` (U+001E) and `\\x1f` (U+001F) sort
BELOW almost every printable character, while their escapes begin with a backslash
(U+005C) which sorts ABOVE most of them. So an element carrying a raw separator is the
only thing that moves when the sort moves -- and it takes a second element whose next
character lies strictly between U+001F and U+005C to expose it. `Z` (U+005A) is that
pivot throughout this module.

Each test therefore states BOTH orders and pins the one the contract chose.
"""

from __future__ import annotations

import hashlib

import pytest

from recon.reference import canon_value, fingerprint

RS = "\x1e"  # SS2.5's element separator
US = "\x1f"  # SS5.4's intra-section joiner
#: U+005A: above the raw separators, below the backslash their escapes begin with.
PIVOT = "Z"


def test_the_pivot_character_is_what_makes_the_two_orders_differ() -> None:
    """The premise every test below rests on, asserted rather than assumed."""
    assert RS < US < PIVOT < "\\"


# =====================================================================================
# SS2.5 -- canon_value's sequence case sorts the ESCAPED elements
# =====================================================================================
#
# A nested sequence is the only element whose CANONICAL form still contains a raw `\x1e`
# (the string case escapes its own). `_escape_element` turns that leading `\x1e` into a
# leading backslash, which is what moves it across the pivot.

#: `["a"]` canonicalizes to a string STARTING with a raw `\x1e` ...
NESTED_CANONICAL = f"{RS}a{RS}"
#: ... and embeds as a string starting with a BACKSLASH.
NESTED_ESCAPED = "\\x1ea\\x1e"


def test_the_nested_element_crosses_the_pivot_when_it_is_escaped() -> None:
    """Raw: the nested child sorts FIRST. Escaped: it sorts LAST. Nothing else about
    the two encodings differs, so the sequence's bytes decide which order ran."""
    assert canon_value(["a"]) == NESTED_CANONICAL
    assert NESTED_CANONICAL < PIVOT  # raw order: nested child first
    assert PIVOT < NESTED_ESCAPED  # escaped order: nested child last


def test_a_sequence_sorts_over_the_escaped_elements_not_the_raw_ones() -> None:
    """SS2.5, pinned to the byte. The escaped order puts `Z` first; sorting the raw
    canonical forms and escaping afterwards would put the nested child first."""
    escaped_order = RS + "".join(f"{element}{RS}" for element in (PIVOT, NESTED_ESCAPED))
    raw_order = RS + "".join(f"{element}{RS}" for element in (NESTED_ESCAPED, PIVOT))

    assert escaped_order != raw_order
    assert canon_value([PIVOT, ["a"]]) == escaped_order
    assert canon_value([PIVOT, ["a"]]) == f"{RS}Z{RS}\\x1ea\\x1e{RS}"


def test_the_sequence_order_is_a_property_of_the_values_not_of_the_input_order() -> None:
    """A sequence is a sorted multiset (SS2.5), so both input orders land on the one
    escaped order -- never on whichever order the caller happened to pass."""
    forward = canon_value([PIVOT, ["a"]])
    reversed_input = canon_value([["a"], PIVOT])
    assert forward == reversed_input == f"{RS}Z{RS}\\x1ea\\x1e{RS}"


def test_a_backslash_alone_cannot_distinguish_the_two_orders() -> None:
    """The lemma from this module's docstring, asserted so the choice of a *separator*
    -- not merely a backslash -- as the distinguishing character is on the record.

    The backslash pass is a prefix-free monotone code, so it preserves order: a test
    built from backslash-bearing elements would pass under BOTH orders and pin nothing.
    """
    elements = ["a\\b", "a\\\\", "a]", "aZ", "\\", "a"]
    escaped_then_sorted = sorted(item.replace("\\", "\\\\") for item in elements)
    sorted_then_escaped = [item.replace("\\", "\\\\") for item in sorted(elements)]
    assert escaped_then_sorted == sorted_then_escaped


# =====================================================================================
# SS5.4 sections 2 and 3 -- the same rule, on refs and on disagreeing paths
# =====================================================================================
#
# Here the "escaping" is `canon_value` itself, and a raw `\x1f` in a ref is the element
# that crosses the pivot: `canon_value` turns it into the four TEXT characters `\ x 1 f`.

C8_VALUES = {
    "household_key": "parent@corp.com",
    "dropped_source": "crm",
    "eligible_member_count": 3,
}
C6_GRADE_VALUES = {"crm.contact.grade": "4", "appdb.student.grade": "5"}

#: A ref carrying SS5.4's own joiner. `make_ref` refuses to build one -- this is the
#: second, independent guard, for a ref that reaches the hash another way.
JOINER_REF = f"appdb:student:a{US}b"
ESCAPED_JOINER_REF = "appdb:student:a\\x1fb"  # the four TEXT characters
PIVOT_REF = f"appdb:student:a{PIVOT}"

JOINER_PATH = f"crm.contact.grade{US}x"
ESCAPED_JOINER_PATH = "crm.contact.grade\\x1fx"
PIVOT_PATH = f"crm.contact.grade{PIVOT}"


def test_the_joiner_bearing_ref_crosses_the_pivot_when_it_is_escaped() -> None:
    assert canon_value(JOINER_REF) == ESCAPED_JOINER_REF
    assert JOINER_REF < PIVOT_REF  # raw order
    assert PIVOT_REF < ESCAPED_JOINER_REF  # escaped order


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: SS5.4's four sections, joined by `|`, with section 2 in the ESCAPED order.
SECTION_2_PAYLOAD = (
    "C8"
    + "|"
    + PIVOT_REF
    + US
    + ESCAPED_JOINER_REF
    + "|"
    + ""
    + "|"
    + US.join(["dropped_source=crm", "eligible_member_count=3", "household_key=parent@corp.com"])
)
#: The same four sections with section 2 sorted RAW and escaped afterwards.
SECTION_2_RAW_ORDER_PAYLOAD = (
    "C8"
    + "|"
    + ESCAPED_JOINER_REF
    + US
    + PIVOT_REF
    + "|"
    + ""
    + "|"
    + US.join(["dropped_source=crm", "eligible_member_count=3", "household_key=parent@corp.com"])
)

SECTION_3_PAYLOAD = (
    "C6"
    + "|"
    + "appdb:student:s1"
    + "|"
    + PIVOT_PATH
    + US
    + ESCAPED_JOINER_PATH
    + "|"
    + US.join(["appdb.student.grade=5", "crm.contact.grade=4"])
)
SECTION_3_RAW_ORDER_PAYLOAD = (
    "C6"
    + "|"
    + "appdb:student:s1"
    + "|"
    + ESCAPED_JOINER_PATH
    + US
    + PIVOT_PATH
    + "|"
    + US.join(["appdb.student.grade=5", "crm.contact.grade=4"])
)


def test_section_2_sorts_over_the_escaped_refs_not_the_raw_ones() -> None:
    """SS5.4 section 2, re-derived from the contract's prose and pinned to a literal."""
    digest = fingerprint("C8", [JOINER_REF, PIVOT_REF], [], C8_VALUES)
    assert digest == _digest(SECTION_2_PAYLOAD)
    assert digest == "3f49314678ca393ff871df08ba317c21a8b9a179470c0bbe41495cae67b3ae7f"
    assert digest != _digest(SECTION_2_RAW_ORDER_PAYLOAD)
    assert (
        _digest(SECTION_2_RAW_ORDER_PAYLOAD)
        == "52f345829721c1a3194b2121b8daf9c2e120d2f917858a9c864cd8e1b0fa5bcf"
    )


def test_section_3_sorts_over_the_escaped_paths_not_the_raw_ones() -> None:
    """SS5.4 section 3 carries the identical rule and gets the identical pin."""
    digest = fingerprint("C6", ["appdb:student:s1"], [JOINER_PATH, PIVOT_PATH], C6_GRADE_VALUES)
    assert digest == _digest(SECTION_3_PAYLOAD)
    assert digest == "dc4596c9f53dfcefb878489b889e48db4133e87143a9cd428a10c6760ca1b1b1"
    assert digest != _digest(SECTION_3_RAW_ORDER_PAYLOAD)
    assert (
        _digest(SECTION_3_RAW_ORDER_PAYLOAD)
        == "70f75b0cb33a34fbab9f334432691ccc1524e0865bf8625421ad4d9f58cfa5ba"
    )


@pytest.mark.parametrize(
    ("conflict_type", "refs", "paths", "values"),
    [
        ("C8", [JOINER_REF, PIVOT_REF], [], C8_VALUES),
        ("C6", ["appdb:student:s1"], [JOINER_PATH, PIVOT_PATH], C6_GRADE_VALUES),
    ],
    ids=["section-2", "section-3"],
)
def test_the_escaped_order_is_reached_from_either_input_order(
    conflict_type: str, refs: list[str], paths: list[str], values: dict
) -> None:
    """Both sections sort, so neither digest may depend on the caller's input order --
    the property that makes the fingerprint a stable idempotency key (R16)."""
    assert fingerprint(conflict_type, refs, paths, values) == fingerprint(
        conflict_type, list(reversed(refs)), list(reversed(paths)), values
    )
