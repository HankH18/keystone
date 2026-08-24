"""The suite reads `conflicts.escalation_reason` off the row (migration 0015).

Until migration 0015, `recon_writer` held UPDATE on `conflicts` for
`(status, last_seen_run)` only, so `recon.reconciler._escalate` wrote the reason
into the `conflict.escalated` audit row and left the column NULL -- and the
grading harness carried a caveat saying so, on `recon.suite.pipeline`'s
`PipelineRun.notes` and in the `oscillation-dedup` row's detail.

0015 granted the column, so the caveat is no longer true, and the fix is **not**
to delete it: `ReconcileReport.escalation_reason_persisted` is a
`has_column_privilege` probe, and *a grant is not a write*. So the harness now
carries `escalation_reason` down from the row itself
(`recon.suite.pipeline._PROPOSAL_ROWS`) and `recon.suite.proposals.
check_oscillation_dedup` fails when the privilege is held but the reason is
absent from an escalated conflict.

That plumbing is what this module pins, against a real migrated database:

* the query selects the column and the mapping surfaces it -- a query that
  compiles is not a query that returns what you think it does;
* an escalated conflict's reason arrives as a string, and a conflict that is not
  escalated arrives as `None` rather than as `""` or as a missing attribute.

**Its own database.** Nothing here touches the one `DATABASE_URL` names or the
one `tests/integration/conftest.py` shares: the rows below would land inside
other modules' `conflicts` / `proposals` counts, and this module's own results
would then depend on collection order. The process is never repointed
(`use_database` is deliberately not called), so a suite that grabbed an engine
earlier keeps the engine it had.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, create_engine, text

from recon.suite.pipeline import ProposalRow, _read_proposals
from tests.er.scratchdb import create_scratch_database, drop_database
from tests.schema.conftest import REQUIRE_DB_REASON, SKIP_REASON, database_is_required

#: Written into `first_seen_run` / `created_run`, so every row here is traceable.
RUN_TAG = "escalation-reason-reporting"

#: A conflict that escalated and recorded why, and one that did neither. Both
#: fingerprints are literals rather than `recon.reference.fingerprint` output:
#: nothing here asserts anything about fingerprint derivation, and a literal
#: cannot collide with a golden row.
ESCALATED_FINGERPRINT = "0" * 64
OPEN_FINGERPRINT = "1" * 64

#: The reason `recon.reconciler._escalate` writes for R16's A->B->A case. Spelled
#: out rather than imported so that a rename there is visible here as a failure
#: rather than being silently renamed along with it.
ESCALATION_REASON = "oscillation"

_INSERT_CONFLICT = text(
    """
    INSERT INTO conflicts
        (fingerprint, type, rule_id, entity_refs, sources, disagreeing_fields,
         observed_values, oscillating, status, escalation_reason,
         first_seen_run, last_seen_run)
    VALUES (:fingerprint, :type, :rule_id, CAST(:entity_refs AS jsonb),
            CAST(:sources AS jsonb), CAST(:fields AS jsonb),
            CAST(:observed AS jsonb), :oscillating,
            CAST(:status AS conflict_status), :reason, :run, :run)
    RETURNING id
    """
)

_INSERT_PROPOSAL = text(
    """
    INSERT INTO proposals
        (conflict_id, fingerprint, action, confidence, evidence, status,
         sensitive, created_run, target_canonical_id)
    VALUES (:conflict_id, :fingerprint, CAST(:action AS jsonb), :confidence,
            CAST(:evidence AS jsonb), CAST(:status AS proposal_status),
            false, :run, :target)
    """
)


@pytest.fixture(scope="module")
def scratch_engine() -> Iterator[Engine]:
    """A migrated database of this module's own, dropped when it is done."""
    try:
        dsn = create_scratch_database("escreason")
    except RuntimeError as exc:  # DATABASE_URL supplies the server coordinates
        if database_is_required():
            pytest.fail(f"{REQUIRE_DB_REASON} ({exc})", pytrace=False)
        pytest.skip(SKIP_REASON)
    engine = create_engine(dsn.replace("postgresql://", "postgresql+psycopg://"), future=True)
    try:
        yield engine
    finally:
        engine.dispose()
        drop_database(dsn)


@pytest.fixture(scope="module")
def rows(scratch_engine: Engine) -> tuple[ProposalRow, ...]:
    """One escalated conflict carrying a reason, one open conflict carrying none."""
    with scratch_engine.begin() as conn:
        for fingerprint, status, reason, oscillating in (
            (ESCALATED_FINGERPRINT, "escalated", ESCALATION_REASON, True),
            (OPEN_FINGERPRINT, "open", None, False),
        ):
            conflict_id = int(
                conn.execute(
                    _INSERT_CONFLICT,
                    {
                        "fingerprint": fingerprint,
                        "type": "C6",
                        "rule_id": "R-006",
                        "entity_refs": json.dumps(["appdb:student:1"]),
                        "sources": json.dumps(["appdb", "crm"]),
                        "fields": json.dumps(["appdb.student.grade"]),
                        "observed": json.dumps({"appdb.student.grade": "5"}),
                        "oscillating": oscillating,
                        "status": status,
                        "reason": reason,
                        "run": RUN_TAG,
                    },
                ).scalar_one()
            )
            conn.execute(
                _INSERT_PROPOSAL,
                {
                    "conflict_id": conflict_id,
                    "fingerprint": fingerprint,
                    # A non-sensitive write set: `ck_proposals_sensitive_covers_
                    # write_set` refuses a sensitive path on a `sensitive=false`
                    # proposal, which is the boundary this module is not testing.
                    "action": json.dumps({"set": {"appdb.student.grade": "5"}}),
                    "confidence": "0.9000",
                    "evidence": json.dumps({"sources": ["appdb", "crm"]}),
                    "status": "pending",
                    "run": RUN_TAG,
                    "target": str(uuid.UUID(int=conflict_id)),
                },
            )

    with scratch_engine.connect() as conn:
        return _read_proposals(conn)


