"""The spend cap, after it stopped being a cap twice.

Round two blocked ``cap_microusd`` and called it done. It was not a cap:
``recon_writer`` still held ``UPDATE(spent_microusd)`` and simply **zeroed a
fully consumed budget**, then kept spending. It could also INSERT a brand new
scope with a cap of its own choosing.

Monotonicity is not the fix either, and this is the trap worth naming: settling
provider-reported actuals against a worst-case reservation legitimately
*decreases* ``spent_microusd``, so "spend may only go up" would have broken the
DESIGN decision it was supposed to protect.

RULING 2's fix is structural. ``budget_ledger`` becomes read-only to the capped
party -- **no** INSERT, **no** UPDATE, no column left to write -- and spend moves
only under the triggers on ``budget_reservations``:

* RESERVE is one atomic ``INSERT``. Its BEFORE trigger takes the ledger row
  lock, checks ``spent + reserve <= cap``, and either increments spend or
  raises ``KS006``. A raise means: halt the run.
* SETTLE is ``open -> settled`` once, ``actual <= reserve``, releasing
  ``reserve - actual`` (``KS007`` otherwise).
* ``open -> reclaimed`` -- the TTL sweeper, which releases the whole reservation
  -- is refused to ``recon_writer``, because a capped party that can reclaim a
  reservation it actually consumed has re-invented "zero the spend".

The concurrency test at the bottom is the one that matters most: real
connections, real backends, a real burst. DESIGN warns that post-call
accounting loses the concurrent-burst race, and this proves the replacement
mechanism does not.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from recon.db import ROLE_RECON_WRITER, role_connection
from tests.schema.conftest import (
    ROLES,
    TEST_TAG,
    RoleTxn,
    assert_insufficient_privilege,
    assert_sqlstate,
    psycopg_dsn,
)

BUDGET_CAP_EXCEEDED = "KS006"
RESERVATION_LIFECYCLE = "KS007"

#: The SECURITY DEFINER ledger mutators. RULING 9 revokes EXECUTE on both from
#: PUBLIC and from every role; the reserve/settle triggers call them as the
#: owner, so the legitimate path never needed the grant it used to hold.
LEDGER_MUTATORS = (
    "keystone_budget_charge(text, bigint)",
    "keystone_budget_release(text, bigint)",
)

RESERVE = (
    "INSERT INTO budget_reservations (scope, idempotency_key, reserve_microusd) "
    "VALUES (:scope, :key, :reserve) RETURNING id"
)
SETTLE = (
    "UPDATE budget_reservations SET actual_microusd = :actual, state = 'settled' "
    "WHERE id = :rid RETURNING state"
)
SPENT = "SELECT spent_microusd FROM budget_ledger WHERE scope = :scope"


@pytest.fixture
def ledger_scope(owner_engine: Engine) -> Iterator[str]:
    """A committed ledger row with a 1_000_000 microusd cap, provisioned by ops.

    Provisioned by the **owner**, because the capped party holds no INSERT on
    ``budget_ledger`` at all -- which is the point, and is asserted directly in
    ``test_the_capped_party_cannot_provision_its_own_scope``.
    """
    scope = f"run:{TEST_TAG}-{uuid.uuid4()}"
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) "
                "VALUES (:scope, 1000000, 0)"
            ),
            {"scope": scope},
        )
    yield scope
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": scope})
        conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": scope})


# ===========================================================================
# The ledger is not writable by the party it caps
# ===========================================================================
def test_the_capped_party_cannot_zero_its_own_spend(role_txn: RoleTxn, ledger_scope: str) -> None:
    """THE attack that broke round two, executed literally.

    Consume the whole budget through the legitimate path, then try to erase the
    record of having consumed it. 0004's column grant allowed exactly this.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(RESERVE), {"scope": ledger_scope, "key": f"{TEST_TAG}-drain", "reserve": 1_000_000}
        )
        # control: the spend really did land, so the UPDATE below is the attack
        # and not a no-op against an empty ledger.
        assert conn.execute(text(SPENT), {"scope": ledger_scope}).scalar_one() == 1_000_000
        conn.execute(
            text("UPDATE budget_ledger SET spent_microusd = 0 WHERE scope = :scope"),
            {"scope": ledger_scope},
        )
    assert_insufficient_privilege(excinfo.value)


