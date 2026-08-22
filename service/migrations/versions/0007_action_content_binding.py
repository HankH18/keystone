"""One approval authorises one write OF THE CONTENT THAT WAS APPROVED.

Revision ID: 0007_action_content_binding
Revises: 0006_single_use_citations
Create Date: 2026-08-22

Round four made a citation single-use. It pinned WHICH entity a citation may
write and made the ledger report the write truthfully -- but nothing tied the
written value to the ``proposals.action`` a human actually approved. The red
team demonstrated it end to end: a proposal approved for ``{"set": {"grade":
"6"}}`` was cited to write entirely different content into its target entity,
and every rule in 0004-0006 was satisfied while it happened, because
``proposal_events.after`` merely had to equal whatever ``entities.current``
was being set to. An honest ledger of a dishonest write is still a dishonest
write.

So the guarantee shipped in 0006 was *"one approval authorises one write"*.
It must be *"one approval authorises one write OF THE CONTENT THAT WAS
APPROVED"*, and that is what this revision installs.

RULING 13 -- ``proposals.action`` is a CLOSED vocabulary
---------------------------------------------------------
An action that can be any JSON at all cannot be compared against anything, so
step one is to make it a value with a meaning. ``ck_proposals_action_vocabulary``
admits exactly one shape::

    {"set": {<path>: <value>, ...}}

and nothing else: not a scalar, not an array, not ``{"set": "anything"}``, not
``{"set": {...}, "unset": [...]}``, not an empty object. Exactly one top-level
key, named ``set``, whose value is a JSON object.

That shape is sufficient for every committed fix template, and the contract's
§6 fix-target table is the whole argument:

* ``C2`` writes ``payments.payment.external_ref``; ``C9`` writes
  ``appdb.enrollment.crm_deal_id``; grade-only ``C6`` writes
  ``crm.contact.grade``; lifecycle-only ``C6`` writes
  ``crm.contact.lifecycle_stage``; mixed ``C6`` and every ``C14`` write the
  disagreeing sensitive path; ``C4`` writes ``crm.contact.email``. Every one of
  those is a **single field set to a single value** -- a one-key ``set``.
* ``C1, C3, C5, C7, C8, C10, C11, C12, C13`` are evidence-only: "no field
  write". Their action is ``{"set": {}}``, and the binding below then means
  precisely what the table says -- an evidence-only proposal authorises a
  canonical write that changes nothing.

**``unset`` is deliberately NOT in the vocabulary.** No committed template
removes a path; adding a verb no template uses would be a widening with no
caller, and the CHECK is the thing that makes the comparison in RULING 14
total. If a future template genuinely needs it, it arrives with its own
migration, its own trigger arm and its own test -- not as a pre-granted
capability.

RULING 14 -- the trigger COMPUTES the approved result and compares
-------------------------------------------------------------------
``keystone_require_proposal_event`` no longer accepts an arbitrary
``NEW.current``. For the apply leg it now requires::

    NEW.current = OLD.current || (cited proposal).action -> 'set'

-- the approved action, applied to the value that was actually there. Nothing
else is authorised. A shallow ``||`` is exactly right for the contract's flat,
source-qualified path vocabulary (§2.4): ``{"set": {"crm.contact.grade": "7"}}``
sets that one key and leaves every other key of ``current`` untouched, so an
apply that silently drops an unrelated field is refused too.

For the reversal leg the content rule already existed and is kept verbatim: a
``rolled_back`` write must equal the value the proposal's own ``applied`` event
captured in ``before``. There is nothing to compute -- the authorised content
of a reversal is a recorded fact, not a function of the action.

A content mismatch raises its own SQLSTATE, ``KS010``, rather than reusing
``KS001``. That is not cosmetic: ``KS001`` already covers a dozen ways a
citation can be unauthorised, so a test asserting ``KS001`` for the content
rule would stay green if the content clause were deleted and some *other*
clause caught the attack. With ``KS010``, the only thing that can produce it is
the content comparison. The trigger therefore evaluates the full rule first
and, only when that fails, re-evaluates it **without** the content clause to
decide which error the caller gets: if everything except content was in order,
the refusal is ``KS010`` and says what was approved, what that authorised, and
what was attempted.

MINOR 15 -- PUBLIC loses EXECUTE on the SECURITY DEFINER trigger functions
---------------------------------------------------------------------------
0006 made ``keystone_budget_reserve`` and ``keystone_budget_settle``
``SECURITY DEFINER`` and revoked EXECUTE on the ledger mutators they call --
but left PUBLIC's default EXECUTE grant on the two trigger functions
themselves. Not exploitable: a function returning ``trigger`` cannot be invoked
by ``SELECT`` (Postgres refuses with ``42P13``/``0A000``), and PostgreSQL
checks EXECUTE on a trigger function when the trigger is *created*, not when it
fires -- so the legitimate path is unaffected. It is revoked because leaving it
is inconsistent with the revocation applied to ``charge``/``release`` in the
same migration, and an owner-run function carrying a default public grant is
the kind of thing that becomes exploitable the day someone changes its return
type.

Project SQLSTATEs (this revision adds the last one)
-----------------------------------------------------
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

All are outside every built-in Postgres error class, so a test asserting one of
them cannot pass on an unrelated failure.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_action_content_binding"
down_revision: str | None = "0006_single_use_citations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECON_WRITER = "recon_writer"
APPLY_WRITER = "apply_writer"
REVIEW_WRITER = "review_writer"
ALL_ROLES = (RECON_WRITER, REVIEW_WRITER, APPLY_WRITER)

#: The one verb ``proposals.action`` admits. See RULING 13 for why ``unset``
#: is absent: no committed fix template removes a path.
ACTION_VERB = "set"

ACTION_VOCABULARY_CONSTRAINT = "ck_proposals_action_vocabulary"

#: The SECURITY DEFINER *trigger* functions. Nothing may call them directly --
#: a trigger-returning function is not callable by ``SELECT`` at all -- and
#: PostgreSQL checks EXECUTE on them at ``CREATE TRIGGER`` time, never at fire
#: time, so revoking the default public grant costs the apply path nothing.
BUDGET_TRIGGER_FUNCTIONS = (
    "keystone_budget_reserve()",
    "keystone_budget_settle()",
)


def upgrade() -> None:
    _pin_action_vocabulary()
    _install_content_bound_citation_trigger()
    _revoke_budget_trigger_execute()


def downgrade() -> None:
    _restore_budget_trigger_execute()
    _restore_0006_citation_trigger()
    op.drop_constraint(ACTION_VOCABULARY_CONSTRAINT, "proposals", type_="check")


# ---------------------------------------------------------------------------
# RULING 13 -- the closed action vocabulary
# ---------------------------------------------------------------------------
def _pin_action_vocabulary() -> None:
    """``action`` is ``{"set": {...}}`` or it is not an action.

    Written as a ``CASE`` rather than a chain of ``AND``s because the later
    clauses are only well-typed on an object: ``jsonb - text`` raises
    ``22023`` on a scalar. SQL does not promise ``AND`` short-circuits, and a
    constraint that can raise a *different* error than the one it exists to
    raise is a constraint whose test can pass for the wrong reason.

    The constraint is created VALIDATED, so it binds every existing row as
    well as every future one, and it binds the schema owner exactly as it binds
    the three boundary roles -- a table invariant, not a grant.
    """
    op.execute(
        f"""
        ALTER TABLE proposals ADD CONSTRAINT {ACTION_VOCABULARY_CONSTRAINT} CHECK (
            CASE WHEN jsonb_typeof(action) = 'object'
                 THEN jsonb_exists(action, '{ACTION_VERB}')
                      AND jsonb_typeof(action -> '{ACTION_VERB}') = 'object'
                      AND action - '{ACTION_VERB}' = '{{}}'::jsonb
                 ELSE false
            END
        )
        """
    )
    op.execute(
        f"""
        COMMENT ON CONSTRAINT {ACTION_VOCABULARY_CONSTRAINT} ON proposals IS
        'The closed fix vocabulary: action is exactly {{"set": {{path: value, ...}}}}. '
        'An empty set object is the evidence-only proposal of contract §6 -- it '
        'authorises a canonical write that changes nothing. The entities trigger '
        'compares NEW.current against OLD.current || action->''set'', so this shape '
        'is what makes that comparison total (SQLSTATE KS010).'
        """
    )


# ---------------------------------------------------------------------------
# RULING 14 -- the trigger computes the approved result
# ---------------------------------------------------------------------------
#: Every clause of the authorisation EXCEPT the content comparison, shared by
#: the two evaluations below so they cannot drift apart. ``{extra}`` is where
#: the content clause is spliced in for the full evaluation.
_APPLIED_ARM = """
                    pe.event = 'applied'
                    AND (
                        p.status::text = 'approved'
                        OR (p.status::text = 'applied' AND p.status_txid = this_txid)
                    )
                    {extra}
