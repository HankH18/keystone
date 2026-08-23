"""`GET /api/entities/*` on the assembled service (R10, R20, R2's "never a 500").

Reached through `create_app()`, over the canonical layer a real
`POST /internal/sync` built -- so a 200 here means the whole chain works:
fixtures -> adapters -> landing -> staging -> ER cascade -> canonical row ->
scope filter -> response.

The fault half of the module is the R2 rule applied to the query side: *never a
500*. Two inputs answered `text/plain` "Internal Server Error" for both key
scopes -- `GET /api/entities/%00` and `GET /api/entities/a%00b` -- because a NUL
reached psycopg, which refuses the parameter with a plain `ValueError` that no
handler caught. A sweep of the rest of the surface found a third: the org-wide
index casts `after` to `uuid` in SQL, so `?after=notauuid`, `?after=` and
`?after=%00` were 500s too. All of them are now the one identifier rule
(`recon.adapters.identifiers`) answering with an RFC7807 document.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.integration.conftest import (
    ADMIN_HEADERS,
    CLIENT_HEADERS,
    CLIENT_TENANT,
)

#: Inputs that used to escape as a 5xx, plus the neighbours a sweep of the same
#: shape turns up. Every one of them must be a problem document.
UNSTORABLE_KEYS: dict[str, str] = {
    "a bare NUL": "\x00",
    "a NUL mid-string": "a\x00b",
    "a NUL inside a source_ref": "crm:contact:a\x00b",
    "a NUL inside an email": "a\x00b@example.test",
    "a control character": "ctrl\x07char",
    "a DEL": "a\x7fb",
    "a C1 control": "a\x85b",
    "whitespace only": " ",
    "over-length": "x" * 5000,
}

#: Keys that are perfectly storable and simply match nothing: still a 404.
UNKNOWN_KEYS: tuple[str, ...] = (
    "CRM-does-not-exist",
    "nobody@example.test",
    "crm:contact:CRM-does-not-exist",
    "00000000-0000-0000-0000-000000000000",
    "\U0001f4a9",
)


def _get(service: TestClient, key: str, headers: dict[str, str]) -> Any:
    return service.get("/api/entities/" + urllib.parse.quote(key, safe=""), headers=headers)


@pytest.fixture(scope="module")
def client_entity(reader: Engine, synced: dict[str, Any]) -> dict[str, Any]:
    """A canonical row the committed **client** key owns, straight from the DB."""
    sql = text(
        """
        SELECT canonical_id::text AS canonical_id, current
          FROM entities
         WHERE current ->> 'tenant' = :tenant
           AND starts_with(current ->> 'anchor_ref', 'appdb:student:')
         ORDER BY canonical_id
         LIMIT 1
        """
    )
    with reader.connect() as conn:
        row = conn.execute(sql, {"tenant": CLIENT_TENANT}).fetchone()
    assert row is not None, f"no student-anchored entity is assigned to {CLIENT_TENANT!r}"
    return {"canonical_id": row.canonical_id, "current": dict(row.current)}


@pytest.fixture(scope="module")
def other_entity(reader: Engine, synced: dict[str, Any]) -> dict[str, Any]:
    """A canonical row assigned to the tenant with no key -- the wall's far side."""
    sql = text(
        """
        SELECT canonical_id::text AS canonical_id
          FROM entities
         WHERE current ->> 'tenant' <> :tenant
         ORDER BY canonical_id
         LIMIT 1
        """
    )
    with reader.connect() as conn:
        row = conn.execute(sql, {"tenant": CLIENT_TENANT}).fetchone()
    assert row is not None, "the dataset holds only one tenant; isolation is untestable"
    return {"canonical_id": row.canonical_id}


# ======================================================================================
# R10 -- the unified view, on the layer the sync built
# ======================================================================================


