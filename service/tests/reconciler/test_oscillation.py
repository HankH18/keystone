"""R16/SS7: A -> B -> A escalation, and never re-proposing the identical fix.

WHAT THE GREEN HERE IS EVIDENCE OF
-----------------------------------
The session fixture ingests **generations 1, 2 and 3** and materializes lineage
over all three, so ``field_lineage`` holds ~1,279,575 real rows and the committed
A -> B -> A scan finds its 25 oscillating pairs **unaided**. Those 25 are exactly
the entries ``golden/conflicts.json`` marks ``"oscillating": true``, matched on
``(type, sorted(entity_refs))``. So the detection half is real: no test plants the
pattern for the pipeline to find.

The escalation half runs through a **``recon_writer``** connection -- the
principal the scheduled job actually authenticates as -- in
:func:`test_the_real_run_escalates_the_golden_25_as_recon_writer`.

WHAT THIS SUITE PREVIOUSLY PROVED, AND WHY THAT MATTERED
---------------------------------------------------------
This module used to hand-write its own lineage and run every escalation test
through a **schema owner** connection, which bypasses grants. It also asserted, in
this docstring, that the path "does work end to end", and justified not
reproducing it here with the claim that ingesting generations 1-2 "changes the
detected conflict set away from the graded one (4,759 conflicts instead of
golden's 3,050)".

Both halves were false, and together they concealed a production blocker:

* ingesting generations 1-2 does **not** change the graded set. Measured:
  generations 1, 2 and 3 ingested, ``run_invariants`` returns 3,050 conflicts in
  exactly the golden per-type distribution, because SS7 has invariants read
  generation 3 only. The fixture now does exactly that and asserts it;
* the end-to-end run under the real principal **crashed**. ``_ESCALATE_CONFLICT``
  sets ``conflicts.escalation_reason``, and migration 0004 grants ``recon_writer``
  ``UPDATE (status, last_seen_run)`` on ``conflicts`` and nothing else, so
  Postgres refused the whole statement with SQLSTATE 42501 -- inside the run's
  transaction, rolling back every proposal. ``run_once()`` wrote **zero**
  proposals the first time any conflict oscillated. Reproduced directly:
  ``UPDATE conflicts SET status=...`` succeeds as ``recon_writer`` and
  ``UPDATE conflicts SET status=..., escalation_reason=...`` is denied.

:func:`recon.reconciler._escalate` now asks ``has_column_privilege`` once per run
and escalates with the columns it holds; the reason always reaches the
``conflict.escalated`` audit row, and ``report.escalation_reason_persisted`` says
whether it also reached the column.

THE GRANT, AND WHAT USED NOT TO BE PROVEN
-----------------------------------------
This section read: *"``conflicts.escalation_reason`` is **not** populated under
``recon_writer`` today, because the grant does not exist ... **The fix is a
one-line migration** adding ``escalation_reason`` to ``CONFLICT_UPDATE_COLUMNS``
(migration 0004); this ticket may not add a migration, and
:func:`test_the_escalation_reason_column_is_ungranted` pins the current state so
the day the grant lands, that test turns red and is updated deliberately rather
than silently."*

The grant landed: ``migrations/versions/0015_escalation_reason_grant.py`` adds
``escalation_reason`` to that column-scoped list. The pinning test turned red,
was updated deliberately, and is now
:func:`test_the_escalation_reason_column_is_granted_to_recon_writer` -- which
asserts the inverse *and* the thing the old docstring asked for, that all 25
escalated rows carry the reason in the column. The reviewer surface therefore
serves ``escalated:oscillation`` from the conflict row itself, not only by
reconstructing it from ``conflicts.oscillating``.

``_escalate``'s degraded branch is still live and still tested through
``report.escalation_reason_persisted``, because it is driven by
``has_column_privilege`` per run rather than by an assumption about which
migrations have been applied.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Connection, text

from recon.invariants.oscillation import scan_field_lineage
from recon.reconciler import (
    ESCALATION_OSCILLATION,
    LINEAGE_GENERATIONS_REQUIRED,
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
    """A stand-in for `LineageScan` with the two attributes the decision reads.

    ``rows`` is deliberately kept separate from the generation count the decision
    now also takes: the whole point of the fix is that the two are different
    questions and only one of them says whether an A -> B -> A could have been
    found.
    """

    def __init__(self, pairs: set[tuple[str, str]], rows: int) -> None:
        self.pairs = pairs
        self.rows = rows

    @property
    def had_input(self) -> bool:
        return self.rows > 0

    def oscillates(self, canonical_id: str, field_path: str) -> bool:
        return (canonical_id, field_path) in self.pairs


#: The shipped configuration before the fix: lineage materialized for generation 3
#: alone. Non-empty, unscannable.
_ONE_GENERATION = 1
_THREE_GENERATIONS = LINEAGE_GENERATIONS_REQUIRED


def test_an_empty_lineage_scan_is_reported_as_no_input_not_as_a_false() -> None:
    """The distinction the whole module exists to preserve.

    A scan over an empty table returning nothing means "there was nothing to
    scan". Reporting that as a confident "does not oscillate" is the failure this
    label prevents, and it is why the packet records which input answered.
    """
    oscillating, source = oscillation_state(_row(), _Scan(set(), rows=0), lineage_generations=0)
    assert oscillating is False
    assert source == OSCILLATION_NO_INPUT


def test_lineage_with_rows_but_ONE_generation_is_also_no_input() -> None:
    """The confident false the row-count guard let through, pinned as no-input.

    CONTRACT CHANGED (deliberate). The old assertion here was
    ``oscillation_state(_row(), _Scan(set(), rows=5000)) == (False,
    OSCILLATION_FROM_SCAN)`` under the name "a scan with rows that finds nothing
    is a real negative". Row count is not the question SS7 asks: the pattern is
    "A, B, A across ascending generations", so lineage covering ONE generation is
    structurally incapable of containing it however many rows it has. The shipped
    configuration was exactly that -- ``materialize(lineage_generations=(3,))``,
    426,175 rows, ``had_input`` True -- and every one of the 3,050 graded
    proposals therefore recorded ``decided_by: lineage_scan`` with
    ``observed: false``, including the 25 that ``golden/conflicts.json`` marks
    oscillating. A wrong answer wearing an authoritative provenance label is worse
    than an admitted unknown.
    """
    oscillating, source = oscillation_state(
        _row(), _Scan(set(), rows=426_175), lineage_generations=_ONE_GENERATION
    )
    assert oscillating is False
    assert source == OSCILLATION_NO_INPUT, (
        "one generation of lineage cannot contain A -> B -> A, so the scan is not "
        "an answer and must not be reported as one"
    )


def test_a_scan_over_three_generations_that_finds_nothing_is_a_real_negative() -> None:
    """With the coverage to find it, "found none" IS a decided negative."""
    oscillating, source = oscillation_state(
        _row(), _Scan(set(), rows=5000), lineage_generations=_THREE_GENERATIONS
    )
    assert oscillating is False
    assert source == OSCILLATION_FROM_SCAN


def test_one_generation_of_lineage_still_defers_to_the_stored_flag() -> None:
    """The fallback the row-count guard made unreachable.

    With ``had_input`` alone, a non-empty single-generation lineage suppressed the
    ``conflicts.oscillating`` column entirely -- ``OSCILLATION_FROM_ROW`` could
    never be reached in the shipped configuration.
    """
    oscillating, source = oscillation_state(
        _row(oscillating=True), _Scan(set(), rows=426_175), lineage_generations=_ONE_GENERATION
    )
    assert oscillating is True
    assert source == OSCILLATION_FROM_ROW


def test_the_live_scan_finds_an_oscillation_the_stored_flag_missed() -> None:
    conflict = _row(oscillating=False)
    key = str(person_key(conflict.entity_refs))
    scan = _Scan({(key, "crm.contact.grade")}, rows=10)
    oscillating, source = oscillation_state(conflict, scan, lineage_generations=_THREE_GENERATIONS)
    assert oscillating is True
    assert source == OSCILLATION_FROM_SCAN


def test_the_stored_flag_is_used_when_there_is_no_lineage_to_scan() -> None:
    oscillating, source = oscillation_state(
        _row(oscillating=True), _Scan(set(), rows=0), lineage_generations=0
    )
    assert oscillating is True
    assert source == OSCILLATION_FROM_ROW


def test_only_the_two_types_that_carry_disagreeing_fields_can_oscillate() -> None:
    """SS7 marks a conflict oscillating "where the conflict's FIELD oscillated"."""
    conflict = _row(type="C12", disagreeing_fields=())
    key = str(person_key(conflict.entity_refs))
    scan = _Scan({(key, "crm.contact.grade")}, rows=10)
    assert oscillation_state(conflict, scan, lineage_generations=_THREE_GENERATIONS) == (
        False,
        OSCILLATION_FROM_SCAN,
    )


# =====================================================================================
# the real path: real lineage, real oscillations, the real principal
# =====================================================================================
@pytest.fixture
def owner(conflict_store: str) -> Iterator[Connection]:
    """A schema-owner connection, rolled back.

    Used ONLY by the tests that must play a reviewer *rejecting* a proposal inside
    the same transaction as the run that created it: migration 0006 lets only the
    review role (or the owner, bound to the same transition graph) move a proposal
    to ``rejected``.

    It is deliberately NOT the fixture for the escalation tests any more. The
    owner bypasses grants, and owner-only escalation coverage is exactly what hid
    a run-aborting ``InsufficientPrivilege`` in the shipped path -- see
    :func:`test_the_real_run_escalates_the_golden_25_as_recon_writer`.
    """
    del conflict_store
    from recon.db import get_engine

    with get_engine().connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


def _golden_oscillating_keys() -> set[tuple[str, tuple[str, ...]]]:
    """``(type, sorted(entity_refs))`` for every golden entry marked oscillating."""
    import json
    from pathlib import Path

    golden = json.loads(
        (Path(__file__).resolve().parents[3] / "golden" / "conflicts.json").read_text()
    )
    return {
        (entry["type"], tuple(sorted(entry["entity_refs"])))
        for entry in golden
        if entry.get("oscillating")
    }


def _an_oscillating_conflict(conn: Connection) -> tuple[int, str, tuple[str, ...], tuple[str, ...]]:
    """A conflict the COMMITTED scan says oscillates, found without planting anything."""
    scan = scan_field_lineage(conn.connection.driver_connection)
    assert scan.pairs, "no oscillating pairs in real lineage; the fixture is not built"
    rows = conn.execute(
        text(
            "SELECT id, fingerprint, entity_refs, disagreeing_fields FROM conflicts "
            " WHERE type IN ('C6', 'C14') AND jsonb_array_length(disagreeing_fields) > 0 "
            " ORDER BY fingerprint"
        )
    ).all()
    for row in rows:
        key = str(person_key(tuple(row.entity_refs)))
        hits = [path for path in row.disagreeing_fields if scan.oscillates(key, path)]
        if hits:
            return row.id, row.fingerprint, tuple(row.entity_refs), tuple(hits)
    raise AssertionError("the scan found pairs but none of them matched a conflict")


def _a_non_oscillating_c6(conn: Connection) -> tuple[int, str]:
    scan = scan_field_lineage(conn.connection.driver_connection)
    rows = conn.execute(
        text(
            "SELECT id, fingerprint, entity_refs, disagreeing_fields FROM conflicts "
            " WHERE type = 'C6' AND jsonb_array_length(disagreeing_fields) > 0 "
            " ORDER BY fingerprint"
        )
    ).all()
    for row in rows:
        key = str(person_key(tuple(row.entity_refs)))
        if not any(scan.oscillates(key, path) for path in row.disagreeing_fields):
            return row.id, row.fingerprint
    raise AssertionError("every C6 oscillates; the control test would prove nothing")


def _clear_lineage(conn: Connection, entity_refs: tuple[str, ...], paths: tuple[str, ...]) -> None:
    """Remove one pair's history so it stops oscillating. The sabotage lever."""
    canonical = str(person_key(entity_refs))
    removed = 0
    for path in paths:
        result = conn.execute(
            text(
                "DELETE FROM field_lineage WHERE canonical_id = CAST(:cid AS uuid) "
                "  AND field = :field"
            ),
            {"cid": canonical, "field": path},
        )
        removed += result.rowcount
    assert removed > 0, (
        f"no field_lineage rows removed for {canonical} {paths}; the sabotage lever "
        "did nothing, so the comparison below would be between two identical runs"
    )


