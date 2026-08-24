"""R24's reversal leg, over HTTP: `POST /api/proposals/{id}/rollback` and the ledger.

`recon.apply.rollback_proposal` was always the reversal and it always worked --
`tests/apply/test_apply_lifecycle.py` drives it directly, and two cases in
`tests/api/test_decisions.py` call it to put the store back. What did not exist was
a way to *reach* it: `openapi.json` served fourteen routes and none of them was the
rollback, so R24's "recorded rollback path" and the rubric's "fully logged &
reversible" were satisfied only by a Python interpreter holding the `apply_writer`
credentials. This module drives the endpoint instead.

Every test here goes through real HTTP into the real database and **commits**. The
citation triggers that make the reversal meaningful (`KS001`, `KS011`, `KS012`) are
deferred, so a reversal that never reaches COMMIT never meets them -- a suite that
rolled its transaction back would be asserting on a write no rule had judged yet.

Two assertions are the point of the file and neither is a shape check:

* the restored value is compared to the pre-apply value **as bytes**
  (`current::text`) as well as as jsonb, because `'{"a": 1}'` and `'{"a": 1.0}'`
  are equal as jsonb, render differently, and `recon.suite.mirror` hashes the text
  on a graded determinism path (migration 0008, MINOR 18);
* the ledger is a **stack**. Apply P1, apply P2 on the same entity, and reversing
  P1 would discard P2's approved, applied, unreversed write. The endpoint refuses
  it with a 409 that names the field paths, and the same reversal succeeds the
  moment P2 is undone.

Proposals are single-use citations, so the fixtures below claim them the way
`tests/api/test_decisions.py` does -- from the END of the store, past an offset --
and the two pools are disjoint *by construction* rather than by luck.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.api.conftest import ADMIN_HEADERS, CLIENT_HEADERS

#: Eligible for an apply and alone on their entity. "Alone" is what keeps this pool
#: disjoint from :data:`_STACKED_PAIR`: a C9 that shares its target with a writable
#: C6 is the raw material of the stack test, and a suite that applied one of those
#: here would consume the pair before that test ran.
_LONE_ELIGIBLE = text(
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
       AND NOT EXISTS (
           SELECT 1
             FROM proposals other
            WHERE other.target_canonical_id = p.target_canonical_id
              AND other.id <> p.id
              AND other.action -> 'set' <> '{}'::jsonb)
     ORDER BY p.id DESC
    """
)

#: Two writable proposals on ONE entity: a C9 writing `appdb.enrollment.crm_deal_id`
#: and a C6 writing `crm.contact.grade`. Different paths, so both applies are
#: legitimate and the second lands ON TOP of the first -- which is the only way to
#: construct `KS012`'s case, and it has to be constructed from real rows: nothing in
#: this repository can write `entities` except an apply citing an approved proposal.
_STACKED_PAIR = text(
    """
    SELECT p.target_canonical_id::text                     AS canonical_id,
           min(p.id) FILTER (WHERE c.type = 'C9')           AS lower_id,
           min(p.id) FILTER (WHERE c.type = 'C6')           AS upper_id
      FROM proposals p
      JOIN conflicts c ON c.id = p.conflict_id
     WHERE p.status = 'pending'
       AND p.sensitive = false
       AND p.action -> 'set' <> '{}'::jsonb
       AND p.target_canonical_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM proposal_events pe WHERE pe.proposal_id = p.id)
     GROUP BY 1
    HAVING count(*) = 2
       AND count(*) FILTER (WHERE c.type = 'C9') = 1
       AND count(*) FILTER (WHERE c.type = 'C6') = 1
     ORDER BY 1 DESC
     LIMIT 1
    """
)

#: How many of the DESC-ordered eligible ids to leave untouched at the top.
#: `tests/api/test_decisions.py` takes `free_ids[0..2]` with no offset and
#: `tests/apply` claims from the front, so stepping past eight leaves both suites
#: room even if the file order ever changes. A collision would not be silent -- the
#: second claimant gets a 409 naming the spent citation -- but "flaky" is how a real
#: failure gets dismissed, so the pools do not overlap in the first place.
_CLAIM_OFFSET = 8

