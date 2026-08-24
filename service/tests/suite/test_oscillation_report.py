"""What the `oscillation-dedup` row says about `conflicts.escalation_reason`.

The row used to carry a caveat -- *"NOTE conflicts.escalation_reason is not
writable by recon_writer, so the reason lives in the conflict.escalated audit row
only"* -- appended only when
`ReconcileReport.escalation_reason_persisted` was `False`. Migration 0015 granted
the column, so on a current database that branch is dead and the row said
**nothing at all** about a property R16 requires.

Silence is the wrong report for a property that is still worth checking, and
deleting a caveat is not the same as repairing what it described. So the check now
reports in both directions, and the positive direction reports the READ-BACK
count rather than the privilege: `escalation_reason_persisted` is a
`has_column_privilege` probe issued once per run, and *a grant is not a write*.

This module drives `check_oscillation_dedup` against a hand-built
:class:`~recon.suite.pipeline.PipelineRun` because the real one needs the fully
loaded graded database (`make suite`, ~6 minutes of `POST /internal/sync` before
it can start). What is faked is the *pipeline result* and, in the two degraded
cases, the *audit-row count* -- `escalation_reasons_in_audit` queries the live
database, which a hand-built run has no way to populate. The decision under test
-- which string the row carries, and whether a missing reason is a failure in
either direction -- is the real code path.

`tests/integration/test_escalation_reason_reporting.py` is the other half. It
proves against a real migrated database that `ProposalRow.escalation_reason`
actually comes off the row, and that the SQL behind `escalation_reasons_in_audit`
counts what its name says over audit rows written by the real redacting writer --
which is what makes the counts faked here mean anything.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from recon.reconciler import ReconcileReport
from recon.suite.mirror import MirrorDigest
from recon.suite.pipeline import PipelineRun, Precondition, ProposalRow
from recon.suite.proposals import check_oscillation_dedup, golden_oscillating

CONFLICTS = 3050

#: The committed golden set's oscillating count. Read from the file rather than
#: written down, because the check reads it from the file too -- a literal here
#: would pass for the wrong reason on the day the dataset changes.
OSCILLATING = golden_oscillating()

EMPTY_MIRROR = MirrorDigest(digests={}, row_counts={})


def _report(*, persisted: bool, escalated: int) -> ReconcileReport:
    return ReconcileReport(
        run_id="fake-first",
        generation=3,
        conflicts_seen=CONFLICTS,
        proposed=CONFLICTS,
        pending=CONFLICTS - 380,
        sensitive_hold=380,
        evidence_only=1950,
        skipped_fingerprint=0,
        skipped_oscillation=0,
        escalated_oscillation=escalated,
        rationale_attached=0,
        lineage_rows=1_279_575,
        lineage_generations=3,
        escalation_reason_persisted=persisted,
        model_version=2,
        model_sha256="0" * 64,
        by_type={},
    )


def _second_pass() -> ReconcileReport:
    """The R16 second pass: nothing new proposed, every fingerprint skipped."""
    report = _report(persisted=True, escalated=0)
    return ReconcileReport(
        **{
            **{f: getattr(report, f) for f in report.__dataclass_fields__},
            "run_id": "fake-second",
            "proposed": 0,
            "pending": 0,
            "sensitive_hold": 0,
            "evidence_only": 0,
            "skipped_fingerprint": CONFLICTS,
        }
    )


def _proposals(*, escalated: int, with_reason: int) -> tuple[ProposalRow, ...]:
    """`escalated` oscillating+escalated rows, `with_reason` of them carrying one."""
    rows: list[ProposalRow] = []
    for index in range(escalated):
        rows.append(
            ProposalRow(
                proposal_id=index + 1,
                fingerprint=f"{index:064d}",
                status="pending",
                sensitive=False,
                action={"set": {"appdb.student.grade": "5"}},
                confidence="0.9000",
                conflict_type="C6",
                disagreeing_fields=("appdb.student.grade",),
                conflict_status="escalated",
                oscillating=True,
                escalation_reason="oscillation" if index < with_reason else None,
            )
        )
    return tuple(rows)


def _run(
    *, persisted: bool, escalated: int, with_reason: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _report(persisted=persisted, escalated=escalated)
    run = PipelineRun(
        started_at=datetime(2026, 8, 24, tzinfo=UTC),
        precondition=Precondition(
            landing={}, entities=43375, links=0, lineage=1_279_575, lineage_generations=3
        ),
        run_a=None,  # type: ignore[arg-type]  # the check reads neither invariant run
        run_b=None,  # type: ignore[arg-type]
        invariants_seconds=12.94,
        invariants_b_seconds=12.9,
        persist_seconds=2.72,
        mirror_before=EMPTY_MIRROR,
        mirror_after=EMPTY_MIRROR,
        report_first=first,
        report_second=_second_pass(),
        dry_a=first,
        dry_b=first,
        reconcile_seconds=7.29,
        proposals=_proposals(escalated=escalated, with_reason=with_reason),
        conflict_status={"escalated": escalated},
        fixtures_root=Path("/nonexistent"),
        dsn_database="fake",
    )
    monkeypatch.setattr("recon.suite.proposals.pipeline", lambda: run)


def test_a_granted_and_written_column_is_reported_positively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row states the property was checked, instead of saying nothing."""
    _run(persisted=True, escalated=OSCILLATING, with_reason=OSCILLATING, monkeypatch=monkeypatch)

    result = check_oscillation_dedup()

    assert result.ok, result.detail
    assert f"escalation_reason on the row {OSCILLATING}/{OSCILLATING}" in result.detail
    assert "migration 0015" in result.detail
    assert "not writable" not in result.detail