def test_the_committed_scan_finds_the_golden_25_without_a_test_planting_them(
    owner: Connection,
) -> None:
    """The detection half, unaided.

    No row in this test writes lineage. The fixture ingests generations 1-3 and
    materializes them; the committed scan is handed the result and must find
    exactly the pairs ``golden/conflicts.json`` marks oscillating.
    """
    scan = scan_field_lineage(owner.connection.driver_connection)
    assert scan.had_input is True
    assert scan.rows > 1_000_000, f"only {scan.rows} lineage rows -- did materialize run?"
    assert len(scan.pairs) == 25, (
        f"the scan found {len(scan.pairs)} A -> B -> A pairs over real generation-1-3 "
        "lineage; golden marks 25 conflicts oscillating"
    )

    generations = owner.execute(
        text("SELECT count(DISTINCT generation) FROM field_lineage")
    ).scalar_one()
    assert generations == 3, "SS7's A -> B -> A window needs three ascending generations"


def test_the_real_run_escalates_the_golden_25_as_recon_writer(writer: Connection) -> None:
    """THE test the suite did not have: real oscillations, real principal.

    Every escalation test previously ran as the schema owner, which bypasses
    grants. Under ``recon_writer`` -- the principal ``run_once()`` authenticates
    as -- the single-statement escalation
    ``UPDATE conflicts SET status=..., escalation_reason=...`` was refused with
    SQLSTATE 42501, and because the exception propagated out of ``reconcile``'s
    transaction the ENTIRE run rolled back: zero proposals, the first time any
    conflict oscillated.

    This asserts the run completes, writes one proposal per conflict, and
    escalates exactly the golden 25 -- as ``recon_writer``.
    """
    report = reconcile(conn=writer, run_id="t7-osc-writer")

    assert writer.execute(text("SELECT current_user")).scalar_one() == "recon_writer"
    assert report.lineage_generations == 3
    assert report.conflicts_seen == report.proposed, (
        "R13: one proposal per conflict. A crash mid-run would show up here as a "
        "short count rather than as an exception, so it is asserted as a number."
    )
    assert report.escalated_oscillation == 25

    escalated = writer.execute(
        text("SELECT count(*) FROM conflicts WHERE status = 'escalated'")
    ).scalar_one()
    assert escalated == 25

    # The reason reaches a durable row whether or not the column grant exists.
    audited = writer.execute(
        text("SELECT count(*) FROM audit_log WHERE action = 'conflict.escalated'")
    ).scalar_one()
    assert audited == 25


