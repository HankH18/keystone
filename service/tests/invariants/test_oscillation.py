"""SS7's `A -> B -> A` scan, and the `oscillating` column it decides (SS8).

Two halves, and the second is the one that used to be missing entirely:

1. **The scan works.** Constructed `field_lineage` rows produce the pinned answer --
   `A, B, A` oscillates, `A, B, C` and `A, A, A` do not, the key is
   `(person_key, field_path)`, and a real detected C6 flips to `oscillating=True`
   when its person's field is given an oscillating lineage.
2. **The gap is red-labelled, not invisible.** The committed suite ingests generation
   3 only (SS7: "Invariants read generation 3 only"), so `field_lineage` is EMPTY,
   the scan has no input, and every detected conflict comes back `oscillating=False`
   while `golden/conflicts.json` carries `oscillating: true` on 25 C6 entries.
   SS5.4's field-exactness list -- `disagreeing_fields`, `sources_involved`,
   `observed_values.keys`, `observed_values`, `expected_verdict` -- deliberately
   excludes `oscillating`, so `grade_run` is *structurally incapable* of reporting
   that divergence and the golden diff stays green either way.

   :func:`test_the_gen3_only_pipeline_cannot_answer_oscillating` asserts that state
   explicitly and names what would close it: nothing in the repository writes
   `field_lineage`, and populating it from generations 1-2 is the ingest/ER ticket's
   work (`tests/invariants/conftest.py::ingested_dsn` says the same thing from the
   fixture side). When that lands, the last assertion here is the one that fails
   first, and :func:`test_detected_oscillation_matches_golden_once_lineage_exists`
   is the assertion to turn on.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from recon.invariants.context import build_context
from recon.invariants.grading import load_golden
from recon.invariants.oscillation import (
    OSCILLATION_TYPES,
    LineageScan,
    mark_oscillating,
    scan_field_lineage,
)
from recon.invariants.runner import DetectedConflict, run_invariants
from recon.reference import person_key

GOLDEN_OSCILLATING = 25

_INSERT = (
    "INSERT INTO field_lineage (canonical_id, field, value_text, source_id, "
    "source_ref, generation) VALUES (%s, %s, %s, %s, %s, %s)"
)


@pytest.fixture(scope="module")
def lineage_conn(ingested_dsn: str):
    """A connection with the ER context built and committed, for rolled-back edits.

    Same shape as `test_unchecked_paths.mutable`: the TEMP tables are committed so a
    test's `rollback()` undoes only that test's `field_lineage` inserts, not the
    `er_*` relations every following test needs.
    """
    with psycopg.connect(ingested_dsn) as conn:
        context = build_context(conn)
        conn.commit()
        yield conn, context
        conn.rollback()


# ======================================================================================
# the scan itself
# ======================================================================================


def _write(conn, rows) -> None:
    with conn.cursor() as cur:
        for canonical_id, field, value, generation in rows:
            cur.execute(_INSERT, (canonical_id, field, value, "crm", "crm:contact:X", generation))


def test_the_aba_pattern_is_the_only_one_that_oscillates(lineage_conn) -> None:
    """SS7: "the pattern `A, B, A` across ascending generations", compared for STRING
    equality on `value_canon`."""
    conn, _context = lineage_conn
    aba, abc, aaa, ab = (str(uuid.uuid4()) for _ in range(4))
    _write(
        conn,
        [
            (aba, "crm.contact.grade", "4", 1),
            (aba, "crm.contact.grade", "5", 2),
            (aba, "crm.contact.grade", "4", 3),
            (abc, "crm.contact.grade", "4", 1),
            (abc, "crm.contact.grade", "5", 2),
            (abc, "crm.contact.grade", "6", 3),
            (aaa, "crm.contact.grade", "4", 1),
            (aaa, "crm.contact.grade", "4", 2),
            (aaa, "crm.contact.grade", "4", 3),
            # Two generations cannot express A -> B -> A at all.
            (ab, "crm.contact.grade", "4", 1),
            (ab, "crm.contact.grade", "5", 2),
        ],
    )
    scan = scan_field_lineage(conn)
    assert scan.had_input
    assert scan.oscillates(aba, "crm.contact.grade")
    assert not scan.oscillates(abc, "crm.contact.grade")
    assert not scan.oscillates(aaa, "crm.contact.grade")
    assert not scan.oscillates(ab, "crm.contact.grade")
    conn.rollback()


def test_the_scan_is_keyed_on_person_key_and_field_path_together(lineage_conn) -> None:
    """SS7: `field_lineage`, the A,B,A scan and R16's dedup are ALL keyed on
    `person_key` -- so one person's oscillating `grade` must not mark that person's
    `lifecycle_stage`, nor another person's `grade`."""
    conn, _context = lineage_conn
    person, other = str(uuid.uuid4()), str(uuid.uuid4())
    _write(
        conn,
        [
            (person, "crm.contact.grade", "4", 1),
            (person, "crm.contact.grade", "5", 2),
            (person, "crm.contact.grade", "4", 3),
            (person, "crm.contact.lifecycle_stage", "lead", 1),
            (person, "crm.contact.lifecycle_stage", "customer", 2),
            (person, "crm.contact.lifecycle_stage", "customer", 3),
            (other, "crm.contact.grade", "7", 1),
            (other, "crm.contact.grade", "7", 2),
            (other, "crm.contact.grade", "7", 3),
        ],
    )
    scan = scan_field_lineage(conn)
    assert scan.pairs == {(person, "crm.contact.grade")}
    conn.rollback()


