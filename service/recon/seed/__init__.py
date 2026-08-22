"""Deterministic synthetic dataset generator and golden export (T-2).

Entry point: `python -m recon.seed --seed <n> --profile dev|full [--out DIR]`.

Two passes (contract v2 SS10 `G31`). Pass 1 materialises every source record; pass 2
runs the **real** `recon.er` cascade over the emitted fixtures and derives every
`entity_refs` value in `golden/` from the links ER actually produced -- never from what
the generator intended to plant. Between them sits the manifest self-check (SS9), which
asserts every A.1 volume, every A.4 minimum, every A.5 ratio and all thirty-nine
construction constraints, and fails the run before a single golden byte is written.
"""

from .plan import PROFILES, Plan, build_plan
from .run import DEFAULT_SEED, SeedFailure, SeedRun, run_seed

__all__ = [
    "DEFAULT_SEED",
    "PROFILES",
    "Plan",
    "SeedFailure",
    "SeedRun",
    "build_plan",
    "run_seed",
]
