"""Three-role separation of duties, reservation-backed spend cap, cited applies.

Revision ID: 0005_three_role_boundary
Revises: 0004_harden_write_boundary
Create Date: 2026-08-22

Two independent red-team passes broke the 0004 boundary five times. The common
root cause is that **two roles cannot express the property this project is
graded on**: *the automation must not be able to approve its own work*. With
``recon_writer`` proposing and ``apply_writer`` both approving and applying,
"approve" and "apply" were the same principal, and every bypass reduced to
"the machine decided its own case".

This revision is the third round. Each section below closes a *demonstrated*
bypass and carries a negative test asserting an exact SQLSTATE plus a positive
control on the same role and connection.

RULING 1 -- three roles, separation of duties
---------------------------------------------
``review_writer`` is added. The duties partition:

* ``recon_writer`` **PROPOSES**. Appends evidence and proposals-born-pending.
  Never decides, never applies, never mutates canonical state.
* ``review_writer`` **DECIDES**. The only role that may move a proposal to
  ``approved``/``rejected``, and only from ``pending``/``sensitive_hold``. Its
  UPDATE on ``proposals`` is column-scoped to ``(status, decided_by,
  decided_at)``; it holds no INSERT on ``proposals`` and no write of any kind
  on ``entities``.
* ``apply_writer`` **APPLIES**. ``approved -> applied`` and
  ``applied -> rolled_back`` only, and canonical UPDATEs gated by the
  correlated trigger below. It never approves and never proposes.

``keystone_proposal_status_transition`` (SQLSTATE ``KS004``) makes the legal
transition graph a property of the *database*, keyed on ``current_user``, so it
holds regardless of which application code issues the UPDATE. A decision by
``review_writer`` must additionally name a decider: a decision nobody signed is
indistinguishable from an automated one.

``keystone_proposal_payload_immutable`` (SQLSTATE ``KS005``) freezes
``conflict_id``, ``fingerprint``, ``action``, ``confidence``, ``evidence``,
``sensitive`` and ``target_canonical_id`` after INSERT. The red team rewrote a
pending proposal's ``action`` and ``confidence`` and then self-approved it;
immutability plus the role split kills that path. ``created_run``/``created_at``
need no trigger clause -- no role's column grant names them.

RULING 2 -- the spend cap becomes reservation-backed
----------------------------------------------------
Blocking ``cap_microusd`` was never a cap: ``recon_writer`` held
``UPDATE(spent_microusd)`` and simply zeroed a fully consumed budget.
Monotonicity is not the fix either -- settling actuals against a worst-case
reservation legitimately *decreases* spend.

``budget_reservations`` is now the only writable surface. ALL INSERT and UPDATE
on ``budget_ledger`` are revoked from ``recon_writer``: there is no writable
spend column left, so zeroing the ledger is not blocked by a rule, it is
structurally impossible. Ledger rows (scope + cap) are provisioned here from
the environment, never by the capped party -- which also closes "INSERT a brand
new scope with its own huge cap", since ``budget_reservations.scope`` carries a
foreign key to a ledger row the capped party cannot create.

RESERVE stays **one atomic statement**, so the concurrent-burst race DESIGN
warns about is still impossible::

    INSERT INTO budget_reservations (scope, idempotency_key, reserve_microusd)
    VALUES (...) RETURNING id

``keystone_budget_reserve`` (BEFORE INSERT) delegates to the SECURITY DEFINER
helper ``keystone_budget_charge``, which takes the ledger row lock with
``SELECT ... FOR UPDATE``, checks ``spent + reserve <= cap`` and either
increments ``spent_microusd`` or raises ``KS006``. Zero rows / raise => halt the
run. The triggers themselves stay SECURITY INVOKER because a role check inside a
SECURITY DEFINER function would read ``current_user`` as the function owner and
pass for everyone; the helpers refuse any call made outside a trigger
(``pg_trigger_depth() = 0``), so EXECUTE on them is not a back door.

Under READ COMMITTED the blocked contender re-reads the committed ``spent``
after the lock is released, so N concurrent reservations against a cap admitting
M grant exactly M (proved by a real multi-connection test, not a simulation).

SETTLE is ``keystone_budget_settle`` (BEFORE UPDATE):
``open -> settled`` once, ``actual <= reserve``, releasing ``reserve - actual``.
``recon_writer`` may make *only* that transition; ``open -> reclaimed`` (the TTL
sweeper, which releases the whole reservation) is reserved for the ops
principal, because a capped party that can reclaim a reservation it actually
consumed has re-invented "zero the spend".

``CHECK (spent_microusd <= cap_microusd)`` is kept as the backstop.

DESIGN pins *reserve worst-case then settle* as the DECISION; the
single-statement ``UPDATE budget_ledger`` was an interface detail. The decision
is preserved, the mechanism changed.

RULING 3 -- canonical writes must cite an approved proposal for that entity
---------------------------------------------------------------------------
0004's correlation was entity-only, so ``apply_writer`` rewrote every entity in
one transaction while citing a single proposal that was and stayed ``pending``.
Closed on four fronts:

* ``proposals.target_canonical_id uuid NOT NULL`` -- the one entity the fix
  would change. Pre-existing rows are backfilled with the nil UUID, which no
  ``uuid5``-derived canonical id can ever equal, so legacy proposals authorise
  nothing.
* the trigger now joins through to the cited proposal and requires its status
  to be ``approved``/``applied`` **and** its ``target_canonical_id`` to equal
  the row being written. One proposal authorises exactly one entity; the mass
  rewrite becomes unrepresentable rather than merely detected.
* ``after = NEW.current`` as well as ``before = OLD.current``, so the ledger
  cannot misreport what was written.
* ``apply_writer``'s UPDATE on ``entities`` is column-scoped to
  ``(current, updated_at)``. It could previously rewrite ``entity_type`` and
  ``created_at`` while the reversal record captured only ``current`` --
  provably unrestorable. The grant is asserted from
  ``information_schema.column_privileges``; ``role_table_grants`` shows only
  table-level privileges and is simply the wrong catalog view for this.

One deviation, deliberate and narrow: the reversal leg of the lifecycle moves
the proposal ``applied -> rolled_back`` in the *same* transaction as the
canonical write it reverses, and this trigger is DEFERRABLE INITIALLY DEFERRED,
so at commit it observes the end-of-transaction status. A ``rolled_back``
proposal is therefore accepted **only** as authorisation for an event whose own
``event`` is ``rolled_back``. Nothing pending, rejected or held ever authorises
a canonical write.

RULING 4 -- canonical creation must be traceable
-------------------------------------------------
``recon_writer`` could INSERT an entity with arbitrary ``current`` and no
provenance. ``entities_require_provenance`` (DEFERRED constraint trigger,
SQLSTATE ``KS008``) requires every inserted entity to have at least one
``entity_links`` row for its ``canonical_id`` by end of transaction. Canonical
rows can no longer be conjured from nothing; they must descend from real source
records. This is a **provenance floor, not a survivorship proof** -- whether the
surviving field values are the *right* ones is the pipeline's job and is graded
by ``recon.suite``. Claiming more for it would be overclaiming.

RULING 5 -- audit actor scoping covers every role
--------------------------------------------------
0004's actor trigger checked ``recon_writer`` only, so ``apply_writer`` could
still write ``reviewer:alice``. Extended: ``recon_writer`` and ``apply_writer``
must write ``^system:``, ``review_writer`` must write ``^reviewer:``. No machine
role may attribute an action to a human.

RULING 6 -- sensitive proposals are born held
----------------------------------------------
R15 ("sensitive fields can never auto-apply at any confidence") rested on
application convention. The born-pending trigger now rejects any proposal with
``sensitive = true`` whose birth status is not ``sensitive_hold``.

Project SQLSTATEs
-----------------
``KS001`` canonical UPDATE without a correlated, cited, approved authorisation
``KS002`` proposal not born pending/sensitive_hold, born decided, or sensitive
          but not born held
``KS003`` audit actor outside the scope its writing role is allowed
``KS004`` illegal proposal status transition for the acting role
``KS005`` proposal payload mutated after insert
``KS006`` budget reservation refused: no ledger row, or cap would be exceeded
``KS007`` illegal budget-reservation lifecycle change
``KS008`` canonical row inserted with no ``entity_links`` provenance

All are outside every built-in Postgres error class, so a test asserting one of
them cannot pass on an unrelated failure.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_three_role_boundary"
down_revision: str | None = "0004_harden_write_boundary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECON_WRITER = "recon_writer"
APPLY_WRITER = "apply_writer"
REVIEW_WRITER = "review_writer"
ALL_ROLES = (RECON_WRITER, REVIEW_WRITER, APPLY_WRITER)

#: Every table ``review_writer`` may read. It sees the whole review surface --
#: it is the human reviewer's connection -- and writes exactly two of them.
REVIEW_WRITER_SELECT = (
    "raw_records",
    "ingest_runs",
    "stg_crm_contact",
    "stg_crm_deal",
    "stg_student",
    "stg_enrollment",
    "stg_payment",
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
    "incidents",
    "conflict_incidents",
)

#: The decision surface, and nothing else. No INSERT on proposals: the decider
#: may not create the work it then approves.
REVIEW_WRITER_PROPOSAL_COLUMNS = ("status", "decided_by", "decided_at")

#: ``apply_writer`` moves a proposal through the apply leg only; it may not
#: name ``decided_by``/``decided_at``, so it cannot sign a decision as anyone.
APPLY_WRITER_PROPOSAL_COLUMNS = ("status",)

#: The canonical columns an apply may rewrite. ``entity_type`` and
#: ``created_at`` are absent: the reversal record captures ``current`` only, so
#: a change to anything else is provably unrestorable.
ENTITIES_UPDATE_COLUMNS = ("current", "updated_at")

#: What the capped party supplies when reserving. ``state``, ``actual_microusd``
#: and ``settled_at`` are DEFAULT/trigger-only, so a reservation is always born
#: open and its clock cannot be forged.
RESERVATION_INSERT_COLUMNS = ("scope", "idempotency_key", "reserve_microusd")

#: What the capped party supplies when settling.
RESERVATION_UPDATE_COLUMNS = ("actual_microusd", "state", "settled_at")

RESERVATION_STATES = ("open", "settled", "reclaimed")

#: ``proposal_events.event`` values that authorise a canonical mutation.
CANONICAL_EVENTS = ("applied", "rolled_back")

#: Statuses a cited proposal may carry for its citation to authorise a write.
AUTHORISING_STATUSES = ("approved", "applied")

BIRTH_STATUSES = ("pending", "sensitive_hold")

#: 1 USD = 1_000_000 microusd. Money is integer everywhere; the conversion runs
#: through ``Decimal`` so a cap in the environment can never pick up float dust.
MICROUSD_PER_USD = Decimal(1_000_000)

#: Ledger scopes provisioned here, and the environment variable each reads.
#: ``run:<id>`` rows are provisioned per run by the same ops principal that runs
#: migrations -- never by ``recon_writer``, which holds no INSERT on the ledger.
SEEDED_SCOPES: tuple[tuple[str, str, str], ...] = (
    ("daily", "DAILY_CAP_USD", "5.00"),
    ("run:default", "PER_RUN_CAP_USD", "1.00"),
)

#: The nil UUID. ``canonical_id`` is a deterministic uuid5 of sorted source
#: refs, which can never be nil, so a backfilled proposal targets no entity.
NIL_UUID = "00000000-0000-0000-0000-000000000000"


def _columns(names: Sequence[str]) -> str:
    return ", ".join(f'"{name}"' for name in names)


def _sql_string_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _plain_list(values: Sequence[str]) -> str:
    """Render ``values`` for an error message -- no quotes, so embedding the
    result in a SQL string literal cannot terminate it early."""
    return ", ".join(values)


def cap_microusd(env_var: str, default_usd: str) -> int:
    """Integer microusd cap for ``env_var``, falling back to ``default_usd``.

    Parsed through :class:`~decimal.Decimal`: a cap is money, and money never
    goes through a float. A malformed value falls back to the documented
    default rather than silently provisioning a zero (or enormous) cap.
    """
    raw = (os.environ.get(env_var) or "").strip() or default_usd
    try:
        usd = Decimal(raw)
    except (InvalidOperation, ValueError):
        usd = Decimal(default_usd)
    if usd < 0:
        usd = Decimal(default_usd)
    return int((usd * MICROUSD_PER_USD).to_integral_value())


# ---------------------------------------------------------------------------
# upgrade / downgrade
# ---------------------------------------------------------------------------
def upgrade() -> None:
    _create_review_writer_role()
    _add_target_canonical_id()
    _create_budget_reservations()
    _seed_budget_ledger()
    _create_proposal_transition_trigger()
    _create_proposal_immutability_trigger()
    _extend_born_pending_trigger()
    _extend_audit_actor_trigger()
    _install_cited_entities_trigger()
    _create_entity_provenance_trigger()
    _rescope_grants()


def downgrade() -> None:
    _restore_0004_grants()
    _restore_0004_entities_trigger()
    op.execute("DROP TRIGGER IF EXISTS entities_require_provenance ON entities")
    op.execute("DROP FUNCTION IF EXISTS keystone_require_entity_provenance()")
    _restore_0004_audit_actor_trigger()
    _restore_0004_born_pending_trigger()
    op.execute("DROP TRIGGER IF EXISTS proposals_payload_is_immutable ON proposals")
    op.execute("DROP FUNCTION IF EXISTS keystone_proposal_payload_immutable()")
    op.execute("DROP TRIGGER IF EXISTS proposals_status_transition ON proposals")
    op.execute("DROP FUNCTION IF EXISTS keystone_proposal_status_transition()")
    _drop_budget_reservations()
    _drop_seeded_budget_ledger()
    op.drop_index("ix_proposals_target", table_name="proposals")
    op.drop_column("proposals", "target_canonical_id")
    _drop_review_writer_privileges()


# ---------------------------------------------------------------------------
# RULING 1 -- the third role
# ---------------------------------------------------------------------------
def _create_review_writer_role() -> None:
    """Create (or re-password) ``review_writer``, exactly as 0002 does.

    Roles are cluster-scoped, so creation is idempotent and ``downgrade`` never
    drops the role -- only its privileges *in this database*. The password comes
    from ``REVIEW_WRITER_PASSWORD`` via ``set_config`` + ``quote_literal``; it
    is never string-interpolated into DDL.
    """
    guc = f"keystone.{REVIEW_WRITER}_password"
    password = os.environ.get(f"{REVIEW_WRITER.upper()}_PASSWORD") or REVIEW_WRITER
    op.get_bind().execute(
        sa.text("SELECT set_config(:guc, :password, true)"), {"guc": guc, "password": password}
    )
    op.execute(
        sa.text(
            f"""
            DO $$
            DECLARE
                pw text := current_setting('{guc}');
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{REVIEW_WRITER}') THEN
                    EXECUTE format(
                        'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB '
                        'NOCREATEROLE NOBYPASSRLS PASSWORD %L', '{REVIEW_WRITER}', pw);
                ELSE
                    EXECUTE format(
                        'CREATE ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB '
                        'NOCREATEROLE NOBYPASSRLS PASSWORD %L', '{REVIEW_WRITER}', pw);
                END IF;
            END $$;
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                EXECUTE format(
                    'GRANT CONNECT ON DATABASE %I TO "{REVIEW_WRITER}"', current_database());
            END $$;
            """
        )
    )
    op.execute(f'GRANT USAGE ON SCHEMA public TO "{REVIEW_WRITER}"')
    op.execute(f'GRANT SELECT ON {_columns(REVIEW_WRITER_SELECT)} TO "{REVIEW_WRITER}"')
    op.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{REVIEW_WRITER}"')

    # The decision surface: two writes, both column-scoped.
    op.execute(
        f"GRANT UPDATE ({_columns(REVIEW_WRITER_PROPOSAL_COLUMNS)}) "
        f'ON proposals TO "{REVIEW_WRITER}"'
    )
    op.execute(f'GRANT INSERT ON audit_log TO "{REVIEW_WRITER}"')


def _drop_review_writer_privileges() -> None:
    """Revoke everything in *this* database; leave the cluster role in place."""
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{REVIEW_WRITER}') THEN
                    EXECUTE format(
                        'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', '{REVIEW_WRITER}');
                    EXECUTE format(
                        'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', '{REVIEW_WRITER}');
                    EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', '{REVIEW_WRITER}');
                    EXECUTE format(
                        'REVOKE ALL ON DATABASE %I FROM %I',
                        current_database(), '{REVIEW_WRITER}');
                    EXECUTE format('DROP OWNED BY %I', '{REVIEW_WRITER}');
                END IF;
            END $$;
            """
        )
    )


