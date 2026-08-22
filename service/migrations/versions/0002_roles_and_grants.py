"""Two-role holds-before-writes boundary: recon_writer and apply_writer.

Revision ID: 0002_roles_and_grants
Revises: 0001_initial_schema
Create Date: 2026-08-22

This revision *is* the enforcement point ARCHITECTURE.md cites. The rule
"proposals land pending; nothing writes the canonical layer except the apply
path" is not a code convention here -- it is a Postgres privilege boundary:

``recon_writer`` -- the detection path (ingest, staging, ER, invariants,
    reconciler). INSERT on the evidence and proposal tables. Explicitly **no**
    UPDATE and **no** DELETE on ``entities`` (canonical) or ``raw_records``
    (landing), and **no** UPDATE on ``proposals`` -- so the process that writes
    a proposal cannot also approve it.

``apply_writer`` -- the apply path only. The sole role that may INSERT/UPDATE
    ``entities``, and the sole role that may move a proposal to
    ``applied``/``rolled_back``. It may not INSERT proposals, so it cannot
    manufacture its own work. Its canonical UPDATEs are additionally gated by
    the ``entities_require_proposal_event`` trigger from 0001.

Operational notes
-----------------
* Roles are **cluster-scoped**, not database-scoped. Creation is idempotent
  (created if absent, password re-applied if present).
* ``downgrade()`` therefore revokes every privilege and runs ``DROP OWNED BY``
  *in this database only*; it does not ``DROP ROLE``. Dropping a shared cluster
  role while another database still grants to it either fails or silently
  breaks that database. Roles outlive a single database's schema.
* Passwords come from ``RECON_WRITER_PASSWORD`` / ``APPLY_WRITER_PASSWORD`` in
  the migration process's environment, falling back to the role name for local
  development (``recon.db.role_password`` does exactly the same). They are set
  through ``set_config`` + ``quote_literal``, never string-interpolated.
* This revision needs a migration principal with CREATEROLE (or superuser).
  ``keystone`` in ``infra/docker-compose.yml`` is a superuser.
* The table OWNER (whoever ran the migration) bypasses all of these grants.
  Application code must connect *as* the role -- see ``recon.db.role_connection``.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_roles_and_grants"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECON_WRITER = "recon_writer"
APPLY_WRITER = "apply_writer"
ROLES = (RECON_WRITER, APPLY_WRITER)

STAGING_TABLES = (
    "stg_crm_contact",
    "stg_crm_deal",
    "stg_student",
    "stg_enrollment",
    "stg_payment",
)

ALL_TABLES = (
    "raw_records",
    "ingest_runs",
    *STAGING_TABLES,
    "entities",
    "entity_links",
    "entity_link_candidates",
    "field_lineage",
    "invariant_results",
    "conflicts",
    "proposals",
    "proposal_events",
    "budget_ledger",
    "audit_log",
    "api_clients",
    "incidents",
    "conflict_incidents",
)

#: Exactly the INSERT surface the ticket pins, plus the tables the detection
#: path cannot function without (budget reserve rows, incident clusters).
RECON_WRITER_INSERT = (
    "raw_records",
    "ingest_runs",
    *STAGING_TABLES,
    "entity_links",
    "entity_link_candidates",
    "field_lineage",
    "invariant_results",
    "conflicts",
    "proposals",
    "audit_log",
    "budget_ledger",
    "incidents",
    "conflict_incidents",
)

#: UPDATE is granted only where a row's own lifecycle demands it. Note the
#: absences: entities, raw_records, proposals, proposal_events, api_clients.
RECON_WRITER_UPDATE = (
    "ingest_runs",
    "conflicts",
    "budget_ledger",
    "incidents",
    "conflict_incidents",
)

#: Staging is a derived, re-materializable cache -- and the only place the
#: detection path may delete, so a generation can be re-materialized. Landing,
#: canonical, evidence and proposal tables are append-only to this role.
RECON_WRITER_DELETE = STAGING_TABLES

APPLY_WRITER_INSERT = ("entities", "proposal_events", "audit_log", "field_lineage")
APPLY_WRITER_UPDATE = ("entities", "proposals")


def _quoted(names: Sequence[str]) -> str:
    return ", ".join(f'"{name}"' for name in names)


def _set_role_password(role: str) -> str:
    """Stash ``role``'s password in a transaction-local GUC and name the GUC.

    ``CREATE ROLE`` cannot take a bind parameter, so the value is passed as a
    parameter to ``set_config`` and re-read inside the DO block via
    ``quote_literal(current_setting(...))``. No interpolation of the secret.
    """
    guc = f"keystone.{role}_password"
    password = os.environ.get(f"{role.upper()}_PASSWORD") or role
    op.get_bind().execute(
        sa.text("SELECT set_config(:guc, :password, true)"), {"guc": guc, "password": password}
    )
    return guc


def upgrade() -> None:
    for role in ROLES:
        guc = _set_role_password(role)
        op.execute(
            sa.text(
                f"""
                DO $$
                DECLARE
                    pw text := current_setting('{guc}');
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                        EXECUTE format(
                            'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB '
                            'NOCREATEROLE NOBYPASSRLS PASSWORD %L', '{role}', pw);
                    ELSE
                        EXECUTE format(
                            'CREATE ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB '
                            'NOCREATEROLE NOBYPASSRLS PASSWORD %L', '{role}', pw);
                    END IF;
                END $$;
                """
            )
        )

    # Never rely on default PUBLIC privileges: strip them, then grant explicitly.
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC")
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC")
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

    roles = _quoted(ROLES)
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                EXECUTE format('GRANT CONNECT ON DATABASE %I TO {roles}', current_database());
            END $$;
            """
        )
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {roles}")
    op.execute(f"GRANT SELECT ON {_quoted(ALL_TABLES)} TO {roles}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {roles}")

    op.execute(f'GRANT INSERT ON {_quoted(RECON_WRITER_INSERT)} TO "{RECON_WRITER}"')
    op.execute(f'GRANT UPDATE ON {_quoted(RECON_WRITER_UPDATE)} TO "{RECON_WRITER}"')
    op.execute(f'GRANT DELETE ON {_quoted(RECON_WRITER_DELETE)} TO "{RECON_WRITER}"')

    op.execute(f'GRANT INSERT ON {_quoted(APPLY_WRITER_INSERT)} TO "{APPLY_WRITER}"')
    op.execute(f'GRANT UPDATE ON {_quoted(APPLY_WRITER_UPDATE)} TO "{APPLY_WRITER}"')


def downgrade() -> None:
    """Revoke everything in *this* database; leave the cluster roles in place.

    ``DROP OWNED BY`` removes the role's privileges on objects in the current
    database only, which is exactly the blast radius a single database's
    migration is allowed to have.
    """
    for role in ROLES:
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                        EXECUTE format(
                            'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', '{role}');
                        EXECUTE format(
                            'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', '{role}');
                        EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', '{role}');
                        EXECUTE format(
                            'REVOKE ALL ON DATABASE %I FROM %I', current_database(), '{role}');
                        EXECUTE format('DROP OWNED BY %I', '{role}');
                    END IF;
                END $$;
                """
            )
        )
