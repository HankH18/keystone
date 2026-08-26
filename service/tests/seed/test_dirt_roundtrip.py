"""`recon/seed/dirt.py`'s round-trip property, over the whole variant space (A.3, R23).

The generator's promise about dirty data is one sentence: *every variant it emits
normalizes back to the clean canonical.* A.3 asks for names with trailing whitespace
and stray quotes, mixed casing, gmail dot/`+alias` variants and case/format-variant
enum values, and then says "these are not all conflicts; your normalization must
survive them without flagging clean records". A variant that does **not** round-trip
is not dirt -- it is a manufactured false positive (an email that normalizes to a
different mailbox becomes a C4 nobody planted) or a silent false negative (a grade
outside the closed family normalizes to `None`, the comparison turns `unchecked`, and
a planted C6 disappears).

No test file imported this module at all. It has guards -- `assert_enum_dirt_is_closed`,
`assert_name_dirt_is_lossless`, `assert_email_dirt_is_lossless` -- and they run inside
the generator on the values a *particular* seed happened to draw. That is a sample.
This is the space: every grade family member, every state code, every enum vocabulary,
and enough (seed, name, intensity) triples to reach all seven of `dirty_name`'s styles.

R23 is why the assertion is spelled `norm_name(dirty(x)) == norm_name(x)` rather than
`== x`: `recon/normalize.py` is the shared spec, and the property is that the two
spellings agree *after* the committed normalizer, not that the dirt is invertible.
"""

from __future__ import annotations

import pytest

# `_ENUM_CANONICAL` is the committed field -> canonical-values table. Imported, never
# re-typed: a second copy in a test keeps passing after the real one changes (R23).
from recon.normalize import _ENUM_CANONICAL as ENUM_CANONICAL
from recon.normalize import (
    GRADE_VALUES,
    QUOTE_CHARS,
    STATE_VALUES,
    norm_email,
    norm_enum,
    norm_name,
)
from recon.seed.corpora import FIRST_NAMES, GMAIL_DOMAINS_ORDERED, LAST_NAMES, PARENT_FIRST_NAMES
from recon.seed.dirt import (
    GRADE_VARIANT_FAMILY,
    assert_email_dirt_is_lossless,
    assert_enum_dirt_is_closed,
    assert_name_dirt_is_lossless,
    dirty_email,
    dirty_enum,
    dirty_grade,
    dirty_name,
    dirty_state,
    gmail_variant,
)
from recon.seed.rng import Rng

#: Fixed draws, never `random`: the generator's determinism rule applies to its tests.
SEEDS: tuple[int, ...] = (0, 1, 7, 20260822, 99991)

#: `dirty_name` picks one of SEVEN styles per unit of intensity, so a sample this size
#: reaches every one of them many times over. `test_the_sample_reaches_every_style`
#: asserts that rather than assuming it.
NAMES: tuple[str, ...] = (
    *FIRST_NAMES[:12],
    *LAST_NAMES[:12],
    *PARENT_FIRST_NAMES[:6],
    # the shapes the styles actually act on: an accentable vowel, a hyphen, a space
    "Aeliana",
    "Fairbank-Mead",
    "Van Doren",
    "O",
)


# ---------------------------------------------------------------------------
# names -- the round trip the task names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("intensity", (1, 2, 3, 4))
def test_name_dirt_normalizes_back_to_the_clean_name(seed: int, intensity: int) -> None:
    """`norm_name(dirty_name(x)) == norm_name(x)` across the variant space.

    Every name in the generator's own corpora, at every intensity the builder uses
    and then some, under five fixed PRNG streams.
    """
    rng = Rng(seed)
    for clean in NAMES:
        dirty = dirty_name(rng, clean, intensity=intensity)
        assert norm_name(dirty) == norm_name(clean), (
            f"name dirt {dirty!r} does not normalize back to {clean!r}: "
            f"{norm_name(dirty)!r} != {norm_name(clean)!r}"
        )
        # the module's own guard agrees, so the property and the guard cannot drift
        assert_name_dirt_is_lossless(dirty, clean)


