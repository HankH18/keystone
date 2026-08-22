"""`norm_email` -- gmail-only local-part canonicalization (contract SS2.1, `G4`).

The negative cases are the load-bearing ones. Universal dot-stripping collapses
legitimately distinct addresses on ordinary domains, which false-positives against
the clean majority -- the hardest-graded population.
"""

from __future__ import annotations

import pytest

from recon.normalize import norm_email, norm_name

# (raw, expected) -- every A.3 dirt shape that reaches an email field.
DIRT_CASES: list[tuple[str, str]] = [
    # trailing / leading whitespace
    ("  parent@corp.com  ", "parent@corp.com"),
    ("\tparent@corp.com\n", "parent@corp.com"),
    # mixed casing
    ("Parent@Corp.COM", "parent@corp.com"),
    ("PARENT@CORP.COM", "parent@corp.com"),
    # stray quotes and backticks
    ("'parent@corp.com'", "parent@corp.com"),
    ('"parent@corp.com"', "parent@corp.com"),
    ("`parent@corp.com`", "parent@corp.com"),
    ("‘parent@corp.com’", "parent@corp.com"),  # noqa: RUF001 - curly quotes are the dirt under test
    (' `"parent@corp.com"` ', "parent@corp.com"),
    # gmail dots
    ("j.o.h.n.doe@gmail.com", "johndoe@gmail.com"),
    ("johndoe@gmail.com", "johndoe@gmail.com"),
    ("John.Doe@GMAIL.com", "johndoe@gmail.com"),
    # gmail +aliases
    ("johndoe+school@gmail.com", "johndoe@gmail.com"),
    ("john.doe+keystone+extra@gmail.com", "johndoe@gmail.com"),
    ("johndoe+@gmail.com", "johndoe@gmail.com"),
    # googlemail is the same mailbox namespace as gmail...
    ("j.o.h.n@googlemail.com", "john@googlemail.com"),
    ("john+alias@googlemail.com", "john@googlemail.com"),
    # ...but the domain itself is never rewritten
    ("john@googlemail.com", "john@googlemail.com"),
]

# The whole point: every other domain is byte-preserved apart from
# trim / casefold / quote-stripping.
NON_GMAIL_PRESERVED: list[tuple[str, str]] = [
    ("jane.doe@corp.com", "jane.doe@corp.com"),
    ("janedoe@corp.com", "janedoe@corp.com"),
    ("jane.doe+billing@corp.com", "jane.doe+billing@corp.com"),
    ("j.a.n.e@outlook.com", "j.a.n.e@outlook.com"),
    ("jane.doe@gmail.com.mx", "jane.doe@gmail.com.mx"),
    ("jane.doe@notgmail.com", "jane.doe@notgmail.com"),
    ("jane.doe@mail.gmail.com", "jane.doe@mail.gmail.com"),
]


@pytest.mark.parametrize(("raw", "expected"), DIRT_CASES)
def test_dirt_normalizes(raw: str, expected: str) -> None:
    assert norm_email(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), NON_GMAIL_PRESERVED)
def test_non_gmail_is_byte_preserved(raw: str, expected: str) -> None:
    assert norm_email(raw) == expected


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # a dot is the ONLY difference -- these are two different mailboxes
        ("jane.doe@corp.com", "janedoe@corp.com"),
        ("j.a.n.e@outlook.com", "jane@outlook.com"),
        ("a.b@school.edu", "ab@school.edu"),
        # a +suffix is the only difference on a non-gmail domain
        ("jane@corp.com", "jane+billing@corp.com"),
        # near-miss domains must not borrow gmail's rules
        ("jane.doe@notgmail.com", "janedoe@notgmail.com"),
        ("jane.doe@gmail.com.mx", "janedoe@gmail.com.mx"),
    ],
)
def test_non_gmail_addresses_differing_by_a_dot_do_not_collapse(left: str, right: str) -> None:
    """NEGATIVE case: collapsing these is the C4 false-positive machine."""
    assert norm_email(left) != norm_email(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("john.doe@gmail.com", "johndoe@gmail.com"),
        ("johndoe+a@gmail.com", "johndoe+b@gmail.com"),
        ("J.O.H.N.Doe+school@Gmail.COM", " 'johndoe@gmail.com' "),
        ("j.o.h.n@googlemail.com", "john@googlemail.com"),
    ],
)
def test_gmail_variants_collapse(left: str, right: str) -> None:
    assert norm_email(left) == norm_email(right)


