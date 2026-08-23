"""Alembic environment.

The database URL is resolved in exactly one place -- ``recon.db.database_url()``,
which reads ``DATABASE_URL`` through ``recon.config`` -- so migrations, the
application, and the tests can never drift onto different databases. A single
run can be redirected with ``alembic -x db_url=...`` (used by the scratch
database test that proves ``upgrade head`` works from empty).

There is no ``target_metadata``: this project does not autogenerate. Every
revision is hand-written so the DDL that ships is the DDL that was reviewed.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from recon.db import database_url
from recon.logging import configure_logging_once

# Alembic runs as its own process with its own logging setup, so it installs the
# privacy-safe structlog chain too -- `recon.db` and anything a revision imports
# must not be able to log an unredacted value here either
# (`recon.logging.ENTRY_POINTS`).
configure_logging_once()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _resolve_url() -> str:
    """``-x db_url=...`` wins; otherwise ``DATABASE_URL`` via recon.config."""
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if override:
        return override
    return database_url().render_as_string(hide_password=False)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run migrations in a transaction."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
