"""Per-session scratch databases -- the isolation the invariant suite runs in.

**Why this exists.** Several agents share one Postgres. A suite that ingests 120,000
records into the database `DATABASE_URL` names, while another suite is doing the
same, produces failures that vary run to run -- and *a suite that looks flaky is a
suite where a real failure gets dismissed*. Two runs of the invariant engine on one
shared database would also see each other's `invariant_results` and `conflicts` rows,
which is the exact thing the golden diff is counting.

**The approach, in one sentence:** `DATABASE_URL` supplies the *server coordinates*
only (host, port, credentials); the suite creates its **own** database on that server,
migrates it, uses it, and drops it -- so the database `DATABASE_URL` names is never
read from or written to.

    DATABASE_URL=postgresql://.../keystone     # only host/port/user are used
      -> connect to the `postgres` maintenance database
      -> CREATE DATABASE keystone_t6_<pid>_<token>
      -> alembic upgrade head            (subprocess, its own DATABASE_URL)
      -> ...the tests run...
      -> DROP DATABASE ... WITH (FORCE)  (in a finally:, so a crash still cleans up)

Three properties make it generalizable to the rest of the test tree:

* **The name carries the pid and a random token**, so two concurrent pytest processes
  -- and two agents -- never collide, and a leaked database is traceable to the run
  that leaked it.
* **Nothing is cached across the boundary.** `recon.config.get_settings` and
  `recon.db`'s engine caches are `lru_cache`d on `DATABASE_URL`; :func:`use_database`
  sets the variable *and* clears both, so a module that grabbed an engine before the
  scratch database existed cannot keep writing to the old one.
* **Migrations run in a subprocess.** Alembic mutates global state (its own config,
  the logging tree, the SQLAlchemy metadata); a subprocess keeps that out of the test
  process entirely, and it is also how a human would create the database.

`DROP DATABASE ... WITH (FORCE)` is Postgres 13+; the project pins 16.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from sqlalchemy.engine import URL, make_url

__all__ = [
    "MAINTENANCE_DATABASE",
    "psycopg_dsn",
    "scratch_database",
    "server_url",
    "use_database",
]

#: The database CREATE/DROP are issued against. Never the one under test.
MAINTENANCE_DATABASE = "postgres"

SERVICE_ROOT = Path(__file__).resolve().parents[2]


def server_url(raw: str) -> URL:
    """`raw` with its driver normalized and its database left as given."""
    url = make_url(raw)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    return url


def psycopg_dsn(url: URL) -> str:
    """A plain libpq DSN for psycopg, with the SQLAlchemy driver tag stripped."""
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def use_database(dsn: str) -> Iterator[str]:
    """Point the whole process at `dsn`, dropping every cached engine/settings object.

    Restores the previous value (and clears the caches again) on the way out, so a
    test that runs after this one does not inherit a scratch database that no longer
    exists.
    """
    from recon.config import get_settings
    from recon.db import reset_engine_cache

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    get_settings.cache_clear()
    reset_engine_cache()
    try:
        yield dsn
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
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


@contextmanager
def scratch_database(label: str, *, url: str | None = None) -> Iterator[str]:
    """Create, migrate, yield and drop a database of this run's own.

    Yields the psycopg DSN of the new database. `label` is a short slug that ends up
    in the name (`keystone_<label>_<pid>_<token>`), so a stray database says which
    suite created it.
    """
    raw = url or os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("DATABASE_URL must be set: it supplies the server coordinates")
    base = server_url(raw)
    name = f"keystone_{label}_{os.getpid()}_{secrets.token_hex(3)}"
    if len(name) > 63:  # Postgres identifier limit; truncating would risk a collision
        raise ValueError(f"scratch database name {name!r} exceeds 63 bytes")

    admin = psycopg_dsn(base.set(database=MAINTENANCE_DATABASE))
    scratch = psycopg_dsn(base.set(database=name))

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        _run_migrations(scratch)
        yield scratch
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
