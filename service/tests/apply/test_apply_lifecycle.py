"""The apply and rollback legs, end to end, against the real write boundary.

**These tests COMMIT.** They have to. Every rule that makes the apply path
trustworthy -- the citation correlation (`KS001`), the content binding
(`KS010`), the ledger-honesty trigger (`KS011`), the reversal binding (`KS012`)
-- is on a DEFERRABLE INITIALLY DEFERRED constraint trigger, so it fires at
COMMIT and at no other moment. A suite that drove the apply inside a transaction
it rolled back would be green against a boundary it never reached: the classic
"what is the green evidence OF" failure.

So each test here consumes its own proposal (a citation is single-use since
migration 0006), commits, asserts against the committed state, and -- where it
applied -- rolls back through the real reversal leg, which is also the
deliverable being proved.

Roles are real logins, not `SET ROLE`: `recon.db.role_connection` authenticates
AS the role, because a table owner bypasses its own grants and a suite run as
the owner would leave the whole separation of duties untested.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from recon.apply import (
    ApplyError,
    apply_proposal,
    assert_sources_are_unwritable,
    auto_apply,
    entity_digest,
    evaluate_auto_apply,
    load_proposal,
    rollback_proposal,
    source_tree_digest,
)
from recon.db import (
    ROLE_APPLY_WRITER,
    ROLE_RECON_WRITER,
    ROLE_REVIEW_WRITER,
    role_connection,
)

_APPROVE = text(
    """
    UPDATE proposals
       SET status = 'approved', decided_by = :decided_by, decided_at = :decided_at
     WHERE id = :proposal_id AND status IN ('pending', 'sensitive_hold')
    RETURNING id
    """
)

_ENTITY_TEXT = text(
    "SELECT current::text AS current_text, updated_at FROM entities WHERE canonical_id = :cid"
)

_EVENTS = text(
    """
    SELECT id, event, actor, txid, canonical_id::text AS canonical_id,
           before::text AS before_text, after::text AS after_text
      FROM proposal_events WHERE proposal_id = :proposal_id ORDER BY id
    """
)


def approve(proposal_id: int, *, who: str = "reviewer:test-suite") -> None:
    """A real `review_writer` decision, committed. The only role that may decide."""
    with role_connection(ROLE_REVIEW_WRITER) as conn:
        moved = conn.execute(
            _APPROVE,
            {"proposal_id": proposal_id, "decided_by": who, "decided_at": datetime.now(UTC)},
        ).fetchone()
        assert moved is not None, f"proposal {proposal_id} was not decidable"


def entity_state(reader: Any, canonical_id: str) -> tuple[str, Any]:
    with reader.connect() as conn:
        row = conn.execute(_ENTITY_TEXT, {"cid": canonical_id}).one()
    return row.current_text, row.updated_at


def events(reader: Any, proposal_id: int) -> list[Any]:
    with reader.connect() as conn:
        return list(conn.execute(_EVENTS, {"proposal_id": proposal_id}))


# ======================================================================================
# the deliverable: apply, then roll back to a byte-identical digest
# ======================================================================================


def test_apply_then_rollback_restores_a_byte_identical_row(
    reader: Any, committed_proposal: int
) -> None:
    """R24 + the rollback path, proved by digest rather than by field comparison."""
    with reader.connect() as conn:
        record = load_proposal(conn, committed_proposal)
    assert record is not None
    canonical_id = record.target_canonical_id
    assert canonical_id

    before_text, _ = entity_state(reader, canonical_id)
    before_digest = entity_digest(before_text)

    # A human decides. The machine cannot: apply_writer's transition graph has
    # no arc into `approved` at all (KS004), which the next test proves.
    approve(committed_proposal)

    with reader.connect() as conn:
        decision = evaluate_auto_apply(conn, committed_proposal)
    assert decision.allowed, decision.detail

    result = auto_apply(committed_proposal)
    assert result.before_digest == before_digest
    assert result.after_digest != before_digest, (
        "the apply produced a byte-identical row, so nothing was written and the rest "
        "of this test would be vacuous"
    )

    after_text, _ = entity_state(reader, canonical_id)
    assert entity_digest(after_text) == result.after_digest
    # The write is exactly the approved action merged onto the prior value.
    expected = {**json.loads(before_text), **record.assignments}
    assert json.loads(after_text) == expected

    applied = [row for row in events(reader, committed_proposal) if row.event == "applied"]
    assert len(applied) == 1
    event = applied[0]
    assert event.canonical_id == canonical_id
    assert event.actor.startswith("system:")
    assert entity_digest(event.before_text) == before_digest
    assert entity_digest(event.after_text) == result.after_digest

    with reader.connect() as conn:
        assert load_proposal(conn, committed_proposal).status == "applied"

    # ---- the reversal leg -------------------------------------------------
    reversal = rollback_proposal(committed_proposal)
    assert reversal.byte_identical
    assert reversal.restored_digest == before_digest

    restored_text, _ = entity_state(reader, canonical_id)
    assert restored_text == before_text, "the restored row is not byte-identical"
    assert entity_digest(restored_text) == before_digest

    with reader.connect() as conn:
        assert load_proposal(conn, committed_proposal).status == "rolled_back"
    labels = [row.event for row in events(reader, committed_proposal)]
    assert labels == ["applied", "rolled_back"]


def test_a_real_apply_leaves_the_source_TREE_byte_identical(
    reader: Any, committed_proposal: int
) -> None:
    """R24's "never to sources", MEASURED across a real committed apply and reversal.

    `assert_sources_are_unwritable()` is a structural argument about the adapter
    classes: no member of any of them is write-shaped, so no code path through
    the port could write a source. That is a claim about the TYPE. It says
    nothing about a stray `open(..., "w")` somewhere else in the process, and
    R24's "never to sources" is a statement about the RUN.

    So the fixture tree is sha256'd per file before and after a full auto-apply
    plus rollback that COMMITS -- the real write boundary, the real citation
    triggers, the real reversal leg -- and every digest must be unchanged. The
    canonical row is asserted to have actually moved in between, or the whole
    comparison would be measuring a run that did nothing.
    """
    checked = assert_sources_are_unwritable()
    assert checked == ("AppDbAdapter", "CrmAdapter", "PaymentsAdapter"), checked

    before_tree = source_tree_digest()
    assert len(before_tree) >= 3, before_tree

    with reader.connect() as conn:
        record = load_proposal(conn, committed_proposal)
    assert record is not None
    canonical_id = record.target_canonical_id
    assert canonical_id
    entity_before, _ = entity_state(reader, canonical_id)

    approve(committed_proposal)
    result = auto_apply(committed_proposal)
    entity_after, _ = entity_state(reader, canonical_id)
    assert entity_after != entity_before, (
        "the apply changed nothing in the canonical layer, so a tree that is also "
        "unchanged proves nothing about whether an apply can touch a source"
    )
    assert result.after_digest == entity_digest(entity_after)

    mid_tree = source_tree_digest()
    assert mid_tree == before_tree, (
        "the apply changed the source fixture tree: "
        f"{sorted(k for k in mid_tree if mid_tree[k] != before_tree.get(k))}"
    )

    rollback_proposal(committed_proposal)
    after_tree = source_tree_digest()
    assert after_tree == before_tree, (
        "the rollback changed the source fixture tree: "
        f"{sorted(k for k in after_tree if after_tree[k] != before_tree.get(k))}"
    )
    # ...and the canonical layer really did move and come back.
    restored, _ = entity_state(reader, canonical_id)
    assert restored == entity_before


def test_a_spent_citation_cannot_be_applied_twice(reader: Any, committed_proposal: int) -> None:
    """One approval, one canonical write. The second attempt is refused."""
    approve(committed_proposal)
    apply_proposal(committed_proposal)
    with pytest.raises(ApplyError) as raised:
        apply_proposal(committed_proposal)
    assert raised.value.reason == "not_approved"

    # And the gate refuses it too, on the rollback-path condition: the applied
    # leg is spent, so a further write could not be reversed.
    with reader.connect() as conn:
        decision = evaluate_auto_apply(conn, committed_proposal)
    assert not decision.allowed
    assert {check.name for check in decision.failed} >= {"rollback_path", "status_appliable"}

    rollback_proposal(committed_proposal)
    with pytest.raises(ApplyError) as reraised:
        rollback_proposal(committed_proposal)
    assert reraised.value.reason == "not_applied"


# ======================================================================================
# separation of duties -- the database refuses, not this code
# ======================================================================================


def test_apply_writer_cannot_approve(apply_conn: Any, store: Any) -> None:
    """KS004. The automation cannot decide its own work."""
    pending = apply_conn.execute(
        text("SELECT id FROM proposals WHERE status = 'pending' ORDER BY id LIMIT 1")
    ).scalar_one()
    with pytest.raises(DBAPIError) as raised:
        apply_conn.execute(
            text("UPDATE proposals SET status = 'approved' WHERE id = :id"), {"id": pending}
        )
    assert _sqlstate(raised.value) == "KS004"


def test_recon_writer_cannot_apply(store: Any) -> None:
    """The proposing role holds no UPDATE on proposals and none on entities."""
    with role_connection(ROLE_RECON_WRITER, commit=False) as conn:
        approved = conn.execute(text("SELECT id FROM proposals ORDER BY id LIMIT 1")).scalar_one()
        with pytest.raises(DBAPIError) as raised:
            conn.execute(
                text("UPDATE proposals SET status = 'applied' WHERE id = :id"), {"id": approved}
            )
    assert _sqlstate(raised.value) in {"42501", "KS004"}


def test_review_writer_cannot_write_the_canonical_layer(review_conn: Any, reader: Any) -> None:
    """The deciding role holds no write of any kind on `entities`."""
    canonical_id = review_conn.execute(
        text("SELECT canonical_id::text FROM entities ORDER BY canonical_id LIMIT 1")
    ).scalar_one()
    with pytest.raises(DBAPIError) as raised:
        review_conn.execute(
            text("UPDATE entities SET current = '{}'::jsonb WHERE canonical_id = CAST(:c AS uuid)"),
            {"c": canonical_id},
        )
    assert _sqlstate(raised.value) == "42501"


def test_an_uncited_canonical_write_is_refused(store: Any, reader: Any) -> None:
    """The boundary itself: apply_writer writing `entities` with no ledger row.

    Committed on purpose -- the citation trigger is deferred, so this is the one
    shape a rolled-back test could never catch. The COMMIT is what raises.
    """
    with reader.connect() as conn:
        canonical_id = conn.execute(
            text("SELECT canonical_id::text FROM entities ORDER BY canonical_id LIMIT 1")
        ).scalar_one()
    with pytest.raises(DBAPIError) as raised, role_connection(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(
                "UPDATE entities SET current = current || '{\"tampered\": true}'::jsonb "
                "WHERE canonical_id = CAST(:c AS uuid)"
            ),
            {"c": canonical_id},
        )
    assert _sqlstate(raised.value) == "KS001"

    with reader.connect() as conn:
        current = conn.execute(
            text("SELECT current FROM entities WHERE canonical_id = CAST(:c AS uuid)"),
            {"c": canonical_id},
        ).scalar_one()
    assert "tampered" not in current


# ======================================================================================
# refusals the module makes before touching the database
# ======================================================================================


def test_an_unapproved_proposal_is_not_applied(reader: Any) -> None:
    with reader.connect() as conn:
        pending = conn.execute(
            text("SELECT id FROM proposals WHERE status = 'pending' ORDER BY id LIMIT 1")
        ).scalar_one()
    with pytest.raises(ApplyError) as raised:
        apply_proposal(pending)
    assert raised.value.reason == "not_approved"


def test_an_evidence_only_proposal_is_not_applied(reader: Any) -> None:
    """Contract SS6's third class writes no field; applying it writes nothing."""
    with reader.connect() as conn:
        empty = conn.execute(
            text(
                "SELECT id FROM proposals WHERE action -> 'set' = '{}'::jsonb "
                "AND status = 'pending' ORDER BY id LIMIT 1"
            )
        ).scalar_one()
    approve(empty)
    with pytest.raises(ApplyError) as raised:
        apply_proposal(empty)
    assert raised.value.reason == "evidence_only"


def test_a_missing_proposal_is_a_named_refusal() -> None:
    with pytest.raises(ApplyError) as raised:
        apply_proposal(2_000_000_000)
    assert raised.value.reason == "not_found"


# ======================================================================================
# helpers
# ======================================================================================


def _sqlstate(error: BaseException) -> str | None:
    original = getattr(error, "orig", error)
    if isinstance(original, psycopg.Error):
        return original.sqlstate
    return None  # pragma: no cover - every raise here is a psycopg error
