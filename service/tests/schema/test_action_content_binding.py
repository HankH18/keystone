"""Round five: one approval, one write, EXACTLY the approved content.

Rounds one to four built up to *one human approval authorises exactly one
canonical write*. They pinned which entity the write may touch, made the
citation single-use, and made the ledger report the write truthfully. What they
never did was tie the written **value** to the ``proposals.action`` a human
looked at and approved. The red team demonstrated the gap end to end: a
proposal approved for ``{"set": {"grade": "6"}}`` was cited to write entirely
different content into its target entity, and every rule in 0004-0006 was
satisfied while it happened.

This module proves the gap is closed, in the shape the previous rounds use:

* every negative asserts an **exact SQLSTATE** -- ``23514`` for the vocabulary
  CHECK, ``42501`` for a privilege, and ``KS010`` for a content mismatch, a
  project code that only the content comparison in
  ``keystone_require_proposal_event`` can produce. ``KS010`` is deliberately
  **not** ``KS001``: ``KS001`` already covers a dozen ways a citation can be
  unauthorised, so a content test asserting it would stay green if the content
  clause were deleted and some other clause happened to catch the attack;
* every negative is paired with a **positive control** doing, over the same
  connection path and as the same role, the legitimate thing the fix must not
  have broken;
* the headline attack is run against **committed** state, over the real roles,
  exactly as it was demonstrated.

The claim, stated once so the tests can be read against it: *one approval, one
write, exactly the approved content.*
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from recon.db import (
    ROLE_APPLY_WRITER,
    ROLE_RECON_WRITER,
    ROLE_REVIEW_WRITER,
    role_connection,
)
from tests.schema.conftest import (
    INSERT_RAW_RECORD,
    ROLES,
    TEST_TAG,
    RoleTxn,
    assert_insufficient_privilege,
    assert_sqlstate,
    raw_record_params,
)

#: A canonical write whose content is not the cited approval's action applied
#: to the pre-update value. Nothing else in the schema raises it.
CONTENT_NOT_APPROVED = "KS010"

#: A citation that is unauthorised for any of the 0004-0006 reasons.
CANONICAL_WRITE_UNAUTHORISED = "KS001"

CHECK_VIOLATION = "23514"

#: This module's landing generation, so its rows never collide with conftest
#: (99, 98), the citation tests (95) or the lifecycle test (96).
GENERATION = 94

#: The two SECURITY DEFINER *trigger* functions. 0006 made them owner-run and
#: revoked EXECUTE on the ledger mutators they call, but left PUBLIC's default
#: EXECUTE on the triggers themselves; 0007 revokes it.
BUDGET_TRIGGER_FUNCTIONS = (
    "keystone_budget_reserve()",
    "keystone_budget_settle()",
)

INSERT_EVENT = (
    "INSERT INTO proposal_events (proposal_id, canonical_id, event, before, after, actor) "
    "VALUES (:pid, :cid, :event, CAST(:before AS jsonb), CAST(:after AS jsonb), 'system:apply')"
)
REWRITE = (
    "UPDATE entities SET current = CAST(:after AS jsonb), updated_at = now() "
    "WHERE canonical_id = :cid"
)
SET_STATUS = "UPDATE proposals SET status = CAST(:s AS proposal_status) WHERE id = :pid"

INSERT_PROPOSAL = (
    "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence, "
    "created_run, target_canonical_id) "
    "VALUES (:cid, :fp, CAST(:action AS jsonb), 0.95, '{}'::jsonb, :run, :target) RETURNING id"
)


# ===========================================================================
# fixtures -- committed, because the demonstrated attack is a real apply
# ===========================================================================
@dataclass
class Approval:
    """One committed entity with provenance, and a proposal factory."""

    engine: Engine
    canonical_id: uuid.UUID
    conflict_id: int
    tag: str

    def approve(self, label: str, sets: dict[str, object]) -> int:
        """Commit a proposal for ``{"set": sets}`` and have a reviewer approve it.

        The decision goes over a real ``review_writer`` connection: an approval
        manufactured by an owner UPDATE would sidestep the transition graph and
        the apply below would then be proving nothing about the guarded path.
        """
        with self.engine.begin() as conn:
            proposal_id = conn.execute(
                text(INSERT_PROPOSAL),
                {
                    "cid": self.conflict_id,
                    "fp": f"{self.tag}-{label}",
                    "run": TEST_TAG,
                    "target": self.canonical_id,
                    "action": json.dumps({"set": sets}, sort_keys=True),
                },
            ).scalar_one()
        with role_connection(ROLE_REVIEW_WRITER) as conn:
            conn.execute(
                text(
                    "UPDATE proposals SET status = 'approved', decided_by = :who, "
                    "decided_at = now() WHERE id = :pid"
                ),
                {"pid": proposal_id, "who": f"reviewer:{TEST_TAG}"},
            )
        return proposal_id

    def set_current(self, value: str) -> None:
        """Seed the pre-update value by re-CREATING the row, not by UPDATE.

        An owner ``UPDATE entities SET current = ...`` would go through the
        citation trigger -- which binds the owner too -- so seeding a starting
        value that way is impossible by design. Canonical CREATION is the
        pipeline's, and is unguarded (migration 0004, MAJOR 6): the guarded
        verb is MUTATION. Deleting and re-inserting is therefore the honest way
        to arrange a pre-update value, and the ``entity_links`` provenance row
        the deferred KS008 trigger demands is already committed.
        """
        with self.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM entities WHERE canonical_id = :c"), {"c": self.canonical_id}
            )
            conn.execute(
                text(
                    "INSERT INTO entities (canonical_id, entity_type, current) "
                    "VALUES (:c, 'person', CAST(:v AS jsonb))"
                ),
                {"c": self.canonical_id, "v": value},
            )

    def current(self) -> str:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT current::text FROM entities WHERE canonical_id = :c"),
                {"c": self.canonical_id},
            ).scalar_one()

    def status(self, proposal_id: int) -> str:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT status FROM proposals WHERE id = :p"), {"p": proposal_id}
            ).scalar_one()


@pytest.fixture
def approval(owner_engine: Engine) -> Iterator[Approval]:
    """A fresh committed entity, its landing record, its link and its conflict."""
    tag = f"{TEST_TAG}-content-{uuid.uuid4()}"
    canonical_id = uuid.uuid5(uuid.NAMESPACE_URL, f"keystone/tests/schema/content/{tag}")

    with owner_engine.begin() as conn:
        conn.execute(INSERT_RAW_RECORD, raw_record_params("crm", tag, GENERATION))
        conn.execute(
            text(
                "INSERT INTO entity_links (canonical_id, source_id, source_key, source_ref, "
                "method, generation) VALUES (:c, 'crm', :k, :r, 'L1', :g)"
            ),
            {"c": canonical_id, "k": tag, "r": f"crm:contact:{tag}", "g": GENERATION},
        )
        conn.execute(
            text(
                "INSERT INTO entities (canonical_id, entity_type, current) "
                "VALUES (:c, 'person', '{}'::jsonb)"
            ),
            {"c": canonical_id},
        )
        conflict_id = conn.execute(
            text(
                "INSERT INTO conflicts (fingerprint, type, entity_refs, sources, "
                "disagreeing_fields, first_seen_run, last_seen_run) "
                "VALUES (:fp, 'field-disagreement', '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, "
                ":run, :run) RETURNING id"
            ),
            {"fp": tag, "run": TEST_TAG},
        ).scalar_one()

    yield Approval(engine=owner_engine, canonical_id=canonical_id, conflict_id=conflict_id, tag=tag)

    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM proposal_events WHERE canonical_id = :c"), {"c": canonical_id}
        )
        conn.execute(text("DELETE FROM proposals WHERE conflict_id = :c"), {"c": conflict_id})
        conn.execute(text("DELETE FROM conflicts WHERE id = :c"), {"c": conflict_id})
        conn.execute(text("DELETE FROM entities WHERE canonical_id = :c"), {"c": canonical_id})
        conn.execute(text("DELETE FROM entity_links WHERE canonical_id = :c"), {"c": canonical_id})
        conn.execute(
            text("DELETE FROM raw_records WHERE generation = :g AND natural_key = :k"),
            {"g": GENERATION, "k": tag},
        )


def _apply(
    proposal_id: int,
    canonical_id: uuid.UUID,
    before: str,
    after: str,
    *,
    commit: bool,
    move_status: bool = True,
) -> None:
    """The apply path exactly as the product runs it: ledger row, canonical
    write, status move -- one transaction, as ``apply_writer``.

    ``move_status=False`` is for the one case where the cited proposal is not
    ``approved``: ``apply_writer`` moving a *pending* proposal would be refused
    by the transition graph (KS004) before the citation trigger ever ran, and
    the test would then assert the wrong rule.
    """
    with role_connection(ROLE_APPLY_WRITER, commit=commit) as conn:
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": proposal_id,
                "cid": canonical_id,
                "event": "applied",
                "before": before,
                "after": after,
            },
        )
        conn.execute(text(REWRITE), {"cid": canonical_id, "after": after})
        if move_status:
            conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "applied"})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


# ===========================================================================
# MAJOR 1 -- the demonstrated attack, run verbatim
# ===========================================================================
def test_an_approval_for_grade_6_cannot_write_different_content(approval: Approval) -> None:
    """THE finding, executed exactly as it was demonstrated.

    A human approves ``{"set": {"grade": "6"}}``. The apply path then cites that
    approval -- correctly, in every other respect: same transaction, right
    entity, honest ``before``, honest ``after``, ``approved`` status, first and
    only use of the citation -- and writes ``{"grade": "12"}`` instead.

    Under 0006 this succeeded. Every rule was satisfied, because no rule looked
    at ``action`` at all. The ledger even recorded the theft accurately, which
    is what made it so hard to see: an honest record of a dishonest write.

    ``KS010`` is the assertion because it is producible by exactly one
    comparison in the schema. A ``KS001`` assertion here would survive the
    deletion of the content clause.
    """
    proposal_id = approval.approve("grade-6", {"grade": "6"})

    with pytest.raises(DBAPIError) as excinfo:
        _apply(
            proposal_id,
            approval.canonical_id,
            before="{}",
            after='{"grade": "12"}',
            commit=False,
        )
    assert_sqlstate(excinfo.value, CONTENT_NOT_APPROVED)

    assert approval.current() == "{}", "the unapproved write must have changed nothing"
    assert approval.status(proposal_id) == "approved", "the citation must remain unspent"


def test_the_same_approval_still_buys_the_write_it_approved(approval: Approval) -> None:
    """The positive control for the test above, on the same role and path.

    Same proposal, same approval, same apply -- writing the content the human
    actually approved. If the content rule were implemented as "refuse applies"
    this is the test that goes red, and a boundary that blocks the legitimate
    path is a failure, not a pass.
    """
    proposal_id = approval.approve("grade-6-honest", {"grade": "6"})

    _apply(proposal_id, approval.canonical_id, before="{}", after='{"grade": "6"}', commit=True)

    assert approval.current() == '{"grade": "6"}'
    assert approval.status(proposal_id) == "applied"


def test_one_approval_one_write_exactly_the_approved_content(approval: Approval) -> None:
    """The whole claim in one test, over real committed transactions.

    Approve one action. Every write that is not that action is refused with
    ``KS010`` -- more content, less content, different content, a reordered
    value. The one write that IS that action lands, and then the citation is
    spent and even the approved content cannot be written again.
    """
    approval.set_current('{"grade": "4", "lifecycle": "lead"}')
    proposal_id = approval.approve("the-claim", {"grade": "6"})
    approved_result = '{"grade": "6", "lifecycle": "lead"}'

    unapproved = [
        pytest.param('{"grade": "12", "lifecycle": "lead"}', id="a-different-value"),
        pytest.param('{"grade": "6"}', id="drops-an-unrelated-field"),
        pytest.param(
            '{"grade": "6", "lifecycle": "customer"}', id="changes-an-unapproved-field-too"
        ),
        pytest.param(
            '{"grade": "6", "lifecycle": "lead", "sneaked": true}', id="adds-an-extra-field"
        ),
    ]
    for case in unapproved:
        (attempt,) = case.values
        with pytest.raises(DBAPIError) as excinfo:
            _apply(
                proposal_id,
                approval.canonical_id,
                before='{"grade": "4", "lifecycle": "lead"}',
                after=attempt,
                commit=False,
            )
        assert_sqlstate(excinfo.value, CONTENT_NOT_APPROVED)
        assert approval.current() == '{"grade": "4", "lifecycle": "lead"}', case.id

    _apply(
        proposal_id,
        approval.canonical_id,
        before='{"grade": "4", "lifecycle": "lead"}',
        after=approved_result,
        commit=True,
    )
    assert json.loads(approval.current()) == json.loads(approved_result)

    # ... and the citation is spent, so even the approved content buys nothing
    # more. (23505 from the single-use index, which fires before the trigger.)
    with pytest.raises(DBAPIError) as excinfo:
        _apply(
            proposal_id,
            approval.canonical_id,
            before=approved_result,
            after=approved_result,
            commit=False,
        )
    assert getattr(excinfo.value.orig, "sqlstate", None) == "23505"


def test_a_set_merges_and_does_not_replace(approval: Approval) -> None:
    """The authorised write is ``OLD.current || action->'set'``, key by key.

    Contract §2.4's field paths are flat, source-qualified strings, so a
    shallow merge is the right semantics: approving ``crm.contact.grade``
    authorises a change to that key and to nothing else. This is the positive
    half -- an apply that leaves every unrelated key exactly where it was.
    """
    approval.set_current('{"crm.contact.grade": "4", "crm.contact.lifecycle_stage": "lead"}')
    proposal_id = approval.approve("merge", {"crm.contact.grade": "6"})

    _apply(
        proposal_id,
        approval.canonical_id,
        before='{"crm.contact.grade": "4", "crm.contact.lifecycle_stage": "lead"}',
        after='{"crm.contact.grade": "6", "crm.contact.lifecycle_stage": "lead"}',
        commit=True,
    )
    assert json.loads(approval.current()) == {
        "crm.contact.grade": "6",
        "crm.contact.lifecycle_stage": "lead",
    }


def test_an_evidence_only_proposal_authorises_no_content_change(approval: Approval) -> None:
    """Contract §6: C1/C3/C5/C7/C8/C10/C11/C12/C13 write no field at all.

    Their action is ``{"set": {}}``, and the binding then means exactly what
    the fix-target table says -- an evidence-only approval authorises a
    canonical write that changes nothing, and any content change citing it is
    ``KS010``. Without the binding, "no field write" was a note in a document
    and an evidence-only approval was a blank cheque like every other.
    """
    approval.set_current('{"grade": "4"}')
    proposal_id = approval.approve("evidence-only", {})

    with pytest.raises(DBAPIError) as excinfo:
        _apply(
            proposal_id,
            approval.canonical_id,
            before='{"grade": "4"}',
            after='{"grade": "6"}',
            commit=False,
        )
    assert_sqlstate(excinfo.value, CONTENT_NOT_APPROVED)

    # control: the no-op write it DOES authorise is accepted, so the rule is a
    # comparison and not a blanket refusal of evidence-only citations.
    _apply(
        proposal_id,
        approval.canonical_id,
        before='{"grade": "4"}',
        after='{"grade": "4"}',
        commit=True,
    )
    assert json.loads(approval.current()) == {"grade": "4"}


def test_the_content_rule_binds_the_schema_owner_too(approval: Approval) -> None:
    """An invariant of the table, not a per-role grant.

    Defence in depth rather than a boundary -- the owner can drop the trigger --
    but the owner is what a careless script connects as, and the four rules
    around this one already bind it.
    """
    proposal_id = approval.approve("owner", {"grade": "6"})

    with pytest.raises(DBAPIError) as excinfo, approval.engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(INSERT_EVENT),
                {
                    "pid": proposal_id,
                    "cid": approval.canonical_id,
                    "event": "applied",
                    "before": "{}",
                    "after": '{"grade": "12"}',
                },
            )
            conn.execute(text(REWRITE), {"cid": approval.canonical_id, "after": '{"grade": "12"}'})
            conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "applied"})
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, CONTENT_NOT_APPROVED)


def test_an_unauthorised_citation_is_still_ks001_not_ks010(approval: Approval) -> None:
    """The two codes name two different failures, and cannot be confused.

    Here the *content* is exactly right and the citation is not: the proposal
    was never approved. That must be ``KS001``. If the trigger reported
    ``KS010`` for everything it refused, the content assertions above would be
    satisfied by any refusal at all and would prove nothing.
    """
    with approval.engine.begin() as conn:
        proposal_id = conn.execute(
            text(INSERT_PROPOSAL),
            {
                "cid": approval.conflict_id,
                "fp": f"{approval.tag}-never-approved",
                "run": TEST_TAG,
                "target": approval.canonical_id,
                "action": '{"set": {"grade": "6"}}',
            },
        ).scalar_one()
    assert approval.status(proposal_id) == "pending"

    with pytest.raises(DBAPIError) as excinfo:
        _apply(
            proposal_id,
            approval.canonical_id,
            before="{}",
            after='{"grade": "6"}',
            commit=False,
            move_status=False,
        )
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED)


def test_a_reversal_still_restores_exactly_what_the_apply_overwrote(approval: Approval) -> None:
    """The reversal leg is content-bound by a recorded fact, not by the action.

    ``rolled_back`` is authorised only when this proposal's own ``applied``
    event captured exactly the value being written back -- 0006's rule, kept
    verbatim, and *not* replaced by ``OLD.current || action->'set'``, which
    would be the wrong content for a reversal by construction. Positive control
    plus the negative, so "kept" is asserted rather than assumed.
    """
    approval.set_current('{"grade": "4"}')
    proposal_id = approval.approve("reversal", {"grade": "6"})
    _apply(
        proposal_id,
        approval.canonical_id,
        before='{"grade": "4"}',
        after='{"grade": "6"}',
        commit=True,
    )

    with (  # negative: a reversal to anything else
        pytest.raises(DBAPIError) as excinfo,
        role_connection(ROLE_APPLY_WRITER, commit=False) as conn,
    ):
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": proposal_id,
                "cid": approval.canonical_id,
                "event": "rolled_back",
                "before": '{"grade": "6"}',
                "after": '{"grade": "99"}',
            },
        )
        conn.execute(text(REWRITE), {"cid": approval.canonical_id, "after": '{"grade": "99"}'})
        conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "rolled_back"})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED)

    with role_connection(ROLE_APPLY_WRITER) as conn:  # control: the real reversal
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": proposal_id,
                "cid": approval.canonical_id,
                "event": "rolled_back",
                "before": '{"grade": "6"}',
                "after": '{"grade": "4"}',
            },
        )
        conn.execute(text(REWRITE), {"cid": approval.canonical_id, "after": '{"grade": "4"}'})
        conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "rolled_back"})
    assert json.loads(approval.current()) == {"grade": "4"}


# ===========================================================================
# MAJOR 1 (a) -- the closed action vocabulary
# ===========================================================================
ILLEGAL_ACTIONS = (
    pytest.param('"anything"', id="a-bare-string"),
    pytest.param("42", id="a-number"),
    pytest.param("null", id="json-null"),
    pytest.param("true", id="a-boolean"),
    pytest.param('[{"set": {"grade": "6"}}]', id="an-array-of-actions"),
    pytest.param("{}", id="an-empty-object-with-no-verb"),
    pytest.param('{"set": "whatever-i-like"}', id="the-shape-the-old-tests-used"),
    pytest.param('{"set": ["grade", "6"]}', id="set-to-an-array"),
    pytest.param('{"set": null}', id="set-to-null"),
    pytest.param('{"unset": ["grade"]}', id="a-verb-no-fix-template-needs"),
    pytest.param('{"set": {"grade": "6"}, "unset": ["lifecycle"]}', id="a-smuggled-second-verb"),
    pytest.param('{"SET": {"grade": "6"}}', id="wrong-case"),
    pytest.param('{"exec": "drop table entities"}', id="an-invented-verb"),
)


@pytest.mark.parametrize("action", ILLEGAL_ACTIONS)
def test_recon_writer_cannot_insert_an_action_outside_the_vocabulary(
    role_txn: RoleTxn, approval: Approval, action: str
) -> None:
    """An action that is not comparable is not an action.

    The content rule can only be total if ``action`` has exactly one shape, so
    the vocabulary CHECK is the other half of MAJOR 1 rather than tidiness.
    ``{"unset": [...]}`` is refused on purpose: no committed fix template in
    contract §6 removes a path, and a verb with no caller is a widening waiting
    for one.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(INSERT_PROPOSAL),
            {
                "cid": approval.conflict_id,
                "fp": f"{approval.tag}-illegal-{uuid.uuid4()}",
                "run": TEST_TAG,
                "target": approval.canonical_id,
                "action": action,
            },
        )
    assert_sqlstate(excinfo.value, CHECK_VIOLATION)
    assert "ck_proposals_action_vocabulary" in str(excinfo.value.orig)


