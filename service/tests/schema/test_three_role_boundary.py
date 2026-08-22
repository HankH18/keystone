"""Round three: the property two roles could not express, proved role by role.

Two independent red-team passes broke the 0004 boundary five times, and every
break reduced to the same thing: with ``recon_writer`` proposing and
``apply_writer`` both approving *and* applying, "approve" and "apply" were the
same principal, so **the machine could approve its own work** -- which is the
property this project is graded on.

Migration ``0005_three_role_boundary`` splits the duties three ways and makes
the legal transition graph a property of the database. This module proves it.

The shape is uniform and deliberate, as in ``test_write_boundary_hardening``:

* every negative asserts an **exact SQLSTATE** -- ``42501`` for a privilege, or
  one of the project codes ``KS001``-``KS008``, none of which any unrelated
  failure can produce;
* every negative is paired with a **positive control** performing, over the
  same connection path and as the same role, the legitimate write the fix must
  not have broken. A negative that passed because the connection was dead would
  fail its control;
* and the whole thing ends with the lifecycle running end to end across all
  three roles, with real commits, so "secure" cannot quietly mean "broken".
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from recon.db import (
    ROLE_APPLY_WRITER,
    ROLE_RECON_WRITER,
    ROLE_REVIEW_WRITER,
    role_connection,
)
from tests.schema.conftest import (
    INSERT_RAW_RECORD,
    TEST_TAG,
    RoleTxn,
    assert_insufficient_privilege,
    assert_sqlstate,
    raw_record_params,
)

CANONICAL_WRITE_UNAUTHORISED = "KS001"
PROPOSAL_NOT_BORN_PENDING = "KS002"
AUDIT_ACTOR_OUT_OF_SCOPE = "KS003"
ILLEGAL_STATUS_TRANSITION = "KS004"
PROPOSAL_PAYLOAD_IMMUTABLE = "KS005"
ENTITY_WITHOUT_PROVENANCE = "KS008"

#: Every proposal column the payload-immutability trigger freezes.
FROZEN_PAYLOAD_ASSIGNMENTS = (
    pytest.param("conflict_id = conflict_id + 1", id="conflict_id"),
    pytest.param("fingerprint = 'fp-rewritten'", id="fingerprint"),
    # A *legal* action under 0007's closed vocabulary, deliberately: the claim
    # here is that the payload is frozen, and an action the CHECK would reject
    # anyway could let this pass on 23514 while KS005 was gone.
    pytest.param('action = \'{"set": {"crm.contact.grade": "12"}}\'::jsonb', id="action"),
    pytest.param("confidence = 0.99", id="confidence"),
    pytest.param("evidence = '{\"forged\": true}'::jsonb", id="evidence"),
    pytest.param("sensitive = NOT sensitive", id="sensitive"),
    pytest.param(
        "target_canonical_id = '00000000-0000-0000-0000-0000000000aa'::uuid",
        id="target_canonical_id",
    ),
)


@pytest.fixture
def pending_proposal(owner_engine: Engine, seeded_rows: dict[str, object]) -> Iterator[int]:
    """A freshly committed ``pending`` proposal, visible to every connection.

    Function-scoped and torn down, because most of these tests decide it and a
    decided proposal cannot be decided again -- which is the point.
    """
    fingerprint = f"{TEST_TAG}-3role-{uuid.uuid4()}"
    with owner_engine.begin() as conn:
        proposal_id = conn.execute(
            text(
                "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, "
                "evidence, created_run, target_canonical_id) "
                "VALUES (:cid, :fp, '{\"set\": {}}'::jsonb, 0.5, '{}'::jsonb, :run, :target) "
                "RETURNING id"
            ),
            {
                "cid": seeded_rows["conflict_id"],
                "fp": fingerprint,
                "run": TEST_TAG,
                "target": seeded_rows["canonical_id"],
            },
        ).scalar_one()
    yield proposal_id
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM proposal_events WHERE proposal_id = :p"), {"p": proposal_id})
        conn.execute(text("DELETE FROM proposals WHERE id = :p"), {"p": proposal_id})


APPROVE = (
    "UPDATE proposals SET status = 'approved', decided_by = 'reviewer:alice', "
    "decided_at = now() WHERE id = :pid"
)


# ===========================================================================
# RULING 1 -- three roles: propose, decide, apply
# ===========================================================================
def test_the_third_role_exists_and_is_not_the_owner(owner_engine: Engine) -> None:
    """The premise. A ``review_writer`` that owned tables would prove nothing."""
    with owner_engine.connect() as conn:
        attrs = conn.execute(
            text(
                "SELECT rolcanlogin, rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = :role"
            ),
            {"role": ROLE_REVIEW_WRITER},
        ).one_or_none()
        owned = (
            conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tableowner = :role"
                ),
                {"role": ROLE_REVIEW_WRITER},
            )
            .scalars()
            .all()
        )
    assert attrs is not None, "review_writer is missing from the cluster"
    can_login, privileged = attrs
    assert can_login is True, "review_writer must be a LOGIN role, not a group"
    assert privileged is False, "review_writer is superuser/bypassrls; grants would not apply"
    assert owned == [], f"review_writer must own no tables, but owns: {owned}"


def test_recon_writer_cannot_decide_its_own_proposal(
    role_txn: RoleTxn, pending_proposal: int, seeded_rows: dict[str, object]
) -> None:
    """THE graded property, stated directly: the proposer never decides.

    Enforced twice over -- ``recon_writer`` holds no UPDATE on ``proposals`` at
    all (this assertion), and the transition trigger refuses it every edge even
    if a future grant were widened.
    """
    with role_txn(ROLE_RECON_WRITER) as conn:  # control: proposing still works
        conn.execute(
            text(
                "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, "
                "evidence, created_run, target_canonical_id) "
                "VALUES (:cid, :fp, '{\"set\": {}}'::jsonb, 0.5, '{}'::jsonb, 'run-1', :target)"
            ),
            {
                "cid": seeded_rows["conflict_id"],
                "fp": f"{TEST_TAG}-propose-control-{uuid.uuid4()}",
                "target": seeded_rows["canonical_id"],
            },
        )

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text(APPROVE), {"pid": pending_proposal})
    assert_insufficient_privilege(excinfo.value)


@pytest.mark.parametrize("target", ["approved", "rejected"])
def test_apply_writer_cannot_approve_a_proposal(
    role_txn: RoleTxn, seeded_rows: dict[str, object], pending_proposal: int, target: str
) -> None:
    """The hole two roles could not close: the applier is not the decider.

    ``apply_writer`` still holds UPDATE on ``proposals.status`` -- it must, to
    move ``approved -> applied`` -- so a *grant* cannot express this. The
    transition trigger can: SQLSTATE ``KS004``, keyed on ``current_user``.
    """
    with role_txn(ROLE_APPLY_WRITER) as conn:  # control: the apply leg IS allowed
        result = conn.execute(
            text("UPDATE proposals SET status = 'applied' WHERE id = :pid"),
            {"pid": seeded_rows["approved_proposal_id"]},
        )
        assert result.rowcount == 1

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text("UPDATE proposals SET status = CAST(:s AS proposal_status) WHERE id = :pid"),
            {"pid": pending_proposal, "s": target},
        )
    assert_sqlstate(excinfo.value, ILLEGAL_STATUS_TRANSITION)


@pytest.mark.parametrize("column", ["decided_by", "decided_at"])
def test_apply_writer_cannot_sign_a_decision(
    role_txn: RoleTxn, seeded_rows: dict[str, object], column: str
) -> None:
    """It cannot even *name* the decision columns, so it cannot sign as anyone.

    Without this, ``apply_writer`` could stamp ``decided_by = 'reviewer:alice'``
    on a proposal while applying it, and the audit trail would show a human
    approving work no human saw.
    """
    value = "'reviewer:alice'" if column == "decided_by" else "now()"
    with role_txn(ROLE_APPLY_WRITER) as conn:  # control: status alone is fine
        conn.execute(
            text("UPDATE proposals SET status = 'applied' WHERE id = :pid"),
            {"pid": seeded_rows["approved_proposal_id"]},
        )

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(f"UPDATE proposals SET {column} = {value} WHERE id = :pid"),
            {"pid": seeded_rows["approved_proposal_id"]},
        )
    assert_insufficient_privilege(excinfo.value)


def test_review_writer_cannot_apply_what_it_approved(
    role_txn: RoleTxn, seeded_rows: dict[str, object], pending_proposal: int
) -> None:
    """The mirror image: the decider does not get to carry out its own decision.

    Approve and apply are separate roles in both directions, so neither half of
    the pair can complete the loop alone.
    """
    with role_txn(ROLE_REVIEW_WRITER) as conn:  # control: deciding IS its job
        result = conn.execute(text(APPROVE), {"pid": pending_proposal})
        assert result.rowcount == 1

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_REVIEW_WRITER) as conn:
        conn.execute(
            text(
                "UPDATE proposals SET status = 'applied', decided_by = 'reviewer:alice', "
                "decided_at = now() WHERE id = :pid"
            ),
            {"pid": seeded_rows["approved_proposal_id"]},
        )
    assert_sqlstate(excinfo.value, ILLEGAL_STATUS_TRANSITION)


def test_review_writer_cannot_re_decide_a_decided_proposal(
    role_txn: RoleTxn, seeded_rows: dict[str, object], pending_proposal: int
) -> None:
    """Decisions come from a hold state only: ``approved`` is not re-openable.

    Otherwise a reviewer role could flip an ``applied`` proposal back to
    ``rejected`` and orphan the canonical write that cites it.
    """
    with role_txn(ROLE_REVIEW_WRITER) as conn:  # control: from pending it works
        conn.execute(text(APPROVE), {"pid": pending_proposal})

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_REVIEW_WRITER) as conn:
        conn.execute(
            text(
                "UPDATE proposals SET status = 'rejected', decided_by = 'reviewer:alice', "
                "decided_at = now() WHERE id = :pid"
            ),
            {"pid": seeded_rows["approved_proposal_id"]},
        )
    assert_sqlstate(excinfo.value, ILLEGAL_STATUS_TRANSITION)


def test_a_decision_must_name_its_decider(role_txn: RoleTxn, pending_proposal: int) -> None:
    """An unsigned decision is indistinguishable from an automated one.

    ``review_writer`` exists precisely so that a human is attributable for every
    approval, so a decision that leaves ``decided_by``/``decided_at`` NULL is
    refused rather than silently recorded as nobody's.
    """
    with role_txn(ROLE_REVIEW_WRITER) as conn:  # control: signed, it works
        conn.execute(text(APPROVE), {"pid": pending_proposal})

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_REVIEW_WRITER) as conn:
        conn.execute(
            text("UPDATE proposals SET status = 'approved' WHERE id = :pid"),
            {"pid": pending_proposal},
        )
    assert_sqlstate(excinfo.value, ILLEGAL_STATUS_TRANSITION)


@pytest.mark.parametrize("birth", ["pending", "sensitive_hold"])
@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_review_writer_may_decide_from_either_hold_state(
    owner_engine: Engine, seeded_rows: dict[str, object], birth: str, decision: str
) -> None:
    """Positive control over the whole decision half of the graph.

    ``sensitive_hold`` must remain decidable *by a human*: R15 forbids sensitive
    fields auto-applying, not a reviewer approving them. A rule that made held
    proposals undecidable would be a deadlock, not a boundary.
    """
    fingerprint = f"{TEST_TAG}-decide-{birth}-{decision}-{uuid.uuid4()}"
    with owner_engine.begin() as conn:
        proposal_id = conn.execute(
            text(
                "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, "
                "evidence, created_run, status, sensitive, target_canonical_id) "
                "VALUES (:cid, :fp, '{\"set\": {}}'::jsonb, 0.5, '{}'::jsonb, :run, "
                "CAST(:status AS proposal_status), :sensitive, :target) RETURNING id"
            ),
            {
                "cid": seeded_rows["conflict_id"],
                "fp": fingerprint,
                "run": TEST_TAG,
                "status": birth,
                "sensitive": birth == "sensitive_hold",
                "target": seeded_rows["canonical_id"],
            },
        ).scalar_one()
    try:
        with role_connection(ROLE_REVIEW_WRITER, commit=False) as conn:
            stored = conn.execute(
                text(
                    "UPDATE proposals SET status = CAST(:s AS proposal_status), "
                    "decided_by = 'reviewer:alice', decided_at = now() "
                    "WHERE id = :pid RETURNING status, decided_by"
                ),
                {"pid": proposal_id, "s": decision},
            ).one()
        assert stored.status == decision
        assert stored.decided_by == "reviewer:alice"
    finally:
        with owner_engine.begin() as conn:
            conn.execute(text("DELETE FROM proposals WHERE id = :p"), {"p": proposal_id})


def test_apply_writer_may_walk_the_apply_leg(
    role_txn: RoleTxn, seeded_rows: dict[str, object]
) -> None:
    """Positive control over the apply half: approved -> applied -> rolled_back."""
    with role_txn(ROLE_APPLY_WRITER) as conn:
        applied = conn.execute(
            text("UPDATE proposals SET status = 'applied' WHERE id = :pid RETURNING status"),
            {"pid": seeded_rows["approved_proposal_id"]},
        ).scalar_one()
        assert applied == "applied"
        reverted = conn.execute(
            text("UPDATE proposals SET status = 'rolled_back' WHERE id = :pid RETURNING status"),
            {"pid": seeded_rows["approved_proposal_id"]},
        ).scalar_one()
    assert reverted == "rolled_back"


# ===========================================================================
# RULING 1 -- the proposal payload is immutable after insert
# ===========================================================================
@pytest.mark.parametrize("assignment", FROZEN_PAYLOAD_ASSIGNMENTS)
def test_a_proposal_payload_cannot_be_rewritten(
    owner_engine: Engine, pending_proposal: int, assignment: str
) -> None:
    """The red team rewrote a pending proposal, then approved the rewrite.

    Exercised as the **owner** because this is an invariant of the table, not a
    privilege: if it were a grant it would evaporate for exactly the principal a
    careless script connects as. The role-level half -- that no deciding role's
    column grant even reaches these columns -- is asserted separately below.
    """
    with owner_engine.connect() as conn:  # control: a decision IS an allowed UPDATE
        transaction = conn.begin()
        conn.execute(text(APPROVE), {"pid": pending_proposal})
        transaction.rollback()

    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(f"UPDATE proposals SET {assignment} WHERE id = :pid"),
                {"pid": pending_proposal},
            )
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, PROPOSAL_PAYLOAD_IMMUTABLE)


def test_the_rewrite_then_self_approve_attack_is_dead(
    owner_engine: Engine, pending_proposal: int
) -> None:
    """The attack executed literally, in one transaction, as the red team ran it.

    Rewrite a pending proposal's ``action`` and ``confidence`` to something that
    would auto-apply, then approve it. The rewrite is refused before the
    approval is ever reached; the role split means even a successful rewrite
    would have had to find a second principal to approve it.
    """
    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(
                    "UPDATE proposals SET action = "
                    '\'{"set": {"crm.contact.grade": "12"}}\'::jsonb, '
                    "confidence = 1.0 WHERE id = :pid"
                ),
                {"pid": pending_proposal},
            )
            conn.execute(text(APPROVE), {"pid": pending_proposal})
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, PROPOSAL_PAYLOAD_IMMUTABLE)


def test_no_deciding_role_may_name_a_payload_column(owner_engine: Engine) -> None:
    """The catalog view of the same fact, so a widened grant is caught early."""
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT grantee, column_name FROM information_schema.column_privileges "
                "WHERE table_name = 'proposals' AND privilege_type = 'UPDATE' "
                "AND grantee = ANY(:roles)"
            ),
            {"roles": [ROLE_RECON_WRITER, ROLE_REVIEW_WRITER, ROLE_APPLY_WRITER]},
        ).all()
    granted: dict[str, set[str]] = {}
    for grantee, column in rows:
        granted.setdefault(grantee, set()).add(column)

    assert granted == {
        ROLE_REVIEW_WRITER: {"status", "decided_by", "decided_at"},
        ROLE_APPLY_WRITER: {"status"},
    }, granted


# ===========================================================================
# RULING 3 -- a canonical write cites an APPROVED proposal for THAT entity
# ===========================================================================
INSERT_EVENT = (
    "INSERT INTO proposal_events (proposal_id, canonical_id, event, before, after, actor) "
    "VALUES (:pid, :canonical_id, 'applied', CAST(:before AS jsonb), CAST(:after AS jsonb), "
    "'system:apply')"
)
REWRITE = (
    "UPDATE entities SET current = CAST(:after AS jsonb), updated_at = now() "
    "WHERE canonical_id = :cid"
)


def test_a_pending_proposal_cannot_authorise_a_canonical_write(
    role_txn: RoleTxn, canonical_pair: dict[str, object]
) -> None:
    """THE bypass RULING 3 exists for, run as the red team ran it.

    0004's correlation was entity-only, so a perfectly well-formed reversal
    record citing a proposal that was and stayed ``pending`` authorised the
    write. Holds-before-writes means nothing if the hold need not be released.

    ``pending_a`` targets **this** entity and satisfies every other clause of
    the correlation, so the status clause is the only thing that can be doing
    the refusing. (An earlier draft cited a pending proposal that also pointed
    at a different entity; a sabotage run showed the test stayed green with the
    status clause deleted, because the target clause was catching it.)
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": canonical_pair["pending_a"],
                "canonical_id": canonical_pair["a"],
                "before": canonical_pair["current_a"],
                "after": '{"grade": "tampered"}',
            },
        )
        conn.execute(text(REWRITE), {"cid": canonical_pair["a"], "after": '{"grade": "tampered"}'})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED)