def test_gmail_and_googlemail_are_not_merged_with_each_other() -> None:
    """Only the local part is canonicalized; the domain is never rewritten."""
    assert norm_email("john@gmail.com") != norm_email("john@googlemail.com")


@pytest.mark.parametrize("raw", [None, "", "   ", "''", " `` "])
def test_absent_values_stay_absent(raw: str | None) -> None:
    """A NULL `guardian2_email` must stay NULL, not become `""` and collide."""
    assert norm_email(raw) is None


def test_value_without_an_at_sign_is_preserved_apart_from_trim_and_casefold() -> None:
    assert norm_email("  NotAnEmail  ") == "notanemail"


def test_only_the_last_at_sign_separates_the_domain() -> None:
    """`rpartition` pins the split, so a stray `@` cannot change the domain."""
    assert norm_email("a.b@c@gmail.com") == "ab@c@gmail.com"
    assert norm_email("a.b@c@corp.com") == "a.b@c@corp.com"


def test_non_string_input_is_a_programming_error() -> None:
    with pytest.raises(TypeError):
        norm_email(42)  # type: ignore[arg-type]


# =====================================================================================
# SS2.1 ruling 6 -- quote handling is SURROUNDING-ONLY, and asymmetric with `norm_name`
# =====================================================================================


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # THE case: an apostrophe inside the local part is part of the mailbox.
        ("o'brien@corp.com", "obrien@corp.com"),
        ("O'Brien@Corp.com", "obrien@corp.com"),
        ("d'angelo@school.edu", "dangelo@school.edu"),
        # a backtick and a double quote, likewise
        ("o`brien@corp.com", "obrien@corp.com"),
        ('o"brien@corp.com', "obrien@corp.com"),
        # curly quotes are in QUOTE_CHARS too, and are equally significant inside
        ("o’brien@corp.com", "obrien@corp.com"),  # noqa: RUF001 - a curly quote is the dirt
        # ...even on gmail, where the LOCAL PART is canonicalized: dots and +aliases
        # are gmail's own semantics; a quote character is not.
        ("o'brien@gmail.com", "obrien@gmail.com"),
        # and in the domain
        ("brien@co'rp.com", "brien@corp.com"),
    ],
)
def test_an_interior_quote_is_significant_and_never_stripped(left: str, right: str) -> None:
    """NEGATIVE case, SS2.1 ruling 6. `norm_name` removes QUOTE_CHARS ANYWHERE so that
    `O'Brien` and `OBrien` are one person; `norm_email` must NOT, or two distinct
    mailboxes collapse onto one address.

    That is the same false-positive class as universal dot-stripping: it fires against
    the clean majority, which is the hardest-graded population, and it manufactures C4
    (`same-person-different-emails`) and C3 (`duplicate-by-email`) out of nothing.
    """
    assert norm_email(left) != norm_email(right)


def test_the_asymmetry_with_norm_name_is_deliberate_and_holds_in_both_directions() -> None:
    """Both halves of ruling 6 in one place, so neither can be "made consistent" with
    the other without a test going red."""
    # names: quotes removed ANYWHERE -> one person, two spellings, they link
    assert norm_name("O'Brien") == norm_name("OBrien") == "obrien"
    assert norm_name("D`Angelo") == norm_name("DAngelo")
    # emails: quotes stripped from the ENDS ONLY -> two mailboxes stay two
    assert norm_email("'o'brien@corp.com'") == "o'brien@corp.com"
    assert norm_email("o'brien@corp.com") != norm_email("obrien@corp.com")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # surrounding quotes ARE stripped -- both halves of the rule are pinned
        ("'o'brien@corp.com'", "o'brien@corp.com"),
        ('"o\'brien@corp.com"', "o'brien@corp.com"),
        ("`o'brien@corp.com`", "o'brien@corp.com"),
        ("  ``o'brien@corp.com''  ", "o'brien@corp.com"),
    ],
)
def test_surrounding_quotes_are_still_stripped(raw: str, expected: str) -> None:
    assert norm_email(raw) == expected


