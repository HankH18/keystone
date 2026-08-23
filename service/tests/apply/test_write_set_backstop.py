"""Migration 0012: the DATABASE refuses a proposal that lies about its write set.

R15 was, until this revision, enforced entirely in Python -- `recon.sensitive.classify`
and `recon.reconciler._assert_action_matches_classification`, both in the same process
as the code they guard. Every other guarantee in this project is a trigger or a grant.
`recon/sensitive.py` said so in prose, and `tests/reconciler/test_reconcile_run.py`'s
`test_the_database_refuses_the_row_the_code_refuses` -- named
`test_the_database_accepts_the_row_the_code_refuses` until this revision -- pinned it as a
measurement: it asserted the bad row IS accepted, and asked to be flipped the day a
migration closed the gap.

`ck_proposals_sensitive_covers_write_set` (migration 0012) closes it:

    sensitive OR NOT jsonb_exists_any(coalesce(action -> 'set', '{}'), <contract SS6>)

Chained with `KS002` -- `sensitive` implies born `sensitive_hold` -- writing a sensitive
path now forces the hold as a property of the TABLE, at every confidence, whatever the
conflict was classified as.

Everything here is STRUCTURAL: each test hand-builds the row it is about, so no seed
change can make it vacuous. The rows are written on connections that are rolled back;
`recon_writer` holds no DELETE on `proposals` (append-only, migration 0004), so a
committed probe row could not be cleaned up.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from recon.reference import AUTO_APPLY_ELIGIBLE, SENSITIVE_FIELDS

CONSTRAINT = "ck_proposals_sensitive_covers_write_set"

_INSERT_PROPOSAL = text(
    """
    INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence,
                           status, sensitive, created_run, target_canonical_id)
    SELECT c.id, :fingerprint, CAST(:action AS jsonb), 0.99, '{}'::jsonb,
           CAST(:status AS proposal_status), :sensitive, 'write-set-backstop-probe',
           gen_random_uuid()
      FROM conflicts c ORDER BY c.fingerprint LIMIT 1
    RETURNING id
    """
)

_CONSTRAINT_DEF = text(
    """
    SELECT pg_get_constraintdef(con.oid) AS definition
      FROM pg_constraint con
      JOIN pg_class rel ON rel.oid = con.conrelid
     WHERE rel.relname = 'proposals' AND con.conname = :name
    """
)


def _insert(conn: Any, *, action: str, sensitive: bool, status: str = "pending") -> Any:
    return conn.execute(
        _INSERT_PROPOSAL,
        {
            "fingerprint": f"probe-{action}-{sensitive}-{status}",
            "action": action,
            "sensitive": sensitive,
            "status": status,
        },
    ).scalar_one()


def _violated_constraint(error: BaseException) -> str | None:
    """The constraint name out of the error's own diagnostics.

    A CHECK raises `23514`, which a dozen other constraints on this table also
    raise, so asserting the SQLSTATE alone would stay green if some unrelated
    check caught the row. The name is the exact handle a project SQLSTATE would
    otherwise have been.
    """
    original = getattr(error, "orig", error)
    if isinstance(original, psycopg.Error):
        return original.diag.constraint_name
    return None  # pragma: no cover - every raise here is a psycopg error


@pytest.fixture
def recon_conn(store: Any) -> Any:
    """A `recon_writer` connection that is ROLLED BACK. The proposing role."""
    from recon.db import ROLE_RECON_WRITER, role_connection

    with role_connection(ROLE_RECON_WRITER, commit=False) as conn:
        yield conn


# =====================================================================================
# the row the database used to accept
# =====================================================================================


def test_the_exact_row_the_gap_was_pinned_on_is_now_refused(recon_conn: Any) -> None:
    """`{"set": {"crm.contact.dob": ...}}` with `sensitive = false`, `status = 'pending'`.

    Character for character the row the reconciler suite's
    `test_the_database_refuses_the_row_the_code_refuses` hand-INSERTed and
    asserted was accepted, back when it was called
    `test_the_database_accepts_the_row_the_code_refuses`. That test now asserts
    the refusal; this is the same probe from the apply side.
    """
    with pytest.raises(DBAPIError) as raised:
        _insert(
            recon_conn,
            action='{"set": {"crm.contact.dob": "2010-01-01"}}',
            sensitive=False,
        )
    recon_conn.rollback()
    assert _violated_constraint(raised.value) == CONSTRAINT


def test_the_demonstrated_attacks_own_row_is_refused(recon_conn: Any) -> None:
    """The red team's write: `crm.contact.email`, contract SS12 D-7's billing email."""
    with pytest.raises(DBAPIError) as raised:
        _insert(
            recon_conn,
            action='{"set": {"crm.contact.email": "attacker@evil.test"}}',
            sensitive=False,
        )
    recon_conn.rollback()
    assert _violated_constraint(raised.value) == CONSTRAINT


@pytest.mark.parametrize("path", sorted(SENSITIVE_FIELDS))
def test_no_unheld_proposal_may_name_any_sensitive_path(recon_conn: Any, path: str) -> None:
    """All twenty of contract SS6's paths, each constructed, each refused."""
    with pytest.raises(DBAPIError) as raised:
        _insert(recon_conn, action=f'{{"set": {{"{path}": "v"}}}}', sensitive=False)
    recon_conn.rollback()
    assert _violated_constraint(raised.value) == CONSTRAINT