def test_a_proposal_for_another_entity_cannot_authorise_this_one(
    role_txn: RoleTxn, canonical_pair: dict[str, object]
) -> None:
    """One approved proposal authorises exactly one entity.

    Here the cited proposal is genuinely ``approved`` -- only its
    ``target_canonical_id`` names the other row. That single clause is what
    turns the mass rewrite from "detected" into "unrepresentable": N entities
    now require N distinct approved proposals.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": canonical_pair["proposal_b"],  # approved, but targets B
                "canonical_id": canonical_pair["a"],
                "before": canonical_pair["current_a"],
                "after": '{"grade": "tampered"}',
            },
        )
        conn.execute(text(REWRITE), {"cid": canonical_pair["a"], "after": '{"grade": "tampered"}'})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED)


def test_the_reversal_record_cannot_misreport_what_was_written(
    role_txn: RoleTxn, canonical_pair: dict[str, object]
) -> None:
    """``after`` must equal the value actually written.

    Without this clause the ledger records a plausible-looking change while the
    row holds something else entirely, and a rollback "restores" a state that
    was never overwritten.

    The control writes ``approved_set`` -- the content ``proposal_a`` is
    approved for -- because since 0007 a canonical write must be exactly the
    approved action applied to the old value. Writing anything else would make
    the *content* rule (KS010) the thing accepting or refusing here, and this
    test is about the ``after`` clause.
    """
    with role_txn(ROLE_APPLY_WRITER) as conn:  # control: honest after value works
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": canonical_pair["proposal_a"],
                "canonical_id": canonical_pair["a"],
                "before": canonical_pair["current_a"],
                "after": canonical_pair["approved_set"],
            },
        )
        conn.execute(
            text(REWRITE), {"cid": canonical_pair["a"], "after": canonical_pair["approved_set"]}
        )
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": canonical_pair["proposal_a"],
                "canonical_id": canonical_pair["a"],
                "before": canonical_pair["current_a"],
                "after": '{"grade": "what-the-ledger-claims"}',
            },
        )
        conn.execute(
            text(REWRITE), {"cid": canonical_pair["a"], "after": '{"grade": "what-was-written"}'}
        )
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED)


@pytest.mark.parametrize("column", ["entity_type", "created_at", "canonical_id"])
def test_apply_writer_cannot_rewrite_anything_but_current(
    role_txn: RoleTxn, canonical_pair: dict[str, object], column: str
) -> None:
    """The reversal record captures ``current``; anything else is unrestorable.

    ``apply_writer`` could previously rewrite ``entity_type`` and ``created_at``
    in the same statement as a legitimate ``current`` change, and the reversal
    record -- which stores only ``current`` -- could never put them back.
    """
    value = {
        "entity_type": "'tampered'",
        "created_at": "now()",
        "canonical_id": "gen_random_uuid()",
    }
    with role_txn(ROLE_APPLY_WRITER) as conn:  # control: current/updated_at are granted
        conn.execute(
            text(
                "UPDATE entities SET current = current, updated_at = now() WHERE canonical_id = :c"
            ),
            {"c": canonical_pair["a"]},
        )

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(f"UPDATE entities SET {column} = {value[column]} WHERE canonical_id = :c"),
            {"c": canonical_pair["a"]},
        )
    assert_insufficient_privilege(excinfo.value)


def test_the_entities_update_grant_is_exactly_current_and_updated_at(
    owner_engine: Engine,
) -> None:
    """Read from ``column_privileges`` -- ``role_table_grants`` cannot see this.

    A column grant does not appear in ``role_table_grants`` at all, which is why
    an earlier pass concluded the column scoping "could not be tested". It can;
    that view is simply the wrong catalog.
    """
    with owner_engine.connect() as conn:
        columns = set(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.column_privileges "
                    "WHERE grantee = :role AND table_name = 'entities' "
                    "AND privilege_type = 'UPDATE'"
                ),
                {"role": ROLE_APPLY_WRITER},
            )
            .scalars()
            .all()
        )
    assert columns == {"current", "updated_at"}, columns


# ===========================================================================
# RULING 4 -- canonical creation must be traceable
# ===========================================================================
INSERT_ENTITY = (
    "INSERT INTO entities (canonical_id, entity_type, current) "
    "VALUES (:cid, 'person', '{\"grade\": \"invented\"}'::jsonb)"
)
INSERT_LINK = (
    "INSERT INTO entity_links (canonical_id, source_id, source_key, source_ref, method, "
    "generation) VALUES (:cid, 'crm', :key, :ref, 'L1', 97)"
)

#: Migration 0006 (RULING 11) makes the link itself carry provenance: it must
#: name a ``raw_records`` row with the same ``(source_id, natural_key,
#: generation)``. The positive control below therefore ingests first, exactly as
#: the pipeline does.
PROVENANCE_GENERATION = 97


def _provenance_args(tag: str) -> dict[str, object]:
    canonical_id = uuid.uuid5(uuid.NAMESPACE_URL, f"keystone/tests/schema/provenance/{tag}")
    return {
        "cid": canonical_id,
        "key": f"{TEST_TAG}-provenance-{tag}",
        "ref": f"crm:contact:{TEST_TAG}-provenance-{tag}",
    }


def test_a_canonical_row_conjured_from_nothing_is_rejected(role_txn: RoleTxn) -> None:
    """``recon_writer`` could INSERT an entity with arbitrary ``current``.

    That is a canonical row no source supports -- and the apply path would then
    happily "fix" it on the strength of a proposal, laundering invented state
    into the graded output. The DEFERRED trigger requires at least one
    ``entity_links`` row for the id by end of transaction.

    ``SET CONSTRAINTS ALL IMMEDIATE`` forces the deferred check inside the test
    transaction, so the assertion is real even though we roll back.
    """
    args = _provenance_args("orphan")
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text(INSERT_ENTITY), {"cid": args["cid"]})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, ENTITY_WITHOUT_PROVENANCE)


def test_a_canonical_row_that_descends_from_a_source_record_is_accepted(
    role_txn: RoleTxn,
) -> None:
    """Positive control: entity resolution's real output still lands.

    Deliberately writes the entity *before* its link, because the rule is
    deferred to end-of-transaction precisely so ER need not care about order.
    A trigger that demanded the link first would break the pipeline while
    looking like a boundary.

    Scope, stated plainly: this proves the canonical row descends from an
    ingested source record. It does **not** prove the surviving field values are
    the right ones -- survivorship correctness is the pipeline's job and is
    graded by ``recon.suite`` against ``golden/``.
    """
    args = _provenance_args("linked")
    with role_txn(ROLE_RECON_WRITER) as conn:
        # RULING 11 / migration 0006: the link must name an ingested record, so
        # the pipeline's real order is ingest -> resolve. The canonical row
        # still comes before its link, which is the ordering claim above.
        conn.execute(
            INSERT_RAW_RECORD,
            raw_record_params("crm", str(args["key"]), PROVENANCE_GENERATION),
        )
        conn.execute(text(INSERT_ENTITY), {"cid": args["cid"]})
        conn.execute(text(INSERT_LINK), args)
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        stored = conn.execute(
            text("SELECT count(*) FROM entities WHERE canonical_id = :cid"), {"cid": args["cid"]}
        ).scalar_one()
    assert stored == 1


def test_the_provenance_rule_binds_the_schema_owner_too(owner_engine: Engine) -> None:
    """ "Canonical rows cannot be conjured" is an invariant, not a privilege.

    Locally the owner is what a careless script connects as, so a rule that
    evaporated for it would guard nothing in the one situation that matters.
    """
    args = _provenance_args("owner")
    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(INSERT_ENTITY), {"cid": args["cid"]})
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, ENTITY_WITHOUT_PROVENANCE)


# ===========================================================================
# RULING 5 -- audit actor scoping covers every role
# ===========================================================================
INSERT_AUDIT = "INSERT INTO audit_log (actor, action, subject) VALUES (:actor, 'decide', :subject)"


@pytest.mark.parametrize(
    "actor",
    [
        pytest.param("reviewer:alice", id="the-attack-0004-still-allowed"),
        pytest.param("alice", id="a-bare-name"),
        pytest.param("apply", id="the-old-default"),
        pytest.param("System:apply", id="wrong-case"),
        pytest.param(" system:apply", id="leading-space-defeats-the-anchor"),
    ],
)
def test_apply_writer_cannot_forge_a_human_actor(role_txn: RoleTxn, actor: str) -> None:
    """0004 scoped the actor rule to ``recon_writer`` only.

    So the *applying* machine -- the one that actually rewrites canonical state
    -- could still sign its audit rows "reviewer:alice". The audit trail is the
    record of who decided; a machine that can forge it hides the forgery.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(text(INSERT_AUDIT), {"actor": actor, "subject": TEST_TAG})
    assert_sqlstate(excinfo.value, AUDIT_ACTOR_OUT_OF_SCOPE)