# =====================================================================================
# SS2.1 ruling 14 -- a value with no `@` gets trim / surrounding-quote strip / casefold
# =====================================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  NotAnEmail  ", "notanemail"),
        ("' Not.An.Email '", "not.an.email"),  # dots SURVIVE: no gmail logic, ever
        ("J.O.H.N.DOE", "j.o.h.n.doe"),
        ("john+school", "john+school"),  # no `+` truncation either
        ("`WEIRD.VALUE+X`", "weird.value+x"),
        ("o'brien", "o'brien"),  # interior quote survives here too
    ],
)
def test_a_value_with_no_at_sign_never_gets_gmail_logic(raw: str, expected: str) -> None:
    """SS2.1 ruling 14. A value with no domain has nothing to scope the gmail rules to,
    so applying them would be a guess -- and `j.o.h.n.doe` and `johndoe` would collapse
    on a value that is not even an address."""
    assert norm_email(raw) == expected


def test_dotted_and_undotted_values_with_no_at_sign_stay_distinct() -> None:
    assert norm_email("j.o.h.n.doe") != norm_email("johndoe")
    assert norm_email("john+school") != norm_email("john")


# =====================================================================================
# SS2.1 -- `norm_email` CASEFOLDS; `.lower()` is not the same function
# =====================================================================================
#
# `casefold()` and `lower()` agree on every ASCII address, so the whole suite -- and
# `st.text()`, which essentially never emits these characters -- stays green if the call
# is swapped. They disagree on exactly the characters that decide whether two spellings
# of one mailbox link.


@pytest.mark.parametrize(
    ("raw", "casefolded", "lowered"),
    [
        # U+00DF LATIN SMALL LETTER SHARP S -- casefold expands it to `ss`
        ("STRAßE@corp.com", "strasse@corp.com", "straße@corp.com"),
        ("STRASSE@corp.com", "strasse@corp.com", "strasse@corp.com"),
        # U+03A3 GREEK CAPITAL SIGMA -- `lower()` picks the FINAL form at word end
        ("ΟΔΟΣ@corp.com", "οδοσ@corp.com", "οδος@corp.com"),
        # U+1E9E LATIN CAPITAL LETTER SHARP S
        ("ẞ@corp.com", "ss@corp.com", "ß@corp.com"),
    ],
    ids=["sharp-s", "sharp-s-spelled-out", "final-sigma", "capital-sharp-s"],
)
def test_the_local_part_is_casefolded_not_lowercased(
    raw: str, casefolded: str, lowered: str
) -> None:
    """SS2.1 says **casefold**, and the two functions are not interchangeable.

    `'STRAßE@corp.com'.lower()` is `'straße@corp.com'`, which never matches the
    `'strasse@corp.com'` the same guardian typed on the other side; and `lower()` maps a
    trailing capital sigma to the FINAL sigma while casefold maps both sigmas to the
    same letter, so `lower()` makes one mailbox two depending on where the letter sits.
    Either way the household key (SS4.8 is `norm_email(guardian_email)`) splits a
    household in half, and `L2` misses the contact-student link.
    """
    assert norm_email(raw) == casefolded
    assert raw.lower() != casefolded or lowered == casefolded
    if lowered != casefolded:
        assert norm_email(raw) != lowered


def test_casefold_makes_the_two_spellings_of_one_mailbox_one_household_key() -> None:
    assert norm_email("STRAßE@corp.com") == norm_email("strasse@corp.com")
    assert norm_email("ΟΔΟΣ@corp.com") == norm_email("οδος@corp.com") == norm_email("οδοσ@corp.com")
    # ...and `.lower()` would not
    assert "STRAßE@corp.com".lower() != "strasse@corp.com".lower()
    assert "ΟΔΟΣ@corp.com".lower() != "οδοσ@corp.com".lower()


def test_casefolding_is_idempotent_on_these_characters() -> None:
    """SS2.1 pins idempotence for every `norm_*`; `.lower()` on the sigma case is not a
    fixed point once the address is re-normalized, so this is not merely cosmetic."""
    for raw in ("STRAßE@corp.com", "ΟΔΟΣ@corp.com", "ẞ@corp.com"):
        once = norm_email(raw)
        assert norm_email(once) == once
