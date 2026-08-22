"""`norm_enum` -- table-driven from the committed vocabularies (contract SS2.1, SS2.3).

Two behaviours are pinned and tested separately, because conflating them is how a
`unchecked` verdict turns into a crash:

* an unmappable but well-formed **value** returns `None` and never raises
  (SS5.1/SS5.8 `unmapped_enum`);
* an unknown **field** raises -- that is a caller bug, not dirty data.
"""

from __future__ import annotations

import unicodedata

import pytest

from recon.normalize import (
    ENUM_FIELDS,
    GRADE_VALUES,
    STATE_VALUES,
    enum_values,
    norm_enum,
)

GRADE_CASES: list[tuple[str, str]] = [
    ("Grade 4", "4"),
    ("grade 4", "4"),
    ("grade4", "4"),
    ("GRADE_4", "4"),
    ("4", "4"),
    ("4th", "4"),
    ("Fourth", "4"),
    ("  `4th` ", "4"),
    ("Grade 12", "12"),
    ("12th", "12"),
    ("Twelfth", "12"),
    ("Kindergarten", "K"),
    ("kindergarten", "K"),
    ("KG", "K"),
    ("kg", "K"),
    ("K", "K"),
    ("Pre-K", "PK"),
    ("pre k", "PK"),
    ("PreK", "PK"),
    ("PK", "PK"),
    ("Pre-Kindergarten", "PK"),
]

STATE_CASES: list[tuple[str, str]] = [
    ("TX", "TX"),
    ("Tx", "TX"),
    ("tx", "TX"),
    ("TEXAS", "TX"),
    ("texas", "TX"),
    ("  Texas  ", "TX"),
    ("'TX'", "TX"),
    ("New York", "NY"),
    ("new-york", "NY"),
    ("NY", "NY"),
]

PROGRAM_CASES: list[tuple[str, str]] = [
    ("Lower School", "Lower School"),
    ("lower school", "Lower School"),
    ("LOWER_SCHOOL", "Lower School"),
    ("lower-school", "Lower School"),
    ("  Middle School ", "Middle School"),
    ("upperschool", "Upper School"),
    ("summer_academy", "Summer Academy"),
]

DEAL_STAGE_CASES: list[tuple[str, str]] = [
    ("New Lead", "New Lead"),
    ("new lead", "New Lead"),
    ("NEW_LEAD", "New Lead"),
    ("Application Submitted", "Application Submitted"),
    ("Deposit Received", "Deposit Received"),
    ("CLOSED_WON", "Closed Won"),
    ("closed won", "Closed Won"),
    ("closed-won", "Closed Won"),
    ("Closed Lost", "Closed Lost"),
    ("Refunded", "Refunded"),
    ("Waitlisted", "Waitlisted"),
]

STAGE_CASES: list[tuple[str, str]] = [
    ("prospect", "prospect"),
    ("Prospect", "prospect"),
    ("deposit_paid", "deposit_paid"),
    ("Deposit Paid", "deposit_paid"),
    ("DEPOSIT-PAID", "deposit_paid"),
    ("withdrawn", "withdrawn"),
    ("refunded", "refunded"),
]

STATUS_CASES: list[tuple[str, str]] = [
    ("prospect", "prospect"),
    ("Active", "active"),
    ("ACTIVE", "active"),
    ("enrolled", "enrolled"),
    ("withdrawn", "withdrawn"),
]

LIFECYCLE_CASES: list[tuple[str, str]] = [
    ("subscriber", "subscriber"),
    ("Subscriber", "subscriber"),
    ("lead", "lead"),
    ("marketingqualifiedlead", "marketingqualifiedlead"),
    ("MarketingQualifiedLead", "marketingqualifiedlead"),
    ("marketing qualified lead", "marketingqualifiedlead"),
    ("MQL", "MQL"),
    ("mql", "MQL"),
    ("salesqualifiedlead", "salesqualifiedlead"),
    ("SQL", "SQL"),
    ("sql", "SQL"),
    ("opportunity", "opportunity"),
    ("customer", "customer"),
    ("evangelist", "evangelist"),
    ("other", "other"),
]

ALL_CASES = (
    [("grade", raw, expected) for raw, expected in GRADE_CASES]
    + [("state", raw, expected) for raw, expected in STATE_CASES]
    + [("program", raw, expected) for raw, expected in PROGRAM_CASES]
    + [("pipeline", raw, expected) for raw, expected in PROGRAM_CASES]
    + [("deal_stage", raw, expected) for raw, expected in DEAL_STAGE_CASES]
    + [("stage", raw, expected) for raw, expected in STAGE_CASES]
    + [("status", raw, expected) for raw, expected in STATUS_CASES]
    + [("lifecycle_stage", raw, expected) for raw, expected in LIFECYCLE_CASES]
)