LEGAL_ACTIONS = (
    pytest.param('{"set": {}}', id="evidence-only"),
    pytest.param('{"set": {"crm.contact.grade": "6"}}', id="c6-grade-only"),
    pytest.param('{"set": {"crm.contact.lifecycle_stage": "customer"}}', id="c6-lifecycle-only"),
    pytest.param('{"set": {"payments.payment.external_ref": "pi_1"}}', id="c2"),
    pytest.param('{"set": {"appdb.enrollment.crm_deal_id": "D-1"}}', id="c9"),
    pytest.param('{"set": {"crm.contact.email": "a@example.invalid"}}', id="c4"),
    pytest.param(
        '{"set": {"crm.contact.first_name": "Ada", "crm.contact.last_name": "L"}}',
        id="more-than-one-path",
    ),
    pytest.param('{"set": {"crm.contact.marketing_consent": false}}', id="a-non-string-value"),
)


@pytest.mark.parametrize("action", LEGAL_ACTIONS)
def test_the_vocabulary_admits_every_committed_fix_template(
    role_txn: RoleTxn, approval: Approval, action: str
) -> None:
    """Positive control: every §6 fix-target row is expressible.

    One case per row of the contract's committed fix-target table, plus the
    evidence-only shape the "no field write" rows need. If the vocabulary were
    too narrow for a template, the reconciler could not write that proposal at
    all -- and this is the test that says so rather than T-9 discovering it.
    """
    with role_txn(ROLE_RECON_WRITER) as conn:
        stored = conn.execute(
            text(INSERT_PROPOSAL.replace("RETURNING id", "RETURNING action")),
            {
                "cid": approval.conflict_id,
                "fp": f"{approval.tag}-legal-{uuid.uuid4()}",
                "run": TEST_TAG,
                "target": approval.canonical_id,
                "action": action,
            },
        ).scalar_one()
    assert stored == json.loads(action)


