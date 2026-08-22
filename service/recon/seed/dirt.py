"""A.3 dirty-data generation, drawn **only** from the closed variant families.

A.3 asks for names with trailing whitespace and stray quotes, mixed casing, gmail
dot/`+alias` variants, case/format-variant enum values, out-of-order timestamps on
~0.5% of records and realistic null rates. Every generator here produces a value
that the committed normalizer maps back to the clean canonical -- that is the whole
point: "these are not all conflicts; your normalization must survive them without
flagging clean records".

**The closed-set rule (SS2.3 rulings 11 and 12) is enforced by construction here.**
Enum dirt is produced by re-spelling a value that is already in the committed
vocabulary and then perturbing only case and the separators `_`, `-` and space --
which `_variant_key` deletes -- or by naming an explicit member of the committed
variant family. A well-formed grade outside the closed families (`"Yr 4"`) would
normalize to `None`, turn the comparison `unchecked`, and silently convert a planted
C6 into a false negative; `assert_enum_dirt_is_closed` re-checks every value this
module returns, so drawing outside the set fails loudly instead.

Email dirt is **surrounding-only** for quotes and whitespace (SS2.1 ruling 6:
`norm_email` strips the ends, never the interior) and dot/`+alias` variation is
emitted on gmail domains **only** (`G4`).
"""

from __future__ import annotations

from recon.normalize import _STATE_NAMES as STATE_FULL_NAMES
from recon.normalize import QUOTE_CHARS, norm_email, norm_enum, norm_name

from .corpora import GMAIL_DOMAINS_ORDERED
from .rng import Rng

__all__ = [
    "GRADE_VARIANT_FAMILY",
    "assert_enum_dirt_is_closed",
    "dirty_email",
    "dirty_enum",
    "dirty_grade",
    "dirty_name",
    "dirty_state",
    "gmail_variant",
]

#: SS2.3 ruling 11 -- the committed grade variant families, spelled out so the
#: generator can only ever draw from inside them.
_ORDINAL = {
    "1": ("1st", "first"),
    "2": ("2nd", "second"),
    "3": ("3rd", "third"),
    "4": ("4th", "fourth"),
    "5": ("5th", "fifth"),
    "6": ("6th", "sixth"),
    "7": ("7th", "seventh"),
    "8": ("8th", "eighth"),
    "9": ("9th", "ninth"),
    "10": ("10th", "tenth"),
    "11": ("11th", "eleventh"),
    "12": ("12th", "twelfth"),
}


def _grade_family(canonical: str) -> tuple[str, ...]:
    if canonical == "PK":
        return ("PK", "Pre-K", "PreK", "Pre K", "Pre-Kindergarten", "Prekindergarten")
    if canonical == "K":
        return ("K", "KG", "Kindergarten", "Grade K")
    suffix, word = _ORDINAL[canonical]
    return (
        canonical,
        f"Grade {canonical}",
        f"grade_{canonical}",
        f"GRADE-{canonical}",
        suffix,
        word.capitalize(),
        f"Grade {suffix}",
        f"Grade {word}",
    )


GRADE_VARIANT_FAMILY: dict[str, tuple[str, ...]] = {
    canonical: _grade_family(canonical)
    for canonical in ("PK", "K", *(str(n) for n in range(1, 13)))
}

_SEPARATOR_STYLES: tuple[str, ...] = (" ", "_", "-", "")
_CASE_STYLES: tuple[str, ...] = ("as-is", "lower", "upper", "title")


def _recase(value: str, style: str) -> str:
    if style == "lower":
        return value.lower()
    if style == "upper":
        return value.upper()
    if style == "title":
        return value.title()
    return value


def dirty_enum(rng: Rng, field: str, canonical: str) -> str:
    """A case / separator variant of `canonical` that `norm_enum(field, .)` maps back.

    Only `_`, `-`, space and case are perturbed, and `_variant_key` deletes exactly
    those -- so the result is inside the committed family by construction, never a
    guess at what the table might tolerate.
    """
    if field == "grade":
        return dirty_grade(rng, canonical)
    if field == "state":
        return dirty_state(rng, canonical)
    separator = rng.pick(_SEPARATOR_STYLES)
    style = rng.pick(_CASE_STYLES)
    spelled = canonical.replace("_", " ").replace("-", " ")
    parts = [part for part in spelled.split(" ") if part]
    joined = separator.join(parts) if len(parts) > 1 else parts[0]
    value = _recase(joined, style)
    if not value:  # pragma: no cover - vocabularies are never empty
        return canonical
    return value


def dirty_state(rng: Rng, canonical: str) -> str:
    """A member of `canonical`'s committed state variant family (SS2.3 ruling 12).

    SS2.3 pins that each code "accepts exactly **two** variants -- the code itself and
    the state's full English name", and A.3 names `TX, Tx, TEXAS` as the family the
    fixtures must exercise. Re-casing the two-letter code alone emitted only half of
    it, so `norm_enum('state', 'TEXAS')`'s branch was never exercised by fixture data.

    The full-name table is **imported from `recon/normalize.py`**, never re-typed here:
    a second copy of a 50-row committed table is exactly the R23 generator/detector
    drift the shared-module rule exists to prevent, and it would rot silently the first
    time one side was edited. `assert_enum_dirt_is_closed` re-checks that whatever comes
    out still normalizes back to `canonical`.
    """
    full_name = STATE_FULL_NAMES[canonical]
    variant = rng.pick((canonical, full_name))
    value = _recase(variant, rng.pick(_CASE_STYLES))
    # `_variant_key` deletes whitespace, `_` and `-`, so a separator swap inside a
    # multi-word name (`New Hampshire` -> `New-Hampshire`) is still inside the family.
    if " " in value and rng.chance(0.5):
        value = value.replace(" ", rng.pick(("-", "_")))
    return value


