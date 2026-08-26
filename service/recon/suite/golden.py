"""Three rows: ``golden-diff``, ``clean-sample``, ``join-check``.

All three grade the SAME detection pass -- :attr:`recon.suite.pipeline
.PipelineRun.run_a` -- against the committed ``golden/`` tree. None of them
re-implements the comparison: the diff and the clean-sample probe are
:mod:`recon.invariants.grading`, imported, so the number the scorecard prints is
the number ``python -m recon.invariants`` prints and the number CI gates on.

``golden-diff`` and ``clean-sample`` are two categories, not three
--------------------------------------------------------------------
Contract SS5.4: an unmatched detection is a **false positive** and an unmatched
golden entry a **false negative**, *independent* of the clean sample. SS8 adds a
stricter probe on top -- an asserted-clean entity is FLAGGED iff any detected
conflict's ``entity_refs`` INTERSECTS its identity refs. They are separate rows
because they can fail separately: a detector can be exact on the golden set and
still smear a conflict's ref list across a neighbour, and the intersection probe
is the only thing that sees it.

All three refuse to grade nothing
-----------------------------------
Each row carries an explicit non-empty floor, because each row's pass predicate
is satisfied by an empty comparison: ``GoldenDiff.passed`` is *no FN and no FP
and no mismatch*, ``CleanSampleResult.passed`` is *nothing flagged*, and
``join-check``'s is *nothing mismatched*. Zero of zero satisfies all three. A
mis-pathed ``KEYSTONE_GOLDEN_DIR``, a truncated ``golden/`` tree or a deploy that
shipped without one would otherwise print three green rows over a comparison that
never happened -- and ``golden-diff`` is the row the whole grading contract hangs
on, so it is the last one that may be allowed to pass vacuously.

``join-check`` goes through HTTP on purpose
--------------------------------------------
It fetches each of the 25 ``golden/expected-views.json`` entries from
``GET /api/entities/{key}`` and compares the endpoint's ``view`` object -- key
for key, no subset test -- against the golden entry. Reading ``entities.current``
straight out of Postgres would grade the resolver and prove nothing about the
endpoint; calling :func:`recon.resolve.person_view` in-process would grade
neither. See :mod:`recon.suite.probe`.
"""

from __future__ import annotations

from typing import Any

from recon.invariants.grading import (
    golden_dir,
    grade_clean_sample,
    grade_run,
    load_clean_sample,
    load_golden,
)
from recon.resolve import VIEW_FIELDS
from recon.suite.checks import CheckResult
from recon.suite.pipeline import pipeline
from recon.suite.probe import admin_headers, json_of, probe_client

__all__ = [
    "CLEAN_SAMPLE",
    "GOLDEN_DIFF",
    "JOIN_CHECK",
    "check_clean_sample",
    "check_golden_diff",
    "check_join",
    "expected_views",
    "view_mismatches",
]

GOLDEN_DIFF = "golden-diff"
CLEAN_SAMPLE = "clean-sample"
JOIN_CHECK = "join-check"

#: Printed in full on a failure; truncated in the row detail.
_DETAIL_LIMIT = 6


def check_golden_diff() -> CheckResult:
    """0 false negatives, 0 false positives, 0 field-exactness mismatches."""
    run = pipeline()
    diff = grade_run(run.run_a.conflicts)

    detected_by_type = run.run_a.by_type()
    golden_by_type: dict[str, int] = {}
    for entry in load_golden():
        conflict_type = str(entry["type"])
        golden_by_type[conflict_type] = golden_by_type.get(conflict_type, 0) + 1

    per_type = " ".join(
        f"{name}:{golden_by_type.get(name, 0)}/{detected_by_type.get(name, 0)}"
        for name in sorted(set(golden_by_type) | set(detected_by_type))
    )
    head = (
        f"FN={len(diff.false_negatives)} FP={len(diff.false_positives)} "
        f"field-mismatches={len(diff.mismatches)} matched={diff.matched}/{diff.golden_total} "
        f"golden/detected per type [{per_type}] dir={golden_dir()}"
    )
    # The non-empty floor its two siblings already have, and for the same reason.
    # `GoldenDiff.passed` is `not (FN or FP or mismatches)`, so an EMPTY golden set
    # diffed against an empty detection satisfies it perfectly -- a mis-pathed
    # `KEYSTONE_GOLDEN_DIR`, a truncated `golden/conflicts.json` or a deploy that
    # shipped without the golden tree would print `PASS ... matched=0/0` on the one
    # row the whole grading contract hangs on. 0 of 0 is not exactness; it is an
    # unasked question.
    if not diff.golden_total:
        return CheckResult.failed(
            GOLDEN_DIFF,
            f"{head} | 0 of 0 golden entries matched is not evidence of anything: "
            "FN, FP and field-mismatch are all vacuously zero over an empty "
            f"comparison. {golden_dir() / 'conflicts.json'} is empty or was not "
            "read; regenerate the golden tree, or point KEYSTONE_GOLDEN_DIR at it",
        )
    if diff.passed:
        return CheckResult.passed(GOLDEN_DIFF, head)

    lines = [head]
    lines += [f"FN {key[0]} {list(key[1])}" for key in diff.false_negatives[:_DETAIL_LIMIT]]
    lines += [f"FP {key[0]} {list(key[1])}" for key in diff.false_positives[:_DETAIL_LIMIT]]
    lines += [mismatch.line().strip() for mismatch in diff.mismatches[:_DETAIL_LIMIT]]
    return CheckResult.failed(GOLDEN_DIFF, " | ".join(lines))