def test_a_granted_but_unwritten_column_fails_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression the old caveat could not catch.

    With the grant held, an escalated conflict whose reason never reached the row
    is a real defect -- and under the old code it was invisible in both
    directions: the caveat branch was dead and nothing checked the value.
    """
    _run(
        persisted=True,
        escalated=OSCILLATING,
        with_reason=OSCILLATING - 1,
        monkeypatch=monkeypatch,
    )

    result = check_oscillation_dedup()

    assert not result.ok
    assert "holds UPDATE on conflicts.escalation_reason" in result.detail
    assert f"only {OSCILLATING - 1} of {OSCILLATING}" in result.detail


def test_an_ungranted_column_still_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """The degraded database is reported honestly, not failed and not hidden.

    A database that predates 0015 -- or one downgraded past it -- makes the
    reconciler write the reason to the `conflict.escalated` audit row and leave
    the column NULL (`recon.reconciler._escalate`: the audit row is written on
    both paths, carrying `label = "escalated:oscillation"` either way). That is
    the documented degraded behaviour, so the row keeps the caveat and does
    **not** raise a failure for it.

    The audit-row count is supplied here rather than left to the live database.
    The `with_reason=0` fixture below models "the column is NULL", which is the
    whole point of the degraded mode; it says nothing about the audit row, and
    before this fixture existed the branch read a live count over unrelated rows
    and this case passed only because the branch asserted nothing at all.
    """
    _run(persisted=False, escalated=OSCILLATING, with_reason=0, monkeypatch=monkeypatch)
    monkeypatch.setattr("recon.suite.proposals.escalation_reasons_in_audit", lambda: OSCILLATING)

    result = check_oscillation_dedup()

    assert result.ok, result.detail
    assert "conflicts.escalation_reason is not writable by recon_writer" in result.detail
    assert f"conflict.escalated audit row {OSCILLATING}/{OSCILLATING}" in result.detail
    assert "escalation_reason on the row" not in result.detail


def test_an_ungranted_column_with_the_reason_nowhere_fails_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The degraded branch is a check, not an exemption.

    The caveat's consolation is that the reason "lives in the `conflict.escalated`
    audit row". Until that was read back, the branch asserted **nothing**: a
    `REVOKE UPDATE (escalation_reason) ON conflicts FROM recon_writer`, an
    `alembic downgrade` past 0015 or a mis-provisioned deploy switched a graded
    assertion off and the scorecard still printed PASS. One reason short of the
    escalated count is enough to fail it, in the same shape as the granted half.
    """
    _run(persisted=False, escalated=OSCILLATING, with_reason=0, monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "recon.suite.proposals.escalation_reasons_in_audit", lambda: OSCILLATING - 1
    )

    result = check_oscillation_dedup()

    assert not result.ok
    assert f"only {OSCILLATING - 1} of {OSCILLATING}" in result.detail
    assert "recorded WHY nowhere" in result.detail