def test_both_rows_are_read(rows: tuple[ProposalRow, ...]) -> None:
    """A guard on the fixture itself: an empty read would make the rest vacuous."""
    assert len(rows) == 2, rows
    assert {row.fingerprint for row in rows} == {ESCALATED_FINGERPRINT, OPEN_FINGERPRINT}


def test_the_escalated_conflicts_reason_arrives_from_the_row(
    rows: tuple[ProposalRow, ...],
) -> None:
    """The column 0015 granted, surfaced by the harness's own query.

    This is the assertion the deleted caveat should have become. Before 0015 the
    column was NULL on every escalated conflict, so `check_oscillation_dedup`
    could only report the privilege; now it reports the value.
    """
    escalated = next(row for row in rows if row.fingerprint == ESCALATED_FINGERPRINT)
    assert escalated.conflict_status == "escalated"
    assert escalated.escalation_reason == ESCALATION_REASON


def test_a_conflict_that_did_not_escalate_carries_no_reason(
    rows: tuple[ProposalRow, ...],
) -> None:
    """`None`, not `""` -- the check counts reasons and an empty string would count."""
    open_row = next(row for row in rows if row.fingerprint == OPEN_FINGERPRINT)
    assert open_row.conflict_status == "open"
    assert open_row.escalation_reason is None


def test_the_harness_query_actually_selects_the_column() -> None:
    """The negative case the two tests above cannot see.

    If `_PROPOSAL_ROWS` stopped selecting `escalation_reason`, `_read_proposals`
    would raise rather than return `None` -- but a future edit that reintroduced a
    literal `escalation_reason=None` in the mapping would make both tests above
    fail for a reason that reads like a database problem. Naming the query here
    makes that edit fail with the truth.
    """
    from recon.suite.pipeline import _PROPOSAL_ROWS

    assert "escalation_reason" in str(_PROPOSAL_ROWS)


def test_the_audit_row_fallback_is_counted_off_the_latest_row(
    scratch_engine: Engine, rows: tuple[ProposalRow, ...]
) -> None:
    """The other half of the caveat, against a real database.

    When `recon_writer` does not hold the column, `check_oscillation_dedup` says
    the reason "lives in the `conflict.escalated` audit row" -- and now counts it,
    with `recon.suite.proposals._REASON_IN_AUDIT_ROW`. `tests/suite/
    test_oscillation_report.py` supplies that count as a fixture, so this is the
    only place the SQL itself runs, over a row written by the real redacting
    writer under the configured `LOG_MODE` rather than by a hand-built INSERT.

    Three legs, in one transaction that is rolled back so the module's other
    tests see the database they set up:

    * no audit row -> 0. The `JOIN LATERAL` drops the conflict rather than
      counting it, which is what a conflict that recorded nothing deserves;
    * the row the reconciler writes -> 1;
    * a LATER row for the same fingerprint without the label -> back to 0. The
      count is over the *latest* row, not any row: `audit_log` is append-only and
      no graded step truncates it, so an `EXISTS` here would keep passing on a
      row an earlier run wrote.
    """
    from recon.logging import insert_audit_row
    from recon.reconciler import AUDIT_ACTOR, ESCALATION_OSCILLATION
    from recon.suite.proposals import _REASON_IN_AUDIT_ROW

    label = f"escalated:{ESCALATION_OSCILLATION}"

    def counted(conn: Connection) -> int:
        return int(conn.execute(_REASON_IN_AUDIT_ROW, {"label": label}).scalar_one())

    def escalated(conn: Connection, *, body: dict[str, object]) -> None:
        insert_audit_row(
            conn,
            actor=AUDIT_ACTOR,
            action="conflict.escalated",
            subject=ESCALATED_FINGERPRINT,
            body=body,
        )

    with scratch_engine.connect() as conn:
        transaction = conn.begin()
        try:
            assert counted(conn) == 0, "counted an escalated conflict that has no audit row"

            escalated(conn, body={"status": "escalated", "label": label})
            assert counted(conn) == 1, (
                "the reason the reconciler writes into the conflict.escalated audit row "
                "is not visible to the fallback count that reports it"
            )

            escalated(conn, body={"status": "escalated"})
            assert counted(conn) == 0, (
                "a later conflict.escalated row that recorded no reason still counted as "
                "one: the fallback is reading any row rather than the latest, so a reason "
                "dropped from the audit body would go on passing forever"
            )
        finally:
            transaction.rollback()
