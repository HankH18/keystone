"""`norm_name` -- A.3 name dirt (contract SS2.1).

Hard requirement: dirt normalizes away, different spellings never merge.
"""

from __future__ import annotations

import unicodedata

import pytest

from recon.normalize import QUOTE_CHARS, norm_name

DIRT_CASES: list[tuple[str, str]] = [
    # trailing / leading whitespace and internal runs
    ("  Maria  ", "maria"),
    ("Maria   Elena", "maria elena"),
    ("\tMaria\nElena ", "maria elena"),
    # NBSP folds to a plain space under NFKD
    ("Maria Elena", "maria elena"),  # noqa: RUF001
    # mixed casing
    ("MARIA", "maria"),
    ("mArIa", "maria"),
    # stray backticks and quotes
    ("`Maria`", "maria"),
    ('"Maria"', "maria"),
    ("'Maria'", "maria"),
    ("Mar`ia", "maria"),
    ("‘Maria’", "maria"),  # noqa: RUF001 - curly quotes are the dirt under test
    ("O'Brien", "obrien"),
    # accents (NFKD-folded)
    ("José", "jose"),
    ("JOSÉ", "jose"),
    ("José", "jose"),  # decomposed form folds to the same thing
    ("García", "garcia"),
    ("Ångström", "angstrom"),
    ("Müller", "muller"),
    ("Renée", "renee"),
    # combinations
    ("  `José   García` ", "jose garcia"),
]


@pytest.mark.parametrize(("raw", "expected"), DIRT_CASES)
def test_dirt_normalizes(raw: str, expected: str) -> None:
    assert norm_name(raw) == expected


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Jon", "John"),
        ("Katherine", "Catherine"),
        ("Ann", "Anne"),
        ("Steven", "Stephen"),
        ("Lee", "Li"),
    ],
)
def test_different_spellings_never_merge(left: str, right: str) -> None:
    """NEGATIVE case: SS2.1 -- `Jon` != `John`. No fuzzy folding, ever."""
    assert norm_name(left) != norm_name(right)


@pytest.mark.parametrize("raw", [None, "", "   ", "``", " '' ", '""'])
def test_absent_values_stay_absent(raw: str | None) -> None:
    assert norm_name(raw) is None


def test_case_and_accent_variants_of_one_name_agree() -> None:
    variants = ["José", " jose ", "JOSÉ", "`José`", "José"]
    assert len({norm_name(v) for v in variants}) == 1


def test_non_string_input_is_a_programming_error() -> None:
    with pytest.raises(TypeError):
        norm_name(42)  # type: ignore[arg-type]


# =====================================================================================
# SS2.1 ruling 7 -- QUOTE_CHARS is the committed SEVEN-character set
# =====================================================================================


def test_quote_chars_is_the_committed_set_verbatim() -> None:
    """SS2.1 ruling 7. Restated literally from the contract rather than recomputed from
    the module: a test that reads the constant it is checking proves nothing.

    The four CURLY quotes are part of the committed set, not an implementer's addition.
    A.3 sprinkles typographic quotes through the CRM export, and a three-character set
    (`"`, `'`, backtick) leaves every one of them in place -- so a name that arrived as
    a smart-quoted value would never normalize onto its clean twin, and its `namedob`
    match key would miss.
    """
    assert QUOTE_CHARS == "\"'`‘’“”"  # noqa: RUF001 - curly quotes ARE the committed dirt
    assert len(QUOTE_CHARS) == 7
    assert list(QUOTE_CHARS) == ['"', "'", "`", "‘", "’", "“", "”"]  # noqa: RUF001 - curly quotes ARE the committed dirt
    # the four curly quotes, named
    for curly in ("‘", "’", "“", "”"):  # noqa: RUF001 - curly quotes ARE the committed dirt
        assert curly in QUOTE_CHARS


@pytest.mark.parametrize("quote", list(QUOTE_CHARS))
def test_every_committed_quote_character_is_removed_from_a_name(quote: str) -> None:
    assert norm_name(f"Ma{quote}ria") == "maria"
    assert norm_name(f"{quote}Maria{quote}") == "maria"
    assert norm_name(f"{quote}{quote}Maria") == "maria"


