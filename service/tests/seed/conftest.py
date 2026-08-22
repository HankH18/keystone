"""Shared fixtures: one real seed run per profile, reused by every manifest test.

The runs are session-scoped because the `full` profile materialises 120,000 records
and re-running it per test would dominate the suite. Each run writes a complete
`fixtures/` + `golden/` tree into a temporary directory, so nothing here touches the
committed `golden/` tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True, scope="session")
def _declare_hash_seed() -> None:
    """Declare `PYTHONHASHSEED=0` for the tests that call `run_seed` IN-PROCESS.

    SS8/`G30` puts the clause on the **entrypoint**, and `recon/seed/__main__.py`
    implements it there: it re-`exec`s once with `PYTHONHASHSEED=0` and then asserts it.
    `sc_determinism` asserts the same value, so a library caller that bypasses the
    entrypoint (every `run_seed(...)` call in `tests/seed/`) has to make the same
    declaration the entrypoint would have made. This sets the variable and nothing else
    -- it weakens no assertion, and the property it declares is independently proved on
    every run by `sc_determinism`'s third clause, which replays the whole of pass 2 over
    a shuffled gen-3 snapshot and requires byte-identical golden documents.
    """
    os.environ["PYTHONHASHSEED"] = "0"


@dataclass(frozen=True)
class SeedTree:
    """A generated tree on disk, plus its two manifests already parsed."""

    root: Path
    profile: str
    seed: int
    manifest: dict
    summary: dict
    conflicts: list
    clean_sample: list
    expected_views: list

    def read_jsonl(self, relative: str) -> list[dict]:
        path = self.root / "fixtures" / relative
        return [json.loads(line) for line in path.read_text().splitlines() if line]


def run_seed_subprocess(
    root: Path, profile: str, seed: int, hash_seed: str = "0"
) -> subprocess.CompletedProcess[str]:
    """Run the CLI exactly as a grader would, in a fresh interpreter."""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hash_seed
    env["PYTHONPATH"] = str(SERVICE_ROOT)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "recon.seed",
            "--seed",
            str(seed),
            "--profile",
            profile,
            "--out",
            str(root),
            "--quiet",
        ],
        cwd=SERVICE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _load(root: Path, profile: str, seed: int) -> SeedTree:
    return SeedTree(
        root=root,
        profile=profile,
        seed=seed,
        manifest=json.loads((root / "fixtures" / "manifest.json").read_text()),
        summary=json.loads((root / "golden" / "manifest-summary.json").read_text()),
        conflicts=json.loads((root / "golden" / "conflicts.json").read_text()),
        clean_sample=json.loads((root / "golden" / "clean-sample.json").read_text()),
        expected_views=json.loads((root / "golden" / "expected-views.json").read_text()),
    )


@pytest.fixture(scope="session")
def dev_tree(tmp_path_factory: pytest.TempPathFactory) -> SeedTree:
    root = tmp_path_factory.mktemp("seed-dev")
    result = run_seed_subprocess(root, "dev", 20260822)
    assert result.returncode == 0, result.stderr
    return _load(root, "dev", 20260822)


@pytest.fixture(scope="session")
def full_tree(tmp_path_factory: pytest.TempPathFactory) -> SeedTree:
    root = tmp_path_factory.mktemp("seed-full")
    result = run_seed_subprocess(root, "full", 20260822)
    assert result.returncode == 0, result.stderr
    return _load(root, "full", 20260822)


@pytest.fixture(scope="session", params=["dev", "full"])
def any_tree(request: pytest.FixtureRequest) -> SeedTree:
    return request.getfixturevalue(f"{request.param}_tree")
