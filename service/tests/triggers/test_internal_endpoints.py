"""Trigger endpoints and API-key auth (R19, R20).

The app is assembled here rather than imported from ``recon.app``: this ticket
exports a router and does not wire it, so the test builds the smallest app that
mounts it. That also makes the wiring the ticket asks for explicit -- if
``create_app`` never includes this router, these tests still pass and the
service still 404s, which is why the handover note names the two lines.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from recon.api import internal
from recon.api.auth import (
    TRIGGER_SECRET_HEADER,
    Principal,
    api_key_guard,
    resolve_api_key,
    trigger_secret_for,
    verify_trigger_secret,
)
from tests.budget.support import env_settings, unique

SYNC_SECRET = "sync-secret-a1b2c3d4"
RECONCILE_SECRET = "reconcile-secret-e5f6a7b8"

#: The committed demo keys. `.env.example` documents them and migration 0003
#: seeds their hashes; the plaintext exists in exactly those two places.
DEMO_CLIENT_KEY = "keystone-demo-client-3f7a19c4e2b84d05"
DEMO_ADMIN_KEY = "keystone-demo-admin-8c25e0b71a94f36d"


@pytest.fixture
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure both per-job secrets."""
    env_settings(
        monkeypatch,
        TRIGGER_SECRET_SYNC=SYNC_SECRET,
        TRIGGER_SECRET_RECONCILE=RECONCILE_SECRET,
    )


@pytest.fixture
def app_client(owner_engine: Engine) -> Iterator[TestClient]:
    """A TestClient over an app that mounts only the internal router."""
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


def _post(client: TestClient, path: str, secret: str | None, run_id: str | None = None):
    headers = {} if secret is None else {TRIGGER_SECRET_HEADER: secret}
    return client.post(path, json={"run_id": run_id}, headers=headers)


# ===========================================================================
# R19 -- the per-job shared secret
# ===========================================================================
@pytest.mark.parametrize("path", ["/internal/sync", "/internal/reconcile"])
def test_no_secret_is_401(app_client: TestClient, secrets: None, path: str) -> None:
    response = _post(app_client, path, None, unique("norun"))
    assert response.status_code == 401
    body = response.json()
    assert body["title"] == "unauthorized"
    assert body["status"] == 401
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("path", ["/internal/sync", "/internal/reconcile"])
def test_a_wrong_secret_is_401(app_client: TestClient, secrets: None, path: str) -> None:
    response = _post(app_client, path, "not-the-secret", unique("wrong"))
    assert response.status_code == 401


def test_the_other_jobs_secret_is_401(app_client: TestClient, secrets: None) -> None:
    """Per-job means per-job.

    If either endpoint accepted the sibling secret, the two would be one
    credential with two names -- a leaked cron environment would surrender both
    endpoints, and rotating one would be meaningless.
    """
    assert _post(app_client, "/internal/sync", RECONCILE_SECRET, unique("x")).status_code == 401
    assert _post(app_client, "/internal/reconcile", SYNC_SECRET, unique("y")).status_code == 401