@pytest.mark.parametrize(("field", "raw", "expected"), ALL_CASES)
def test_dirty_variants_map_to_the_canonical_value(field: str, raw: str, expected: str) -> None:
    assert norm_enum(field, raw) == expected


@pytest.mark.parametrize("field", ENUM_FIELDS)
def test_every_canonical_value_maps_to_itself(field: str) -> None:
    """Totality over the declared domain: no canonical value is unmappable."""
    for value in enum_values(field):
        assert norm_enum(field, value) == value


@pytest.mark.parametrize("field", ENUM_FIELDS)
@pytest.mark.parametrize("raw", ["", "   ", "not-a-value", "zzz", "13th", "Grade 99", "`?`"])
def test_unmappable_value_returns_none_and_never_raises(field: str, raw: str) -> None:
    """SS5.1/SS5.8: this path becomes `verdict='unchecked'`, never a crash."""
    assert norm_enum(field, raw) is None


@pytest.mark.parametrize("field", ENUM_FIELDS)
def test_none_and_non_strings_return_none(field: str) -> None:
    assert norm_enum(field, None) is None
    assert norm_enum(field, 4) is None  # type: ignore[arg-type]


def test_unknown_field_raises() -> None:
    """An undeclared field is a caller bug, not dirty data (SS2.3)."""
    with pytest.raises(ValueError, match="unknown enum field"):
        norm_enum("nope", "x")
    with pytest.raises(ValueError, match="unknown enum field"):
        enum_values("nope")


def test_lifecycle_vocabulary_keeps_mql_and_marketingqualifiedlead_distinct() -> None:
    """SS2.3 lists both spellings as separate committed vocabulary entries."""
    assert norm_enum("lifecycle_stage", "MQL") == "MQL"
    assert norm_enum("lifecycle_stage", "marketingqualifiedlead") == "marketingqualifiedlead"
    assert norm_enum("lifecycle_stage", "SQL") == "SQL"
    assert norm_enum("lifecycle_stage", "salesqualifiedlead") == "salesqualifiedlead"


def test_pipeline_and_program_share_one_table() -> None:
    """SS2.3: `deal.pipeline` is the same four values with the same tolerance."""
    assert enum_values("pipeline") == enum_values("program")
    for raw, expected in PROGRAM_CASES:
        assert norm_enum("pipeline", raw) == norm_enum("program", raw) == expected


def test_state_is_the_fifty_state_map() -> None:
    assert len(enum_values("state")) == 50
    assert len(set(enum_values("state"))) == 50


def test_the_variant_table_refuses_to_be_ambiguous() -> None:
    """A future contributor adding a colliding dirty variant fails at import, not
    silently at detection time."""
    from recon.normalize import _build_variants

    _build_variants({"A": ("a-1",), "B": ("b-1",)})
    with pytest.raises(ValueError, match="ambiguous enum variant"):
        _build_variants({"A": ("shared",), "B": ("SHARED",)})
    with pytest.raises(ValueError, match="empty variant key"):
        _build_variants({"A": ("  ",)})


# =====================================================================================
# SS2.3 ruling 11 -- the grade variant families are a CLOSED set
# =====================================================================================

#: Restated from the contract, not read out of the module.
GRADE_FAMILIES: dict[str, tuple[str, ...]] = {
    "PK": ("PK", "Pre-K", "PreK", "Pre K", "Pre-Kindergarten", "Prekindergarten"),
    "K": ("K", "KG", "Kindergarten", "Grade K"),
    "1": ("1", "Grade 1", "1st", "first", "Grade 1st", "Grade first"),
    "2": ("2", "Grade 2", "2nd", "second", "Grade 2nd", "Grade second"),
    "3": ("3", "Grade 3", "3rd", "third", "Grade 3rd", "Grade third"),
    "4": ("4", "Grade 4", "4th", "fourth", "Grade 4th", "Grade fourth"),
    "5": ("5", "Grade 5", "5th", "fifth", "Grade 5th", "Grade fifth"),
    "6": ("6", "Grade 6", "6th", "sixth", "Grade 6th", "Grade sixth"),
    "7": ("7", "Grade 7", "7th", "seventh", "Grade 7th", "Grade seventh"),
    "8": ("8", "Grade 8", "8th", "eighth", "Grade 8th", "Grade eighth"),
    "9": ("9", "Grade 9", "9th", "ninth", "Grade 9th", "Grade ninth"),
    "10": ("10", "Grade 10", "10th", "tenth", "Grade 10th", "Grade tenth"),
    "11": ("11", "Grade 11", "11th", "eleventh", "Grade 11th", "Grade eleventh"),
    "12": ("12", "Grade 12", "12th", "twelfth", "Grade 12th", "Grade twelfth"),
}


