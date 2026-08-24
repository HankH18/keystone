"""The three write endpoints must reach Postgres as their least-privilege role.

`tests/schema/test_role_permissions.py` and `tests/schema/test_write_boundary_hardening.py`
prove what `review_writer` and `apply_writer` may and may not do -- they open the
role connection themselves. Nothing proved that the *endpoints* open one: replacing
all three `role_connection(...)` calls in `recon/api/review.py` with
`get_engine().begin()`, so that every approve, apply and rollback runs as the schema
owner (who bypasses every grant), left `uv run pytest tests/api` at 120 passed.

What is asserted here is the login each statement actually executed under, read off
the connection Postgres received it on, for the duration of one real HTTP request.
`recon.db.engine_for_role` builds a separate engine per role and authenticates as a
real login rather than issuing `SET ROLE`, so the connection's user *is* the
principal the grants are checked against.

One citation, three legs. A proposal is single-use, so the approve, the apply and
the reversal below are the same row moving through its lifecycle, and the row is
left `rolled_back` -- the state `test_the_endpoint_restores_the_canonical_row_byte_for_byte`
leaves its own citation in.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, text

from tests.api.conftest import ADMIN_HEADERS

#: The rollback suite's pool query, imported rather than copied: this module claims
#: from the same list and must shrink and grow with it. See :func:`citation`.
from tests.api.test_rollback_api import _LONE_ELIGIBLE

#: Statements that change rows. `WITH` leads a CTE that may or may not end in DML,
#: so it is resolved by looking for a DML keyword anywhere in the statement --
#: over-inclusive on purpose, because a write this scanner misses is a write the
#: assertion below stops covering.
_DML = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})

_LEADING_COMMENT = re.compile(r"\A(?:\s|--[^\n]*\n|/\*.*?\*/)*", re.DOTALL)


def _write_verb(statement: str) -> str | None:
    """The DML verb `statement` performs, or `None` if it changes nothing."""
    body = _LEADING_COMMENT.sub("", statement)
    head = body.split(None, 1)[0].upper() if body.split() else ""
    if head in _DML:
        return head
    if head == "WITH" and re.search(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", body, re.IGNORECASE):
        return "WITH"
    return None


@contextmanager
def watch_write_principals() -> Iterator[list[tuple[str, str]]]:
    """Record `(login role, verb)` for every write executed while this is open.

    The listener is attached to the `Engine` class, so it sees every engine in the
    process -- including one the code under test creates for itself. Closing the
    window before any assertion keeps the suite's own bookkeeping out of the record.
    """
    seen: list[tuple[str, str]] = []

    def record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        verb = _write_verb(statement)
        if verb is not None:
            seen.append((conn.engine.url.username or "", verb))

    event.listen(Engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", record)


def principals(writes: list[tuple[str, str]]) -> list[str]:
    return sorted({who for who, _ in writes})


@pytest.fixture(scope="module")
def citation(review_api: TestClient, reader: Any) -> int:
    """One unspent applyable proposal, claimed from the END of the shared pool.

    Every other claimant takes from the front of this same `id DESC` list --
    `tests/api/test_decisions.py` at 0..2, `tests/api/test_rollback_api.py` past an
    offset of 8, `tests/apply` from the front -- so the last row is disjoint from all
    of them by construction, and stays disjoint however the suites are ordered:
    spending a citation removes it from the front of the list, never from the end.
    """
    with reader.connect() as conn:
        ids = [row.id for row in conn.execute(_LONE_ELIGIBLE)]
    assert len(ids) > 20, (
        f"only {len(ids)} unspent applyable proposals remain; the front of this pool "
        "is claimed up to index 13, so the last row is only safely unclaimed while "
        "the pool is comfortably longer than that"
    )
    return ids[-1]


def status_of(reader: Any, proposal_id: int) -> str:
    with reader.connect() as conn:
        return conn.execute(
            text("SELECT status::text FROM proposals WHERE id = :id"), {"id": proposal_id}
        ).scalar_one()


def test_each_write_endpoint_transacts_as_its_own_role(
    review_api: TestClient, reader: Any, citation: int
) -> None:
    """approve as `review_writer`, apply and reverse as `apply_writer`.

    A leg that issued no write at all would satisfy "no write ran as the wrong
    principal" vacuously, so each leg asserts that it wrote *something* and that the
    proposal moved; the principal assertion is what the endpoint is on trial for.
    """
    with watch_write_principals() as writes:
        response = review_api.post(f"/api/proposals/{citation}/approve", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    assert writes, "the approve endpoint reached COMMIT without executing a single write"
    assert principals(writes) == ["review_writer"], (
        f"approve wrote as {principals(writes)}, not as review_writer alone: the "
        "decision boundary is the Postgres login, and a decision written by the schema "
        f"owner is written by a principal no grant constrains (statements: {writes})"
    )
    assert status_of(reader, citation) == "approved"

    with watch_write_principals() as writes:
        response = review_api.post(f"/api/proposals/{citation}/apply", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    assert writes, "the apply endpoint reached COMMIT without executing a single write"
    assert principals(writes) == ["apply_writer"], (
        f"apply wrote as {principals(writes)}, not as apply_writer alone: the canonical "
        "layer is only ever written by the one role the apply grants are attached to "
        f"(statements: {writes})"
    )
    assert status_of(reader, citation) == "applied"

    with watch_write_principals() as writes:
        response = review_api.post(f"/api/proposals/{citation}/rollback", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    assert writes, "the rollback endpoint reached COMMIT without executing a single write"
    assert principals(writes) == ["apply_writer"], (
        f"rollback wrote as {principals(writes)}, not as apply_writer alone: the "
        "reversal is the apply path's other half and runs under the same login "
        f"(statements: {writes})"
    )
    assert status_of(reader, citation) == "rolled_back"