def check_clean_sample() -> CheckResult:
    """0 of the asserted-clean entities flagged (ref-set INTERSECTION, SS8)."""
    run = pipeline()
    sample = load_clean_sample()
    result = grade_clean_sample(run.run_a.conflicts, sample)

    head = (
        f"{result.sampled} asserted-clean entities, {len(result.flagged)} flagged "
        f"(SS8 intersection predicate over {len(run.run_a.conflicts)} detected conflicts)"
    )
    if result.sampled != len(sample):  # pragma: no cover - defensive
        return CheckResult.failed(
            CLEAN_SAMPLE, f"graded {result.sampled} of {len(sample)} sampled entities"
        )
    if not sample:
        return CheckResult.failed(
            CLEAN_SAMPLE,
            "golden/clean-sample.json is empty: zero flagged out of zero sampled is "
            "not evidence of anything",
        )
    if result.passed:
        return CheckResult.passed(CLEAN_SAMPLE, head)

    detail = " | ".join(
        f"FLAGGED {list(identity)} by {key[0]} {list(key[1])}"
        for identity, key in result.flagged[:_DETAIL_LIMIT]
    )
    return CheckResult.failed(CLEAN_SAMPLE, f"{head} | {detail}")


def expected_views() -> list[dict[str, Any]]:
    """``golden/expected-views.json`` -- the 25 committed cross-source views."""
    import json

    return json.loads((golden_dir() / "expected-views.json").read_text(encoding="utf-8"))


def view_mismatches(expected: dict[str, Any], served: dict[str, Any]) -> list[str]:
    """Field-by-field differences between a golden view and the served one.

    Compares the WHOLE key set both ways. A subset test would pass a view that
    dropped ``payments`` entirely, which is precisely the join failure this check
    exists to catch.
    """
    problems: list[str] = []
    missing = sorted(set(expected) - set(served))
    extra = sorted(set(served) - set(expected))
    if missing:
        problems.append(f"missing keys {missing}")
    if extra:
        problems.append(f"unexpected keys {extra}")
    for name in sorted(set(expected) & set(served)):
        if expected[name] != served[name]:
            problems.append(f"{name}: golden={expected[name]!r} served={served[name]!r}")
    return problems


def check_join() -> CheckResult:
    """Every ``golden/expected-views.json`` entry matches ``GET /api/entities/{key}``."""
    pipeline()  # precondition + identity layer, asserted before anything is fetched
    entries = expected_views()
    if not entries:
        return CheckResult.failed(
            JOIN_CHECK,
            "golden/expected-views.json is empty: 0 of 0 views matching is a green "
            "that grades nothing",
        )

    headers = admin_headers()
    failures: list[str] = []
    checked = 0
    with probe_client() as client:
        for entry in entries:
            key = str(entry["person_key"])
            response = client.get(
                f"/api/entities/{key}", params={"lineage": "false"}, headers=headers
            )
            if response.status_code != 200:
                failures.append(f"{key}: HTTP {response.status_code} {response.text[:120]}")
                continue
            body = json_of(response)
            served = body.get("view")
            if not isinstance(served, dict):
                failures.append(f"{key}: response carried no 'view' object")
                continue
            checked += 1
            problems = view_mismatches(entry, served)
            if problems:
                failures.append(f"{key}: " + "; ".join(problems[:3]))

    head = (
        f"{checked}/{len(entries)} unified views fetched from GET /api/entities/"
        f"{{key}} match golden/expected-views.json across {len(VIEW_FIELDS)} view fields"
    )
    if failures:
        return CheckResult.failed(
            JOIN_CHECK,
            f"{head} | {len(failures)} mismatched | " + " | ".join(failures[:_DETAIL_LIMIT]),
        )
    if checked != len(entries):  # pragma: no cover - defensive
        return CheckResult.failed(JOIN_CHECK, f"only {checked} of {len(entries)} views compared")
    return CheckResult.passed(JOIN_CHECK, head)
