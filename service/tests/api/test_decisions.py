"""approve / reject / apply -- and the three roles that make them different acts.

DESIGN pins the separation of duties as the enforcement boundary for
"holds before writes": `review_writer` DECIDES, `apply_writer` APPLIES, and
neither can do the other's job. These tests drive the real HTTP endpoints and
then look at what the DATABASE recorded -- the role that made the write, the
decider it named, the ledger row it left -- because an endpoint that returned
200 while running everything as one principal would pass a shape test.

They COMMIT. The citation triggers are deferred, so an approve/apply cycle that
never reaches COMMIT never meets them.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.api.conftest import ADMIN_HEADERS, CLIENT_HEADERS

_PENDING_ELIGIBLE = text(
    """
    SELECT p.id
      FROM proposals p
      JOIN conflicts c ON c.id = p.conflict_id
      JOIN entities e ON e.canonical_id = p.target_canonical_id
     WHERE p.status = 'pending'
       AND p.sensitive = false
       AND p.confidence >= 0.95
       AND c.type = 'C9'
       AND p.action -> 'set' <> '{}'::jsonb
       AND NOT EXISTS (SELECT 1 FROM proposal_events pe WHERE pe.proposal_id = p.id)
     ORDER BY p.id DESC
    """
)

_PENDING_ANY = text("SELECT id FROM proposals WHERE status = 'pending' ORDER BY id DESC LIMIT 50")


@pytest.fixture(scope="module")
def free_ids(review_api: TestClient, reader: Any) -> list[int]:
    """Eligible, unspent proposals, handed out from the END of the store.

    `tests/apply` claims from the front. Two suites drawing single-use citations
    from one store would otherwise collide, and the collision would look like a
    flaky test rather than like the resource exhaustion it is.
    """
    with reader.connect() as conn:
        ids = [row.id for row in conn.execute(_PENDING_ELIGIBLE)]
    assert len(ids) >= 4, f"only {len(ids)} eligible proposals remain for the decision tests"
    return ids


@pytest.fixture(scope="module")
def spare_ids(reader: Any, free_ids: list[int]) -> list[int]:
    """Pending proposals that are NOT reserved for the apply cases above.

    Disjoint by construction rather than by luck: a decision test that happened
    to reject the proposal an apply test was going to use would fail in the apply
    test, several cases later, with a message about the wrong thing.
    """
    reserved = set(free_ids)
    with reader.connect() as conn:
        ids = [row.id for row in conn.execute(_PENDING_ANY) if row.id not in reserved]
    assert len(ids) >= 4
    return ids


def proposal_row(reader: Any, proposal_id: int) -> Any:
    with reader.connect() as conn:
        return conn.execute(
            text(
                "SELECT status::text AS status, decided_by, decided_at "
                "FROM proposals WHERE id = :id"
            ),
            {"id": proposal_id},
        ).one()


def events(reader: Any, proposal_id: int) -> list[Any]:
    with reader.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT event, actor FROM proposal_events WHERE proposal_id = :id ORDER BY id"
                ),
                {"id": proposal_id},
            )
        )


# ---------------------------------------------------------------------------
# the decisions
# ---------------------------------------------------------------------------


def test_approve_records_a_named_reviewer(
    review_api: TestClient, reader: Any, free_ids: list[int]
) -> None:
    """A decision must name its decider: KS004 refuses one that does not."""
    proposal_id = free_ids[0]
    response = review_api.post(f"/api/proposals/{proposal_id}/approve", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"  # A5: the updated proposal comes back
    assert body["decided_by"].startswith("reviewer:")
    assert body["decided_at"] is not None

    row = proposal_row(reader, proposal_id)
    assert row.status == "approved"
    assert row.decided_by == "reviewer:demo-admin", (
        "the decision must be attributed to the authenticated reviewer, not to the service"
    )
    assert row.decided_at is not None


def test_reject_is_terminal(review_api: TestClient, reader: Any, spare_ids: list[int]) -> None:
    proposal_id = spare_ids[0]
    assert (
        review_api.post(f"/api/proposals/{proposal_id}/reject", headers=ADMIN_HEADERS).status_code
        == 200
    )
    assert proposal_row(reader, proposal_id).status == "rejected"
    # KS004 has no arc out of `rejected` for any role.
    second = review_api.post(f"/api/proposals/{proposal_id}/approve", headers=ADMIN_HEADERS)
    assert second.status_code == 409
    assert second.json()["title"] == "proposal not decidable"


def test_apply_requires_an_approval_first(
    review_api: TestClient, reader: Any, spare_ids: list[int]
) -> None:
    """ "approved only" is DESIGN's own word for this endpoint."""
    proposal_id = spare_ids[1]
    response = review_api.post(f"/api/proposals/{proposal_id}/apply", headers=ADMIN_HEADERS)
    assert response.status_code == 409
    assert response.json()["title"] == "not_approved"
    assert proposal_row(reader, proposal_id).status == "pending"
    assert events(reader, proposal_id) == []