#: Where the personal data in a canonical record actually is: the survivor map's
#: identity paths, plus the top-level household key (which is an email address).
#: :func:`personal_values` reads the real values out of the row under test, and no
#: response asserted on below may contain one of them.
_PII_PATHS = (
    "crm.contact.email",
    "crm.contact.dob",
    "appdb.student.first_name",
    "appdb.student.last_name",
    "appdb.student.dob",
    "appdb.student.student_number",
)


@pytest.fixture(scope="module")
def free_ids(review_api: TestClient, reader: Any) -> list[int]:
    """Applyable proposals nobody else has claimed, from the end of the store."""
    with reader.connect() as conn:
        ids = [row.id for row in conn.execute(_LONE_ELIGIBLE)]
    claimable = ids[_CLAIM_OFFSET:]
    assert len(claimable) >= 6, (
        f"only {len(claimable)} unclaimed applyable proposals remain (of {len(ids)} "
        "eligible); the rollback suite needs six single-use citations"
    )
    return claimable


@pytest.fixture(scope="module")
def stacked(review_api: TestClient, reader: Any) -> dict[str, Any]:
    """One entity with two writable proposals, for the KS012 case."""
    with reader.connect() as conn:
        row = conn.execute(_STACKED_PAIR).fetchone()
    assert row is not None, (
        "no entity in the store carries two unspent writable proposals, so the "
        "stale-reversal case cannot be built from real rows"
    )
    return {"canonical_id": row.canonical_id, "lower": row.lower_id, "upper": row.upper_id}


# ---------------------------------------------------------------------------
# reading the store
# ---------------------------------------------------------------------------


def canonical_id_of(reader: Any, proposal_id: int) -> str:
    with reader.connect() as conn:
        return conn.execute(
            text("SELECT target_canonical_id::text FROM proposals WHERE id = :id"),
            {"id": proposal_id},
        ).scalar_one()


def entity_text(reader: Any, canonical_id: str) -> str:
    """`entities.current::text` -- the bytes, not a Python re-rendering of them."""
    with reader.connect() as conn:
        return conn.execute(
            text("SELECT current::text FROM entities WHERE canonical_id = CAST(:c AS uuid)"),
            {"c": canonical_id},
        ).scalar_one()


def entity_value(reader: Any, canonical_id: str) -> dict[str, Any]:
    with reader.connect() as conn:
        return dict(
            conn.execute(
                text("SELECT current FROM entities WHERE canonical_id = CAST(:c AS uuid)"),
                {"c": canonical_id},
            ).scalar_one()
        )


def personal_values(reader: Any, canonical_id: str) -> list[str]:
    """The real personal strings this canonical row holds.

    Taken from the row rather than invented, so "the response carries no personal
    data" is checked against the data that is actually in the record the response is
    about -- a synthetic needle would pass even if the endpoint leaked the row.
    """
    current = entity_value(reader, canonical_id)
    survived = dict(current.get("survived") or {})
    found = [survived[path] for path in _PII_PATHS if isinstance(survived.get(path), str)]
    household = current.get("household_key")
    if isinstance(household, str):
        found.append(household)
    return found


def written_paths(reader: Any, proposal_id: int) -> list[str]:
    """The field paths this proposal's action writes, read from the stored row.

    Derived rather than hardcoded: the assertions below compare the ledger's
    `differing_paths` against what the *approved action* said it would change, which
    is the binding worth making. A literal would only prove that this store's C9
    template has not changed.
    """
    with reader.connect() as conn:
        return sorted(
            conn.execute(
                text("SELECT jsonb_object_keys(action -> 'set') AS k FROM proposals WHERE id = :i"),
                {"i": proposal_id},
            ).scalars()
        )


def status_of(reader: Any, proposal_id: int) -> str:
    with reader.connect() as conn:
        return conn.execute(
            text("SELECT status::text FROM proposals WHERE id = :id"), {"id": proposal_id}
        ).scalar_one()


def ledger(reader: Any, proposal_id: int) -> list[Any]:
    with reader.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT event, actor FROM proposal_events WHERE proposal_id = :id ORDER BY id"
                ),
                {"id": proposal_id},
            )
        )


