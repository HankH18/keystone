"""Per-session scratch databases for the T-5 suites.

`DATABASE_URL` supplies the **server coordinates only** -- host, port, credentials.
This module creates its own database on that server, migrates it, hands back the
DSN and drops it at process exit, so the database `DATABASE_URL` names is never
read from or written to. Several agents share one Postgres; a suite that ingests
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


def use_database(dsn: str) -> None:
    """Point the whole process at `dsn`, dropping every cached engine/settings object.

    `recon.config.get_settings` and `recon.db`'s engines are `lru_cache`d on
    `DATABASE_URL`; without clearing them a module that grabbed an engine before
    the scratch database existed keeps writing to the old one.
    """
    from recon.config import get_settings
    from recon.db import reset_engine_cache

    os.environ["DATABASE_URL"] = dsn
    get_settings.cache_clear()
    reset_engine_cache()


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