def test_the_escalated_set_is_exactly_goldens_oscillating_set(writer: Connection) -> None:
    """Not just the right COUNT -- the right conflicts."""
    reconcile(conn=writer, run_id="t7-osc-identity")
    rows = writer.execute(
        text("SELECT type, entity_refs FROM conflicts WHERE status = 'escalated'")
    ).all()
    escalated = {(row.type, tuple(sorted(row.entity_refs))) for row in rows}
    assert escalated == _golden_oscillating_keys()


def test_the_escalation_reason_column_is_granted_to_recon_writer(writer: Connection) -> None:
    """The remediation this test asked for, applied. Migration 0015.

    **This test used to pin the degraded state**, under the name
    ``test_the_escalation_reason_column_is_ungranted_to_recon_writer``, and its
    docstring named the conditions of its own replacement: *"Migration 0004
    narrowed ``recon_writer``'s UPDATE on ``conflicts`` to ``(status,
    last_seen_run)``. ``escalation_reason`` is not in that list, so the
    reconciler writes the reason to the audit row only and reports
    ``escalation_reason_persisted=False``. The proper fix is a migration adding
    the column to ``CONFLICT_UPDATE_COLUMNS``; this ticket may not add one. **When
    that migration lands this test turns red**, which is the point: the
    remediation is then applied deliberately (flip both assertions and assert the
    column is populated) rather than leaving a stale caveat in the docs."*

    ``migrations/versions/0015_escalation_reason_grant.py`` is that migration, so
    this is that deliberate update: the grant assertion is inverted, and the
    assertion the old docstring asked for -- **the column is populated on all 25
    escalations** -- is added rather than the old one merely being deleted. The
    contract is strictly stronger than it was: the degraded path proved a NULL,
    this proves a value.

    Nothing is loosened. ``report.escalation_reason_persisted is granted`` is
    unchanged and still binds the report to the catalogue rather than to a
    constant, so a future migration that narrowed the grant again would fail here
    on ``granted is True`` and not silently pass.
    """
    granted = writer.execute(
        text(
            "SELECT has_column_privilege(current_user, 'conflicts', 'escalation_reason', 'UPDATE')"
        )
    ).scalar_one()
    report = reconcile(conn=writer, run_id="t7-osc-grant")
    assert report.escalation_reason_persisted is granted
    assert granted is True, (
        "recon_writer holds no UPDATE on conflicts.escalation_reason. Migration "
        "0015 grants it (column-scoped, alongside status and last_seen_run); if "
        "that migration has been reverted, the reconciler degrades to writing the "
        "reason into the conflict.escalated audit row only and this contract is "
        "the weaker one again"
    )
    # The escalation happened, the reason is on the row, and it is still in the
    # audit row too -- the audit trail did not stop carrying it when the column
    # started to.
    assert report.escalated_oscillation == 25
    on_row = writer.execute(
        text(
            "SELECT count(*) FROM conflicts "
            " WHERE status = 'escalated' AND escalation_reason = :reason"
        ),
        {"reason": ESCALATION_OSCILLATION},
    ).scalar_one()
    assert on_row == 25, (
        f"{on_row} of 25 escalated conflicts carry escalation_reason="
        f"{ESCALATION_OSCILLATION!r} on the row. With the 0015 grant in place the "
        "reviewer surface serves 'escalated:<reason>' from the conflict row itself "
        "instead of reconstructing it from the oscillating flag"
    )
    body_has_reason = writer.execute(
        text(
            "SELECT count(*) FROM audit_log WHERE action = 'conflict.escalated' "
            "  AND detail::text LIKE '%reason_on_conflict_row%'"
        )
    ).scalar_one()
    assert body_has_reason == 25


