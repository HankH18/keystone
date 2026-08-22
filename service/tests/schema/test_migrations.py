"""`alembic upgrade head` from a COMPLETELY EMPTY database, and back down.

This drops and recreates a scratch database on the same cluster rather than
running against the already-migrated development database. Running "upgrade
head" against a dirty database proves nothing: every CREATE could be a no-op
that a fresh deploy would fail on.

The alembic CLI is invoked as a subprocess so the thing under test is the
documented command (`uv run alembic upgrade head`), not a bespoke in-process
re-implementation of it.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, make_url, text

from tests.schema.conftest import SCRATCH_DB

EXPECTED_TABLE_SAMPLE = {
    "raw_records",
    "stg_crm_contact",
    "entities",
    "proposals",
    "budget_ledger",
    "budget_reservations",
    "api_clients",
    "incidents",
}


@pytest.fixture
def scratch_database(configured_url: str) -> Iterator[str]:
    """A freshly created, completely empty database; dropped afterwards."""
    url = make_url(configured_url)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT", future=True)
    drop = text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
    try:
        with admin.connect() as conn:
            conn.execute(drop)
            conn.execute(text(f'CREATE DATABASE "{SCRATCH_DB}"'))
        yield url.set(database=SCRATCH_DB).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(drop)
        admin.dispose()


def _alembic(service_root: Path, scratch_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url={scratch_url}", *args],
        cwd=service_root,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _tables(url: str) -> set[str]:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            return {
                name
                for (name,) in conn.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                ).all()
            }
    finally:
        engine.dispose()


def test_upgrade_head_on_an_empty_database_then_downgrade_base(
    service_root: Path, scratch_database: str
) -> None:
    assert _tables(scratch_database) == set(), "the scratch database was not empty"

    up = _alembic(service_root, scratch_database, "upgrade", "head")
    assert up.returncode == 0, f"upgrade failed:\n{up.stdout}\n{up.stderr}"

    after_upgrade = _tables(scratch_database)
    assert after_upgrade >= EXPECTED_TABLE_SAMPLE, sorted(EXPECTED_TABLE_SAMPLE - after_upgrade)

    engine = create_engine(scratch_database, future=True)
    try:
        with engine.connect() as conn:
            extensions = set(conn.execute(text("SELECT extname FROM pg_extension")).scalars().all())
            seeded = conn.execute(text("SELECT count(*) FROM api_clients")).scalar_one()
    finally:
        engine.dispose()

    assert {"vector", "pgcrypto"} <= extensions, extensions
    assert seeded == 2, "the demo API clients were not seeded by the migration"

    down = _alembic(service_root, scratch_database, "downgrade", "base")
    assert down.returncode == 0, f"downgrade failed:\n{down.stdout}\n{down.stderr}"

    after_downgrade = _tables(scratch_database)
    assert after_downgrade <= {"alembic_version"}, sorted(after_downgrade)


def test_roles_survive_a_downgrade_in_another_database(
    service_root: Path, scratch_database: str, owner_engine
) -> None:
    """Roles are cluster-scoped: tearing down one database must not disarm the
    privilege boundary in another."""
    up = _alembic(service_root, scratch_database, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    down = _alembic(service_root, scratch_database, "downgrade", "base")
    assert down.returncode == 0, down.stderr

    with owner_engine.connect() as conn:
        grants = conn.execute(
            text(
                "SELECT grantee, privilege_type, column_name "
                "FROM information_schema.column_privileges "
                "WHERE table_name = 'entities' AND grantee = ANY(:roles)"
            ),
            {"roles": ["recon_writer", "review_writer", "apply_writer"]},
        ).all()

    # Read from column_privileges, not role_table_grants: migration 0005
    # column-scopes apply_writer's UPDATE to (current, updated_at), and a
    # column grant does not appear in role_table_grants at all -- asserting
    # there would have quietly inverted the claim.
    assert ("apply_writer", "UPDATE", "current") in grants
    assert ("apply_writer", "UPDATE", "entity_type") not in grants
    assert ("recon_writer", "UPDATE", "current") not in grants
    assert ("recon_writer", "SELECT", "current") in grants
    assert ("review_writer", "SELECT", "current") in grants
    assert not [row for row in grants if row[0] == "review_writer" and row[1] != "SELECT"]
