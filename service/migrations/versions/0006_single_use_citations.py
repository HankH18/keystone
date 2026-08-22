"""Single-use citations, a closed event vocabulary, and no pg_temp back door.

Revision ID: 0006_single_use_citations
Revises: 0005_three_role_boundary
Create Date: 2026-08-22

Round three closed eight of ten demonstrated exploits. This revision closes the
two blockers and four lesser findings that survived it. Every section below
carries a negative test asserting an exact SQLSTATE, a positive control on the
same role and connection, and a sabotage run proving the test goes red when the
rule is removed.

RULING 7 -- a citation is CONSUMED, not held forever
-----------------------------------------------------
The single most graded property in this project is *one human approval
authorises exactly one canonical write*. 0005 did not have it. Its citation
rule accepted a cited proposal whose status was ``approved`` **or**
``applied``, and nothing marked a citation as spent -- so after one legitimate
apply the proposal sat at ``applied`` forever and authorised an unbounded
series of further arbitrary rewrites of its target entity. An approval was a
standing licence.

Four changes, each independently load-bearing:

* **``applied`` no longer authorises an apply.** The rule now pairs the event
  with the status of the proposal it cites::

      event = 'applied'      requires status = 'approved'
                             or status = 'applied' *moved in this transaction*
      event = 'rolled_back'  requires status = 'applied'
                             or status = 'rolled_back' *moved in this transaction*

  The "moved in this transaction" half is not a loophole, it is what makes the
  rule expressible at all: the entities trigger is DEFERRABLE INITIALLY
  DEFERRED, so at COMMIT it observes end-of-transaction status, and the
  legitimate apply moves ``approved -> applied`` in the *same* transaction as
  the canonical write. ``proposals.status_txid`` -- written only by
  ``keystone_proposal_status_transition``, named by no role's column grant, and
  forced to NULL at birth -- records which transaction last moved the status,
  so "already applied, in an earlier transaction" is distinguishable from
  "being applied right now". A proposal applied yesterday authorises nothing.

* **A citation is consumable exactly once.** Two partial UNIQUE indexes on
  ``proposal_events``: ``UNIQUE(proposal_id) WHERE event = 'applied'`` and
  ``UNIQUE(proposal_id) WHERE event = 'rolled_back'``. The authorising event
  must carry the *current* transaction id (``txid`` is DEFAULT-only since
  0004), so a replay must insert a second row -- and cannot. One approval, one
  apply, one reversal, forever.

* **A rollback must restore what the apply overwrote.** ``rolled_back`` is
  accepted only when the proposal's own ``applied`` event captured exactly the
  value being written back. Otherwise "roll back" was a second arbitrary write
  wearing the reversal leg's clothes.

* **``before = OLD.current`` and ``after = NEW.current`` are kept**, unchanged
  and still enforced, so the ledger can neither omit nor misreport the write.

RULING 8 -- ``proposal_events.event`` is a closed vocabulary
------------------------------------------------------------
``apply_writer`` could write any label it liked into the reversal ledger,
including ``approved`` -- a decision word, in the one table a reader would
consult to find out what happened to a proposal. ``event`` is now pinned by
``ck_proposal_events_event_vocabulary`` to exactly three values: the two
canonical mutations (``applied``, ``rolled_back``) and ``noted``, the sole
non-authorising label. ``noted`` is deliberately kept: it is what makes "an
event label is not an authorisation" provable on its own, and it is precisely
*not* a decision word. No decision vocabulary exists in this table at all.

RULING 9 -- the pg_temp escape, closed at both layers
------------------------------------------------------
``pg_trigger_depth() = 0`` was never a boundary. Every role held TEMPORARY on
the database, so any of them could define a trigger function in ``pg_temp``,
attach it to a temp table, and call ``keystone_budget_release`` from inside it
where the depth is 1 -- releasing spend it never reserved. Two independent
layers, because either alone is one CVE away from failing:

* EXECUTE on ``keystone_budget_charge`` and ``keystone_budget_release`` is
  revoked from PUBLIC and from all three roles. ``has_function_privilege``
  now answers false for every one of them. This is possible because the
  reserve/settle trigger functions become **SECURITY DEFINER**, i.e. owner-run,
  so the legitimate path calls the helpers as the owner and needs no grant. The
  role check inside ``keystone_budget_settle`` therefore moves from
  ``current_user`` (which a definer function reports as the owner) to
  ``session_user``, which is the authenticated login role -- ``recon.db``
  connects *as* the role and never uses ``SET ROLE``, and changing
  ``session_user`` requires superuser, which no boundary role is.
* TEMPORARY on the database is revoked from PUBLIC and from the three roles, so
  ``pg_temp`` is not a schema any of them can define code in at all. The escape
  now fails at its first step with ``42501``.

``pg_trigger_depth() = 0`` stays in both helpers as the third layer, and is
still proved -- against the owner, the one principal that can still reach them.

RULING 10 -- the reversal ledger gets the actor scoping the audit log has
-------------------------------------------------------------------------
RULING 5 scoped actors on ``audit_log`` and stopped there, so ``apply_writer``
-- whose column grant includes ``actor`` -- forged ``reviewer:alice`` into
``proposal_events``. The same rule now binds both tables: ``recon_writer`` and
``apply_writer`` must write ``^system:``, ``review_writer`` must write
``^reviewer:``. No machine role may attribute anything to a human, in either
ledger.

RULING 11 -- the provenance floor stops being self-satisfiable
---------------------------------------------------------------
0005 required a canonical row to have an ``entity_links`` row, and
``entity_links`` referenced nothing ingested -- so ``recon_writer`` fabricated
a canonical entity in two INSERTs of its own invention. Every link must now
name a ``raw_records`` row with the same ``(source_id, natural_key,
generation)``.

This is a constraint **trigger**, not a physical FOREIGN KEY, and the reason is
in the schema rather than in convenience: ``raw_records`` is the append-only
landing table and duplicate natural keys within a generation are *legitimate
input* (C11, asserted in ``test_schema_shape``), so the referenced columns have
no unique key for a FK to point at. The trigger is the closest real equivalent:
DEFERRABLE INITIALLY DEFERRED, so ingestion order stays an implementation
detail, and existence-checked per row.

What this proves, stated so nobody has to guess: a canonical row now descends
from a record that came through the landing table. What it does **not** prove:
that the record came through an adapter. ``recon_writer`` still holds INSERT on
``raw_records``, so fabrication is raised from two INSERTs to three, with the
third landing in ``raw_records``. It is a provenance floor. Survivorship
correctness is NOT graded anywhere yet: ``recon.suite`` currently registers one
check (``mirror-unchanged``), and the golden-set diff that will grade it is
T-14's, unbuilt at the time of writing. Stated in the future tense on purpose --
a control named in the present tense reads as settled.

**Corrected in 0007.** This paragraph originally justified the floor by saying
the third INSERT lands "in the table the suite's mirror-unchanged hash check
reads". At the time ``recon.suite`` had an empty check registry and nothing
read it -- a cited control that did not exist, which reads as settled and is
worse than admitting the limit. The control was then built rather than the
sentence reworded. What exists today, precisely:

* ``recon.suite.mirror.mirror_digest`` content-hashes all seven landing and
  staging tables (``ingest_runs``, ``raw_records``, ``stg_*``). It is real and
  exercised: ``tests/schema/test_suite_mirror_check.py`` has ``recon_writer``
  commit exactly the landing row this floor forces it to leave and asserts the
  digest moves and names ``raw_records``;
* the check ``mirror-unchanged`` is registered in ``recon.suite`` under that
  name, and ``python -m recon.suite`` exits non-zero on it;
* it **currently FAILS**, deliberately, with ``not yet implemented: recon
  .reconciler does not exist yet (T-9)``. Hashing an untouched database twice
  and reporting PASS would be a green caused by the absence of the thing under
  test. So: a fabricated landing row is *detectable by a real digest today*,
  and the "unchanged across a reconciler run" assertion arrives with the
  reconciler.

RULING 12 -- the transition graph binds the owner, and a signature is frozen
-----------------------------------------------------------------------------
Two defects in ``keystone_proposal_status_transition``:

* It early-returned on an unchanged status **before** any role check, and
  ``review_writer`` holds ``UPDATE(decided_by, decided_at)`` -- so it could
  rewrite or NULL the signature on an already-decided proposal, and
  ``apply_writer`` would still apply it. ``decided_by`` and ``decided_at`` are
  now frozen once non-NULL for **every** principal, checked before anything
  else, and the role check moves ahead of the unchanged-status early return:
  for the three boundary roles, whose column grants reach only the decision and
  apply surface, an UPDATE that moves no status is never a legitimate
  statement. The owner keeps the early return, because it holds columns
  (``created_run``, ``rationale``) whose update is not a transition.
* KS002 / KS005 / KS008 bind the schema owner; KS004 did not, so the owner
  could move a proposal ``pending -> applied`` in one statement naming an
  arbitrary human decider. The owner is now bound to the same graph, and a
  decision it makes must name a decider like anyone else's. **This is defence
  in depth, not a boundary**: the owner can drop this trigger outright. It is
  here because an inconsistency between four rules that bind the owner and one
  that does not is worse than either choice, and because locally the owner is
  what a careless script connects as.

Project SQLSTATEs
-----------------
``KS001`` canonical UPDATE without a single-use, cited, correlated authorisation
``KS002`` proposal not born pending/sensitive_hold, born decided, or sensitive
          but not born held
``KS003`` audit_log or proposal_events actor outside the writing role's scope
``KS004`` illegal proposal status transition, or a frozen signature rewritten
``KS005`` proposal payload mutated after insert
``KS006`` budget reservation refused: no ledger row, or cap would be exceeded
``KS007`` illegal budget-reservation lifecycle change, or a direct mutator call
``KS008`` canonical row inserted with no ``entity_links`` provenance
``KS009`` ``entity_links`` row naming no ingested ``raw_records`` row

All are outside every built-in Postgres error class, so a test asserting one of
them cannot pass on an unrelated failure.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_single_use_citations"
down_revision: str | None = "0005_three_role_boundary"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECON_WRITER = "recon_writer"
APPLY_WRITER = "apply_writer"
REVIEW_WRITER = "review_writer"
ALL_ROLES = (RECON_WRITER, REVIEW_WRITER, APPLY_WRITER)

#: Statuses a proposal may be *born* in.
BIRTH_STATUSES = ("pending", "sensitive_hold")

#: ``proposal_events.event`` values that authorise a canonical mutation.
CANONICAL_EVENTS = ("applied", "rolled_back")

#: The CLOSED vocabulary of the reversal ledger: the two canonical mutations
#: plus one explicitly non-authorising label. No decision word appears here, so
#: ``apply_writer`` cannot write ``approved`` into the record of what happened
#: to a proposal. ``noted`` is kept so "an event label is not an authorisation"
#: stays provable on its own rather than by absence.
PROPOSAL_EVENT_VOCABULARY = ("applied", "rolled_back", "noted")

#: The SECURITY DEFINER ledger mutators, by signature. No role may call them.
LEDGER_MUTATORS = (
    "keystone_budget_charge(text, bigint)",
    "keystone_budget_release(text, bigint)",
)

DECISIONS = ("approved", "rejected")


def _sql_string_list(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _plain_list(values: Sequence[str]) -> str:
    """Render ``values`` for an error message -- no quotes, so embedding the
    result in a SQL string literal cannot terminate it early."""
    return ", ".join(values)


def _role_list() -> str:
    return _sql_string_list(ALL_ROLES)


# ---------------------------------------------------------------------------
# upgrade / downgrade
# ---------------------------------------------------------------------------
def upgrade() -> None:
    _add_status_txid()
    _pin_event_vocabulary()
    _make_citations_single_use()
    _install_single_use_citation_trigger()
    _install_transition_trigger()
    _install_born_pending_trigger()
    _install_proposal_event_actor_trigger()
    _install_link_provenance_trigger()
    _make_budget_triggers_owner_run()
    _revoke_ledger_mutator_execute()
    _revoke_temporary()


def downgrade() -> None:
    _restore_temporary()
    _restore_ledger_mutator_execute()
    _restore_0005_budget_triggers()
    op.execute("DROP TRIGGER IF EXISTS entity_links_require_raw_record ON entity_links")
    op.execute("DROP FUNCTION IF EXISTS keystone_require_link_provenance()")
    op.execute("DROP TRIGGER IF EXISTS proposal_events_actor_scope ON proposal_events")
    op.execute("DROP FUNCTION IF EXISTS keystone_proposal_event_actor_scope()")
    _restore_0005_born_pending_trigger()
    _restore_0005_transition_trigger()
    _restore_0005_citation_trigger()
    op.drop_index("uq_proposal_events_rolled_back_once", table_name="proposal_events")
    op.drop_index("uq_proposal_events_applied_once", table_name="proposal_events")
    op.drop_constraint("ck_proposal_events_event_vocabulary", "proposal_events", type_="check")
    op.drop_column("proposals", "status_txid")


# ---------------------------------------------------------------------------
# RULING 7 -- the citation is consumed
# ---------------------------------------------------------------------------
def _add_status_txid() -> None:
    """Record WHICH transaction last moved a proposal's status.

    The entities trigger is deferred to COMMIT, so it can only ever see
    end-of-transaction status. Without this column "the proposal is applied"
    and "the proposal is being applied right now, by me" are the same
    observation -- which is exactly how one approval became a standing licence.

    Maintained by ``keystone_proposal_status_transition`` and nowhere else, and
    forced to NULL at birth by ``keystone_proposal_born_pending``. No role's
    column grant names it: ``review_writer`` holds UPDATE on
    ``(status, decided_by, decided_at)`` and ``apply_writer`` on ``(status)``,
    so neither can write it; ``recon_writer``'s table-level INSERT on
    ``proposals`` *would* reach it, which is why birth overwrites it rather
    than trusting the grant surface.
    """
    op.add_column(
        "proposals",
        sa.Column(
            "status_txid",
            sa.BigInteger,
            nullable=True,
            comment=(
                "Transaction id of the most recent status change, stamped by "
                "keystone_proposal_status_transition and by nothing else. Lets the "
                "deferred citation trigger tell 'applied in THIS transaction' "
                "(a legitimate apply) from 'applied earlier' (a replayed citation)."
            ),
        ),
    )
    op.create_index("ix_proposals_status_txid", "proposals", ["status_txid"])


def _pin_event_vocabulary() -> None:
    """RULING 8. ``event`` is a closed set, so a label cannot impersonate a
    decision."""
    op.create_check_constraint(
        "ck_proposal_events_event_vocabulary",
        "proposal_events",
        f"event IN ({_sql_string_list(PROPOSAL_EVENT_VOCABULARY)})",
    )


def _make_citations_single_use() -> None:
    """One apply and one reversal per proposal, enforced by the index, forever.

    Partial rather than whole-column unique: ``noted`` events are ordinary
    ledger entries and may repeat. The two that *authorise* may not.
    """
    op.create_index(
        "uq_proposal_events_applied_once",
        "proposal_events",
        ["proposal_id"],
        unique=True,
        postgresql_where=sa.text("event = 'applied'"),
    )
    op.create_index(
        "uq_proposal_events_rolled_back_once",
        "proposal_events",
        ["proposal_id"],
        unique=True,
        postgresql_where=sa.text("event = 'rolled_back'"),
    )


def _install_single_use_citation_trigger() -> None:
    """0005's citation rule, with the standing licence removed.

    Every clause is load-bearing and each is proved separately in
    ``tests/schema/test_single_use_citation.py``:

    * ``pe.canonical_id = NEW.canonical_id`` -- the record names the row written
    * ``pe.before = OLD.current`` -- what was overwritten, so it can be restored
    * ``pe.after = NEW.current`` -- the ledger cannot misreport what was written
    * ``p.target_canonical_id = NEW.canonical_id`` -- ONE proposal, ONE entity
    * the event/status pairing below -- an ``applied`` proposal authorises a
      rollback and nothing else, and only a proposal that is ``approved`` (or
      that moved ``approved -> applied`` in this very transaction) authorises an
      apply
    * for a reversal, an ``applied`` event of the same proposal must have
      captured exactly the value being written back -- otherwise a "rollback"
      is a second arbitrary write

    The partial unique indexes are the other half of the same rule and cannot
    be expressed here: ``pe.txid`` must be the current transaction, so a replay
    needs a second ``applied`` row, and there can only ever be one.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_require_proposal_event() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            this_txid bigint := pg_current_xact_id()::text::bigint;
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
                WHERE pe.txid = this_txid
                  AND pe.canonical_id = NEW.canonical_id
                  AND pe.before = OLD.current
                  AND pe.after = NEW.current
                  AND p.target_canonical_id = NEW.canonical_id
                  AND (
                        (
                            pe.event = 'applied'
                            AND (
                                p.status::text = 'approved'
                                OR (p.status::text = 'applied'
                                    AND p.status_txid = this_txid)
                            )
                        )
                     OR (
                            pe.event = 'rolled_back'
                            AND (
                                p.status::text = 'applied'
                                OR (p.status::text = 'rolled_back'
                                    AND p.status_txid = this_txid)
                            )
                            AND EXISTS (
                                SELECT 1 FROM proposal_events ap
                                WHERE ap.proposal_id = p.id
                                  AND ap.event = 'applied'
                                  AND ap.canonical_id = NEW.canonical_id
                                  AND ap.before = NEW.current
                            )
                        )
                  )
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS001',
                    MESSAGE = 'canonical UPDATE on entities ' || NEW.canonical_id
                        || ' requires a same-transaction proposal_events row whose'
                        || ' canonical_id is that row, whose before/after equal the pre-'
                        || ' and post-update values of current, and which cites a proposal'
                        || ' whose target_canonical_id is that same row and whose status'
                        || ' still authorises the event: applied requires approved (or'
                        || ' approved -> applied in THIS transaction), rolled_back requires'
                        || ' applied (or applied -> rolled_back in THIS transaction) and'
                        || ' must restore the value the applied event captured. An'
                        || ' already-applied proposal is a SPENT citation: one approval'
                        || ' authorises one canonical write and one reversal, never more';
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )


