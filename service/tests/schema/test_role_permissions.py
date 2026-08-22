"""The holds-before-writes boundary, proved against a live Postgres.

Every negative test here asserts on ``psycopg.errors.InsufficientPrivilege`` /
SQLSTATE 42501 specifically -- never merely "something was raised" -- and every
negative is paired with a **positive control**: the same role, over the same
connection path, performing the write it *is* allowed to perform. A test that
passed because the connection was broken would fail its control.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from recon.db import ROLE_APPLY_WRITER, ROLE_RECON_WRITER, ROLE_REVIEW_WRITER
from tests.schema.conftest import ROLES, RoleTxn, assert_insufficient_privilege, assert_sqlstate

CANONICAL_TABLE = "entities"
LANDING_TABLE = "raw_records"


# ---------------------------------------------------------------------------
# The premise every other test rests on: these roles are NOT the owner.
# ---------------------------------------------------------------------------
def test_writer_roles_are_not_the_schema_owner(owner_engine: Engine) -> None:
    """A table owner bypasses its own grants, so this must never be the owner.

    If `recon_writer` owned `entities`, every negative test below would still
    pass trivially while proving nothing.
    """
    with owner_engine.connect() as conn:
        owners = dict(
            conn.execute(
                text("SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public'")
            ).all()
        )
        attrs = dict(
            conn.execute(
                text(
                    "SELECT rolname, rolsuper OR rolbypassrls FROM pg_roles "
                    "WHERE rolname = ANY(:roles)"
                ),
                {"roles": list(ROLES)},
            ).all()
        )

    assert set(attrs) == set(ROLES), f"writer roles missing from the cluster: {attrs}"
    for role in ROLES:
        assert attrs[role] is False, f"{role} is superuser/bypassrls; grants would not apply"

    assert owners[CANONICAL_TABLE] not in ROLES
    assert owners[LANDING_TABLE] not in ROLES
    owning_roles = {owner for owner in owners.values() if owner in ROLES}
    assert not owning_roles, f"writer roles must own no tables, but own: {owning_roles}"


@pytest.mark.parametrize("role", ROLES)
def test_role_connection_authenticates_as_that_role(role_txn: RoleTxn, role: str) -> None:
    """Guards against the boundary being silently disabled by a shared DSN."""
    with role_txn(role) as conn:
        current, session = conn.execute(text("SELECT current_user, session_user")).one()
    assert current == role
    assert session == role


# ---------------------------------------------------------------------------
# recon_writer: INSERT-only on the detection surface
# ---------------------------------------------------------------------------
RECON_WRITER_ALLOWED_INSERTS = (
    (
        "raw_records",
        "INSERT INTO raw_records (source_id, entity_type, natural_key, generation, "
        "payload, row_hash, load_id) "
        "VALUES ('crm', 'contact', 'CRM-0000001', 3, '{}'::jsonb, 'h', 'load')",
    ),
    (
        "ingest_runs",
        "INSERT INTO ingest_runs (run_id, source_id, generation, status) "
        "VALUES ('run-1', 'crm', 3, 'ok')",
    ),
    (
        "stg_crm_contact",
        "INSERT INTO stg_crm_contact (generation, source_id, source_ref, row_hash, crm_id) "
        "VALUES (3, 'crm', 'crm:contact:CRM-0000001', 'h', 'CRM-0000001')",
    ),
    (
        "stg_crm_deal",
        "INSERT INTO stg_crm_deal (generation, source_id, source_ref, row_hash, deal_id) "
        "VALUES (3, 'crm', 'crm:deal:DEAL-0000001', 'h', 'DEAL-0000001')",
    ),
    (
        "stg_student",
        "INSERT INTO stg_student (generation, source_id, source_ref, row_hash, student_id) "
        "VALUES (3, 'appdb', 'appdb:student:s1', 'h', 's1')",
    ),
    (
        "stg_enrollment",
        "INSERT INTO stg_enrollment (generation, source_id, source_ref, row_hash, enrollment_id) "
        "VALUES (3, 'appdb', 'appdb:enrollment:e1', 'h', 'e1')",
    ),
    (
        "stg_payment",
        "INSERT INTO stg_payment (generation, source_id, source_ref, row_hash, payment_id) "
        "VALUES (3, 'payments', 'payments:payment:pi_1', 'h', 'pi_1')",
    ),
    (
        "entity_links",
        "INSERT INTO entity_links (canonical_id, source_id, source_key, source_ref, method, "
        "generation) VALUES (gen_random_uuid(), 'crm', 'CRM-9999999', "
        "'crm:contact:CRM-9999999', 'L1', 3)",
    ),
    (
        "entity_link_candidates",
        "INSERT INTO entity_link_candidates (source_ref, key_class, resolved_ref, generation, "
        "accepted) VALUES ('crm:contact:CRM-0000001', 'namedob', 'appdb:student:s2', 3, false)",
    ),
    (
        # MAJOR 6 / migration 0004: canonical CREATION is deterministic pipeline
        # output, so entity resolution materialises the row. MUTATION stays the
        # guarded path. See test_write_boundary_hardening.py for the pairing.
        "entities",
        "INSERT INTO entities (canonical_id, entity_type, current) "
        "VALUES (gen_random_uuid(), 'person', '{}'::jsonb)",
    ),
    (
        "field_lineage",
        "INSERT INTO field_lineage (canonical_id, field, value_text, source_id, generation) "
        "VALUES (gen_random_uuid(), 'grade', '4', 'crm', 3)",
    ),
    (
        "invariant_results",
        "INSERT INTO invariant_results (run_id, rule_id, rule_version, record_ref, entity_type, "
        "verdict) VALUES ('run-1', 'R-006', 'v1', 'crm:contact:CRM-0000001', 'contact', 'pass')",
    ),
    (
        "conflicts",
        "INSERT INTO conflicts (fingerprint, type, entity_refs, sources, disagreeing_fields, "
        "first_seen_run, last_seen_run) VALUES ('fp-recon-writer-control', 'field-disagreement', "
        "'[]'::jsonb, '[]'::jsonb, '[]'::jsonb, 'run-1', 'run-1')",
    ),
    (
        "audit_log",
        # Machine-scoped actor: migration 0004 forbids recon_writer attributing
        # an action to a human reviewer (SQLSTATE KS003). The claim under test
        # -- "recon_writer may append to audit_log" -- is unchanged.
        "INSERT INTO audit_log (actor, action, subject) VALUES ('system:recon', 'detect', 'run-1')",
    ),
)


@pytest.mark.parametrize(
    ("table", "statement"),
    RECON_WRITER_ALLOWED_INSERTS,
    ids=[t for t, _ in RECON_WRITER_ALLOWED_INSERTS],
)
def test_recon_writer_may_insert(role_txn: RoleTxn, table: str, statement: str) -> None:
    """Positive control for the whole INSERT surface the ticket pins."""
    with role_txn(ROLE_RECON_WRITER) as conn:
        result = conn.execute(text(statement))
        assert result.rowcount == 1, f"insert into {table} affected {result.rowcount} rows"


def test_recon_writer_may_insert_a_pending_proposal(
    role_txn: RoleTxn, seeded_rows: dict[str, object]
) -> None:
    """The reconciler's actual job: land a proposal in a non-terminal state."""
    with role_txn(ROLE_RECON_WRITER) as conn:
        status = conn.execute(
            text(
                """
                INSERT INTO proposals (
                    conflict_id, fingerprint, action, confidence, evidence, created_run, status,
                    target_canonical_id)
                VALUES (:cid, 'fp-pending-control', '{"set": {}}'::jsonb, 0.42, '{}'::jsonb,
                        'run-1', 'sensitive_hold', :target)
                RETURNING status
                """
            ),
            {"cid": seeded_rows["conflict_id"], "target": seeded_rows["canonical_id"]},
        ).scalar_one()
    assert status == "sensitive_hold"


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("UPDATE entities SET entity_type = 'tampered'", id="update-canonical"),
        pytest.param("DELETE FROM entities", id="delete-canonical"),
        # The `insert-canonical` case that used to live here asserted the OLD
        # role shape. MAJOR 6 / migration 0004 reverses it: canonical CREATION
        # is deterministic pipeline output and belongs to recon_writer, while
        # MUTATION stays the guarded apply path. The reversed contract is now
        # asserted positively, not dropped -- see
        # test_write_boundary_hardening.py::test_recon_writer_may_insert_a_canonical_entity
        # and ::test_apply_writer_cannot_insert_a_canonical_entity.
    ],
)
def test_recon_writer_cannot_mutate_the_canonical_table(role_txn: RoleTxn, statement: str) -> None:
    """recon_writer may append canonical rows but never UPDATE or DELETE one."""
    with role_txn(ROLE_RECON_WRITER) as conn:  # positive control, same role
        conn.execute(text("SELECT count(*) FROM entities")).scalar_one()
        conn.execute(
            text("INSERT INTO audit_log (actor, action) VALUES ('system:recon', 'control')")
        )

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text(statement))
    assert_insufficient_privilege(excinfo.value)


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("UPDATE raw_records SET row_hash = 'tampered'", id="update-landing"),
        pytest.param("DELETE FROM raw_records", id="delete-landing"),
    ],
)
def test_recon_writer_cannot_mutate_the_landing_table(role_txn: RoleTxn, statement: str) -> None:
    """raw_records is append-only: INSERT yes, UPDATE/DELETE never."""
    with role_txn(ROLE_RECON_WRITER) as conn:  # positive control: INSERT is allowed
        conn.execute(
            text(
                "INSERT INTO raw_records (source_id, entity_type, natural_key, generation, "
                "payload, row_hash, load_id) "
                "VALUES ('crm', 'contact', 'CRM-0000002', 3, '{}'::jsonb, 'h', 'load')"
            )
        )

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text(statement))
    assert_insufficient_privilege(excinfo.value)


