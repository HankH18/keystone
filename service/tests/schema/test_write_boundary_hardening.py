"""The bypasses an adversarial review walked through, closed and proven closed.

Every test in this module was written against a **demonstrated** hole in the
0001-0003 boundary, not a hypothetical one. The shape is uniform and
deliberate:

* the negative asserts an **exact SQLSTATE** -- ``42501`` for a privilege, or
  one of the project codes ``KS001``/``KS002``/``KS003``, none of which any
  unrelated failure can produce;
* the positive control performs, over the same connection path and as the same
  role, the legitimate write the fix must not have broken. A negative that
  passed because the connection was dead would fail its control.

Migration ``0004_harden_write_boundary`` is the implementation; its module
docstring records what each fix is for.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from recon.db import ROLE_APPLY_WRITER, ROLE_RECON_WRITER, ROLE_REVIEW_WRITER
from tests.schema.conftest import (
    TEST_TAG,
    RoleTxn,
    assert_insufficient_privilege,
    assert_sqlstate,
)

#: Project SQLSTATEs, pinned here so a test cannot pass on a built-in error.
CANONICAL_WRITE_UNAUTHORISED = "KS001"
PROPOSAL_NOT_BORN_PENDING = "KS002"
AUDIT_ACTOR_NOT_MACHINE_SCOPED = "KS003"
#: Migration 0008, RULING 15: an ``applied``/``rolled_back`` ledger row that
#: describes a canonical write this transaction did not perform.
EVENT_DESCRIBES_NO_WRITE = "KS011"

INSUFFICIENT_PRIVILEGE = "42501"

# ===========================================================================
# BLOCKER 1 -- a proposal must be born pending
# ===========================================================================
INSERT_PROPOSAL = (
    "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence, "
    "created_run, status, decided_by, decided_at, target_canonical_id) "
    "VALUES (:cid, :fp, '{\"set\": {}}'::jsonb, 0.9, '{}'::jsonb, 'run-1', "
    "CAST(:status AS proposal_status), :decided_by, :decided_at, "
    "'00000000-0000-0000-0000-0000000000ff'::uuid)"
)


@pytest.mark.parametrize("status", ["approved", "applied", "rejected", "rolled_back"])
def test_recon_writer_cannot_insert_a_pre_decided_proposal(
    role_txn: RoleTxn, seeded_rows: dict[str, object], status: str
) -> None:
    """The hole: no UPDATE on proposals, but INSERT was unconstrained.

    ``recon_writer`` could simply *create* a proposal already ``approved`` or
    ``applied`` and hand itself pre-approved work. The old negative test only
    covered UPDATE, so this passed silently.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(INSERT_PROPOSAL),
            {
                "cid": seeded_rows["conflict_id"],
                "fp": f"fp-born-{status}",
                "status": status,
                "decided_by": None,
                "decided_at": None,
            },
        )
    assert_sqlstate(excinfo.value, PROPOSAL_NOT_BORN_PENDING)


@pytest.mark.parametrize(
    ("decided_by", "decided_at"),
    [
        pytest.param("reviewer@keystone", None, id="decided_by"),
        pytest.param(None, "2026-08-22T00:00:00+00:00", id="decided_at"),
        pytest.param("reviewer@keystone", "2026-08-22T00:00:00+00:00", id="both"),
    ],
)
def test_a_proposal_cannot_be_born_already_decided(
    role_txn: RoleTxn,
    seeded_rows: dict[str, object],
    decided_by: str | None,
    decided_at: str | None,
) -> None:
    """A ``pending`` row that already names a decider is a forged decision."""
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(INSERT_PROPOSAL),
            {
                "cid": seeded_rows["conflict_id"],
                "fp": f"fp-born-decided-{decided_by}-{decided_at}",
                "status": "pending",
                "decided_by": decided_by,
                "decided_at": decided_at,
            },
        )
    assert_sqlstate(excinfo.value, PROPOSAL_NOT_BORN_PENDING)


def test_the_born_pending_rule_binds_the_schema_owner_too(
    owner_engine: Engine, seeded_rows: dict[str, object]
) -> None:
    """ "For every role" includes the role that bypasses every grant.

    Grants are a per-role privilege; this rule is an invariant of the table. If
    it were a grant it would evaporate for the owner -- and locally, the owner
    is what a careless script connects as.
    """
    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(INSERT_PROPOSAL),
                {
                    "cid": seeded_rows["conflict_id"],
                    "fp": "fp-born-owner",
                    "status": "applied",
                    "decided_by": None,
                    "decided_at": None,
                },
            )
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, PROPOSAL_NOT_BORN_PENDING)