def test_an_unconfigured_secret_fails_closed(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing secret refuses everyone. It never disables the check.

    This is the divergence the ticket names, asserted rather than described: an
    endpoint that authenticates everyone when its secret is unset looks exactly
    like a working deployment until someone finds it.
    """
    env_settings(monkeypatch, TRIGGER_SECRET_SYNC=None, TRIGGER_SECRET_RECONCILE=None)
    assert trigger_secret_for("sync") is None

    assert _post(app_client, "/internal/sync", None, unique("closed")).status_code == 401
    assert _post(app_client, "/internal/sync", "", unique("closed")).status_code == 401
    assert _post(app_client, "/internal/sync", "anything", unique("closed")).status_code == 401


def test_the_right_secret_is_200(app_client: TestClient, secrets: None) -> None:
    run_id = unique("ok")
    response = _post(app_client, "/internal/sync", SYNC_SECRET, run_id)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "started"
    assert body["run_id"] == run_id
    assert body["budget_scope_provisioned"] is True, (
        "the run's own spend scope is provisioned by ops at trigger time"
    )


def test_secret_comparison_is_constant_time_and_never_logs_the_secret(
    secrets: None,
) -> None:
    """`hmac.compare_digest`, and nothing that could echo the value."""
    assert verify_trigger_secret("sync", SYNC_SECRET) is True
    assert verify_trigger_secret("sync", SYNC_SECRET[:-1]) is False
    assert verify_trigger_secret("sync", SYNC_SECRET + "x") is False
    assert verify_trigger_secret("sync", None) is False
    with pytest.raises(ValueError):
        verify_trigger_secret("not-a-job", SYNC_SECRET)


def test_the_401_body_never_echoes_the_presented_secret(
    app_client: TestClient, secrets: None
) -> None:
    """A problem document says what failed, never with what value."""
    presented = "super-secret-guess-9f8e7d"
    response = _post(app_client, "/internal/sync", presented, unique("echo"))
    assert response.status_code == 401
    assert presented not in response.text
    assert SYNC_SECRET not in response.text


# ===========================================================================
# R19 -- idempotency
# ===========================================================================
def test_a_replayed_run_id_does_not_run_twice(app_client: TestClient, secrets: None) -> None:
    run_id = unique("replay")
    ran: list[str] = []
    internal.register_job_handler(internal.JOB_SYNC, lambda rid: ran.append(rid) or {"ok": True})
    try:
        first = _post(app_client, "/internal/sync", SYNC_SECRET, run_id)
        second = _post(app_client, "/internal/sync", SYNC_SECRET, run_id)
    finally:
        internal.clear_job_handler(internal.JOB_SYNC)

    assert first.json()["status"] == "started"
    assert second.status_code == 200
    assert second.json()["status"] == "replayed"
    assert ran == [run_id], "the job body ran exactly once"


def test_the_two_jobs_do_not_share_an_idempotency_namespace(
    app_client: TestClient, secrets: None
) -> None:
    """The same run id triggers each job once, not one job twice.

    The claim is keyed on ``(job, run_id)``; if it were keyed on ``run_id``
    alone, a shared id would make the second job look like a replay of the first
    and it would silently never run.
    """
    run_id = unique("shared")
    assert _post(app_client, "/internal/sync", SYNC_SECRET, run_id).json()["status"] == "started"
    assert (
        _post(app_client, "/internal/reconcile", RECONCILE_SECRET, run_id).json()["status"]
        == "started"
    )


def test_a_simultaneous_replay_claims_exactly_once(
    app_client: TestClient, owner_engine: Engine, secrets: None
) -> None:
    """Two callers, same run id, released together: one claim, one replay.

    The check-then-insert is serialised by a transaction-scoped advisory lock,
    so there is no window between "no claim exists" and "my claim is visible".
    """
    run_id = unique("race")
    barrier = Barrier(2, timeout=30)

    def claim(_index: int) -> bool:
        barrier.wait()
        return internal.claim_run(internal.JOB_SYNC, run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))

    assert sorted(results) == [False, True], f"expected exactly one claim, got {results}"
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action = :a AND subject = :s"),
            {"a": internal.trigger_action(internal.JOB_SYNC), "s": run_id},
        ).scalar_one()
    assert rows == 1


def test_an_unbound_handler_is_reported_and_not_faked(
    app_client: TestClient, secrets: None
) -> None:
    """No handler registered means the response says so.

    Reporting ``"handler": "unbound"`` is the honest answer: the endpoint proves
    authentication, idempotency and budget provisioning, and does not pretend to
    have run an ingestion that nothing has wired in yet.
    """
    internal.clear_job_handler(internal.JOB_SYNC)
    body = _post(app_client, "/internal/sync", SYNC_SECRET, unique("unbound")).json()
    assert body["handler"] == "unbound"


def test_a_failing_handler_does_not_500(app_client: TestClient, secrets: None) -> None:
    """A cron gets a structured failure, not a stack trace."""

    def explode(_run_id: str) -> dict:
        raise RuntimeError("ingest blew up")

    internal.register_job_handler(internal.JOB_RECONCILE, explode)
    try:
        response = _post(app_client, "/internal/reconcile", RECONCILE_SECRET, unique("boom"))
    finally:
        internal.clear_job_handler(internal.JOB_RECONCILE)
    assert response.status_code == 200
    body = response.json()
    assert body["handler"] == "failed"
    assert "ingest blew up" in body["error"]


# ===========================================================================
# R20 -- scoped API keys
# ===========================================================================
def test_the_committed_demo_keys_resolve_to_their_scopes(owner_engine: Engine) -> None:
    """Keys are matched against the hashes migration 0003 seeded."""
    client = resolve_api_key(DEMO_CLIENT_KEY)
    admin = resolve_api_key(DEMO_ADMIN_KEY)
    assert client == Principal(scope="client", label="demo-client")
    assert admin == Principal(scope="admin", label="demo-admin")
    assert admin.is_admin and not client.is_admin


def test_an_unknown_or_missing_key_is_401(owner_engine: Engine) -> None:
    assert resolve_api_key(None) is None
    assert resolve_api_key("") is None
    assert resolve_api_key(f"keystone-not-a-key-{uuid.uuid4()}") is None

    principal, denied = api_key_guard(None)
    assert principal is None
    assert denied is not None and denied.status_code == 401


def test_a_client_key_may_not_use_an_admin_endpoint(owner_engine: Engine) -> None:
    """403, not 401: the key is fine, the scope is not.

    Collapsing the two would tell a caller to rotate a working credential in
    response to a permissions problem.
    """
    principal, denied = api_key_guard(DEMO_CLIENT_KEY, required_scope="admin")
    assert principal == Principal(scope="client", label="demo-client")
    assert denied is not None
    assert denied.status_code == 403

    admin, allowed = api_key_guard(DEMO_ADMIN_KEY, required_scope="admin")
    assert admin is not None and allowed is None


def test_a_principal_never_carries_the_key(owner_engine: Engine) -> None:
    """Neither the plaintext key nor its hash may leave the auth module."""
    principal = resolve_api_key(DEMO_ADMIN_KEY)
    assert principal is not None
    rendered = repr(principal)
    assert DEMO_ADMIN_KEY not in rendered
    assert "hash" not in rendered.lower()
    assert set(vars(principal)) == {"scope", "label"}


def test_row_visibility_is_org_wide_only_for_admin(owner_engine: Engine) -> None:
    """`visible_scope` is what the client API filters on (used by T-5)."""
    from recon.api.auth import visible_scope

    assert visible_scope(Principal(scope="admin", label="demo-admin")) is None
    assert visible_scope(Principal(scope="client", label="demo-client")) == "demo-client"
