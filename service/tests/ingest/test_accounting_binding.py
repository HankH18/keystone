"""The accounting invariant is BOUND: deleting a guard turns a test red (R2, R18).

`tests/ingest/test_accounting.py` asserts that the equation holds on honest
loads. That is necessary and it is not sufficient, and the gap was measured: two
independent sabotages -- deleting `LoadResult.check()` outright, and skipping the
landing write while still reporting the count -- left the whole in-scope suite
**green**. A guard nothing exercises is documentation.

So this file is the other half. Each test *causes* the failure the guard exists
to catch, on the real path, and asserts the guard caught it:

* the landing write silently writes fewer rows than it accepted -- the shape the
  original silent skip had, where `loaded` was `len(records)` and the write was
  gated;
* the landing write is skipped entirely;
* the staging materialization drops rows (covered by
  `tests/ingest/test_atomic_loads.py`, which sabotages `_materialize` the same
  way).

Delete `load.check()` from the endpoint and the first two go red. Delete
`_check_source_accounting` from `ingest_source` and the file-path one goes red.

**And a violation is a structured fault, not a crash.** The guard used to raise
an `IngestAccountingError` that nothing caught, so the check against a silent
skip produced the bare 500 R2 forbids -- an internal fault reported as a blank
page. A violation is now logged, written to `audit_log` (R18) through
`recon.logging.insert_audit_row`, returned as an RFC7807 document naming the
invariant, and -- because the whole load runs in one transaction -- rolled back
rather than half-committed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import recon.ingest as ingest_module
from recon.adapters import FaultInjectingAdapter, stub_records
from recon.app import create_app
from recon.db import ROLE_RECON_WRITER, role_connection
from recon.ingest import (
    ACCOUNTING_INVARIANT,
    AUDIT_ACTOR_INGEST,
    IngestAccountingError,
    Landing,
    ingest_source,
)
from tests.ingest.conftest import TRIGGER_HEADERS

GEN = 981
GEN_FILE = 982


def contact(index: int) -> str:
    return json.dumps(
        {
            "crm_id": f"CRM-BIND-{index:06d}",
            "email": f"bind{index}@example.invalid",
            "first_name": "Grace",
            "last_name": "Hopper",
            "lifecycle_stage": "lead",
            "created_at": "2026-03-01T00:00:00Z",
            "updated_at": "2026-03-02T00:00:00Z",
        },
        sort_keys=True,
    )


@pytest.fixture
def api(trigger_secret: str, owner_engine: Engine) -> Iterator[TestClient]:
    _clear(owner_engine)
    with TestClient(create_app()) as client:
        yield client
    _clear(owner_engine)


def _clear(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in ("stg_crm_contact", "raw_records", "source_generations", "ingest_runs"):
            conn.execute(
                text(f"DELETE FROM {table} WHERE generation = ANY(:g)"),
                {"g": [GEN, GEN_FILE]},
            )
        conn.execute(text("DELETE FROM audit_log WHERE action = 'ingest.accounting_violation'"))


def _post(client: TestClient, records: Sequence[str], *, run_id: str) -> tuple[int, dict]:
    response = client.post(
        "/internal/ingest/records",
        json={
            "source": "crm",
            "entity_type": "contact",
            "generation": GEN,
            "records": list(records),
            "run_id": run_id,
        },
        headers=TRIGGER_HEADERS,
    )
    return response.status_code, response.json()


def _audited(engine: Engine, run_id: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM audit_log WHERE action = 'ingest.accounting_violation' "
                    "AND subject = :s AND actor = :a"
                ),
                {"s": run_id, "a": AUDIT_ACTOR_INGEST},
            ).scalar_one()
        )


# ======================================================================================
# sabotage 1: the landing write under-writes
# ======================================================================================


def test_a_landing_write_that_drops_rows_is_refused_not_reported_as_a_200(
    api: TestClient, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Land four of five and the endpoint must refuse the whole load.

    This is the exact silent skip the invariant was added for: a write that
    quietly does less than it was asked, with `len(records)` reported as though
    it were evidence.
    """
    original = ingest_module._land_records

    def under_write(conn, records, **kwargs):
        result = original(conn, records[:-1], **kwargs)
        return result

    monkeypatch.setattr(ingest_module, "_land_records", under_write)

    status, body = _post(api, [contact(i) for i in range(5)], run_id="bind-underwrite")
    assert status >= 500, (
        f"a load that landed 4 of 5 accepted records answered {status}; a 200 over "
        "a short write is the silent skip R2 forbids"
    )
    assert body["type"].endswith("accounting_violation")
    assert body["invariant"] == ACCOUNTING_INVARIANT
    assert body["detail"].startswith("every payload validated")

    with owner_engine.connect() as conn:
        landed = conn.execute(
            text("SELECT count(*) FROM raw_records WHERE run_id = 'bind-underwrite'")
        ).scalar_one()
    assert landed == 0, (
        f"{landed} rows survived a refused load; the whole load runs in one "
        "transaction so a violation rolls it back rather than half-committing it"
    )
    assert _audited(owner_engine, "bind-underwrite") == 1, (
        "an internal fault must leave a durable audit row (R18), not only a log line"
    )


# ======================================================================================
# sabotage 2: the landing write is skipped entirely
# ======================================================================================