@pytest.mark.parametrize("status", ["pending", "sensitive_hold"])
def test_a_proposal_may_be_born_in_a_hold_state(
    role_txn: RoleTxn, seeded_rows: dict[str, object], status: str
) -> None:
    """Positive control: the two states DESIGN says a proposal lands in."""
    with role_txn(ROLE_RECON_WRITER) as conn:
        stored = conn.execute(
            text(INSERT_PROPOSAL + " RETURNING status"),
            {
                "cid": seeded_rows["conflict_id"],
                "fp": f"fp-born-ok-{status}",
                "status": status,
                "decided_by": None,
                "decided_at": None,
            },
        ).scalar_one()
    assert stored == status


def test_a_terminal_status_is_still_reachable_by_decision(
    owner_engine: Engine, seeded_rows: dict[str, object]
) -> None:
    """The rule constrains *birth*, not the lifecycle.

    Without this control, "a proposal must be born pending" could have been
    implemented as a CHECK constraint that also made ``applied`` unreachable --
    which would break the apply path rather than guard it.

    The claim is unchanged; the path to it is. Migration 0006 (RULING 12) binds
    the schema owner to the same transition graph as the three roles, so
    ``pending -> applied`` in one statement naming an arbitrary human decider is
    no longer a legal move for *anyone* -- it was the last principal for which
    it was, and the inconsistency was the finding. The terminal status is
    reached here by walking the graph the product walks: decide, then apply. If
    the born-pending rule were ever reimplemented as a CHECK, this control still
    goes red.
    """
    with owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(INSERT_PROPOSAL),
                {
                    "cid": seeded_rows["conflict_id"],
                    "fp": "fp-born-then-decided",
                    "status": "pending",
                    "decided_by": None,
                    "decided_at": None,
                },
            )
            decided = conn.execute(
                text(
                    "UPDATE proposals SET status = 'approved', decided_by = 'reviewer:alice', "
                    "decided_at = now() WHERE fingerprint = 'fp-born-then-decided' "
                    "RETURNING status"
                )
            ).scalar_one()
            stored = conn.execute(
                text(
                    "UPDATE proposals SET status = 'applied' "
                    "WHERE fingerprint = 'fp-born-then-decided' RETURNING status"
                )
            ).scalar_one()
        finally:
            transaction.rollback()
    assert decided == "approved"
    assert stored == "applied"


# ===========================================================================
# BLOCKER 2 -- the capped party may not raise its own cap
# ===========================================================================
#: Ledger rows are provisioned by migration/config -- never by the capped
#: party -- so the fixtures that need one connect as the owner.
SEED_BUDGET = (
    "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) "
    "VALUES (:scope, 1000000, 250000) ON CONFLICT (scope) DO UPDATE "
    "SET cap_microusd = EXCLUDED.cap_microusd, spent_microusd = EXCLUDED.spent_microusd"
)


@pytest.fixture
def owner_budget_scope(owner_engine: Engine) -> Iterator[str]:
    """A committed ledger row for one scope, provisioned by the ops principal."""
    scope = f"run:{TEST_TAG}-cap"
    with owner_engine.begin() as conn:
        conn.execute(text(SEED_BUDGET), {"scope": scope})
    yield scope
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": scope})
        conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": scope})


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(
            "UPDATE budget_ledger SET cap_microusd = 999999999 WHERE scope = :scope",
            id="raise-the-cap",
        ),
        pytest.param(
            "UPDATE budget_ledger SET cap_microusd = 999999999, spent_microusd = 0 "
            "WHERE scope = :scope",
            id="raise-the-cap-and-zero-the-spend",
        ),
        pytest.param(
            "UPDATE budget_ledger SET scope = 'run:renamed' WHERE scope = :scope",
            id="rename-the-scope",
        ),
        pytest.param(
            # RULING 2: the attack that broke the 0004 cap. `spent_microusd`
            # was in recon_writer's column grant, so the capped party simply
            # zeroed a fully consumed budget. There is now no writable spend
            # column at all.
            "UPDATE budget_ledger SET spent_microusd = 0 WHERE scope = :scope",
            id="zero-the-spend",
        ),
        pytest.param(
            # RULING 2: and it cannot conjure a fresh scope with its own cap.
            "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) "
            "VALUES ('run:my-own-huge-cap', 999999999, 0)",
            id="invent-a-scope-with-its-own-cap",
        ),
    ],
)
def test_recon_writer_cannot_rewrite_the_budget_cap(
    role_txn: RoleTxn, owner_budget_scope: str, statement: str
) -> None:
    """The hole: table-level UPDATE let the capped party edit its own cap, and
    0004's column grant still let it zero its own spend.

    "The process being capped may raise the cap" is not a cap, and neither is
    "may erase what it spent". Migration 0005 revokes **all** INSERT and UPDATE
    on ``budget_ledger`` from ``recon_writer``: the spend column is maintained
    only by the ``budget_reservations`` triggers, so this is not blocked by a
    rule -- there is nothing left to write.
    """
    with role_txn(ROLE_RECON_WRITER) as conn:  # control: it can still read the ledger
        conn.execute(
            text("SELECT cap_microusd FROM budget_ledger WHERE scope = :scope"),
            {"scope": owner_budget_scope},
        ).scalar_one()

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text(statement), {"scope": owner_budget_scope})
    assert_insufficient_privilege(excinfo.value)