def dirty_grade(rng: Rng, canonical: str) -> str:
    """A member of `canonical`'s committed variant family (SS2.3 ruling 11), re-cased."""
    variant = rng.pick(GRADE_VARIANT_FAMILY[canonical])
    return _recase(variant, rng.pick(_CASE_STYLES))


_ACCENTS = {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú"}
_NAME_QUOTES = tuple(QUOTE_CHARS)


def dirty_name(rng: Rng, clean: str, *, intensity: int = 1) -> str:
    """A.3 name dirt: trailing whitespace, stray quotes, mixed casing, accents.

    `norm_name` casefolds, NFKD-folds accents, removes every `QUOTE_CHARS` character
    **anywhere**, collapses internal whitespace and trims -- so every variant here is
    equal to `norm_name(clean)`. It never changes a *spelling*: `Jon` never becomes
    `John` (SS2.1).
    """
    value = clean
    for _ in range(intensity):
        style = rng.randint(0, 6)
        if style == 0:
            value = f"{value} "
        elif style == 1:
            value = f" {value}"
        elif style == 2:
            quote = rng.pick(_NAME_QUOTES)
            value = f"{quote}{value}{quote}"
        elif style == 3:
            value = value.upper()
        elif style == 4:
            value = value.lower()
        elif style == 5:
            index = next(
                (i for i, ch in enumerate(value) if ch.lower() in _ACCENTS),
                None,
            )
            if index is not None:
                replacement = _ACCENTS[value[index].lower()]
                value = value[:index] + replacement + value[index + 1 :]
        else:
            value = f"{value}{rng.pick(_NAME_QUOTES)}"
    return value


def gmail_variant(rng: Rng, address: str) -> str:
    """A dot / `+alias` variant of a **gmail** address (A.3, `G4`).

    Never applied to any other domain: universal dot-stripping collapses distinct
    mailboxes, so `norm_email` scopes the rule to gmail and the generator must scope
    the dirt the same way.
    """
    local, _, domain = address.rpartition("@")
    if domain not in GMAIL_DOMAINS_ORDERED:
        raise ValueError(f"gmail_variant refuses non-gmail domain {domain!r} (G4)")
    style = rng.randint(0, 2)
    if style == 0 and len(local) > 3:
        cut = 1 + rng.randint(0, len(local) - 3)
        local = f"{local[:cut]}.{local[cut:]}"
    elif style == 1:
        local = f"{local}+{rng.pick(('school', 'billing', 'admissions', 'family'))}"
    else:
        if len(local) > 3:
            cut = 1 + rng.randint(0, len(local) - 3)
            local = f"{local[:cut]}.{local[cut:]}"
        local = f"{local}+{rng.pick(('school', 'billing'))}"
    return f"{local}@{domain}"


def dirty_email(rng: Rng, address: str) -> str:
    """A.3 email dirt that `norm_email` undoes: surrounding quotes/whitespace, casing.

    Interior quotes are deliberately **never** emitted: `norm_email` strips the ends
    only (SS2.1 ruling 6), so an interior quote would make two spellings of one
    mailbox normalize unequal and manufacture a false C4.
    """
    value = address
    style = rng.randint(0, 4)
    if style == 0:
        value = f" {value}"
    elif style == 1:
        value = f"{value} "
    elif style == 2:
        quote = rng.pick(_NAME_QUOTES)
        value = f"{quote}{value}{quote}"
    elif style == 3:
        local, _, domain = value.rpartition("@")
        value = f"{local.capitalize()}@{domain}"
    return value


def assert_enum_dirt_is_closed(field: str, raw: str, canonical: str) -> None:
    """Fail loudly if an emitted enum value falls outside the committed family."""
    mapped = norm_enum(field, raw)
    if mapped != canonical:
        raise ValueError(
            f"enum dirt {raw!r} for field {field!r} normalizes to {mapped!r}, "
            f"not {canonical!r} -- SS2.3's variant families are a CLOSED set and drawing "
            "outside them turns a planted conflict into an unchecked non-event"
        )


def assert_name_dirt_is_lossless(raw: str, clean: str) -> None:
    """Fail loudly if name dirt changed the normalized spelling."""
    if norm_name(raw) != norm_name(clean):
        raise ValueError(f"name dirt {raw!r} does not normalize back to {clean!r}")


def assert_email_dirt_is_lossless(raw: str, clean: str) -> None:
    """Fail loudly if email dirt changed the normalized address."""
    if norm_email(raw) != norm_email(clean):
        raise ValueError(f"email dirt {raw!r} does not normalize back to {clean!r}")