def test_recon_writer_cannot_approve_its_own_proposal(
    role_txn: RoleTxn, seeded_rows: dict[str, object]
) -> None:
    """Holds before writes: the detector may propose but never decide."""
    with role_txn(ROLE_RECON_WRITER) as conn:  # control: it can read and insert proposals
        conn.execute(
            text("SELECT status FROM proposals WHERE id = :pid"),
            {"pid": seeded_rows["proposal_id"]},
        ).scalar_one()

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text("UPDATE proposals SET status = 'approved' WHERE id = :pid"),
            {"pid": seeded_rows["proposal_id"]},
        )
    assert_insufficient_privilege(excinfo.value)


def test_recon_writer_cannot_write_proposal_events(
    role_txn: RoleTxn, seeded_rows: dict[str, object]
) -> None:
    """Only the apply path writes the reversal ledger."""
    with role_txn(ROLE_RECON_WRITER) as conn:  # control
        conn.execute(text("SELECT count(*) FROM proposal_events")).scalar_one()

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(
                "INSERT INTO proposal_events (proposal_id, event, actor) "
                "VALUES (:pid, 'applied', 'recon')"
            ),
            {"pid": seeded_rows["proposal_id"]},
        )
    assert_insufficient_privilege(excinfo.value)


# ---------------------------------------------------------------------------
# apply_writer: the canonical layer, and only through the apply path
# ---------------------------------------------------------------------------
def test_apply_writer_may_update_entities_with_a_reversal_record(
    role_txn: RoleTxn, seeded_rows: dict[str, object]
) -> None:
    """The permitted apply path: canonical write + reversal record, one txn.

    `SET CONSTRAINTS ALL IMMEDIATE` forces the deferred trigger to run inside
    the test transaction, so the assertion is real even though we roll back.

    RULING 3 / migration 0005: the cited proposal must itself be *approved* and
    must target this exact entity, so the control now cites
    `approved_proposal_id` rather than the pending one. Citing the pending
    proposal here would have been the very bypass 0005 closes -- see
    test_three_role_boundary.py::test_a_pending_proposal_cannot_authorise_a_canonical_write.
    """
    with role_txn(ROLE_APPLY_WRITER) as conn:
        # MAJOR 5 / migration 0004: the reversal record must name the row it
        # authorises and capture its pre-update value. The seeded entity's
        # `current` is '{}', which is what `before` records here. 0005 adds
        # `after`, so the ledger cannot misreport what was written.
        conn.execute(
            text(
                "INSERT INTO proposal_events "
                "(proposal_id, canonical_id, event, before, after, actor) "
                "VALUES (:pid, :cid, 'applied', '{}'::jsonb, "
                "'{\"grade\": \"4\"}'::jsonb, 'system:apply')"
            ),
            {"pid": seeded_rows["approved_proposal_id"], "cid": seeded_rows["canonical_id"]},
        )
        result = conn.execute(
            text(
                'UPDATE entities SET current = \'{"grade": "4"}\'::jsonb, updated_at = now() '
                "WHERE canonical_id = :cid"
            ),
            {"cid": seeded_rows["canonical_id"]},
        )
        assert result.rowcount == 1
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_apply_writer_may_not_insert_a_canonical_entity(role_txn: RoleTxn) -> None:
    """Reversed contract: "no reversal record needed" was the bug, not a feature.

    This assertion used to read "materializing a new canonical row needs no
    reversal record (INSERT only)" and pass. That is exactly the hole MAJOR 6
    names: the correlation trigger is AFTER UPDATE, so an INSERT bypassed the
    guarded path entirely and apply_writer could fabricate canonical state with
    no proposal and no way back. Migration 0004 pins canonical CREATION to the
    deterministic pipeline (recon_writer) and leaves apply_writer only
    MUTATION, so a proposal can change canonical state but never invent it.

    The old assertion is replaced by the correct one rather than deleted, so
    the change of contract is visible here rather than implied.
    """
    with role_txn(ROLE_APPLY_WRITER) as conn:  # control: it still reads the table
        conn.execute(text("SELECT count(*) FROM entities")).scalar_one()

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(
                "INSERT INTO entities (canonical_id, entity_type, current) "
                "VALUES (:cid, 'person', '{}'::jsonb)"
            ),
            {"cid": uuid.uuid5(uuid.NAMESPACE_URL, "keystone/tests/schema/new-entity")},
        )
    assert_insufficient_privilege(excinfo.value)