def test_recon_writer_may_still_reserve_spend_against_the_cap(
    role_txn: RoleTxn, owner_budget_scope: str
) -> None:
    """Positive control: reserve-then-settle still works, by its new mechanism.

    DESIGN pins *reserve worst-case then settle* as the decision; the
    single-statement ``UPDATE budget_ledger`` was an interface detail, and it
    was the thing the red team walked through. The decision is preserved: ONE
    atomic statement still reserves, and the ledger still moves -- it just moves
    under a trigger the capped party cannot reach.

    If the grants had been scoped wrongly the boundary would be "secure" and
    the product broken, which is exactly what this control exists to catch.
    """
    with role_txn(ROLE_RECON_WRITER) as conn:
        reservation = conn.execute(
            text(
                "INSERT INTO budget_reservations (scope, idempotency_key, reserve_microusd) "
                "VALUES (:scope, :key, :reserve) RETURNING id, state"
            ),
            {"scope": owner_budget_scope, "key": f"{TEST_TAG}-control", "reserve": 100_000},
        ).one()
        assert reservation.state == "open"

        spent = conn.execute(
            text("SELECT spent_microusd FROM budget_ledger WHERE scope = :scope"),
            {"scope": owner_budget_scope},
        ).scalar_one()
        assert spent == 350_000, "the reserve must have moved the ledger"

        settled = conn.execute(
            text(
                "UPDATE budget_reservations SET actual_microusd = :actual, state = 'settled' "
                "WHERE id = :rid RETURNING state"
            ),
            {"rid": reservation.id, "actual": 40_000},
        ).scalar_one()
        assert settled == "settled"

        released = conn.execute(
            text("SELECT spent_microusd FROM budget_ledger WHERE scope = :scope"),
            {"scope": owner_budget_scope},
        ).scalar_one()
    assert released == 290_000, "settling must release reserve - actual"


def test_the_budget_ledger_is_not_writable_by_the_capped_party(owner_engine: Engine) -> None:
    """The catalog view of the same fact, so a re-widened grant is caught even
    if a future statement never happens to exercise it.

    This assertion used to read ``columns == {"spent_microusd", "updated_at"}``
    and pass -- and that grant was the bypass. It is replaced by the strictly
    stronger form: ``recon_writer`` holds **no** INSERT or UPDATE privilege on
    any column of ``budget_ledger``.
    """
    with owner_engine.connect() as conn:
        columns = set(
            conn.execute(
                text(
                    "SELECT privilege_type || ':' || column_name "
                    "FROM information_schema.column_privileges "
                    "WHERE grantee = :role AND table_name = 'budget_ledger' "
                    "AND privilege_type IN ('INSERT', 'UPDATE')"
                ),
                {"role": ROLE_RECON_WRITER},
            )
            .scalars()
            .all()
        )
    assert columns == set(), columns


