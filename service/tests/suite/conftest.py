"""Fixtures for the harness's own tests.

``KEYSTONE_REQUIRE_DB`` follows the convention ``tests/schema/conftest.py`` set:
a missing ``DATABASE_URL`` skips by default and **fails** when the variable is
set, so a CI run told the database is mandatory cannot report success without
one.

The endpoint tests use a scratch database of their own
(:mod:`tests.er.scratchdb`) rather than the configured one. They seed nothing and
read only ``api_clients``, but a suite that authenticates against the shared
database while another agent is dropping and recreating it is a suite that looks
flaky -- and a suite that looks flaky is one where a real failure gets dismissed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

REQUIRE_DB_ENV = "KEYSTONE_REQUIRE_DB"

SKIP_REASON = (
    "DATABASE_URL is not set: these tests need the live Postgres from "
    "infra/docker-compose.yml (host port 55432)."
)

REQUIRE_DB_REASON = (
    f"{REQUIRE_DB_ENV} is set, so these tests must actually run -- but "
    "DATABASE_URL is not configured, so every one of them would have skipped "
    "and the run would have reported a green that proves nothing."
)


def database_is_required() -> bool:
    raw = os.environ.get(REQUIRE_DB_ENV, "")
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


@pytest.fixture(scope="session")
def service_root_path() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def scratch_database() -> Iterator[str]:
    """A migrated scratch database, with the process pointed at it."""
    if not os.environ.get("DATABASE_URL"):
        if database_is_required():
            pytest.fail(REQUIRE_DB_REASON, pytrace=False)
        pytest.skip(SKIP_REASON)

    from tests.er.scratchdb import create_scratch_database, drop_database, use_database

    previous = os.environ.get("DATABASE_URL")
    dsn = create_scratch_database("t14suite")
    use_database(dsn)
    try:
        yield dsn
    finally:
        if previous:
            use_database(previous)
        drop_database(dsn)