def test_approve_then_apply_writes_the_canonical_row(
    review_api: TestClient, reader: Any, free_ids: list[int]
) -> None:
    """The whole R11 loop over HTTP, ending in a real canonical write."""
    proposal_id = free_ids[1]
    with reader.connect() as conn:
        canonical_id = conn.execute(
            text("SELECT target_canonical_id::text FROM proposals WHERE id = :id"),
            {"id": proposal_id},
        ).scalar_one()
        before = conn.execute(
            text("SELECT current::text FROM entities WHERE canonical_id = CAST(:c AS uuid)"),
            {"c": canonical_id},
        ).scalar_one()

    assert (
        review_api.post(f"/api/proposals/{proposal_id}/approve", headers=ADMIN_HEADERS).status_code
        == 200
    )
    applied = review_api.post(f"/api/proposals/{proposal_id}/apply", headers=ADMIN_HEADERS)
    assert applied.status_code == 200, applied.text
    assert applied.json()["status"] == "applied"

    ledger = events(reader, proposal_id)
    assert [row.event for row in ledger] == ["applied"]
    assert ledger[0].actor.startswith("system:"), (
        "apply_writer may only write machine-scoped ledger actors (KS003); a human name "
        "here would mean the automation attributed its own write to a reviewer"
    )

    with reader.connect() as conn:
        after = conn.execute(
            text("SELECT current::text FROM entities WHERE canonical_id = CAST(:c AS uuid)"),
            {"c": canonical_id},
        ).scalar_one()
    assert after != before

    # Put the store back: the reversal leg is part of the same deliverable.
    from recon.apply import rollback_proposal

    reversal = rollback_proposal(proposal_id)
    assert reversal.byte_identical
    with reader.connect() as conn:
        restored = conn.execute(
            text("SELECT current::text FROM entities WHERE canonical_id = CAST(:c AS uuid)"),
            {"c": canonical_id},
        ).scalar_one()
    assert restored == before


# ---------------------------------------------------------------------------
# R24 over HTTP
# ---------------------------------------------------------------------------


def test_the_auto_path_applies_an_eligible_proposal(
    review_api: TestClient, reader: Any, free_ids: list[int]
) -> None:
    """`?auto=true` runs R24's gate. This one passes every condition."""
    proposal_id = free_ids[2]
    assert (
        review_api.post(f"/api/proposals/{proposal_id}/approve", headers=ADMIN_HEADERS).status_code
        == 200
    )
    response = review_api.post(
        f"/api/proposals/{proposal_id}/apply", params={"auto": "true"}, headers=ADMIN_HEADERS
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "applied"
    assert events(reader, proposal_id)[0].actor == "system:auto-apply"

    from recon.apply import rollback_proposal

    assert rollback_proposal(proposal_id).byte_identical


def test_the_auto_path_refuses_a_sensitive_proposal_even_once_approved(
    review_api: TestClient, reader: Any
) -> None:
    """R15 over HTTP: approved by a human, and still never auto-applied.

    The refusal body carries every condition the gate evaluated, and the single
    evaluated condition is the sensitivity one -- the confidence was never
    reached, let alone compared.
    """
    with reader.connect() as conn:
        proposal_id = conn.execute(
            text(
                "SELECT id FROM proposals WHERE status = 'sensitive_hold' "
                "AND action -> 'set' <> '{}'::jsonb ORDER BY id DESC LIMIT 1"
            )
        ).scalar_one()
    assert (
        review_api.post(f"/api/proposals/{proposal_id}/approve", headers=ADMIN_HEADERS).status_code
        == 200
    )
    response = review_api.post(
        f"/api/proposals/{proposal_id}/apply", params={"auto": "true"}, headers=ADMIN_HEADERS
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["title"] == "auto-apply refused"
    assert body["auto_apply"]["reason"] == "sensitive_hold"
    assert [check["check"] for check in body["auto_apply"]["checks"]] == ["not_sensitive"]

    assert proposal_row(reader, proposal_id).status == "approved"
    assert events(reader, proposal_id) == [], "a refused auto-apply left a ledger row"

    # The refusal is AUDITED, and the audit row survives the refusal. It has to be
    # written in a transaction of its own: `auto_apply` raises, and the raise
    # unwinds the endpoint's apply_writer transaction with everything in it.
    with reader.connect() as conn:
        audited = conn.execute(
            text(
                "SELECT count(*) FROM audit_log "
                "WHERE action = 'proposal.auto_apply_refused' AND subject = :s"
            ),
            {"s": str(proposal_id)},
        ).scalar_one()
    assert audited == 1, (
        "the refused auto-apply left no audit row; docs/proposal-policy.md claims a "
        "refusal is audited, and a document naming a control that does not exist is "
        "the exact failure this project has shipped twice"
    )


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


def test_a_client_key_cannot_decide_anything(
    review_api: TestClient, reader: Any, spare_ids: list[int]
) -> None:
    proposal_id = spare_ids[2]
    for action in ("approve", "reject", "apply"):
        response = review_api.post(f"/api/proposals/{proposal_id}/{action}", headers=CLIENT_HEADERS)
        assert response.status_code == 403
    assert proposal_row(reader, proposal_id).status == "pending"


def test_an_unauthenticated_decision_is_401(review_api: TestClient, spare_ids: list[int]) -> None:
    assert review_api.post(f"/api/proposals/{spare_ids[3]}/approve").status_code == 401
