"""An `applied` event must describe a canonical write that actually happened.

Revision ID: 0008_ledger_write_honesty
Revises: 0007_action_content_binding
Create Date: 2026-08-22

Round five bound the apply leg's *content* to the approved action and stated the
guarantee as *"one human approval authorises one canonical write of the content
that was approved, and one reversal of it, and then nothing"*. Round six
demonstrated that the second half of that sentence was false, and the shape of
the hole is worth stating exactly, because it is the shape every rule up to here
kept missing.

Every rule in 0004-0007 fires on **UPDATE of ``entities``**. Nothing anywhere
required a ledger row to co-occur with a write. So ``apply_writer`` could INSERT
an ``applied`` ``proposal_events`` row describing a write that never happened,
choosing its ``before`` freely, and commit it -- no canonical UPDATE, therefore
no trigger, therefore no rule. The rollback arm then read that attacker-authored
``before`` as *the value to restore* and wrote it into the canonical row. The
reproduction, run against the live database before this revision::

    entity before attack: {"grade": "4"}
    LEG 1 (forge applied event, no UPDATE): ACCEPTED
    LEG 2 (rollback writes the forged value): ACCEPTED
    entity AFTER attack: {"grade": "ATTACKER-CHOSEN",
                          "crm.contact.email": "pwned@example.invalid"}

One approval bought one *arbitrary* canonical write, laundered through the
reversal leg. The apply leg's content binding (``KS010``) never ran, because no
apply ever happened.

Note the ``after`` value in that reproduction: it is the entity's own current
value, ``{"grade": "4"}``. That detail decides the design below. The obvious
formulation of the fix -- *"at end of transaction the cited entity's current
value must equal the event's ``after``"* -- does **not** catch this attack,
because the attacker simply sets ``after`` to what the row already holds and
forges only ``before``. The reproduction was written that way deliberately. The
rule that actually binds is not about the value; it is about the **write**.

RULING 15 -- an ``applied`` or ``rolled_back`` event must describe a canonical
write this transaction actually performed
-------------------------------------------------------------------------------
``proposal_events_describe_a_real_write`` is a DEFERRABLE INITIALLY DEFERRED
constraint trigger on ``proposal_events``. For every ``applied`` or
``rolled_back`` row it requires, at end of transaction:

* ``canonical_id`` is not NULL -- an event claiming a canonical write must say
  which row was written;
* that row exists in ``entities``;
* and that row carries **this transaction's** ``xmin`` -- i.e. this transaction
  really did write it.

DEFERRED because the pipeline's statement order is an implementation detail: the
apply path writes its ledger row before its canonical UPDATE, and a trigger that
demanded the reverse would break the product while looking like a boundary.

Why ``xmin`` and not a value comparison, spelled out because it is the whole
point: ``apply_writer`` is the only role holding INSERT on ``proposal_events``
(migration 0005's column grants) and holds **no INSERT on ``entities`` at all**
-- only ``UPDATE (current, updated_at)``. So for the role that can write the
ledger, "this row carries my transaction's xmin" means "I UPDATEd it here",
which is precisely the event the 0004-0007 rules guard. A forged event with no
accompanying UPDATE fails no matter what values it claims.

RULING 16 -- one canonical-mutating event per entity per transaction
----------------------------------------------------------------------
``uq_proposal_events_canonical_write_once`` is ``UNIQUE (canonical_id, txid)
WHERE event IN ('applied','rolled_back')``. Without it RULING 15 is satisfiable
by a *decoy*: forge an event for entity E and, in the same transaction, perform
one genuine canonical write of E citing a different proposal. E then carries
this transaction's xmin, RULING 15 passes, and the forged event commits with its
arbitrary ``before`` intact.

With it, an entity has at most one canonical-mutating event per transaction, so
the pairing between events and writes is one-to-one: every UPDATE of E in the
transaction must be authorised by *this* event (0004's correlation), which forces
``before = OLD.current``; and RULING 15 forces at least one such UPDATE to exist.
Together they make ``before`` provably the value the row held when the
transaction began -- which is the only property the reversal leg was ever
entitled to assume.

The cost is real and is stated rather than hidden: a transaction may no longer
apply two approvals to the same entity. Two approvals are two writes, so they
are two transactions. Applies to *different* entities still batch freely, which
is what ``test_every_row_of_a_multi_row_update_needs_its_own_record`` proves.

RULING 17 -- the reversal arm gets the content clause it never had
--------------------------------------------------------------------
RULING 15 and 16 close the forgery at its source; RULING 17 closes it at the
point of consumption, because either alone leaves a gap and neither is a
substitute for the other. The rollback arm required only ``ap.before =
NEW.current`` -- "the value I am writing back is one this proposal's apply
recorded". It never asked whether the row still holds what that apply left. So a
*stale* reversal was authorised: apply P1 (X -> Y), apply P2 (Y -> Z), then roll
back P1 and the canonical row goes to X, silently discarding an approved,
applied, unreversed write. It now also requires::

    ap.after = OLD.current

-- a reversal may only undo the write that is currently on top.

That clause gets its own SQLSTATE, ``KS012``, produced by exactly one comparison,
so a test for it cannot be satisfied by some other clause refusing. The
pre-existing ``ap.before = NEW.current`` clause keeps raising ``KS001``, which is
what the tests written for it in 0006 assert; ``KS012`` is scoped to the clause
this revision adds and nothing else.

MINOR 18 -- jsonb equality is not textual equality, and the mirror hashes text
--------------------------------------------------------------------------------
``'{"amount": 1}'::jsonb = '{"amount": 1.0}'::jsonb`` is TRUE, and their
``::text`` renderings differ (``{"amount": 1}`` vs ``{"amount": 1.0}``) -- jsonb
preserves the numeric's scale. So an approval for ``{"set": {"amount": 1}}``
could land as ``1.000`` with every constraint satisfied. That is not cosmetic:
``recon.suite.mirror`` hashes ``md5(t::text)``, determinism is graded, and two
runs of the same approved action could therefore produce different hashes while
the write boundary reported both as correct. Every jsonb equality in the
citation rule is now pinned ``a = b AND a::text = b::text``.

MINOR 19 -- ``entities.current`` must be a JSON object
--------------------------------------------------------
The evidence-only action ``{"set": {}}`` is documented as authorising a write
that changes nothing, and the trigger implements that as ``OLD.current ||
'{}'::jsonb``. For a non-object ``current`` that is not a no-op: ``'[1,2]'::jsonb
|| '{}'::jsonb`` is ``[1, 2, {}]``. ``ck_entities_current_is_object`` makes the
documented meaning true rather than approximately true, and makes the merge in
RULING 14 total.

MINOR 20 -- the ``KS010`` diagnostic no longer leaks the record it refuses
----------------------------------------------------------------------------
0007's ``KS010`` message embedded the cited action, the authorised result and
the attempted value -- the whole canonical record, which holds
``crm.contact.email``, legal names and ``dob``. That string is returned to the
client *and* written to the Postgres server log, so a refused write leaked the
personal data of the person it was about, to two places, on every attempt.

``keystone_differing_paths`` replaces it: the message names the **field paths**
that differ and never a value. The diagnostic keeps the only thing an operator
needs -- *which* field is not the approved one -- and the values stay in the
database where the privacy policy governs them. ``KS012`` is built the same way
from the start.

Project SQLSTATEs (this revision adds two)
--------------------------------------------
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
``KS010`` canonical UPDATE whose content is not the cited approval's action
          applied to the pre-update value
``KS011`` an ``applied``/``rolled_back`` event describing a canonical write this
          transaction did not perform
``KS012`` a reversal that does not restore the state the cited apply left

All are outside every built-in Postgres error class, so a test asserting one of
them cannot pass on an unrelated failure.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_ledger_write_honesty"
down_revision: str | None = "0007_action_content_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The ledger words that claim a canonical write. ``noted`` is deliberately not
#: one of them: it authorises nothing and describes nothing (0006, RULING 8).
CANONICAL_EVENTS = ("applied", "rolled_back")

CURRENT_IS_OBJECT_CONSTRAINT = "ck_entities_current_is_object"
CANONICAL_WRITE_ONCE_INDEX = "uq_proposal_events_canonical_write_once"
LEDGER_HONESTY_TRIGGER = "proposal_events_describe_a_real_write"


def upgrade() -> None:
    _pin_current_is_an_object()
    _install_differing_paths_helper()
    _make_canonical_events_singular_per_transaction()
    _install_ledger_honesty_trigger()
    _install_reversal_bound_citation_trigger()


def downgrade() -> None:
    _restore_0007_citation_trigger()
    op.execute(f"DROP TRIGGER IF EXISTS {LEDGER_HONESTY_TRIGGER} ON proposal_events")
    op.execute("DROP FUNCTION IF EXISTS keystone_require_event_describes_a_write()")
    op.execute(f"DROP INDEX IF EXISTS {CANONICAL_WRITE_ONCE_INDEX}")
    op.execute("DROP FUNCTION IF EXISTS keystone_differing_paths(jsonb, jsonb)")
    op.drop_constraint(CURRENT_IS_OBJECT_CONSTRAINT, "entities", type_="check")


# ---------------------------------------------------------------------------
# MINOR 19 -- current is a JSON object
# ---------------------------------------------------------------------------
def _pin_current_is_an_object() -> None:
    """``OLD.current || '{}'::jsonb`` is only a no-op on an object.

    Created VALIDATED, so it binds every existing row as well as every future
    one, and it binds the schema owner exactly as it binds the three boundary
    roles -- a table invariant, not a grant.
    """
    op.execute(
        f"""
        ALTER TABLE entities ADD CONSTRAINT {CURRENT_IS_OBJECT_CONSTRAINT}
        CHECK (jsonb_typeof(current) = 'object')
        """
    )
    op.execute(
        f"""
        COMMENT ON CONSTRAINT {CURRENT_IS_OBJECT_CONSTRAINT} ON entities IS
        'The canonical value is a JSON object of source-qualified field paths. '
        'The content binding computes the authorised write as OLD.current || '
        'action->''set'', and for a non-object current that APPENDS rather than '
        'merging -- ''[1,2]''::jsonb || ''{{}}''::jsonb is [1, 2, {{}}] -- so the '
        'evidence-only action would not mean what it is documented to mean.'
        """
    )


# ---------------------------------------------------------------------------
# MINOR 20 -- a diagnostic that names paths and never values
# ---------------------------------------------------------------------------
def _install_differing_paths_helper() -> None:
    """Which field paths differ, never what they hold.

    A pure function over its two arguments: it opens no table, so it can leak
    nothing the caller did not already pass in, and the strings it returns are
    contract §2.4 field paths (``crm.contact.email``) -- schema names, not
    personal data. That is why it keeps the ordinary PUBLIC EXECUTE default
    while 0007 revoked it from the owner-run trigger functions: those read and
    mutate the ledger, this reads nothing.

    The text comparison is deliberate and is the same rule as MINOR 18: two
    values that are equal as jsonb but render differently are a real difference,
    because the mirror digest hashes the text.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_differing_paths(expected jsonb, actual jsonb)
        RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
        DECLARE
            paths text;
        BEGIN
            IF jsonb_typeof(expected) IS DISTINCT FROM 'object'
               OR jsonb_typeof(actual) IS DISTINCT FROM 'object' THEN
                RETURN '(the whole value: one side is not a JSON object)';
            END IF;
            SELECT string_agg(k, ', ' ORDER BY k) INTO paths
            FROM (
                SELECT jsonb_object_keys(expected) AS k
                UNION
                SELECT jsonb_object_keys(actual) AS k
            ) keys
            WHERE (expected -> k) IS DISTINCT FROM (actual -> k)
               OR (expected -> k)::text IS DISTINCT FROM (actual -> k)::text;
            RETURN coalesce(
                paths,
                '(no field path differs: the two values are equal as jsonb and differ '
                || 'only in how a number is rendered, which the mirror digest hashes)');
        END;
        $$;
        """
    )
    op.execute(
        """
        COMMENT ON FUNCTION keystone_differing_paths(jsonb, jsonb) IS
        'Names the field paths at which two canonical values differ, and never '
        'their contents. The citation trigger''s KS010/KS012 messages are returned '
        'to the client and written to the server log, so they must not carry the '
        'canonical record (crm.contact.email, legal names, dob).'
        """
    )


# ---------------------------------------------------------------------------
# RULING 16 -- one canonical-mutating event per entity per transaction
# ---------------------------------------------------------------------------
def _make_canonical_events_singular_per_transaction() -> None:
    """The decoy defence: RULING 15 is only 1:1 if the event is unique.

    A partial UNIQUE index rather than a trigger clause on purpose: a unique
    index is enforced by the index itself at statement time, so two concurrent
    backends cannot both pass a check and then both insert. The single-use
    indexes of 0006 are the same mechanism for the same reason.
    """
    events = ", ".join(f"'{event}'" for event in CANONICAL_EVENTS)
    op.execute(
        f"""
        CREATE UNIQUE INDEX {CANONICAL_WRITE_ONCE_INDEX}
        ON proposal_events (canonical_id, txid)
        WHERE event IN ({events})
        """
    )
    op.execute(
        f"""
        COMMENT ON INDEX {CANONICAL_WRITE_ONCE_INDEX} IS
        'One canonical-mutating ledger event per entity per transaction. This is '
        'what makes the pairing between events and canonical UPDATEs one-to-one, '
        'and therefore what makes an applied event''s before provably the value '
        'the row held when the transaction began. Two approvals for one entity '
        'are two writes, so they are two transactions.'
        """
    )


# ---------------------------------------------------------------------------
# RULING 15 -- the event must describe a write that actually happened
# ---------------------------------------------------------------------------
def _install_ledger_honesty_trigger() -> None:
    """``applied``/``rolled_back`` require a canonical write in this transaction.

    ``e.xmin = pg_current_xact_id()::xid`` is the test for "this transaction
    wrote this row". The cast truncates the 64-bit full transaction id to the
    32 bits ``xmin`` actually stores, so the comparison is correct across
    wraparound rather than merely usually correct.

    Two properties of the comparison, stated because they are load-bearing:

    * it is TRUE for a row this transaction INSERTed as well as one it UPDATEd.
      That is not a hole for the boundary, because ``apply_writer`` -- the only
      role holding INSERT on ``proposal_events`` -- holds no INSERT on
      ``entities`` at all;
    * it is FALSE for a row written inside a SAVEPOINT, because the subxact gets
      its own xid. That direction is fail-closed: a legitimate apply wrapped in
      a savepoint would be refused loudly, never silently admitted. The apply
      path does not use savepoints (``recon.db.role_connection`` opens a plain
      transaction) and the end-to-end lifecycle test would go red the day it
      did.
    """
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_require_event_describes_a_write() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            written_here boolean;
        BEGIN
            IF NEW.canonical_id IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS011',
                    MESSAGE = 'proposal_events row ' || NEW.id || ' is a ''' || NEW.event
                        || ''' event naming no canonical row: an event that claims a'
                        || ' canonical write must say which row was written, or the'
                        || ' reversal leg has nothing to check it against';
            END IF;

            SELECT (e.xmin = pg_current_xact_id()::xid) INTO written_here
            FROM entities e
            WHERE e.canonical_id = NEW.canonical_id;

            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS011',
                    MESSAGE = 'proposal_events row ' || NEW.id || ' is a ''' || NEW.event
                        || ''' event for canonical row ' || NEW.canonical_id
                        || ', which does not exist: an event cannot describe a write to'
                        || ' a row that is not there';
            END IF;

            IF NOT written_here THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS011',
                    MESSAGE = 'proposal_events row ' || NEW.id || ' is a ''' || NEW.event
                        || ''' event for canonical row ' || NEW.canonical_id
                        || ', but this transaction performed no canonical write of that'
                        || ' row. A ledger event is a record of a write, not a substitute'
                        || ' for one: an event with no write behind it would hand the'
                        || ' reversal leg an author-chosen before value to restore';
            END IF;

            RETURN NULL;
        END;
        $$;
        """
    )
    events = ", ".join(f"'{event}'" for event in CANONICAL_EVENTS)
    op.execute(
        f"""
        CREATE CONSTRAINT TRIGGER {LEDGER_HONESTY_TRIGGER}
        AFTER INSERT OR UPDATE ON proposal_events
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW WHEN (NEW.event IN ({events}))
        EXECUTE FUNCTION keystone_require_event_describes_a_write();
        """
    )