@pytest.mark.parametrize("canonical", sorted(GRADE_FAMILIES))
def test_every_committed_grade_variant_maps_to_its_canonical(canonical: str) -> None:
    """SS2.3 ruling 11: SS2.3's eight examples are examples; THIS is the committed set."""
    for variant in GRADE_FAMILIES[canonical]:
        assert norm_enum("grade", variant) == canonical
        # ...and folding is case- and separator-insensitive over the same family, which
        # is why `Grade 4`, `grade4`, `GRADE-4` and `grade_4` are one key, not four rows.
        for dirty in (
            variant.upper(),
            variant.lower(),
            f"  {variant}  ",
            variant.replace(" ", "_"),
        ):
            assert norm_enum("grade", dirty) == canonical


def test_the_grade_families_cover_every_canonical_value_and_no_others() -> None:
    assert sorted(GRADE_FAMILIES) == sorted(GRADE_VALUES)
    assert len(GRADE_FAMILIES) == 14


@pytest.mark.parametrize(
    "outside",
    [
        # well-formed grades a careless generator might reach for -- all OUTSIDE the set
        "Yr 4",
        "Year 4",
        "Form IV",
        "4e",
        "G4",
        "Gr. 4",
        "Grade Four A",
        "Reception",
        "Nursery",
        "Senior",
        "Freshman",
        "13",
        "Grade 13",
        "Pre-Prep",
    ],
)
def test_a_grade_outside_the_closed_set_normalizes_to_none(outside: str) -> None:
    """SS2.3 ruling 11: the generator may draw grade dirt from NOWHERE ELSE.

    A well-formed grade outside the set normalizes to `None`, which makes the `grade`
    comparison `unchecked` (SS5.1) rather than a comparison -- so a planted C6 becomes an
    unchecked non-event and its golden entry a silent false negative. This test is the
    check that the set is closed; adding a family here without adding it to the module
    (or vice versa) is the drift it exists to catch.
    """
    assert norm_enum("grade", outside) is None


# =====================================================================================
# SS2.3 ruling 12 -- exactly the 50 states: no DC, no territories
# =====================================================================================

FIFTY_STATES = [
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
]


def test_state_values_are_exactly_the_fifty_states() -> None:
    """SS2.3 ruling 12. Restated literally so the count is a claim, not a readback."""
    assert len(FIFTY_STATES) == 50
    assert sorted(STATE_VALUES) == sorted(FIFTY_STATES)
    assert len(STATE_VALUES) == 50
    assert len(set(STATE_VALUES)) == 50


@pytest.mark.parametrize(
    "excluded",
    [
        "DC",  # the one the generator must not emit
        "PR",
        "GU",
        "VI",
        "AS",
        "MP",  # territories
        "AA",
        "AE",
        "AP",  # military
        "ON",
        "BC",
        "QC",  # Canadian provinces
    ],
)
def test_no_territory_or_district_is_in_the_committed_map(excluded: str) -> None:
    """SS2.3 ruling 12: the generator must not emit `DC`. `crm.contact.state` exists
    solely to exercise a committed normalizer under unit test (SS1.1 reason (c)), so a
    value that normalizes to `None` makes the field's only reason for existing
    untestable."""
    assert excluded not in STATE_VALUES
    assert norm_enum("state", excluded) is None


@pytest.mark.parametrize(
    "district_name", ["District of Columbia", "Washington DC", "Washington, D.C.", "Puerto Rico"]
)
def test_the_district_and_territory_names_do_not_map_either(district_name: str) -> None:
    assert norm_enum("state", district_name) is None


def test_each_state_accepts_exactly_its_code_and_its_full_name() -> None:
    for code in STATE_VALUES:
        assert norm_enum("state", code) == code
        assert norm_enum("state", code.lower()) == code
        assert norm_enum("state", f" {code} ") == code
    # the contract's own worked example
    for variant in ("TX", "Tx", "TEXAS", "texas", "  Texas ", "tExAs"):
        assert norm_enum("state", variant) == "TX"
    assert norm_enum("state", "New-Hampshire") == "NH"


# =====================================================================================
# SS2.3 ruling 12 -- ALL FIFTY full English names, restated literally
# =====================================================================================
#
# `test_each_state_accepts_exactly_its_code_and_its_full_name` above asserts the code,
# the lower-cased code and the padded code for all fifty, then `TX` and `New-Hampshire`
# as one-offs -- so 48 of the 50 full names are unbound and misspelling any of them
# ("Massachusets", "Pennsylvannia") silently deletes that variant. The contract says
# "each code accepts exactly TWO variants -- the code itself and the state's full
# English name", so both halves are stated here for all fifty.

