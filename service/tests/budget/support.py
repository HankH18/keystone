"""Shared live-database harness for the T-8 packages (budget, llm, triggers).

Every one of these tests needs a real Postgres, because the thing under test is
a cap enforced by triggers and grants. A mocked connection would assert that the
Python called the right SQL, which is precisely the claim that was already false
twice: the cap lives in the database, so the test has to reach it.

``KEYSTONE_REQUIRE_DB`` behaves exactly as it does for the schema package -- the
gate helpers are imported from there rather than re-implemented, so the two can
never drift into disagreeing about what "the database is required" means.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from recon.budget import DAILY_SCOPE, DAILY_SCOPE_ENV, run_scope
from recon.db import DatabaseNotConfigured, database_url
from tests.schema.conftest import (
    REQUIRE_DB_REASON,
    SKIP_REASON,
    database_is_required,
)

#: Marker embedded in every row these tests commit, so teardown finds them all.
TEST_TAG = "t8-test"


def unique(prefix: str) -> str:
    """A collision-proof identifier for one test's rows."""
    return f"{prefix}-{TEST_TAG}-{uuid.uuid4()}"


@pytest.fixture(scope="session")
def configured_url() -> str:
    """The configured DSN, or skip/fail per ``KEYSTONE_REQUIRE_DB``."""
    try:
        return database_url().render_as_string(hide_password=False)
    except DatabaseNotConfigured:
        if database_is_required():
            pytest.fail(REQUIRE_DB_REASON, pytrace=False)
        pytest.skip(SKIP_REASON)


@pytest.fixture(scope="session")
def owner_engine(configured_url: str) -> Iterator[Engine]:
    """Engine for the principal in ``DATABASE_URL`` -- ops/schema owner locally.

    This is also the **sweeper** principal: migration 0005 refuses
    ``open -> reclaimed`` to ``recon_writer``, so the TTL sweeper runs here and
    nowhere else.
    """
    engine = create_engine(configured_url, future=True)
    with engine.connect() as conn:
        migrated = conn.execute(text("SELECT to_regclass('public.budget_reservations')")).scalar()
    if migrated is None:
        pytest.fail(
            "DATABASE_URL points at a database with no Keystone schema. "
            "Run `uv run alembic upgrade head` in service/ first."
        )
    yield engine
    engine.dispose()


ScopeFactory = Callable[..., str]


def run_id_for(scope: str) -> str:
    """The ``run_id`` whose per-run scope IS ``scope``.

    Every scope :func:`make_scope` builds is ``run:<something>``, so a test that
    wants exactly one observable ledger row points the mandated daily cap at that
    row (which :func:`make_scope` does for the first scope it creates) and passes
    this as ``run_id``: both mandated scopes then resolve to the same row.

    Note what this is *not*: a way to drop the daily cap. There is none any more
    -- :func:`recon.budget.reserve` has no ``scopes`` parameter at all. A test
    chooses which ledger row carries the mandated cap, exactly as a deployment
    does, and every rule the cap enforces still fires.
    """
    if not scope.startswith("run:"):
        raise ValueError(f"{scope!r} is not a run scope; make_scope names them 'run:<id>'")
    resolved = run_scope(scope.removeprefix("run:"))
    assert resolved == scope
    return scope.removeprefix("run:")


@pytest.fixture
def make_scope(owner_engine: Engine) -> Iterator[ScopeFactory]:
    """Factory for throwaway ledger scopes, provisioned by **ops**.

    The capped party holds no INSERT on ``budget_ledger`` at all except for its
    own ``run:`` scope under a cap the ops-seeded ``run:default`` row bounds
    (migration 0010), so a fixture that provisioned an arbitrary scope through
    ``recon_writer`` could not exist -- which is the property under test, not an
    inconvenience.

    **It also points the mandated daily cap at one of the scopes it creates.**
    ``recon.budget`` refuses a test process the real ``daily`` row outright and
    there is no way to reserve without a daily scope, so every test that reserves
    needs a stand-in; making it the fixture's job means no test can forget, and
    none can be written that reserves without one.

    Which scope: ``hint="daily"`` claims the stand-in and keeps it, which is what
    the two-scope tests want (a daily row and a separate run row, each with its
    own cap). Otherwise the newest scope becomes the stand-in, so a test that
    makes a fresh scope per case gets a fresh, isolated ledger row per case --
    both mandated scopes then resolve to that one row, which is how a test gets
    one number to assert on without anything being able to *drop* a cap.
    """
    created: list[str] = []
    pinned: list[str] = []
    previous_daily = os.environ.get(DAILY_SCOPE_ENV)

    def _make(cap_microusd: int, hint: str = "run") -> str:
        scope = f"run:{unique(hint)}"
        with owner_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) "
                    "VALUES (:scope, :cap, 0)"
                ),
                {"scope": scope, "cap": cap_microusd},
            )
        if hint == "daily":
            pinned.append(scope)
            os.environ[DAILY_SCOPE_ENV] = scope
        elif not pinned:
            os.environ[DAILY_SCOPE_ENV] = scope
        created.append(scope)
        return scope

    yield _make

    if previous_daily is None:
        os.environ.pop(DAILY_SCOPE_ENV, None)
    else:
        os.environ[DAILY_SCOPE_ENV] = previous_daily

    with owner_engine.begin() as conn:
        for scope in created:
            conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": scope})
            conn.execute(text("DELETE FROM audit_log WHERE subject = :s"), {"s": scope})
            conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": scope})
        conn.execute(
            text("DELETE FROM audit_log WHERE subject LIKE :pattern"), {"pattern": f"%{TEST_TAG}%"}
        )


