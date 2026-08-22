"""`determinism` marker -- the COMMITTED `golden/` tree is the output of THIS code.

`golden/` is a committed grading contract that "every seed run rewrites byte-identically"
(`.claude/CLAUDE.md`). Nothing enforced that. Every other seed test writes into
`tmp_path` and compares runs against each other, so a stale committed golden set -- the
worst failure mode this deliverable has, because the harness would then grade a detector
against a dataset the fixtures do not contain -- would have shipped green.

This regenerates the full profile at `DEFAULT_SEED` into a temp directory and asserts
sha256 equality against the four committed `golden/*.json` files. It is the one test
that reads the repository's own golden tree.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from recon.seed.run import DEFAULT_SEED

from .conftest import run_seed_subprocess

pytestmark = pytest.mark.determinism

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_FILES = (
    "conflicts.json",
    "clean-sample.json",
    "expected-views.json",
    "manifest-summary.json",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_committed_golden_tree_reproduces_from_the_committed_code(tmp_path: Path) -> None:
    committed = REPO_ROOT / "golden"
    missing = [name for name in GOLDEN_FILES if not (committed / name).is_file()]
    assert not missing, f"the committed golden tree is missing {missing}"

    result = run_seed_subprocess(tmp_path, "full", DEFAULT_SEED)
    assert result.returncode == 0, result.stderr

    stale = []
    for name in GOLDEN_FILES:
        fresh = tmp_path / "golden" / name
        if _sha256(fresh) != _sha256(committed / name):
            stale.append(name)
    assert not stale, (
        "the committed golden/ tree is NOT the output of the committed generator at "
        f"--profile full --seed {DEFAULT_SEED}; stale files: {stale}. "
        "Re-run `make seed` and commit the result."
    )


def test_the_committed_summary_records_the_canonical_profile_and_seed() -> None:
    summary = json.loads((REPO_ROOT / "golden" / "manifest-summary.json").read_text())
    assert summary["profile"] == "full", "the committed golden set must be the full profile"
    assert summary["seed"] == DEFAULT_SEED
    assert all(summary["self_check"].values()), "a committed golden set with a failed check"
