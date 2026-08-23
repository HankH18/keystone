"""``determinism`` -- SPEC success criterion 2, in its three parts.

    "Two full runs from the same seed produce byte-identical datasets, identical
     conflict sets, and identical confidence scores."

Three artifacts, three comparisons, and each one can fail without the others:

**dataset hash.** Two ``python -m recon.seed`` runs at the same seed, into two
scratch directories, hashed file by file. They run as **subprocesses**, not as
in-process calls, for the reason ``recon/seed/__main__.py`` re-execs itself over:
``PYTHONHASHSEED`` is read by the interpreter at startup, so a determinism check
that called ``run_seed()`` in this process would grade whatever hash seed the
suite happened to inherit -- and would keep passing on the one run where it
mattered. Two subprocesses also make "identical" mean identical across process
boundaries, which is the claim. The first run's tree is additionally compared
**against the committed ``golden/`` files**, so the contract the grader diffs
against is proven to be the contract this generator emits, not a snapshot that
drifted.

**conflict set.** Two independent detection passes (``run_a`` / ``run_b`` in
:mod:`recon.suite.pipeline`), on two separate connections, over the same data.
Compared as the full ordered conflict payload, not just the fingerprints: two
runs can agree on which conflicts exist and disagree on the observed values
inside them.

**confidence vector.** Three scoring passes -- the two rolled-back dry runs and
the committed one -- must produce the same ``(fingerprint, confidence)`` vector.
Three rather than two because the committed run is the one whose numbers a
reviewer sees, and a dry/committed divergence would otherwise be invisible.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recon.invariants.grading import golden_dir
from recon.seed.run import DEFAULT_SEED
from recon.suite.checks import CheckResult
from recon.suite.pipeline import pipeline

__all__ = [
    "DETERMINISM",
    "SEED_PROFILE_ENV",
    "SeedPair",
    "SeedRunArtifacts",
    "check_determinism",
    "reset_seed_cache",
    "seed_pair",
    "seed_profile",
    "seed_twice",
    "tree_digest",
]

DETERMINISM = "determinism"

#: Which dataset profile the two seed runs use. ``full`` is the graded default
#: and the only profile the committed ``golden/`` comparison is defined for.
SEED_PROFILE_ENV = "KEYSTONE_SUITE_SEED_PROFILE"

_SERVICE_ROOT = Path(__file__).resolve().parents[2]

_DETAIL_LIMIT = 5


def seed_profile() -> str:
    return os.environ.get(SEED_PROFILE_ENV, "full").strip() or "full"


def tree_digest(root: Path) -> tuple[str, dict[str, str]]:
    """``(overall sha256, {relative path: sha256})`` for every file under ``root``.

    Paths are relative and sorted, and the path itself is hashed alongside its
    bytes: two trees with the same file contents under different names are not
    the same dataset, and a digest over contents alone would call them equal.
    """
    per_file: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        per_file[path.relative_to(root).as_posix()] = digest.hexdigest()

    overall = hashlib.sha256()
    for name in sorted(per_file):
        overall.update(f"{name}:{per_file[name]}\n".encode())
    return overall.hexdigest(), per_file


class SeedRunArtifacts:
    """One seed subprocess: where it wrote, what it hashed, what it printed."""

    __slots__ = ("digest", "files", "root", "seconds", "stderr")

    def __init__(
        self, root: Path, digest: str, files: dict[str, str], seconds: float, stderr: str
    ) -> None:
        self.root = root
        self.digest = digest
        self.files = files
        self.seconds = seconds
        self.stderr = stderr


def _run_seed_subprocess(out_dir: Path, *, seed: int, profile: str) -> SeedRunArtifacts:
    """Run the committed generator CLI into ``out_dir``. Raises on a non-zero exit."""
    env = dict(os.environ)
    # SS8 / G30: the generator asserts this itself and re-execs to get it. Setting
    # it here means the child never has to, so the process we measure is the
    # process we launched.
    env["PYTHONHASHSEED"] = "0"
    started = time.perf_counter()
    completed = subprocess.run(  # fixed argv, no shell
        [
            sys.executable,
            "-m",
            "recon.seed",
            "--seed",
            str(seed),
            "--profile",
            profile,
            "--out",
            str(out_dir),
            "--quiet",
        ],
        cwd=str(_SERVICE_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    seconds = time.perf_counter() - started
    if completed.returncode != 0:
        raise RuntimeError(
            f"`python -m recon.seed --profile {profile}` exited "
            f"{completed.returncode}: {(completed.stderr or completed.stdout)[-600:]}"
        )
    digest, files = tree_digest(out_dir)
    return SeedRunArtifacts(out_dir, digest, files, seconds, completed.stderr)


@contextmanager
def seed_twice(
    *, seed: int = DEFAULT_SEED, profile: str | None = None
) -> Iterator[tuple[SeedRunArtifacts, SeedRunArtifacts]]:
    """Run the generator twice into two scratch trees and hash both.

    A context manager: the two trees (~125MB each at ``full``) exist only inside
    the ``with`` block and are removed on the way out.
    """
    profile = profile or seed_profile()
    with tempfile.TemporaryDirectory(prefix="keystone-determinism-") as workspace:
        base = Path(workspace)
        first = _run_seed_subprocess(base / "run-a", seed=seed, profile=profile)
        second = _run_seed_subprocess(base / "run-b", seed=seed, profile=profile)
        yield first, second


def _committed_golden_drift(produced_golden: Path) -> tuple[str, ...]:
    """Committed ``golden/*.json`` files the regenerated tree does not reproduce."""
    committed = golden_dir()
    problems: list[str] = []
    for name in sorted(path.name for path in committed.glob("*.json")):
        produced = produced_golden / name
        if not produced.exists():
            problems.append(f"golden/{name}: the run wrote no such file")
            continue
        want = hashlib.sha256((committed / name).read_bytes()).hexdigest()
        got = hashlib.sha256(produced.read_bytes()).hexdigest()
        if want != got:
            problems.append(f"golden/{name}: committed {want[:12]} regenerated {got[:12]}")
    return tuple(problems)


@dataclass(frozen=True)
class SeedPair:
    """Two seed subprocesses, reduced to what outlives their scratch trees.

    Shared by ``determinism`` (the digests) and ``manifest`` (the self-check map
    and the volumes). Run once per process: at ``full`` the pair costs about a
    minute, and two rows describing two *different* pairs of runs would be worse
    than slow -- they would be incomparable.
    """

    profile: str
    seed: int
    digest_a: str
    digest_b: str
    files_a: Mapping[str, str]
    files_b: Mapping[str, str]
    seconds_a: float
    seconds_b: float
    #: ``golden/manifest-summary.json`` as run A emitted it -- a LIVE self-check
    #: report, not the committed copy of one.
    summary: Mapping[str, Any]
    #: ``fixtures/manifest.json`` as run A emitted it.
    fixtures_manifest: Mapping[str, Any]
    golden_drift: tuple[str, ...]

    @property
    def identical(self) -> bool:
        return self.digest_a == self.digest_b

    def differing_files(self) -> list[str]:
        return sorted(
            name
            for name in set(self.files_a) | set(self.files_b)
            if self.files_a.get(name) != self.files_b.get(name)
        )


_PAIR_CACHE: dict[str, SeedPair] = {}


def seed_pair(*, seed: int = DEFAULT_SEED, profile: str | None = None) -> SeedPair:
    """The process-wide pair of seed runs, produced on first use."""
    profile = profile or seed_profile()
    cache_key = f"{profile}:{seed}"
    if cache_key in _PAIR_CACHE:
        return _PAIR_CACHE[cache_key]

    with seed_twice(seed=seed, profile=profile) as (first, second):
        summary = json.loads(
            (first.root / "golden" / "manifest-summary.json").read_text(encoding="utf-8")
        )
        fixtures_manifest = json.loads(
            (first.root / "fixtures" / "manifest.json").read_text(encoding="utf-8")
        )
        drift = _committed_golden_drift(first.root / "golden") if profile == "full" else ()
        pair = SeedPair(
            profile=profile,
            seed=seed,
            digest_a=first.digest,
            digest_b=second.digest,
            files_a=dict(first.files),
            files_b=dict(second.files),
            seconds_a=first.seconds,
            seconds_b=second.seconds,
            summary=summary,
            fixtures_manifest=fixtures_manifest,
            golden_drift=drift,
        )
    _PAIR_CACHE[cache_key] = pair
    return pair


def reset_seed_cache() -> None:
    """Drop the cached seed pair. For tests that vary the profile."""
    _PAIR_CACHE.clear()


def _conflict_payload_digest(conflicts: Sequence[Any]) -> str:
    payload = json.dumps(
        [conflict.as_json() for conflict in sorted(conflicts, key=lambda c: c.fingerprint)],
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def check_determinism() -> CheckResult:
    """Two seeded runs -> identical dataset, conflict set and confidence vector."""
    run = pipeline()
    profile = seed_profile()
    failures: list[str] = []

    # -- 2. conflict set (two detection passes, two connections) --------------
    fingerprints_a = run.run_a.fingerprints
    fingerprints_b = run.run_b.fingerprints
    if fingerprints_a != fingerprints_b:
        only_a = sorted(set(fingerprints_a) - set(fingerprints_b))[:_DETAIL_LIMIT]
        only_b = sorted(set(fingerprints_b) - set(fingerprints_a))[:_DETAIL_LIMIT]
        failures.append(
            f"two detection passes disagree: {len(fingerprints_a)} vs "
            f"{len(fingerprints_b)} conflicts; only in A {only_a}; only in B {only_b}"
        )
    conflicts_a = _conflict_payload_digest(run.run_a.conflicts)
    conflicts_b = _conflict_payload_digest(run.run_b.conflicts)
    if conflicts_a != conflicts_b:
        failures.append(
            f"the two detection passes produced the same fingerprints but different "
            f"payloads: {conflicts_a[:12]} vs {conflicts_b[:12]}"
        )

    # -- 3. confidence vector (two dry runs + the committed run) --------------
    vectors = {
        "dry-a": run.dry_a.confidence_digest(),
        "dry-b": run.dry_b.confidence_digest(),
        "committed": run.report_first.confidence_digest(),
    }
    if len(set(vectors.values())) != 1:
        failures.append(f"confidence vectors differ across scoring passes: {vectors}")
    if not run.report_first.confidence_vector():
        failures.append("the confidence vector is empty, so 'identical' grades nothing")

    # -- 1. dataset hash (two generator subprocesses) -------------------------
    pair = seed_pair(profile=profile)
    if not pair.identical:
        differing = pair.differing_files()
        failures.append(
            f"two `--profile {profile}` seed runs produced different trees "
            f"({pair.digest_a[:12]} vs {pair.digest_b[:12]}); "
            f"{len(differing)} file(s) differ, e.g. {differing[:_DETAIL_LIMIT]}"
        )
    if pair.golden_drift:
        failures.append(
            "the regenerated golden tree does not match the committed one: "
            + "; ".join(pair.golden_drift[:_DETAIL_LIMIT])
        )
    golden_note = (
        "; committed golden/ reproduced byte-for-byte"
        if pair.profile == "full" and not pair.golden_drift
        else ""
    )
    # `==` only when they really are equal: a head line that reads "a == b" over
    # two different digests is a detail string lying about the thing it prints.
    dataset_note = (
        f"dataset {pair.digest_a[:16]} {'==' if pair.identical else '!='} "
        f"{pair.digest_b[:16]} over "
        f"{len(pair.files_a)} files (profile={profile}, seed={pair.seed}, "
        f"{pair.seconds_a:.1f}s + {pair.seconds_b:.1f}s, PYTHONHASHSEED=0 subprocesses"
        f"{golden_note})"
    )

    detail = (
        f"{dataset_note}; conflict set {len(fingerprints_a)} fingerprints, payload "
        f"{conflicts_a[:16]} {'==' if conflicts_a == conflicts_b else '!='} "
        f"{conflicts_b[:16]}; confidence vector "
        f"{len(run.report_first.confidence_vector())} entries, "
        f"{vectors['committed'][:16]} across dry-a/dry-b/committed"
    )
    if failures:
        return CheckResult.failed(DETERMINISM, f"{detail} | " + " | ".join(failures))
    return CheckResult.passed(DETERMINISM, detail)
