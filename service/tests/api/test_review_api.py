"""R11's reviewer surface: the shapes and the pagination the dashboard was built on.

Every assertion here is against `dashboard/src/lib/contract.ts` -- the document
the dashboard is already coded to -- rather than against this ticket's own idea
of a good response. Where the two differ, the test says which assumption
(A1..A10) it is testing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import ADMIN_HEADERS, CLIENT_HEADERS

DASHBOARD = Path(__file__).resolve().parents[3] / "dashboard" / "src" / "lib"

PINNED_CONFLICT_FIELDS = {
    "id",
    "fingerprint",
    "type",
    "entity_refs",
    "sources",
    "disagreeing_fields",
    "status",
    "first_seen_run",
    "last_seen_run",
}
PINNED_PROPOSAL_FIELDS = {
    "id",
    "conflict_id",
    "fingerprint",
    "action",
    "confidence",
    "evidence",
    "rationale",
    "status",
    "sensitive",
    "created_run",
    "decided_by",
    "decided_at",
}


def get(client: TestClient, path: str, **params: Any) -> Any:
    response = client.get(path, params=params, headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# A1 -- the envelope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/conflicts", "/api/proposals"])
def test_the_pagination_envelope_is_the_assumed_one(review_api: TestClient, path: str) -> None:
    """A1: `{items, page, page_size, total}`, params `page` (1-based) and `page_size`."""
    body = get(review_api, path, page=1, page_size=5)
    assert set(body) == {"items", "page", "page_size", "total"}
    assert body["page"] == 1
    assert body["page_size"] == 5
    assert len(body["items"]) == 5
    assert body["total"] > 5
    assert "warnings" not in body, (
        "`warnings` is the DASHBOARD's verdict about a response (filterGuard.ts); a "
        "service that sent one would be telling the client what to think of it"
    )


@pytest.mark.parametrize("path", ["/api/conflicts", "/api/proposals"])
def test_paging_walks_every_row_exactly_once(review_api: TestClient, path: str) -> None:
    """1-based pages, stable order, no row seen twice and none skipped."""
    first = get(review_api, path, page=1, page_size=10)
    second = get(review_api, path, page=2, page_size=10)
    ids = [row["id"] for row in first["items"]] + [row["id"] for row in second["items"]]
    assert len(set(ids)) == 20
    assert ids == sorted(ids, key=int)
    assert first["total"] == second["total"]


@pytest.mark.parametrize("path", ["/api/conflicts", "/api/proposals"])
def test_page_size_is_capped_on_the_server(review_api: TestClient, path: str) -> None:
    """R11's non-goal: never load 100k rows. The client clamps; the server refuses."""
    over = review_api.get(path, params={"page_size": 5000}, headers=ADMIN_HEADERS)
    assert over.status_code == 422
    assert get(review_api, path, page_size=100)["page_size"] == 100


# ---------------------------------------------------------------------------
# row shapes
# ---------------------------------------------------------------------------


def test_a_conflict_row_carries_every_pinned_column(review_api: TestClient) -> None:
    row = get(review_api, "/api/conflicts", page_size=1)["items"][0]
    assert set(row) >= PINNED_CONFLICT_FIELDS
    assert isinstance(row["id"], str), "Conflict.id is a string in the client contract"
    assert isinstance(row["sources"], list), "A7: sources is an array of source ids"
    assert set(row["sources"]) <= {"appdb", "crm", "payments"}
    assert isinstance(row["entity_refs"], list)
    assert isinstance(row["disagreeing_fields"], list)


def test_a_proposal_row_carries_every_pinned_column(review_api: TestClient) -> None:
    row = get(review_api, "/api/proposals", page_size=1)["items"][0]
    assert set(row) >= PINNED_PROPOSAL_FIELDS
    assert isinstance(row["id"], str)
    assert isinstance(row["conflict_id"], str), (
        "filterGuard.ts compares proposal.conflict_id === query.conflict_id with ===; a "
        "number here makes every conflict-detail page warn that its own filter was ignored"
    )
    assert isinstance(row["confidence"], float)
    assert 0.0 <= row["confidence"] <= 1.0
    assert isinstance(row["sensitive"], bool)
    assert row["status"] in {
        "pending",
        "approved",
        "rejected",
        "applied",
        "rolled_back",
        "sensitive_hold",
    }


def test_a10_action_target_path_is_readable_from_the_action(review_api: TestClient) -> None:
    """A10: `action` carries the field path a fix would write, or the proposal is
    evidence-only and the dashboard renders "no field write"."""
    page = get(review_api, "/api/proposals", type="C9", page_size=5)
    assert page["items"]
    for row in page["items"]:
        assignments = row["action"]["set"]
        assert list(assignments) == ["appdb.enrollment.crm_deal_id"]


def test_a9_evidence_carries_observed_values(review_api: TestClient) -> None:
    """A9: `evidence.observed_values`, keyed by source-qualified field path.

    The committed packet nests it under `conflict`, so this records what the real
    key path is -- the dashboard reads `evidence.observed_values` and degrades to
    "-" when absent, which is A9's stated (loud) failure mode.
    """
    row = get(review_api, "/api/proposals", type="C6", page_size=1)["items"][0]
    assert "observed_values" in row["evidence"]["conflict"]
    assert row["evidence"]["schema"] == "keystone.evidence.v1"


# ---------------------------------------------------------------------------
# A2 -- per-id GETs
# ---------------------------------------------------------------------------