def test_a_sensitive_path_smuggled_beside_an_eligible_one_is_refused(recon_conn: Any) -> None:
    """`jsonb_exists_any` reads EVERY key, so one bad key in a multi-key action is enough."""
    with pytest.raises(DBAPIError) as raised:
        _insert(
            recon_conn,
            action='{"set": {"crm.contact.grade": "7", "crm.deal.stage": "Closed Won"}}',
            sensitive=False,
        )
    recon_conn.rollback()
    assert _violated_constraint(raised.value) == CONSTRAINT


# =====================================================================================
# controls -- without these the tests above could pass by refusing everything
# =====================================================================================


@pytest.mark.parametrize("path", sorted(AUTO_APPLY_ELIGIBLE))
def test_an_eligible_write_is_still_accepted(recon_conn: Any, path: str) -> None:
    """The control. The constraint must refuse the write SET, not the INSERT."""
    assert _insert(recon_conn, action=f'{{"set": {{"{path}": "v"}}}}', sensitive=False)
    recon_conn.rollback()


def test_a_held_proposal_may_write_its_sensitive_path(recon_conn: Any) -> None:
    """C4's committed template writes `crm.contact.email` and must remain insertable.

    R15 forces a sensitive proposal to HUMAN REVIEW; it does not forbid the fix
    from existing. A constraint that refused this row would delete the entire C4
    fix template from the system, which is the opposite of what SS6 requires.
    """
    assert _insert(
        recon_conn,
        action='{"set": {"crm.contact.email": "new@example.test"}}',
        sensitive=True,
        status="sensitive_hold",
    )


def test_an_evidence_only_proposal_is_still_accepted(recon_conn: Any) -> None:
    """`{"set": {}}` names no path, so there is nothing for the constraint to object to."""
    assert _insert(recon_conn, action='{"set": {}}', sensitive=False)


# =====================================================================================
# sabotage: the constraint is what is doing the refusing
# =====================================================================================


def test_dropping_the_constraint_makes_the_bad_row_land_again(reader: Any) -> None:
    """Sabotage, inside a transaction that is rolled back.

    Without this, every refusal above could be coming from some other rule on the
    table and the migration could be a no-op. The constraint is dropped, the same
    row is inserted and lands, and the whole transaction -- DDL included -- is
    rolled back, so the store leaves this test exactly as it entered it.

    Run as the schema OWNER because dropping a constraint is DDL, which is also
    the honest statement of this backstop's scope: it binds every principal's
    ROWS, and the principal who writes migrations can remove it.
    """
    action = '{"set": {"crm.contact.dob": "2010-01-01"}}'

    # 1. with the constraint installed, the row is refused.
    with reader.connect() as conn:
        with pytest.raises(DBAPIError) as raised:
            _insert(conn, action=action, sensitive=False)
        conn.rollback()
        assert _violated_constraint(raised.value) == CONSTRAINT

        # 2. SABOTAGE: drop it, and the SAME row lands. Rolled back either way,
        #    DDL included -- Postgres is transactional for DDL, so the constraint
        #    is restored by the rollback and never absent outside this block.
        try:
            conn.execute(text(f"ALTER TABLE proposals DROP CONSTRAINT {CONSTRAINT}"))
            landed = _insert(conn, action=action, sensitive=False)
            assert landed, (
                "the row did not land even with the constraint dropped, so the "
                "refusals in this file are coming from something else"
            )
        finally:
            conn.rollback()

    # 3. ...and the constraint is back on a fresh connection, still refusing.
    with reader.connect() as conn:
        assert conn.execute(_CONSTRAINT_DEF, {"name": CONSTRAINT}).fetchone() is not None
        with pytest.raises(DBAPIError) as reraised:
            _insert(conn, action=action, sensitive=False)
        conn.rollback()
    assert _violated_constraint(reraised.value) == CONSTRAINT


