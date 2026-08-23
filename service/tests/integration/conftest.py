"""One real service, one real database, one real sync -- shared by this package.

Every other suite in the repository tests a part. This one tests the assembly,
so it deliberately owns nothing of its own: the app is `recon.app.create_app()`,
the database is a scratch database migrated by `alembic upgrade head`, and the
data gets there the only way the grader's data gets there -- by firing
`POST /internal/sync` with the trigger secret.

That is the whole point. Both defects this package exists to catch were invisible
to suites that assembled their own app: `recon/api/entities.py` was mounted by a
`conftest.py` and by nothing else, and `recon.resolve.materialize` was called by
its own tests and by nothing else. A fixture that mounts a router, or that
materializes by hand, cannot see either.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text

from tests.er.dataset import FIXTURES, _require_server
from tests.er.scratchdb import create_scratch_database, drop_database, use_database

#: The per-job secret the sync trigger is fired with (R19). A test value: the
#: real one comes from the deployment environment and is never committed.
SYNC_SECRET = "t14-integration-sync-secret"

#: `migrations/versions/0003_seed_api_clients.py`, and `.env.example`.
DEMO_CLIENT_API_KEY = "keystone-demo-client-3f7a19c4e2b84d05"
DEMO_ADMIN_API_KEY = "keystone-demo-admin-8c25e0b71a94f36d"

CLIENT_HEADERS = {"X-Api-Key": DEMO_CLIENT_API_KEY}
ADMIN_HEADERS = {"X-Api-Key": DEMO_ADMIN_API_KEY}

#: `api_clients.label` for the committed client key -- the tenant it may read.
CLIENT_TENANT = "demo-client"

#: The four tables `recon.resolve.materialize` writes, and the reason this
#: package exists: on the documented grader path all four were empty.
CANONICAL_TABLES = ("entities", "entity_links", "entity_link_candidates", "field_lineage")


@pytest.fixture(scope="session")
def integration_database() -> Iterator[str]:
    """A migrated scratch database, and the process pointed at it.

    `DATABASE_URL` supplies the server coordinates only (`tests.er.scratchdb`).
    The previous value is restored on the way out so a suite that ran before this
    one -- `tests/er` builds its own database the same way -- is not left pointing
    at a database this fixture has dropped.
    """
    _require_server()
    if not (FIXTURES / "manifest.json").is_file():
        pytest.fail(
            f"no committed fixture tree at {FIXTURES}: run `make seed` (or "
            "`uv run python -m recon.seed --profile full`) first. This package "
            "drives the real sync, which reads that tree through the adapters."
        )

    previous = os.environ.get("DATABASE_URL")
    dsn = create_scratch_database("t14int")
    use_database(dsn)
    try:
        yield dsn
    finally:
        if previous:
            use_database(previous)
        drop_database(dsn)


@pytest.fixture(scope="session")
def trigger_secret(integration_database: str) -> Iterator[None]:
    """Configure `TRIGGER_SECRET_SYNC` for the session and drop the settings cache."""
    from recon.config import get_settings

    previous = os.environ.get("TRIGGER_SECRET_SYNC")
    os.environ["TRIGGER_SECRET_SYNC"] = SYNC_SECRET
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TRIGGER_SECRET_SYNC", None)
        else:
            os.environ["TRIGGER_SECRET_SYNC"] = previous
        get_settings.cache_clear()


@pytest.fixture(scope="session")
def service(trigger_secret: None) -> Iterator[TestClient]:
    """The real application from the real factory. No router mounted by hand."""
    from recon.app import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture(scope="session")
def synced(service: TestClient, integration_database: str) -> dict[str, Any]:
    """Fire `POST /internal/sync` once and return its body.

    Session-scoped because it is the expensive thing and because it is the thing
    under test: every assertion in this package is about the state one authentic
    sync leaves behind.
    """
    response = service.post(
        "/internal/sync",
        json={"run_id": "t14-integration-sync"},
        headers={"X-Trigger-Secret": SYNC_SECRET},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


@pytest.fixture(scope="session")
def reader(integration_database: str) -> Iterator[Engine]:
    """Owner-principal reads of what the pipeline wrote."""
    engine = create_engine(
        integration_database.replace("postgresql://", "postgresql+psycopg://"), future=True
    )
    try:
        yield engine
    finally:
        engine.dispose()


def table_count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