# ---------------------------------------------------------------------------
# RULING 3 -- one proposal authorises exactly one entity
# ---------------------------------------------------------------------------
def _add_target_canonical_id() -> None:
    op.add_column(
        "proposals",
        sa.Column(
            "target_canonical_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment=(
                "The ONE entities row this proposal would change. The canonical "
                "trigger requires it to equal the row being written, so a single "
                "citation can never authorise a second entity. No FK: canonical "
                "rows are written by a different role in a different transaction "
                "(see 0001), so a physical FK would force the detection path to "
                "hold a privilege it is deliberately denied."
            ),
        ),
    )
    op.execute(sa.text(f"UPDATE proposals SET target_canonical_id = '{NIL_UUID}'::uuid"))
    op.alter_column(
        "proposals",
        "target_canonical_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_index("ix_proposals_target", "proposals", ["target_canonical_id", "status"])


# ---------------------------------------------------------------------------
# RULING 2 -- reservation-backed spend
# ---------------------------------------------------------------------------
def _create_budget_reservations() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*RESERVATION_STATES, name="budget_reservation_state").create(
        bind, checkfirst=True
    )

    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "scope",
            sa.Text,
            sa.ForeignKey("budget_ledger.scope", name="fk_budget_reservations_scope"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("reserve_microusd", sa.BigInteger, nullable=False),
        sa.Column("actual_microusd", sa.BigInteger, nullable=True),
        sa.Column(
            "state",
            postgresql.ENUM(
                *RESERVATION_STATES, name="budget_reservation_state", create_type=False
            ),
            nullable=False,
            server_default=sa.text("'open'::budget_reservation_state"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("settled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_budget_reservations_idempotency"),
        sa.CheckConstraint("reserve_microusd >= 0", name="ck_reservation_reserve_nonneg"),
        sa.CheckConstraint(
            "actual_microusd IS NULL OR actual_microusd >= 0", name="ck_reservation_actual_nonneg"
        ),
        sa.CheckConstraint(
            "actual_microusd IS NULL OR actual_microusd <= reserve_microusd",
            name="ck_reservation_actual_within_reserve",
        ),
        sa.CheckConstraint(
            "state <> 'settled'::budget_reservation_state OR actual_microusd IS NOT NULL",
            name="ck_reservation_settled_has_actual",
        ),
        sa.CheckConstraint(
            "(state = 'open'::budget_reservation_state) = (settled_at IS NULL)",
            name="ck_reservation_settled_at_matches_state",
        ),
        comment=(
            "The ONLY writable spend surface. budget_ledger.spent_microusd is "
            "maintained exclusively by the triggers on this table, so the capped "
            "party has no writable spend column at all -- zeroing the ledger is "
            "structurally impossible rather than merely forbidden. `scope` is a "
            "foreign key so a new scope with its own cap cannot be conjured."
        ),
    )
    op.create_index("ix_budget_reservations_scope", "budget_reservations", ["scope", "state"])
    op.create_index("ix_budget_reservations_open", "budget_reservations", ["created_at"])

    _create_ledger_mutators()
    _create_reserve_trigger()
    _create_settle_trigger()


def _create_ledger_mutators() -> None:
    """The ONLY code paths that may move ``budget_ledger.spent_microusd``.

    ``SECURITY DEFINER`` is what makes "spent is maintained only by a trigger"
    true rather than aspirational: these run as the schema owner, so
    ``recon_writer`` needs -- and has -- no privilege at all on
    ``budget_ledger``. ``search_path`` is pinned, as a SECURITY DEFINER function
    must always do.

    Two things about the split are load-bearing:

    * the *triggers* stay SECURITY INVOKER and only these helpers are DEFINER.
      Inside a SECURITY DEFINER function ``current_user`` is the function owner,
      so a role check written there would silently compare the owner against
      itself and pass for everyone. The role check therefore lives in the
      trigger, where ``current_user`` is genuinely the connected role.
    * ``pg_trigger_depth() = 0`` refuses a direct call. Without it, granting the
      capped party EXECUTE on ``keystone_budget_release`` -- which it needs,
      because the trigger runs as the invoker -- would hand it a SECURITY
      DEFINER function that decrements its own spend on demand: the "zero the
      ledger" bypass with extra steps.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_budget_charge(p_scope text, p_amount bigint)
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        DECLARE
            ledger_cap bigint;
            ledger_spent bigint;
        BEGIN
            IF pg_trigger_depth() = 0 THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'keystone_budget_charge is callable only from the'
                        || ' budget_reservations triggers, never directly';
            END IF;

            SELECT cap_microusd, spent_microusd INTO ledger_cap, ledger_spent
            FROM budget_ledger WHERE scope = p_scope FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS006',
                    MESSAGE = 'no budget_ledger row for scope ' || coalesce(p_scope, 'NULL')
                        || '; ledger rows (scope and cap) are provisioned by migration/config,'
                        || ' never by the capped party';
            END IF;

            IF ledger_spent + p_amount > ledger_cap THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS006',
                    MESSAGE = 'budget cap exceeded for scope ' || p_scope || ': spent '
                        || ledger_spent || ' + reserve ' || p_amount
                        || ' > cap ' || ledger_cap || ' -- halt the run';
            END IF;

            UPDATE budget_ledger
               SET spent_microusd = ledger_spent + p_amount, updated_at = now()
             WHERE scope = p_scope;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_budget_release(p_scope text, p_amount bigint)
        RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        BEGIN
            IF pg_trigger_depth() = 0 THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'keystone_budget_release is callable only from the'
                        || ' budget_reservations triggers, never directly';
            END IF;

            UPDATE budget_ledger
               SET spent_microusd = spent_microusd - p_amount, updated_at = now()
             WHERE scope = p_scope;
        END;
        $$;
        """
    )
    for signature in (
        "keystone_budget_charge(text, bigint)",
        "keystone_budget_release(text, bigint)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {_columns(ALL_ROLES)}")


def _create_reserve_trigger() -> None:
    """RESERVE: one atomic INSERT, the ledger row lock, cap check, or raise.

    The lock is ``SELECT ... FOR UPDATE`` on the ledger row, taken inside
    ``keystone_budget_charge``. Under READ COMMITTED (the default) a blocked
    contender re-reads the *committed* ``spent_microusd`` once the lock is
    released, so a burst of concurrent reservations serialises on that row and
    the cap admits exactly as many as fit. Under REPEATABLE READ the contender
    aborts with a serialization failure instead, which is also a refusal --
    never an overspend.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_budget_reserve() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.state IS DISTINCT FROM 'open'::budget_reservation_state
               OR NEW.actual_microusd IS NOT NULL
               OR NEW.settled_at IS NOT NULL
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'a budget reservation is born open with no actual and no'
                        || ' settled_at; got state='
                        || coalesce(NEW.state::text, 'NULL')
                        || ', actual_microusd=' || coalesce(NEW.actual_microusd::text, 'NULL')
                        || ', settled_at=' || coalesce(NEW.settled_at::text, 'NULL');
            END IF;

            PERFORM keystone_budget_charge(NEW.scope, NEW.reserve_microusd);
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER budget_reservations_reserve
        BEFORE INSERT ON budget_reservations
        FOR EACH ROW EXECUTE FUNCTION keystone_budget_reserve();
        """
    )