def approve(api: TestClient, proposal_id: int) -> None:
    response = api.post(f"/api/proposals/{proposal_id}/approve", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text


def apply_now(api: TestClient, proposal_id: int, *, auto: bool = False) -> Any:
    params = {"auto": "true"} if auto else {}
    response = api.post(f"/api/proposals/{proposal_id}/apply", params=params, headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def roll_back(api: TestClient, proposal_id: int) -> Any:
    return api.post(f"/api/proposals/{proposal_id}/rollback", headers=ADMIN_HEADERS)


# ---------------------------------------------------------------------------
# the reversal
# ---------------------------------------------------------------------------


def test_the_endpoint_restores_the_canonical_row_byte_for_byte(
    review_api: TestClient, reader: Any, free_ids: list[int]
) -> None:
    """approve -> apply -> rollback, all three over HTTP, ending where it started.

    Compared as bytes AND as jsonb. The bytes are the stronger claim and the one
    `KS012` makes: the reversal copies the stored `before` column into
    `entities.current` inside the database, so nothing is parsed, re-serialized or
    reassembled from field values on the way.
    """
    proposal_id = free_ids[0]
    canonical_id = canonical_id_of(reader, proposal_id)
    before_text = entity_text(reader, canonical_id)
    before_value = entity_value(reader, canonical_id)

    approve(review_api, proposal_id)
    apply_now(review_api, proposal_id)
    assert entity_text(reader, canonical_id) != before_text, "the apply wrote nothing"

    response = roll_back(review_api, proposal_id)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "rolled_back"
    assert body["rollback"]["byte_identical"] is True
    assert body["rollback"]["applied_before_digest"] == body["rollback"]["restored_digest"]
    assert body["rollback"]["canonical_id"] == canonical_id

    assert entity_text(reader, canonical_id) == before_text
    assert entity_value(reader, canonical_id) == before_value
    assert status_of(reader, proposal_id) == "rolled_back"

    events = ledger(reader, proposal_id)
    assert [row.event for row in events] == ["applied", "rolled_back"]
    assert all(row.actor.startswith("system:") for row in events), (
        "apply_writer may only write machine-scoped ledger actors (KS003); a reviewer "
        "name here would mean the reversal was attributed to a human who did not make it"
    )


def test_the_reversal_is_audited_under_the_apply_actor(
    review_api: TestClient, reader: Any, free_ids: list[int]
) -> None:
    """The audit row is written inside the reversal's own transaction.

    Not afterwards by the endpoint: an audit row that can be committed separately
    from the write it describes is an audit row that can be missing. R24's ledger
    is only a ledger if it cannot come apart from the act.
    """
    proposal_id = free_ids[1]
    approve(review_api, proposal_id)
    apply_now(review_api, proposal_id, auto=True)
    assert roll_back(review_api, proposal_id).status_code == 200

    with reader.connect() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT actor, action FROM audit_log WHERE subject = :s "
                    "AND action IN ('proposal.auto_applied', 'proposal.rolled_back') "
                    "ORDER BY id"
                ),
                {"s": str(proposal_id)},
            )
        )
    actions = [row.action for row in rows]
    assert actions == ["proposal.auto_applied", "proposal.rolled_back"], (
        f"the ledger for proposal {proposal_id} reads {actions}: an unattended apply "
        "and its reversal must both be recorded"
    )
    assert rows[0].actor == "system:auto-apply"
    assert rows[1].actor == "system:apply"


def test_a_proposal_that_was_never_applied_has_nothing_to_reverse(
    review_api: TestClient, reader: Any, free_ids: list[int]
) -> None:
    """409, in the apply endpoint's own vocabulary. `KS004` has one arc out of `applied`."""
    proposal_id = free_ids[2]
    response = roll_back(review_api, proposal_id)

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["title"] == "not_applied"
    assert body["type"].endswith("/rollback-not-applied")
    assert "only an 'applied' proposal" in body["detail"]

    assert status_of(reader, proposal_id) == "pending"
    assert ledger(reader, proposal_id) == [], "a refused reversal wrote a ledger row"