def _restore_0005_citation_trigger() -> None:
    """Put back the 0005 body verbatim so downgrade is a true inverse."""
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
                        p.status::text IN ('approved', 'applied')
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
                        || ' proposal that is approved, applied and whose'
                        || ' target_canonical_id is that same row'
                        || ' (holds-before-writes: one approved proposal, one entity)';
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )


# ---------------------------------------------------------------------------
# RULING 12 -- the transition graph, the frozen signature, and the owner
# ---------------------------------------------------------------------------
def _install_transition_trigger() -> None:
    """0005's graph, plus a frozen signature and the owner inside the graph.

    Order inside the function is the fix, not decoration:

    1. ``decided_by``/``decided_at`` are frozen once non-NULL, for every
       principal, **before** anything can return early. ``review_writer`` holds
       ``UPDATE(decided_by, decided_at)`` and 0005 early-returned on an
       unchanged status before any check, so the signature on a decided
       proposal was rewritable -- and ``apply_writer`` would still have applied
       it. A decision nobody can un-sign is the only kind that attributes
       anything.
    2. ``status_txid`` is stamped here and nowhere else, so a caller-supplied
       value is always overwritten.
    3. The role check runs **ahead of the unchanged-status early return**: for
       the three boundary roles an UPDATE that moves no status can only be an
       attempt to rewrite a decision in place, because their column grants reach
       nothing else on this table. The owner keeps the early return -- it holds
       columns (``created_run``, ``rationale``) whose update is not a
       transition, and ``ON CONFLICT DO UPDATE`` on re-detection is one.
    4. The owner is then bound to the same transition graph (RULING 12), and
       must name a decider on a decision exactly as ``review_writer`` must.
       **This is defence in depth, not a boundary**: the owner can drop this
       trigger. It is here because four other rules bind the owner and the
       inconsistency was worse than either choice.
    """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION keystone_proposal_status_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            allowed boolean;
            moved boolean := NEW.status IS DISTINCT FROM OLD.status;
        BEGIN
            IF OLD.decided_by IS NOT NULL
               AND NEW.decided_by IS DISTINCT FROM OLD.decided_by
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS004',
                    MESSAGE = 'the signature on proposal ' || OLD.id || ' is frozen:'
                        || ' decided_by is ' || OLD.decided_by || ' and may not become '
                        || coalesce(NEW.decided_by, 'NULL')
                        || '; a decision that can be re-signed or unsigned attributes'
                        || ' nothing, and the apply path would still apply it';
            END IF;
            IF OLD.decided_at IS NOT NULL
               AND NEW.decided_at IS DISTINCT FROM OLD.decided_at
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS004',
                    MESSAGE = 'the signature on proposal ' || OLD.id || ' is frozen:'
                        || ' decided_at is ' || OLD.decided_at || ' and may not become '
                        || coalesce(NEW.decided_at::text, 'NULL')
                        || '; a decision that can be re-dated attributes nothing';
            END IF;

            IF moved THEN
                NEW.status_txid := pg_current_xact_id()::text::bigint;
            ELSE
                NEW.status_txid := OLD.status_txid;
            END IF;

            IF NOT moved THEN
                IF current_user IN ({_role_list()}) THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'KS004',
                        MESSAGE = 'role ' || current_user || ' may not UPDATE proposal '
                            || OLD.id || ' without moving its status (it is '
                            || OLD.status::text || '): the boundary roles hold column'
                            || ' grants over the decision and apply surface only, so an'
                            || ' update that moves nothing can only be an attempt to'
                            || ' rewrite a decision in place';
                END IF;
                RETURN NEW;
            END IF;

            IF current_user = '{REVIEW_WRITER}' THEN
                allowed := OLD.status::text IN ({_sql_string_list(BIRTH_STATUSES)})
                       AND NEW.status::text IN ({_sql_string_list(DECISIONS)});
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
                allowed := (OLD.status::text IN ({_sql_string_list(BIRTH_STATUSES)})
                            AND NEW.status::text IN ({_sql_string_list(DECISIONS)}))
                        OR (OLD.status::text = 'approved' AND NEW.status::text = 'applied')
                        OR (OLD.status::text = 'applied' AND NEW.status::text = 'rolled_back');
                IF allowed
                   AND NEW.status::text IN ({_sql_string_list(DECISIONS)})
                   AND (NEW.decided_by IS NULL OR NEW.decided_at IS NULL)
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = 'KS004',
                        MESSAGE = 'a decision must name its decider: ' || current_user
                            || ' must set decided_by and decided_at when moving proposal '
                            || OLD.id || ' to ' || NEW.status::text
                            || ' (the owner is bound to the transition graph as defence in'
                            || ' depth -- it can drop this trigger, so this is not a'
                            || ' boundary, but the graph should not have an exception)';
                END IF;
            END IF;

            IF NOT allowed THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS004',
                    MESSAGE = 'role ' || current_user || ' may not move proposal ' || OLD.id
                        || ' from ' || OLD.status::text || ' to ' || NEW.status::text
                        || '; separation of duties: {REVIEW_WRITER} decides'
                        || ' (pending|sensitive_hold -> approved|rejected), {APPLY_WRITER}'
                        || ' applies (approved -> applied, applied -> rolled_back),'
                        || ' {RECON_WRITER} only proposes, and the owner is bound to the'
                        || ' same graph as defence in depth';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )


