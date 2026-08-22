"""Fixtures for the schema tests.

These tests need a live Postgres. The negative permission tests cannot be
faked: they connect as separate LOGIN roles and assert on a specific SQLSTATE,
so a broken connection, a typo, or a missing table produces a *different* error
and fails the test rather than passing it.

``KEYSTONE_REQUIRE_DB`` -- why skipping had to stop being the default story
--------------------------------------------------------------------------
Skipping when ``DATABASE_URL`` is unset keeps a laptop without docker usable.
It also meant 76 of 81 tests skipped in the ticketed verify chain while the run
still reported success. **A green that proves nothing is worse than a red**: it
is indistinguishable from a green that proves everything, and it is the exact
shape of failure this whole package exists to prevent.

So the skip is now opt-out. Set ``KEYSTONE_REQUIRE_DB=1`` -- CI does, see
``.github/workflows/ci.yml`` -- and a missing ``DATABASE_URL`` becomes a hard
collection **error**, not a skip. A database-less run can then never masquerade
as a pass.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

import psycopg
import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from recon.db import (
    ROLE_REVIEW_WRITER,
    WRITER_ROLES,
    DatabaseNotConfigured,
    database_url,
    role_connection,
    role_url,
)

SKIP_REASON = (
    "DATABASE_URL is not set: the schema tests need the live Postgres from "
    "infra/docker-compose.yml (host port 55432). Export DATABASE_URL and run "
    "`uv run alembic upgrade head` first."
)

#: When truthy, "no DATABASE_URL" is a hard error instead of a skip.
REQUIRE_DB_ENV = "KEYSTONE_REQUIRE_DB"

REQUIRE_DB_REASON = (
    f"{REQUIRE_DB_ENV} is set, so the schema tests must actually run -- but "
    "DATABASE_URL is not configured, so every one of them would have skipped "
    "and the run would have reported a green that proves nothing. Start "
    "infra/docker-compose.yml, export DATABASE_URL, and run "
    "`uv run alembic upgrade head`."
)


def database_is_required() -> bool:
    """True when the environment forbids skipping these tests.

    Anything other than an empty string, ``0``, ``false`` or ``no`` counts as
    "required", so the common ``KEYSTONE_REQUIRE_DB=1`` works and a typo errs
    towards running the tests rather than silently skipping them.
    """
    raw = os.environ.get(REQUIRE_DB_ENV, "")
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


#: Marker embedded in every row these tests commit, so teardown can find them.
TEST_TAG = "schema-test"

#: Migration 0006 (RULING 11) requires every ``entity_links`` row to name a
#: ``raw_records`` row with the same ``(source_id, natural_key, generation)``.
#: A canonical row must descend from a record that actually came through the
#: landing table, so every fixture that seeds a link now seeds its record too.
#: This is a *provenance floor*: the link's ingested identity has to exist. It
#: is not proof that the record arrived through an adapter.
INSERT_RAW_RECORD = text(
    "INSERT INTO raw_records (source_id, entity_type, natural_key, generation, payload, "
    "row_hash, load_id, run_id) "
    "VALUES (:source_id, 'person', :natural_key, :generation, '{}'::jsonb, :row_hash, "
    ":load_id, :run_id)"
)


def raw_record_params(
    source_id: str, natural_key: str, generation: int, *, run: str = TEST_TAG
) -> dict[str, object]:
    """Bind parameters seeding the landing record an ``entity_links`` row needs."""
    return {
        "source_id": source_id,
        "natural_key": natural_key,
        "generation": generation,
        "row_hash": f"{TEST_TAG}-hash-{source_id}-{natural_key}-{generation}",
        "load_id": f"{TEST_TAG}-load-{generation}",
        "run_id": run,
    }


#: Migration 0007 (RULING 13) closes ``proposals.action`` to exactly one shape,
#: ``{"set": {path: value, ...}}``, and (RULING 14) binds the canonical write to
#: it: an apply may only set ``current`` to ``OLD.current || action->'set'``.
#: Every fixture proposal therefore has to declare, up front, the one write it
#: authorises -- which is the point. ``{"set": {}}`` is the evidence-only
#: proposal of contract §6: it authorises a canonical write that changes
#: nothing.
EVIDENCE_ONLY_ACTION = '{"set": {}}'

#: What ``seeded_rows``' approved proposal is approved to write. The seeded
#: entity's ``current`` is ``{}``, so this authorises exactly ``{"grade": "4"}``
#: and nothing else.
SEEDED_APPROVED_SET = '{"grade": "4"}'
SEEDED_APPROVED_ACTION = '{"set": {"grade": "4"}}'

#: What every ``canonical_pair`` proposal is approved to write. Both entities
#: carry a single ``grade`` key, so ``OLD.current || {"grade": "tampered"}`` is
#: ``{"grade": "tampered"}`` for either of them -- one value the correlation
#: tests can write to A or B without the *content* rule being the clause that
#: refuses, so each of those tests still proves the clause it names.
PAIR_APPROVED_SET = '{"grade": "tampered"}'
PAIR_APPROVED_ACTION = '{"set": {"grade": "tampered"}}'


RoleTxn = Callable[[str], AbstractContextManager[Connection]]


@pytest.fixture(scope="session")
def configured_url() -> str:
    """The configured DSN.

    Without ``DATABASE_URL`` this skips the package -- unless
    ``KEYSTONE_REQUIRE_DB`` is set, in which case it *fails*, because a run that
    was told the database is mandatory must not be able to report success
    without one.
    """
    try:
        return database_url().render_as_string(hide_password=False)
    except DatabaseNotConfigured:
        if database_is_required():
            pytest.fail(REQUIRE_DB_REASON, pytrace=False)
        pytest.skip(SKIP_REASON)


@pytest.fixture(scope="session")
def owner_engine(configured_url: str) -> Iterator[Engine]:
    """Engine for the principal in ``DATABASE_URL`` -- the schema owner locally."""
    engine = create_engine(configured_url, future=True)
    with engine.connect() as conn:
        migrated = conn.execute(text("SELECT to_regclass('public.entities')")).scalar()
    if migrated is None:
        pytest.fail(
            "DATABASE_URL points at a database with no Keystone schema. "
            "Run `uv run alembic upgrade head` in service/ before the schema tests."
        )
    yield engine
    engine.dispose()


@pytest.fixture
def role_txn(configured_url: str) -> RoleTxn:
    """Factory: ``with role_txn(ROLE_RECON_WRITER) as conn:`` -- always rolls back.

    The connection authenticates **as the role**, not as the owner with
    ``SET ROLE``; an owner connection would bypass every grant under test and
    the negative assertions would prove nothing.
    """

    @contextmanager
    def _txn(role: str) -> Iterator[Connection]:
        with role_connection(role, commit=False) as conn:
            yield conn

    return _txn


@pytest.fixture(scope="session")
def seeded_rows(owner_engine: Engine) -> Iterator[dict[str, object]]:
    """Committed rows the role tests need to already exist, cleaned up after.

    A conflict and its proposals must be *visible from another connection*, so
    they are committed rather than created inside a test transaction.

    Three things here are load-bearing for migration 0005:

    * the entity is committed **with an ``entity_links`` row**. The deferred
      ``entities_require_provenance`` trigger (KS008) fires at COMMIT, so a
      canonical row with no provenance can no longer be seeded at all -- which
      is the point of that rule, and this fixture is the first thing it binds.
    * every proposal names a ``target_canonical_id``. One proposal authorises
      exactly one entity.
    * ``approved_proposal_id`` is driven to ``approved`` **over a real
      ``review_writer`` connection**, not by an owner UPDATE. The apply-path
      tests need an already-approved proposal to control on, and manufacturing
      one as the owner would quietly bypass the very transition graph under
      test.

    Migration 0007 adds a fourth: every proposal declares the content it
    authorises. ``approved_proposal_id`` carries ``SEEDED_APPROVED_ACTION``, so
    the one canonical write it can buy is ``{"grade": "4"}`` -- and the pending
    ``proposal_id`` carries the evidence-only action, which authorises a write
    that changes nothing.
    """
    canonical_id = uuid.uuid5(uuid.NAMESPACE_URL, "keystone/tests/schema/entity")
    fingerprint = f"{TEST_TAG}-{canonical_id}"
    approved_fingerprint = f"{TEST_TAG}-approved-{canonical_id}"

    insert_proposal = text(
        """
        INSERT INTO proposals (
            conflict_id, fingerprint, action, confidence, evidence, created_run,
            target_canonical_id)
        VALUES (:cid, :fp, CAST(:action AS jsonb), 0.5, '{}'::jsonb, :run, :target)
        ON CONFLICT (fingerprint) WHERE status <> 'rejected'::proposal_status
        DO UPDATE SET created_run = EXCLUDED.created_run
        RETURNING id
        """
    )

    with owner_engine.begin() as conn:
        # RULING 11: the link must name an ingested record, so the landing row
        # comes first. Appended unconditionally -- raw_records is append-only
        # and duplicate natural keys within a generation are legitimate input --
        # and removed by name in teardown.
        conn.execute(INSERT_RAW_RECORD, raw_record_params("crm", f"{TEST_TAG}-entity", 99))
        conn.execute(
            text(
                "INSERT INTO entity_links (canonical_id, source_id, source_key, source_ref, "
                "method, generation) VALUES (:cid, 'crm', :key, :ref, 'L1', 99) "
                "ON CONFLICT (generation, source_id, source_key) DO NOTHING"
            ),
            {
                "cid": canonical_id,
                "key": f"{TEST_TAG}-entity",
                "ref": f"crm:contact:{TEST_TAG}-entity",
            },
        )
        conn.execute(
            text(
                "INSERT INTO entities (canonical_id, entity_type, current) "
                "VALUES (:cid, 'person', '{}'::jsonb) ON CONFLICT DO NOTHING"
            ),
            {"cid": canonical_id},
        )
        conflict_id = conn.execute(
            text(
                """
                INSERT INTO conflicts (
                    fingerprint, type, entity_refs, sources, disagreeing_fields,
                    first_seen_run, last_seen_run)
                VALUES (:fp, 'field-disagreement', '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
                        :run, :run)
                ON CONFLICT (fingerprint) DO UPDATE SET last_seen_run = EXCLUDED.last_seen_run
                RETURNING id
                """
            ),
            {"fp": fingerprint, "run": TEST_TAG},
        ).scalar_one()
        proposal_id = conn.execute(
            insert_proposal,
            {
                "cid": conflict_id,
                "fp": fingerprint,
                "run": TEST_TAG,
                "target": canonical_id,
                "action": EVIDENCE_ONLY_ACTION,
            },
        ).scalar_one()
        approved_proposal_id = conn.execute(
            insert_proposal,
            {
                "cid": conflict_id,
                "fp": approved_fingerprint,
                "run": TEST_TAG,
                "target": canonical_id,
                "action": SEEDED_APPROVED_ACTION,
            },
        ).scalar_one()

    # The decision is made by the role that is allowed to make it. If 0005's
    # transition graph were wrong, this raises at collection time rather than
    # letting the apply tests run against a proposal nobody approved.
    with role_connection(ROLE_REVIEW_WRITER) as conn:
        conn.execute(
            text(
                "UPDATE proposals SET status = 'approved', decided_by = :who, "
                "decided_at = now() WHERE id = :pid AND status <> 'approved'"
            ),
            {"pid": approved_proposal_id, "who": f"reviewer:{TEST_TAG}"},
        )

    yield {
        "canonical_id": canonical_id,
        "conflict_id": conflict_id,
        "proposal_id": proposal_id,
        "approved_proposal_id": approved_proposal_id,
        "fingerprint": fingerprint,
    }

    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM proposal_events WHERE proposal_id IN (:pid, :apid)"),
            {"pid": proposal_id, "apid": approved_proposal_id},
        )
        conn.execute(text("DELETE FROM proposals WHERE conflict_id = :cid"), {"cid": conflict_id})
        conn.execute(text("DELETE FROM conflicts WHERE id = :cid"), {"cid": conflict_id})
        conn.execute(text("DELETE FROM entities WHERE canonical_id = :eid"), {"eid": canonical_id})
        conn.execute(
            text("DELETE FROM entity_links WHERE canonical_id = :eid"), {"eid": canonical_id}
        )
        conn.execute(
            text("DELETE FROM raw_records WHERE generation = 99 AND natural_key = :key"),
            {"key": f"{TEST_TAG}-entity"},
        )


#: Committed pre-update value of each fixture entity. The correlation rule
#: compares ``proposal_events.before`` against exactly this.
CURRENT_A = '{"grade": "4"}'
CURRENT_B = '{"grade": "7"}'


@pytest.fixture(scope="session")
def canonical_pair(owner_engine: Engine) -> Iterator[dict[str, object]]:
    """Two committed canonical rows with *different* current values.

    Session-scoped and shared: both the 0004 correlation tests and the 0005
    citation tests need the same pair, and committing two competing copies of
    the same canonical ids from two modules would deadlock on cleanup.

    Two rows, because the mass-rewrite attack needs a second entity to sweep up
    behind the one legitimate reversal record. Different values, because a
    correlation rule that compared ``before`` against a value both rows happen
    to share would pass for the wrong reason.

    Migration 0005 adds two things this fixture must now provide:

    * an ``entity_links`` row per entity -- the deferred provenance trigger
      (KS008) fires at COMMIT, so a canonical row with no source lineage can no
      longer be seeded at all;
    * one **approved** proposal per entity, each targeting only its own
      ``canonical_id``. RULING 3 makes one proposal authorise exactly one
      entity, so the mass-rewrite attack can no longer even be *expressed* with
      a single citation, and the legitimate batched apply needs one approved
      proposal per row. The decisions are made over a real ``review_writer``
      connection, never by an owner UPDATE that would sidestep the graph.

    Migration 0007 adds one more: each proposal's ``action`` names the content
    it authorises (``PAIR_APPROVED_ACTION``), because a citation now buys one
    write of that content and nothing else.
    """
    first = uuid.uuid5(uuid.NAMESPACE_URL, "keystone/tests/schema/correlation/a")
    second = uuid.uuid5(uuid.NAMESPACE_URL, "keystone/tests/schema/correlation/b")

    link = text(
        "INSERT INTO entity_links (canonical_id, source_id, source_key, source_ref, method, "
        "generation) VALUES (:cid, 'crm', :key, :ref, 'L1', 98) "
        "ON CONFLICT (generation, source_id, source_key) DO NOTHING"
    )
    insert = text(
        "INSERT INTO entities (canonical_id, entity_type, current) "
        "VALUES (:cid, 'person', CAST(:current AS jsonb)) "
        "ON CONFLICT (canonical_id) DO UPDATE SET current = EXCLUDED.current"
    )
    conflict_sql = text(
        "INSERT INTO conflicts (fingerprint, type, entity_refs, sources, disagreeing_fields, "
        "first_seen_run, last_seen_run) VALUES (:fp, 'field-disagreement', '[]'::jsonb, "
        "'[]'::jsonb, '[]'::jsonb, :run, :run) "
        "ON CONFLICT (fingerprint) DO UPDATE SET last_seen_run = EXCLUDED.last_seen_run "
        "RETURNING id"
    )
    proposal_sql = text(
        "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence, "
        "created_run, target_canonical_id) "
        f"VALUES (:cid, :fp, '{PAIR_APPROVED_ACTION}'::jsonb, 0.9, '{{}}'::jsonb, :run, "
        ":target) "
        "ON CONFLICT (fingerprint) WHERE status <> 'rejected'::proposal_status "
        "DO UPDATE SET created_run = EXCLUDED.created_run RETURNING id"
    )

    pending_sql = text(
        "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence, "
        "created_run, target_canonical_id) "
        f"VALUES (:cid, :fp, '{PAIR_APPROVED_ACTION}'::jsonb, 0.9, '{{}}'::jsonb, :run, "
        ":target) "
        "ON CONFLICT (fingerprint) WHERE status <> 'rejected'::proposal_status "
        "DO UPDATE SET created_run = EXCLUDED.created_run RETURNING id"
    )

    proposals: dict[str, int] = {}
    with owner_engine.begin() as conn:
        conflict_id = conn.execute(
            conflict_sql, {"fp": f"{TEST_TAG}-correlation", "run": TEST_TAG}
        ).scalar_one()
        for key, cid, current in (("a", first, CURRENT_A), ("b", second, CURRENT_B)):
            # RULING 11: the link must descend from an ingested record.
            conn.execute(
                INSERT_RAW_RECORD,
                raw_record_params("crm", f"{TEST_TAG}-correlation-{key}", 98),
            )
            conn.execute(
                link,
                {
                    "cid": cid,
                    "key": f"{TEST_TAG}-correlation-{key}",
                    "ref": f"crm:contact:{TEST_TAG}-correlation-{key}",
                },
            )
            conn.execute(insert, {"cid": cid, "current": current})
            proposals[key] = conn.execute(
                proposal_sql,
                {
                    "cid": conflict_id,
                    "fp": f"{TEST_TAG}-correlation-{key}",
                    "run": TEST_TAG,
                    "target": cid,
                },
            ).scalar_one()
        # A proposal that targets entity A and stays PENDING. It exists so the
        # "the citation must be an APPROVED proposal" clause can be proved on
        # its own: cite this and every other clause of the correlation is
        # satisfied, so only the status clause can be doing the refusing.
        pending_a = conn.execute(
            pending_sql,
            {
                "cid": conflict_id,
                "fp": f"{TEST_TAG}-correlation-pending-a",
                "run": TEST_TAG,
                "target": first,
            },
        ).scalar_one()

    with role_connection(ROLE_REVIEW_WRITER) as conn:
        conn.execute(
            text(
                "UPDATE proposals SET status = 'approved', decided_by = :who, "
                "decided_at = now() WHERE id = ANY(:ids) AND status <> 'approved'"
            ),
            {"ids": list(proposals.values()), "who": f"reviewer:{TEST_TAG}"},
        )

    yield {
        "a": first,
        "b": second,
        "current_a": CURRENT_A,
        "current_b": CURRENT_B,
        "approved_set": PAIR_APPROVED_SET,
        "proposal_a": proposals["a"],
        "proposal_b": proposals["b"],
        "pending_a": pending_a,
        "conflict_id": conflict_id,
    }

    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM proposal_events WHERE canonical_id = ANY(:ids)"),
            {"ids": [first, second]},
        )
        conn.execute(
            text("DELETE FROM proposal_events WHERE proposal_id = ANY(:ids)"),
            {"ids": [*proposals.values(), pending_a]},
        )
        conn.execute(text("DELETE FROM proposals WHERE conflict_id = :cid"), {"cid": conflict_id})
        conn.execute(text("DELETE FROM conflicts WHERE id = :cid"), {"cid": conflict_id})
        conn.execute(
            text("DELETE FROM entities WHERE canonical_id = ANY(:ids)"), {"ids": [first, second]}
        )
        conn.execute(
            text("DELETE FROM entity_links WHERE canonical_id = ANY(:ids)"),
            {"ids": [first, second]},
        )
        conn.execute(
            text("DELETE FROM raw_records WHERE generation = 98 AND natural_key = ANY(:keys)"),
            {"keys": [f"{TEST_TAG}-correlation-a", f"{TEST_TAG}-correlation-b"]},
        )


def assert_insufficient_privilege(error: BaseException) -> None:
    """Assert this is Postgres *permission denied*, not any other failure.

    Asserting only "an exception was raised" would let a typo, a missing table,
    or a dead connection pass as proof of a privilege boundary. This pins both
    the psycopg exception class and SQLSTATE 42501.
    """
    assert isinstance(error, DBAPIError), f"expected a DBAPI error, got {type(error).__name__}"
    orig = error.orig
    assert isinstance(orig, psycopg.errors.InsufficientPrivilege), (
        f"expected InsufficientPrivilege, got {type(orig).__name__}: {orig}"
    )
    assert orig.sqlstate == "42501", f"expected SQLSTATE 42501, got {orig.sqlstate}"


def assert_sqlstate(error: BaseException, sqlstate: str) -> None:
    """Assert the underlying Postgres error carries exactly ``sqlstate``."""
    assert isinstance(error, DBAPIError), f"expected a DBAPI error, got {type(error).__name__}"
    actual = getattr(error.orig, "sqlstate", None)
    assert actual == sqlstate, f"expected SQLSTATE {sqlstate}, got {actual}: {error.orig}"


#: All three boundary roles, in lifecycle order: propose, decide, apply.
ROLES = WRITER_ROLES

SCRATCH_DB = os.environ.get("KEYSTONE_SCRATCH_DB", "keystone_schema_test")


def psycopg_dsn(role: str) -> str:
    """Plain libpq DSN authenticated as ``role``.

    The concurrency proof needs genuinely independent connections, opened
    directly with psycopg rather than borrowed from a pooled SQLAlchemy engine,
    so "concurrent" means concurrent backends and not interleaved statements on
    one socket.
    """
    return role_url(role).set(drivername="postgresql").render_as_string(hide_password=False)
