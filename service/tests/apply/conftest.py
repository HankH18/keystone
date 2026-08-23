"""Fixtures for the apply suite (T-11).

Everything reads the ONE graded store `tests/apply/store.py` builds -- real
conflicts from the committed invariant engine, real proposals from the committed
reconciler. No proposal here was written by hand.

Two kinds of connection, and the difference is load-bearing:

`apply_conn`
    an `apply_writer` connection whose transaction is **rolled back**. Good for
    everything that reads or that must be refused; useless for proving a
    successful apply, because the citation triggers are DEFERRABLE INITIALLY
    DEFERRED and a rolled-back transaction never reaches COMMIT, so they never
    fire. A suite built only on this would be green against a boundary it never
    touched.
`committed_proposal`
    a proposal reserved for a test that COMMITS. Each such test consumes its own
    proposal, because migration 0006 makes a citation single-use: one approval,
    one canonical write, one reversal, forever.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Connection, text

from tests.apply.store import GradedStore, ensure_store

REQUIRE_DB_ENV = "KEYSTONE_REQUIRE_DB"

SKIP_REASON = (
    "DATABASE_URL is not set. The apply suite needs a Postgres server "
    "(infra/docker-compose.yml, host port 55432); it creates and drops its own "
    "database on that server and never touches the one DATABASE_URL names."
)

REQUIRE_DB_REASON = (
    f"{REQUIRE_DB_ENV} is set, so the apply suite must actually run -- but DATABASE_URL "
    "is not configured, so every database test would have skipped and the run would "
    "have reported a green that proves nothing."
)


def _require_server() -> None:
    if os.environ.get("DATABASE_URL"):
        return
    if os.environ.get(REQUIRE_DB_ENV, "").strip().lower() not in {"", "0", "false", "no", "off"}:
        pytest.fail(REQUIRE_DB_REASON)
    pytest.skip(SKIP_REASON)


@pytest.fixture(scope="session")
def store() -> GradedStore:
    """The graded conflict + proposal store."""
    _require_server()
    return ensure_store()


@pytest.fixture(scope="session")
def reader(store: GradedStore) -> Any:
    from recon.db import get_engine

    return get_engine()


@pytest.fixture
def apply_conn(store: GradedStore) -> Iterator[Connection]:
    """An `apply_writer` connection that is ROLLED BACK. Never reaches COMMIT."""
    from recon.db import ROLE_APPLY_WRITER, role_connection

    with role_connection(ROLE_APPLY_WRITER, commit=False) as conn:
        yield conn


@pytest.fixture
def review_conn(store: GradedStore) -> Iterator[Connection]:
    """A `review_writer` connection that is ROLLED BACK."""
    from recon.db import ROLE_REVIEW_WRITER, role_connection

    with role_connection(ROLE_REVIEW_WRITER, commit=False) as conn:
        yield conn


_ELIGIBLE_PROPOSALS = text(
    """
    SELECT p.id
      FROM proposals p
      JOIN conflicts c ON c.id = p.conflict_id
      JOIN entities e ON e.canonical_id = p.target_canonical_id
     WHERE p.status = 'pending'
       AND p.sensitive = false
       AND p.confidence >= 0.95
       AND c.type = ANY(CAST(:types AS text[]))
       AND p.action -> 'set' <> '{}'::jsonb
       AND NOT EXISTS (SELECT 1 FROM proposal_events pe WHERE pe.proposal_id = p.id)
     ORDER BY p.id
    """
)


@pytest.fixture(scope="session")
def eligible_ids(store: GradedStore, reader: Any) -> list[int]:
    """Every proposal in the store that R24's gate could conceivably admit.

    Selected by the DATABASE's own view of the four conditions rather than by
    calling the module under test, so a bug in the gate cannot also choose the
    population the gate is tested on.
    """
    from recon.apply import AUTO_APPLY_CASE_TYPES

    with reader.connect() as conn:
        rows = conn.execute(
            _ELIGIBLE_PROPOSALS, {"types": sorted(AUTO_APPLY_CASE_TYPES)}
        ).fetchall()
    ids = [row.id for row in rows]
    assert ids, (
        "no proposal in the graded store is high-confidence, non-sensitive, of an "
        "approved case type and unspent -- the apply path would be tested on nothing"
    )
    return ids


@pytest.fixture(scope="session")
def _claimed(eligible_ids: list[int]) -> dict[str, int]:
    return {}


@pytest.fixture
def committed_proposal(
    request: pytest.FixtureRequest, eligible_ids: list[int], _claimed: dict[str, int]
) -> int:
    """A proposal reserved for THIS test, which is going to commit.

    Handed out one per requesting test and remembered by test id, so a rerun of
    the same test in one session gets the same row and two different tests never
    fight over a single-use citation.
    """
    key = request.node.nodeid
    if key not in _claimed:
        taken = set(_claimed.values())
        free = [pid for pid in eligible_ids if pid not in taken]
        assert free, "the apply suite has consumed every eligible proposal in the store"
        _claimed[key] = free[0]
    return _claimed[key]
