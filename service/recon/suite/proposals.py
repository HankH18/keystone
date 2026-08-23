"""Two rows: ``proposal-safety`` and ``oscillation-dedup``.

``proposal-safety`` -- R13's four claims, each with a number
--------------------------------------------------------------
1. **N conflicts -> N proposals.** Not "at least N", not "N or a skip": one
   proposal per conflict, counted three ways that must agree -- the conflicts the
   run loaded, the proposals the run says it wrote, and the rows actually in
   ``proposals``. Two of those three agreeing is how a run that rolled back half
   its work still looks complete.
2. **Every proposal is born held or pending.** ``approved``/``applied`` at birth
   is SQLSTATE ``KS002`` in the database; the check asserts it in the data too,
   because a backstop that is the only control is not a backstop.
3. **Every C14 and every sensitive-target proposal is held.** Recomputed from
   :func:`recon.sensitive.classify` -- the same pure function the reconciler
   used, re-run here over the conflict's ``type`` and ``disagreeing_fields`` as
   they were *stored*, and compared against the stored ``status``. Reading the
   ``proposals.sensitive`` column and asserting it agrees with
   ``status = 'sensitive_hold'`` would only prove the writer was internally
   consistent with itself.
4. **The mirror is byte-unchanged across the run.** The digests bracketing the
   committed ``run_once()`` (see :mod:`recon.suite.pipeline`), compared table by
   table.

``oscillation-dedup`` -- R16's two rules, which are not the same rule
-----------------------------------------------------------------------
* a second pass over an unchanged database proposes **zero**, every conflict
  skipped for ``fingerprint_dedup``;
* a conflict whose field oscillated A -> B -> A is **escalated**. The count is
  asserted against ``golden/conflicts.json``'s own ``"oscillating": true``
  entries rather than against "more than zero", because zero escalations and
  twenty-five escalations both look like a pass to a check that only asserts
  consistency with whatever the run happened to find.
"""

from __future__ import annotations

import json

from recon.invariants.grading import golden_dir
from recon.sensitive import BIRTH_STATUSES, STATUS_SENSITIVE_HOLD, classify
from recon.suite.checks import CheckResult
from recon.suite.mirror import MIRROR_TABLES
from recon.suite.pipeline import pipeline

__all__ = [
    "OSCILLATION_DEDUP",
    "PROPOSAL_SAFETY",
    "check_oscillation_dedup",
    "check_proposal_safety",
    "golden_oscillating",
]

PROPOSAL_SAFETY = "proposal-safety"
OSCILLATION_DEDUP = "oscillation-dedup"

_DETAIL_LIMIT = 5

#: Contract SS6 holds every one of these on the strength of the type alone.
ALWAYS_HELD_TYPES = ("C14",)


def golden_oscillating() -> int:
    """How many committed golden entries are marked ``"oscillating": true``."""
    entries = json.loads((golden_dir() / "conflicts.json").read_text(encoding="utf-8"))
    return sum(1 for entry in entries if entry.get("oscillating"))