def test_the_vocabulary_binds_the_schema_owner_too(approval: Approval) -> None:
    """A table CHECK, so no principal has a shape the trigger cannot compare."""
    with pytest.raises(DBAPIError) as excinfo, approval.engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(INSERT_PROPOSAL),
                {
                    "cid": approval.conflict_id,
                    "fp": f"{approval.tag}-owner-illegal",
                    "run": TEST_TAG,
                    "target": approval.canonical_id,
                    "action": '{"set": "anything"}',
                },
            )
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, CHECK_VIOLATION)


def test_the_action_vocabulary_constraint_is_validated(owner_engine: Engine) -> None:
    """Catalog assertion: the CHECK binds existing rows, not only new ones.

    A ``NOT VALID`` constraint would look identical in ``\\d proposals`` and
    would leave every row written before this migration outside the rule --
    including any the trigger later has to compare against.
    """
    with owner_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT contype, convalidated FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = 'proposals' AND c.conname = "
                "'ck_proposals_action_vocabulary'"
            )
        ).one_or_none()
    assert row is not None, "the action vocabulary constraint does not exist"
    assert row.contype == "c", row
    assert row.convalidated is True, "the constraint is NOT VALID: old rows escape it"


# ===========================================================================
# MINOR 3 -- PUBLIC loses EXECUTE on the SECURITY DEFINER trigger functions
# ===========================================================================
@pytest.mark.parametrize("function", BUDGET_TRIGGER_FUNCTIONS)
def test_public_holds_no_execute_on_the_budget_trigger_functions(
    owner_engine: Engine, function: str
) -> None:
    """0006 revoked the mutators and left the triggers' default public grant.

    Not exploitable on its own -- a function returning ``trigger`` cannot be
    called by ``SELECT`` -- but inconsistent with the revocation applied to
    ``charge``/``release`` in the same migration, and an owner-run function
    carrying a default public grant is one return-type change away from
    mattering. ``has_function_privilege`` answers for the effective privilege,
    PUBLIC's default included, so this cannot be satisfied by accident.
    """
    with owner_engine.connect() as conn:
        allowed = conn.execute(
            text("SELECT has_function_privilege('public', :fn, 'EXECUTE')"), {"fn": function}
        ).scalar_one()
    assert allowed is False, f"PUBLIC still holds EXECUTE on {function}"


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("function", BUDGET_TRIGGER_FUNCTIONS)
def test_no_boundary_role_holds_execute_on_the_budget_trigger_functions(
    owner_engine: Engine, role: str, function: str
) -> None:
    """The same revocation, per role, so a later ``GRANT ... TO PUBLIC`` cannot
    restore it for the three principals the boundary is about."""
    with owner_engine.connect() as conn:
        allowed = conn.execute(
            text("SELECT has_function_privilege(:role, :fn, 'EXECUTE')"),
            {"role": role, "fn": function},
        ).scalar_one()
    assert allowed is False, f"{role} still holds EXECUTE on {function}"