@pytest.mark.parametrize("actor", ["system:apply", "system:apply/run-1"])
def test_apply_writer_may_write_a_machine_scoped_audit_row(role_txn: RoleTxn, actor: str) -> None:
    """Positive control: the apply path still audits its own work."""
    with role_txn(ROLE_APPLY_WRITER) as conn:
        stored = conn.execute(
            text(INSERT_AUDIT + " RETURNING actor"), {"actor": actor, "subject": TEST_TAG}
        ).scalar_one()
    assert stored == actor


@pytest.mark.parametrize(
    "actor",
    [
        pytest.param("system:recon", id="a-machine-actor"),
        pytest.param("alice", id="a-bare-name"),
        pytest.param("Reviewer:alice", id="wrong-case"),
    ],
)
def test_review_writer_must_write_a_reviewer_scoped_actor(role_txn: RoleTxn, actor: str) -> None:
    """The decider's rows are attributable to a human, and say so.

    A reviewer connection writing ``system:`` would let a human decision be
    filed as an automated one, which is the same lie in the other direction.
    """
    with role_txn(ROLE_REVIEW_WRITER) as conn:  # control: a reviewer actor works
        conn.execute(text(INSERT_AUDIT), {"actor": "reviewer:alice", "subject": TEST_TAG})

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_REVIEW_WRITER) as conn:
        conn.execute(text(INSERT_AUDIT), {"actor": actor, "subject": TEST_TAG})
    assert_sqlstate(excinfo.value, AUDIT_ACTOR_OUT_OF_SCOPE)