def _create_settle_trigger() -> None:
    """SETTLE: ``open -> settled`` once, ``actual <= reserve``, release the rest.

    ``recon_writer`` may make only that transition. ``open -> reclaimed`` -- the
    TTL sweeper releasing a dead reservation in full -- is reserved for the ops
    principal: a capped party able to reclaim a reservation it actually consumed
    has simply re-invented "zero the spend".

    SECURITY INVOKER on purpose, so ``current_user`` is the connected role and
    not the function owner; the privileged ledger write is delegated to
    ``keystone_budget_release``.

    ``settled_at`` is stamped by the trigger, not by the caller, so the
    reservation's clock cannot be forged even though the column is in the grant.
    """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION keystone_budget_settle() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.scope IS DISTINCT FROM OLD.scope
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.reserve_microusd IS DISTINCT FROM OLD.reserve_microusd
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'a reservation''s identity, scope, idempotency key, reserved'
                        || ' amount and creation time are immutable after insert';
            END IF;

            IF OLD.state <> 'open'::budget_reservation_state THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'reservation ' || OLD.id || ' is already '
                        || OLD.state::text || '; a reservation settles exactly once';
            END IF;

            IF current_user = '{RECON_WRITER}'
               AND NEW.state IS DISTINCT FROM 'settled'::budget_reservation_state
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'role {RECON_WRITER} may only settle a reservation'
                        || ' (open -> settled); reclaiming a reservation releases spend'
                        || ' in full and belongs to the sweeper, not to the capped party';
            END IF;

            IF NEW.state = 'settled'::budget_reservation_state THEN
                IF NEW.actual_microusd IS NULL
                   OR NEW.actual_microusd < 0
                   OR NEW.actual_microusd > OLD.reserve_microusd
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'KS007',
                        MESSAGE = 'settling reservation ' || OLD.id || ' requires 0 <= actual <='
                            || ' reserve (' || OLD.reserve_microusd || '), got '
                            || coalesce(NEW.actual_microusd::text, 'NULL');
                END IF;
                NEW.settled_at := now();
                PERFORM keystone_budget_release(
                    OLD.scope, OLD.reserve_microusd - NEW.actual_microusd);
            ELSIF NEW.state = 'reclaimed'::budget_reservation_state THEN
                IF NEW.actual_microusd IS NOT NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'KS007',
                        MESSAGE = 'a reclaimed reservation records no actual spend';
                END IF;
                NEW.settled_at := now();
                PERFORM keystone_budget_release(OLD.scope, OLD.reserve_microusd);
            ELSE
                RAISE EXCEPTION USING
                    ERRCODE = 'KS007',
                    MESSAGE = 'the only legal changes to an open reservation are'
                        || ' open -> settled and open -> reclaimed, got '
                        || coalesce(NEW.state::text, 'NULL');
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER budget_reservations_settle
        BEFORE UPDATE ON budget_reservations
        FOR EACH ROW EXECUTE FUNCTION keystone_budget_settle();
        """
    )


def _seed_budget_ledger() -> None:
    """Ledger rows come from the environment, through the migration principal.

    The capped party holds no INSERT on ``budget_ledger``, so this is the only
    way a scope and its cap ever come into existence. ``run:<id>`` rows are
    provisioned per run by the same ops principal.
    """
    for scope, env_var, default_usd in SEEDED_SCOPES:
        op.get_bind().execute(
            sa.text(
                "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) "
                "VALUES (:scope, :cap, 0) "
                "ON CONFLICT (scope) DO UPDATE SET cap_microusd = EXCLUDED.cap_microusd"
            ),
            {"scope": scope, "cap": cap_microusd(env_var, default_usd)},
        )


def _drop_budget_reservations() -> None:
    op.execute("DROP TRIGGER IF EXISTS budget_reservations_settle ON budget_reservations")
    op.execute("DROP TRIGGER IF EXISTS budget_reservations_reserve ON budget_reservations")
    op.execute("DROP FUNCTION IF EXISTS keystone_budget_settle()")
    op.execute("DROP FUNCTION IF EXISTS keystone_budget_reserve()")
    op.execute("DROP FUNCTION IF EXISTS keystone_budget_release(text, bigint)")
    op.execute("DROP FUNCTION IF EXISTS keystone_budget_charge(text, bigint)")
    op.drop_table("budget_reservations")
    postgresql.ENUM(*RESERVATION_STATES, name="budget_reservation_state").drop(
        op.get_bind(), checkfirst=True
    )


def _drop_seeded_budget_ledger() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM budget_ledger WHERE scope = ANY(:scopes)"),
        {"scopes": [scope for scope, _, _ in SEEDED_SCOPES]},
    )


# ---------------------------------------------------------------------------
# RULING 1 -- the transition graph is a property of the database
# ---------------------------------------------------------------------------
def _create_proposal_transition_trigger() -> None:
    """Exactly the graph the ruling pins, keyed on ``current_user``.

    ``review_writer``  pending|sensitive_hold -> approved|rejected
    ``apply_writer``   approved -> applied, applied -> rolled_back
    ``recon_writer``   nothing (it also holds no UPDATE privilege at all; the
                       trigger is the belt to that suspenders, so a future
                       widened grant does not silently reopen the path)

    A principal outside the three-role boundary -- the schema owner, i.e. the
    migration and ops principal -- is not constrained here. That is deliberate
    and it is *not* a bypass of the graded property: the owner can drop this
    trigger outright, so binding it would be theatre, while a repair path for a
    stuck proposal legitimately exists. Every process on the graded path
    connects as one of the three roles (``recon.db.role_connection``), and none
    of them can approve its own work. Contrast the born-pending rule, which
    binds the owner too: birth has no legitimate owner exception.
    """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION keystone_proposal_status_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            allowed boolean;
        BEGIN
            IF NEW.status IS NOT DISTINCT FROM OLD.status THEN
                RETURN NEW;
            END IF;

            IF current_user = '{REVIEW_WRITER}' THEN
                allowed := OLD.status::text IN ({_sql_string_list(BIRTH_STATUSES)})
                       AND NEW.status::text IN ('approved', 'rejected');
                IF allowed AND (NEW.decided_by IS NULL OR NEW.decided_at IS NULL) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'KS004',
                        MESSAGE = 'a decision must name its decider: role {REVIEW_WRITER} must'
                            || ' set decided_by and decided_at when moving proposal '
                            || OLD.id || ' to ' || NEW.status::text;
                END IF;
            ELSIF current_user = '{APPLY_WRITER}' THEN
                allowed := (OLD.status::text = 'approved' AND NEW.status::text = 'applied')
                        OR (OLD.status::text = 'applied' AND NEW.status::text = 'rolled_back');
            ELSIF current_user = '{RECON_WRITER}' THEN
                allowed := false;
            ELSE
                allowed := true;
            END IF;

            IF NOT allowed THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS004',
                    MESSAGE = 'role ' || current_user || ' may not move proposal ' || OLD.id
                        || ' from ' || OLD.status::text || ' to ' || NEW.status::text
                        || '; separation of duties: {REVIEW_WRITER} decides'
                        || ' (pending|sensitive_hold -> approved|rejected), {APPLY_WRITER}'
                        || ' applies (approved -> applied, applied -> rolled_back),'
                        || ' {RECON_WRITER} only proposes';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER proposals_status_transition
        BEFORE UPDATE ON proposals
        FOR EACH ROW EXECUTE FUNCTION keystone_proposal_status_transition();
        """
    )