# ===========================================================================
# BLOCKER 3 -- txid is DEFAULT-only, so it cannot be pre-dated
# ===========================================================================
@pytest.mark.parametrize(
    ("column", "value"),
    [
        pytest.param("txid", "9223372036854775807", id="txid"),
        pytest.param("id", "1", id="id"),
        pytest.param("ts", "'2000-01-01T00:00:00+00:00'", id="ts"),
    ],
)
def test_apply_writer_cannot_supply_trigger_relevant_proposal_event_columns(
    role_txn: RoleTxn, seeded_rows: dict[str, object], column: str, value: str
) -> None:
    """The hole: ``txid`` was an ordinary writable bigint with only a DEFAULT.

    With table-level INSERT, ``apply_writer`` could seed reversal rows stamped
    with *future* transaction ids and then commit arbitrary canonical UPDATEs
    in later transactions, satisfying the trigger with records it wrote in
    advance and leaving no true reversal path. INSERT is now column-scoped and
    excludes ``txid`` entirely, so ``pg_current_xact_id()`` is the only thing
    that ever populates it. ``id`` and ``ts`` are excluded for the same reason:
    a ledger whose identity and clock the writer chooses is not a ledger.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(
                f"INSERT INTO proposal_events (proposal_id, event, actor, {column}) "
                f"VALUES (:pid, 'applied', 'system:apply', {value})"
            ),
            {"pid": seeded_rows["proposal_id"]},
        )
    assert_insufficient_privilege(excinfo.value)


def test_proposal_event_txid_is_stamped_with_the_writing_transaction(
    role_txn: RoleTxn, seeded_rows: dict[str, object]
) -> None:
    """Positive control, and the reason excluding the column is sufficient.

    The apply path never names ``txid``; the DEFAULT does, and it can only ever
    name the transaction actually doing the writing.
    """
    with role_txn(ROLE_APPLY_WRITER) as conn:
        stamped, current = conn.execute(
            text(
                "INSERT INTO proposal_events (proposal_id, event, before, after, actor) "
                "VALUES (:pid, 'applied', '{}'::jsonb, '{}'::jsonb, 'system:apply') "
                "RETURNING txid, pg_current_xact_id()::text::bigint"
            ),
            {"pid": seeded_rows["proposal_id"]},
        ).one()
    assert stamped == current, "txid must be the transaction that wrote the row"


def test_proposal_event_insert_grant_excludes_txid(owner_engine: Engine) -> None:
    """Catalog assertion: no INSERT privilege on ``txid`` for any writer role."""
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT grantee, column_name FROM information_schema.column_privileges "
                "WHERE table_name = 'proposal_events' AND privilege_type = 'INSERT' "
                "AND grantee = ANY(:roles)"
            ),
            {"roles": [ROLE_RECON_WRITER, ROLE_APPLY_WRITER]},
        ).all()
    granted = {(grantee, column) for grantee, column in rows}
    assert not {pair for pair in granted if pair[1] in {"txid", "id", "ts"}}, granted
    assert (ROLE_APPLY_WRITER, "canonical_id") in granted, granted


# ===========================================================================
# MAJOR 5 -- the reversal record must name the row it authorises
# ===========================================================================
INSERT_EVENT = (
    "INSERT INTO proposal_events (proposal_id, canonical_id, event, before, after, actor) "
    "VALUES (:pid, :canonical_id, :event, CAST(:before AS jsonb), "
    "'{\"grade\": \"tampered\"}'::jsonb, 'system:apply')"
)

REWRITE = (
    'UPDATE entities SET current = \'{"grade": "tampered"}\'::jsonb, updated_at = now() '
    "WHERE canonical_id = ANY(:ids)"
)


def test_a_correlated_reversal_record_authorises_the_canonical_update(
    role_txn: RoleTxn, canonical_pair: dict[str, object]
) -> None:
    """Positive control for the whole correlation rule.

    One entity, one reversal record naming that entity, carrying the apply
    event and the exact pre- and post-update values, citing the *approved*
    proposal that targets that entity. This is what the apply function does.
    """
    with role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": canonical_pair["proposal_a"],
                "canonical_id": canonical_pair["a"],
                "event": "applied",
                "before": canonical_pair["current_a"],
            },
        )
        result = conn.execute(text(REWRITE), {"ids": [canonical_pair["a"]]})
        assert result.rowcount == 1
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_one_decoy_event_cannot_authorise_a_mass_canonical_rewrite(
    role_txn: RoleTxn, canonical_pair: dict[str, object]
) -> None:
    """THE attack the old trigger permitted, executed literally.

    The 0001 trigger asserted only that *some* ``proposal_events`` row carried
    the current txid. So one legitimate reversal record for entity A authorised
    an unbounded rewrite of every other canonical row in the same transaction,
    each with no record of what it overwrote and therefore no way back.

    Here the record for A is genuine and the sweep takes B along with it. The
    correlated trigger must reject the transaction because *B* has no record --
    not because anything was wrong with A's.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": canonical_pair["proposal_a"],
                "canonical_id": canonical_pair["a"],
                "event": "applied",
                "before": canonical_pair["current_a"],
            },
        )
        result = conn.execute(text(REWRITE), {"ids": [canonical_pair["a"], canonical_pair["b"]]})
        assert result.rowcount == 2, "the sweep must really have touched both rows"
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED)