# ---------------------------------------------------------------------------
# RULING 17 + MINOR 18 + MINOR 20 -- the citation rule
# ---------------------------------------------------------------------------
#: Every clause of the apply authorisation except its content comparison.
_APPLIED_ARM = """
                    pe.event = 'applied'
                    AND (
                        p.status::text = 'approved'
                        OR (p.status::text = 'applied' AND p.status_txid = this_txid)
                    )
                    {extra}
"""

#: Every clause of the reversal authorisation except the one RULING 17 adds.
#: ``{extra}`` is spliced INSIDE the EXISTS, next to the 0006 clause it joins.
_ROLLED_BACK_ARM = """
                    pe.event = 'rolled_back'
                    AND (
                        p.status::text = 'applied'
                        OR (p.status::text = 'rolled_back' AND p.status_txid = this_txid)
                    )
                    AND EXISTS (
                        SELECT 1 FROM proposal_events ap
                        WHERE ap.proposal_id = p.id
                          AND ap.event = 'applied'
                          AND ap.canonical_id = NEW.canonical_id
                          AND ap.before = NEW.current
                          AND ap.before::text = NEW.current::text
                          {extra}
                    )
"""

#: The apply leg's content clause: the write is the approved action applied to
#: the value that was actually there -- as jsonb AND as text (MINOR 18).
_APPLY_CONTENT_CLAUSE = """
                    AND NEW.current = (
                        OLD.current || coalesce(p.action -> 'set', '{}'::jsonb))
                    AND NEW.current::text = (
                        OLD.current || coalesce(p.action -> 'set', '{}'::jsonb))::text
"""