def test_an_empty_lineage_is_reported_as_no_input_not_as_no_oscillation() -> None:
    """The distinction a bare `False` destroys, and the reason `LineageScan` carries
    `rows` at all."""
    empty = LineageScan(pairs=frozenset(), rows=0)
    scanned = LineageScan(pairs=frozenset(), rows=1_000)
    assert not empty.had_input
    assert scanned.had_input
    assert empty.pairs == scanned.pairs  # identical answer, different evidence


# ======================================================================================
# the wiring: a real detected conflict flips
# ======================================================================================


def test_a_real_c6_flips_to_oscillating_when_its_field_oscillates(lineage_conn) -> None:
    """End to end: the flag is DERIVED, not defaulted.

    This is the assertion that would have caught the constant `False`: it takes a
    conflict the engine really detected, gives its person's really-disagreeing field
    an `A, B, A` lineage, and requires the flag to move.
    """
    conn, context = lineage_conn
    baseline = run_invariants(conn, run_id="osc-baseline", context=context)
    assert baseline.lineage is not None and not baseline.lineage.had_input
    assert baseline.oscillating_count == 0

    subject = next(
        conflict
        for conflict in baseline.conflicts
        if conflict.type == "C6" and conflict.disagreeing_fields
    )
    canonical_id = str(person_key(subject.entity_refs))
    path = subject.disagreeing_fields[0]
    _write(
        conn,
        [
            (canonical_id, path, "A", 1),
            (canonical_id, path, "B", 2),
            (canonical_id, path, "A", 3),
        ],
    )

    run = run_invariants(conn, run_id="osc-flipped", context=context)
    assert run.lineage is not None and run.lineage.had_input
    flagged = {conflict.key for conflict in run.conflicts if conflict.oscillating}
    assert flagged == {subject.key}, "exactly the one person whose field oscillates"

    # SS5.4: `oscillating` is NOT a fingerprint input and NOT a field-exactness
    # assertion, so flipping it must move neither the digest nor the golden diff.
    before = {conflict.key: conflict.fingerprint for conflict in baseline.conflicts}
    after = {conflict.key: conflict.fingerprint for conflict in run.conflicts}
    assert before == after
    conn.rollback()