@pytest.mark.parametrize(
    ("event", "before_key", "why"),
    [
        pytest.param(
            "applied", "current_b", "wrong-before-value", id="before-is-not-the-old-value"
        ),
        pytest.param("noted", "current_a", "wrong-event", id="event-is-not-an-apply-event"),
    ],
)
def test_a_reversal_record_that_fails_any_correlation_clause_is_rejected(
    role_txn: RoleTxn,
    canonical_pair: dict[str, object],
    event: str,
    before_key: str,
    why: str,
) -> None:
    """Each clause of the correlation is load-bearing, proved one at a time.

    ``before`` must equal the *pre-update* value: a record that stores anything
    else cannot restore the row, which is what makes the rollback path real
    rather than decorative. ``event`` must be a canonical-mutating event, so an
    unrelated note cannot double as an authorisation.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": canonical_pair["proposal_a"],
                "canonical_id": canonical_pair["a"],
                "event": event,
                "before": canonical_pair[before_key],
            },
        )
        conn.execute(text(REWRITE), {"ids": [canonical_pair["a"]]})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED), why


def test_a_reversal_record_for_a_different_entity_is_rejected(
    role_txn: RoleTxn, canonical_pair: dict[str, object], owner_engine: Engine
) -> None:
    """``canonical_id`` must be the row actually updated, not any row.

    The transaction is unchanged; the SQLSTATE it now raises is not, and the
    reason is recorded here rather than implied. Migration 0008 (RULING 15) adds
    an earlier and broader rule on the other table: an ``applied`` event must
    describe a canonical write this transaction actually performed. This
    transaction's event names entity B, and B is never written -- which is
    exactly the forgery shape that rule exists to refuse -- so ``KS011`` fires
    at ``SET CONSTRAINTS`` before the entities trigger is reached.

    The assertion is therefore re-pointed at the rule that now refuses first,
    not loosened: ``KS011`` is a project SQLSTATE produced by exactly one
    trigger, so it is as exact as the ``KS001`` it replaces, and the write is
    still proved to have been refused *and* to have moved nothing. The
    ``pe.canonical_id = NEW.canonical_id`` clause of the entities trigger stays
    proved on its own by ``test_one_decoy_event_cannot_authorise_a_mass_canonical
    _rewrite`` above, which is the same clause seen from the other side -- an
    updated row with no record naming it -- and which 0008 leaves untouched.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": canonical_pair["proposal_b"],
                "canonical_id": canonical_pair["b"],
                "event": "applied",
                "before": canonical_pair["current_a"],
            },
        )
        conn.execute(text(REWRITE), {"ids": [canonical_pair["a"]]})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, EVENT_DESCRIBES_NO_WRITE)

    with owner_engine.connect() as conn:
        for key, expected in (("a", "current_a"), ("b", "current_b")):
            held = conn.execute(
                text("SELECT current::text FROM entities WHERE canonical_id = :c"),
                {"c": canonical_pair[key]},
            ).scalar_one()
            assert held == canonical_pair[expected], (
                f"the refused transaction must have moved nothing: entity {key} changed"
            )