def test_the_sample_reaches_every_style_dirty_name_can_produce() -> None:
    """The property above is only worth its breadth if the breadth is real.

    `dirty_name` branches seven ways. A round-trip test that happened to draw two of
    them would read exactly like this one and prove a quarter as much, so the seven
    outcomes are enumerated from the output rather than assumed: a trailing space, a
    leading space, both kinds of quoting, upper, lower, and an accent fold.
    """
    seen: set[str] = set()
    for seed in SEEDS:
        rng = Rng(seed)
        for clean in NAMES:
            for _ in range(40):
                dirty = dirty_name(rng, clean, intensity=1)
                if dirty == clean:
                    continue  # style 5 on a name with no accentable vowel
                if dirty.endswith(" "):
                    seen.add("trailing space")
                if dirty.startswith(" "):
                    seen.add("leading space")
                if dirty[0] in QUOTE_CHARS and dirty[-1] == dirty[0]:
                    seen.add("wrapped in quotes")
                elif dirty[-1] in QUOTE_CHARS:
                    seen.add("trailing quote")
                if dirty == clean.upper() and clean.upper() != clean:
                    seen.add("upper")
                if dirty == clean.lower() and clean.lower() != clean:
                    seen.add("lower")
                if any(ch in dirty for ch in "áéíóú"):
                    seen.add("accent")
    assert seen == {
        "trailing space",
        "leading space",
        "wrapped in quotes",
        "trailing quote",
        "upper",
        "lower",
        "accent",
    }, sorted(seen)