STATE_FULL_NAMES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}


def test_the_full_name_table_covers_every_committed_code_exactly_once() -> None:
    assert sorted(STATE_FULL_NAMES) == sorted(STATE_VALUES)
    assert len(STATE_FULL_NAMES) == 50
    assert len(set(STATE_FULL_NAMES.values())) == 50


@pytest.mark.parametrize(("code", "name"), sorted(STATE_FULL_NAMES.items()), ids=lambda x: x)
def test_every_state_full_name_resolves_to_its_code(code: str, name: str) -> None:
    """SS2.3 ruling 12, the second of the two committed variants, for all fifty."""
    assert norm_enum("state", name) == code


@pytest.mark.parametrize(("code", "name"), sorted(STATE_FULL_NAMES.items()), ids=lambda x: x)
def test_every_state_full_name_resolves_under_every_committed_folding(code: str, name: str) -> None:
    """The folding is "the same as `grade`": case, whitespace, `_` and `-` (SS2.3).

    Multi-word names are where this bites -- `New-Hampshire`, `new_mexico`,
    `RHODEISLAND` and `  South   Carolina ` are all one variant key, not four rows.
    """
    variants = [
        name,
        name.lower(),
        name.upper(),
        name.casefold(),
        f"  {name}  ",
        name.replace(" ", "-"),
        name.replace(" ", "_"),
        name.replace(" ", ""),
        f"`{name}`",
        name.swapcase(),
    ]
    for variant in variants:
        assert norm_enum("state", variant) == code, variant


@pytest.mark.parametrize("name", sorted(STATE_FULL_NAMES.values()), ids=lambda x: x)
def test_a_misspelled_state_name_does_not_resolve(name: str) -> None:
    """The other half of the claim: the table is exact, so a near-miss is `None`.

    Without this, a full-name table could accept a superset and the "exactly two
    variants" clause would be unbound in the other direction.
    """
    assert norm_enum("state", name + "x") is None
    assert norm_enum("state", "New " + name) is None


# =====================================================================================
# SS2.3 -- the enum variant key FOLDS UNICODE, and both folding decisions are pinned
# =====================================================================================
#
# `_variant_key` NFKD-normalizes, strips combining marks, and casefolds a SECOND time.
# Both decisions are invisible to ASCII inputs, so the whole suite -- and `st.text()`,
# which essentially never emits these characters -- stays green if either is degraded.


def test_the_variant_key_casefolds_after_nfkd_expansion() -> None:
    """NFKD of a compatibility character can yield UPPER CASE (`U+33A9` -> `Pa`), so a
    single casefold leaves the lookup key upper-cased and the value falls off the table
    into `None` -- i.e. `verdict='unchecked'`, and a planted C6 becomes a silent false
    negative (SS5.1)."""
    folded = unicodedata.normalize("NFKD", "㎩".casefold())
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    assert stripped == "Pa"  # what ONE casefold leaves behind: not the table key
    assert stripped not in {"pa"}
    assert norm_enum("state", "㎩") == "PA"
    assert norm_enum("state", "㎒") is None  # and an expansion that is NOT a state stays None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Téxas", "TX"),
        ("TÉXAS", "TX"),
        ("Cãlifornia", "CA"),
        ("Fĺorida", "FL"),
        ("Néw Yórk", "NY"),
    ],
    ids=["Texas", "TEXAS", "California", "Florida", "New York"],
)
def test_the_variant_key_uses_NFKD_so_accents_actually_decompose(raw: str, expected: str) -> None:
    """SS2.3 folds accents by DECOMPOSING (NFKD) and dropping the combining marks.

    NFK**C** composes instead: `'Téxas'` stays `'téxas'` with a precomposed `é`, the
    combining-mark strip becomes a no-op, the key misses the table and `norm_enum`
    returns `None`. That is the accent dirt A.3 sprinkles through the CRM export turning
    a mappable value into `unchecked` -- and it is invisible to every ASCII test.
    """
    assert norm_enum("state", raw) == expected
    composed = unicodedata.normalize("NFKC", raw.casefold())
    assert any(unicodedata.combining(ch) for ch in unicodedata.normalize("NFKD", raw))
    assert not any(unicodedata.combining(ch) for ch in composed)  # NFKC leaves nothing to strip
    assert composed.replace(" ", "") not in {expected.casefold()}


def test_accent_folding_reaches_every_enum_field_not_just_state() -> None:
    assert norm_enum("program", "Lówer School") == "Lower School"
    assert norm_enum("grade", "Fóurth") == "4"
    assert norm_enum("deal_stage", "Clósed Wón") == "Closed Won"
    assert norm_enum("status", "enrólled") == "enrolled"