def test_only_the_types_carrying_disagreeing_fields_can_oscillate() -> None:
    """SS7 marks a conflict oscillating "where the conflict's **field** oscillated",
    and SS2.4 says only `R-006`/`R-014` populate `disagreeing_fields`. A type with no
    field has nothing the scan could have matched."""
    assert set(OSCILLATION_TYPES) == {"C6", "C14"}
    refs = ("appdb:student:s1", "crm:contact:CRM-0000001")
    key = str(person_key(refs))
    scan = LineageScan(pairs=frozenset({(key, "crm.contact.grade")}), rows=3)
    c5 = DetectedConflict(
        type="C5",
        rule_id="R-005",
        entity_refs=refs,
        sources_involved=("appdb", "crm"),
        disagreeing_fields=(),
        observed_values={},
        fingerprint="deadbeef",
    )
    c6 = DetectedConflict(
        type="C6",
        rule_id="R-006",
        entity_refs=refs,
        sources_involved=("appdb", "crm"),
        disagreeing_fields=("crm.contact.grade",),
        observed_values={},
        fingerprint="cafebabe",
    )
    marked = {conflict.type: conflict.oscillating for conflict in mark_oscillating([c5, c6], scan)}
    assert marked == {"C5": False, "C6": True}


# ======================================================================================
# the gap, asserted rather than disclosed
# ======================================================================================


def test_golden_carries_twenty_five_oscillating_c6_entries() -> None:
    """SS7/SS8: "Entries carry `"oscillating": true` where the conflict's field
    oscillated". This is the number the engine has to reach eventually."""
    entries = [entry for entry in load_golden() if entry.get("oscillating")]
    assert len(entries) == GOLDEN_OSCILLATING
    assert {entry["type"] for entry in entries} == {"C6"}


def test_the_gen3_only_pipeline_cannot_answer_oscillating(invariant_run) -> None:
    """**KNOWN GAP, deliberately asserted red-labelled rather than left silent.**

    The suite ingests generation 3 only, nothing in the repository writes
    `field_lineage`, and SS7's scan therefore has no input. The engine reports that
    honestly -- `lineage.rows == 0`, so "no lineage to scan", not "scanned and found
    none" -- but the answer it persists into the NOT NULL `conflicts.oscillating`
    column is still 0 against golden's 25.

    Owning ticket: whoever lands generations 1-2 ingest + `field_lineage` population
    (SS3's `field_lineage(person_key, field_path, generation, value_canon,
    source_ref)`). `tests/invariants/conftest.py::ingested_dsn` records the same
    boundary. Nothing in the invariant engine can close it: the scan, its wiring and
    its tests are all here and green -- the input is not.

    When that ticket lands this assertion is expected to FAIL, and
    :func:`test_detected_oscillation_matches_golden_once_lineage_exists` below is the
    one to un-skip.
    """
    assert invariant_run.lineage is not None
    assert invariant_run.lineage.rows == 0, (
        "field_lineage now has rows -- the known gap is closing; enable "
        "test_detected_oscillation_matches_golden_once_lineage_exists"
    )
    assert invariant_run.oscillating_count == 0
    assert invariant_run.oscillating_count != GOLDEN_OSCILLATING, (
        "the gap closed: update this test and the one below"
    )


def test_detected_oscillation_matches_golden_once_lineage_exists(invariant_run) -> None:
    """The assertion the gap above is measured against. Skipped -- with a reason --
    only while `field_lineage` is empty; it is a real assertion the moment it is not.
    """
    assert invariant_run.lineage is not None
    if not invariant_run.lineage.had_input:
        pytest.skip(
            "KNOWN GAP: `field_lineage` is empty because the suite ingests generation "
            "3 only and nothing in the repository writes it yet (owning ticket: "
            "generations 1-2 ingest + field_lineage population). SS7's A -> B -> A "
            "scan is implemented and unit-tested in this file; it has no input."
        )
    golden_keys = {
        (entry["type"], tuple(sorted(entry["entity_refs"])))
        for entry in load_golden()
        if entry.get("oscillating")
    }
    detected_keys = {conflict.key for conflict in invariant_run.conflicts if conflict.oscillating}
    assert detected_keys == golden_keys