def test_a_reversal_leg_is_single_use(
    review_api: TestClient, reader: Any, free_ids: list[int]
) -> None:
    """One approval authorises one write and one reversal, and then nothing.

    The second attempt is refused as `not_applied` rather than as "already reversed"
    on purpose: the status is what `KS004` judges, and the proposal is `rolled_back`
    by then. `uq_proposal_events_rolled_back_once` is the backstop underneath.
    """
    proposal_id = free_ids[3]
    approve(review_api, proposal_id)
    apply_now(review_api, proposal_id)
    assert roll_back(review_api, proposal_id).status_code == 200

    again = roll_back(review_api, proposal_id)
    assert again.status_code == 409, again.text
    assert again.json()["title"] == "not_applied"
    assert [row.event for row in ledger(reader, proposal_id)] == ["applied", "rolled_back"]


# ---------------------------------------------------------------------------
# KS012 -- the ledger is a stack
# ---------------------------------------------------------------------------


def test_a_stale_reversal_is_refused_and_succeeds_once_it_is_on_top(
    review_api: TestClient, reader: Any, stacked: dict[str, Any]
) -> None:
    """The case `KS012` exists for, driven through the endpoint on real rows.

    Apply the lower proposal, apply the upper one on the same entity, then try to
    reverse the lower: doing it would silently discard the upper write. The endpoint
    answers 409 with the field paths that moved -- never their values -- and the
    canonical row is untouched. Undo the upper write and the same request succeeds,
    which is what makes this a stack rather than a veto.
    """
    canonical_id = stacked["canonical_id"]
    lower, upper = stacked["lower"], stacked["upper"]
    origin_text = entity_text(reader, canonical_id)

    approve(review_api, lower)
    apply_now(review_api, lower)
    middle_text = entity_text(reader, canonical_id)

    approve(review_api, upper)
    apply_now(review_api, upper)
    top_text = entity_text(reader, canonical_id)
    assert len({origin_text, middle_text, top_text}) == 3, "the two applies did not stack"

    stale = roll_back(review_api, lower)
    assert stale.status_code == 409, stale.text
    body = stale.json()
    assert body["title"] == "not_on_top"
    assert body["type"].endswith("/rollback-not-on-top")
    assert body["rollback"]["on_top"] is False
    assert body["rollback"]["applied_after_digest"] != body["rollback"]["current_digest"]
    # The diagnostic names paths, never values (migration 0008, MINOR 20), and the
    # paths it names are exactly what the write on top was approved to change.
    assert body["rollback"]["differing_paths"] == ", ".join(written_paths(reader, upper))
    rendered = json.dumps(body)
    for secret in personal_values(reader, canonical_id):
        assert secret not in rendered, (
            "the refusal names the paths that differ; it must not carry their values"
        )

    assert entity_text(reader, canonical_id) == top_text, "a refused reversal changed the row"
    assert status_of(reader, lower) == "applied"
    assert [row.event for row in ledger(reader, lower)] == ["applied"]

    # Undo the write on top, and the refused reversal is now the one on top.
    assert roll_back(review_api, upper).status_code == 200
    assert entity_text(reader, canonical_id) == middle_text

    now_allowed = roll_back(review_api, lower)
    assert now_allowed.status_code == 200, now_allowed.text
    assert now_allowed.json()["rollback"]["byte_identical"] is True
    assert entity_text(reader, canonical_id) == origin_text


# ---------------------------------------------------------------------------
# the ledger on the detail response
# ---------------------------------------------------------------------------