def test_skipping_the_landing_write_entirely_is_refused(
    api: TestClient, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other measured sabotage: report the count, perform no write.

    `Landing(landed=len(records))` is exactly what the pre-fix code returned on
    the dry-run path, so this is the bug being re-installed on the persist path.
    """

    def write_nothing(conn, records, **kwargs):
        return Landing(landed=0)

    monkeypatch.setattr(ingest_module, "_land_records", write_nothing)

    status, body = _post(api, [contact(i) for i in range(3)], run_id="bind-nowrite")
    assert status >= 500
    assert body["type"].endswith("accounting_violation")
    assert body["invariant"] == ACCOUNTING_INVARIANT
    assert "read=3" in body["detail"] and "landed=0" in body["detail"]

    with owner_engine.connect() as conn:
        ledger = conn.execute(
            text("SELECT count(*) FROM source_generations WHERE generation = :g"),
            {"g": GEN},
        ).scalar_one()
    assert ledger == 0, (
        "a load that did not balance may not reach the completeness ledger at "
        "all; SS5.3 lets every absence rule run against a generation marked complete"
    )
    assert _audited(owner_engine, "bind-nowrite") == 1


def test_a_healthy_load_writes_no_violation_row(api: TestClient, owner_engine: Engine) -> None:
    """The green no-op control: the guard fires only when something is wrong."""
    status, body = _post(api, [contact(i) for i in range(3)], run_id="bind-healthy")
    assert status == 200
    assert body["accepted"] == 3
    assert _audited(owner_engine, "bind-healthy") == 0


# ======================================================================================
# sabotage 3: the file path
# ======================================================================================


def test_the_file_path_refuses_a_load_that_under_reports(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ingest_source` raises rather than stamping a ledger row it cannot justify.

    Delete `_check_source_accounting` from `ingest_source` and this goes red --
    which is what "bound" means. The failure is loud on this path by design: the
    file path is a batch job whose caller is the scheduler, and a sync that dies
    costs one run, while a sync that under-reports fabricates conflicts.
    """
    records = stub_records(6, source_id="crm", entity_type="contact", generation=GEN_FILE)
    adapter = FaultInjectingAdapter(
        source_id="crm", mode="ok", records=records, available_generations=(GEN_FILE,)
    )
    adapter.entity_types = ("contact",)

    def write_nothing(conn, records, **kwargs):
        return Landing(landed=0)

    monkeypatch.setattr(ingest_module, "_land_records", write_nothing)

    with (
        pytest.raises(IngestAccountingError) as excinfo,
        role_connection(ROLE_RECON_WRITER) as conn,
    ):
        ingest_source(
            adapter,
            GEN_FILE,
            run_id="bind-file",
            conn=conn,
            stall_timeout=2.0,
            deadline_seconds=10.0,
        )
    assert ACCOUNTING_INVARIANT in str(excinfo.value)
    assert "unaccounted=6" in str(excinfo.value)

    with owner_engine.connect() as conn:
        ledger = conn.execute(
            text("SELECT count(*) FROM source_generations WHERE generation = :g"),
            {"g": GEN_FILE},
        ).scalar_one()
    assert ledger == 0


def test_a_healthy_file_load_reaches_the_ledger(owner_engine: Engine) -> None:
    """The green no-op control for the file path."""
    records = stub_records(6, source_id="crm", entity_type="contact", generation=GEN_FILE)
    adapter = FaultInjectingAdapter(
        source_id="crm", mode="ok", records=records, available_generations=(GEN_FILE,)
    )
    adapter.entity_types = ("contact",)
    with role_connection(ROLE_RECON_WRITER) as conn:
        result = ingest_source(
            adapter,
            GEN_FILE,
            run_id="bind-file-ok",
            conn=conn,
            stall_timeout=2.0,
            deadline_seconds=10.0,
        )
    assert result.records_ok == 6
    with owner_engine.connect() as conn:
        loaded = conn.execute(
            text(
                "SELECT loaded_count FROM source_generations WHERE generation = :g "
                "AND source_id = 'crm' AND entity_type = 'contact'"
            ),
            {"g": GEN_FILE},
        ).scalar_one()
    assert loaded == 6
    _clear(owner_engine)


# ======================================================================================
# the fault is structured, never a bare 5xx
# ======================================================================================


def test_the_violation_response_is_a_problem_document_naming_the_invariant(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator gets the equation and the numbers, not `Internal Server Error`."""

    def write_nothing(conn, records, **kwargs):
        return Landing(landed=0)

    monkeypatch.setattr(ingest_module, "_land_records", write_nothing)

    response = api.post(
        "/internal/ingest/records",
        json={
            "source": "crm",
            "entity_type": "contact",
            "generation": GEN,
            "records": [contact(1)],
            "run_id": "bind-shape",
        },
        headers=TRIGGER_HEADERS,
    )
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    for key in ("type", "title", "status", "detail", "invariant", "run_id", "accepted"):
        assert key in body, f"the problem document is missing {key!r}: {sorted(body)}"
    assert body["status"] == response.status_code


def test_a_payload_rejection_still_wins_over_an_internal_fault(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 first: a malformed payload's 4xx is a fact about the payload.

    Our own outage may not overwrite it -- the same rule the storage-failure
    branch already follows. An operator told "500" for a payload they can fix is
    an operator who cannot fix it.
    """

    def write_nothing(conn, records, **kwargs):
        return Landing(landed=0)

    monkeypatch.setattr(ingest_module, "_land_records", write_nothing)

    status, body = _post(api, ['{"not":"a contact"}'], run_id="bind-rejection-wins")
    assert 400 <= status < 500, f"a malformed payload answered {status}"
    assert body["rejected"] == 1