def test_the_penalty_is_recorded_on_a_real_oscillating_proposal(writer: Connection) -> None:
    """As ``recon_writer``: the packet says the scan decided, and why it could.

    ``lineage_generations == 3`` is the assertion that makes ``decided_by:
    lineage_scan`` mean something. Under the shipped gen-3-only fixture it was 1,
    the scan could not have found an A -> B -> A, and every packet claimed
    ``lineage_scan`` anyway.
    """
    _, fingerprint, _, _ = _an_oscillating_conflict(writer)

    reconcile(conn=writer, run_id="t7-osc-scored")
    confidence, evidence = writer.execute(
        text("SELECT confidence, evidence FROM proposals WHERE fingerprint = :fp"),
        {"fp": fingerprint},
    ).one()
    assert evidence["oscillation"]["observed"] is True
    assert evidence["oscillation"]["decided_by"] == OSCILLATION_FROM_SCAN
    assert evidence["oscillation"]["lineage_generations"] == 3
    assert evidence["confidence"]["signals"]["oscillation_observed"] is True

    # The persisted packet must re-derive the stored number, penalty included.
    term = next(t for t in evidence["confidence"]["terms"] if t["signal"] == "oscillation_observed")
    assert Decimal(term["contribution"]) == Decimal("-0.25")
    assert Decimal(evidence["confidence"]["confidence"]) == Decimal(confidence)


