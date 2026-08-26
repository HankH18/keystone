"""Two `alembic upgrade head` runs on one cluster must both succeed (T-14 infra).

Roles are a **cluster** object; migration 0002 provisions them from a
**per-database** migration. Two suites, each creating its own scratch database on
the shared Postgres, therefore both reach 0002 and both try to provision
`recon_writer` and `apply_writer` at the same time. Before the guard in
`_provision_role_sql`, that was measured at **9 of 12** concurrent upgrades
crashing, every one of them on

    ALTER ROLE recon_writer WITH ... PASSWORD ...
    -> psycopg.errors.InternalError_: tuple concurrently updated

`pg_authid` is a shared catalog updated without MVCC waiting: a concurrent
`ALTER ROLE` does not block, it fails. The `CREATE ROLE` branch has the mirror
race (`duplicate_object`) on a cluster where the roles do not exist yet.

This module drives the real thing -- N subprocesses running `alembic upgrade head`
against N freshly created databases -- because the failure only exists between
processes. It is skipped, not faked, when there is no server to run it on.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from sqlalchemy.engine import make_url

from tests.er.dataset import _require_server
from tests.er.scratchdb import MAINTENANCE_DATABASE, SERVICE_ROOT

#: Enough parallelism to lose the race reliably: at 4 the unguarded migration
#: failed in 9 of 12 attempts.
CONCURRENCY = 4

#: The revision `alembic upgrade head` must land on. Spelled out rather than
#: derived, so a migration that exits 0 without running -- the way this test could
#: lie -- is caught by a literal that does not move on its own.
#: `test_the_pinned_head_is_alembics_head` keeps the literal honest.
#:
#: Bumped from `0016_price_embedding_models` when
#: `0017_cap_message_states_refusal` landed -- the migration that restates
#: `keystone_budget_charge` so the `KS006` cap message reports the refusal the
#: trigger performs instead of ending with `-- halt the run`, an instruction the
#: reconcile path does not follow and which `record_cap_hit` was persisting into
#: `audit_log`. (It replaced `0015_escalation_reason_grant` one revision earlier,
#: when `0016` gave the three embedding models `recon.incidents` reserves against
#: a price.) That bump is exactly the event this literal exists to make loud:
#: `test_the_pinned_head_is_alembics_head` went red on the commit that added the
#: migration and the literal was moved deliberately, in the same commit, rather
#: than being derived so that it could never disagree.
EXPECTED_HEAD = "0017_cap_message_states_refusal"


def _admin_dsn() -> str:
    url = make_url(os.environ["DATABASE_URL"]).set(
        drivername="postgresql", database=MAINTENANCE_DATABASE
    )
    return url.render_as_string(hide_password=False)


def _database_dsn(name: str) -> str:
    url = make_url(os.environ["DATABASE_URL"]).set(drivername="postgresql", database=name)
    return url.render_as_string(hide_password=False)


@pytest.fixture
def fresh_databases() -> Iterator[list[str]]:
    """`CONCURRENCY` empty databases on the configured server, dropped afterwards.

    Deliberately **not** `create_scratch_database`: that helper migrates as part
    of creating, and migrating is the thing under test.
    """
    _require_server()
    names = [f"keystone_t14race_{os.getpid()}_{secrets.token_hex(3)}" for _ in range(CONCURRENCY)]
    admin = _admin_dsn()
    with psycopg.connect(admin, autocommit=True) as conn:
        for name in names:
            conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield names
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            for name in names:
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _upgrade(name: str) -> tuple[str, int, str]:
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=SERVICE_ROOT,
        env={**os.environ, "DATABASE_URL": _database_dsn(name)},
        capture_output=True,
        text=True,
        check=False,
    )
    return name, completed.returncode, (completed.stdout + completed.stderr)[-1500:]


def test_concurrent_upgrades_into_different_databases_all_succeed(
    fresh_databases: list[str],
) -> None:
    """The infrastructure claim: parallel suites are possible at all."""
    with ThreadPoolExecutor(max_workers=len(fresh_databases)) as pool:
        results = list(pool.map(_upgrade, fresh_databases))

    failures = [(name, output) for name, code, output in results if code != 0]
    assert not failures, (
        f"{len(failures)} of {len(results)} concurrent `alembic upgrade head` runs "
        "failed. Role provisioning in migration 0002 must survive the cluster-global "
        "race (advisory lock + idempotent handling of duplicate_object and "
        f"'tuple concurrently updated'). First failure:\n{failures[0][0]}\n{failures[0][1]}"
    )


def test_all_of_them_really_reached_head(fresh_databases: list[str]) -> None:
    """A migration that exits 0 without running is the way this test could lie."""
    with ThreadPoolExecutor(max_workers=len(fresh_databases)) as pool:
        results = list(pool.map(_upgrade, fresh_databases))
    assert all(code == 0 for _name, code, _out in results)

    for name in fresh_databases:
        with psycopg.connect(_database_dsn(name)) as conn:
            head = conn.execute("SELECT version_num FROM alembic_version").fetchone()
            assert head is not None and head[0] == EXPECTED_HEAD, (
                f"{name} is at {head!r}, not at head"
            )
            granted = conn.execute(
                "SELECT count(*) FROM information_schema.role_table_grants "
                "WHERE grantee = 'recon_writer' AND privilege_type = 'INSERT'"
            ).fetchone()
            assert granted is not None and granted[0] > 0, (
                f"{name} reached head but recon_writer holds no INSERT grant: the "
                "role branch was skipped rather than run"
            )


def test_advisory_locks_do_not_span_databases() -> None:
    """Why the advisory lock alone is not the fix, asserted rather than asserted-to.

    If this ever fails -- if Postgres made advisory locks cluster-wide -- the
    retry loop in 0002 would be redundant, and the comment explaining it would be
    wrong. Better to find that out here than to trust a lock that is not held.
    """
    _require_server()
    name = f"keystone_t14lock_{os.getpid()}_{secrets.token_hex(3)}"
    admin = _admin_dsn()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        with (
            psycopg.connect(_database_dsn(name), autocommit=True) as first,
            psycopg.connect(admin, autocommit=True) as second,
        ):
            first.execute("SELECT pg_advisory_lock(987654321)")
            row = second.execute("SELECT pg_try_advisory_lock(987654321)").fetchone()
            assert row is not None and row[0] is True, (
                "an advisory lock taken in one database blocked another database: "
                "advisory locks are now cluster-scoped, so migration 0002's retry "
                "loop can be simplified to just the lock"
            )
            second.execute("SELECT pg_advisory_unlock(987654321)")
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_the_pinned_head_is_alembics_head() -> None:
    """`EXPECTED_HEAD` must be the head of the real migration tree.

    The literal above is what makes "reached head" falsifiable: derived from the
    same place alembic derives it, the assertion would be satisfied by a database
    that is at whatever the tree happens to say, including a tree with a broken
    tail. So it stays a literal -- and this test is the alarm that says, in one
    sentence, that a migration landed and the literal was not bumped with it.

    It needs no server: `ScriptDirectory` reads `migrations/versions/` off disk.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(SERVICE_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(SERVICE_ROOT / "migrations"))
    heads = tuple(ScriptDirectory.from_config(config).get_heads())

    assert heads == (EXPECTED_HEAD,), (
        f"migrations/versions/ has head(s) {heads!r}, but this module pins "
        f"{EXPECTED_HEAD!r}. A migration landed without the pin being bumped: "
        "update EXPECTED_HEAD in the same commit that adds the revision, so "
        "`test_all_of_them_really_reached_head` keeps checking a revision that "
        "was written down on purpose."
    )
