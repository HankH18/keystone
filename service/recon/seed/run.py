"""The seed run: two passes, one self-check, then the tree (SS8, SS9, SS10 `G31`).

    pass 1   build_dataset       materialise every source record, clean and planted
    project  snapshot(gen 1..3)  three COMPLETE snapshots per source (SS7)
    pass 2   recon.er.resolve    the REAL cascade over the emitted fixtures
             run_sweep           SS9.1(b) over every gen-3 entity
             build_golden        apply_precedence -> the surviving entries
    gate     run_self_check      every A.1 volume, A.4 minimum, A.5 ratio and G1..G39
    emit     fixtures/, golden/

The gate sits between `build_golden` and the golden write on purpose: an unplantable
conflict, or a surplus of one in any sweep column, **fails the run** and no golden tree
is produced. Fixtures are written first because SS9.1 says the self-check "executes the
detector's own `recon/er.py` over the emitted fixtures" -- so the bytes on disk are the
bytes it judged.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recon.er import Resolution, Snapshot, resolve
from recon.reference import A1_VOLUMES, CONFLICT_MINIMUMS

from .build import build_dataset
from .emit import reset_directory, write_json, write_tree
from .generations import GENERATIONS, SOURCE_FILES, snapshot, snapshot_records
from .golden import build_golden
from .malformed import build_malformed_cases
from .plan import build_plan
from .selfcheck import SelfCheckReport, run_self_check
from .sweep import run_sweep

__all__ = ["DEFAULT_SEED", "SeedRun", "run_seed"]

#: The committed canonical seed (repo README / A.6).
DEFAULT_SEED = 20260822


class SeedFailure(RuntimeError):
    """Raised when the manifest self-check fails. No golden tree is written."""


@dataclass
class SeedRun:
    seed: int
    profile: str
    out_dir: Path
    report: SelfCheckReport
    golden_entries: int
    clean_sample: int
    expected_views: int
    elapsed_seconds: float


def _fixture_files(dataset: Any) -> dict[str, list[dict[str, Any]]]:
    files: dict[str, list[dict[str, Any]]] = {}
    for generation in GENERATIONS:
        for source, entity_type in SOURCE_FILES:
            files[f"{source}/gen{generation}/{entity_type}.jsonl"] = snapshot_records(
                dataset, source, entity_type, generation
            )
    files["malformed/cases.jsonl"] = build_malformed_cases()
    return files


def run_seed(
    seed: int = DEFAULT_SEED,
    profile: str = "full",
    out_dir: Path | None = None,
    *,
    quiet: bool = False,
) -> SeedRun:
    """Run the whole generator. Raises `SeedFailure` if any named check fails."""
    started = time.monotonic()
    if out_dir is None and profile != "full":
        raise SeedFailure(
            f"refusing to write the repository tree from --profile {profile}: only "
            "`--profile full` may overwrite the committed fixtures/ and golden/ trees "
            "(SS9: 'All gates, benchmarks, and the committed golden/ files are full'). "
            f"Run `python -m recon.seed --profile {profile} --out <scratch-dir>` "
            "(or `make seed-dev`) instead."
        )
    root = Path(out_dir) if out_dir is not None else Path(__file__).resolve().parents[3]
    fixtures_dir = root / "fixtures"
    golden_dir = root / "golden"

    plan = build_plan(profile)
    dataset = build_dataset(seed, plan)

    snapshots: dict[int, Snapshot] = {gen: snapshot(dataset, gen) for gen in GENERATIONS}
    resolutions: dict[int, Resolution] = {gen: resolve(snapshots[gen]) for gen in GENERATIONS}

    sweep = run_sweep(snapshots[3], resolutions[3])
    golden = build_golden(dataset, sweep, resolutions[3])
    malformed = build_malformed_cases()

    # -- fixtures first: the self-check judges the bytes that were actually emitted.
    reset_directory(fixtures_dir)
    files = _fixture_files(dataset)
    file_manifest = write_tree(fixtures_dir, files)

    expected_counts = {
        f"gen{generation}": {
            f"{source}.{entity_type}": len(
                snapshot_records(dataset, source, entity_type, generation)
            )
            for source, entity_type in SOURCE_FILES
        }
        for generation in GENERATIONS
    }

    report = run_self_check(
        dataset,
        snapshots,
        resolutions,
        sweep,
        golden,
        malformed,
        fixtures_dir=fixtures_dir,
        file_manifest=file_manifest,
    )
    if not quiet:
        print(report.render())
    if not report.passed:
        # SS9.1 says no `golden/` tree is written on a failure. Leaving the REJECTED
        # fixture tree on disk next to a previously-valid committed `golden/` is the
        # same defect one level down: a consumer that loads both without first looking
        # for `fixtures/manifest.json` grades against a dataset the golden set does not
        # describe. The rejected fixtures go with the rejected golden set.
        shutil.rmtree(fixtures_dir, ignore_errors=True)
        raise SeedFailure(
            "manifest self-check FAILED -- no golden/ or fixtures/ tree written:\n"
            + "\n".join(
                f"  - {result.name} ({result.constraint}): {result.detail}"
                for result in report.failures
            )
        )

    manifest = {
        "profile": profile,
        "seed": seed,
        "generated_by": "recon.seed",
        "generations": list(GENERATIONS),
        "files": file_manifest,
        "expected_counts": expected_counts,
        "a1_volumes": {f"{s}.{e}": v for (s, e), v in sorted(A1_VOLUMES.items())},
        "planned_volumes": {f"{s}.{e}": v for (s, e), v in sorted(plan.volumes.items())},
        "conflict_minimums": dict(sorted(CONFLICT_MINIMUMS.items())),
        "planned_conflicts": dict(sorted(plan.conflicts.items())),
        "malformed_cases": len(malformed),
        "oscillating_fields": len(dataset.oscillations),
    }
    write_json(fixtures_dir / "manifest.json", manifest)

    reset_directory(golden_dir)
    write_json(golden_dir / "conflicts.json", golden.conflicts)
    write_json(golden_dir / "clean-sample.json", golden.clean_sample)
    write_json(golden_dir / "expected-views.json", golden.expected_views)

    summary = {
        "profile": profile,
        "seed": seed,
        "conflict_counts": _counts(golden.conflicts),
        "conflict_minimums": dict(sorted(CONFLICT_MINIMUMS.items())),
        "golden_entries": len(golden.conflicts),
        "compound_entries": sum(1 for row in golden.conflicts if row["compound_with"]),
        "compound_ratio": round(golden.compound_ratio, 6),
        "tri_source_student_fraction": round(plan.tri_source_student_fraction, 6),
        "fully_consistent_entity_fraction": round(golden.fully_consistent_fraction, 6),
        "entity_count": golden.entity_count,
        "inconsistent_entity_count": golden.inconsistent_entity_count,
        "clean_sample_size": len(golden.clean_sample),
        "expected_view_count": len(golden.expected_views),
        "malformed_cases": len(malformed),
        "oscillating_fields": len(dataset.oscillations),
        "record_counts_gen3": expected_counts["gen3"],
        "record_counts_gen1": expected_counts["gen1"],
        "multi_child_households": sum(1 for h in dataset.households if h.size > 1),
        "deal_less_leads": plan.leads,
        "precedence_fired": {str(k): v for k, v in sorted(golden.suppression_report.items())},
        "self_check": {result.name: result.passed for result in report.results},
        "fingerprint_digest": _digest(golden.fingerprints),
    }
    write_json(golden_dir / "manifest-summary.json", summary)

    elapsed = time.monotonic() - started
    if not quiet:
        print(
            f"wrote {len(file_manifest)} fixture files, {len(golden.conflicts)} golden "
            f"conflicts, {len(golden.clean_sample)} clean-sample entities, "
            f"{len(golden.expected_views)} expected views in {elapsed:.1f}s"
        )
    return SeedRun(
        seed=seed,
        profile=profile,
        out_dir=root,
        report=report,
        golden_entries=len(golden.conflicts),
        clean_sample=len(golden.clean_sample),
        expected_views=len(golden.expected_views),
        elapsed_seconds=elapsed,
    )


def _counts(conflicts: list[dict[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(sorted(CONFLICT_MINIMUMS), 0)
    for row in conflicts:
        counts[str(row["type"])] += 1
    return counts


def _digest(fingerprints: list[str]) -> str:
    import hashlib

    return hashlib.sha256("\n".join(sorted(fingerprints)).encode("utf-8")).hexdigest()