def check_proposal_safety() -> CheckResult:
    """One proposal per conflict, every sensitive target held, mirror untouched."""
    run = pipeline()
    report = run.report_first
    rows = run.proposals
    failures: list[str] = []

    # -- 1. N -> N, counted three independent ways ---------------------------
    conflicts_seen = report.conflicts_seen
    if not conflicts_seen:
        failures.append(
            "the run saw ZERO conflicts, so 'N conflicts -> N proposals' is "
            "0 -> 0 and grades nothing"
        )
    if report.proposed != conflicts_seen:
        failures.append(
            f"the run reported {report.proposed} proposals for {conflicts_seen} conflicts "
            f"(skipped fingerprint={report.skipped_fingerprint}, "
            f"oscillation={report.skipped_oscillation})"
        )
    if len(rows) != conflicts_seen:
        failures.append(
            f"the proposals table holds {len(rows)} rows for {conflicts_seen} conflicts"
        )

    # -- 2. birth vocabulary --------------------------------------------------
    born_wrong = sorted({row.status for row in rows} - set(BIRTH_STATUSES))
    if born_wrong:
        failures.append(f"proposals born with a status outside the birth vocabulary: {born_wrong}")

    # -- 3. holds, recomputed from the committed classifier -------------------
    held = sum(1 for row in rows if row.status == STATUS_SENSITIVE_HOLD)
    c14_total = sum(1 for row in rows if row.conflict_type in ALWAYS_HELD_TYPES)
    c14_unheld = [
        row.fingerprint
        for row in rows
        if row.conflict_type in ALWAYS_HELD_TYPES and row.status != STATUS_SENSITIVE_HOLD
    ]
    if c14_total == 0:
        failures.append(
            "no C14 proposal exists in this run, so 'every C14 is held' is vacuously true"
        )
    if c14_unheld:
        failures.append(f"{len(c14_unheld)} C14 proposal(s) not held: {c14_unheld[:_DETAIL_LIMIT]}")

    misclassified: list[str] = []
    sensitive_targets = 0
    for row in rows:
        verdict = classify(row.conflict_type, row.disagreeing_fields)
        if verdict.status == STATUS_SENSITIVE_HOLD:
            sensitive_targets += 1
        if verdict.status != row.status:
            misclassified.append(
                f"{row.conflict_type} {row.fingerprint[:12]}: stored {row.status}, "
                f"classifier says {verdict.status}"
            )
        if verdict.sensitive != row.sensitive:
            misclassified.append(
                f"{row.conflict_type} {row.fingerprint[:12]}: stored sensitive="
                f"{row.sensitive}, classifier says {verdict.sensitive}"
            )
    if misclassified:
        failures.append(
            f"{len(misclassified)} proposal(s) disagree with recon.sensitive.classify: "
            + "; ".join(misclassified[:_DETAIL_LIMIT])
        )

    # -- 4. the read-only mirror ---------------------------------------------
    changed = run.mirror_before.changed_tables(run.mirror_after)
    if changed:
        failures.append(
            "the reconciler modified the read-only mirror: "
            + ", ".join(
                f"{table} ({run.mirror_before.row_counts.get(table)} -> "
                f"{run.mirror_after.row_counts.get(table)} rows)"
                for table in changed
            )
        )

    detail = (
        f"{conflicts_seen} conflicts -> {len(rows)} proposals "
        f"(pending={report.pending} sensitive_hold={held} evidence_only={report.evidence_only}); "
        f"C14 held {c14_total - len(c14_unheld)}/{c14_total}; "
        f"sensitive-target held {held}/{sensitive_targets} (recomputed by "
        f"recon.sensitive.classify); mirror {len(MIRROR_TABLES)} tables "
        f"{sum(run.mirror_before.row_counts.values())} rows byte-unchanged; "
        f"run={report.run_id}"
    )
    if failures:
        return CheckResult.failed(PROPOSAL_SAFETY, f"{detail} | " + " | ".join(failures))
    return CheckResult.passed(PROPOSAL_SAFETY, detail)


def check_oscillation_dedup() -> CheckResult:
    """A second pass proposes zero; oscillating conflicts escalate."""
    run = pipeline()
    first, second = run.report_first, run.report_second
    failures: list[str] = []

    if second.proposed != 0:
        failures.append(
            f"the second reconcile proposed {second.proposed} new proposal(s) over an "
            "unchanged database; R16's fingerprint dedup is not holding"
        )
    if second.skipped_fingerprint != first.conflicts_seen:
        failures.append(
            f"the second reconcile skipped {second.skipped_fingerprint} of "
            f"{first.conflicts_seen} conflicts for fingerprint dedup"
        )

    expected_oscillating = golden_oscillating()
    detected_oscillating = sum(1 for row in run.proposals if row.oscillating)
    escalated = sum(1 for row in run.proposals if row.conflict_status == "escalated")
    escalated_and_oscillating = sum(
        1 for row in run.proposals if row.oscillating and row.conflict_status == "escalated"
    )

    if expected_oscillating == 0:
        failures.append(
            "golden/conflicts.json marks no entry oscillating, so 'oscillating "
            "conflicts escalate' cannot be observed in this dataset"
        )
    if detected_oscillating != expected_oscillating:
        failures.append(
            f"{detected_oscillating} conflicts carry oscillating=true; "
            f"golden/conflicts.json marks {expected_oscillating}"
        )
    if escalated_and_oscillating != detected_oscillating:
        failures.append(
            f"{detected_oscillating - escalated_and_oscillating} oscillating conflict(s) "
            "were not escalated"
        )
    if escalated != escalated_and_oscillating:
        failures.append(
            f"{escalated - escalated_and_oscillating} conflict(s) are escalated without "
            "carrying oscillating=true"
        )
    if first.escalated_oscillation != escalated_and_oscillating:
        failures.append(
            f"the run reported {first.escalated_oscillation} escalations but "
            f"{escalated_and_oscillating} are in the conflicts table"
        )

    lineage_note = f"lineage {first.lineage_rows} rows over {first.lineage_generations} generations"
    detail = (
        f"second pass proposed {second.proposed} "
        f"(skipped_fingerprint={second.skipped_fingerprint}/{first.conflicts_seen}); "
        f"oscillating {detected_oscillating}/{expected_oscillating} golden, all escalated="
        f"{escalated_and_oscillating}; {lineage_note}"
    )
    if not first.escalation_reason_persisted:
        detail += (
            "; NOTE conflicts.escalation_reason is not writable by recon_writer, so the "
            "reason lives in the conflict.escalated audit row only"
        )
    if failures:
        return CheckResult.failed(OSCILLATION_DEDUP, f"{detail} | " + " | ".join(failures))
    return CheckResult.passed(OSCILLATION_DEDUP, detail)