def test_a2_per_id_gets_exist_and_agree_with_the_list(review_api: TestClient) -> None:
    listed = get(review_api, "/api/conflicts", page_size=1)["items"][0]
    single = get(review_api, f"/api/conflicts/{listed['id']}")
    assert single["id"] == listed["id"]
    assert single["fingerprint"] == listed["fingerprint"]

    listed_proposal = get(review_api, "/api/proposals", page_size=1)["items"][0]
    single_proposal = get(review_api, f"/api/proposals/{listed_proposal['id']}")
    assert single_proposal["id"] == listed_proposal["id"]
    assert single_proposal["action"] == listed_proposal["action"]


def test_the_proposal_detail_reports_r24s_verdict(review_api: TestClient) -> None:
    """An addition to A2's shape: why this proposal is or is not auto-appliable."""
    held = get(review_api, "/api/proposals", status="sensitive_hold", page_size=1)["items"][0]
    detail = get(review_api, f"/api/proposals/{held['id']}")
    assert detail["auto_apply"]["allowed"] is False
    assert detail["auto_apply"]["reason"] == "sensitive_hold"


def test_a_missing_row_is_an_rfc7807_404(review_api: TestClient) -> None:
    response = review_api.get("/api/proposals/999999999", headers=ADMIN_HEADERS)
    assert response.status_code == 404
    body = response.json()
    assert set(body) >= {"type", "title", "status", "detail"}
    assert body["status"] == 404


# ---------------------------------------------------------------------------
# R20 -- scope
# ---------------------------------------------------------------------------


def test_a_client_key_sees_fewer_rows_than_admin(review_api: TestClient) -> None:
    """Row visibility: a client key sees only its own tenant's conflicts."""
    admin_total = get(review_api, "/api/proposals", page_size=1)["total"]
    client_page = review_api.get("/api/proposals", params={"page_size": 1}, headers=CLIENT_HEADERS)
    assert client_page.status_code == 200
    client_total = client_page.json()["total"]
    assert 0 < client_total < admin_total, (
        f"client scope returned {client_total} of {admin_total} proposals; the tenant "
        "wall is either absent or total"
    )


def test_a_missing_key_is_401_and_a_client_key_cannot_decide(review_api: TestClient) -> None:
    """R20: 401 for unauthenticated, 403 for authenticated-with-the-wrong-scope."""
    assert review_api.get("/api/conflicts").status_code == 401
    assert review_api.get("/api/conflicts", headers={"X-Api-Key": "nope"}).status_code == 401
    row = get(review_api, "/api/proposals", page_size=1)["items"][0]
    for action in ("approve", "reject", "apply"):
        response = review_api.post(f"/api/proposals/{row['id']}/{action}", headers=CLIENT_HEADERS)
        assert response.status_code == 403, (
            f"{action} accepted a client key; DESIGN pins reviewer actions as org-wide"
        )


def test_a_row_outside_the_tenant_is_404_not_403(review_api: TestClient) -> None:
    """The membership-oracle argument `recon.api.entities` documents, applied here."""
    admin_ids = [row["id"] for row in get(review_api, "/api/proposals", page_size=100)["items"]]
    client_ids = {
        row["id"]
        for row in review_api.get(
            "/api/proposals", params={"page_size": 100}, headers=CLIENT_HEADERS
        ).json()["items"]
    }
    hidden = next(pid for pid in admin_ids if pid not in client_ids)
    response = review_api.get(f"/api/proposals/{hidden}", headers=CLIENT_HEADERS)
    assert response.status_code == 404, (
        "a row the caller may not see must be NOT FOUND; 403 tells an unauthorised "
        "caller the row exists"
    )


# ---------------------------------------------------------------------------
# the dashboard's own vocabularies
# ---------------------------------------------------------------------------


def test_the_served_conflict_types_are_the_dashboards(review_api: TestClient) -> None:
    """The fourteen types `contract.ts` enumerates, read from the client source."""
    source = (DASHBOARD / "contract.ts").read_text()
    declared = json.loads(
        "["
        + source.split("export const CONFLICT_TYPES = [", 1)[1]
        .split("] as const", 1)[0]
        .replace("'", '"')
        .rstrip()
        .rstrip(",")
        + "]"
    )
    served = {row["type"] for row in get(review_api, "/api/conflicts", page_size=100)["items"]}
    assert served <= set(declared)
    for conflict_type in declared:
        page = get(review_api, "/api/conflicts", type=conflict_type, page_size=1)
        assert page["total"] > 0, f"{conflict_type} is in the client vocabulary and absent here"


@pytest.mark.parametrize("path", ["/api/conflicts", "/api/proposals"])
def test_an_out_of_range_page_still_reports_the_real_total(
    review_api: TestClient, path: str
) -> None:
    """A page past the end is empty; the filter that produced it still matched rows.

    `count(*) OVER ()` rides along on a page that has rows and has nothing to ride
    on when the page is empty. Reporting `total = 0` there would tell a reviewer
    the filter matched nothing -- so the endpoint pays for a second count in that
    one case, and this is what would go red if that were dropped as an
    optimisation.
    """
    first = get(review_api, path, page=1, page_size=10)
    assert first["total"] > 10
    beyond = get(review_api, path, page=first["total"], page_size=10)
    assert beyond["items"] == []
    assert beyond["total"] == first["total"], (
        f"{path} reported total={beyond['total']} on an out-of-range page and "
        f"total={first['total']} on the first one; the pagination control is lying"
    )