def test_a_reversal_record_with_no_before_value_is_rejected(
    role_txn: RoleTxn, canonical_pair: dict[str, object]
) -> None:
    """A NULL ``before`` records nothing, so it authorises nothing.

    This is why the trigger compares with ``=`` rather than
    ``IS NOT DISTINCT FROM``: the permissive form would accept an empty record.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": canonical_pair["proposal_a"],
                "canonical_id": canonical_pair["a"],
                "event": "applied",
                "before": None,
            },
        )
        conn.execute(text(REWRITE), {"ids": [canonical_pair["a"]]})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED)


def test_every_row_of_a_multi_row_update_needs_its_own_record(
    role_txn: RoleTxn, canonical_pair: dict[str, object]
) -> None:
    """Positive control for the mass-rewrite negative: two rows, two records.

    Without this, "reject any multi-row UPDATE" would pass the attack test
    while breaking legitimate batched applies. The rule is per-row correlation,
    not a row-count limit.

    RULING 3 sharpens it: each record must also cite an approved proposal whose
    ``target_canonical_id`` is that row, so a legitimate batched apply of two
    entities is two decisions, not one decision applied twice.
    """
    with role_txn(ROLE_APPLY_WRITER) as conn:
        for key, current, proposal in (
            ("a", "current_a", "proposal_a"),
            ("b", "current_b", "proposal_b"),
        ):
            conn.execute(
                text(INSERT_EVENT),
                {
                    "pid": canonical_pair[proposal],
                    "canonical_id": canonical_pair[key],
                    "event": "applied",
                    "before": canonical_pair[current],
                },
            )
        result = conn.execute(text(REWRITE), {"ids": [canonical_pair["a"], canonical_pair["b"]]})
        assert result.rowcount == 2
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


# ===========================================================================
# MAJOR 6 -- the pipeline may APPEND canonical rows, only apply may MUTATE
# ===========================================================================
def test_recon_writer_may_insert_a_canonical_entity(role_txn: RoleTxn) -> None:
    """Canonical CREATION is deterministic pipeline output (entity resolution).

    DESIGN says ``recon_writer`` has "no UPDATE/DELETE/INSERT on canonical or
    landing tables", which is self-contradictory: ingestion must append to
    ``raw_records``. The defensible reading, pinned in migration 0004 and to be
    recorded in ARCHITECTURE.md, is **the pipeline may APPEND, only the guarded
    path may MUTATE**. So entity resolution materialises the canonical row and
    can never touch it again.
    """
    with role_txn(ROLE_RECON_WRITER) as conn:
        result = conn.execute(
            text(
                "INSERT INTO entities (canonical_id, entity_type, current) "
                "VALUES (:cid, 'person', '{}'::jsonb)"
            ),
            {"cid": uuid.uuid5(uuid.NAMESPACE_URL, "keystone/tests/schema/er-materialised")},
        )
        assert result.rowcount == 1


def test_apply_writer_cannot_insert_a_canonical_entity(
    role_txn: RoleTxn, seeded_rows: dict[str, object]
) -> None:
    """Canonical MUTATION is the guarded path -- and only mutation.

    The hole: the correlation trigger is AFTER UPDATE only, so an ``INSERT``
    slipped past it entirely. ``apply_writer`` could fabricate brand-new
    canonical rows with no proposal, no reversal record and no rollback path,
    which is precisely the "auto-apply writes canonical state out of nowhere"
    outcome the whole boundary exists to prevent. Withdrawing INSERT means a
    proposal can only ever CHANGE canonical state, never invent it.
    """
    with role_txn(ROLE_APPLY_WRITER) as conn:  # control: UPDATE is still its job
        conn.execute(text("SELECT count(*) FROM entities")).scalar_one()

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(
                "INSERT INTO entities (canonical_id, entity_type, current) "
                "VALUES (gen_random_uuid(), 'person', '{}'::jsonb)"
            )
        )
    assert_insufficient_privilege(excinfo.value)


def test_recon_writer_cannot_turn_its_insert_into_an_update(role_txn: RoleTxn) -> None:
    """The obvious way to launder an INSERT grant into a MUTATE grant.

    ``INSERT ... ON CONFLICT DO UPDATE`` is an UPDATE wearing an INSERT's
    syntax, and it is exactly what a pipeline "just re-materialising" a
    canonical row would reach for. Postgres requires UPDATE privilege on the
    conflict target's columns, which recon_writer does not have -- so append is
    genuinely append, and re-running entity resolution over an existing
    canonical row cannot quietly rewrite it.
    """
    with role_txn(ROLE_RECON_WRITER) as conn:  # control: a plain append works
        conn.execute(
            text(
                "INSERT INTO entities (canonical_id, entity_type, current) "
                "VALUES (gen_random_uuid(), 'person', '{}'::jsonb)"
            )
        )

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(
                "INSERT INTO entities (canonical_id, entity_type, current) "
                "VALUES (:cid, 'person', '{\"grade\": \"9\"}'::jsonb) "
                "ON CONFLICT (canonical_id) DO UPDATE SET current = EXCLUDED.current"
            ),
            {"cid": uuid.uuid5(uuid.NAMESPACE_URL, "keystone/tests/schema/upsert")},
        )
    assert_insufficient_privilege(excinfo.value)


def test_apply_writer_cannot_name_canonical_id_in_an_update(
    role_txn: RoleTxn, canonical_pair: dict[str, object]
) -> None:
    """RULING 3: identity is not in the grant, so it is refused before the trigger.

    0005 column-scopes ``apply_writer``'s UPDATE on ``entities`` to
    ``(current, updated_at)``. Naming ``canonical_id`` -- or ``entity_type``, or
    ``created_at`` -- is now a privilege error at parse time, one layer *earlier*
    than the trigger. The trigger's own immutability clause is unchanged and is
    still proved directly, against a principal that holds the privilege, in
    ``test_the_trigger_pins_canonical_id_immutable_for_every_principal``.
    """
    new_id = uuid.uuid5(uuid.NAMESPACE_URL, "keystone/tests/schema/rehomed")
    with role_txn(ROLE_APPLY_WRITER) as conn:  # control: `current` IS in the grant
        conn.execute(
            text("UPDATE entities SET current = current WHERE canonical_id = :cid"),
            {"cid": canonical_pair["a"]},
        )

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text("UPDATE entities SET canonical_id = :new_id WHERE canonical_id = :old_id"),
            {"new_id": new_id, "old_id": canonical_pair["a"]},
        )
    assert_insufficient_privilege(excinfo.value)


def test_the_trigger_pins_canonical_id_immutable_for_every_principal(
    owner_engine: Engine, canonical_pair: dict[str, object]
) -> None:
    """Identity is not state, so the reversal record cannot restore it.

    ``proposal_events.before`` captures ``current``, not ``canonical_id``. A row
    that changed identity therefore has no record able to put it back, which is
    the same defect as a missing ``before`` -- so the trigger pins
    ``canonical_id`` immutable. It also has no legitimate reason to change: it
    is a deterministic uuid5 of the row's sorted source refs.

    Exercised as the **owner**, the one principal whose column grants cannot
    stop it: this proves the rule is an invariant of the table and not merely a
    side effect of ``apply_writer``'s narrowed grant.
    """
    new_id = uuid.uuid5(uuid.NAMESPACE_URL, "keystone/tests/schema/rehomed")
    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(INSERT_EVENT),
                {
                    "pid": canonical_pair["proposal_a"],
                    "canonical_id": new_id,
                    "event": "applied",
                    "before": canonical_pair["current_a"],
                },
            )
            conn.execute(
                text(
                    "UPDATE entities SET canonical_id = :new_id, "
                    'current = \'{"grade": "tampered"}\'::jsonb WHERE canonical_id = :old_id'
                ),
                {"new_id": new_id, "old_id": canonical_pair["a"]},
            )
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED)


def test_the_canonical_role_split_is_exactly_as_pinned(owner_engine: Engine) -> None:
    """Append vs mutate vs read-only, read straight out of the catalog.

    Neither writer role may DELETE a canonical row: reversal is an UPDATE back
    to the recorded ``before`` value, and a deleted row has nothing to reverse
    to. ``review_writer`` may not write it at any verb.

    Read from ``information_schema.column_privileges``, not
    ``role_table_grants``: the latter lists only *table-level* privileges, so
    once ``apply_writer``'s UPDATE is column-scoped to ``(current, updated_at)``
    it vanishes from that view entirely -- which would silently turn this
    assertion into "apply_writer has no UPDATE", i.e. a green that proves the
    opposite of what it claims. That view is simply the wrong catalog for a
    column grant.
    """
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT grantee, privilege_type, column_name "
                "FROM information_schema.column_privileges "
                "WHERE table_name = 'entities' AND grantee = ANY(:roles)"
            ),
            {"roles": [ROLE_RECON_WRITER, ROLE_REVIEW_WRITER, ROLE_APPLY_WRITER]},
        ).all()
    by_role: dict[tuple[str, str], set[str]] = {}
    for grantee, privilege, column in rows:
        by_role.setdefault((grantee, privilege), set()).add(column)

    every_column = {"canonical_id", "entity_type", "current", "created_at", "updated_at"}
    assert by_role == {
        (ROLE_RECON_WRITER, "SELECT"): every_column,
        (ROLE_RECON_WRITER, "INSERT"): every_column,
        (ROLE_REVIEW_WRITER, "SELECT"): every_column,
        (ROLE_APPLY_WRITER, "SELECT"): every_column,
        # RULING 3: entity_type and created_at are NOT here. An apply that could
        # rewrite them while the reversal record captured only `current` was
        # provably unrestorable.
        (ROLE_APPLY_WRITER, "UPDATE"): {"current", "updated_at"},
    }, by_role


# ===========================================================================
# MINOR 9 -- the automation may not impersonate a reviewer
# ===========================================================================
INSERT_AUDIT = "INSERT INTO audit_log (actor, action, subject) VALUES (:actor, 'decide', :subject)"


@pytest.mark.parametrize(
    "actor",
    [
        pytest.param("reviewer@keystone", id="an-email"),
        pytest.param("alice", id="a-bare-name"),
        pytest.param("recon", id="the-old-default"),
        pytest.param("System:recon", id="wrong-case"),
        pytest.param(" system:recon", id="leading-space-defeats-the-anchor"),
    ],
)
def test_recon_writer_cannot_forge_a_human_actor_in_the_audit_log(
    role_txn: RoleTxn, actor: str
) -> None:
    """The audit trail is the record of who decided; forging it hides the forgery.

    ``recon_writer`` had plain INSERT on ``audit_log``, so the automation could
    attribute its own action to a named reviewer. The anchored ``^system:``
    match is enforced by a trigger keyed on ``current_user``, so it cannot be
    sidestepped by leading whitespace or a case change.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text(INSERT_AUDIT), {"actor": actor, "subject": TEST_TAG})
    assert_sqlstate(excinfo.value, AUDIT_ACTOR_NOT_MACHINE_SCOPED)