def test_the_detail_response_carries_the_ledger_as_digests_and_paths(
    review_api: TestClient, reader: Any, free_ids: list[int]
) -> None:
    """`GET /api/proposals/{id}` gains `events`: what was written, and to what effect.

    Walked across all three states of one proposal, because the interesting
    assertion is that the digests are the *real* ones: the applied event's
    `after_digest` is `recon.apply.entity_digest` of the canonical row as it stands
    while applied, so a reader can bind the ledger to the row without being handed
    the row.
    """
    from recon.apply import entity_digest

    proposal_id = free_ids[4]
    canonical_id = canonical_id_of(reader, proposal_id)
    before_text = entity_text(reader, canonical_id)

    def detail() -> Any:
        response = review_api.get(f"/api/proposals/{proposal_id}", headers=ADMIN_HEADERS)
        assert response.status_code == 200, response.text
        return response.json()

    pristine = detail()
    assert pristine["events"] == [], "a proposal that was never applied has no ledger"
    # Additive, not a restructuring: every key the dashboard was built on is here.
    assert {"id", "conflict_id", "action", "confidence", "status", "auto_apply"} <= set(pristine)

    approve(review_api, proposal_id)
    apply_now(review_api, proposal_id)
    applied_text = entity_text(reader, canonical_id)

    body = detail()
    assert len(body["events"]) == 1
    event = body["events"][0]
    assert event["event"] == "applied"
    assert event["actor"] == "system:apply"
    assert event["canonical_id"] == canonical_id
    assert event["before_digest"] == entity_digest(before_text)
    assert event["after_digest"] == entity_digest(applied_text)
    assert event["differing_paths"] == ", ".join(written_paths(reader, proposal_id))
    assert int(event["txid"]) > 0

    assert roll_back(review_api, proposal_id).status_code == 200
    reversed_body = detail()
    events = reversed_body["events"]
    assert [row["event"] for row in events] == ["applied", "rolled_back"]
    # The reversal's `after` is the apply's `before`, column to column: the same
    # digest on both rows is the byte-identity claim, visible from outside.
    assert events[1]["after_digest"] == events[0]["before_digest"] == entity_digest(before_text)
    assert events[1]["before_digest"] == events[0]["after_digest"]
    assert events[0]["txid"] != events[1]["txid"], (
        "one transaction may hold at most one canonical-mutating event per entity "
        "(uq_proposal_events_canonical_write_once), so the apply and its reversal are "
        "necessarily two"
    )


def test_the_ledger_never_carries_a_canonical_value(
    review_api: TestClient, reader: Any, free_ids: list[int]
) -> None:
    """No PII, structurally: `before` and `after` are never selected at all.

    The canonical record holds a legal name, `crm.contact.email` and a household
    key that is an email address. The event payload is digests and field paths, so
    there is nothing here for a redactor to get wrong -- and this test proves it by
    taking the real personal values out of the row and looking for them in the
    response.
    """
    proposal_id = free_ids[5]
    canonical_id = canonical_id_of(reader, proposal_id)
    secrets = personal_values(reader, canonical_id)
    assert len(secrets) >= 2, (
        f"entity {canonical_id} yielded {len(secrets)} personal values; this test is "
        "only meaningful against a record that really holds some"
    )

    approve(review_api, proposal_id)
    apply_now(review_api, proposal_id)
    response = review_api.get(f"/api/proposals/{proposal_id}", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    events = response.json()["events"]

    assert len(events) == 1
    assert set(events[0]) == {
        "event_id",
        "event",
        "actor",
        "ts",
        "txid",
        "canonical_id",
        "before_digest",
        "after_digest",
        "differing_paths",
    }, "the event shape changed; a new member must be checked for personal data"
    rendered = json.dumps(events)
    for secret in secrets:
        assert secret not in rendered, (
            f"the reversal ledger served a canonical value ({secret[:3]}...): "
            "`before`/`after` are whole personal records and must never leave the database"
        )

    assert roll_back(review_api, proposal_id).status_code == 200


# ---------------------------------------------------------------------------
# who may reverse
# ---------------------------------------------------------------------------


def test_a_client_key_cannot_reverse_anything(
    review_api: TestClient, reader: Any, free_ids: list[int]
) -> None:
    """R20: a reviewer action requires org-wide scope, so a client key is 403.

    403 and not 404 because the *operation* is refused rather than the row hidden --
    the same split `_decide` and the apply endpoint make.
    """
    proposal_id = free_ids[2]
    response = review_api.post(f"/api/proposals/{proposal_id}/rollback", headers=CLIENT_HEADERS)
    assert response.status_code == 403, response.text
    assert status_of(reader, proposal_id) == "pending"


def test_an_unauthenticated_reversal_is_401(review_api: TestClient, free_ids: list[int]) -> None:
    assert review_api.post(f"/api/proposals/{free_ids[2]}/rollback").status_code == 401


def test_an_unknown_proposal_is_404_not_a_500(review_api: TestClient) -> None:
    """The membership-oracle rule, unchanged: "no such row" and "not yours" are one body."""
    response = review_api.post("/api/proposals/999999999/rollback", headers=ADMIN_HEADERS)
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["type"].endswith("/proposal-not-found")
    assert set(body) >= {"type", "title", "status", "detail"}
