"""The retention sweep, against the live schema with real backdated rows (R26).

Nothing here is simulated. The rows are inserted into the migrated database, the
sweep issues real DELETE and UPDATE statements, and the assertions name each
seeded row individually: *exactly* the rows outside their window go, and every
row inside its window is still there afterwards. The whole thing runs inside a
transaction that is always rolled back, so the development database is unchanged.

Three claims in ``recon.privacy``'s docstring are asserted here against the
database rather than asserted in prose:

* no application writer role holds DELETE on any retention-bearing table, so the
  sweep is the ops principal's job (``test_no_writer_role_can_purge*``);
* ``proposals.evidence`` cannot be anonymised in place even by the schema owner
  (SQLSTATE ``KS005``), which is *why* a proposal is purged whole
  (``test_proposal_evidence_cannot_be_anonymised``);
* a parent with a surviving child is retained rather than raising ``23503``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from recon.db import WRITER_ROLES, role_connection
from recon.privacy import (
    PURGE_ACTOR,
    RETENTION,
    PurgeNotPermitted,
    canonical_json,
    is_token,
    retention_rule,
    run_purge,
)

#: Fixed sweep moment, so "older than N days" is arithmetic and not a race.
NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

TAG = "privacy-purge-test"

EMAIL = "brenmar-fairbank-mead@gmail.com"
NAME = "Fairbank-Mead"


def ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


# ---------------------------------------------------------------------------
# structural checks -- no database rows needed
# ---------------------------------------------------------------------------


def test_every_table_has_a_retention_decision(owner_conn: Connection) -> None:
    """No table may be left without a documented disposition."""
    tables = {
        row[0]
        for row in owner_conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
    }
    tables.discard("alembic_version")
    covered = {rule.table for rule in RETENTION}
    assert not tables - covered, (
        f"no retention rule for {sorted(tables - covered)}: a new table needs an entry "
        f"in recon.privacy.RETENTION and a row in docs/retention-policy.md before it "
        f"can hold data"
    )
    assert not covered - tables, (
        f"retention rule for a table that does not exist: {sorted(covered - tables)}"
    )


def test_dependents_are_swept_before_their_parents() -> None:
    """Execution order must put every guard's child ahead of the parent.

    Otherwise the parent's ``NOT EXISTS`` guard would still see the child and the
    parent would never age out at all.
    """
    order = [(rule.table, rule.disposition) for rule in RETENTION]
    for index, rule in enumerate(RETENTION):
        for child, _, _ in rule.dependents:
            child_positions = [i for i, (table, _) in enumerate(order) if table == child]
            assert child_positions, f"{rule.table} names dependent {child} with no rule of its own"
            assert min(child_positions) < index, (
                f"{child} must be swept before {rule.table}, but its rule comes later"
            )


def test_every_windowed_rule_names_a_real_timestamp_column(owner_conn: Connection) -> None:
    """A window over a column that does not exist would silently sweep nothing."""
    for rule in RETENTION:
        if rule.window_days is None:
            continue
        found = owner_conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                "AND table_name=:t AND column_name=:c"
            ),
            {"t": rule.table, "c": rule.ts_column},
        ).scalar()
        assert found, f"{rule.table}.{rule.ts_column} does not exist"
        checked = (*rule.columns, *rule.pk_columns) if rule.disposition == "anonymize" else ()
        for column in checked:
            exists = owner_conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns WHERE table_schema='public' "
                    "AND table_name=:t AND column_name=:c"
                ),
                {"t": rule.table, "c": column},
            ).scalar()
            assert exists, f"{rule.table}.{column} does not exist"


# ---------------------------------------------------------------------------
# which principal purges
# ---------------------------------------------------------------------------


def test_no_writer_role_holds_delete_on_a_retention_table(owner_conn: Connection) -> None:
    """The catalog, not a comment, is the evidence for "the owner purges".

    Migrations 0001-0008 give the application roles DELETE on the ``stg_*``
    re-materialisable cache and nowhere else. This asserts that is still true, so
    a future migration that widened a grant to make purging convenient would turn
    this red.
    """
    granted = {
        (row[0], row[1])
        for row in owner_conn.execute(
            text(
                "SELECT grantee, table_name FROM information_schema.role_table_grants "
                "WHERE table_schema='public' AND privilege_type='DELETE' "
                "AND grantee = ANY(:roles)"
            ),
            {"roles": list(WRITER_ROLES)},
        )
    }
    expected = {
        ("recon_writer", table)
        for table in (
            "stg_crm_contact",
            "stg_crm_deal",
            "stg_student",
            "stg_enrollment",
            "stg_payment",
        )
    }
    assert granted == expected


@pytest.mark.parametrize("role", WRITER_ROLES)
def test_the_sweep_refuses_to_run_as_a_writer_role(configured_url: str, role: str) -> None:
    """Fail loudly, not by deleting nothing. Connects AS the role, not SET ROLE."""
    with role_connection(role, commit=False) as conn, pytest.raises(PurgeNotPermitted, match=role):
        run_purge(conn, now=NOW, dry_run=True)


def test_the_owner_principal_is_accepted(owner_conn: Connection) -> None:
    results = run_purge(owner_conn, now=NOW, dry_run=True)
    assert results, "the sweep produced no results at all"


# ---------------------------------------------------------------------------
# the write boundary forced purge-over-anonymize on proposals
# ---------------------------------------------------------------------------


def test_proposal_evidence_cannot_be_anonymised(
    owner_conn: Connection, seeded: dict[str, Any]
) -> None:
    """KS005 for the schema owner too -- so the only disposition left is DELETE."""
    savepoint = owner_conn.begin_nested()
    with pytest.raises(DBAPIError) as excinfo:
        owner_conn.execute(
            text("UPDATE proposals SET evidence = '{}'::jsonb WHERE id = :i"),
            {"i": seeded["p_new"]},
        )
    assert "KS005" in str(excinfo.value.orig.sqlstate)
    savepoint.rollback()
    # ...which is exactly why the rule for proposals is purge and not anonymize
    assert retention_rule("proposals", "purge").disposition == "purge"
    assert not retention_rule("proposals", "purge").columns


# ---------------------------------------------------------------------------
# the sweep itself
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded(owner_conn: Connection) -> dict[str, Any]:
    """Backdated rows on both sides of every window. Rolled back by `owner_conn`."""
    ids: dict[str, Any] = {}
    ex = owner_conn.execute

    def scalar(sql: str, **params: Any) -> Any:
        return ex(text(sql), params).scalar_one()

    pii = {"guardian_email": EMAIL, "last_name": NAME, "grade": "6"}

    # --- landing + staging (30d / 90d) -------------------------------------
    for key, days in (("rr_old", 200), ("rr_new", 10), ("rr_blocked", 200)):
        ids[key] = scalar(
            "INSERT INTO raw_records (source_id, entity_type, natural_key, generation, payload,"
            " row_hash, load_id, run_id, ingest_ts)"
            " VALUES ('appdb','student',:nk,1,CAST(:p AS jsonb),:rh,:ld,:run,:ts) RETURNING id",
            nk=f"{TAG}-{key}",
            p=canonical_json(pii),
            rh=f"{TAG}-{key}",
            ld=TAG,
            run=TAG,
            ts=ago(days),
        )
    for key, days, parent in (("stg_old", 100, "rr_old"), ("stg_recent", 1, "rr_blocked")):
        ids[key] = scalar(
            "INSERT INTO stg_student (generation, source_id, source_ref, raw_record_id, row_hash,"
            " materialized_at, student_id, guardian_email, last_name)"
            " VALUES (1,'appdb',:ref,:rr,:rh,:ts,:sid,:em,:ln) RETURNING id",
            ref=f"appdb:student:{TAG}-{key}",
            rr=ids[parent],
            rh=f"{TAG}-{key}",
            ts=ago(days),
            sid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{TAG}/{key}")),
            em=EMAIL,
            ln=NAME,
        )

    # --- lineage (180d) -----------------------------------------------------
    for key, days in (("fl_old", 200), ("fl_new", 10)):
        ids[key] = scalar(
            "INSERT INTO field_lineage (canonical_id, field, value_text, source_id, generation,"
            " observed_ts) VALUES (:cid,'appdb.student.guardian_email',:v,'appdb',1,:ts)"
            " RETURNING id",
            cid=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{TAG}/{key}")),
            v=EMAIL,
            ts=ago(days),
        )

    # --- audit log (730d) ---------------------------------------------------
    for key, days in (("al_old", 800), ("al_new", 10)):
        ids[key] = scalar(
            "INSERT INTO audit_log (actor, action, subject, detail, ts)"
            " VALUES ('system:test',:a,:s,CAST(:d AS jsonb),:ts) RETURNING id",
            a=f"{TAG}.{key}",
            s=TAG,
            d=canonical_json({"body": "already redacted"}),
            ts=ago(days),
        )

    # --- invariant results: ANONYMIZE at 180d ------------------------------
    for key, days in (("ir_old", 200), ("ir_new", 10)):
        ids[key] = scalar(
            "INSERT INTO invariant_results (run_id, rule_id, rule_version, record_ref,"
            " entity_type, verdict, detail, created_at)"
            " VALUES (:run,'C4','v1',:ref,'student','fail',CAST(:d AS jsonb),:ts) RETURNING id",
            run=f"{TAG}-{key}",
            ref=f"appdb:student:{TAG}-{key}",
            d=canonical_json(pii),
            ts=ago(days),
        )

    # --- candidates (90d) ---------------------------------------------------
    for key, days in (("elc_old", 200), ("elc_new", 10)):
        ids[key] = scalar(
            "INSERT INTO entity_link_candidates (source_ref, key_class, resolved_ref, generation,"
            " rule, accepted, detail, created_at)"
            " VALUES (:ref,'email',:res,1,'L1',true,CAST(:d AS jsonb),:ts) RETURNING id",
            ref=f"appdb:student:{TAG}-{key}",
            res=f"crm:contact:{TAG}-{key}",
            d=canonical_json(pii),
            ts=ago(days),
        )

    # --- ingest runs: ANONYMIZE error_detail at 90d ------------------------
    for key, days in (("run_old", 200), ("run_new", 10)):
        ex(
            text(
                "INSERT INTO ingest_runs (run_id, source_id, generation, status, started_at,"
                " error_detail) VALUES (:run,'appdb',1,'partial',:ts,CAST(:d AS jsonb))"
            ),
            {"run": f"{TAG}-{key}", "ts": ago(days), "d": canonical_json(pii)},
        )
        ids[key] = f"{TAG}-{key}"

    # --- conflicts / proposals / events ------------------------------------
    def conflict(key: str, days: int) -> int:
        return scalar(
            "INSERT INTO conflicts (fingerprint, type, entity_refs, sources, disagreeing_fields,"
            " observed_values, first_seen_run, last_seen_run, created_at)"
            " VALUES (:fp,'C4',CAST(:refs AS jsonb),'[\"appdb\"]'::jsonb,"
            " '[\"guardian_email\"]'::jsonb, CAST(:obs AS jsonb),:run,:run,:ts) RETURNING id",
            fp=f"{TAG}-{key}",
            refs=canonical_json([f"appdb:student:{TAG}-{key}", EMAIL]),
            obs=canonical_json(pii),
            run=TAG,
            ts=ago(days),
        )

    def proposal(key: str, conflict_id: int, days: int) -> int:
        return scalar(
            "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence,"
            " rationale, created_run, target_canonical_id, created_at)"
            " VALUES (:c,:fp,'{\"set\": {}}'::jsonb,0.5,CAST(:ev AS jsonb),:r,:run,:t,:ts)"
            " RETURNING id",
            c=conflict_id,
            fp=f"{TAG}-{key}",
            ev=canonical_json(pii),
            r=f"guardian_email={EMAIL}",
            run=TAG,
            t=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{TAG}/{key}")),
            ts=ago(days),
        )

    def event(key: str, proposal_id: int, days: int) -> int:
        return scalar(
            "INSERT INTO proposal_events (proposal_id, event, before, after, actor, ts)"
            " VALUES (:p,'noted',CAST(:b AS jsonb),CAST(:b AS jsonb),'system:test',:ts)"
            " RETURNING id",
            p=proposal_id,
            b=canonical_json(pii),
            ts=ago(days),
        )

    # 400d: past the anonymize window (365), inside the purge window (730)
    ids["c_anon"] = conflict("c_anon", 400)
    # 800d, no dependents: anonymised then purged
    ids["c_gone"] = conflict("c_gone", 800)
    # 800d but holds a proposal that is still inside ITS window: must survive
    ids["c_blocked"] = conflict("c_blocked", 800)
    # 800d but still a member of an incident created recently: must survive
    ids["c_ci_blocked"] = conflict("c_ci_blocked", 800)
    # recent: untouched, PII intact
    ids["c_new"] = conflict("c_new", 10)

    ids["p_old"] = proposal("p_old", ids["c_anon"], 400)
    ids["p_new"] = proposal("p_new", ids["c_new"], 10)
    ids["p_blocked"] = proposal("p_blocked", ids["c_blocked"], 10)
    ids["pe_old"] = event("pe_old", ids["p_old"], 400)
    ids["pe_new"] = event("pe_new", ids["p_new"], 10)

    ids["incident"] = scalar(
        "INSERT INTO incidents (label, created_at) VALUES (:l,:ts) RETURNING id",
        l=TAG,
        ts=ago(800),
    )
    for key, conflict_id, days in (
        ("ci_gone", ids["c_gone"], 800),
        ("ci_recent", ids["c_ci_blocked"], 10),
    ):
        ex(
            text(
                "INSERT INTO conflict_incidents (incident_id, conflict_id, distance, created_at)"
                " VALUES (:i,:c,0.1,:ts)"
            ),
            {"i": ids["incident"], "c": conflict_id, "ts": ago(days)},
        )
        ids[key] = (ids["incident"], conflict_id)
    return ids


def _exists(conn: Connection, table: str, row_id: Any, column: str = "id") -> bool:
    return bool(
        conn.execute(text(f"SELECT 1 FROM {table} WHERE {column} = :i"), {"i": row_id}).scalar()
    )


def test_purge_deletes_exactly_the_rows_outside_the_window(
    owner_conn: Connection, seeded: dict[str, Any]
) -> None:
    """The core assertion: everything outside its window goes, nothing inside does."""
    run_purge(owner_conn, now=NOW)

    gone = [
        ("stg_student", seeded["stg_old"], "id"),
        ("raw_records", seeded["rr_old"], "id"),
        ("field_lineage", seeded["fl_old"], "id"),
        ("audit_log", seeded["al_old"], "id"),
        ("entity_link_candidates", seeded["elc_old"], "id"),
        ("proposal_events", seeded["pe_old"], "id"),
        ("proposals", seeded["p_old"], "id"),
        ("conflicts", seeded["c_gone"], "id"),
    ]
    kept = [
        ("stg_student", seeded["stg_recent"], "id"),
        ("raw_records", seeded["rr_new"], "id"),
        ("field_lineage", seeded["fl_new"], "id"),
        ("audit_log", seeded["al_new"], "id"),
        ("entity_link_candidates", seeded["elc_new"], "id"),
        ("proposal_events", seeded["pe_new"], "id"),
        ("proposals", seeded["p_new"], "id"),
        ("proposals", seeded["p_blocked"], "id"),
        ("conflicts", seeded["c_anon"], "id"),
        ("conflicts", seeded["c_new"], "id"),
        ("invariant_results", seeded["ir_old"], "id"),
        ("invariant_results", seeded["ir_new"], "id"),
        ("ingest_runs", seeded["run_old"], "run_id"),
        ("ingest_runs", seeded["run_new"], "run_id"),
    ]
    for table, row_id, column in gone:
        assert not _exists(owner_conn, table, row_id, column), (
            f"{table} row outside its window survived the purge"
        )
    for table, row_id, column in kept:
        assert _exists(owner_conn, table, row_id, column), (
            f"{table} row INSIDE its window was purged"
        )


def test_a_parent_with_a_surviving_child_is_retained(
    owner_conn: Connection, seeded: dict[str, Any]
) -> None:
    """The FK guard, on all three graphs: staging, proposals, incident membership."""
    run_purge(owner_conn, now=NOW)
    # 200 days old, but its staging child is 1 day old
    assert _exists(owner_conn, "raw_records", seeded["rr_blocked"])
    # 800 days old, but holds a proposal from 10 days ago
    assert _exists(owner_conn, "conflicts", seeded["c_blocked"])
    # 800 days old, but its incident-membership edge is 10 days old
    assert _exists(owner_conn, "conflicts", seeded["c_ci_blocked"])


def test_anonymize_redacts_old_rows_and_leaves_recent_ones_alone(
    owner_conn: Connection, seeded: dict[str, Any]
) -> None:
    """Anonymize keeps the row and removes the values -- and only past the window."""
    run_purge(owner_conn, now=NOW)

    old = owner_conn.execute(
        text("SELECT detail FROM invariant_results WHERE id = :i"), {"i": seeded["ir_old"]}
    ).scalar_one()
    assert is_token(old["guardian_email"])
    assert old["grade"] == "6", "anonymize redacted a non-PII field it should have kept"
    assert EMAIL not in canonical_json(old)

    recent = owner_conn.execute(
        text("SELECT detail FROM invariant_results WHERE id = :i"), {"i": seeded["ir_new"]}
    ).scalar_one()
    assert recent["guardian_email"] == EMAIL, "a row inside its window was anonymised"

    error_detail = owner_conn.execute(
        text("SELECT error_detail FROM ingest_runs WHERE run_id = :i"), {"i": seeded["run_old"]}
    ).scalar_one()
    assert is_token(error_detail["guardian_email"])
    fresh = owner_conn.execute(
        text("SELECT error_detail FROM ingest_runs WHERE run_id = :i"), {"i": seeded["run_new"]}
    ).scalar_one()
    assert fresh["guardian_email"] == EMAIL


def test_conflicts_are_anonymised_before_they_are_purged(
    owner_conn: Connection, seeded: dict[str, Any]
) -> None:
    """The two-stage rule: values go at 365 days, the row at 730."""
    run_purge(owner_conn, now=NOW)
    row = (
        owner_conn.execute(
            text(
                "SELECT entity_refs, observed_values, fingerprint, type, status FROM conflicts "
                "WHERE id = :i"
            ),
            {"i": seeded["c_anon"]},
        )
        .mappings()
        .one()
    )
    assert is_token(row["observed_values"]["guardian_email"])
    assert any(is_token(ref) for ref in row["entity_refs"])
    assert EMAIL not in canonical_json(dict(row))
    # the identity that makes re-detection idempotent survives intact
    assert row["fingerprint"] == f"{TAG}-c_anon"
    assert row["type"] == "C4"

    untouched = owner_conn.execute(
        text("SELECT observed_values FROM conflicts WHERE id = :i"), {"i": seeded["c_new"]}
    ).scalar_one()
    assert untouched["guardian_email"] == EMAIL


def test_dry_run_reports_the_same_work_and_changes_nothing(
    owner_conn: Connection, seeded: dict[str, Any]
) -> None:
    planned = {
        (r.table, r.disposition): r.rows for r in run_purge(owner_conn, now=NOW, dry_run=True)
    }
    assert _exists(owner_conn, "raw_records", seeded["rr_old"])
    assert _exists(owner_conn, "proposals", seeded["p_old"])
    # leaf tables (no dependents) are exact
    assert planned[("stg_student", "purge")] >= 1
    assert planned[("field_lineage", "purge")] >= 1
    assert planned[("invariant_results", "anonymize")] >= 1

    done = {(r.table, r.disposition): r.rows for r in run_purge(owner_conn, now=NOW)}
    assert done[("stg_student", "purge")] == planned[("stg_student", "purge")]
    assert done[("field_lineage", "purge")] == planned[("field_lineage", "purge")]
    assert done[("invariant_results", "anonymize")] == planned[("invariant_results", "anonymize")]
    # a parent still blocked by a child this sweep will delete is a LOWER bound,
    # never an over-report -- see `run_purge`'s docstring
    assert planned[("raw_records", "purge")] <= done[("raw_records", "purge")]
    assert done[("raw_records", "purge")] >= 1


def test_a_second_sweep_is_a_no_op(owner_conn: Connection, seeded: dict[str, Any]) -> None:
    """Idempotent: nothing left to delete, and nothing left to re-redact."""
    run_purge(owner_conn, now=NOW)
    second = {(r.table, r.disposition): r.rows for r in run_purge(owner_conn, now=NOW)}
    for (table, disposition), rows in second.items():
        assert rows == 0, f"the second sweep still {disposition}d {rows} rows in {table}"


def test_the_sweep_audits_itself_without_naming_a_value(
    owner_conn: Connection, seeded: dict[str, Any]
) -> None:
    """R21: the retention job is itself an auditable action, and logs counts only."""
    run_purge(owner_conn, now=NOW)
    row = (
        owner_conn.execute(
            text(
                "SELECT actor, action, subject, detail FROM audit_log "
                "WHERE action = 'retention.purge' ORDER BY id DESC LIMIT 1"
            )
        )
        .mappings()
        .one()
    )
    assert row["actor"] == PURGE_ACTOR
    assert row["subject"].startswith("principal:")
    body = canonical_json(row["detail"])
    assert EMAIL not in body and NAME not in body
    # The sweep's own row is `audit_log.detail` like every other one, so it has
    # DESIGN's hash+preview shape (DESIGN.md L91, docs/retention-policy.md S4):
    # `{mode, body_sha256, body}`. It used to write a bare `{ran_at, tables}`,
    # which carried neither the hash nor the mode the design pins -- this
    # assertion moved down one level when `run_purge` was routed through
    # `recon.logging.audit_row` instead of hand-rolling its own detail.
    assert row["detail"]["mode"] == "safe"
    assert len(row["detail"]["body_sha256"]) == 64
    assert "ran_at" in row["detail"]["body"]
    tables = {entry["table"] for entry in row["detail"]["body"]["tables"]}
    assert "raw_records" in tables and "conflicts" in tables


def test_retained_tables_are_never_touched(owner_conn: Connection, seeded: dict[str, Any]) -> None:
    """A `retain` rule must report zero and issue no statement."""
    results = {(r.table, r.disposition): r for r in run_purge(owner_conn, now=NOW)}
    for rule in RETENTION:
        if rule.disposition != "retain":
            continue
        result = results[(rule.table, "retain")]
        assert result.rows == 0
        assert result.cutoff is None
        assert result.window_days is None