def test_the_capped_party_cannot_provision_its_own_scope(role_txn: RoleTxn) -> None:
    """The other half of round two's escape: a new scope with its own huge cap.

    Ledger rows come from migration/config. A reservation's ``scope`` is a
    foreign key to one of them, so there is no way to reserve against a budget
    nobody granted.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(
                "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) "
                "VALUES ('run:my-own-cap', 999999999, 0)"
            )
        )
    assert_insufficient_privilege(excinfo.value)


def test_reserving_against_an_unprovisioned_scope_is_refused(role_txn: RoleTxn) -> None:
    """And it cannot route around the ledger by naming a scope that has none.

    The foreign key would refuse this anyway; the trigger refuses it first, with
    a project SQLSTATE that says *why*, so a run halts with a diagnosis rather
    than an integrity error.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(RESERVE),
            {"scope": "run:never-provisioned", "key": f"{TEST_TAG}-ghost", "reserve": 1},
        )
    assert_sqlstate(excinfo.value, BUDGET_CAP_EXCEEDED)


# ===========================================================================
# Reserve, settle, and the release that monotonicity would have broken
# ===========================================================================
def test_reserve_then_settle_moves_the_ledger_both_ways(
    role_txn: RoleTxn, ledger_scope: str
) -> None:
    """Positive control for DESIGN's decision, under the new mechanism.

    Reserve worst-case, spend less, settle the actual: the ledger goes **up**
    then **down**. This is exactly why "spend may only increase" was not an
    acceptable fix -- it would have made honest settlement impossible.
    """
    with role_txn(ROLE_RECON_WRITER) as conn:
        reservation_id = conn.execute(
            text(RESERVE),
            {"scope": ledger_scope, "key": f"{TEST_TAG}-settle", "reserve": 400_000},
        ).scalar_one()
        assert conn.execute(text(SPENT), {"scope": ledger_scope}).scalar_one() == 400_000

        state = conn.execute(text(SETTLE), {"rid": reservation_id, "actual": 25_000}).scalar_one()
        assert state == "settled"
        released = conn.execute(text(SPENT), {"scope": ledger_scope}).scalar_one()
    assert released == 25_000, "settling releases reserve - actual"


def test_a_reservation_settles_exactly_once(role_txn: RoleTxn, ledger_scope: str) -> None:
    """Settling twice would release the same reservation twice -- free budget."""
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        reservation_id = conn.execute(
            text(RESERVE),
            {"scope": ledger_scope, "key": f"{TEST_TAG}-twice", "reserve": 400_000},
        ).scalar_one()
        conn.execute(text(SETTLE), {"rid": reservation_id, "actual": 10_000})  # control
        conn.execute(text(SETTLE), {"rid": reservation_id, "actual": 10_000})
    assert_sqlstate(excinfo.value, RESERVATION_LIFECYCLE)


def test_an_actual_cannot_exceed_its_reservation(role_txn: RoleTxn, ledger_scope: str) -> None:
    """Otherwise settlement is a second, uncapped spend channel.

    ``actual > reserve`` would *raise* the ledger past a cap that was already
    checked, which is the whole reason the reservation is worst-case.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        reservation_id = conn.execute(
            text(RESERVE),
            {"scope": ledger_scope, "key": f"{TEST_TAG}-over", "reserve": 100},
        ).scalar_one()
        conn.execute(text(SETTLE), {"rid": reservation_id, "actual": 100})  # control: == reserve
        conn.execute(
            text(
                "UPDATE budget_reservations SET actual_microusd = 101, state = 'settled' "
                "WHERE id = :rid"
            ),
            {"rid": reservation_id},
        )
    assert_sqlstate(excinfo.value, RESERVATION_LIFECYCLE)


def test_the_capped_party_cannot_reclaim_a_reservation(
    role_txn: RoleTxn, ledger_scope: str
) -> None:
    """ "Zero the spend", in its new disguise.

    ``reclaimed`` releases the *whole* reservation, so a capped party able to
    reclaim one it actually consumed gets its budget back for free. Reclaiming
    dead reservations is the TTL sweeper's job and runs as the ops principal.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        reservation_id = conn.execute(
            text(RESERVE),
            {"scope": ledger_scope, "key": f"{TEST_TAG}-reclaim", "reserve": 500_000},
        ).scalar_one()
        conn.execute(  # control: settling the same row IS allowed
            text(SETTLE), {"rid": reservation_id, "actual": 1}
        )
        second = conn.execute(
            text(RESERVE),
            {"scope": ledger_scope, "key": f"{TEST_TAG}-reclaim-2", "reserve": 500_000},
        ).scalar_one()
        conn.execute(
            text("UPDATE budget_reservations SET state = 'reclaimed' WHERE id = :rid"),
            {"rid": second},
        )
    assert_sqlstate(excinfo.value, RESERVATION_LIFECYCLE)


