"""Malformed payloads over HTTP: RFC7807 4xx, logged, counted -- never a 500 (R2).

R2 is a statement about what a *client* sees, so these tests drive the real
FastAPI app with a real `TestClient` and assert on real responses. Each of the 24
committed cases is posted and its `expect_code` -- read from
`fixtures/malformed/cases.jsonl`, not written here -- is required of the response.

Three failure modes are each asserted separately because they are three different
bugs and only one of them looks like an error at the client:

* **a 500** would say "we broke", not "your payload is broken";
* **a silent skip** would answer 200 while dropping the row, which is the failure
  R2 names explicitly and is invisible unless the run's counters are checked --
  so `ingest_runs.records_rejected` is read back from the database;
* **a wrong 4xx** loses the distinction the corpus encodes: a duplicate key (409)
  is a different operator action from an oversized body (413).

`duplicate_primary_key` is posted as one batch, deliberately. The committed corpus
gives *both* colliding lines `expect_code: 409`, and neither payload is defective
on its own -- the collision is a property of the load. Posting them separately
would be asserting something the contract does not say.
"""

from __future__ import annotations

from collections import defaultdict

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from recon.app import create_app
from tests.ingest.conftest import TRIGGER_HEADERS


@pytest.fixture
def api(owner_engine, trigger_secret) -> TestClient:
    """A client bound to the real app, with the live database behind it.

    It depends on `trigger_secret` because the endpoint fails closed (R19): with
    no secret configured every request here would be a 401, and this module is
    about what a *payload* earns, not about what an unauthenticated caller does --
    that is `test_trigger_auth.py`.
    """
    with TestClient(create_app()) as client:
        yield client


def _post(client: TestClient, source: str, entity_type: str, raws: list[str], **extra):
    body = {
        "source": source,
        "entity_type": entity_type,
        "generation": 960,
        "records": raws,
        **extra,
    }
    return client.post("/internal/ingest/records", json=body, headers=TRIGGER_HEADERS)


def test_every_committed_case_answers_with_its_committed_status(
    api: TestClient, malformed_cases: list[dict]
) -> None:
    singles = [c for c in malformed_cases if c["kind"] != "duplicate_primary_key"]
    duplicates = [c for c in malformed_cases if c["kind"] == "duplicate_primary_key"]
    assert singles and duplicates

    for index, case in enumerate(singles):
        response = _post(
            api,
            case["source"],
            case["entity_type"],
            [case["raw"]],
            run_id=f"http-single-{case['case_id']}-{index}",
        )
        assert response.status_code == case["expect_code"], (
            f"{case['case_id']} ({case['kind']}) answered {response.status_code}"
        )
        assert response.status_code < 500, f"{case['case_id']} produced a 5xx"
        problem = response.json()
        assert {"type", "title", "status", "detail"} <= set(problem)
        assert problem["status"] == case["expect_code"]
        assert problem["type"].endswith(case["kind"])
        assert problem["accepted"] == 0 and problem["rejected"] == 1

    response = _post(
        api,
        duplicates[0]["source"],
        duplicates[0]["entity_type"],
        [case["raw"] for case in duplicates],
        run_id="http-duplicates",
    )
    assert response.status_code == 409
    assert response.json()["rejected"] == len(duplicates)


def test_no_committed_case_can_produce_a_5xx(api: TestClient, malformed_cases: list[dict]) -> None:
    """The single blanket assertion R2 makes: never a 500."""
    for index, case in enumerate(malformed_cases):
        response = _post(
            api,
            case["source"],
            case["entity_type"],
            [case["raw"]],
            run_id=f"http-no5xx-{index}",
        )
        assert response.status_code < 500, (
            f"{case['case_id']} produced {response.status_code}: a malformed payload "
            "is the client's problem and must never be reported as the server's"
        )


def test_the_run_counts_every_rejection(
    api: TestClient, owner_engine, malformed_cases: list[dict]
) -> None:
    """A rejected payload is counted in `ingest_runs`, so it cannot be a silent skip."""
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for case in malformed_cases:
        grouped[(case["source"], case["entity_type"])].append(case)

    run_id = "http-counted"
    expected_rejects: dict[str, int] = defaultdict(int)
    for (source, entity_type), cases in sorted(grouped.items()):
        response = _post(
            api, source, entity_type, [c["raw"] for c in cases], run_id=f"{run_id}-{entity_type}"
        )
        assert 400 <= response.status_code < 500
        payload = response.json()
        assert payload["accepted"] == 0
        assert payload["rejected"] == len(cases)
        assert payload["persisted"] is True
        expected_rejects[f"{run_id}-{entity_type}"] += len(cases)

    with owner_engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT run_id, records_ok, records_rejected, status FROM ingest_runs "
                "WHERE run_id LIKE :prefix"
            ),
            {"prefix": f"{run_id}-%"},
        ).all()
        landed = connection.execute(
            text("SELECT count(*) FROM raw_records WHERE run_id LIKE :prefix"),
            {"prefix": f"{run_id}-%"},
        ).scalar()

    assert {row.run_id for row in rows} == set(expected_rejects)
    total_rejected = 0
    for row in rows:
        assert row.records_ok == 0
        assert row.status == "partial"
        assert row.records_rejected == expected_rejects[row.run_id]
        total_rejected += row.records_rejected
    assert total_rejected == len(malformed_cases)
    assert landed == 0, "no malformed payload may reach the landing table"


