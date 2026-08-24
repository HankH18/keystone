"""Per-session scratch databases for the T-5 suites.

`DATABASE_URL` supplies the **server coordinates only** -- host, port, credentials.
This module creates its own database on that server, migrates it, hands back the
DSN and drops it at process exit, so the database `DATABASE_URL` names is never
read from or written to -- and neither is the one `OPS_DATABASE_URL` names, which
:func:`use_database` moves as well. Several agents share one Postgres; a suite that ingests
120,000 records into the shared database while another suite is doing the same
produces failures that vary run to run, and *a suite that looks flaky is a suite
where a real failure gets dismissed*.

The name carries the pid and a random token, so two concurrent pytest processes
never collide and a leaked database is traceable to the run that leaked it.
Migrations run in a subprocess because alembic mutates global state (its own
config, the logging tree, the SQLAlchemy metadata) -- and because that is how a
human would create the database.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

import psycopg
from sqlalchemy.engine import URL, make_url

__all__ = ["MAINTENANCE_DATABASE", "create_scratch_database", "drop_database", "use_database"]

#: CREATE/DROP are issued against this database. Never the one under test.
MAINTENANCE_DATABASE = "postgres"

SERVICE_ROOT = Path(__file__).resolve().parents[2]


def _server_url(raw: str) -> URL:
    url = make_url(raw)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    return url


def _dsn(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


#: The principal environment as it stood before the first switch. Captured on
#: that first call rather than at import, because a suite that ran earlier in the
#: same process may already have moved the process off the configured database.
_AMBIENT: dict[str, str | None] | None = None


def use_database(dsn: str) -> None:
    """Point the whole process at `dsn`, dropping every cached engine/settings object.

    `recon.config.get_settings` and `recon.db`'s engines are `lru_cache`d on the
    DSN variables; without clearing them a module that grabbed an engine before
    the scratch database existed keeps writing to the old one.

    **Every** principal variable moves (:data:`recon.db.PRINCIPAL_ENV_VARS`), not
    just `DATABASE_URL`. This helper's own callers are the proof that the second
    one is not decorative: `tests/integration/conftest.py` and
    `tests/suite/conftest.py` drive the real `POST /internal/sync`, whose trigger
    provisions a ledger scope through `recon.budget.ops_engine()` and whose stage
    3 opens `recon.api.internal._invariant_dsn()`. Both prefer
    `OPS_DATABASE_URL` whenever it is set -- which is the deployed shape
    (`infra/render.yaml`, where it is the production owner DSN) -- so while this
    function repointed `DATABASE_URL` alone, one run of
    `tests/integration/test_sync_pipeline.py` wrote 752,000 `invariant_results`
    rows and three `budget_ledger` scopes into a database the run neither created
    nor drops, and four of its own assertions failed because the rows it went
    looking for were not in the database it thought it was using.

    **Restoring.** This is deliberately not a context manager: its callers hold
    the process on a scratch database for a whole session and put it back by
    calling this again with the DSN they saved. So the first switch records the
    environment it found, and a call handing `DATABASE_URL` back that original
    value restores the ops principal that came with it -- including leaving it
    **unset** when it was unset, which `recon.db.restore_principal` does and a
    blank string would not. Any other DSN is a move to another database, and
    moves the whole principal there.
    """
    global _AMBIENT
    from recon.db import restore_principal, switch_principal

    if _AMBIENT is not None and dsn == _AMBIENT["DATABASE_URL"]:
        restore_principal(_AMBIENT)
        return
    previous = switch_principal(dsn)
    if _AMBIENT is None:
        _AMBIENT = previous


def _run_migrations(dsn: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=SERVICE_ROOT,
        env={**os.environ, "DATABASE_URL": dsn},
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "alembic upgrade head failed on the scratch database:\n"
            f"{completed.stdout}\n{completed.stderr}"
        )


def create_scratch_database(label: str, *, url: str | None = None) -> str:
    """Create and migrate a database of this run's own; return its psycopg DSN."""
    raw = url or os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("DATABASE_URL must be set: it supplies the server coordinates")
    base = _server_url(raw)
    name = f"keystone_{label}_{os.getpid()}_{secrets.token_hex(3)}"
    if len(name) > 63:  # Postgres identifier limit; truncating would risk a collision
        raise ValueError(f"scratch database name {name!r} exceeds 63 bytes")

    admin = _dsn(base.set(database=MAINTENANCE_DATABASE))
    scratch = _dsn(base.set(database=name))
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    _run_migrations(scratch)
    return scratch


def drop_database(dsn: str) -> None:
    """Drop the scratch database `dsn` names. Safe to call twice."""
    url = _server_url(dsn)
    name = url.database
    admin = _dsn(url.set(database=MAINTENANCE_DATABASE))
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
