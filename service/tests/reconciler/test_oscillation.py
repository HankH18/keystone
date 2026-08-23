"""R16/SS7: A -> B -> A escalation, and never re-proposing the identical fix.

READ THIS BEFORE TRUSTING THE GREEN
------------------------------------
The session fixture ingests **generation 3 only**, so ``field_lineage`` is empty
in this database and contract SS7's A -> B -> A scan has nothing to read. These
tests therefore **write the lineage themselves** -- and everything downstream of
that write is the real path:

* the rows go into the real ``field_lineage`` table, through the real schema,
  keyed on the real ``person_key`` of a **real conflict** the committed invariant
  engine detected on the committed fixtures;
* they are read back by ``recon.invariants.oscillation.scan_field_lineage`` --
  the committed scan, not a re-implementation;
* the conflict is escalated, scored and suppressed by the real reconciler.

**What that does NOT prove:** that the ingest/ER path *populates* ``field_lineage``
for generations 1-2, and therefore that the oscillating conflicts are found
without a test writing the rows.

That half was measured separately rather than assumed. In a scratch database with
generations **1-3** ingested and ``recon.resolve.materialize`` run over all three,
``field_lineage`` held 1,279,575 real rows, the committed A -> B -> A scan found
**25** oscillating conflicts unaided, the reconciler escalated all 25 to
``escalated:oscillation`` with the -0.25 penalty and 25 ``conflict.escalated``
audit rows, and after a reviewer rejected all 25 the next run refused exactly
those 25 identical fixes (``skipped_oscillation=25``) while the remaining 4,734
were blocked by ordinary fingerprint dedup. So the path does work end to end.

It is not reproduced *here* for one specific reason, and it is a real constraint
rather than a cost: ingesting generations 1-2 changes the detected conflict set
away from the graded one (4,759 conflicts instead of golden's 3,050 -- the
invariant engine's generation filtering is incomplete, an upstream matter). This
suite asserts proposal COUNTS against the graded set, so it cannot also be the
suite that ingests generations 1-2. The two configurations are exercised
separately and the ticket report says so.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Connection, text

from recon.invariants.oscillation import scan_field_lineage
from recon.reconciler import (
    OSCILLATION_FROM_ROW,
    OSCILLATION_FROM_SCAN,
    OSCILLATION_NO_INPUT,
    SKIP_OSCILLATION,
    ConflictRow,
    oscillation_state,
    reconcile,
)
from recon.reference import person_key

pytestmark = pytest.mark.usefixtures("conflict_store")


# =====================================================================================
# the decision function, with no database at all
# =====================================================================================
def _row(**kwargs: object) -> ConflictRow:
    defaults: dict[str, object] = {
        "id": 1,
        "fingerprint": "fp",
        "type": "C6",
        "rule_id": "R-006",
        "entity_refs": ("appdb:student:s1", "crm:contact:c1"),
        "sources_involved": ("appdb", "crm"),
        "disagreeing_fields": ("appdb.student.grade", "crm.contact.grade"),
        "observed_values": {},
        "oscillating": False,
        "status": "open",
        "escalation_reason": None,
    }
    defaults.update(kwargs)
    return ConflictRow(**defaults)  # type: ignore[arg-type]


class _Scan:
    """A stand-in for `LineageScan` with the two attributes the decision reads."""

    def __init__(self, pairs: set[tuple[str, str]], rows: int) -> None:
        self.pairs = pairs
        self.rows = rows

    @property
    def had_input(self) -> bool:
        return self.rows > 0

    def oscillates(self, canonical_id: str, field_path: str) -> bool:
        return (canonical_id, field_path) in self.pairs


def test_an_empty_lineage_scan_is_reported_as_no_input_not_as_a_false() -> None:
    """The distinction the whole module exists to preserve.

    A scan over an empty table returning nothing means "there was nothing to
    scan". Reporting that as a confident "does not oscillate" is the failure this
    label prevents, and it is why the packet records which input answered.
    """
    oscillating, source = oscillation_state(_row(), _Scan(set(), rows=0))
    assert oscillating is False
    assert source == OSCILLATION_NO_INPUT


def test_a_scan_with_rows_that_finds_nothing_is_a_real_negative() -> None:
    oscillating, source = oscillation_state(_row(), _Scan(set(), rows=5000))
    assert oscillating is False
    assert source == OSCILLATION_FROM_SCAN


def test_the_live_scan_finds_an_oscillation_the_stored_flag_missed() -> None:
    conflict = _row(oscillating=False)
    key = str(person_key(conflict.entity_refs))
    scan = _Scan({(key, "crm.contact.grade")}, rows=10)
    oscillating, source = oscillation_state(conflict, scan)
    assert oscillating is True
    assert source == OSCILLATION_FROM_SCAN


def test_the_stored_flag_is_used_when_there_is_no_lineage_to_scan() -> None:
    oscillating, source = oscillation_state(_row(oscillating=True), _Scan(set(), rows=0))
    assert oscillating is True
    assert source == OSCILLATION_FROM_ROW


def test_only_the_two_types_that_carry_disagreeing_fields_can_oscillate() -> None:
    """SS7 marks a conflict oscillating "where the conflict's FIELD oscillated"."""
    conflict = _row(type="C12", disagreeing_fields=())
    key = str(person_key(conflict.entity_refs))
    scan = _Scan({(key, "crm.contact.grade")}, rows=10)
    assert oscillation_state(conflict, scan) == (False, OSCILLATION_FROM_SCAN)


# =====================================================================================
# the real path, with lineage this test writes
# =====================================================================================
@pytest.fixture
def owner(conflict_store: str) -> Iterator[Connection]:
    """A schema-owner connection, rolled back.

    Deliberately the owner rather than ``recon_writer``: this test has to play a
    reviewer *rejecting* a proposal inside the same transaction as the run that
    created it, and only the review role (or the owner, which migration 0006
    binds to the same transition graph) may move a proposal to ``rejected``.
    The write BOUNDARY is tested as ``recon_writer`` in ``test_reconcile_run.py``;
    what is under test here is the POLICY.
    """
    del conflict_store
    from recon.db import get_engine

    with get_engine().connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


def _a_real_c6_conflict(conn: Connection) -> tuple[int, str, tuple[str, ...], tuple[str, ...]]:
    row = conn.execute(
        text(
            "SELECT id, fingerprint, entity_refs, disagreeing_fields FROM conflicts "
            " WHERE type = 'C6' AND jsonb_array_length(disagreeing_fields) > 0 "
            " ORDER BY fingerprint LIMIT 1"
        )
    ).one()
    return row.id, row.fingerprint, tuple(row.entity_refs), tuple(row.disagreeing_fields)


def _write_aba_lineage(conn: Connection, canonical_id: str, field_path: str) -> None:
    """Give ONE ``(canonical_id, field)`` an A -> B -> A history: A, then B, then A.

    The existing generation-3 rows for that pair are removed first. The session
    fixture materializes generation 3, so ``field_lineage`` already holds ~426,000
    real rows and this pair already has a generation-3 entry; leaving it in place
    would leave the pair's newest value coming from the real row (the scan takes
    the lexicographically smallest ``source_ref`` per generation, SS4.6's
    tiebreak) and no A -> B -> A would exist. Replacing that pair's history is
    what "this field oscillated" means, and it leaves every other pair's real
    lineage untouched -- so the scan has to find one planted pattern inside
    426,000 genuine rows rather than in an empty table.
    """
    conn.execute(
        text(
            "DELETE FROM field_lineage WHERE canonical_id = CAST(:cid AS uuid) AND field = :field"
        ),
        {"cid": canonical_id, "field": field_path},
    )
    conn.execute(
        text(
            "INSERT INTO field_lineage (canonical_id, field, value_text, source_id, "
            "source_ref, generation) VALUES "
            "(CAST(:cid AS uuid), :field, 'A', 'crm', 'crm:contact:probe', 1), "
            "(CAST(:cid AS uuid), :field, 'B', 'crm', 'crm:contact:probe', 2), "
            "(CAST(:cid AS uuid), :field, 'A', 'crm', 'crm:contact:probe', 3)"
        ),
        {"cid": canonical_id, "field": field_path},
    )


def test_the_committed_scan_finds_the_pattern_this_test_wrote(owner: Connection) -> None:
    """First, prove the scan sees it -- otherwise everything below is vacuous.

    Both halves are asserted: the pair does NOT oscillate over the real,
    materialized generation-3 lineage, and it DOES once a history is written for
    it. Without the first half a scan that returned every pair would pass.
    """
    _, _, entity_refs, paths = _a_real_c6_conflict(owner)
    canonical_id = str(person_key(entity_refs))

    before = scan_field_lineage(owner.connection.driver_connection)
    assert before.had_input is True, "the fixture materializes generation 3; lineage is not empty"
    assert before.rows > 100_000, f"only {before.rows} lineage rows -- did materialize run?"
    assert not before.oscillates(canonical_id, paths[0])
    assert before.pairs == frozenset(), (
        "generation-3-only lineage cannot contain an A -> B -> A pattern, so the "
        f"scan must find none; it found {sorted(before.pairs)[:3]}"
    )

    _write_aba_lineage(owner, canonical_id, paths[0])

    scan = scan_field_lineage(owner.connection.driver_connection)
    assert scan.had_input is True
    assert scan.oscillates(canonical_id, paths[0])
    assert scan.pairs == frozenset({(canonical_id, paths[0])}), (
        "exactly one pair was given a history; the scan must not invent others"
    )


def test_an_oscillating_conflict_is_marked_escalated_oscillation(owner: Connection) -> None:
    conflict_id, fingerprint, entity_refs, paths = _a_real_c6_conflict(owner)
    _write_aba_lineage(owner, str(person_key(entity_refs)), paths[0])

    report = reconcile(conn=owner, run_id="t7-osc-escalate")

    assert report.lineage_rows > 0
    assert report.escalated_oscillation == 1
    status, reason = owner.execute(
        text("SELECT status::text, escalation_reason FROM conflicts WHERE id = :id"),
        {"id": conflict_id},
    ).one()
    assert (status, reason) == ("escalated", "oscillation")

    # SS7's label, as the dashboard renders it.
    assert f"{status}:{reason}" == "escalated:oscillation"

    audited = owner.execute(
        text(
            "SELECT count(*) FROM audit_log WHERE action = 'conflict.escalated' AND subject = :fp"
        ),
        {"fp": fingerprint},
    ).scalar_one()
    assert audited == 1


def test_only_the_oscillating_conflict_is_escalated(owner: Connection) -> None:
    """A blast-radius check: escalation must not spill onto the other 4,000."""
    _, _, entity_refs, paths = _a_real_c6_conflict(owner)
    _write_aba_lineage(owner, str(person_key(entity_refs)), paths[0])

    reconcile(conn=owner, run_id="t7-osc-scope")

    escalated = owner.execute(
        text("SELECT count(*) FROM conflicts WHERE status = 'escalated'")
    ).scalar_one()
    assert escalated == 1


def test_oscillation_lowers_the_confidence_by_exactly_the_committed_weight(
    owner: Connection,
) -> None:
    """-0.25, the heaviest penalty in the model, applied to a real conflict."""
    conflict_id, fingerprint, entity_refs, paths = _a_real_c6_conflict(owner)

    baseline = reconcile(conn=owner, run_id="t7-osc-baseline")
    before = owner.execute(
        text("SELECT confidence FROM proposals WHERE fingerprint = :fp"), {"fp": fingerprint}
    ).scalar_one()
    assert baseline.proposed > 0
    owner.execute(text("DELETE FROM audit_log"))
    owner.execute(text("DELETE FROM proposals"))

    _write_aba_lineage(owner, str(person_key(entity_refs)), paths[0])
    reconcile(conn=owner, run_id="t7-osc-scored")
    after, evidence = owner.execute(
        text("SELECT confidence, evidence FROM proposals WHERE fingerprint = :fp"),
        {"fp": fingerprint},
    ).one()

    assert Decimal(before) - Decimal(after) == Decimal("0.2500")
    assert evidence["oscillation"]["observed"] is True
    assert evidence["oscillation"]["decided_by"] == OSCILLATION_FROM_SCAN
    assert evidence["oscillation"]["lineage_rows"] > 0
    assert evidence["confidence"]["signals"]["oscillation_observed"] is True
    del conflict_id


def _reject(conn: Connection, fingerprint: str) -> None:
    conn.execute(
        text(
            "UPDATE proposals SET status = 'rejected', decided_by = 'reviewer:test', "
            "decided_at = now() WHERE fingerprint = :fp"
        ),
        {"fp": fingerprint},
    )


def test_a_rejected_fix_is_never_re_proposed_while_the_field_oscillates(
    owner: Connection,
) -> None:
    """R16's actual bite, and the case fingerprint dedup deliberately does not cover.

    A rejected proposal frees the fingerprint (the unique index is partial on
    ``status <> 'rejected'``). Without the oscillation rule the next run would
    re-propose the identical fix a human just refused, on a field the source keeps
    re-asserting -- which is exactly what R16 forbids.
    """
    _, fingerprint, entity_refs, paths = _a_real_c6_conflict(owner)
    _write_aba_lineage(owner, str(person_key(entity_refs)), paths[0])

    first = reconcile(conn=owner, run_id="t7-osc-r1")
    assert any(o.fingerprint == fingerprint and o.proposed for o in first.outcomes)

    _reject(owner, fingerprint)

    second = reconcile(conn=owner, run_id="t7-osc-r2")
    outcome = next(o for o in second.outcomes if o.fingerprint == fingerprint)
    assert outcome.proposed is False
    assert outcome.skip_reason == SKIP_OSCILLATION
    assert second.skipped_oscillation == 1

    live = owner.execute(
        text("SELECT count(*) FROM proposals WHERE fingerprint = :fp AND status <> 'rejected'"),
        {"fp": fingerprint},
    ).scalar_one()
    assert live == 0


def test_the_control_a_rejected_fix_that_does_not_oscillate_IS_re_proposed(
    owner: Connection,
) -> None:
    """The sabotage check: prove the suppression came from OSCILLATION.

    Same setup, same rejection, no lineage. If this conflict were also suppressed,
    the test above would be evidence of nothing but fingerprint bookkeeping.
    """
    _, fingerprint, _, _ = _a_real_c6_conflict(owner)

    first = reconcile(conn=owner, run_id="t7-ctl-r1")
    assert any(o.fingerprint == fingerprint and o.proposed for o in first.outcomes)

    _reject(owner, fingerprint)

    second = reconcile(conn=owner, run_id="t7-ctl-r2")
    outcome = next(o for o in second.outcomes if o.fingerprint == fingerprint)
    assert outcome.proposed is True, (
        "without oscillation a rejected fingerprint is proposable again; if this "
        "fails, the oscillation test above proves nothing about oscillation"
    )
    assert second.skipped_oscillation == 0
    assert second.proposed == 1


def test_an_oscillating_conflict_still_gets_its_FIRST_proposal(owner: Connection) -> None:
    """R16 forbids re-proposing the identical fix; the first one is not a re-propose.

    The conflict is escalated and the score carries the penalty, but it does reach
    a human -- a silently dropped conflict would be the worse failure.
    """
    _, fingerprint, entity_refs, paths = _a_real_c6_conflict(owner)
    _write_aba_lineage(owner, str(person_key(entity_refs)), paths[0])

    report = reconcile(conn=owner, run_id="t7-osc-first")
    outcome = next(o for o in report.outcomes if o.fingerprint == fingerprint)
    assert outcome.proposed is True
    assert outcome.escalated is True
    assert report.escalated_oscillation == 1