def test_the_query_endpoint_answers_for_a_real_key(
    service: TestClient, client_entity: dict[str, Any]
) -> None:
    """Registered? paid? what stage? -- answered end to end from a person key."""
    response = _get(service, client_entity["canonical_id"], ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["key"]["form"] == "person_key"
    assert body["key"]["canonical_id"] == client_entity["canonical_id"]
    assert set(body["answer"]) == {"registered", "paid", "stage", "sources"}
    assert isinstance(body["answer"]["registered"], bool)
    assert body["lineage"], "R1 pins field-level lineage on every record"


def test_the_natural_key_form_reaches_the_same_row(
    service: TestClient, client_entity: dict[str, Any]
) -> None:
    """The identifier a reviewer is holding is a source id, not a UUID."""
    anchor = client_entity["current"]["anchor_ref"]
    natural_key = anchor.split(":")[-1]
    response = _get(service, natural_key, ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["key"]["canonical_id"] == client_entity["canonical_id"]


# ======================================================================================
# R20 -- scope
# ======================================================================================


def test_the_client_key_reads_its_own_row(
    service: TestClient, client_entity: dict[str, Any]
) -> None:
    response = _get(service, client_entity["canonical_id"], CLIENT_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    # `scope` is the principal's scope name; `tenant` is the row's owner. The
    # client key's scope resolves to the tenant label, and the row it just read
    # is one of that tenant's -- which is the filter doing its job.
    assert body["scope"] == "client"
    assert body["tenant"] == CLIENT_TENANT


def test_the_client_key_cannot_read_another_tenants_row(
    service: TestClient, other_entity: dict[str, Any]
) -> None:
    """404, not 403: a 403 would confirm the row exists to a caller who may not see it."""
    assert _get(service, other_entity["canonical_id"], ADMIN_HEADERS).status_code == 200
    response = _get(service, other_entity["canonical_id"], CLIENT_HEADERS)
    assert response.status_code == 404, response.text


def test_the_org_wide_index_is_admin_only(service: TestClient, synced: dict[str, Any]) -> None:
    assert service.get("/api/entities?limit=1", headers=ADMIN_HEADERS).status_code == 200
    assert service.get("/api/entities?limit=1", headers=CLIENT_HEADERS).status_code == 403


def test_an_unknown_api_key_is_401(service: TestClient, synced: dict[str, Any]) -> None:
    response = service.get("/api/entities?limit=1", headers={"X-Api-Key": "not-a-key"})
    assert response.status_code == 401


# ======================================================================================
# never a 500
# ======================================================================================


@pytest.mark.parametrize(("why", "key"), sorted(UNSTORABLE_KEYS.items()))
@pytest.mark.parametrize("headers", [ADMIN_HEADERS, CLIENT_HEADERS], ids=["admin", "client"])
def test_an_unstorable_key_is_a_problem_document_not_a_500(
    service: TestClient, synced: dict[str, Any], why: str, key: str, headers: dict[str, str]
) -> None:
    """Both key scopes, because both answered `text/plain` before."""
    response = _get(service, key, headers)
    assert response.status_code < 500, (
        f"{why} answered {response.status_code} {response.text[:120]!r}; DESIGN pins an "
        "RFC7807 problem document and R2 pins 'never a 500'"
    )
    assert response.status_code == 422, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 422
    assert body["field"] == "key"
    assert "rule" in body, "the rejection quotes the one identifier rule"


@pytest.mark.parametrize("key", UNKNOWN_KEYS)
def test_a_storable_key_that_matches_nothing_is_a_404_problem(
    service: TestClient, synced: dict[str, Any], key: str
) -> None:
    """The validator must not turn "not found" into "malformed"."""
    response = _get(service, key, ADMIN_HEADERS)
    assert response.status_code == 404, response.text
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("cursor", ["notauuid", "", "\x00", "x" * 300, "12345"])
def test_a_bad_index_cursor_is_a_problem_document_not_a_500(
    service: TestClient, synced: dict[str, Any], cursor: str
) -> None:
    """`after` is cast to `uuid` in SQL; every unparseable value used to be a 500."""
    response = service.get(
        "/api/entities?after=" + urllib.parse.quote(cursor, safe=""), headers=ADMIN_HEADERS
    )
    assert response.status_code == 422, (
        f"{cursor!r} -> {response.status_code} {response.text[:120]}"
    )
    assert response.headers["content-type"].startswith("application/problem+json")


def test_the_index_cursor_still_paginates(service: TestClient, synced: dict[str, Any]) -> None:
    """The control: a real cursor is not collateral damage of the validation."""
    first = service.get("/api/entities?limit=2", headers=ADMIN_HEADERS).json()
    assert first["count"] == 2, first
    second = service.get(
        f"/api/entities?limit=2&after={first['next_after']}", headers=ADMIN_HEADERS
    ).json()
    assert second["count"] == 2
    assert {row["person_key"] for row in first["entities"]}.isdisjoint(
        row["person_key"] for row in second["entities"]
    )
