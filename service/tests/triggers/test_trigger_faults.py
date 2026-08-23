"""Every line of ``_trigger`` between two guards is itself guarded (R19, R2).

``POST /internal/{sync,reconcile}`` is an authenticated endpoint that a cron
calls, and R2's rule -- a structured status, never a bare 500 -- is what makes a
failed job *reportable*. Two guards were already in place: ``claim_run`` is
wrapped in a storage handler and ``_run_job`` catches everything the job body
throws. The line **between them** was not.

``provision_run_scope(run_id)`` opens its own connection (the ops principal, not
the caller's) and reads ``PER_RUN_CAP_USD`` out of the environment. A storage
fault or a malformed cap there raised straight out of the handler: FastAPI's
default 500, no body, no problem document, on an authenticated request whose run
had already been claimed and committed. That is the last naked 500 on this path.

There is a second one further down that is easy to miss, because the guard that
should catch it has already returned by the time it happens: ``JSONResponse``
encodes its body **in its constructor**, so a handler returning a mapping holding
a ``datetime``, a ``Decimal`` or a dataclass raised ``TypeError`` after
``_run_job``'s ``except`` had passed. The summary is now round-tripped through
``json.dumps`` inside the guard.

Each test here *causes* the fault on the real path and asserts the endpoint
answered a document rather than a stack trace; each has a green no-op control
beside it, because a test that only ever sees a 503 proves nothing about the
happy path.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from recon.api import internal
from recon.api.auth import TRIGGER_SECRET_HEADER
from tests.budget.support import env_settings, unique

SYNC_SECRET = "sync-secret-fault-9f1c"
RECONCILE_SECRET = "reconcile-secret-fault-2b7d"


@pytest.fixture
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    env_settings(
        monkeypatch,
        TRIGGER_SECRET_SYNC=SYNC_SECRET,
        TRIGGER_SECRET_RECONCILE=RECONCILE_SECRET,
    )


@pytest.fixture
def app_client(owner_engine: Engine) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(internal.router)
    with TestClient(app) as client:
        yield client
    _cleanup(owner_engine)


def _cleanup(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM audit_log WHERE action LIKE 'trigger.%' AND subject LIKE :p"),
            {"p": "%t8-test%"},
        )
        conn.execute(
            text("DELETE FROM budget_reservations WHERE scope LIKE :p"), {"p": "run:%t8-test%"}
        )
        conn.execute(text("DELETE FROM budget_ledger WHERE scope LIKE :p"), {"p": "run:%t8-test%"})


def _post(client: TestClient, path: str, secret: str, run_id: str):
    return client.post(path, json={"run_id": run_id}, headers={TRIGGER_SECRET_HEADER: secret})


def _claimed(engine: Engine, job: str, run_id: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM audit_log WHERE action = :a AND subject = :s"),
                {"a": internal.trigger_action(job), "s": run_id},
            ).scalar_one()
        )


def _scope_rows(engine: Engine, run_id: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM budget_ledger WHERE scope = :s"),
                {"s": f"run:{run_id}"},
            ).scalar_one()
        )


# ======================================================================================
# the storage fault between the two guards
# ======================================================================================


def test_a_storage_fault_provisioning_the_run_scope_is_a_503_not_a_500(
    app_client: TestClient, secrets: None, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured gap: a fault on this line was FastAPI's bare 500."""

    def refuse(run_id: str, cap_microusd: int | None = None) -> bool:
        raise psycopg.OperationalError("connection to server was lost")

    monkeypatch.setattr(internal, "provision_run_scope", refuse)

    run_id = unique("scope-fault")
    response = _post(app_client, "/internal/sync", SYNC_SECRET, run_id)

    assert response.status_code == 503, (
        f"a storage fault provisioning the run scope answered {response.status_code}; "
        "an authenticated endpoint must never answer a bare 500"
    )
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["title"] == "storage unavailable"
    assert body["status"] == 503
    assert "spend scope" in body["detail"]
    assert "OperationalError" in body["detail"]
    assert SYNC_SECRET not in response.text