"""

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
                    )
"""

#: The content clause itself, in one place: the write must be the approved
#: action applied to the value that was actually there.
_CONTENT_CLAUSE = """
                    AND NEW.current = (
                        OLD.current || coalesce(p.action -> 'set', '{}'::jsonb))
"""

_CORRELATION = """
                SELECT {selection}
                FROM proposal_events pe
                JOIN proposals p ON p.id = pe.proposal_id
                WHERE pe.txid = this_txid
                  AND pe.canonical_id = NEW.canonical_id
                  AND pe.before = OLD.current
                  AND pe.after = NEW.current
                  AND p.target_canonical_id = NEW.canonical_id
                  AND (
                        ({applied_arm})
                     OR ({rolled_back_arm})
                  )
"""


def _correlation_sql(selection: str, *, with_content: bool, reversals: bool = True) -> str:
    """Render the correlation query.

    ``with_content`` splices in the content clause; ``reversals`` keeps the
    rollback arm. The diagnostic evaluation drops both, because it exists only
    to answer "was this apply legitimate except for its content?".
    """
    return _CORRELATION.format(
        selection=selection,
        applied_arm=_APPLIED_ARM.format(extra=_CONTENT_CLAUSE if with_content else ""),
        rolled_back_arm=_ROLLED_BACK_ARM if reversals else "false",
    )


