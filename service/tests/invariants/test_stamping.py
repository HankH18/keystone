"""SS5.8 -- every `stg_*` row is stamped, in the pinned vocabulary, with its reason.

    "Every `stg_*` row is stamped in `invariant_results` for every rule whose scope
     includes it. A row in scope of **zero** rules gets one synthetic row
     (`rule_id='R-000'`, `verdict='unchecked'`, `detail.reason='no_rule_in_scope'`).
     Pinned `verdict` vocabulary, closed: `ok`, `conflict`, `unchecked`.
     `detail.reason` is **required** on `unchecked` and **forbidden** otherwise."

The per-record verdicts *are* the grading contract, so this file checks the count,
the vocabulary, the reason discipline and the round trip through the database --
including the one place the contract and the committed Postgres enum disagree.
"""

from __future__ import annotations

import psycopg
import pytest

from recon.invariants.rules import DB_VERDICT, load_rules
from recon.invariants.runner import InvariantRun, persist_run, run_invariants
from recon.reference import UNCHECKED_REASONS, VERDICTS

SCOPE_COUNTS = {
    "stg_student": 25_000,
    "stg_payment": 18_000,
    "stg_crm_contact": 40_000,
    "stg_enrollment": 22_000,
    "stg_crm_deal": 15_000,
}


def test_every_rule_stamps_every_row_in_its_scope(invariant_run) -> None:
    for outcome in invariant_run.outcomes:
        assert outcome.rows == SCOPE_COUNTS[outcome.scope_table], outcome.rule_id


def test_the_total_stamp_count_is_the_scope_table_summed(invariant_run) -> None:
    expected = sum(SCOPE_COUNTS[spec.scope_table] for spec in load_rules())
    assert len(invariant_run.results) == expected


def test_the_verdict_vocabulary_is_closed(invariant_run) -> None:
    seen = {verdict for *_head, verdict, _reason in invariant_run.results}
    assert seen <= set(VERDICTS)
    assert seen == {"ok", "conflict", "unchecked"}


def test_reason_is_required_on_unchecked_and_forbidden_otherwise(invariant_run) -> None:
    for rule_id, _version, ref, _entity, verdict, reason in invariant_run.results:
        if verdict == "unchecked":
            assert reason in UNCHECKED_REASONS, (rule_id, ref, reason)
        else:
            assert reason is None, (rule_id, ref, verdict, reason)


def test_every_deal_row_carries_the_synthetic_r000_stamp(invariant_run) -> None:
    """SS5.8: `stg_crm_deal` is in the scope of no rule, so every deal row is
    `R-000` / `unchecked` / `no_rule_in_scope` -- the explicit statement that nothing
    checked it, which is not the same claim as "checked and clean"."""
    rows = [row for row in invariant_run.results if row[0] == "R-000"]
    assert len(rows) == SCOPE_COUNTS["stg_crm_deal"]
    assert {(row[3], row[4], row[5]) for row in rows} == {("deal", "unchecked", "no_rule_in_scope")}


def test_each_record_is_stamped_once_per_in_scope_rule(invariant_run) -> None:
    pairs = [(rule_id, ref) for rule_id, _v, ref, _e, _verdict, _r in invariant_run.results]
    assert len(set(pairs)) == len(pairs)


def test_the_run_persists_and_reads_back(ingested_dsn) -> None:
    """The round trip through `invariant_results` and `conflicts`.

    SS5.8 pins the vocabulary as `ok`/`conflict`/`unchecked`; the committed
    `invariant_verdict` Postgres enum spells the first two `pass`/`fail`. The mapping
    lives at this one boundary and is total and injective, which is what this asserts
    -- the divergence is reported in the ticket's `contract_gaps`, not papered over.
    """
    assert set(DB_VERDICT) == set(VERDICTS)
    assert len(set(DB_VERDICT.values())) == len(DB_VERDICT)

    with psycopg.connect(ingested_dsn) as conn:
        run = run_invariants(conn, run_id="t6-persist")
        persist_run(conn, run)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT verdict::text, count(*) FROM invariant_results "
                "WHERE run_id = 't6-persist' GROUP BY 1 ORDER BY 1"
            )
            counts = dict(cur.fetchall())
            cur.execute(
                "SELECT count(*), count(DISTINCT fingerprint) FROM conflicts "
                "WHERE first_seen_run = 't6-persist'"
            )
            total, distinct = cur.fetchone()
            cur.execute(
                "SELECT count(*) FROM invariant_results "
                "WHERE run_id = 't6-persist' AND verdict = 'unchecked' "
                "AND (detail ->> 'reason') IS NULL"
            )
            reasonless = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM invariant_results "
                "WHERE run_id = 't6-persist' AND verdict <> 'unchecked' "
                "AND detail IS NOT NULL"
            )
            spurious = cur.fetchone()[0]
        conn.rollback()

    assert sum(counts.values()) == len(run.results)
    assert counts["fail"] == sum(1 for row in run.results if row[4] == "conflict")
    assert total == len(run.conflicts)
    assert distinct == total
    assert reasonless == 0
    assert spurious == 0


