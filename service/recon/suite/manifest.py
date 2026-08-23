"""``manifest`` -- the generator's 47 self-checks and Appendix A's minimums.

Three things, in a deliberate chain, so that no link can be true on its own:

1. **The 47 self-checks, from a LIVE run.** The report read here is the
   ``self_check`` map that :func:`recon.seed.run.run_seed` wrote during the
   determinism check's first subprocess -- not the copy committed under
   ``golden/``. That distinction is the whole value of the row: a committed
   ``manifest-summary.json`` asserting 47 green self-checks is a *file saying it
   passed*, and re-reading it proves only that the file has not been edited.
   Sharing :func:`recon.suite.determinism.seed_pair` means the suite pays for
   one pair of generator runs and grades both determinism and the manifest from
   them.
2. **The count is pinned.** ``EXPECTED_SELF_CHECKS`` is 47. "All of them passed"
   is not a claim until you say how many there were: a generator refactor that
   dropped nine checks would otherwise still print all-green.
3. **Appendix A's floors, and then a cross-check.** The A.1 volumes, the fourteen
   A.4 conflict minimums, A.4's structural floors (multi-child households,
   deal-less leads, oscillating fields, malformed payloads) and A.5's compound
   ratio are asserted against the live summary; the live summary's own totals are
   then re-derived from the **committed ``golden/`` files** it is supposed to
   describe. A summary that says "3,050 golden entries" while
   ``golden/conflicts.json`` holds 2,900 is a broken contract even though every
   floor in it is satisfied.

Profile note: floors A.4 states "per 100k records" -- multi-child households and
deal-less leads -- scale with the profile (contract SS12 D-13), so they are
asserted only when the pair ran at ``full``. Everything else is asserted at every
profile, and the row says which profile produced it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from recon.invariants.grading import golden_dir
from recon.suite.checks import CheckResult
from recon.suite.determinism import seed_pair

__all__ = [
    "A1_VOLUME_FLOORS",
    "A4_STRUCTURAL_FLOORS",
    "A5_COMPOUND_RATIO",
    "EXPECTED_SELF_CHECKS",
    "MANIFEST",
    "check_manifest",
]

MANIFEST = "manifest"

#: How many named self-checks ``recon.seed.selfcheck`` is expected to run.
#: Pinned so that losing checks is a red row, not a quieter green one.
EXPECTED_SELF_CHECKS = 47

#: SPEC R22 / Appendix A.1 -- the graded ~100k generation-3 snapshot.
A1_VOLUME_FLOORS: Mapping[str, int] = {
    "crm.contact": 40_000,
    "crm.deal": 15_000,
    "appdb.student": 25_000,
    "appdb.enrollment": 22_000,
    "payments.payment": 18_000,
}

#: A.4's structural floors. ``scaled`` marks the two that contract SS12 D-13
#: records as scaling with the profile ("per 100k records"), so they are only
#: asserted for the ``full`` profile.
A4_STRUCTURAL_FLOORS: Mapping[str, tuple[int, bool]] = {
    # key: (floor, scales_with_profile)
    "multi_child_households": (1_000, True),
    "deal_less_leads": (3_000, True),
    "oscillating_fields": (25, False),
    "malformed_cases": (20, False),
    "clean_sample_size": (1_000, False),
}

#: A.5 -- at least a tenth of the surviving golden entries are compound-cause.
A5_COMPOUND_RATIO = 0.10

_DETAIL_LIMIT = 6


def _committed_golden_totals() -> dict[str, Any]:
    """Totals re-derived from the committed ``golden/`` files themselves."""
    root = golden_dir()
    conflicts = json.loads((root / "conflicts.json").read_text(encoding="utf-8"))
    clean = json.loads((root / "clean-sample.json").read_text(encoding="utf-8"))
    views = json.loads((root / "expected-views.json").read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for entry in conflicts:
        name = str(entry["type"])
        counts[name] = counts.get(name, 0) + 1
    return {
        "golden_entries": len(conflicts),
        "conflict_counts": dict(sorted(counts.items())),
        "clean_sample_size": len(clean),
        "expected_view_count": len(views),
        "compound_entries": sum(1 for entry in conflicts if entry.get("compound_with")),
    }


def check_manifest() -> CheckResult:
    """47 generator self-checks green, and every Appendix-A minimum met."""
    pair = seed_pair()
    summary = pair.summary
    failures: list[str] = []

    # -- 1/2. the live self-check report --------------------------------------
    self_check = summary.get("self_check")
    if not isinstance(self_check, Mapping) or not self_check:
        return CheckResult.failed(
            MANIFEST,
            "the generator run emitted no self_check map, so 'the 47 self-checks "
            "passed' has no evidence behind it",
        )
    failed_checks = sorted(name for name, passed in self_check.items() if not passed)
    if failed_checks:
        failures.append(
            f"{len(failed_checks)} generator self-check(s) failed: {failed_checks[:_DETAIL_LIMIT]}"
        )
    if len(self_check) != EXPECTED_SELF_CHECKS:
        failures.append(
            f"the generator ran {len(self_check)} named self-checks, not "
            f"{EXPECTED_SELF_CHECKS}: the count is pinned so that checks cannot be "
            "lost without the scorecard noticing"
        )

    # -- 3a. A.1 volumes ------------------------------------------------------
    volumes = summary.get("record_counts_gen3") or {}
    if pair.profile == "full":
        for qualified, floor in sorted(A1_VOLUME_FLOORS.items()):
            observed = int(volumes.get(qualified, 0))
            if observed < floor:
                failures.append(f"A.1 {qualified}: {observed} < {floor}")

    # -- 3b. the fourteen A.4 conflict minimums -------------------------------
    counts = summary.get("conflict_counts") or {}
    minimums = summary.get("conflict_minimums") or {}
    if not minimums:
        failures.append("the summary carries no conflict_minimums block to assert against")
    for name in sorted(minimums):
        floor = int(minimums[name])
        observed = int(counts.get(name, 0))
        if observed < floor:
            failures.append(f"A.4 {name}: {observed} < {floor}")

    # -- 3c. A.4's structural floors ------------------------------------------
    for name, (floor, scales) in sorted(A4_STRUCTURAL_FLOORS.items()):
        if scales and pair.profile != "full":
            continue
        observed = int(summary.get(name, 0))
        if observed < floor:
            failures.append(f"A.4 {name}: {observed} < {floor}")

    # -- 3d. A.5 compound ratio ------------------------------------------------
    compound_ratio = float(summary.get("compound_ratio", 0.0))
    if compound_ratio < A5_COMPOUND_RATIO:
        failures.append(f"A.5 compound ratio {compound_ratio:.4f} < {A5_COMPOUND_RATIO}")

    # -- 3e. the summary against the files it describes ------------------------
    cross_note = "cross-check skipped (non-full profile)"
    if pair.profile == "full":
        totals = _committed_golden_totals()
        for name in ("golden_entries", "clean_sample_size", "expected_view_count"):
            if int(summary.get(name, -1)) != totals[name]:
                failures.append(
                    f"summary {name}={summary.get(name)} but the committed golden tree "
                    f"holds {totals[name]}"
                )
        if dict(counts) != totals["conflict_counts"]:
            differing = sorted(
                name
                for name in set(counts) | set(totals["conflict_counts"])
                if int(counts.get(name, 0)) != int(totals["conflict_counts"].get(name, 0))
            )
            failures.append(
                f"summary conflict_counts disagree with golden/conflicts.json on "
                f"{differing[:_DETAIL_LIMIT]}"
            )
        cross_note = (
            f"cross-checked against committed golden/ "
            f"({totals['golden_entries']} entries, {totals['clean_sample_size']} clean, "
            f"{totals['expected_view_count']} views)"
        )

    detail = (
        f"{len(self_check) - len(failed_checks)}/{len(self_check)} generator self-checks "
        f"green (expected {EXPECTED_SELF_CHECKS}); profile={pair.profile} seed={pair.seed}; "
        f"A.4 conflict minimums {len(minimums)}/14 asserted; "
        f"multi_child_households={summary.get('multi_child_households')} "
        f"deal_less_leads={summary.get('deal_less_leads')} "
        f"oscillating_fields={summary.get('oscillating_fields')} "
        f"malformed={summary.get('malformed_cases')}; A.5 compound ratio "
        f"{compound_ratio:.4f}; {cross_note}"
    )
    if failures:
        return CheckResult.failed(MANIFEST, f"{detail} | " + " | ".join(failures[:_DETAIL_LIMIT]))
    return CheckResult.passed(MANIFEST, detail)