#: RULING 17: the reversal restores the write that is currently on top.
_REVERSAL_CONTENT_CLAUSE = """
                          AND ap.after = OLD.current
                          AND ap.after::text = OLD.current::text
"""

_CORRELATION = """
                SELECT {selection}
                FROM proposal_events pe
                JOIN proposals p ON p.id = pe.proposal_id
                WHERE pe.txid = this_txid
                  AND pe.canonical_id = NEW.canonical_id
                  AND pe.before = OLD.current
                  AND pe.before::text = OLD.current::text
                  AND pe.after = NEW.current
                  AND pe.after::text = NEW.current::text
                  AND p.target_canonical_id = NEW.canonical_id
                  AND (
                        ({applied_arm})
                     OR ({rolled_back_arm})
                  )
"""


def _correlation_sql(
    selection: str,
    *,
    apply_content: bool,
    reversal_content: bool,
    applies: bool = True,
    reversals: bool = True,
) -> str:
    """Render the correlation query from the one template.

    The full rule and both diagnostics come from here, so a future edit cannot
    make a diagnostic disagree with the rule it is diagnosing. Each diagnostic
    keeps exactly one arm and drops exactly one clause, which is what makes its
    SQLSTATE mean one thing.
    """
    return _CORRELATION.format(
        selection=selection,
        applied_arm=(
            _APPLIED_ARM.format(extra=_APPLY_CONTENT_CLAUSE if apply_content else "")
            if applies
            else "false"
        ),
        rolled_back_arm=(
            _ROLLED_BACK_ARM.format(extra=_REVERSAL_CONTENT_CLAUSE if reversal_content else "")
            if reversals
            else "false"
        ),
    )