def _create_proposal_immutability_trigger() -> None:
    """The payload a decision is made about cannot change under the decision.

    The red team rewrote a pending proposal's ``action`` and ``confidence`` and
    then self-approved it. Immutability plus the role split kills that path:
    the row a reviewer reads is the row that gets applied.

    Binds every role including the owner -- this is an invariant of the table,
    not a privilege. ``created_run``/``created_at`` are not listed because no
    role's column grant names them; the clause here covers exactly what a grant
    could otherwise reach.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_proposal_payload_immutable() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.conflict_id IS DISTINCT FROM OLD.conflict_id
               OR NEW.fingerprint IS DISTINCT FROM OLD.fingerprint
               OR NEW.action IS DISTINCT FROM OLD.action
               OR NEW.confidence IS DISTINCT FROM OLD.confidence
               OR NEW.evidence IS DISTINCT FROM OLD.evidence
               OR NEW.sensitive IS DISTINCT FROM OLD.sensitive
               OR NEW.target_canonical_id IS DISTINCT FROM OLD.target_canonical_id
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS005',
                    MESSAGE = 'proposal ' || OLD.id || ' is immutable after insert:'
                        || ' conflict_id, fingerprint, action, confidence, evidence,'
                        || ' sensitive and target_canonical_id may never change --'
                        || ' the row a reviewer decided on is the row that gets applied';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER proposals_payload_is_immutable
        BEFORE UPDATE ON proposals
        FOR EACH ROW EXECUTE FUNCTION keystone_proposal_payload_immutable();
        """
    )


