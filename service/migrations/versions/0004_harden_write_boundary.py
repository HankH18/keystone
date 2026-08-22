"""Harden the holds-before-writes boundary: born-pending, column grants, correlation.

Revision ID: 0004_harden_write_boundary
Revises: 0003_seed_api_clients
Create Date: 2026-08-22

Revisions 0001-0002 established the boundary; an adversarial review then walked
straight through it. Every change below closes a *demonstrated* bypass, not a
theoretical one. Each has a negative test asserting the exact SQLSTATE and a
positive control proving the legitimate path still works.

What was broken, and what this revision does about it
-----------------------------------------------------

**A proposal could be born approved.** ``recon_writer`` had no UPDATE on
``proposals`` -- but nothing constrained ``status`` at INSERT time, so the
detection path could simply insert ``status='applied', decided_by='reviewer'``
and hand itself pre-approved work. The grant was guarding the wrong verb. A
BEFORE INSERT trigger (``proposals_must_be_born_pending``, SQLSTATE ``KS002``)
now requires every proposal, from *every* role including the owner, to be born
``pending``/``sensitive_hold`` with no decision recorded. Terminal states are
reachable only by UPDATE, which only ``apply_writer`` holds.

**The capped party could raise its own cap.** ``recon_writer`` held
*table-level* UPDATE on ``budget_ledger``, which includes ``cap_microusd``. The
in-app spend cap is graded under burst, and "the process being capped may edit
the cap" is not a cap. The grant is re-issued COLUMN-scoped over exactly
``(spent_microusd, updated_at)`` -- the two columns the reserve-then-settle
statement writes.

**The reversal ledger could be pre-dated.** ``proposal_events.txid`` is an
ordinary writable bigint carrying only a DEFAULT of ``pg_current_xact_id()``.
With table-level INSERT, ``apply_writer`` could seed rows stamped with *future*
transaction ids and then commit arbitrary canonical UPDATEs later with no
reversal record. INSERT is re-issued COLUMN-scoped, excluding ``txid`` (and
``id`` and ``ts``): the DEFAULT is now the only way that column is ever
populated, so ``txid`` cannot lie about which transaction wrote the row.

**The trigger correlated nothing.** It asserted only that *some*
``proposal_events`` row carried the current txid, so one decoy row authorised
unlimited canonical rewrites in that transaction. ``proposal_events`` gains
``canonical_id``, and ``keystone_require_proposal_event`` now demands, **per
updated entity row**, a same-transaction event whose ``canonical_id`` is that
entity, whose ``event`` is a canonical-mutating event, and whose ``before``
EQUALS the pre-update value of ``entities.current``. That last clause is what
makes the rollback path real rather than decorative: a reversal record that
does not capture the prior state cannot restore it. For the same reason
``canonical_id`` is immutable under the trigger: the reversal record captures
``current``, not identity, so a row that changed identity could not be restored
from it -- and a canonical id is a deterministic uuid5 of its source refs, so
it has no legitimate reason to change.

**Canonical rows could be fabricated.** The trigger is AFTER UPDATE only, so
``apply_writer`` could INSERT brand-new canonical rows with no proposal and no
reversal path -- and DESIGN's "recon_writer has no UPDATE/DELETE/INSERT on
canonical or landing tables" is self-contradictory anyway, since ingestion must
append to ``raw_records``. The defensible reading, pinned here and to be
recorded in ARCHITECTURE.md, is **the pipeline may APPEND, only the guarded
path may MUTATE**:

* canonical entity CREATION is deterministic pipeline output (entity
  resolution), so ``recon_writer`` gets INSERT on ``entities`` and never
  UPDATE or DELETE;
* canonical entity MUTATION is the guarded path, so ``apply_writer`` gets
  UPDATE on ``entities`` and never INSERT or DELETE, gated by the correlated
  trigger above.

A proposal can therefore only ever CHANGE canonical state, never fabricate it,
and every change carries a same-transaction record of what it overwrote.

**The automation could impersonate a reviewer.** ``recon_writer`` could write
``audit_log`` rows attributing an action to a human. A BEFORE INSERT trigger
(``audit_log_actor_scope``, SQLSTATE ``KS003``) requires that when
``current_user`` is ``recon_writer`` the actor is machine-scoped (``^system:``).

**Conflicts were table-level writable.** ``recon_writer`` could rewrite
``entity_refs``, ``type`` and ``fingerprint`` -- i.e. redefine what a conflict
*is* after the fact, and ``fingerprint`` is the idempotency key. UPDATE is
re-issued COLUMN-scoped over ``(status, last_seen_run)``, the two columns
re-detection legitimately advances.

Project SQLSTATEs
-----------------
``KS001`` canonical UPDATE without a correlated same-transaction reversal record
``KS002`` proposal not born pending/sensitive_hold, or born already decided
``KS003`` recon_writer wrote an audit row with a non-machine actor

All three are outside every built-in Postgres error class, so a test asserting
one of them cannot pass on an unrelated failure.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_harden_write_boundary"
down_revision: str | None = "0003_seed_api_clients"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECON_WRITER = "recon_writer"
APPLY_WRITER = "apply_writer"

#: Statuses a proposal may be *born* in. Everything else is a decision, and a
#: decision is an UPDATE, which the detection path does not hold.
BIRTH_STATUSES = ("pending", "sensitive_hold")

#: ``proposal_events.event`` values that authorise a canonical mutation. Both
#: directions of the guarded path are here: an apply and its reversal each
#: rewrite ``entities.current`` and each must record what it overwrote.
CANONICAL_EVENTS = ("applied", "rolled_back")

#: Exactly the columns the reserve-then-settle UPDATE writes. ``cap_microusd``
#: is deliberately absent: the capped party may not raise its own cap.
BUDGET_UPDATE_COLUMNS = ("spent_microusd", "updated_at")

#: Exactly the columns re-detection advances. ``fingerprint`` (the idempotency
#: key), ``type`` and ``entity_refs`` are deliberately absent: they define what
#: the conflict *is* and may not be rewritten after the fact.
CONFLICT_UPDATE_COLUMNS = ("status", "last_seen_run")

#: ``proposal_events`` columns the apply path may supply. ``txid`` is absent so
#: the ``pg_current_xact_id()`` DEFAULT always applies; ``id`` is absent so the
#: identity sequence is authoritative; ``ts`` is absent so the reversal ledger's
#: clock cannot be forged.
PROPOSAL_EVENT_INSERT_COLUMNS = (
    "proposal_id",
    "canonical_id",
    "event",
    "before",
    "after",
    "actor",
)


def _columns(names: Sequence[str]) -> str:
    return ", ".join(f'"{name}"' for name in names)


def _sql_string_list(values: Sequence[str]) -> str:
    """Render ``values`` as a SQL ``IN`` list of quoted literals."""
    return ", ".join(f"'{value}'" for value in values)


def _plain_list(values: Sequence[str]) -> str:
    """Render ``values`` for an error message -- no quotes, so embedding the
    result inside a SQL string literal cannot terminate it early."""
    return ", ".join(values)


# ---------------------------------------------------------------------------
# upgrade
# ---------------------------------------------------------------------------
def upgrade() -> None:
    _add_canonical_id_to_proposal_events()
    _create_born_pending_trigger()
    _create_audit_actor_trigger()
    _install_correlated_entities_trigger()
    _rescope_grants()


def downgrade() -> None:
    _restore_0002_grants()
    _restore_uncorrelated_entities_trigger()
    op.execute("DROP TRIGGER IF EXISTS audit_log_actor_scope ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS keystone_audit_actor_scope()")
    op.execute("DROP TRIGGER IF EXISTS proposals_must_be_born_pending ON proposals")
    op.execute("DROP FUNCTION IF EXISTS keystone_proposal_born_pending()")
    op.drop_index("ix_proposal_events_canonical", table_name="proposal_events")
    op.drop_column("proposal_events", "canonical_id")


# ---------------------------------------------------------------------------
# MAJOR 5 -- the reversal record must name the row it authorises
# ---------------------------------------------------------------------------
def _add_canonical_id_to_proposal_events() -> None:
    """Give the reversal ledger the correlation key it was missing.

    Nullable by design: not every proposal event is a canonical mutation (a
    decision or a note carries no entity). The trigger requires the column to
    be populated for the events that *do* authorise a canonical write, which is
    where it is load-bearing.
    """
    op.add_column(
        "proposal_events",
        sa.Column(
            "canonical_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment=(
                "The entities row this event authorises/reverses. NULL for events "
                "that touch no canonical row. Correlates the reversal record to "
                "the exact row rewritten -- without it, one decoy event authorised "
                "unlimited canonical rewrites in the same transaction."
            ),
        ),
    )
    op.create_index("ix_proposal_events_canonical", "proposal_events", ["canonical_id", "txid"])


def _install_correlated_entities_trigger() -> None:
    """Replace the "some row, any row" check with a per-row correlation.

    The trigger stays a DEFERRABLE INITIALLY DEFERRED constraint trigger on the
    same name, so the apply function may still write the canonical row and the
    reversal record in either order within one transaction. What changes is what
    counts as a reversal record:

    * ``canonical_id`` must be the row actually updated -- not any row;
    * ``event`` must be a canonical-mutating event -- not any label;
    * ``before`` must EQUAL ``OLD.current`` -- the pre-update value. A record
      that does not capture the prior state cannot restore it, so a rollback
      ledger that fails this clause is decorative.

    ``before = OLD.current`` is deliberately a plain ``=`` and not
    ``IS NOT DISTINCT FROM``: a NULL ``before`` records nothing and must not
    satisfy the requirement.
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