def test_the_budget_triggers_still_fire_without_that_grant(
    owner_engine: Engine, role_txn: RoleTxn
) -> None:
    """The positive control that makes the revocation safe rather than clever.

    PostgreSQL checks EXECUTE on a trigger function at ``CREATE TRIGGER`` time,
    never when it fires, so the reserve/settle path is unaffected -- but that is
    a claim about Postgres internals and this asserts it against the real
    server. Reserve as ``recon_writer`` (the BEFORE INSERT trigger charges the
    ledger) and settle (the BEFORE UPDATE trigger releases the unspent
    remainder), then read ``spent_microusd`` back: if either trigger had
    stopped firing, the ledger would not move.
    """
    scope = f"run:{TEST_TAG}-trigger-grant-{uuid.uuid4()}"
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) "
                "VALUES (:scope, 1000000, 0)"
            ),
            {"scope": scope},
        )
    try:
        with role_connection(ROLE_RECON_WRITER) as conn:
            reservation_id = conn.execute(
                text(
                    "INSERT INTO budget_reservations (scope, idempotency_key, "
                    "reserve_microusd) VALUES (:scope, :key, 400000) RETURNING id"
                ),
                {"scope": scope, "key": f"{TEST_TAG}-trigger-grant"},
            ).scalar_one()
            reserved = conn.execute(
                text("SELECT spent_microusd FROM budget_ledger WHERE scope = :s"), {"s": scope}
            ).scalar_one()
        assert reserved == 400_000, "the BEFORE INSERT reserve trigger did not fire"

        with role_connection(ROLE_RECON_WRITER) as conn:
            conn.execute(
                text(
                    "UPDATE budget_reservations SET actual_microusd = 150000, "
                    "state = 'settled' WHERE id = :rid"
                ),
                {"rid": reservation_id},
            )
        with owner_engine.connect() as conn:
            settled = conn.execute(
                text("SELECT spent_microusd FROM budget_ledger WHERE scope = :s"), {"s": scope}
            ).scalar_one()
        assert settled == 150_000, "the BEFORE UPDATE settle trigger did not fire"
    finally:
        with owner_engine.begin() as conn:
            conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": scope})
            conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": scope})


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("function", BUDGET_TRIGGER_FUNCTIONS)
def test_no_role_can_call_a_budget_trigger_function_directly(role: str, function: str) -> None:
    """Belt and braces, live: the call is refused, and 42501 is why.

    Two independent reasons exist for the refusal now -- no EXECUTE, and a
    trigger-returning function is not callable by ``SELECT`` at all. The
    privilege check runs first, so this asserts the layer 0007 added.
    """
    with pytest.raises(DBAPIError) as excinfo, role_connection(role, commit=False) as conn:
        conn.execute(text(f"SELECT {function}"))
    assert_insufficient_privilege(excinfo.value)