# ===========================================================================
# RULING 6 -- sensitive proposals are born held
# ===========================================================================
INSERT_SENSITIVE = (
    "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence, "
    "created_run, status, sensitive, target_canonical_id) "
    "VALUES (:cid, :fp, '{\"set\": {}}'::jsonb, :confidence, '{}'::jsonb, 'run-1', "
    "CAST(:status AS proposal_status), :sensitive, :target)"
)


@pytest.mark.parametrize("confidence", [0.01, 0.99, 1.0])
def test_a_sensitive_proposal_cannot_be_born_pending(
    role_txn: RoleTxn, seeded_rows: dict[str, object], confidence: float
) -> None:
    """R15 stops being an application convention.

    "Sensitive fields can never auto-apply at any confidence" rested on the
    reconciler classifying correctly and choosing the birth status; one wrong
    branch and a sensitive fix lands ``pending`` where a high-confidence
    auto-apply path can reach it. Parametrised across confidences because the
    rule is explicitly *independent* of confidence.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(
            text(INSERT_SENSITIVE),
            {
                "cid": seeded_rows["conflict_id"],
                "fp": f"{TEST_TAG}-sensitive-{confidence}",
                "status": "pending",
                "sensitive": True,
                "confidence": confidence,
                "target": seeded_rows["canonical_id"],
            },
        )
    assert_sqlstate(excinfo.value, PROPOSAL_NOT_BORN_PENDING)


def test_a_sensitive_proposal_born_held_is_accepted(
    role_txn: RoleTxn, seeded_rows: dict[str, object]
) -> None:
    """Positive control: the classifier's correct output still lands."""
    with role_txn(ROLE_RECON_WRITER) as conn:
        stored = conn.execute(
            text(INSERT_SENSITIVE + " RETURNING status, sensitive"),
            {
                "cid": seeded_rows["conflict_id"],
                "fp": f"{TEST_TAG}-sensitive-ok",
                "status": "sensitive_hold",
                "sensitive": True,
                "confidence": 0.99,
                "target": seeded_rows["canonical_id"],
            },
        ).one()
    assert (stored.status, stored.sensitive) == ("sensitive_hold", True)