def _restore_uncorrelated_entities_trigger() -> None:
    """Put back the 0001 body verbatim so downgrade is a true inverse."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_require_proposal_event() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM proposal_events
                WHERE txid = pg_current_xact_id()::text::bigint
            ) THEN
                RAISE EXCEPTION
                    'canonical UPDATE on entities requires a proposal_events row in the '
                    'same transaction (holds-before-writes)'
                    USING ERRCODE = 'KS001';
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )


# ---------------------------------------------------------------------------
# BLOCKER 1 -- a proposal must be born pending
# ---------------------------------------------------------------------------
def _create_born_pending_trigger() -> None:
    """No role, not even the owner, may insert a pre-decided proposal.

    This is deliberately a trigger rather than a CHECK constraint: a CHECK on
    ``status`` would also forbid the legitimate UPDATE that moves a proposal to
    ``approved``/``applied``. The rule is about *birth*, so it belongs on
    INSERT only.

    ``RETURN NEW`` matters: a BEFORE INSERT trigger returning NULL would
    silently swallow the row instead of storing it.
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
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER proposals_must_be_born_pending
        BEFORE INSERT ON proposals
        FOR EACH ROW EXECUTE FUNCTION keystone_proposal_born_pending();
        """
    )


# ---------------------------------------------------------------------------
# MINOR 9 -- the automation may not impersonate a reviewer
# ---------------------------------------------------------------------------
def _create_audit_actor_trigger() -> None:
    """recon_writer's audit rows must be machine-scoped.

    Scoped to ``current_user`` rather than applied universally, because human
    reviewer actions are legitimate audit rows -- they just do not come from the
    detection path's role. The owner and ``apply_writer`` are unaffected.
    """
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
    op.execute(
        """
        CREATE TRIGGER audit_log_actor_scope
        BEFORE INSERT ON audit_log
        FOR EACH ROW EXECUTE FUNCTION keystone_audit_actor_scope();
        """
    )


# ---------------------------------------------------------------------------
# BLOCKER 2 / BLOCKER 3 / MAJOR 6 / MINOR 10 -- the grant surface
# ---------------------------------------------------------------------------
def _rescope_grants() -> None:
    """Narrow every over-broad grant the review walked through.

    A table-level grant silently covers columns added later, which is exactly
    why ``budget_ledger``, ``conflicts`` and ``proposal_events`` are re-issued
    column-scoped: the privilege must name what it permits.
    """
    # BLOCKER 2: the capped party may advance spend, never the cap.
    op.execute(f'REVOKE UPDATE ON budget_ledger FROM "{RECON_WRITER}"')
    op.execute(
        f'GRANT UPDATE ({_columns(BUDGET_UPDATE_COLUMNS)}) ON budget_ledger TO "{RECON_WRITER}"'
    )

    # MINOR 10: re-detection advances a conflict; it does not redefine one.
    op.execute(f'REVOKE UPDATE ON conflicts FROM "{RECON_WRITER}"')
    op.execute(
        f'GRANT UPDATE ({_columns(CONFLICT_UPDATE_COLUMNS)}) ON conflicts TO "{RECON_WRITER}"'
    )

    # BLOCKER 3: txid is DEFAULT-only, so it cannot be pre-dated.
    op.execute(f'REVOKE INSERT ON proposal_events FROM "{APPLY_WRITER}"')
    op.execute(
        f"GRANT INSERT ({_columns(PROPOSAL_EVENT_INSERT_COLUMNS)}) "
        f'ON proposal_events TO "{APPLY_WRITER}"'
    )

    # MAJOR 6: the pipeline may APPEND canonical rows, only the guarded path
    # may MUTATE them. Neither role may DELETE (never granted, asserted in
    # tests/schema/test_role_permissions.py).
    op.execute(f'REVOKE INSERT ON entities FROM "{APPLY_WRITER}"')
    op.execute(f'GRANT INSERT ON entities TO "{RECON_WRITER}"')


def _restore_0002_grants() -> None:
    """Exact inverse of :func:`_rescope_grants`, back to the 0002 surface."""
    op.execute(f'REVOKE INSERT ON entities FROM "{RECON_WRITER}"')
    op.execute(f'GRANT INSERT ON entities TO "{APPLY_WRITER}"')

    op.execute(
        f"REVOKE INSERT ({_columns(PROPOSAL_EVENT_INSERT_COLUMNS)}) "
        f'ON proposal_events FROM "{APPLY_WRITER}"'
    )
    op.execute(f'GRANT INSERT ON proposal_events TO "{APPLY_WRITER}"')

    op.execute(
        f'REVOKE UPDATE ({_columns(CONFLICT_UPDATE_COLUMNS)}) ON conflicts FROM "{RECON_WRITER}"'
    )
    op.execute(f'GRANT UPDATE ON conflicts TO "{RECON_WRITER}"')

    op.execute(
        f'REVOKE UPDATE ({_columns(BUDGET_UPDATE_COLUMNS)}) ON budget_ledger FROM "{RECON_WRITER}"'
    )
    op.execute(f'GRANT UPDATE ON budget_ledger TO "{RECON_WRITER}"')