@pytest.mark.parametrize("actor", ["system:recon", "system:reconciler/run-1"])
def test_recon_writer_may_write_a_machine_scoped_audit_row(role_txn: RoleTxn, actor: str) -> None:
    """Positive control: the detection path still audits its own work."""
    with role_txn(ROLE_RECON_WRITER) as conn:
        stored = conn.execute(
            text(INSERT_AUDIT + " RETURNING actor"), {"actor": actor, "subject": TEST_TAG}
        ).scalar_one()
    assert stored == actor


def test_the_actor_rule_is_scoped_to_the_deciding_role(role_txn: RoleTxn) -> None:
    """Control on the rule's *scope*: reviewer actions are legitimate audit rows.

    They just do not come from a machine role. A blanket ``^system:``
    requirement would have made the human review trail unwritable, so this
    proves the trigger keys on ``current_user`` rather than on the column.

    This control used to run as ``apply_writer`` and pass -- which was the hole
    RULING 5 names: 0004 checked ``recon_writer`` only, so the *applying*
    machine could still sign an audit row "reviewer:alice". Under 0005 the
    reviewer-shaped actor is legitimate for exactly one role, and that role
    holds no canonical or proposal-creating privilege at all.
    """
    with role_txn(ROLE_REVIEW_WRITER) as conn:
        stored = conn.execute(
            text(INSERT_AUDIT + " RETURNING actor"),
            {"actor": "reviewer:alice", "subject": TEST_TAG},
        ).scalar_one()
    assert stored == "reviewer:alice"