def test_a_malformed_spend_cap_is_a_503_not_a_500(
    app_client: TestClient, secrets: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same line's other failure mode: it also reads the environment.

    ``provision_run_scope`` resolves ``PER_RUN_CAP_USD`` on the way through, so a
    cap nobody can parse is a configuration fault arriving at exactly the same
    unguarded line -- which is why the guard catches ``Exception`` rather than a
    tuple of storage types.
    """

    def refuse(run_id: str, cap_microusd: int | None = None) -> bool:
        raise ValueError("PER_RUN_CAP_USD is not a number: 'one dollar'")

    monkeypatch.setattr(internal, "provision_run_scope", refuse)

    response = _post(app_client, "/internal/reconcile", RECONCILE_SECRET, unique("cap-fault"))
    assert response.status_code == 503
    assert response.json()["title"] == "storage unavailable"


def test_the_job_body_does_not_run_when_the_spend_scope_is_missing(
    app_client: TestClient, secrets: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed on spend (R17): no scope, no run.

    The per-run cap lives in the scope that line creates. Running the handler
    anyway would be an uncapped run, which is the one outcome R17 forbids
    outright -- so the trigger refuses and the operator re-fires under a fresh
    run id.
    """
    ran: list[str] = []

    def refuse(run_id: str, cap_microusd: int | None = None) -> bool:
        raise psycopg.OperationalError("no connection")

    monkeypatch.setattr(internal, "provision_run_scope", refuse)
    internal.register_job_handler(internal.JOB_SYNC, lambda run_id: ran.append(run_id) or {})
    try:
        response = _post(app_client, "/internal/sync", SYNC_SECRET, unique("noscope"))
    finally:
        internal.clear_job_handler(internal.JOB_SYNC)

    assert response.status_code == 503
    assert ran == [], "the job body ran without a provisioned spend scope"


def test_the_claim_survives_a_provisioning_fault_and_is_reported(
    app_client: TestClient, secrets: None, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At-most-once is not weakened by the new guard, and says so.

    The claim commits before the body runs (this endpoint's documented
    semantics), so a run refused here stays claimed. The detail names the
    remedy -- a fresh run id -- rather than leaving a cron to retry into a
    silent replay.
    """

    def refuse(run_id: str, cap_microusd: int | None = None) -> bool:
        raise psycopg.OperationalError("no connection")

    monkeypatch.setattr(internal, "provision_run_scope", refuse)
    run_id = unique("claim-kept")
    first = _post(app_client, "/internal/sync", SYNC_SECRET, run_id)
    assert first.status_code == 503
    assert "fresh run id" in first.json()["detail"]
    assert _claimed(owner_engine, internal.JOB_SYNC, run_id) == 1

    monkeypatch.undo()
    replay = _post(app_client, "/internal/sync", SYNC_SECRET, run_id)
    assert replay.status_code == 200
    assert replay.json()["status"] == "replayed"


# ======================================================================================
# the response body the guard cannot see
# ======================================================================================


def test_a_handler_summary_that_cannot_be_rendered_is_not_a_500(
    app_client: TestClient, secrets: None
) -> None:
    """``JSONResponse`` encodes in its constructor -- after ``_run_job``'s guard.

    A handler returning an ordinary mapping holding a ``datetime`` therefore
    raised ``TypeError`` past every guard in the function. The summary is now
    round-tripped inside the guard, so the job is reported as failed instead of
    the endpoint answering a blank 500.
    """

    def unrenderable(_run_id: str) -> dict:
        return {"finished_at": datetime.now(tz=UTC), "records": 12}

    internal.register_job_handler(internal.JOB_RECONCILE, unrenderable)
    try:
        response = _post(
            app_client, "/internal/reconcile", RECONCILE_SECRET, unique("unrenderable")
        )
    finally:
        internal.clear_job_handler(internal.JOB_RECONCILE)

    assert response.status_code == 200, (
        f"an unrenderable handler summary answered {response.status_code}; "
        "the guard is upstream of the encoder, so it must do the encoding"
    )
    body = response.json()
    assert body["handler"] == "failed"
    assert "TypeError" in body["error"]


# ======================================================================================
# green no-op controls
# ======================================================================================


def test_the_control_a_healthy_trigger_provisions_and_runs(
    app_client: TestClient, secrets: None, owner_engine: Engine
) -> None:
    """Nothing sabotaged: 200, a provisioned scope, and the handler's summary."""
    ran: list[str] = []

    def handler(run_id: str) -> dict:
        ran.append(run_id)
        return {"records_ok": 3}

    run_id = unique("healthy")
    internal.register_job_handler(internal.JOB_SYNC, handler)
    try:
        response = _post(app_client, "/internal/sync", SYNC_SECRET, run_id)
    finally:
        internal.clear_job_handler(internal.JOB_SYNC)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "started"
    assert body["budget_scope_provisioned"] is True
    assert body["handler"] == "ran"
    assert body["result"] == {"records_ok": 3}
    assert ran == [run_id]
    assert _scope_rows(owner_engine, run_id) == 1


def test_the_control_a_renderable_summary_survives_the_round_trip(
    app_client: TestClient, secrets: None
) -> None:
    """The encoder guard does not eat a legitimate summary."""

    def handler(_run_id: str) -> dict:
        return {"generations": [1, 2, 3], "degraded": False, "elapsed_ms": 12.5}

    internal.register_job_handler(internal.JOB_RECONCILE, handler)
    try:
        response = _post(app_client, "/internal/reconcile", RECONCILE_SECRET, unique("clean"))
    finally:
        internal.clear_job_handler(internal.JOB_RECONCILE)

    assert response.status_code == 200
    assert response.json()["result"] == {
        "generations": [1, 2, 3],
        "degraded": False,
        "elapsed_ms": 12.5,
    }