def test_name_dirt_never_changes_the_spelling() -> None:
    """SS2.1: dirt re-spells a name, it never renames it.

    The normalized form is what a rule compares, so a "dirty" variant whose letters
    differ (`Jon` -> `John`) would be a planted C14 that nobody planted. Asserted on
    the *letters*, independently of the round trip, because the round trip is exactly
    the property that would hide a compensating pair of bugs in `norm_name`.
    """
    rng = Rng(20260822)
    for clean in NAMES:
        for intensity in (1, 2, 3):
            dirty = dirty_name(rng, clean, intensity=intensity)
            stripped = "".join(
                ch for ch in dirty if ch.isalpha() or ch in "-" or ch.isspace()
            ).strip()
            folded = "".join(
                {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u"}.get(ch, ch)
                for ch in stripped.lower()
            )
            assert " ".join(folded.split()) == " ".join(clean.lower().split()), (
                f"{dirty!r} is not a re-spelling of {clean!r}"
            )


# ---------------------------------------------------------------------------
# emails
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_email_dirt_normalizes_back_to_the_clean_address(seed: int) -> None:
    """Surrounding quotes/whitespace and casing only -- `norm_email` undoes all three."""
    rng = Rng(seed)
    addresses = [
        f"{local}@{domain}"
        for local in ("brenmar-fairbank-mead", "a.b", "quen.solis+x")
        for domain in ("example.test", "mail.test", *GMAIL_DOMAINS_ORDERED)
    ]
    for address in addresses:
        for _ in range(12):
            dirty = dirty_email(rng, address)
            assert norm_email(dirty) == norm_email(address), (
                f"email dirt {dirty!r} does not normalize back to {address!r}"
            )
            assert_email_dirt_is_lossless(dirty, address)


@pytest.mark.parametrize("seed", SEEDS)
def test_gmail_variants_normalize_back_and_are_gmail_only(seed: int) -> None:
    """`G4`: dot/`+alias` variation is a gmail rule, so the dirt is gmail-scoped.

    Universal dot-stripping would collapse two distinct mailboxes at any other
    domain, which is a manufactured C4. The refusal is asserted, not assumed.
    """
    rng = Rng(seed)
    for domain in GMAIL_DOMAINS_ORDERED:
        address = f"brenmar.fairbank@{domain}"
        for _ in range(12):
            dirty = gmail_variant(rng, address)
            assert norm_email(dirty) == norm_email(address), dirty
    with pytest.raises(ValueError, match="G4"):
        gmail_variant(Rng(1), "somebody@example.test")


# ---------------------------------------------------------------------------
# enums -- the whole closed set, not a sample
# ---------------------------------------------------------------------------


def test_every_committed_grade_variant_normalizes_to_its_canonical() -> None:
    """`GRADE_VARIANT_FAMILY` exhaustively, before any PRNG is involved.

    SS2.3 ruling 11 makes the families a CLOSED set; this walks the whole set, so a
    member that stopped mapping fails here whether or not a seed happens to draw it.
    """
    assert set(GRADE_VARIANT_FAMILY) == set(GRADE_VALUES)
    for canonical, family in sorted(GRADE_VARIANT_FAMILY.items()):
        assert canonical in family
        for variant in family:
            for spelling in (variant, variant.lower(), variant.upper(), variant.title()):
                assert norm_enum("grade", spelling) == canonical, (
                    f"grade variant {spelling!r} normalizes to "
                    f"{norm_enum('grade', spelling)!r}, not {canonical!r}"
                )


@pytest.mark.parametrize("seed", SEEDS)
def test_dirty_grade_and_dirty_state_stay_inside_their_families(seed: int) -> None:
    """Every canonical value, drawn repeatedly, must round-trip through `norm_enum`."""
    rng = Rng(seed)
    for canonical in GRADE_VALUES:
        for _ in range(8):
            value = dirty_grade(rng, canonical)
            assert norm_enum("grade", value) == canonical, value
            assert_enum_dirt_is_closed("grade", value, canonical)
    for code in STATE_VALUES:
        for _ in range(8):
            value = dirty_state(rng, code)
            assert norm_enum("state", value) == code, value
            assert_enum_dirt_is_closed("state", value, code)


@pytest.mark.parametrize("seed", SEEDS)
def test_dirty_enum_round_trips_for_every_field_and_every_value(seed: int) -> None:
    """The whole enum table: every field `norm_enum` knows, every canonical value.

    `_ENUM_CANONICAL` is imported rather than re-typed. A second copy of a committed
    table in a test is the R23 generator/detector drift the shared-module rule exists
    to prevent, one level down -- it would keep passing after the real table changed.
    """
    rng = Rng(seed)
    assert ENUM_CANONICAL, "the enum table is empty; the import is wrong"
    for field, values in sorted(ENUM_CANONICAL.items()):
        for canonical in values:
            for _ in range(6):
                value = dirty_enum(rng, field, canonical)
                assert norm_enum(field, value) == canonical, (
                    f"{field} dirt {value!r} normalizes to {norm_enum(field, value)!r}, "
                    f"not {canonical!r}"
                )
                assert_enum_dirt_is_closed(field, value, canonical)


def test_the_closed_set_guard_actually_refuses_an_outside_value() -> None:
    """`assert_enum_dirt_is_closed` is the generator's own failure path; drive it.

    `"Yr 4"` is A.3's own example of a well-formed value outside the committed family:
    it normalizes to `None`, which turns a comparison `unchecked` and converts a
    planted C6 into a false negative rather than into a loud failure.
    """
    assert norm_enum("grade", "Yr 4") is None
    with pytest.raises(ValueError, match="CLOSED set"):
        assert_enum_dirt_is_closed("grade", "Yr 4", "4")


def test_the_name_and_email_guards_actually_refuse_a_lossy_variant() -> None:
    """The other two guards, likewise: a spelling change must raise, not pass."""
    with pytest.raises(ValueError, match="does not normalize back"):
        assert_name_dirt_is_lossless("Jonathan", "Jon")
    with pytest.raises(ValueError, match="does not normalize back"):
        assert_email_dirt_is_lossless("other@example.test", "someone@example.test")


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_the_same_seed_draws_the_same_dirt() -> None:
    """`G30`: dirt is on a graded path, so it is a pure function of the PRNG stream."""

    def draw() -> list[str]:
        rng = Rng(20260822)
        out: list[str] = []
        for clean in NAMES[:8]:
            out.append(dirty_name(rng, clean, intensity=2))
            out.append(dirty_email(rng, f"{clean.lower()}@example.test"))
            out.append(dirty_grade(rng, "4"))
            out.append(dirty_state(rng, "TX"))
        return out

    assert draw() == draw()
