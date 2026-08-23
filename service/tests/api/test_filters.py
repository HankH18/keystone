"""Server-side filtering (R11), and contract assumption **A8** in particular.

A8 is the one assumption in `dashboard/src/lib/contract.ts` with a SILENT
failure mode: `/api/proposals` is asked to filter by `source` and `type`, the
`proposals` table has neither column, and a service that ignores an unknown
query parameter answers 200 with the UNFILTERED page. The reviewer then works
from wrong rows under a heading that says otherwise, with nothing red anywhere.

The client cannot catch it -- `filterGuard.ts` raises an `unverifiable` warning
because a proposal row carries nothing to check the filter against. So it is
caught here instead, on the server, against the real graded store:

* every filter, applied alone, returns only matching rows (`test_*_filter_*`);
* every filter, applied alone, returns FEWER rows than no filter -- otherwise
  "every row matches" would pass vacuously against a filter that did nothing;
* the combination of two filters is the intersection, not one of them;
* a value outside the committed vocabulary is a 422, never an ignored parameter.

The last one matters as much as the rest: rejecting an unservable filter is what
makes "the response is filtered" a property of the endpoint rather than a hope.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import ADMIN_HEADERS

PAGE = 100


def page(client: TestClient, path: str, **params: Any) -> dict[str, Any]:
    response = client.get(path, params={"page_size": PAGE, **params}, headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(scope="module")
def unfiltered(review_api: TestClient) -> dict[str, int]:
    return {
        "/api/conflicts": page(review_api, "/api/conflicts")["total"],
        "/api/proposals": page(review_api, "/api/proposals")["total"],
    }


# ---------------------------------------------------------------------------
# A8 -- the two filters that need a JOIN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("conflict_type", ["C2", "C4", "C6", "C9", "C14"])
def test_a8_the_proposals_type_filter_is_applied(
    review_api: TestClient, unfiltered: dict[str, int], conflict_type: str
) -> None:
    """`type` is not a column of `proposals`; it is a predicate on the joined conflict."""
    body = page(review_api, "/api/proposals", type=conflict_type)
    assert 0 < body["total"] < unfiltered["/api/proposals"], (
        f"type={conflict_type} returned {body['total']} of "
        f"{unfiltered['/api/proposals']} proposals -- the filter did nothing"
    )
    assert body["items"]
    assert {row["conflict_type"] for row in body["items"]} == {conflict_type}


@pytest.mark.parametrize("source", ["appdb", "crm", "payments"])
def test_a8_the_proposals_source_filter_is_applied(
    review_api: TestClient, unfiltered: dict[str, int], source: str
) -> None:
    """`source` likewise: `conflicts.sources @> [...]`, applied in SQL."""
    body = page(review_api, "/api/proposals", source=source)
    assert 0 < body["total"] < unfiltered["/api/proposals"]
    assert body["items"]
    for row in body["items"]:
        assert source in row["conflict_sources"], (
            f"proposal {row['id']} came back for source={source} and its conflict names "
            f"{row['conflict_sources']}"
        )


def test_a8_the_two_filters_intersect(review_api: TestClient) -> None:
    """Both at once is the intersection, not whichever one the query noticed."""
    both = page(review_api, "/api/proposals", type="C4", source="payments")
    only_type = page(review_api, "/api/proposals", type="C4")
    assert both["total"] <= only_type["total"]
    for row in both["items"]:
        assert row["conflict_type"] == "C4"
        assert "payments" in row["conflict_sources"]


def test_a8_verified_against_the_conflicts_endpoint(review_api: TestClient) -> None:
    """The cross-check `filterGuard.ts` tells a reviewer to do by hand.

    Every proposal returned for one type must belong to a conflict the conflicts
    endpoint also serves under that type. Two endpoints, one answer.
    """
    proposals = page(review_api, "/api/proposals", type="C9")["items"]
    assert proposals
    for row in proposals[:10]:
        conflict = review_api.get(
            f"/api/conflicts/{row['conflict_id']}", headers=ADMIN_HEADERS
        ).json()
        assert conflict["type"] == "C9"


# ---------------------------------------------------------------------------
# the filters the client CAN verify
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "sensitive_hold"])
def test_the_proposal_status_filter_is_applied(
    review_api: TestClient, unfiltered: dict[str, int], status: str
) -> None:
    body = page(review_api, "/api/proposals", status=status)
    assert 0 < body["total"] < unfiltered["/api/proposals"]
    assert {row["status"] for row in body["items"]} == {status}


def test_a3_the_conflict_id_filter_is_applied(review_api: TestClient) -> None:
    """A3: a conflict detail page shows ITS proposal, not someone else's."""
    proposal = page(review_api, "/api/proposals")["items"][0]
    body = page(review_api, "/api/proposals", conflict_id=proposal["conflict_id"])
    assert body["total"] >= 1
    assert {row["conflict_id"] for row in body["items"]} == {proposal["conflict_id"]}


@pytest.mark.parametrize("conflict_type", ["C1", "C6", "C14"])
def test_the_conflicts_type_filter_is_applied(
    review_api: TestClient, unfiltered: dict[str, int], conflict_type: str
) -> None:
    body = page(review_api, "/api/conflicts", type=conflict_type)
    assert 0 < body["total"] < unfiltered["/api/conflicts"]
    assert {row["type"] for row in body["items"]} == {conflict_type}


@pytest.mark.parametrize("source", ["appdb", "crm", "payments"])
def test_the_conflicts_source_filter_is_applied(
    review_api: TestClient, unfiltered: dict[str, int], source: str
) -> None:
    body = page(review_api, "/api/conflicts", source=source)
    assert 0 < body["total"] < unfiltered["/api/conflicts"]
    for row in body["items"]:
        assert source in row["sources"]


def test_the_conflicts_status_filter_is_applied(
    review_api: TestClient, unfiltered: dict[str, int]
) -> None:
    body = page(review_api, "/api/conflicts", status="open")
    assert 0 < body["total"] <= unfiltered["/api/conflicts"]
    assert {row["status"] for row in body["items"]} == {"open"}


# ---------------------------------------------------------------------------
# an unservable filter is refused, never ignored
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/conflicts", {"type": "C99"}),
        ("/api/conflicts", {"source": "salesforce"}),
        ("/api/proposals", {"type": "nope"}),
        ("/api/proposals", {"source": "salesforce"}),
        ("/api/proposals", {"status": "half-approved"}),
        ("/api/proposals", {"conflict_id": "not-a-number"}),
    ],
)
def test_an_unknown_filter_value_is_422(
    review_api: TestClient, path: str, params: dict[str, str]
) -> None:
    """A8's failure mode, closed at the source: nothing is silently dropped."""
    response = review_api.get(path, params=params, headers=ADMIN_HEADERS)
    assert response.status_code == 422, (
        f"{path} answered {response.status_code} for {params}; an unservable filter must "
        "be refused, because a 200 with unfiltered rows is the silent failure A8 names"
    )
    body = response.json()
    assert set(body) >= {"type", "title", "status", "detail"}