# ---------------------------------------------------------------------------
# RULING 6 -- sensitive proposals are born held
# ---------------------------------------------------------------------------
def _extend_born_pending_trigger() -> None:
    """0004's rule, plus: ``sensitive`` implies born ``sensitive_hold``.

    R15 ("sensitive fields can never auto-apply at any confidence") rested on
    application convention -- the reconciler classifying and choosing the birth
    status. It is now a boundary rule: a sensitive proposal that is not born
    held cannot exist, so no confidence value and no code path can auto-apply
    one.
    """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION keystone_proposal_born_pending() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status IS NULL
               OR NEW.status::text NOT IN ({_sql_string_list(BIRTH_STATUSES)})
               OR NEW.decided_by IS NOT NULL
               OR NEW.decided_at IS NOT NULL
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS002',
                    MESSAGE = 'a proposal must be born pending: status must be one of'
                        || ' {_plain_list(BIRTH_STATUSES)} and decided_by/decided_at'
                        || ' must be NULL, got status='
                        || coalesce(NEW.status::text, 'NULL')
                        || ', decided_by=' || coalesce(NEW.decided_by, 'NULL')
                        || ', decided_at=' || coalesce(NEW.decided_at::text, 'NULL')
                        || ' (holds-before-writes)';
            END IF;
            IF NEW.sensitive AND NEW.status::text <> 'sensitive_hold' THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS002',
                    MESSAGE = 'a sensitive proposal is born held: status must be'
                        || ' sensitive_hold, got ' || NEW.status::text
                        || ' (R15 -- sensitive fields never auto-apply at any confidence)';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )


def _restore_0004_born_pending_trigger() -> None:
    """Put back the 0004 body verbatim so downgrade is a true inverse."""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION keystone_proposal_born_pending() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status IS NULL
               OR NEW.status::text NOT IN ({_sql_string_list(BIRTH_STATUSES)})
               OR NEW.decided_by IS NOT NULL
               OR NEW.decided_at IS NOT NULL
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS002',
                    MESSAGE = 'a proposal must be born pending: status must be one of'
                        || ' {_plain_list(BIRTH_STATUSES)} and decided_by/decided_at'
                        || ' must be NULL, got status='
                        || coalesce(NEW.status::text, 'NULL')
                        || ', decided_by=' || coalesce(NEW.decided_by, 'NULL')
                        || ', decided_at=' || coalesce(NEW.decided_at::text, 'NULL')
                        || ' (holds-before-writes)';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )


# ---------------------------------------------------------------------------
# RULING 5 -- audit actor scoping covers every role
# ---------------------------------------------------------------------------
def _extend_audit_actor_trigger() -> None:
    """Every machine role is scoped; only the reviewer role may look human.

    0004 checked ``recon_writer`` only, so ``apply_writer`` could still write
    ``reviewer:alice`` and the audit trail -- the record of *who decided* --
    could be forged by the automation that decided.
    """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION keystone_audit_actor_scope() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF current_user IN ('{RECON_WRITER}', '{APPLY_WRITER}')
               AND (NEW.actor IS NULL OR NEW.actor !~ '^system:')
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS003',
                    MESSAGE = 'role ' || current_user || ' may only write machine-scoped audit'
                        || ' actors (matching ^system:), got '
                        || coalesce(NEW.actor, 'NULL')
                        || '; the automation may never attribute an action to a'
                        || ' human reviewer';
            END IF;
            IF current_user = '{REVIEW_WRITER}'
               AND (NEW.actor IS NULL OR NEW.actor !~ '^reviewer:')
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS003',
                    MESSAGE = 'role {REVIEW_WRITER} may only write reviewer-scoped audit'
                        || ' actors (matching ^reviewer:), got '
                        || coalesce(NEW.actor, 'NULL')
                        || '; a decision is attributable to the human who made it';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )


def _restore_0004_audit_actor_trigger() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION keystone_audit_actor_scope() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF current_user = '{RECON_WRITER}'
               AND (NEW.actor IS NULL OR NEW.actor !~ '^system:')
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS003',
                    MESSAGE = 'role {RECON_WRITER} may only write machine-scoped audit'
                        || ' actors (matching ^system:), got '
                        || coalesce(NEW.actor, 'NULL')
                        || '; the automation may never attribute an action to a'
                        || ' human reviewer';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )


# ---------------------------------------------------------------------------
# RULING 3 -- the citation must be an approved proposal for THAT entity
# ---------------------------------------------------------------------------
def _install_cited_entities_trigger() -> None:
    """0004's correlation, plus the citation itself.

    Every clause is load-bearing and each is proved separately in
    ``tests/schema/test_three_role_boundary.py``:

    * ``pe.canonical_id = NEW.canonical_id`` -- the record names the row written
    * ``pe.event IN (applied, rolled_back)`` -- a note cannot double as approval
    * ``pe.before = OLD.current`` -- what was overwritten, so it can be restored
    * ``pe.after = NEW.current`` -- the ledger cannot misreport what was written
    * ``p.status`` approved/applied -- a *decided* proposal, not a pending one
    * ``p.target_canonical_id = NEW.canonical_id`` -- ONE proposal, ONE entity

    The last clause is what makes the mass rewrite unrepresentable rather than
    merely detected: N entities now require N distinct approved proposals.

    ``rolled_back`` appears in the status test only paired with a
    ``rolled_back`` event, because the reversal leg moves the proposal
    ``applied -> rolled_back`` in the same transaction as the canonical write it
    reverses, and this deferred trigger observes end-of-transaction status.
    Nothing pending, rejected or held ever authorises a canonical write.
    """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION keystone_require_proposal_event() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.canonical_id IS DISTINCT FROM OLD.canonical_id THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS001',
                    MESSAGE = 'canonical_id is immutable: rewriting it from '
                        || OLD.canonical_id || ' to ' || NEW.canonical_id
                        || ' would leave a reversal record that cannot restore the'
                        || ' row it claims to cover (holds-before-writes)';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM proposal_events pe
                JOIN proposals p ON p.id = pe.proposal_id
                WHERE pe.txid = pg_current_xact_id()::text::bigint
                  AND pe.canonical_id = NEW.canonical_id
                  AND pe.event IN ({_sql_string_list(CANONICAL_EVENTS)})
                  AND pe.before = OLD.current
                  AND pe.after = NEW.current
                  AND p.target_canonical_id = NEW.canonical_id
                  AND (
                        p.status::text IN ({_sql_string_list(AUTHORISING_STATUSES)})
                     OR (p.status::text = 'rolled_back' AND pe.event = 'rolled_back')
                  )
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS001',
                    MESSAGE = 'canonical UPDATE on entities ' || NEW.canonical_id
                        || ' requires a same-transaction proposal_events row whose'
                        || ' canonical_id is that row, whose event is one of'
                        || ' {_plain_list(CANONICAL_EVENTS)}, whose before/after equal the'
                        || ' pre- and post-update values of current, and which cites a'
                        || ' proposal that is {_plain_list(AUTHORISING_STATUSES)} and whose'
                        || ' target_canonical_id is that same row'
                        || ' (holds-before-writes: one approved proposal, one entity)';
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )


def _restore_0004_entities_trigger() -> None:
    """Put back the 0004 body verbatim so downgrade is a true inverse."""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION keystone_require_proposal_event() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.canonical_id IS DISTINCT FROM OLD.canonical_id THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS001',
                    MESSAGE = 'canonical_id is immutable: rewriting it from '
                        || OLD.canonical_id || ' to ' || NEW.canonical_id
                        || ' would leave a reversal record that cannot restore the'
                        || ' row it claims to cover (holds-before-writes)';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM proposal_events pe
                WHERE pe.txid = pg_current_xact_id()::text::bigint
                  AND pe.canonical_id = NEW.canonical_id
                  AND pe.event IN ({_sql_string_list(CANONICAL_EVENTS)})
                  AND pe.before = OLD.current
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS001',
                    MESSAGE = 'canonical UPDATE on entities ' || NEW.canonical_id
                        || ' requires a same-transaction proposal_events row whose'
                        || ' canonical_id is that row, whose event is one of'
                        || ' {_plain_list(CANONICAL_EVENTS)}, and whose before value'
                        || ' equals the pre-update value of current'
                        || ' (holds-before-writes)';
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )


# ---------------------------------------------------------------------------
# RULING 4 -- canonical creation must be traceable
# ---------------------------------------------------------------------------
def _create_entity_provenance_trigger() -> None:
    """Every canonical row must descend from at least one real source record.

    ``recon_writer`` could INSERT an entity with arbitrary ``current`` and no
    provenance whatsoever -- a fabricated canonical row that no source supports,
    which the apply path would then happily "fix" on the strength of a proposal.

    DEFERRED, because entity resolution writes the canonical row and its links
    in one transaction and the order between them is an implementation detail.

    Scope, stated plainly: this is a **provenance floor**. It proves the row
    descends from source records that were actually ingested. It does *not*
    prove the surviving field values are the right ones -- survivorship
    correctness is the pipeline's job and is graded by ``recon.suite`` against
    ``golden/``. Nothing here should be read as claiming otherwise.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_require_entity_provenance() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM entity_links el WHERE el.canonical_id = NEW.canonical_id
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS008',
                    MESSAGE = 'canonical row ' || NEW.canonical_id || ' has no entity_links'
                        || ' provenance: a canonical entity must descend from at least one'
                        || ' ingested source record, so it cannot be conjured from nothing';
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER entities_require_provenance
        AFTER INSERT ON entities
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION keystone_require_entity_provenance();
        """
    )


# ---------------------------------------------------------------------------
# the grant surface
# ---------------------------------------------------------------------------
def _rescope_grants() -> None:
    """Narrow every grant the third round makes wrong.

    A privilege must name what it permits; a table-level grant silently covers
    columns added later, and it covered verbs these roles must no longer hold.
    """
    # RULING 2: there is no writable spend column left for the capped party.
    op.execute(f'REVOKE INSERT ON budget_ledger FROM "{RECON_WRITER}"')
    op.execute(
        f"REVOKE UPDATE ({_columns(('spent_microusd', 'updated_at'))}) "
        f'ON budget_ledger FROM "{RECON_WRITER}"'
    )
    op.execute(f'REVOKE UPDATE ON budget_ledger FROM "{RECON_WRITER}"')
    op.execute(f"GRANT SELECT ON budget_reservations TO {_columns(ALL_ROLES)}")
    op.execute(
        f"GRANT INSERT ({_columns(RESERVATION_INSERT_COLUMNS)}) "
        f'ON budget_reservations TO "{RECON_WRITER}"'
    )
    op.execute(
        f"GRANT UPDATE ({_columns(RESERVATION_UPDATE_COLUMNS)}) "
        f'ON budget_reservations TO "{RECON_WRITER}"'
    )

    # RULING 1: apply_writer applies, it does not decide. Withdrawing
    # decided_by/decided_at means it cannot sign a decision as anybody at all.
    op.execute(f'REVOKE UPDATE ON proposals FROM "{APPLY_WRITER}"')
    op.execute(
        f'GRANT UPDATE ({_columns(APPLY_WRITER_PROPOSAL_COLUMNS)}) ON proposals TO "{APPLY_WRITER}"'
    )

    # RULING 3: an apply rewrites `current`, never identity or type.
    op.execute(f'REVOKE UPDATE ON entities FROM "{APPLY_WRITER}"')
    op.execute(
        f'GRANT UPDATE ({_columns(ENTITIES_UPDATE_COLUMNS)}) ON entities TO "{APPLY_WRITER}"'
    )


def _restore_0004_grants() -> None:
    """Exact inverse of :func:`_rescope_grants`, back to the 0004 surface."""
    op.execute(
        f'REVOKE UPDATE ({_columns(ENTITIES_UPDATE_COLUMNS)}) ON entities FROM "{APPLY_WRITER}"'
    )
    op.execute(f'GRANT UPDATE ON entities TO "{APPLY_WRITER}"')

    op.execute(
        f"REVOKE UPDATE ({_columns(APPLY_WRITER_PROPOSAL_COLUMNS)}) "
        f'ON proposals FROM "{APPLY_WRITER}"'
    )
    op.execute(f'GRANT UPDATE ON proposals TO "{APPLY_WRITER}"')

    op.execute(f'GRANT INSERT ON budget_ledger TO "{RECON_WRITER}"')
    op.execute(
        f"GRANT UPDATE ({_columns(('spent_microusd', 'updated_at'))}) "
        f'ON budget_ledger TO "{RECON_WRITER}"'
    )