def test_entities_update_without_a_reversal_record_is_rejected(
    role_txn: RoleTxn, seeded_rows: dict[str, object]
) -> None:
    """ "Only through the apply path" is a trigger, not a comment.

    SQLSTATE KS001 is project-specific: no unrelated failure can produce it.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text("UPDATE entities SET current = '{}'::jsonb WHERE canonical_id = :cid"),
            {"cid": seeded_rows["canonical_id"]},
        )
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, "KS001")


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("DELETE FROM entities", id="delete-canonical"),
        pytest.param(
            "INSERT INTO raw_records (source_id, entity_type, natural_key, generation, "
            "payload, row_hash, load_id) "
            "VALUES ('crm', 'contact', 'CRM-0000003', 3, '{}'::jsonb, 'h', 'load')",
            id="insert-landing",
        ),
        pytest.param(
            "INSERT INTO invariant_results (run_id, rule_id, rule_version, record_ref, "
            "entity_type, verdict) VALUES ('r', 'R-006', 'v1', 'ref', 'contact', 'fail')",
            id="insert-evidence",
        ),
        pytest.param(
            "UPDATE conflicts SET status = 'resolved'",
            id="update-conflicts",
        ),
    ],
)
def test_apply_writer_is_confined_to_the_apply_path(role_txn: RoleTxn, statement: str) -> None:
    """apply_writer may not delete canonical rows nor write the detection surface."""
    with role_txn(ROLE_APPLY_WRITER) as conn:  # positive control on its own surface
        # RULING 5 / migration 0005: apply_writer is a machine role, so its
        # audit actor must match ^system: (SQLSTATE KS003). The claim under
        # test -- "apply_writer may append to audit_log" -- is unchanged.
        conn.execute(
            text("INSERT INTO audit_log (actor, action) VALUES ('system:apply', 'control')")
        )

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(text(statement))
    assert_insufficient_privilege(excinfo.value)


def test_apply_writer_cannot_manufacture_a_proposal(
    role_txn: RoleTxn, seeded_rows: dict[str, object]
) -> None:
    """It applies decided proposals; it cannot create the work, nor decide it.

    The control used to read `UPDATE ... status = 'approved', decided_by =
    'reviewer'` and pass -- which is exactly the hole RULING 1 names: with two
    roles, "approve" and "apply" were the same principal, so the machine could
    approve its own work. Migration 0005 splits them: `apply_writer`'s grant no
    longer reaches `decided_by`/`decided_at` at all, and the transition trigger
    allows it only `approved -> applied` and `applied -> rolled_back`. The
    control is therefore the *apply* leg against the already-approved fixture
    proposal, and the assertion that it cannot approve is not dropped but
    strengthened -- see
    test_three_role_boundary.py::test_apply_writer_cannot_approve_a_proposal.
    """
    with role_txn(ROLE_APPLY_WRITER) as conn:  # control: the apply leg IS allowed
        result = conn.execute(
            text("UPDATE proposals SET status = 'applied' WHERE id = :pid"),
            {"pid": seeded_rows["approved_proposal_id"]},
        )
        assert result.rowcount == 1

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(
                "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, "
                "evidence, created_run, target_canonical_id) "
                "VALUES (:cid, 'fp-apply-writer', '{\"set\": {}}'::jsonb, 1.0, '{}'::jsonb, 'r', "
                ":target)"
            ),
            {"cid": seeded_rows["conflict_id"], "target": seeded_rows["canonical_id"]},
        )
    assert_insufficient_privilege(excinfo.value)


# ---------------------------------------------------------------------------
# review_writer: decides, and does nothing else (RULING 1 / migration 0005)
# ---------------------------------------------------------------------------
APPROVE = (
    "UPDATE proposals SET status = 'approved', decided_by = 'reviewer:alice', "
    "decided_at = now() WHERE id = :pid"
)


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(
            "INSERT INTO entities (canonical_id, entity_type, current) "
            "VALUES (gen_random_uuid(), 'person', '{}'::jsonb)",
            id="insert-canonical",
        ),
        pytest.param("UPDATE entities SET current = '{}'::jsonb", id="update-canonical"),
        pytest.param("DELETE FROM entities", id="delete-canonical"),
    ],
)
def test_review_writer_has_no_write_access_to_the_canonical_table(
    role_txn: RoleTxn, seeded_rows: dict[str, object], statement: str
) -> None:
    """The decider decides. It never touches canonical state, by any verb.

    A reviewer role that could also write `entities` would collapse "approve"
    and "apply" back into one principal from the other direction.
    """
    with role_txn(ROLE_REVIEW_WRITER) as conn:  # control: its own decision surface works
        result = conn.execute(text(APPROVE), {"pid": seeded_rows["proposal_id"]})
        assert result.rowcount == 1

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_REVIEW_WRITER) as conn:
        conn.execute(text(statement))
    assert_insufficient_privilege(excinfo.value)


def test_review_writer_cannot_create_the_work_it_decides(
    role_txn: RoleTxn, seeded_rows: dict[str, object]
) -> None:
    """No INSERT on proposals: the decider may not manufacture its own caseload."""
    with role_txn(ROLE_REVIEW_WRITER) as conn:  # control: it can read and decide
        conn.execute(
            text("SELECT status FROM proposals WHERE id = :pid"),
            {"pid": seeded_rows["proposal_id"]},
        ).scalar_one()
        conn.execute(text(APPROVE), {"pid": seeded_rows["proposal_id"]})

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_REVIEW_WRITER) as conn:
        conn.execute(
            text(
                "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, "
                "evidence, created_run, target_canonical_id) "
                "VALUES (:cid, 'fp-review-writer', '{\"set\": {}}'::jsonb, 1.0, '{}'::jsonb, 'r', "
                ":target)"
            ),
            {"cid": seeded_rows["conflict_id"], "target": seeded_rows["canonical_id"]},
        )
    assert_insufficient_privilege(excinfo.value)


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(
            "INSERT INTO raw_records (source_id, entity_type, natural_key, generation, "
            "payload, row_hash, load_id) "
            "VALUES ('crm', 'contact', 'CRM-0000009', 3, '{}'::jsonb, 'h', 'load')",
            id="insert-landing",
        ),
        pytest.param(
            "INSERT INTO conflicts (fingerprint, type, entity_refs, sources, "
            "disagreeing_fields, first_seen_run, last_seen_run) VALUES ('fp-review', 'x', "
            "'[]'::jsonb, '[]'::jsonb, '[]'::jsonb, 'r', 'r')",
            id="insert-evidence",
        ),
        pytest.param(
            "INSERT INTO proposal_events (proposal_id, event, actor) VALUES (1, 'applied', 'r')",
            id="insert-reversal-ledger",
        ),
        pytest.param(
            "INSERT INTO budget_reservations (scope, idempotency_key, reserve_microusd) "
            "VALUES ('daily', 'review-writer-attack', 1)",
            id="insert-budget-reservation",
        ),
    ],
)
def test_review_writer_cannot_write_the_detection_or_apply_surfaces(
    role_txn: RoleTxn, statement: str
) -> None:
    """It is not the proposer and not the applier; it holds neither surface."""
    with role_txn(ROLE_REVIEW_WRITER) as conn:  # control: its own audit surface works
        conn.execute(
            text("INSERT INTO audit_log (actor, action) VALUES ('reviewer:alice', 'control')")
        )

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_REVIEW_WRITER) as conn:
        conn.execute(text(statement))
    assert_insufficient_privilege(excinfo.value)
