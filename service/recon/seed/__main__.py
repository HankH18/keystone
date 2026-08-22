"""`python -m recon.seed --seed <n> --profile dev|full [--out DIR]`.

**SS8 / `G30`: this entrypoint SETS and ASSERTS `PYTHONHASHSEED=0`.** The variable is
read by the interpreter at startup, so setting it in-process is too late -- the only
way to *set* it is to re-`exec` once with it in the environment. `_enforce_hash_seed`
does exactly that (guarded by a sentinel so it can never loop), and then asserts the
value it ended up with. A run started at `PYTHONHASHSEED=random` therefore does not
"complete silently with a green manifest": it re-execs at 0 and the `sc_determinism`
row asserts the result, instead of a report line narrating whatever it found.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .plan import PROFILES
from .run import DEFAULT_SEED, SeedFailure, run_seed

#: Set on the re-exec'd child so a hostile environment cannot make this loop.
_REEXEC_SENTINEL = "KEYSTONE_SEED_HASH_SEED_SET"


def _enforce_hash_seed(argv: list[str] | None) -> None:
    """Set `PYTHONHASHSEED=0` (re-exec once if needed) and assert it (SS8, `G30`)."""
    if os.environ.get("PYTHONHASHSEED") != "0":
        if os.environ.get(_REEXEC_SENTINEL) == "1":  # pragma: no cover - unreachable
            raise SystemExit("PYTHONHASHSEED is not '0' after re-exec; refusing to run (SS8 / G30)")
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = "0"
        env[_REEXEC_SENTINEL] = "1"
        args = ["-m", "recon.seed", *(argv if argv is not None else sys.argv[1:])]
        os.execve(sys.executable, [sys.executable, *args], env)
    if os.environ.get("PYTHONHASHSEED") != "0":  # pragma: no cover - defensive
        raise SystemExit("PYTHONHASHSEED must be '0' (SS8 / G30)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m recon.seed",
        description=(
            "Deterministic synthetic dataset generator and golden-set export. "
            "Same seed => byte-identical fixtures, conflict set and golden tree."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"PRNG seed (default: {DEFAULT_SEED}, the committed canonical seed)",
    )
    parser.add_argument(
        "--profile",
        choices=PROFILES,
        default="full",
        help="dev (~6k records, all 14 classes at scaled minimums) or full (the graded 120k set)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="repository root to write fixtures/ and golden/ into (default: the repo root)",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress the self-check report")
    args = parser.parse_args(argv)

    _enforce_hash_seed(argv)

    try:
        run_seed(seed=args.seed, profile=args.profile, out_dir=args.out, quiet=args.quiet)
    except SeedFailure as failure:
        print(str(failure), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
