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

from sqlalchemy import text

from recon.db import get_engine
from recon.invariants.grading import golden_dir
from recon.reconciler import ESCALATION_OSCILLATION
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

#: The fallback the degraded branch of :func:`check_oscillation_dedup` names, read
#: back instead of asserted in prose. Counted over the same population as
#: ``escalated`` -- proposals joined to their conflict, exactly as
#: ``recon.suite.pipeline._PROPOSAL_ROWS`` joins them -- so the two numbers compare.
#:
#: The LATERAL takes the **latest** ``conflict.escalated`` row for the fingerprint,
#: not any of them. ``audit_log`` is append-only and no graded step truncates it, so
#: an `EXISTS` here passes on a row an earlier run wrote: with the reason dropped
#: from the audit body, ``EXISTS`` still found 25/25 and the check still said PASS.
#: A conflict with no audit row at all is dropped by the join and is therefore
#: counted as missing, which is what it is.
_REASON_IN_AUDIT_ROW = text(
    """
    SELECT count(*) AS n
      FROM proposals p
      JOIN conflicts c ON c.id = p.conflict_id
      JOIN LATERAL (
               SELECT a.detail -> 'body' ->> 'label' AS label
                 FROM audit_log a
                WHERE a.action = 'conflict.escalated'
                  AND a.subject = c.fingerprint
                ORDER BY a.id DESC
                LIMIT 1
           ) latest ON true
     WHERE c.status = 'escalated'
       AND latest.label = :label
    """
)


def escalation_reasons_in_audit() -> int:
    """Escalated conflicts whose latest ``conflict.escalated`` row carries the reason.

    ``audit_log.detail`` is ``{"mode", "body_sha256", "body"}`` under ``LOG_MODE=safe``
    and ``{"mode", "body"}`` under ``full``, so ``detail -> 'body'`` is the payload in
    either mode, and ``label`` is allow-listed (``recon.privacy.SAFE_KEYS``) rather
    than tokenised -- confirmed by reading a row back, not assumed.
    """
    with get_engine().connect() as conn:
        return int(
            conn.execute(
                _REASON_IN_AUDIT_ROW, {"label": f"escalated:{ESCALATION_OSCILLATION}"}
            ).scalar_one()
        )


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

    # R16's escalation is required to record WHY. Until migration 0015,
    # `recon_writer` held no UPDATE on `conflicts.escalation_reason`, so the
    # reconciler wrote the reason to the `conflict.escalated` audit row and left
    # the column NULL -- and this check said so in a caveat. The grant exists now,
    # so the column is READ BACK rather than the caveat being deleted: the flag
    # `escalation_reason_persisted` is a `has_column_privilege` probe, and a grant
    # is not a write. Whichever way it comes out is reported below; neither
    # outcome is silence.
    reasons_written = sum(
        1
        for row in run.proposals
        if row.conflict_status == "escalated" and row.escalation_reason is not None
    )
    audited_reasons = -1
    if first.escalation_reason_persisted:
        if reasons_written != escalated:
            failures.append(
                f"recon_writer holds UPDATE on conflicts.escalation_reason (migration 0015) "
                f"but only {reasons_written} of {escalated} escalated conflict(s) carry a "
                f"reason on the row"
            )
    else:
        # The branch that used to assert nothing. Without the grant this check was
        # a PASS with a note, so an `alembic downgrade` past 0015 or a stray REVOKE
        # switched a graded assertion off while the scorecard still printed PASS --
        # and the note's consolation ("the reason lives in the audit row") was
        # never itself checked. It is checked here.
        audited_reasons = escalation_reasons_in_audit()
        if audited_reasons != escalated:
            failures.append(
                f"conflicts.escalation_reason is not writable by recon_writer AND only "
                f"{audited_reasons} of {escalated} escalated conflict(s) carry the reason "
                f"in a conflict.escalated audit row: R16's escalation recorded WHY nowhere"
            )

    lineage_note = f"lineage {first.lineage_rows} rows over {first.lineage_generations} generations"
    detail = (
        f"second pass proposed {second.proposed} "
        f"(skipped_fingerprint={second.skipped_fingerprint}/{first.conflicts_seen}); "
        f"oscillating {detected_oscillating}/{expected_oscillating} golden, all escalated="
        f"{escalated_and_oscillating}; {lineage_note}"
    )
    if first.escalation_reason_persisted:
        # The positive half. It reports the READ-BACK count, not the privilege:
        # "granted" is a fact about the catalogue and `escalation_reason=N/N` is a
        # fact about the rows, and only the second one is evidence that R16's
        # escalation recorded its reason where a reviewer can see it.
        detail += f"; escalation_reason on the row {reasons_written}/{escalated} (migration 0015)"
    else:
        detail += (
            "; NOTE conflicts.escalation_reason is not writable by recon_writer; "
            f"reason in the conflict.escalated audit row {audited_reasons}/{escalated}"
        )
    if failures:
        return CheckResult.failed(OSCILLATION_DEDUP, f"{detail} | " + " | ".join(failures))
    return CheckResult.passed(OSCILLATION_DEDUP, detail)
