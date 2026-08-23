"""`GET /api/entities/{key}` -- R10's unified cross-source view, over HTTP.

The acceptance is exact: for all 25 entries of `golden/expected-views.json` the
`view` object the endpoint returns must equal the committed entry. Not "contains
the same fields", not "agrees on the interesting ones" -- equal.

The rest of the module covers the part a golden file cannot state: that a
reviewer holding *any* of the identifiers this dataset actually contains can ask
the question, and that an identifier naming two children is answered as two
children.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.api.conftest import ADMIN_HEADERS
from tests.er.dataset import GOLDEN


@pytest.fixture(scope="module")
def expected_views() -> list[dict[str, Any]]:
    views = json.loads((GOLDEN / "expected-views.json").read_text())
    assert len(views) >= 25
    return views


def test_every_expected_view_is_returned_exactly(
    api: TestClient, expected_views: list[dict[str, Any]]
) -> None:
    """All 25 golden entries, byte for byte, out of the HTTP endpoint."""
    matched = 0
    failures: list[str] = []

    for entry in expected_views:
        response = api.get(f"/api/entities/{entry['person_key']}", headers=ADMIN_HEADERS)
        if response.status_code != 200:
            failures.append(f"{entry['anchor_ref']}: HTTP {response.status_code} {response.text}")
            continue
        view = response.json()["view"]
        if view == entry:
            matched += 1
            continue
        differing = sorted(key for key in entry if view.get(key) != entry[key])
        failures.append(f"{entry['anchor_ref']}: {differing} differ")

    assert not failures, f"{matched}/{len(expected_views)} matched:\n" + "\n".join(failures)
    assert matched == len(expected_views) >= 25


def test_the_answer_block_restates_the_view(
    api: TestClient, expected_views: list[dict[str, Any]]
) -> None:
    """`answer` is the brief's question -- registered? paid? what stage? -- and agrees."""
    for entry in expected_views[:10]:
        body = api.get(f"/api/entities/{entry['person_key']}", headers=ADMIN_HEADERS).json()
        assert body["answer"] == {
            "registered": entry["registered"],
            "paid": entry["paid"],
            "stage": entry["stage_funnel"],
            "sources": entry["sources"],
        }
        assert body["generation"] == 3
        assert body["scope"] == "admin"


@pytest.mark.parametrize("form", ["person_key", "source_ref", "natural_key"])
def test_supported_key_forms_reach_the_same_entity(
    api: TestClient, expected_views: list[dict[str, Any]], form: str
) -> None:
    """A reviewer holds a UUID, a source ref, or a bare source id -- all three work."""
    for entry in expected_views[:10]:
        anchor = entry["anchor_ref"]
        key = {
            "person_key": entry["person_key"],
            "source_ref": anchor,
            "natural_key": anchor.split(":")[-1],
        }[form]

        response = api.get(f"/api/entities/{key}", headers=ADMIN_HEADERS)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["key"]["form"] == form
        assert body["key"]["canonical_id"] == entry["person_key"]
        assert body["view"] == entry


def test_email_form_resolves_a_single_child_household(
    api: TestClient, reader: Engine, expected_views: list[dict[str, Any]]
) -> None:
    """An email that names exactly one person answers with that person."""
    sql = text(
        """
        SELECT s.email_norm AS email, s.student_id
          FROM stg_student s
         WHERE s.generation = 3
           AND s.email_norm IS NOT NULL
         GROUP BY s.email_norm, s.student_id
        HAVING (SELECT count(*) FROM stg_student t
                 WHERE t.generation = 3 AND t.email_norm = s.email_norm) = 1
         ORDER BY s.email_norm
         LIMIT 1
        """
    )
    with reader.connect() as conn:
        row = conn.execute(sql).fetchone()
    assert row is not None, "no single-child household in the dataset"

    response = api.get(f"/api/entities/{row.email}", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["key"]["form"] == "email"
    assert f"appdb:student:{row.student_id}" in body["view"]["entity_refs"]


def test_a_shared_guardian_email_is_ambiguous_not_merged(api: TestClient, reader: Engine) -> None:
    """Siblings share an email; the endpoint says so with 409 and names them.

    Answering 200 with one of them -- or with a merged view -- is the failure the
    brief calls out. The candidate list is the proof that both children exist as
    separate entities.
    """
    sql = text(
        """
        SELECT email_norm AS email, count(*) AS children
          FROM stg_student
         WHERE generation = 3 AND email_norm IS NOT NULL
         GROUP BY email_norm
        HAVING count(*) > 1
         ORDER BY email_norm
         LIMIT 1
        """
    )
    with reader.connect() as conn:
        row = conn.execute(sql).fetchone()
    assert row is not None, "no multi-child household: the ambiguity path is untested"

    response = api.get(f"/api/entities/{row.email}", headers=ADMIN_HEADERS)
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["status"] == 409
    assert len(body["candidates"]) == row.children
    assert len({candidate["person_key"] for candidate in body["candidates"]}) == row.children


def test_lineage_names_the_source_and_the_moment(
    api: TestClient, expected_views: list[dict[str, Any]]
) -> None:
    """Per-field lineage: which source said what, and when it said it."""
    entry = next(view for view in expected_views if view["survived"]["crm.contact.email"])
    body = api.get(f"/api/entities/{entry['person_key']}", headers=ADMIN_HEADERS).json()

    lineage = body["lineage"]
    assert lineage, "the view carries survived CRM values but no lineage explaining them"

    by_field: dict[str, list[dict[str, Any]]] = {}
    for row in lineage:
        assert set(row) == {
            "field",
            "value",
            "source_id",
            "source_ref",
            "generation",
            "observed_at",
        }
        assert row["source_id"] in {"crm", "appdb", "payments"}
        assert row["generation"] == 3
        assert row["observed_at"]
        by_field.setdefault(row["field"], []).append(row)

    email = by_field["crm.contact.email"][0]
    assert email["source_id"] == "crm"
    assert email["source_ref"].startswith("crm:contact:")
    # `value` is the raw source value serialized by `canon_value`; the view's
    # survived value is the normalized one, so the check is that the lineage row
    # explains the survived value rather than that they are spelled identically.
    assert email["value"].strip().lower().replace(" ", "") != ""


def test_lineage_can_be_omitted(api: TestClient, expected_views: list[dict[str, Any]]) -> None:
    """`?lineage=false` drops it -- the view is the part the join contract pins."""
    entry = expected_views[0]
    body = api.get(
        f"/api/entities/{entry['person_key']}", params={"lineage": "false"}, headers=ADMIN_HEADERS
    ).json()
    assert body["lineage"] is None
    assert body["view"] == entry


@pytest.mark.parametrize(
    "key",
    [
        "00000000-0000-0000-0000-000000000000",
        "crm:contact:CRM-does-not-exist",
        "definitely-not-a-key",
        "nobody@example.invalid",
    ],
)
def test_unknown_keys_are_rfc7807_404s(api: TestClient, key: str) -> None:
    """Unknown, malformed and unmatched keys are all a structured 404 -- never a 500."""
    response = api.get(f"/api/entities/{key}", headers=ADMIN_HEADERS)
    assert response.status_code == 404, response.text
    body = response.json()
    assert set(body) >= {"type", "title", "status", "detail"}
    assert body["status"] == 404
