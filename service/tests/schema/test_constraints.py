"""Constraints that carry a requirement, exercised with real DML.

Each of these asserts on the exact SQLSTATE, and each has a positive control so
a broken connection cannot masquerade as an enforced constraint.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from recon.db import ROLE_RECON_WRITER
from tests.schema.conftest import TEST_TAG, RoleTxn, assert_sqlstate

CHECK_VIOLATION = "23514"
UNIQUE_VIOLATION = "23505"
INVALID_ENUM_INPUT = "22P02"

BUDGET_CAP_EXCEEDED = "KS006"

#: The two statuses a proposal may be *born* in. Everything else is reached by
#: a transition, and since migration 0006 that is true for the owner too.
BIRTH_STATUSES = ("pending", "sensitive_hold")

INSERT_BUDGET = (
    "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) VALUES (:scope, :cap, :spent)"
)


@contextmanager
def owner_txn(engine: Engine) -> Iterator[Connection]:
    """An owner transaction that always rolls back.

    Migration 0005 revokes every INSERT and UPDATE on ``budget_ledger`` from
    ``recon_writer`` -- the capped party has no writable spend column left at
    all. Ledger rows (scope + cap) are therefore provisioned by the ops
    principal, which is what these CHECK-constraint tests must connect as. The
    claims below are unchanged: they are about the ``ck_budget_spent_within_cap``
    backstop, not about who may write the row. Who may write the row is
    asserted, negatively and positively, in
    ``test_write_boundary_hardening.py`` and ``test_budget_reservations.py``.
    """
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


def test_budget_cap_check_accepts_spend_up_to_the_cap(owner_engine: Engine) -> None:
    """Positive control: spending exactly the cap is legal."""
    with owner_txn(owner_engine) as conn:
        conn.execute(
            text(INSERT_BUDGET), {"scope": "run:control", "cap": 1_000_000, "spent": 1_000_000}
        )
        spent = conn.execute(
            text("SELECT spent_microusd FROM budget_ledger WHERE scope = 'run:control'")
        ).scalar_one()
    assert spent == 1_000_000


def test_budget_cap_check_rejects_an_overspending_insert(owner_engine: Engine) -> None:
    with pytest.raises(DBAPIError) as excinfo, owner_txn(owner_engine) as conn:
        conn.execute(
            text(INSERT_BUDGET), {"scope": "run:overspend", "cap": 1_000_000, "spent": 1_000_001}
        )
    assert_sqlstate(excinfo.value, CHECK_VIOLATION)
    assert "ck_budget_spent_within_cap" in str(excinfo.value.orig)


def test_budget_cap_check_rejects_an_overspending_update(owner_engine: Engine) -> None:
    """The backstop under the reservation trigger, exercised as a raw UPDATE.

    The CHECK is deliberately kept: it is the last line of defence if the
    trigger that maintains ``spent_microusd`` were ever wrong, and it binds the
    owner too.
    """
    with pytest.raises(DBAPIError) as excinfo, owner_txn(owner_engine) as conn:
        conn.execute(
            text(INSERT_BUDGET), {"scope": "run:update", "cap": 1_000_000, "spent": 900_000}
        )
        conn.execute(
            text(
                "UPDATE budget_ledger SET spent_microusd = spent_microusd + 200000 "
                "WHERE scope = 'run:update'"
            )
        )
    assert_sqlstate(excinfo.value, CHECK_VIOLATION)
    assert "ck_budget_spent_within_cap" in str(excinfo.value.orig)


def test_budget_reserve_refuses_at_the_cap_instead_of_overspending(
    owner_engine: Engine, role_txn: RoleTxn
) -> None:
    """The designed reserve pattern degrades to a refusal, never to an overspend.

    Renamed from ``test_budget_reserve_statement_returns_no_row_at_the_cap``
    because the *mechanism* changed and the name named the mechanism. DESIGN's
    decision -- reserve worst-case, then settle -- is preserved and so is the
    single atomic statement; what changed is that the statement is now an INSERT
    into ``budget_reservations`` whose BEFORE trigger holds the ledger row lock,
    because the previous ``UPDATE budget_ledger`` interface required the capped
    party to hold a privilege it used to zero its own spend.

    "Zero rows" becomes "raise KS006". Both mean: halt the run. Neither can
    overspend.
    """
    scope = f"run:{TEST_TAG}-reserve"
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                INSERT_BUDGET + " ON CONFLICT (scope) DO UPDATE SET cap_microusd = 1000000, "
                "spent_microusd = 0"
            ),
            {"scope": scope, "cap": 1_000_000, "spent": 0},
        )
    try:
        reserve = text(
            "INSERT INTO budget_reservations (scope, idempotency_key, reserve_microusd) "
            "VALUES (:scope, :key, :reserve) RETURNING id"
        )
        with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
            granted = conn.execute(
                reserve, {"scope": scope, "key": f"{TEST_TAG}-r1", "reserve": 1_000_000}
            ).scalar_one()
            assert granted is not None  # control: the first reservation succeeds
            spent = conn.execute(
                text("SELECT spent_microusd FROM budget_ledger WHERE scope = :s"), {"s": scope}
            ).scalar_one()
            assert spent == 1_000_000, "the whole cap is now reserved"

            conn.execute(reserve, {"scope": scope, "key": f"{TEST_TAG}-r2", "reserve": 1})
        assert_sqlstate(excinfo.value, BUDGET_CAP_EXCEEDED)
    finally:
        with owner_engine.begin() as conn:
            conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": scope})
            conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": scope})


def test_conflict_fingerprint_is_unique(role_txn: RoleTxn) -> None:
    insert = text(
        "INSERT INTO conflicts (fingerprint, type, entity_refs, sources, disagreeing_fields, "
        "first_seen_run, last_seen_run) VALUES (:fp, 'field-disagreement', '[]'::jsonb, "
        "'[]'::jsonb, '[]'::jsonb, 'run-1', 'run-1')"
    )
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(insert, {"fp": "fp-unique-control"})  # control: first insert succeeds
        conn.execute(insert, {"fp": "fp-unique-control"})
    assert_sqlstate(excinfo.value, UNIQUE_VIOLATION)
    assert "uq_conflicts_fingerprint" in str(excinfo.value.orig)


def test_open_proposals_are_unique_per_fingerprint(
    role_txn: RoleTxn, owner_engine, seeded_rows: dict[str, object]
) -> None:
    """Re-proposing an unresolved conflict is impossible; re-proposing after a
    rejection is not blocked."""
    insert = text(
        "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence, "
        "created_run, status, target_canonical_id) "
        "VALUES (:cid, :fp, '{\"set\": {}}'::jsonb, 0.5, '{}'::jsonb, 'run-1', :status, "
        ":target)"
    )
    params = {
        "cid": seeded_rows["conflict_id"],
        "fp": "fp-dedup",
        "target": seeded_rows["canonical_id"],
    }

    # Control: a rejected row does not block re-proposing. Migration 0004 makes
    # a proposal born pending/sensitive_hold (SQLSTATE KS002), so `rejected` is
    # now reached the way the product reaches it -- a decision UPDATE -- rather
    # than by inserting a terminal status directly. The claim is unchanged and
    # the path exercised is strictly more real; only the owner can do both the
    # INSERT and the decision UPDATE inside one transaction.
    with owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(insert, {**params, "status": "pending"})
            conn.execute(
                text(
                    "UPDATE proposals SET status = 'rejected', decided_by = 'reviewer', "
                    "decided_at = now() WHERE fingerprint = :fp"
                ),
                {"fp": params["fp"]},
            )
            conn.execute(insert, {**params, "status": "pending"})
        finally:
            transaction.rollback()

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(insert, {**params, "status": "pending"})
        conn.execute(insert, {**params, "status": "sensitive_hold"})
    assert_sqlstate(excinfo.value, UNIQUE_VIOLATION)


@pytest.mark.parametrize(
    "status", ["pending", "approved", "rejected", "applied", "rolled_back", "sensitive_hold"]
)
def test_proposal_status_enum_accepts_every_design_value(
    owner_engine: Engine, seeded_rows: dict[str, object], status: str
) -> None:
    """Every DESIGN status is storable on a proposals row.

    The claim is unchanged; the path to it is. Migration 0004 requires a
    proposal to be *born* pending/sensitive_hold (SQLSTATE KS002), so the
    terminal values are reached by the decision UPDATE the apply path actually
    performs, against the committed pending proposal from `seeded_rows`.
    Asserting them via a direct INSERT would now be asserting a path the
    boundary forbids -- and would have been the only way this test could stay
    green while the born-pending rule was broken.

    RULING 1 / migration 0005 splits the decision from the apply, so no single
    *role* can reach all six values any more: the storability of the column is
    now asserted as the owner, and which role may make which transition is
    asserted, edge by edge, in ``test_three_role_boundary.py``. Running this as
    ``apply_writer`` would now assert the transition graph by accident and pass
    for the wrong reason.

    RULING 12 / migration 0006 binds the owner to that graph too, as defence in
    depth, so a single UPDATE straight to any value is no longer legal for any
    principal. The claim -- every DESIGN status is storable on a proposals row
    -- is unchanged and the assertion below is untouched; the row simply walks
    the legal path to each value, which is the path the product walks. A birth
    status is reached by being born in it.
    """
    if status in BIRTH_STATUSES:
        with owner_txn(owner_engine) as conn:
            stored = conn.execute(
                text(
                    "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, "
                    "evidence, created_run, target_canonical_id, status, sensitive) "
                    "VALUES (:cid, :fp, '{\"set\": {}}'::jsonb, 0.5, '{}'::jsonb, 'run-1', "
                    ":target, CAST(:status AS proposal_status), :sensitive) RETURNING status"
                ),
                {
                    "cid": seeded_rows["conflict_id"],
                    "fp": f"fp-storable-{status}",
                    "target": seeded_rows["canonical_id"],
                    "status": status,
                    "sensitive": status == "sensitive_hold",
                },
            ).scalar_one()
        assert stored == status
        return

    walk = {
        "approved": ["approved"],
        "rejected": ["rejected"],
        "applied": ["approved", "applied"],
        "rolled_back": ["approved", "applied", "rolled_back"],
    }[status]
    with owner_txn(owner_engine) as conn:
        for step in walk:
            signature = (
                ", decided_by = 'reviewer:alice', decided_at = now()"
                if step in {"approved", "rejected"}
                else ""
            )
            stored = conn.execute(
                text(
                    f"UPDATE proposals SET status = CAST(:status AS proposal_status){signature} "
                    "WHERE id = :pid RETURNING status"
                ),
                {"pid": seeded_rows["proposal_id"], "status": step},
            ).scalar_one()
    assert stored == status


def test_proposal_status_enum_rejects_an_unknown_value(
    role_txn: RoleTxn, seeded_rows: dict[str, object]
) -> None:
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(
                "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence, "
                "created_run, target_canonical_id, status) "
                "VALUES (:cid, 'fp-bogus', '{\"set\": {}}'::jsonb, 0.5, '{}'::jsonb, "
                "'run-1', :target, CAST('auto_applied' AS proposal_status))"
            ),
            {"cid": seeded_rows["conflict_id"], "target": seeded_rows["canonical_id"]},
        )
    assert_sqlstate(excinfo.value, INVALID_ENUM_INPUT)


def test_confidence_outside_zero_to_one_is_rejected(
    role_txn: RoleTxn, seeded_rows: dict[str, object]
) -> None:
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(
                "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence, "
                "created_run, target_canonical_id) "
                "VALUES (:cid, 'fp-conf', '{}'::jsonb, 1.5, '{}'::jsonb, 'run-1', :target)"
            ),
            {"cid": seeded_rows["conflict_id"], "target": seeded_rows["canonical_id"]},
        )
    assert_sqlstate(excinfo.value, CHECK_VIOLATION)


def test_api_client_hash_column_refuses_a_plaintext_key(role_txn: RoleTxn, owner_engine) -> None:
    """Storing a plaintext key is structurally impossible, not merely discouraged."""
    with owner_engine.connect() as conn:  # control: a real hash is accepted
        transaction = conn.begin()
        conn.execute(
            text(
                "INSERT INTO api_clients (key_hash, scope, label) "
                "VALUES (repeat('a', 64), 'client', 'control')"
            )
        )
        transaction.rollback()

    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(
                    "INSERT INTO api_clients (key_hash, scope, label) "
                    "VALUES ('keystone-demo-admin-plaintext', 'admin', 'oops')"
                )
            )
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, CHECK_VIOLATION)
    assert "ck_api_clients_hash_is_sha256_hex" in str(excinfo.value.orig)


def test_money_columns_are_integer_never_float(owner_engine) -> None:
    """`Money is integer microusd; never float` -- asserted against the catalog."""
    with owner_engine.connect() as conn:
        floats = conn.execute(
            text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' "
                "AND data_type IN ('real', 'double precision')"
            )
        ).all()
        money = dict(
            conn.execute(
                text(
                    "SELECT table_name || '.' || column_name, data_type "
                    "FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND (column_name LIKE '%%microusd' OR column_name LIKE '%%_cents')"
                )
            ).all()
        )
    assert floats == [], f"floating-point columns found: {floats}"
    assert money, "expected money columns to exist"
    assert set(money.values()) == {"bigint"}, money
