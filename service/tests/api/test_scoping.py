"""R20 tenant scoping: the door, and the walls.

A suite that only checks 401s proves the door is locked. It says nothing about
whether an authenticated client can walk into another tenant's rows, which is the
property R20 is actually about. So every isolation assertion here is made
**twice**, on the same row: the client key reads the entity it owns, and gets
nothing for the entity it does not -- while the admin key reads both, which is
what proves the second row exists and was withheld rather than absent.

Why a withheld row is 404 and not 403: with 403 the response distinguishes "no
such entity" from "an entity you may not see", which is a membership oracle over
every key a caller can guess. The 403 of R20 lives on the operation a scope
genuinely gates -- the org-wide index -- and is asserted below.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.api.conftest import (
    ADMIN_HEADERS,
    CLIENT_HEADERS,
    CLIENT_TENANT,
    OTHER_TENANT,
)

_ENDPOINTS = ["/api/entities/00000000-0000-0000-0000-000000000000", "/api/entities"]


@pytest.mark.parametrize("path", _ENDPOINTS)
def test_missing_key_is_401(api: TestClient, path: str) -> None:
    response = api.get(path)
    assert response.status_code == 401, response.text
    assert response.json()["status"] == 401


@pytest.mark.parametrize("path", _ENDPOINTS)
def test_unknown_key_is_401(api: TestClient, path: str) -> None:
    response = api.get(path, headers={"X-Api-Key": "not-a-real-key"})
    assert response.status_code == 401, response.text


def test_client_key_reads_its_own_row(api: TestClient, client_entity: dict[str, Any]) -> None:
    """POSITIVE: the wall has a door, and the client's own key opens it."""
    key = client_entity["canonical_id"]
    client = api.get(f"/api/entities/{key}", headers=CLIENT_HEADERS)
    admin = api.get(f"/api/entities/{key}", headers=ADMIN_HEADERS)

    assert client.status_code == 200, client.text
    assert admin.status_code == 200, admin.text
    assert client.json()["view"] == admin.json()["view"]
    assert client.json()["tenant"] == CLIENT_TENANT
    assert client.json()["scope"] == "client"


def test_client_key_gets_nothing_for_another_tenants_row(
    api: TestClient, other_entity: dict[str, Any]
) -> None:
    """NEGATIVE: same endpoint, same shape of key, a row the client does not own."""
    key = other_entity["canonical_id"]
    client = api.get(f"/api/entities/{key}", headers=CLIENT_HEADERS)
    admin = api.get(f"/api/entities/{key}", headers=ADMIN_HEADERS)

    assert admin.status_code == 200, "the row must exist, or the 404 below proves nothing"
    assert admin.json()["tenant"] == OTHER_TENANT
    assert client.status_code == 404, client.text
    body = client.json()
    assert "view" not in body and "answer" not in body and "lineage" not in body
    assert other_entity["current"]["anchor_ref"] not in client.text
    assert other_entity["current"]["survived"]["appdb.student.last_name"] not in client.text


def test_scoping_survives_every_key_form(api: TestClient, other_entity: dict[str, Any]) -> None:
    """The filter is on the row, so no alternative identifier walks around it."""
    anchor = other_entity["current"]["anchor_ref"]
    for key in (other_entity["canonical_id"], anchor, anchor.split(":")[-1]):
        response = api.get(f"/api/entities/{key}", headers=CLIENT_HEADERS)
        assert response.status_code == 404, f"{key} leaked: {response.text}"
        assert api.get(f"/api/entities/{key}", headers=ADMIN_HEADERS).status_code == 200


def test_client_key_cannot_enumerate_the_org(api: TestClient) -> None:
    """403, not 404: the org-wide index is the operation the scope gates."""
    response = api.get("/api/entities", headers=CLIENT_HEADERS)
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["status"] == 403
    assert "admin" in body["detail"]


def test_admin_index_sees_both_tenants(api: TestClient, reader: Engine) -> None:
    """The admin scope is org-wide, which is what makes the client's 404 a wall."""
    seen: set[str] = set()
    after: str | None = None
    for _ in range(20):
        params: dict[str, Any] = {"limit": 200}
        if after:
            params["after"] = after
        body = api.get("/api/entities", params=params, headers=ADMIN_HEADERS).json()
        seen.update(row["tenant"] for row in body["entities"])
        after = body["next_after"]
        if not after or len(seen) > 1:
            break
    assert seen == {CLIENT_TENANT, OTHER_TENANT}, f"admin saw tenants {sorted(seen)}"


def test_both_tenants_actually_hold_rows(reader: Engine) -> None:
    """The partition is real, not a label every row happens to share."""
    sql = text(
        "SELECT current ->> 'tenant' AS tenant, count(*) AS n FROM entities GROUP BY 1 ORDER BY 1"
    )
    with reader.connect() as conn:
        counts = {row.tenant: row.n for row in conn.execute(sql)}
    assert set(counts) == {CLIENT_TENANT, OTHER_TENANT}, counts
    assert min(counts.values()) > 1000, f"a tenant holds too few rows to be a partition: {counts}"


def test_ambiguity_does_not_leak_across_tenants(api: TestClient, reader: Engine) -> None:
    """A shared guardian email is 409 for admin and 404 for a client that owns neither.

    The 409 body names candidate `person_key`s, so it is the one response that
    could disclose a person outside the caller's scope. It is built from the
    already-filtered rows, and this asserts that.
    """
    sql = text(
        """
        SELECT s.email_norm AS email
          FROM stg_student s
          JOIN entity_links el
            ON el.generation = 3 AND el.source_ref = 'appdb:student:' || s.student_id
          JOIN entities e ON e.canonical_id = el.canonical_id
         WHERE s.generation = 3 AND s.email_norm IS NOT NULL
           AND e.current ->> 'tenant' = :tenant
         GROUP BY s.email_norm
        HAVING count(DISTINCT el.canonical_id) > 1
         ORDER BY s.email_norm
         LIMIT 1
        """
    )
    with reader.connect() as conn:
        row = conn.execute(sql, {"tenant": OTHER_TENANT}).fetchone()
    if row is None:  # pragma: no cover - the partition puts households on both sides
        pytest.skip(f"no multi-child household landed in {OTHER_TENANT}")

    assert api.get(f"/api/entities/{row.email}", headers=ADMIN_HEADERS).status_code == 409
    denied = api.get(f"/api/entities/{row.email}", headers=CLIENT_HEADERS)
    assert denied.status_code == 404, denied.text
    assert "candidates" not in denied.json()