@pytest.mark.parametrize(
    "call",
    [
        pytest.param("SELECT keystone_budget_release(:scope, 1000000)", id="release"),
        pytest.param("SELECT keystone_budget_charge(:scope, -1000000)", id="charge-a-refund"),
    ],
)
def test_the_ledger_mutators_refuse_a_direct_call(
    owner_engine: Engine, role_txn: RoleTxn, ledger_scope: str, call: str
) -> None:
    """The SECURITY DEFINER helpers are not a back door, at either layer.

    Round three relied on ``pg_trigger_depth() = 0`` alone, and that was never a
    boundary: every role held TEMPORARY on the database, so any of them could
    define a trigger function in ``pg_temp`` and call
    ``keystone_budget_release`` from inside it, where the depth is 1. RULING 9 /
    migration 0006 fixes both layers, and this test now asserts both:

    * **the privilege.** EXECUTE is revoked from PUBLIC and from all three
      roles, so the capped party is refused at the privilege check with
      ``42501`` -- before a single line of the function body runs. This is why
      the role case no longer reads ``KS007``: the refusal became strictly
      earlier and stronger, and the SQLSTATE moved with it. The reserve/settle
      triggers are SECURITY DEFINER now, so the legitimate path calls the
      helpers as the owner and needs no grant -- proved by the control below.
    * **the depth guard.** ``KS007`` is not dropped; it is asserted against the
      **owner**, the one principal that can still reach the function body at
      all. A future ``GRANT EXECUTE`` that undid layer one would still hit it.
    """
    with role_txn(ROLE_RECON_WRITER) as conn:  # control: the trigger path works
        conn.execute(
            text(RESERVE), {"scope": ledger_scope, "key": f"{TEST_TAG}-direct", "reserve": 1}
        )

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text(call), {"scope": ledger_scope})
    assert_insufficient_privilege(excinfo.value)

    with pytest.raises(DBAPIError) as owner_excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(call), {"scope": ledger_scope})
        finally:
            transaction.rollback()
    assert_sqlstate(owner_excinfo.value, RESERVATION_LIFECYCLE)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("mutator", LEDGER_MUTATORS)
def test_no_role_holds_execute_on_the_ledger_mutators(
    owner_engine: Engine, role: str, mutator: str
) -> None:
    """RULING 9, read straight out of the catalog.

    ``has_function_privilege`` is the assertion the ruling names, and it is the
    one that cannot be satisfied by accident: it answers for the effective
    privilege, PUBLIC's default grant included.
    """
    with owner_engine.connect() as conn:
        allowed = conn.execute(
            text("SELECT has_function_privilege(:role, :fn, 'EXECUTE')"),
            {"role": role, "fn": mutator},
        ).scalar_one()
    assert allowed is False, f"{role} can still call {mutator}: the pg_temp escape is open"


