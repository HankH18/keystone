"""`POST /internal/reconcile` runs the reconciler (R13's scheduled trigger, R19).

The defect: the route was mounted, the secret was checked, `claim_run` consumed
the run id and `provision_run_scope` created the run's ledger scope -- and
`create_app()` registered a handler for `sync` and no other, so `_handler_for`
returned `None`, the endpoint logged `internal.handler_unbound` and answered
HTTP 200 `{"status": "started", "handler": "unbound"}`. `infra/render.yaml`
schedules that endpoint hourly, and "200 started" is what a cron health check
reads as success: a scheduled job reporting green for work no code performed.

Everything here goes through the real service. The application is
`recon.app.create_app()`, the conflicts were detected by a real
`POST /internal/sync` (`tests/integration/conftest.py::synced`), the trigger is
fired with the real per-job secret over HTTP, and the writes are made by the real
`recon_writer` login against a migrated scratch database. No handler is
registered by a fixture -- registering one here would recreate exactly the blind
spot that let this ship.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.integration.conftest import table_count

#: A test value, like `SYNC_SECRET`. The real one is a deployment secret.
RECONCILE_SECRET = "t14-integration-reconcile-secret"

RUN_ID = "t14-integration-reconcile"


@pytest.fixture(scope="module")
def reconcile_secret(integration_database: str) -> Iterator[None]:
    """Configure `TRIGGER_SECRET_RECONCILE` and drop the cached settings.

    `recon.api.auth.TRIGGER_SECRET_FIELDS` gives the reconcile job **no**
    fallback to the deprecated shared secret, deliberately, so this endpoint is
    unreachable until its own variable is set.
    """
    from recon.config import get_settings

    previous = os.environ.get("TRIGGER_SECRET_RECONCILE")
    os.environ["TRIGGER_SECRET_RECONCILE"] = RECONCILE_SECRET
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TRIGGER_SECRET_RECONCILE", None)
        else:
            os.environ["TRIGGER_SECRET_RECONCILE"] = previous
        get_settings.cache_clear()


@pytest.fixture(scope="module")
def reconciled(
    service: TestClient, synced: dict[str, Any], reconcile_secret: None
) -> dict[str, Any]:
    """Fire the reconcile trigger once, over HTTP, and return its body."""
    response = service.post(
        "/internal/reconcile",
        json={"run_id": RUN_ID},
        headers={"X-Trigger-Secret": RECONCILE_SECRET},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_the_trigger_is_bound_and_reports_a_run_that_happened(
    reconciled: dict[str, Any],
) -> None:
    """`handler: "ran"`, not `handler: "unbound"` -- the whole of W1."""
    assert reconciled["job"] == "reconcile"
    assert reconciled["run_id"] == RUN_ID
    assert reconciled["budget_scope_provisioned"] is True
    assert reconciled["handler"] == "ran", (
        f"the reconcile trigger reported {reconciled.get('handler')!r}. "
        "`create_app()` must bind `reconcile_job` to JOB_RECONCILE, or this "
        "endpoint authenticates, burns the run id and does nothing while "
        "answering 200 to an hourly cron"
    )
    assert reconciled["status"] == "started", reconciled

    result = reconciled["result"]
    assert result["run_id"] == RUN_ID, (
        "the report must carry the trigger's run id, not a derived one, so the "
        "proposals tie back to the audit_log trigger claim"
    )
    assert result["conflicts_seen"] > 0, result
    assert result["proposed"] > 0, result


def test_the_proposals_are_actually_in_the_table(
    reader: Engine, reconciled: dict[str, Any]
) -> None:
    """The response is a claim; the rows are the report.

    Before the binding this count was zero after any number of triggers.
    """
    result = reconciled["result"]
    with reader.connect() as conn:
        created = int(
            conn.execute(
                text("SELECT count(*) FROM proposals WHERE created_run = :run"), {"run": RUN_ID}
            ).scalar_one()
        )
    assert created > 0, "no proposal reached the table after a bound reconcile trigger"
    assert created == result["proposed"], (created, result["proposed"])
    assert table_count(reader, "proposals") >= created


def test_every_proposal_is_born_held(reader: Engine, reconciled: dict[str, Any]) -> None:
    """Holds before writes, checked on the rows the wired path produced.

    The grants and `KS002` enforce it; this states that the wired pipeline has
    not found a way around them.
    """
    with reader.connect() as conn:
        statuses = {
            str(row[0])
            for row in conn.execute(
                text("SELECT DISTINCT status::text FROM proposals WHERE created_run = :run"),
                {"run": RUN_ID},
            )
        }
    assert statuses, "no proposals to judge"
    assert statuses <= {"pending", "sensitive_hold"}, statuses


def test_the_run_leaves_the_audit_trail_the_trigger_claim_points_at(
    reader: Engine, reconciled: dict[str, Any]
) -> None:
    """R18: the trigger claim, the run row and the per-proposal rows all agree."""
    with reader.connect() as conn:
        claims = int(
            conn.execute(
                text(
                    "SELECT count(*) FROM audit_log "
                    " WHERE action = 'trigger.reconcile' AND subject = :run"
                ),
                {"run": RUN_ID},
            ).scalar_one()
        )
        runs = int(
            conn.execute(
                text(
                    "SELECT count(*) FROM audit_log "
                    " WHERE action = 'reconcile.run' AND subject = :run"
                ),
                {"run": RUN_ID},
            ).scalar_one()
        )
        created = int(
            conn.execute(
                text("SELECT count(*) FROM audit_log WHERE action = 'proposal.created'")
            ).scalar_one()
        )
    assert claims == 1, "the trigger claim is the idempotency marker; there must be exactly one"
    assert runs == 1, "the reconciler must write one `reconcile.run` audit row per run"
    assert created >= reconciled["result"]["proposed"]


def test_the_default_provider_attaches_no_rationale(
    reader: Engine, reconciled: dict[str, Any]
) -> None:
    """`LLM_PROVIDER=mock` is the default: the wired run makes no model call.

    This is the determinism half of the rationale wiring, asserted on the wired
    path rather than argued from the code: with no provider configured the hook
    is `no_rationale` itself, so every proposal lands with `rationale NULL` and
    the budget ledger is untouched.
    """
    assert reconciled["result"]["rationale_attached"] == 0
    with reader.connect() as conn:
        with_text = int(
            conn.execute(
                text(
                    "SELECT count(*) FROM proposals "
                    " WHERE created_run = :run AND rationale IS NOT NULL"
                ),
                {"run": RUN_ID},
            ).scalar_one()
        )
    assert with_text == 0


def test_the_escalation_reason_reaches_the_conflict_row(
    reader: Engine, reconciled: dict[str, Any]
) -> None:
    """Migration 0015, proven through the deployed pipeline rather than a fixture.

    `recon.reconciler._escalate` asks `has_column_privilege` once per run and
    writes the reason to `conflicts.escalation_reason` only when the grant is
    there. Under migration 0004's two-column grant every escalated row carried
    NULL, and the reviewer surface could only render `escalated:oscillation`
    through its `oscillating` fallback.
    """
    result = reconciled["result"]
    assert result["escalation_reason_persisted"] is True, (
        "recon_writer holds no UPDATE on conflicts.escalation_reason on this "
        "database; migration 0015 grants it"
    )
    assert result["escalated_oscillation"] > 0, result

    with reader.connect() as conn:
        escalated, with_reason = conn.execute(
            text(
                "SELECT count(*), count(escalation_reason) FROM conflicts "
                " WHERE status = 'escalated'"
            )
        ).one()
    assert escalated > 0
    assert with_reason == escalated, (
        f"{with_reason} of {escalated} escalated conflicts carry a reason on the row"
    )


def test_the_reviewer_surface_serves_the_reason_from_the_row(
    service: TestClient, reconciled: dict[str, Any]
) -> None:
    """The consequence a reviewer sees: `escalated:oscillation`, not bare `escalated`.

    Through the real HTTP endpoint with the committed admin key, because the
    composite status is rendered by `recon.api.review`'s SQL.

    **What this does not prove.** An oscillation escalation sets both
    `escalation_reason` and `oscillating`, so the reason branch and the
    `oscillating` fallback of that CASE produce the same string and nothing
    observable from outside says which one fired. The substantive claim is the
    one the previous test asserts -- the column is populated -- and this asserts
    that populating it did not change what a reviewer sees.
    """
    from tests.integration.conftest import ADMIN_HEADERS

    response = service.get(
        "/api/conflicts",
        params={"status": "escalated:oscillation", "page_size": 5},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == reconciled["result"]["escalated_oscillation"], body["total"]
    assert {row["status"] for row in body["items"]} == {"escalated:oscillation"}


def test_a_replayed_run_id_does_not_reconcile_twice(
    service: TestClient, reader: Engine, reconciled: dict[str, Any]
) -> None:
    """At-most-once per run id, now that there is something to run twice.

    While the endpoint was unbound this property was vacuous: a replay of nothing
    is nothing. It is only a real guarantee once the handler writes rows.
    """
    with reader.connect() as conn:
        before = int(conn.execute(text("SELECT count(*) FROM proposals")).scalar_one())

    response = service.post(
        "/internal/reconcile",
        json={"run_id": RUN_ID},
        headers={"X-Trigger-Secret": RECONCILE_SECRET},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "replayed", body
    assert "handler" not in body, body

    with reader.connect() as conn:
        after = int(conn.execute(text("SELECT count(*) FROM proposals")).scalar_one())
    assert after == before


def test_the_reconcile_endpoint_refuses_the_other_jobs_secret(
    service: TestClient, reconcile_secret: None
) -> None:
    """Per-job secrets, still per-job now that the endpoint does something."""
    from tests.integration.conftest import SYNC_SECRET

    response = service.post(
        "/internal/reconcile",
        json={"run_id": "t14-integration-reconcile-wrong-secret"},
        headers={"X-Trigger-Secret": SYNC_SECRET},
    )
    assert response.status_code == 401, response.text
