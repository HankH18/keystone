"""The determinism spine: one owned PRNG, integer time, integer money.

Everything the generator randomises goes through **one** `random.Random(seed)`
instance threaded by hand (repo non-negotiable, `G30`). Module-level `random.*`,
`uuid4()`, `datetime.now()`, `set` iteration and `dict`-insertion order that depends
on an unordered source are all forbidden on any path that reaches an output.

Time is integer **seconds since the epoch 2026-01-01T00:00:00Z**, formatted only at
emit time, so no clock, locale or timezone can move a byte. `canon_value` reads a
naive datetime as already-UTC and truncates to whole seconds (SS2.5 ruling 4); this
module never produces sub-second precision at all, so the truncation is a no-op and
the two sides cannot disagree about it.

Money is integer cents everywhere. The one float in the pinned schemas,
`crm.deal.amount`, is derived from whole cents by `amount_dollars`, which keeps
`G39` true by construction: a whole number of cents can never sit on a half-cent
boundary, so `Money(round(amount * 100))`'s half-to-even tie-break is unobservable.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import TypeVar

__all__ = ["EPOCH", "Rng", "amount_dollars", "iso_date", "iso_timestamp"]

T = TypeVar("T")

#: SS8 / DESIGN "Owned PRNG" -- integer offsets are measured from here.
EPOCH: datetime = datetime(2026, 1, 1, 0, 0, 0)

_DAY = 86400


class Rng:
    """A thin, explicit wrapper over one `random.Random`.

    Thin on purpose: every helper is a single documented draw, so the *sequence* of
    draws is readable at the call site and a refactor that reorders them is visible
    as a diff rather than as a silently different dataset.
    """

    __slots__ = ("_random", "seed")

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._random = random.Random(seed)

    def fork(self, label: str) -> Rng:
        """A child PRNG bound to `label`.

        Used to isolate an independent phase (malformed payloads, the clean sample)
        so that adding a draw to one phase cannot shift every later phase's stream.
        The child seed is a pure function of `(seed, label)`.
        """
        child = Rng.__new__(Rng)
        child.seed = self.seed
        child._random = random.Random(f"{self.seed}:{label}")
        return child

    def randint(self, low: int, high: int) -> int:
        """Uniform integer in `[low, high]`, inclusive."""
        return self._random.randint(low, high)

    def pick(self, options: Sequence[T]) -> T:
        """Uniform choice from an **ordered** sequence. Never a `set`."""
        if not options:
            raise ValueError("pick() requires a non-empty sequence")
        return options[self._random.randrange(len(options))]

    def chance(self, probability: float) -> bool:
        """`True` with probability `probability`."""
        return self._random.random() < probability

    def shuffled(self, items: Sequence[T]) -> list[T]:
        """A shuffled **copy**; the input sequence is left alone."""
        pool = list(items)
        self._random.shuffle(pool)
        return pool

    def sample(self, items: Sequence[T], count: int) -> list[T]:
        """`count` distinct items, drawn without replacement, order-stable."""
        if count > len(items):
            raise ValueError(f"cannot sample {count} from {len(items)} items")
        return self._random.sample(list(items), count)


def iso_timestamp(offset_seconds: int) -> str:
    """`YYYY-MM-DDTHH:MM:SSZ` at `offset_seconds` after the epoch (SS2.5 ruling 4)."""
    return (EPOCH + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_date(year: int, day_of_year: int) -> str:
    """`YYYY-MM-DD` -- the pinned `norm_dob` shape."""
    return (date(year, 1, 1) + timedelta(days=day_of_year)).isoformat()


def amount_dollars(cents: int) -> float:
    """Whole cents -> the CRM-shaped dollar float (`G39`).

    `crm.deal.amount` is the only float in the pinned schemas. Deriving it from an
    integer number of cents is what makes `G39` true *by construction*: `amount * 100`
    can never be an exact `.5` in IEEE-754, so `Money.from_dollars`'s banker's-rounding
    tie-break can never decide a graded byte. `sc_amount_no_half_cent` re-checks it.
    """
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise TypeError(f"amount_dollars requires int cents, got {type(cents).__name__}")
    return cents / 100


def day_seconds(days: int, seconds: int = 0) -> int:
    """`days` whole days plus `seconds`, in seconds -- the only time arithmetic used."""
    return days * _DAY + seconds