@pytest.mark.parametrize("role", ROLES)
def test_no_role_may_create_anything_in_pg_temp(owner_engine: Engine, role: str) -> None:
    """The escape, run verbatim, failing at its first step.

    The published exploit was: create a trigger function in ``pg_temp``, attach
    it to a temp table, and call ``keystone_budget_release`` from inside it,
    where ``pg_trigger_depth()`` is 1 -- releasing spend the role never
    reserved. TEMPORARY on the database is revoked from PUBLIC and from all
    three roles, so step one is now ``42501`` and there is no ``pg_temp``
    schema for any of them to define code in.
    """
    with pytest.raises(DBAPIError) as excinfo, role_connection(role, commit=False) as conn:
        conn.execute(text("CREATE TEMP TABLE keystone_escape_hatch (x integer)"))
    assert_insufficient_privilege(excinfo.value)

    with owner_engine.connect() as conn:
        allowed = conn.execute(
            text("SELECT has_database_privilege(:role, current_database(), 'TEMP')"),
            {"role": role},
        ).scalar_one()
    assert allowed is False, f"{role} still holds TEMPORARY: pg_temp is still a code store"


def test_the_sweeper_may_reclaim_and_the_ledger_is_released(
    owner_engine: Engine, role_txn: RoleTxn, ledger_scope: str
) -> None:
    """Positive control for the reclaim path, on the principal that owns it.

    Without this, "recon_writer may not reclaim" could have been implemented by
    making reclaim impossible for everyone, which would leak the whole cap to
    dead reservations and halt runs forever.
    """
    with role_txn(ROLE_RECON_WRITER) as conn:
        pass  # connection control: the role is reachable

    with owner_engine.begin() as conn:
        reservation_id = conn.execute(
            text(RESERVE),
            {"scope": ledger_scope, "key": f"{TEST_TAG}-sweep", "reserve": 300_000},
        ).scalar_one()
        assert conn.execute(text(SPENT), {"scope": ledger_scope}).scalar_one() == 300_000
        conn.execute(
            text("UPDATE budget_reservations SET state = 'reclaimed' WHERE id = :rid"),
            {"rid": reservation_id},
        )
        remaining = conn.execute(text(SPENT), {"scope": ledger_scope}).scalar_one()
    assert remaining == 0, "reclaiming a dead reservation releases it in full"


def test_a_reservation_is_born_open_and_cannot_be_pre_settled(
    owner_engine: Engine, ledger_scope: str
) -> None:
    """A reservation that arrives already settled never charged the ledger.

    Exercised as the owner because ``recon_writer``'s INSERT grant does not even
    name ``state`` -- two independent layers, and this proves the trigger layer
    holds on its own.
    """
    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(
                    "INSERT INTO budget_reservations "
                    "(scope, idempotency_key, reserve_microusd, actual_microusd, state) "
                    "VALUES (:scope, :key, 1000, 0, 'settled')"
                ),
                {"scope": ledger_scope, "key": f"{TEST_TAG}-preborn"},
            )
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, RESERVATION_LIFECYCLE)


@pytest.mark.parametrize(
    "column",
    ["state", "actual_microusd", "settled_at", "created_at", "id"],
)
def test_the_reserve_insert_grant_is_exactly_what_the_caller_supplies(
    owner_engine: Engine, column: str
) -> None:
    """The caller names scope, key and amount. Everything else is the database's.

    A reservation whose ``state``, ``settled_at`` or ``created_at`` the writer
    chooses is not a reservation; it is a note the writer can back-date.
    """
    with owner_engine.connect() as conn:
        granted = set(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.column_privileges "
                    "WHERE grantee = :role AND table_name = 'budget_reservations' "
                    "AND privilege_type = 'INSERT'"
                ),
                {"role": ROLE_RECON_WRITER},
            )
            .scalars()
            .all()
        )
    assert granted == {"scope", "idempotency_key", "reserve_microusd"}, granted
    assert column not in granted