def _install_content_bound_citation_trigger() -> None:
    """0006's citation rule, plus: the CONTENT must be the approved content.

    Structure, and why it is two evaluations rather than one:

    1. The full rule -- every 0006 clause, plus ``NEW.current = OLD.current ||
       action->'set'`` on the apply arm. If it holds, the write is authorised
       and the trigger returns.
    2. Otherwise the *same* rule is evaluated again with the content clause
       removed, restricted to the apply arm. If **that** matches, the citation
       was in every respect legitimate and only the written content was not
       what the human approved -- so the caller gets ``KS010`` and a message
       naming the approved action, the write it authorised, and the write that
       was attempted.
    3. Otherwise the citation itself is unauthorised and the caller gets
       ``KS001``, unchanged from 0006.

    The two evaluations are generated from one template (``_CORRELATION``) so a
    future edit cannot make the diagnostic disagree with the rule. Step 2 is a
    diagnostic and never an authorisation: it runs only after step 1 has
    already refused, and every path out of it raises.

    The reversal arm carries no computed content clause because its content is
    a recorded fact rather than a function: ``rolled_back`` is authorised only
    when this proposal's own ``applied`` event captured exactly the value being
    written back. That clause is 0006's, kept verbatim.
    """
    full = _correlation_sql("1", with_content=True)
    # The diagnostic only ever explains an APPLY: a reversal has no computed
    # content, so a failed reversal is a plain KS001. ``INTO`` is written
    # immediately after the select list, which is where PL/pgSQL documents it.
    relaxed = _correlation_sql("p.action INTO approved_action", with_content=False, reversals=False)

    op.execute(
        f"""
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

            IF EXISTS ({full}) THEN
                RETURN NULL;
            END IF;

            {relaxed}
            LIMIT 1;

            IF FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS010',
                    MESSAGE = 'canonical UPDATE on entities ' || NEW.canonical_id
                        || ' writes content no human approved: the cited proposal''s'
                        || ' action is ' || approved_action::text || ', which authorises'
                        || ' exactly ' || (OLD.current
                             || coalesce(approved_action -> 'set', '{{}}'::jsonb))::text
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


def _restore_0006_citation_trigger() -> None:
    """Put back the 0006 body verbatim so downgrade is a true inverse."""
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


# ---------------------------------------------------------------------------
# MINOR 15 -- PUBLIC loses EXECUTE on the owner-run trigger functions
# ---------------------------------------------------------------------------
def _revoke_budget_trigger_execute() -> None:
    """Consistency with the revocation 0006 applied to charge/release.

    Revoked from PUBLIC *and then* from each role by name, so a future
    ``GRANT ... TO PUBLIC`` elsewhere cannot silently restore it for the three
    principals the boundary is about.
    """
    for signature in BUDGET_TRIGGER_FUNCTIONS:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        for role in ALL_ROLES:
            op.execute(f'REVOKE ALL ON FUNCTION {signature} FROM "{role}"')


def _restore_budget_trigger_execute() -> None:
    """Restore the Postgres default (EXECUTE to PUBLIC) on downgrade."""
    for signature in BUDGET_TRIGGER_FUNCTIONS:
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO PUBLIC")