# =====================================================================================
# SS2.1 ruling 6 -- `norm_name` removes quote characters ANYWHERE (not surrounding-only)
# =====================================================================================


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("O'Brien", "OBrien"),
        ("D`Angelo", "DAngelo"),
        ("Mc'Donald", "McDonald"),
        ("O’Brien", "OBrien"),  # noqa: RUF001 - a curly apostrophe is the CRM export shape
        ('Jo"seph', "Joseph"),
        ("O'Br'ien", "OBrien"),
    ],
)
def test_an_interior_quote_is_removed_so_the_two_spellings_are_one_name(
    left: str, right: str
) -> None:
    """SS2.1 ruling 6. `O'Brien` and `OBrien` are one person spelled two ways and MUST
    normalize equal -- `L2`/`L3` and the `namedob` match key both key on the normalized
    tuple, so a surviving apostrophe is a missed link, i.e. a false negative on every
    conflict that link would have surfaced.

    The mirror rule in `norm_email` is surrounding-only for exactly the opposite reason
    (see `tests/normalize/test_norm_email.py`). The asymmetry is deliberate.
    """
    assert norm_name(left) == norm_name(right)
    assert not any(quote in (norm_name(left) or "") for quote in QUOTE_CHARS)


def test_a_surrounding_only_strip_would_not_be_enough() -> None:
    """States the mutation directly: `.strip(QUOTE_CHARS)` leaves the interior quote."""
    assert "O'Brien".casefold().strip(QUOTE_CHARS) == "o'brien"
    assert norm_name("O'Brien") == "obrien"
    assert norm_name("O'Brien") != "O'Brien".casefold().strip(QUOTE_CHARS)


# =====================================================================================
# SS2.1 -- the SECOND casefold is load-bearing, not a redundant line
# =====================================================================================
#
# `norm_name` casefolds, NFKD-folds, then casefolds AGAIN. The second pass looks
# redundant to every reader and to every property test built on `st.text()`: hypothesis
# essentially never generates a character whose NFKD expansion contains an upper-case
# letter, so the whole suite stays green with the line deleted. These are those
# characters, stated explicitly.


@pytest.mark.parametrize(
    ("raw", "expected", "nfkd_expansion"),
    [
        ("㎒", "mhz", "MHz"),  # U+3392 SQUARE MHZ -- the case the docstring cites
        ("㎓", "ghz", "GHz"),  # U+3393 SQUARE GHZ
        ("㎐", "hz", "Hz"),  # U+3390 SQUARE HZ
        ("㎩", "pa", "Pa"),  # U+33A9 SQUARE PA
        ("㏇", "co.", "Co."),  # U+33C7 SQUARE CO
    ],
    ids=["MHz", "GHz", "Hz", "Pa", "Co"],
)
def test_nfkd_may_yield_upper_case_so_the_second_casefold_is_required(
    raw: str, expected: str, nfkd_expansion: str
) -> None:
    """SS2.1: `norm_name` output is always casefolded.

    Without the second pass these return their NFKD expansion verbatim -- `'MHz'`, not
    `'mhz'` -- so the normalizer emits an upper-case name, `match_keys`' `namedob` tuple
    stops matching its clean twin, and the `COMPARED_FIELDS` `name_*` rows manufacture a
    C6/C14 out of a casing difference SS2.1 says is dirt.
    """
    folded = unicodedata.normalize("NFKD", raw.casefold())
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    assert stripped == nfkd_expansion  # what ONE casefold leaves behind
    assert stripped != expected
    assert norm_name(raw) == expected


def test_idempotence_itself_breaks_without_the_second_casefold() -> None:
    """SS2.1 pins idempotence as a property (`f(f(x)) == f(x)`), and a single-casefold
    `norm_name` violates it on this input: `'㎒' -> 'MHz' -> 'mhz'`. Stated here as a
    table case because `st.text()` does not reach these code points."""
    assert norm_name("㎒") == "mhz"
    assert norm_name(norm_name("㎒")) == norm_name("㎒")
    single_pass = "".join(
        ch for ch in unicodedata.normalize("NFKD", "㎒".casefold()) if not unicodedata.combining(ch)
    )
    assert single_pass == "MHz"
    assert norm_name(single_pass) == "mhz" != single_pass  # not a fixed point


def test_the_second_casefold_still_leaves_ordinary_names_alone() -> None:
    """The pass is a no-op for every value that is not a compatibility character, which
    is why nothing else in the suite notices it."""
    for name in ("Maria", "José", "O'Brien", "  Ana  García "):
        assert norm_name(name) == norm_name(norm_name(name))