def test_conflicts_carry_the_contract_shape(invariant_run) -> None:
    """SS8: `sources_involved` is derived mechanically from the ref prefixes, and
    `disagreeing_fields` is populated only by `R-006`/`R-014`."""
    for conflict in invariant_run.conflicts:
        assert conflict.expected_verdict == "conflict"
        assert set(conflict.sources_involved) <= {"crm", "appdb", "payments"}
        assert conflict.sources_involved == tuple(sorted(set(conflict.sources_involved)))
        if conflict.disagreeing_fields:
            assert conflict.type in {"C6", "C14"}
        assert conflict.entity_refs


def test_the_detection_path_can_persist_as_recon_writer(ingested_dsn) -> None:
    """The write boundary: `recon_writer` PROPOSES, and that is the role the detection
    path writes `invariant_results` and `conflicts` with.

    It is a live check, not a comment: it connects **as** the restricted role, because
    a table owner bypasses its own grants and using the owner engine here would
    silently disable the boundary.

    It also documents where the boundary and this engine meet. Migration 0006 revoked
    `TEMPORARY` on the database from `recon_writer`, and the runner materializes the
    SS4 cascade into `TEMP` tables -- so evaluation runs on the owner connection and
    only the *writes* go through this role. Reported in the ticket's `contract_gaps`.
    """
    from sqlalchemy.engine import make_url

    from recon.db import ROLE_RECON_WRITER, role_password

    url = make_url(ingested_dsn).set(
        drivername="postgresql",
        username=ROLE_RECON_WRITER,
        password=role_password(ROLE_RECON_WRITER),
    )

    with psycopg.connect(ingested_dsn) as owner:
        run = run_invariants(owner, run_id="t6-role")
        sample = InvariantRun(
            run_id=run.run_id,
            generation=run.generation,
            status=run.status,
            incomplete=run.incomplete,
            outcomes=run.outcomes,
            results=run.results[:500],
            raw_conflicts=run.raw_conflicts,
            conflicts=run.conflicts[:50],
        )
        owner.rollback()

    with psycopg.connect(url.render_as_string(hide_password=False)) as writer:
        persist_run(writer, sample)
        with writer.cursor() as cur:
            cur.execute("SELECT count(*) FROM invariant_results WHERE run_id = 't6-role'")
            stamped = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM conflicts WHERE first_seen_run = 't6-role'")
            conflicts = cur.fetchone()[0]
        writer.rollback()

    assert stamped == 500
    assert conflicts == 50


def test_recon_writer_cannot_create_the_runner_temp_tables(ingested_dsn) -> None:
    """The reason evaluation runs on the owner connection, asserted rather than assumed.

    Migration 0006 revoked `TEMPORARY` from `recon_writer` ("`pg_temp` is no longer a
    place to define code"). If a later migration grants it back, this test fails and
    the runner can be moved wholly onto the restricted role -- which is the state the
    contract's pipeline description implies.
    """
    from sqlalchemy.engine import make_url

    from recon.db import ROLE_RECON_WRITER, role_password

    url = make_url(ingested_dsn).set(
        drivername="postgresql",
        username=ROLE_RECON_WRITER,
        password=role_password(ROLE_RECON_WRITER),
    )
    with psycopg.connect(url.render_as_string(hide_password=False)) as writer:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            writer.execute("CREATE TEMP TABLE t6_probe (x integer)")
        writer.rollback()