# ===========================================================================
# MINOR 10 -- re-detection advances a conflict, it does not redefine one
# ===========================================================================
SEED_CONFLICT = (
    "INSERT INTO conflicts (fingerprint, type, entity_refs, sources, disagreeing_fields, "
    "first_seen_run, last_seen_run) VALUES (:fp, 'field-disagreement', "
    "'[\"crm:contact:1\"]'::jsonb, '[\"crm\"]'::jsonb, '[\"grade\"]'::jsonb, 'run-1', 'run-1')"
)


@pytest.mark.parametrize(
    "assignment",
    [
        pytest.param("fingerprint = 'fp-forged'", id="fingerprint"),
        pytest.param("type = 'not-a-conflict'", id="type"),
        pytest.param("entity_refs = '[]'::jsonb", id="entity_refs"),
        pytest.param("oscillating = true", id="oscillating"),
        pytest.param("first_seen_run = 'run-0'", id="first_seen_run"),
    ],
)
def test_recon_writer_cannot_rewrite_a_conflicts_identity(
    role_txn: RoleTxn, assignment: str
) -> None:
    """``fingerprint`` is the idempotency key; ``type``/``entity_refs`` are what
    the conflict *is*. Table-level UPDATE let the detector redefine a conflict
    after the fact -- including making a detected conflict fingerprint as a
    different, already-resolved one."""
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text(SEED_CONFLICT), {"fp": "fp-conflict-attack"})  # control: INSERT allowed
        conn.execute(
            text(f"UPDATE conflicts SET {assignment} WHERE fingerprint = 'fp-conflict-attack'")
        )
    assert_insufficient_privilege(excinfo.value)


def test_recon_writer_may_advance_a_conflict_through_re_detection(role_txn: RoleTxn) -> None:
    """Positive control: exactly what re-detection does to an existing conflict."""
    with role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text(SEED_CONFLICT), {"fp": "fp-conflict-control"})
        status, last_seen = conn.execute(
            text(
                "UPDATE conflicts SET status = 'resolved', last_seen_run = 'run-2' "
                "WHERE fingerprint = 'fp-conflict-control' RETURNING status, last_seen_run"
            )
        ).one()
    assert (status, last_seen) == ("resolved", "run-2")


def test_the_conflicts_update_grant_is_exactly_the_advancing_columns(
    owner_engine: Engine,
) -> None:
    with owner_engine.connect() as conn:
        columns = set(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.column_privileges "
                    "WHERE grantee = :role AND table_name = 'conflicts' "
                    "AND privilege_type = 'UPDATE'"
                ),
                {"role": ROLE_RECON_WRITER},
            )
            .scalars()
            .all()
        )
    assert columns == {"status", "last_seen_run"}, columns