def test_the_sensitive_birth_rule_binds_the_schema_owner_too(
    owner_engine: Engine, seeded_rows: dict[str, object]
) -> None:
    """As with born-pending: an invariant of the table, not a per-role grant."""
    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(INSERT_SENSITIVE),
                {
                    "cid": seeded_rows["conflict_id"],
                    "fp": f"{TEST_TAG}-sensitive-owner",
                    "status": "pending",
                    "sensitive": True,
                    "confidence": 0.5,
                    "target": seeded_rows["canonical_id"],
                },
            )
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, PROPOSAL_NOT_BORN_PENDING)


# ===========================================================================
# The whole lifecycle, across all three roles, with real commits
# ===========================================================================
def _current(conn: Connection, canonical_id: uuid.UUID) -> str:
    return conn.execute(
        text("SELECT current::text FROM entities WHERE canonical_id = :c"), {"c": canonical_id}
    ).scalar_one()


def test_the_whole_lifecycle_runs_end_to_end(owner_engine: Engine) -> None:
    """propose (recon) -> decide (review) -> apply (apply, with reversal) -> rollback.

    Four separate committed transactions on four connections, each authenticated
    as the role that owns that step. Nothing here is rolled back, so a boundary
    that is "secure" only because the legitimate path is broken fails loudly.

    This is the test that would catch the worst outcome of the last two rounds:
    a rule so tight that the product cannot run.
    """
    canonical_id = uuid.uuid5(uuid.NAMESPACE_URL, "keystone/tests/schema/lifecycle")
    fingerprint = f"{TEST_TAG}-lifecycle-{uuid.uuid4()}"
    applied_value = '{"grade": "9"}'
    original_value = "{}"

    with owner_engine.begin() as conn:
        # RULING 11: a link names an ingested record, so ingestion comes first.
        conn.execute(INSERT_RAW_RECORD, raw_record_params("crm", f"{TEST_TAG}-lifecycle", 96))
        conn.execute(
            text(
                "INSERT INTO entity_links (canonical_id, source_id, source_key, source_ref, "
                "method, generation) VALUES (:c, 'crm', :k, :r, 'L1', 96) "
                "ON CONFLICT (generation, source_id, source_key) DO NOTHING"
            ),
            {
                "c": canonical_id,
                "k": f"{TEST_TAG}-lifecycle",
                "r": f"crm:contact:{TEST_TAG}-lifecycle",
            },
        )
        conn.execute(
            text(
                "INSERT INTO entities (canonical_id, entity_type, current) "
                "VALUES (:c, 'person', '{}'::jsonb) "
                "ON CONFLICT (canonical_id) DO UPDATE SET current = '{}'::jsonb"
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
            {"fp": fingerprint, "run": TEST_TAG},
        ).scalar_one()

    try:
        # 1. PROPOSE -- recon_writer, committed.
        with role_connection(ROLE_RECON_WRITER) as conn:
            proposal_id = conn.execute(
                text(
                    "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, "
                    "evidence, created_run, target_canonical_id) "
                    "VALUES (:cid, :fp, CAST(:action AS jsonb), 0.95, '{}'::jsonb, :run, :target) "
                    "RETURNING id, status"
                ),
                {
                    "cid": conflict_id,
                    "fp": fingerprint,
                    "action": f'{{"set": {applied_value}}}',
                    "run": TEST_TAG,
                    "target": canonical_id,
                },
            ).one()
            conn.execute(
                text("INSERT INTO audit_log (actor, action, subject) VALUES (:a, 'propose', :s)"),
                {"a": "system:reconciler", "s": fingerprint},
            )
        assert proposal_id.status == "pending", "a proposal is born pending"

        # 2. DECIDE -- review_writer, committed. A different principal entirely.
        with role_connection(ROLE_REVIEW_WRITER) as conn:
            decided = conn.execute(
                text(APPROVE + " RETURNING status, decided_by"), {"pid": proposal_id.id}
            ).one()
            conn.execute(
                text("INSERT INTO audit_log (actor, action, subject) VALUES (:a, 'approve', :s)"),
                {"a": "reviewer:alice", "s": fingerprint},
            )
        assert (decided.status, decided.decided_by) == ("approved", "reviewer:alice")

        # 3. APPLY -- apply_writer, committed, canonical write + reversal record
        #    + the proposal's own apply leg, all in ONE transaction.
        with role_connection(ROLE_APPLY_WRITER) as conn:
            conn.execute(
                text(INSERT_EVENT),
                {
                    "pid": proposal_id.id,
                    "canonical_id": canonical_id,
                    "before": original_value,
                    "after": applied_value,
                },
            )
            conn.execute(text(REWRITE), {"cid": canonical_id, "after": applied_value})
            conn.execute(
                text("UPDATE proposals SET status = 'applied' WHERE id = :pid"),
                {"pid": proposal_id.id},
            )
            conn.execute(
                text("INSERT INTO audit_log (actor, action, subject) VALUES (:a, 'apply', :s)"),
                {"a": "system:apply", "s": fingerprint},
            )

        with owner_engine.connect() as conn:
            assert _current(conn, canonical_id) == applied_value

        # 4. ROLLBACK -- apply_writer again: restore from the recorded `before`.
        with role_connection(ROLE_APPLY_WRITER) as conn:
            conn.execute(
                text(
                    "INSERT INTO proposal_events (proposal_id, canonical_id, event, before, "
                    "after, actor) VALUES (:pid, :c, 'rolled_back', CAST(:before AS jsonb), "
                    "CAST(:after AS jsonb), 'system:apply')"
                ),
                {
                    "pid": proposal_id.id,
                    "c": canonical_id,
                    "before": applied_value,
                    "after": original_value,
                },
            )
            conn.execute(text(REWRITE), {"cid": canonical_id, "after": original_value})
            conn.execute(
                text("UPDATE proposals SET status = 'rolled_back' WHERE id = :pid"),
                {"pid": proposal_id.id},
            )

        with owner_engine.connect() as conn:
            assert _current(conn, canonical_id) == original_value, (
                "the rollback must restore the value the reversal record captured"
            )
            final_status = conn.execute(
                text("SELECT status FROM proposals WHERE id = :p"), {"p": proposal_id.id}
            ).scalar_one()
            events = (
                conn.execute(
                    text("SELECT event FROM proposal_events WHERE proposal_id = :p ORDER BY id"),
                    {"p": proposal_id.id},
                )
                .scalars()
                .all()
            )
        assert final_status == "rolled_back"
        assert events == ["applied", "rolled_back"]
    finally:
        with owner_engine.begin() as conn:
            conn.execute(
                text("DELETE FROM proposal_events WHERE canonical_id = :c"), {"c": canonical_id}
            )
            conn.execute(text("DELETE FROM proposals WHERE conflict_id = :c"), {"c": conflict_id})
            conn.execute(text("DELETE FROM conflicts WHERE id = :c"), {"c": conflict_id})
            conn.execute(text("DELETE FROM audit_log WHERE subject = :s"), {"s": fingerprint})
            conn.execute(text("DELETE FROM entities WHERE canonical_id = :c"), {"c": canonical_id})
            conn.execute(
                text("DELETE FROM entity_links WHERE canonical_id = :c"), {"c": canonical_id}
            )
            conn.execute(
                text("DELETE FROM raw_records WHERE generation = 96 AND natural_key = :k"),
                {"k": f"{TEST_TAG}-lifecycle"},
            )
