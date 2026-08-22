"""Synthetic name / word corpora, built at import from committed syllable tables.

No real PII, ever (repo non-negotiable), and **no Faker**: Faker's own date and uuid
helpers are documented non-reproducible, and its name providers would still have to
be indexed by our PRNG to be deterministic -- at which point the provider is only a
word list. So the word list is committed here and indexed by the owned
`random.Random`, which is the property `G30` actually needs.

`NAME_CORPUS_MIN` (SS2.2) pins the size at 2,000 first names x 1,000 last names =
2x10^6 pairs. Against 43,175 name-bearing records that is a 2.2% load factor, which
is what makes `G5`'s rejection sampling terminate.
"""

from __future__ import annotations

from recon.reference import NAME_CORPUS_MIN_FIRST, NAME_CORPUS_MIN_LAST

__all__ = [
    "EMAIL_DOMAINS",
    "FIRST_NAMES",
    "GMAIL_DOMAINS_ORDERED",
    "LAST_NAMES",
    "NON_GMAIL_DOMAINS",
    "PARENT_FIRST_NAMES",
    "WORDS",
]

_FIRST_STEMS: tuple[str, ...] = (
    "Aden",
    "Bela",
    "Caro",
    "Dara",
    "Elia",
    "Fina",
    "Gale",
    "Hale",
    "Ilan",
    "Jora",
    "Kade",
    "Lena",
    "Mira",
    "Nola",
    "Orin",
    "Pela",
    "Quen",
    "Rina",
    "Soli",
    "Tara",
    "Umal",
    "Vira",
    "Wena",
    "Xane",
    "Yara",
    "Zeda",
    "Amri",
    "Bren",
    "Cyra",
    "Doro",
    "Emri",
    "Fela",
    "Grae",
    "Hesu",
    "Isla",
    "Jael",
    "Kora",
    "Liro",
    "Muna",
    "Neri",
)

_FIRST_TAILS: tuple[str, ...] = (
    "a",
    "ah",
    "an",
    "ar",
    "as",
    "ay",
    "e",
    "el",
    "en",
    "er",
    "es",
    "et",
    "ia",
    "ie",
    "in",
    "io",
    "is",
    "it",
    "iu",
    "ix",
    "o",
    "ol",
    "on",
    "or",
    "os",
    "ov",
    "oy",
    "ua",
    "ue",
    "ui",
    "us",
    "ux",
    "y",
    "ya",
    "ye",
    "yn",
    "yo",
    "ys",
    "za",
    "ze",
    "am",
    "at",
    "av",
    "id",
    "il",
    "im",
    "ir",
    "iv",
    "un",
    "ur",
)

_LAST_STEMS: tuple[str, ...] = (
    "Ashby",
    "Brant",
    "Calder",
    "Dunmore",
    "Ellery",
    "Fairbank",
    "Garrow",
    "Halloway",
    "Ingram",
    "Jessup",
    "Kellard",
    "Lindqvist",
    "Marchetti",
    "Norwood",
    "Ossory",
    "Pemberly",
    "Quarrier",
    "Ravensby",
    "Stanmore",
    "Thackery",
    "Underhill",
    "Vasquez",
    "Whitlock",
    "Xavier",
    "Yardley",
    "Zeller",
    "Ambrose",
    "Bexley",
    "Corbin",
    "Darrow",
    "Everton",
    "Falconer",
    "Granville",
    "Hollis",
    "Ironside",
    "Jarrow",
    "Kingsley",
    "Lowther",
    "Merrick",
    "Nashwood",
    "Orrick",
    "Pryce",
    "Quilling",
    "Redmayne",
    "Selwyn",
    "Tolliver",
    "Ulverton",
    "Vandermere",
    "Wexford",
    "Yorke",
)

_LAST_TAILS: tuple[str, ...] = (
    "",
    "-Bell",
    "-Cross",
    "-Dane",
    "-Ford",
    "-Gray",
    "-Hart",
    "-Keel",
    "-Lowe",
    "-Mead",
    "-Pike",
    "-Reed",
    "-Shaw",
    "-Vale",
    "-Wynn",
    "son",
    "ton",
    "field",
    "worth",
    "by",
)


def _build_first_names() -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for stem in _FIRST_STEMS:
        for tail in _FIRST_TAILS:
            name = f"{stem}{tail}"
            folded = name.casefold()
            if folded in seen:  # pragma: no cover - syllable tables are collision-free
                continue
            seen.add(folded)
            names.append(name)
    return tuple(names)


def _build_last_names() -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for stem in _LAST_STEMS:
        for tail in _LAST_TAILS:
            name = f"{stem}{tail}"
            folded = name.casefold()
            if folded in seen:  # pragma: no cover - syllable tables are collision-free
                continue
            seen.add(folded)
            names.append(name)
    return tuple(names)


FIRST_NAMES: tuple[str, ...] = _build_first_names()
LAST_NAMES: tuple[str, ...] = _build_last_names()

#: Guardian first names -- a disjoint slice, so a payer name never collides with a
#: student name in the `namedob` key space (`G5`).
PARENT_FIRST_NAMES: tuple[str, ...] = tuple(f"{stem}wen" for stem in _FIRST_STEMS) + tuple(
    f"{stem}mar" for stem in _FIRST_STEMS
)

if len(FIRST_NAMES) < NAME_CORPUS_MIN_FIRST:  # pragma: no cover - import guard
    raise ValueError(f"first-name corpus is {len(FIRST_NAMES)}, below NAME_CORPUS_MIN_FIRST")
if len(LAST_NAMES) < NAME_CORPUS_MIN_LAST:  # pragma: no cover - import guard
    raise ValueError(f"last-name corpus is {len(LAST_NAMES)}, below NAME_CORPUS_MIN_LAST")

#: SS2.1 `G4` -- the ONLY domains that may carry dot / `+alias` local-part variation.
GMAIL_DOMAINS_ORDERED: tuple[str, ...] = ("gmail.com", "googlemail.com")

#: Every other domain: all addresses of one person are byte-identical after
#: `norm_email`, so a variant on one of these can never normalize back (`G4`, C4).
NON_GMAIL_DOMAINS: tuple[str, ...] = (
    "brightmail.example",
    "civicpost.example",
    "harbourmail.example",
    "lanternmail.example",
    "meadowpost.example",
    "northgate.example",
    "quillmail.example",
    "riversidepost.example",
    "stonebridge.example",
    "willowmail.example",
)

EMAIL_DOMAINS: tuple[str, ...] = (*GMAIL_DOMAINS_ORDERED, *NON_GMAIL_DOMAINS)

#: Deal-name vocabulary. Deterministic, indexed by the owned PRNG.
WORDS: tuple[str, ...] = (
    "Admissions",
    "Autumn",
    "Bursary",
    "Campus",
    "Cohort",
    "Enrolment",
    "Family",
    "Intake",
    "Onboarding",
    "Placement",
    "Registration",
    "Renewal",
    "Scholarship",
    "Semester",
    "Transfer",
)