def spent(engine: Engine, scope: str) -> int:
    """``budget_ledger.spent_microusd`` for ``scope``."""
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT spent_microusd FROM budget_ledger WHERE scope = :s"), {"s": scope}
            ).scalar_one()
        )


def cap(engine: Engine, scope: str) -> int:
    """``budget_ledger.cap_microusd`` for ``scope``."""
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT cap_microusd FROM budget_ledger WHERE scope = :s"), {"s": scope}
            ).scalar_one()
        )


def reservations(engine: Engine, scope: str) -> list[tuple[str, int, int | None]]:
    """``(state, reserve, actual)`` for every reservation on ``scope``, oldest first."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT state::text AS state, reserve_microusd, actual_microusd "
                "FROM budget_reservations WHERE scope = :s ORDER BY id"
            ),
            {"s": scope},
        ).fetchall()
    return [(row.state, row.reserve_microusd, row.actual_microusd) for row in rows]


def audit_count(engine: Engine, *, action: str, subject: str) -> int:
    """How many ``audit_log`` rows match ``(action, subject)``."""
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM audit_log WHERE action = :a AND subject = :s"),
                {"a": action, "s": subject},
            ).scalar_one()
        )


def check_constraint_exists(engine: Engine) -> bool:
    """Is the ``spent <= cap`` backstop CHECK still on the ledger?

    The burst proves the trigger never let spend past the cap. This proves the
    *backstop* is still there while it did so -- a burst that passed because
    someone had dropped the CHECK would be a different and much worse result.
    """
    with engine.connect() as conn:
        return bool(
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'ck_budget_spent_within_cap' AND contype = 'c'"
                )
            ).scalar_one()
        )


def env_settings(monkeypatch: pytest.MonkeyPatch, **values: str | None) -> None:
    """Set/clear environment variables and drop the cached ``Settings``.

    ``get_settings`` is ``lru_cache``d, so an env change is otherwise invisible.
    A value of ``None`` **deletes** the variable -- which is how the keyless
    tests prove the suite runs with ``ANTHROPIC_API_KEY`` genuinely absent
    rather than merely set to an empty string.

    The cache is cleared again on teardown so a later test never inherits this
    test's settings.
    """
    from recon.config import get_settings

    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    get_settings.cache_clear()


#: Reconstructs `budget_ledger.spent_microusd` for one scope from the rows that
#: are still there. Sound because migration 0005 makes `budget_reservations` the
#: ONLY thing that moves spend: open rows hold their reservation, settled rows
#: hold their actual, reclaimed rows hold nothing.
_RECONCILE_SPEND = text(
    "UPDATE budget_ledger SET spent_microusd = COALESCE(("
    "  SELECT sum(CASE WHEN state = 'open' THEN reserve_microusd "
    "               WHEN state = 'settled' THEN actual_microusd ELSE 0 END) "
    "  FROM budget_reservations WHERE scope = :s), 0) WHERE scope = :s"
)


@pytest.fixture(scope="session", autouse=True)
def _keep_the_real_daily_scope_clean(configured_url: str) -> Iterator[None]:
    """Belt to `RealDailyScopeRefused`'s braces: no test rows survive on `daily`.

    ``recon.budget`` refuses a test process the real daily scope outright, so
    this should never have anything to do. It exists because the failure mode it
    cleans up after is *sticky*: a single leaked row charges the production daily
    ledger, and every later run of the suite then fails on a mess an earlier run
    made. Detection stays in the test (loud, in the run that caused it); the
    cleanup is here (quiet, at teardown), so one bad run cannot brick the next
    ten.

    The ledger is reconstructed from the surviving rows rather than decremented,
    because a decrement that is wrong in either direction is a worse lie than the
    row it was trying to remove.
    """
    yield
    engine = create_engine(configured_url, future=True)
    try:
        with engine.begin() as conn:
            removed = conn.execute(
                text(
                    "DELETE FROM budget_reservations WHERE scope = :s AND idempotency_key LIKE :p"
                ),
                {"s": DAILY_SCOPE, "p": f"%{TEST_TAG}%"},
            ).rowcount
            conn.execute(
                text("DELETE FROM audit_log WHERE subject LIKE :p"), {"p": f"%{TEST_TAG}%"}
            )
            if removed:
                conn.execute(_RECONCILE_SPEND, {"s": DAILY_SCOPE})
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _settings_cache_isolation() -> Iterator[None]:
    """Never leak one test's ``Settings`` into the next one."""
    from recon.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def has_env(name: str) -> bool:
    """Is ``name`` present in the process environment at all?"""
    return name in os.environ