def test_the_idempotency_key_is_unique(role_txn: RoleTxn, ledger_scope: str) -> None:
    """A retried reserve must not charge the budget twice.

    DESIGN keys settlement on an idempotency id; the same id must therefore
    identify at most one reservation, or a retry storm silently multiplies spend.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(RESERVE), {"scope": ledger_scope, "key": f"{TEST_TAG}-idem", "reserve": 1}
        )  # control
        conn.execute(
            text(RESERVE), {"scope": ledger_scope, "key": f"{TEST_TAG}-idem", "reserve": 1}
        )
    assert_sqlstate(excinfo.value, "23505")


def test_the_cap_check_is_still_the_backstop(owner_engine: Engine, ledger_scope: str) -> None:
    """``CHECK (spent_microusd <= cap_microusd)`` survives as the last defence.

    If the reserve trigger were ever wrong, this is what still refuses the
    write. Exercised as the owner, since nobody else can write the ledger.
    """
    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(
                    "UPDATE budget_ledger SET spent_microusd = cap_microusd + 1 "
                    "WHERE scope = :scope"
                ),
                {"scope": ledger_scope},
            )
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, "23514")
    assert "ck_budget_spent_within_cap" in str(excinfo.value.orig)


# ===========================================================================
# The burst: N real connections, a cap that admits M, exactly M granted
# ===========================================================================
def _reserve_once(dsn: str, barrier: Barrier, scope: str, index: int, reserve: int) -> str:
    """One independent backend racing for one slot of budget.

    Returns ``"granted"``, the SQLSTATE of the refusal, or ``"error:<...>"`` --
    so a test that "passed" because every connection failed for an unrelated
    reason cannot be mistaken for a test that passed because the cap held.
    """
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")  # force the connection open before the barrier
            barrier.wait(timeout=30)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO budget_reservations "
                        "(scope, idempotency_key, reserve_microusd) VALUES (%s, %s, %s)",
                        (scope, f"{TEST_TAG}-burst-{index}", reserve),
                    )
                conn.commit()
            except psycopg.Error as exc:
                conn.rollback()
                return exc.sqlstate or "unknown"
            return "granted"
    except Exception as exc:  # pragma: no cover - diagnostic path only
        return f"error:{type(exc).__name__}:{exc}"


def test_a_concurrent_burst_grants_exactly_the_number_the_cap_admits(
    owner_engine: Engine,
) -> None:
    """The burst test, with real concurrent connections -- not a simulation.

    Twelve independent psycopg backends, released together by a barrier, each
    reserving a quarter of a cap that admits four. DESIGN pins reserve-worst-case
    *because* post-call accounting loses this race, so the replacement mechanism
    has to win it: the BEFORE trigger takes ``SELECT ... FOR UPDATE`` on the
    ledger row, so the contenders serialise on that row and each blocked one
    re-reads the committed ``spent_microusd`` before deciding.

    The assertions are deliberately three-sided:

    * exactly ``M`` grants -- not "at most", because granting fewer than the cap
      allows would be a broken product that still looked "safe";
    * every refusal carries ``KS006`` -- so a deadlock, a dropped connection or
      a serialization failure cannot masquerade as the cap holding;
    * the ledger lands exactly on the cap, with ``M`` stored reservations.
    """
    admitted = 4
    contenders = 12
    reserve = 250_000
    cap = admitted * reserve
    scope = f"run:{TEST_TAG}-burst-{uuid.uuid4()}"

    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) "
                "VALUES (:scope, :cap, 0)"
            ),
            {"scope": scope, "cap": cap},
        )

    dsn = psycopg_dsn(ROLE_RECON_WRITER)
    barrier = Barrier(contenders)
    try:
        with ThreadPoolExecutor(max_workers=contenders) as pool:
            outcomes = list(
                pool.map(
                    lambda index: _reserve_once(dsn, barrier, scope, index, reserve),
                    range(contenders),
                )
            )

        granted = [outcome for outcome in outcomes if outcome == "granted"]
        refused = [outcome for outcome in outcomes if outcome != "granted"]

        assert len(granted) == admitted, f"expected exactly {admitted} grants, got {outcomes}"
        assert set(refused) == {BUDGET_CAP_EXCEEDED}, (
            f"every refusal must be the cap refusing ({BUDGET_CAP_EXCEEDED}), got {outcomes}"
        )

        with owner_engine.connect() as conn:
            spent = conn.execute(text(SPENT), {"scope": scope}).scalar_one()
            stored = conn.execute(
                text("SELECT count(*) FROM budget_reservations WHERE scope = :s"), {"s": scope}
            ).scalar_one()
        assert spent == cap, "the burst must land exactly on the cap, never past it"
        assert stored == admitted
    finally:
        with owner_engine.begin() as conn:
            conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": scope})
            conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": scope})