# =====================================================================================
# the frozen list versus the live constant
# =====================================================================================


def test_the_installed_constraint_still_matches_the_contract(reader: Any) -> None:
    """The drift alarm the migration's frozen path list is paid for by.

    Migration 0012 writes contract SS6's set out as literals rather than importing
    `recon.reference`, because a migration is a historical artifact and one that
    imported a live constant would silently change what an old database enforces.
    The cost of freezing is drift, so drift is made LOUD: the installed
    constraint's own definition is read back out of `pg_get_constraintdef()`, its
    literals parsed, and compared with the live set. Adding a path to SS6 without
    a follow-up migration fails here, naming both sets.
    """
    with reader.connect() as conn:
        row = conn.execute(_CONSTRAINT_DEF, {"name": CONSTRAINT}).fetchone()
    assert row is not None, f"{CONSTRAINT} is not installed; migration 0012 did not run"

    array = re.search(r"ARRAY\[(.*?)\]", row.definition, re.DOTALL)
    assert array, f"could not find the path array in: {row.definition}"
    enforced = set(re.findall(r"'((?:[^']|'')*)'::text", array.group(1)))
    assert enforced, f"parsed no paths out of: {array.group(1)}"

    assert enforced == set(SENSITIVE_FIELDS), (
        "the database enforces a different sensitive-path set from the one the code "
        "classifies on.\n"
        f"  only in the database : {sorted(enforced - set(SENSITIVE_FIELDS))}\n"
        f"  only in recon.reference: {sorted(set(SENSITIVE_FIELDS) - enforced)}\n"
        "Contract SS6 changed without a migration: add one, or the backstop is "
        "protecting a set nobody classifies on any more."
    )


def test_the_constraint_is_validated_and_not_deferrable(reader: Any) -> None:
    """A NOT VALID constraint binds new rows only; a deferrable one can be turned off."""
    with reader.connect() as conn:
        row = conn.execute(
            text(
                "SELECT con.convalidated, con.condeferrable, con.contype "
                "  FROM pg_constraint con JOIN pg_class rel ON rel.oid = con.conrelid "
                " WHERE rel.relname = 'proposals' AND con.conname = :name"
            ),
            {"name": CONSTRAINT},
        ).one()
    assert row.convalidated, "the constraint is NOT VALID, so it binds no existing row"
    assert not row.condeferrable, "a DEFERRABLE check can be switched off inside a session"
    assert row.contype == "c"


def test_the_backstop_binds_the_schema_owner_too(reader: Any) -> None:
    """Defence in depth, stated as a measurement rather than as a claim.

    `reader` is `DATABASE_URL` itself -- the schema owner, who bypasses grants by
    definition. A CHECK is not a grant, so the row is refused for the owner as
    well. This is a real property and it is deliberately not called a boundary:
    `test_dropping_the_constraint_makes_the_bad_row_land_again` shows the same
    principal removing it. The boundary is the three non-owner roles.
    """
    with reader.connect() as conn:
        with pytest.raises(DBAPIError) as raised:
            _insert(
                conn,
                action='{"set": {"appdb.student.student_number": "S-1"}}',
                sensitive=False,
            )
        conn.rollback()
    assert _violated_constraint(raised.value) == CONSTRAINT


def test_the_graded_store_itself_satisfies_the_new_constraint(reader: Any) -> None:
    """No committed proposal was made unrepresentable by this migration.

    The reconciler wrote every proposal in this store BEFORE the constraint could
    have been consulted only in the sense that it was written by the same code
    path -- so this re-checks the whole population against the rule the database
    now enforces, from the rows rather than from the classifier that produced
    them. A count of zero here with a non-zero denominator is the statement.
    """
    with reader.connect() as conn:
        total, violating = conn.execute(
            text(
                "SELECT count(*), "
                "       count(*) FILTER (WHERE NOT p.sensitive AND EXISTS ("
                "         SELECT 1 FROM jsonb_object_keys(p.action -> 'set') k"
                "          WHERE k = ANY(CAST(:paths AS text[]))))"
                "  FROM proposals p"
            ),
            {"paths": sorted(SENSITIVE_FIELDS)},
        ).one()
    assert total > 0, "the store holds no proposals, so this test is vacuous"
    assert violating == 0, f"{violating} of {total} committed proposals violate {CONSTRAINT}"