def _install_reversal_bound_citation_trigger() -> None:
    """0007's rule, plus RULING 17, minus the personal data in its diagnostics.

    Three evaluations, in this order, and the order is the reason each SQLSTATE
    means one thing:

    1. the full rule. If it holds, the write is authorised and the trigger
       returns;
    2. otherwise the same rule with the **apply** arm only and its content
       clause dropped. If that matches, the citation was legitimate in every
       respect except the content written, and the caller gets ``KS010``;
    3. otherwise the same rule with the **reversal** arm only and RULING 17's
       clause dropped. If that matches, the reversal cites a real apply of this
       proposal but the row has moved on since, and the caller gets ``KS012``.
       The 0006 clause ``ap.before = NEW.current`` is *kept* in this
       evaluation, so a reversal writing a value the apply never captured is
       still ``KS001`` -- which is what 0006's tests assert, and they keep
       proving that clause rather than being re-labelled by this revision.
    4. otherwise ``KS001``, unchanged.

    Steps 2 and 3 are diagnostics and never authorisations: they run only after
    step 1 has already refused, and every path out of them raises.
    """
    full = _correlation_sql("1", apply_content=True, reversal_content=True)
    apply_diagnostic = _correlation_sql(
        "p.id, p.action INTO cited_proposal, approved_action",
        apply_content=False,
        reversal_content=True,
        reversals=False,
    )
    reversal_diagnostic = _correlation_sql(
        "p.id INTO cited_proposal",
        apply_content=True,
        reversal_content=False,
        applies=False,
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION keystone_require_proposal_event() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            this_txid bigint := pg_current_xact_id()::text::bigint;
            approved_action jsonb;
            cited_proposal bigint;
            apply_left jsonb;
        BEGIN
            IF NEW.canonical_id IS DISTINCT FROM OLD.canonical_id THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS001',
                    MESSAGE = 'canonical_id is immutable: rewriting it from '
                        || OLD.canonical_id || ' to ' || NEW.canonical_id
                        || ' would leave a reversal record that cannot restore the'
                        || ' row it claims to cover (holds-before-writes)';
            END IF;

            IF EXISTS ({full}) THEN
                RETURN NULL;
            END IF;

            {apply_diagnostic}
            LIMIT 1;

            IF FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS010',
                    MESSAGE = 'canonical UPDATE on entities ' || NEW.canonical_id
                        || ' writes content no human approved: proposal ' || cited_proposal
                        || ' authorises its approved action applied to the pre-update'
                        || ' value, which differs from the value attempted at field'
                        || ' path(s) ' || keystone_differing_paths(
                             OLD.current || coalesce(approved_action -> 'set', '{{}}'::jsonb),
                             NEW.current)
                        || '. The values themselves are deliberately not reported here:'
                        || ' this message reaches the client and the server log, and the'
                        || ' canonical record carries personal data. One approval'
                        || ' authorises one canonical write OF THE CONTENT THAT WAS'
                        || ' APPROVED -- a citation is not a blank cheque';
            END IF;

            {reversal_diagnostic}
            LIMIT 1;

            IF FOUND THEN
                SELECT ap.after INTO apply_left
                FROM proposal_events ap
                WHERE ap.proposal_id = cited_proposal AND ap.event = 'applied'
                LIMIT 1;
                RAISE EXCEPTION USING
                    ERRCODE = 'KS012',
                    MESSAGE = 'canonical UPDATE on entities ' || NEW.canonical_id
                        || ' is a reversal that does not restore the state the cited'
                        || ' apply left: proposal ' || cited_proposal || '''s applied'
                        || ' event recorded a post-write value differing from the row''s'
                        || ' current value at field path(s) '
                        || keystone_differing_paths(apply_left, OLD.current)
                        || '. The values are not reported here for the same reason as'
                        || ' KS010. A reversal may only undo the write it reverses; once'
                        || ' another approved write has landed on the row, that write is'
                        || ' the one on top and this reversal would silently discard it';
            END IF;

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
        END;
        $$;
        """
    )


def _restore_0007_citation_trigger() -> None:
    """Put back the 0007 body verbatim so downgrade is a true inverse."""
    op.execute(
        """
        CREATE OR REPLACE FUNCTION keystone_require_proposal_event() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            this_txid bigint := pg_current_xact_id()::text::bigint;
            approved_action jsonb;
        BEGIN
            IF NEW.canonical_id IS DISTINCT FROM OLD.canonical_id THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS001',
                    MESSAGE = 'canonical_id is immutable: rewriting it from '
                        || OLD.canonical_id || ' to ' || NEW.canonical_id
                        || ' would leave a reversal record that cannot restore the'
                        || ' row it claims to cover (holds-before-writes)';
            END IF;

            IF EXISTS (
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
                            AND NEW.current = (
                                OLD.current || coalesce(p.action -> 'set', '{}'::jsonb))
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
                RETURN NULL;
            END IF;

            SELECT p.action INTO approved_action
            FROM proposal_events pe
            JOIN proposals p ON p.id = pe.proposal_id
            WHERE pe.txid = this_txid
              AND pe.canonical_id = NEW.canonical_id
              AND pe.before = OLD.current
              AND pe.after = NEW.current
              AND p.target_canonical_id = NEW.canonical_id
              AND pe.event = 'applied'
              AND (
                    p.status::text = 'approved'
                    OR (p.status::text = 'applied' AND p.status_txid = this_txid)
              )
            LIMIT 1;

            IF FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS010',
                    MESSAGE = 'canonical UPDATE on entities ' || NEW.canonical_id
                        || ' writes content no human approved: the cited proposal''s'
                        || ' action is ' || approved_action::text || ', which authorises'
                        || ' exactly ' || (OLD.current
                             || coalesce(approved_action -> 'set', '{}'::jsonb))::text
                        || ', but current was set to ' || NEW.current::text
                        || '. One approval authorises one canonical write OF THE CONTENT'
                        || ' THAT WAS APPROVED -- a citation is not a blank cheque';
            END IF;

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
        END;
        $$;
        """
    )