def test_a_mixed_batch_reports_every_per_record_status(
    api: TestClient, malformed_cases: list[dict]
) -> None:
    """One batch, several kinds: 422 overall, with each record's own 4xx inside."""
    contacts = [
        case
        for case in malformed_cases
        if case["source"] == "crm" and case["entity_type"] == "contact"
    ]
    assert len({case["expect_code"] for case in contacts}) > 1

    response = _post(api, "crm", "contact", [case["raw"] for case in contacts], run_id="http-mixed")
    assert response.status_code == 422
    problem = response.json()
    assert problem["type"].endswith("multiple_rejections")
    statuses = [entry["status"] for entry in problem["problems"]]
    assert statuses == [case["expect_code"] for case in contacts]
    for entry in problem["problems"]:
        assert {"type", "title", "status", "detail"} <= set(entry)


def test_a_well_formed_batch_with_an_unknown_enum_value_is_accepted(
    api: TestClient, owner_engine
) -> None:
    """The other half of SS7: an unrecognised enum value is NOT malformed."""
    raw = (
        '{"crm_id":"CRM-9600001","email":"enum.http@example.test","first_name":"Ada",'
        '"last_name":"Byron","lifecycle_stage":"galactic_overlord",'
        '"created_at":"2026-02-01T00:00:00Z","updated_at":"2026-02-02T00:00:00Z",'
        '"external_id":null,"dob":"2012-05-04","grade":"4","state":"TX",'
        '"marketing_consent":true}'
    )
    response = _post(api, "crm", "contact", [raw], run_id="http-enum")

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert response.json()["rejected"] == 0

    with owner_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT lifecycle_stage, lifecycle_norm, unchecked_fields FROM stg_crm_contact "
                "WHERE crm_id = 'CRM-9600001'"
            )
        ).one()
    assert row.lifecycle_stage == "galactic_overlord"
    assert row.lifecycle_norm is None
    assert row.unchecked_fields == {"crm.contact.lifecycle_stage": "unmapped_enum"}


def test_reusing_a_run_id_is_a_409_not_a_double_landing(api: TestClient, owner_engine) -> None:
    """Landing is append-only, so a repeated load id would silently duplicate rows.

    It would also mis-pair the returned landing ids with the staging rows built
    from them, which is a corrupted lineage rather than a loud failure -- so the
    second attempt is refused, with the client-supplied run id named in the detail.
    """
    raw = (
        '{"crm_id":"CRM-9600002","email":"repeat@example.test","first_name":"Ada",'
        '"last_name":"Byron","lifecycle_stage":"lead","created_at":"2026-02-01T00:00:00Z",'
        '"updated_at":"2026-02-02T00:00:00Z","external_id":null,"dob":"2012-05-04",'
        '"grade":"4","state":"TX","marketing_consent":true}'
    )
    first = _post(api, "crm", "contact", [raw], run_id="http-repeat")
    second = _post(api, "crm", "contact", [raw], run_id="http-repeat")

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["type"].endswith("duplicate_load")

    with owner_engine.connect() as connection:
        landed = connection.execute(
            text("SELECT count(*) FROM raw_records WHERE run_id = 'http-repeat'")
        ).scalar()
    assert landed == 1, "the refused load must not have added a second copy"


def test_an_unknown_source_is_a_400_not_a_500(api: TestClient) -> None:
    response = _post(api, "salesforce", "contact", ["{}"], run_id="http-unknown-source")
    assert response.status_code == 400
    assert response.json()["type"].endswith("unknown_source")


def test_a_structurally_invalid_request_body_is_a_4xx(api: TestClient) -> None:
    """Even the envelope is validated server-side; nothing here can 500."""
    response = api.post("/internal/ingest/records", json={"source": "crm"}, headers=TRIGGER_HEADERS)
    assert 400 <= response.status_code < 500
