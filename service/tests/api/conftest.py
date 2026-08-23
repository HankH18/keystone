"""Fixtures for the client API suites: entities (T-5) and the review surface (T-11).

The application is `recon.app.create_app()` and **nothing is mounted here**. It
used to be: this file added `entities_router` itself, because `recon/app.py`
belonged to another ticket -- which is exactly how that router shipped
unreachable while every test covering it passed. T-11 mounts both routers in the
factory, `tests/integration/test_route_table.py` guards the mount, and the extra
`include_router` call is gone rather than left as a harmless duplicate: a fixture
that mounts a router is a fixture that can hide an unmounted one.

The committed demo keys are spelled out here for the same reason
`tests/schema/test_api_clients_seed.py` spells them out: they are the credentials
migration 0003 seeded, and a test that derived them from the same helper the
service uses would pass even if both changed together.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.er.dataset import Dataset, ensure_dataset

#: `migrations/versions/0003_seed_api_clients.py`, and `.env.example`.
DEMO_CLIENT_API_KEY = "keystone-demo-client-3f7a19c4e2b84d05"
DEMO_ADMIN_API_KEY = "keystone-demo-admin-8c25e0b71a94f36d"

#: `api_clients.label` for the client key -- what `visible_scope()` returns, and
#: therefore the tenant a client key may read (`recon.resolve.TENANT_LABELS`).
CLIENT_TENANT = "demo-client"
OTHER_TENANT = "demo-tenant-b"

CLIENT_HEADERS = {"X-Api-Key": DEMO_CLIENT_API_KEY}
ADMIN_HEADERS = {"X-Api-Key": DEMO_ADMIN_API_KEY}


@pytest.fixture(scope="session")
def dataset() -> Dataset:
    """The materialized generation-3 dataset (shared with `tests/er`)."""
    return ensure_dataset()


@pytest.fixture(scope="session")
def reader(dataset: Dataset) -> Engine:
    from recon.db import get_engine

    return get_engine()


@pytest.fixture(scope="session")
def api(dataset: Dataset) -> Iterator[TestClient]:
    """The real application, exactly as `make serve` builds it."""
    from recon.app import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture(scope="session")
def store() -> Any:
    """The graded conflict + proposal store, in the same database as `dataset`.

    Shared with `tests/apply` through `tests.apply.store`, which memoizes it per
    process. One database for the whole run is not tidiness: `use_database()`
    re-points the PROCESS at a DSN, so two suites each creating their own scratch
    database would silently steal each other's engines.
    """
    from tests.apply.store import ensure_store

    return ensure_store()


@pytest.fixture(scope="session")
def review_api(store: Any, api: TestClient) -> TestClient:
    """The same application, guaranteed to have conflicts and proposals to serve."""
    return api


def _entity_in(reader: Engine, tenant: str) -> dict[str, Any]:
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
        row = conn.execute(sql, {"tenant": tenant}).fetchone()
    assert row is not None, f"no student-anchored entity is assigned to tenant {tenant!r}"
    return {"canonical_id": row.canonical_id, "current": dict(row.current)}


@pytest.fixture(scope="session")
def client_entity(reader: Engine) -> dict[str, Any]:
    """An entity the committed **client** key owns."""
    return _entity_in(reader, CLIENT_TENANT)


@pytest.fixture(scope="session")
def other_entity(reader: Engine) -> dict[str, Any]:
    """An entity assigned to the other tenant -- the wall's far side."""
    return _entity_in(reader, OTHER_TENANT)
