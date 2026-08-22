"""`determinism` marker -- same seed => byte-identical tree, in separate processes.

Two runs are compared as **subprocesses launched at different `PYTHONHASHSEED` values**.
Note what that now proves and what it does not: SS8/`G30` requires the entrypoint to
*set* `PYTHONHASHSEED=0`, and `recon/seed/__main__.py` does it by re-`exec`ing once, so
both subprocesses ultimately emit their tree at 0 and this comparison no longer probes
hash-order independence by itself. That property is instead asserted **on every run** by
`sc_determinism`'s third clause, which replays the whole of pass 2 over a shuffled gen-3
snapshot and requires byte-identical golden documents -- a stricter probe of the same
thing, and one a grader's own run exercises rather than only the test suite.

A third run at a *different* seed must produce a *different* tree -- and every file
except the static malformed corpus must differ -- so the test cannot be satisfied by a
generator that emits a constant, nor by one whose conflict *addressing* is seed-invariant.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from .conftest import run_seed_subprocess

pytestmark = pytest.mark.determinism


def _tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def test_same_seed_is_byte_identical_across_hash_seeds(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    assert run_seed_subprocess(first, "dev", 20260822, hash_seed="0").returncode == 0
    assert run_seed_subprocess(second, "dev", 20260822, hash_seed="524287").returncode == 0

    left, right = _tree_hashes(first), _tree_hashes(second)
    assert set(left) == set(right), "the two runs emitted different file sets"
    differing = sorted(name for name in left if left[name] != right[name])
    assert not differing, f"files differ between two runs at the same seed: {differing}"
    assert left, "the run emitted no files at all"


def test_a_different_seed_produces_a_different_tree(tmp_path: Path) -> None:
    baseline, other = tmp_path / "baseline", tmp_path / "other"
    assert run_seed_subprocess(baseline, "dev", 20260822, hash_seed="0").returncode == 0
    assert run_seed_subprocess(other, "dev", 999983, hash_seed="0").returncode == 0

    left, right = _tree_hashes(baseline), _tree_hashes(other)
    assert set(left) == set(right)
    # The malformed corpus is committed static data and is deliberately seed-independent.
    assert left["fixtures/malformed/cases.jsonl"] == right["fixtures/malformed/cases.jsonl"]
    # EVERY other file must move. "at least one file differs" was satisfied by a single
    # differing file, and it was: `golden/clean-sample.json` -- a graded golden artifact
    # -- carried zero seed entropy, because the conflict *addressing* was a fixed index
    # partition and only field values moved with the seed. That is the assertion which
    # would have caught it.
    identical = sorted(
        name
        for name in left
        if left[name] == right[name] and name != "fixtures/malformed/cases.jsonl"
    )
    assert not identical, (
        "these files are identical at two different seeds, so a detector that hardcoded "
        f"their contents would score against any seed: {identical}"
    )


def test_repeated_run_into_a_dirty_directory_is_still_identical(tmp_path: Path) -> None:
    """A second run over an existing tree must not merge with, or inherit, the first."""
    root = tmp_path / "tree"
    assert run_seed_subprocess(root, "dev", 20260822).returncode == 0
    first = _tree_hashes(root)
    (root / "fixtures" / "stale.jsonl").write_text("{}\n")
    assert run_seed_subprocess(root, "dev", 20260822).returncode == 0
    assert _tree_hashes(root) == first, "a stale file survived, or the rewrite was not identical"


def test_the_entrypoint_sets_pythonhashseed_to_zero(tmp_path: Path) -> None:
    """SS8 / `G30`: "the seed entrypoint **sets and asserts** `PYTHONHASHSEED=0`".

    It was implemented nowhere -- `selfcheck.py` printed the variable as a display fact
    and a run at `PYTHONHASHSEED=random` completed with a green manifest. The entrypoint
    now re-`exec`s once with the variable set, so whatever the caller's environment says,
    the run that emits the tree runs at 0 and `sc_determinism` asserts it.
    """
    root = tmp_path / "random-hash-seed"
    result = run_seed_subprocess(root, "dev", 20260822, hash_seed="random")
    assert result.returncode == 0, result.stderr

    summary = json.loads((root / "golden" / "manifest-summary.json").read_text())
    assert summary["self_check"]["sc_determinism"] is True

    baseline = tmp_path / "zero-hash-seed"
    assert run_seed_subprocess(baseline, "dev", 20260822, hash_seed="0").returncode == 0
    assert _tree_hashes(root) == _tree_hashes(baseline)