def test_oscillation_lowers_the_confidence_by_exactly_the_committed_weight(
    owner: Connection,
) -> None:
    """-0.25, the heaviest penalty in the model, on a REAL oscillating conflict.

    The sabotage lever is deleting that pair's lineage: with it the conflict
    oscillates and is penalised, without it the same conflict is not. If the
    penalty came from anywhere else, both runs would score the same.

    Runs as the OWNER only because it has to clear ``proposals``/``audit_log``
    between the two runs, and ``recon_writer`` is append-only on both by design
    (migration 0004) -- which is itself a property worth having. The recon_writer
    half of this path is asserted directly above.
    """
    _, fingerprint, entity_refs, paths = _an_oscillating_conflict(owner)

    reconcile(conn=owner, run_id="t7-osc-scored")
    penalised = owner.execute(
        text("SELECT confidence FROM proposals WHERE fingerprint = :fp"),
        {"fp": fingerprint},
    ).scalar_one()

    owner.execute(text("DELETE FROM audit_log"))
    owner.execute(text("DELETE FROM proposals"))
    owner.execute(text("UPDATE conflicts SET status = 'open' WHERE status = 'escalated'"))
    _clear_lineage(owner, entity_refs, paths)

    reconcile(conn=owner, run_id="t7-osc-baseline")
    plain, plain_evidence = owner.execute(
        text("SELECT confidence, evidence FROM proposals WHERE fingerprint = :fp"),
        {"fp": fingerprint},
    ).one()
    assert plain_evidence["oscillation"]["observed"] is False
    assert Decimal(plain) - Decimal(penalised) == Decimal("0.2500")


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

    Runs as the owner because only the review role may set ``rejected``; the
    escalation this depends on is proven under ``recon_writer`` above.
    """
    _, fingerprint, _, _ = _an_oscillating_conflict(owner)

    first = reconcile(conn=owner, run_id="t7-osc-r1")
    assert any(o.fingerprint == fingerprint and o.proposed for o in first.outcomes)
    assert first.escalated_oscillation == 25

    _reject(owner, fingerprint)

    second = reconcile(conn=owner, run_id="t7-osc-r2")
    outcome = next(o for o in second.outcomes if o.fingerprint == fingerprint)
    assert outcome.proposed is False
    assert outcome.skip_reason == SKIP_OSCILLATION
    assert second.skipped_oscillation == 1
    assert second.skipped_fingerprint == 3049

    live = owner.execute(
        text("SELECT count(*) FROM proposals WHERE fingerprint = :fp AND status <> 'rejected'"),
        {"fp": fingerprint},
    ).scalar_one()
    assert live == 0


def test_the_control_a_rejected_fix_that_does_not_oscillate_IS_re_proposed(
    owner: Connection,
) -> None:
    """The sabotage check: prove the suppression came from OSCILLATION.

    Same setup, same rejection, on a conflict the scan does NOT flag. If this one
    were also suppressed, the test above would be evidence of nothing but
    fingerprint bookkeeping.
    """
    _, fingerprint = _a_non_oscillating_c6(owner)

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


def test_an_oscillating_conflict_still_gets_its_FIRST_proposal(writer: Connection) -> None:
    """R16 forbids re-proposing the identical fix; the first one is not a re-propose.

    The conflict is escalated and the score carries the penalty, but it does reach
    a human -- a silently dropped conflict would be the worse failure.
    """
    _, fingerprint, _, _ = _an_oscillating_conflict(writer)

    report = reconcile(conn=writer, run_id="t7-osc-first")
    outcome = next(o for o in report.outcomes if o.fingerprint == fingerprint)
    assert outcome.proposed is True
    assert outcome.escalated is True
    assert report.escalated_oscillation == 25
