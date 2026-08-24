"""`GET /api/incidents`: the envelope, the auth, and the truth about the mount.

Live database, real conflicts, real clusters, real API keys from
`api_clients` (migration 0003 seeds the two demo rows).

**Read this before believing the green.** These tests mount
`recon.api.incidents.router` on a bare `FastAPI()` themselves, to exercise the
router's own contract without the rest of the application in the way. That is
exactly the shape of the two routers this repository has already shipped
unreachable: every assertion in their suites was true and both endpoints 404'd
in the running service. `recon/app.py` DOES mount this router now, and
:func:`test_the_router_is_mounted_in_the_real_app` asserts that here rather than
leaving this file's green to mean only "the router works if someone mounts it".
`tests/integration/test_route_table.py::test_the_incidents_router_is_served_by_
the_factory` binds the same fact from the other side.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from recon.api.auth import install_problem_handler
from recon.api.incidents import MEMBERS_PER_INCIDENT, router
from recon.api.review import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from recon.api.review import _page_params as review_page_params
from recon.app import create_app
from recon.budget import PriceTable
from recon.incidents import MockEmbeddingProvider, cluster_conflicts
from tests.incidents.conftest import golden_records, insert_golden_conflicts, run_id_for, unique

#: The committed demo keys, seeded by migration 0003. Plaintext lives only in
#: that migration and in `.env.example`; only the sha256 is ever stored.
ADMIN_KEY = "keystone-demo-admin-8c25e0b71a94f36d"
CLIENT_KEY = "keystone-demo-client-3f7a19c4e2b84d05"

#: A deterministic slice of the golden set. Small enough that a full HTTP suite
#: stays quick, taken by a stride over a file written in a pinned order so it is
#: as reproducible as the whole thing.
API_STRIDE = 17


@pytest.fixture(scope="module")
def api_client() -> Iterator[TestClient]:
    """The incidents router on a bare app, with the RFC7807 handler installed.

    Bare rather than `create_app()` for one reason only: `create_app()` does not
    include this router yet, so mounting it here is the only way to exercise the
    endpoint at all -- and the test below asserts that this is still true.
    """
    app = FastAPI()
    install_problem_handler(app)
    app.include_router(router)
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def clustered(owner_engine: Engine, embedding_prices: PriceTable) -> Iterator[dict[str, object]]:
    """A slice of the golden set, clustered and written, cleaned up afterwards."""
    run_tag = unique("api")
    scope = f"run:{run_tag}"
    import os

    from recon.budget import DAILY_SCOPE_ENV

    previous = os.environ.get(DAILY_SCOPE_ENV)
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO budget_ledger (scope, cap_microusd, spent_microusd) "
                "VALUES (:s, :cap, 0)"
            ),
            {"s": scope, "cap": 10_000_000},
        )
    os.environ[DAILY_SCOPE_ENV] = scope

    conflict_ids = insert_golden_conflicts(
        owner_engine, golden_records(step=API_STRIDE), run_tag=run_tag
    )
    run = cluster_conflicts(
        run_id=run_id_for(scope),
        provider=MockEmbeddingProvider(),
        table=embedding_prices,
    )
    yield {"run": run, "conflict_ids": conflict_ids}

    if previous is None:
        os.environ.pop(DAILY_SCOPE_ENV, None)
    else:
        os.environ[DAILY_SCOPE_ENV] = previous
    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM conflict_incidents WHERE incident_id = ANY(:ids)"),
            {"ids": list(run.incident_ids)},
        )
        conn.execute(
            text("DELETE FROM incidents WHERE id = ANY(:ids)"), {"ids": list(run.incident_ids)}
        )
        # Only the conflicts this fixture actually inserted: `first_seen_run` is
        # its own tag, and `insert_golden_conflicts` never touches a row that was
        # already there. Membership rows are gone with the incidents above, so an
        # FK violation here would mean some other suite is pointing at these
        # conflicts -- which should fail loudly rather than be deleted around.
        conn.execute(text("DELETE FROM conflicts WHERE first_seen_run = :run"), {"run": run_tag})
        conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": scope})
        conn.execute(text("DELETE FROM audit_log WHERE subject LIKE :p"), {"p": f"%{run_tag}%"})
        conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": scope})


def test_the_router_is_mounted_in_the_real_app() -> None:
    """**The mount, asserted rather than described.**

    This replaces `test_the_router_is_not_mounted_in_the_real_app_yet`, which was
    written to go red the day `create_app()` gained the mount and **could not**:
    it read `create_app().routes` FLAT, and FastAPI wraps every `include_router`
    call in an `_IncludedRouter` whose own `path` is `None`. That set holds four
    paths -- `/openapi.json`, `/docs`, `/docs/oauth2-redirect`, `/redoc` -- and
    no API path at all, so `"/api/incidents" not in paths` was true whether or
    not the router was mounted. The mount landed, the tripwire stayed green, and
    this module's docstring went on describing a 404 that no longer happened.

    So the enumeration here is the OpenAPI document -- what a client is actually
    offered -- rather than a flat walk of a nested container, and the assertion
    is the true direction. `tests/integration/test_route_table.py` walks the
    container correctly and binds the same fact from the other side; the two
    disagree only if one of them breaks.
    """
    paths = set(create_app().openapi()["paths"])
    assert "/api/incidents" in paths, (
        "the real create_app() does not serve GET /api/incidents: the router is "
        "built and not mounted, which is how /internal/sync and /api/entities/{key} "
        f"shipped unreachable with green suites. Served paths: {sorted(paths)}"
    )


def test_the_envelope_is_the_one_the_dashboard_contract_pins(
    api_client: TestClient, clustered: dict[str, object]
) -> None:
    """A1: `{items, page, page_size, total}` and nothing else.

    `warnings` is deliberately absent -- that is the client's verdict about the
    response, not the service's, exactly as `recon.api.review._page` documents.
    """
    response = api_client.get("/api/incidents", headers={"X-Api-Key": ADMIN_KEY})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "page", "page_size", "total"}
    assert body["page"] == 1
    assert body["page_size"] == DEFAULT_PAGE_SIZE
    assert body["total"] == clustered["run"].incidents  # type: ignore[attr-defined]


def test_an_incident_carries_its_label_model_and_member_conflicts(
    api_client: TestClient, clustered: dict[str, object]
) -> None:
    """One item is one incident: what it is, what embedded it, and who is in it."""
    body = api_client.get("/api/incidents", headers={"X-Api-Key": ADMIN_KEY}).json()
    item = body["items"][0]
    assert set(item) == {
        "id",
        "label",
        "embedding_model",
        "embedding_dim",
        "created_at",
        "member_count",
        "members",
    }
    # `id` is a string for the same reason `recon.api.review._conflict_row`'s is:
    # the column is bigint and the dashboard compares ids with `===`.
    assert isinstance(item["id"], str)
    assert item["embedding_model"] == "mock-embedding-v1"
    assert item["embedding_dim"] == 256
    assert item["member_count"] >= 1
    assert item["members"]
    member = item["members"][0]
    assert set(member) == {
        "id",
        "fingerprint",
        "type",
        "rule_id",
        "sources",
        "disagreeing_fields",
        "status",
        "distance",
    }
    assert isinstance(member["id"], str)
    assert 0.0 <= member["distance"] <= 2.0


def test_member_count_is_the_truth_even_when_members_is_truncated(
    api_client: TestClient, clustered: dict[str, object]
) -> None:
    """The silent-failure guard: a UI reading `members.length` must not be lied to.

    The biggest incident in this slice has far more members than
    :data:`MEMBERS_PER_INCIDENT`, so `members` is truncated and `member_count`
    is not. An endpoint that reported the truncated length would under-report an
    incident of hundreds as one of twenty, and nothing would look wrong.
    """
    body = api_client.get(
        "/api/incidents", params={"members": 3}, headers={"X-Api-Key": ADMIN_KEY}
    ).json()
    biggest = body["items"][0]
    assert biggest["member_count"] > 3
    assert len(biggest["members"]) == 3

    default = api_client.get("/api/incidents", headers={"X-Api-Key": ADMIN_KEY}).json()
    assert len(default["items"][0]["members"]) == MEMBERS_PER_INCIDENT


def test_incidents_are_served_biggest_first(
    api_client: TestClient, clustered: dict[str, object]
) -> None:
    """R25 is about noticing a pattern early; the 500-member incident is the pattern."""
    body = api_client.get(
        "/api/incidents", params={"page_size": MAX_PAGE_SIZE}, headers={"X-Api-Key": ADMIN_KEY}
    ).json()
    counts = [item["member_count"] for item in body["items"]]
    assert counts == sorted(counts, reverse=True)


def test_pagination_walks_every_incident_exactly_once(
    api_client: TestClient, clustered: dict[str, object]
) -> None:
    """`page`/`page_size` are 1-based and disjoint, and `total` is the whole set."""
    first = api_client.get(
        "/api/incidents", params={"page": 1, "page_size": 5}, headers={"X-Api-Key": ADMIN_KEY}
    ).json()
    second = api_client.get(
        "/api/incidents", params={"page": 2, "page_size": 5}, headers={"X-Api-Key": ADMIN_KEY}
    ).json()
    assert first["total"] == second["total"]
    ids = [item["id"] for item in first["items"]] + [item["id"] for item in second["items"]]
    assert len(set(ids)) == len(ids)
    assert len(first["items"]) == 5


def test_a_page_past_the_end_still_reports_the_real_total(
    api_client: TestClient, clustered: dict[str, object]
) -> None:
    """`count(*) OVER ()` rides on pages that have rows; an empty page pays for a count.

    Reporting `total = 0` there would tell a dashboard the set is empty when it
    is not -- the same quiet lie `recon.api.review._total` exists to prevent.
    """
    body = api_client.get(
        "/api/incidents", params={"page": 500}, headers={"X-Api-Key": ADMIN_KEY}
    ).json()
    assert body["items"] == []
    assert body["total"] == clustered["run"].incidents  # type: ignore[attr-defined]


def test_page_params_agree_with_the_reviewer_surface() -> None:
    """The clamp is copied from `recon.api.review`; the copy must not drift.

    Two endpoints that cap at different sizes is exactly the drift that makes a
    dashboard believe one limit and get another.
    """
    from recon.api.incidents import _page_params

    for page in (0, 1, 2, 7, 500):
        for size in (0, 1, 25, 100, 101, 10_000):
            assert _page_params(page, size) == review_page_params(page, size)


def test_a_missing_key_is_401_and_a_client_key_is_403(api_client: TestClient) -> None:
    """R20. An incident is an org-wide aggregate, so `client` is refused by scope.

    403, not 404: this is a judgement about the *operation*, and telling a caller
    "your key is fine, your scope is not" is what stops it from rotating a
    working key in response to a permissions problem. The 404-for-invisible rule
    `recon.api.review` follows is about rows belonging to one tenant; an
    incident's `member_count` spans every tenant, so there is no row-level answer
    that does not leak it.
    """
    anonymous = api_client.get("/api/incidents")
    assert anonymous.status_code == 401
    assert anonymous.headers["content-type"].startswith("application/problem+json")
    assert set(anonymous.json()) >= {"type", "title", "status", "detail"}

    scoped = api_client.get("/api/incidents", headers={"X-Api-Key": CLIENT_KEY})
    assert scoped.status_code == 403
    assert scoped.json()["status"] == 403


def test_an_oversized_page_is_rejected_by_the_server(api_client: TestClient) -> None:
    """R11's non-goal is explicit: never hand a client 100k rows."""
    response = api_client.get(
        "/api/incidents",
        params={"page_size": MAX_PAGE_SIZE + 1},
        headers={"X-Api-Key": ADMIN_KEY},
    )
    assert response.status_code == 422