def _restore_0005_transition_trigger() -> None:
    """Put back the 0005 body verbatim so downgrade is a true inverse."""
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


def _install_born_pending_trigger() -> None:
    """0005's birth rule, plus: ``status_txid`` is NULL at birth.

    ``recon_writer`` holds *table-level* INSERT on ``proposals``, so it could
    otherwise supply a ``status_txid`` of its own choosing at birth. Nothing it
    could write there would help -- a newborn proposal is ``pending`` and
    authorises nothing -- but a column the citation rule reads must not be
    caller-writable at any point in the row's life.
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
            NEW.status_txid := NULL;
            RETURN NEW;
        END;
        $$;
        """
    )


def _restore_0005_born_pending_trigger() -> None:
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


# ---------------------------------------------------------------------------
# RULING 10 -- the reversal ledger is actor-scoped like the audit log
# ---------------------------------------------------------------------------
def _install_proposal_event_actor_trigger() -> None:
    """The same rule ``audit_log`` has had since RULING 5, on the other ledger.

    ``apply_writer``'s INSERT grant on ``proposal_events`` includes ``actor``,
    and nothing scoped it -- so the automation wrote ``reviewer:alice`` into the
    record of what happened to a proposal. Machine roles write ``^system:``;
    only the reviewer role may look human.
    """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION keystone_proposal_event_actor_scope() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF current_user IN ('{RECON_WRITER}', '{APPLY_WRITER}')
               AND (NEW.actor IS NULL OR NEW.actor !~ '^system:')
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS003',
                    MESSAGE = 'role ' || current_user || ' may only write machine-scoped'
                        || ' proposal_events actors (matching ^system:), got '
                        || coalesce(NEW.actor, 'NULL')
                        || '; the automation may never attribute an entry in the reversal'
                        || ' ledger to a human reviewer';
            END IF;
            IF current_user = '{REVIEW_WRITER}'
               AND (NEW.actor IS NULL OR NEW.actor !~ '^reviewer:')
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS003',
                    MESSAGE = 'role {REVIEW_WRITER} may only write reviewer-scoped'
                        || ' proposal_events actors (matching ^reviewer:), got '
                        || coalesce(NEW.actor, 'NULL');
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER proposal_events_actor_scope
        BEFORE INSERT ON proposal_events
        FOR EACH ROW EXECUTE FUNCTION keystone_proposal_event_actor_scope();
        """
    )


# ---------------------------------------------------------------------------
# RULING 11 -- a link must name an ingested record
# ---------------------------------------------------------------------------
def _install_link_provenance_trigger() -> None:
    """Every ``entity_links`` row must name a real ``raw_records`` row.

    A constraint trigger rather than a FOREIGN KEY, and the reason is the
    schema's own: ``raw_records`` is append-only landing and duplicate natural
    keys within a generation are legitimate input (C11), so
    ``(source_id, natural_key, generation)`` has no unique key for a FK to
    reference. This is the closest real equivalent -- an existence check on the
    same ingested identity, deferred to end of transaction so ingestion order
    stays an implementation detail.

    Scope, stated so nobody has to guess: this proves a canonical row descends
    from a record that came through the **landing table**. It does not prove
    that record came through an adapter -- ``recon_writer`` still holds INSERT
    on ``raw_records``, so fabrication costs three INSERTs instead of two, with
    the third landing in ``raw_records``. A provenance floor, not a
    survivorship proof.

    0007 built the reader this used to cite before it existed:
    ``recon.suite.mirror`` hashes the landing and staging tables and the
    ``mirror-unchanged`` check is registered in the suite. The digest is
    implemented and exercised; the "unchanged across a reconciler run"
    assertion FAILS loudly as not-yet-implemented until T-9 lands the
    reconciler. See the RULING 11 section of the module docstring above.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_require_link_provenance() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM raw_records rr
                WHERE rr.source_id = NEW.source_id
                  AND rr.natural_key = NEW.source_key
                  AND rr.generation = NEW.generation
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS009',
                    MESSAGE = 'entity_links row for canonical ' || NEW.canonical_id
                        || ' names no ingested record: no raw_records row has'
                        || ' (source_id, natural_key, generation) = ('
                        || NEW.source_id || ', ' || NEW.source_key || ', '
                        || NEW.generation || '). A canonical entity must descend from a'
                        || ' record that actually came through the landing table';
            END IF;
            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER entity_links_require_raw_record
        AFTER INSERT OR UPDATE ON entity_links
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION keystone_require_link_provenance();
        """
    )


# ---------------------------------------------------------------------------
# RULING 9 -- the pg_temp escape
# ---------------------------------------------------------------------------
def _make_budget_triggers_owner_run() -> None:
    """The reserve/settle triggers become owner-run, so no role needs EXECUTE.

    0005 kept these SECURITY INVOKER for one good reason: inside a SECURITY
    DEFINER function ``current_user`` is the function owner, so a role check
    written there compares the owner against itself and passes for everyone.
    The cost was that every role needed EXECUTE on the SECURITY DEFINER ledger
    mutators -- and that grant was the pg_temp escape's payload.

    The role check therefore moves to ``session_user``, which is the
    authenticated LOGIN role and is unaffected by SECURITY DEFINER. That is
    sound here specifically because ``recon.db.role_connection`` authenticates
    *as* the role and never issues ``SET ROLE``, and because changing
    ``session_user`` needs ``SET SESSION AUTHORIZATION``, which requires
    superuser -- and all three boundary roles are NOSUPERUSER. The one case
    where the two differ is the owner doing ``SET ROLE recon_writer``, which
    reads as the owner; the owner is the sweeper principal and may reclaim
    anyway, so that is a widening of nothing.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_budget_reserve() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
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
        f"""
        CREATE OR REPLACE FUNCTION keystone_budget_settle() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
        DECLARE
            acting_role text := session_user;
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

            IF acting_role = '{RECON_WRITER}'
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


def _restore_0005_budget_triggers() -> None:
    """Put back the SECURITY INVOKER bodies verbatim so downgrade inverts."""
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


def _revoke_ledger_mutator_execute() -> None:
    """No role may call the ledger mutators, at any depth, from anywhere.

    ``pg_trigger_depth() = 0`` is kept inside both functions as the last layer,
    but it is no longer what stands between the capped party and its own
    ``spent_microusd``: the privilege does.
    """
    for signature in LEDGER_MUTATORS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        for role in ALL_ROLES:
            op.execute(f'REVOKE ALL ON FUNCTION {signature} FROM "{role}"')


def _restore_ledger_mutator_execute() -> None:
    for signature in LEDGER_MUTATORS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(
            f"GRANT EXECUTE ON FUNCTION {signature} TO "
            + ", ".join(f'"{role}"' for role in ALL_ROLES)
        )


def _revoke_temporary() -> None:
    """``pg_temp`` is not a schema any boundary role may define code in.

    TEMPORARY on a database is granted to PUBLIC by default, and that default
    is what made the escape possible: a role that can create a temp table can
    create a function in ``pg_temp``, attach it as a trigger, and execute
    arbitrary PL/pgSQL at ``pg_trigger_depth() = 1``. Revoked from PUBLIC and
    then explicitly from each role, so a future ``GRANT ... TO PUBLIC``
    elsewhere does not silently restore it for these three.
    """
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                EXECUTE format(
                    'REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
            END $$;
            """
        )
    )
    for role in ALL_ROLES:
        op.execute(
            sa.text(
                f"""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                        EXECUTE format(
                            'REVOKE TEMPORARY ON DATABASE %I FROM %I',
                            current_database(), '{role}');
                    END IF;
                END $$;
                """
            )
        )


def _restore_temporary() -> None:
    """Restore the Postgres default (TEMPORARY to PUBLIC) on downgrade."""
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
                EXECUTE format(
                    'GRANT TEMPORARY ON DATABASE %I TO PUBLIC', current_database());
            END $$;
            """
        )
    )
