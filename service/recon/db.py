"""Database engines, sessions, and role-scoped connections.

Everything here derives from ``DATABASE_URL`` supplied by :mod:`recon.config`.
There is deliberately **no DSN literal anywhere in this module** -- a
misconfigured process must fail loudly with :class:`DatabaseNotConfigured`
rather than quietly connecting to some default database.

Two things live here that the rest of the service depends on:

``get_engine()`` / ``session_scope()``
    The ordinary application engine, connecting as whatever principal
    ``DATABASE_URL`` names (in local development that is the schema *owner*).

``role_connection(role)`` / ``engine_for_role(role)``
    The holds-before-writes boundary. Postgres privileges are the enforcement
    point for "proposals land pending, nothing writes the canonical layer
    without going through apply" -- but only if the process actually *connects
    as* the restricted role. A table owner bypasses its own grants, so every
    path must acquire its connection through ``role_connection(...)``. Using
    the owner engine for any of them silently disables the boundary.

    Three roles, because two cannot express the property the project is graded
    on -- *the automation must not be able to approve its own work*. With one
    role proposing and a second both approving and applying, "approve" and
    "apply" are the same principal. Duties therefore partition three ways:

    ============== ========= ==================================================
    role           duty      may
    ============== ========= ==================================================
    recon_writer   PROPOSES  append evidence and proposals-born-pending
    review_writer  DECIDES   pending|sensitive_hold -> approved|rejected
    apply_writer   APPLIES   approved -> applied, applied -> rolled_back, and
                             UPDATE entities(current, updated_at) under the
                             cited-proposal trigger
    ============== ========= ==================================================

    The graph is enforced by the database (``migrations/versions/
    0005_three_role_boundary.py`` and ``0006_single_use_citations.py``, SQLSTATE
    ``KS004``), not by this module: a caller that picks the wrong role gets an
    error, not a silent bypass. Since 0006 the graph binds the schema owner too
    -- as defence in depth, not as a boundary, since the owner can drop the
    trigger -- and ``decided_by``/``decided_at`` are frozen once set, so a
    decision cannot be re-signed, re-dated or unsigned by anyone.

    **A citation is spent when it is used.** One approved proposal authorises
    exactly one canonical write and one reversal, and then nothing. An apply
    must therefore write its ``proposal_events`` row, its canonical UPDATE and
    its ``approved -> applied`` status move in ONE transaction (0006, SQLSTATE
    ``KS001`` plus ``uq_proposal_events_applied_once``); a rollback does the
    same on the ``applied -> rolled_back`` leg and must restore exactly the
    value the apply's event captured. Re-citing an already-applied proposal in
    a later transaction is refused, not merely audited -- which is what "one
    human approval, one canonical write" means.

    **And the write must be the content that was approved.** Since 0007
    ``proposals.action`` is a closed vocabulary -- exactly ``{"set": {path:
    value, ...}}``, enforced by ``ck_proposals_action_vocabulary``, with
    ``{"set": {}}`` for the evidence-only proposals of contract §6 -- and the
    apply leg accepts a canonical UPDATE only when::

        NEW.current = OLD.current || action -> 'set'

    Anything else is SQLSTATE ``KS010``, a code no other rule produces. A
    caller therefore does not choose what to write: it writes the merge of the
    approved action onto the current value, or the transaction is refused.

    **And the ledger cannot describe a write that did not happen.** Every rule
    above fires on UPDATE of ``entities``, so until 0008 nothing stopped
    ``apply_writer`` INSERTing an ``applied`` ``proposal_events`` row with an
    author-chosen ``before`` and performing no canonical UPDATE at all -- no
    write, therefore no trigger, therefore no rule -- and then citing that row
    on the reversal leg to write its chosen value into the canonical row. Three
    rules close it and each is load-bearing:

    * an ``applied``/``rolled_back`` event must name a canonical row that
      **this transaction actually wrote** (deferred constraint trigger
      ``proposal_events_describe_a_real_write``, SQLSTATE ``KS011``);
    * a transaction may write at most one canonical-mutating event per entity
      (``uq_proposal_events_canonical_write_once``), which is what makes the
      pairing between events and writes one-to-one and therefore makes an
      event's ``before`` provably the value the row held at transaction start.
      Two approvals for one entity are two writes, so they are two
      transactions;
    * a reversal must restore the write that is currently on top: the cited
      ``applied`` event's ``after`` must equal the row's pre-reversal value, as
      well as its ``before`` equalling the value written back (SQLSTATE
      ``KS012``). Rolling back an earlier apply after a later one has landed
      would silently discard an approved, applied, unreversed write.

    Every jsonb comparison in these rules is pinned textually as well as
    semantically (``a = b AND a::text = b::text``), because ``'{"amount": 1}'``
    and ``'{"amount": 1.0}'`` are equal as jsonb and differ as text, and
    ``recon.suite.mirror`` hashes ``md5(row::text)`` on a graded determinism
    path. ``entities.current`` is CHECKed to be a JSON object, which is what
    makes ``OLD.current || action->'set'`` a merge rather than an append.

    The ``KS010`` and ``KS012`` diagnostics name the **field paths** that differ
    and never their values: those messages reach the client and the Postgres
    server log, and the canonical record carries personal data.

    Two things a caller never has to arrange, and must not try to:
    ``proposal_events.actor`` is role-scoped like ``audit_log.actor``
    (``^system:`` for the machine roles, ``^reviewer:`` for ``review_writer``,
    SQLSTATE ``KS003``), and ``proposal_events.event`` is a closed vocabulary of
    ``applied``/``rolled_back``/``noted`` -- no decision word can be written
    into the reversal ledger at all.

Role credentials are read from the environment, never stored here:
``RECON_WRITER_PASSWORD``, ``REVIEW_WRITER_PASSWORD`` and
``APPLY_WRITER_PASSWORD``. When unset they fall back to the role name, which is
what the migrations also do for local development; production must set them
explicitly (see ``migrations/versions/0002_roles_and_grants.py`` and
``0005_three_role_boundary.py``).
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from recon.config import get_settings

__all__ = [
    "API_KEY_SALT",
    "PRINCIPAL_ENV_VARS",
    "ROLE_APPLY_WRITER",
    "ROLE_RECON_WRITER",
    "ROLE_REVIEW_WRITER",
    "WRITER_ROLES",
    "DatabaseNotConfigured",
    "api_key_hash",
    "connected_to",
    "database_url",
    "engine_for_role",
    "get_engine",
    "get_sessionmaker",
    "restore_principal",
    "role_connection",
    "role_password",
    "role_url",
    "session_scope",
    "switch_principal",
]

#: PROPOSES. Detection path (ingest, staging, ER, invariants, reconciler):
#: append-only on the reconciliation tables, INSERT on ``entities`` but never
#: UPDATE/DELETE, no UPDATE at all on ``proposals``, and -- since 0005 -- no
#: INSERT or UPDATE anywhere on ``budget_ledger``. It reserves spend by
#: INSERTing a ``budget_reservations`` row and settles it once; the ledger's
#: ``spent_microusd`` is maintained only by that table's triggers, so the capped
#: party has no writable spend column to zero. Since 0006 it also holds no
#: EXECUTE on the SECURITY DEFINER ledger mutators and no TEMPORARY on the
#: database -- the triggers run as the owner, so nothing legitimate needs
#: either, and ``pg_temp`` is no longer a place to define code that calls them.
#: Its ``entity_links`` rows must name a ``raw_records`` row with the same
#: ``(source_id, natural_key, generation)``: a canonical row descends from an
#: ingested record (SQLSTATE ``KS009``).
ROLE_RECON_WRITER = "recon_writer"

#: DECIDES. The human review API's role, and the ONLY one that may move a
#: proposal to ``approved``/``rejected`` (and only from ``pending`` or
#: ``sensitive_hold``). Its UPDATE on ``proposals`` is column-scoped to
#: ``(status, decided_by, decided_at)``; it holds no INSERT on ``proposals``
#: and no write of any kind on ``entities``. Its audit rows must be
#: ``^reviewer:`` scoped.
ROLE_REVIEW_WRITER = "review_writer"

#: APPLIES. The apply path only: ``approved -> applied`` and
#: ``applied -> rolled_back``, plus ``UPDATE entities(current, updated_at)``
#: gated by the cited-proposal trigger. It cannot propose, cannot approve, and
#: cannot name a decider. Since 0006 it also cannot re-use a citation: the
#: proposal it cites must be moving through its apply or reversal leg in the
#: same transaction, and each leg is writable exactly once per proposal. Since
#: 0007 it also cannot choose the *content*: the only canonical write a
#: citation authorises is ``OLD.current || action->'set'`` of the proposal it
#: cites (SQLSTATE ``KS010``). Since 0008 it cannot write a ledger event that
#: describes a canonical write it did not perform (SQLSTATE ``KS011``), cannot
#: write two canonical events for one entity in one transaction, and cannot
#: reverse an apply that a later approved write has already covered (SQLSTATE
#: ``KS012``).
ROLE_APPLY_WRITER = "apply_writer"

#: Ordered by the lifecycle: propose, decide, apply.
WRITER_ROLES = (ROLE_RECON_WRITER, ROLE_REVIEW_WRITER, ROLE_APPLY_WRITER)

#: Committed, non-secret salt for API-key hashing. The database stores only
#: ``sha256(f"{API_KEY_SALT}:{key}")`` -- never the plaintext key. This value is
#: duplicated verbatim in the seed migration, which is immutable history; the
#: schema test suite asserts the two agree.
API_KEY_SALT = "keystone-api-key-salt-v1"

_ASYNC_SAFE_DRIVER = "postgresql+psycopg"


class DatabaseNotConfigured(RuntimeError):
    """Raised when ``DATABASE_URL`` is absent from the environment."""


def database_url() -> URL:
    """Return ``DATABASE_URL`` as a SQLAlchemy :class:`URL`, psycopg-driven.

    A bare ``postgresql://`` DSN (what ``.env.example`` and most hosting
    providers hand you) is rewritten onto the ``postgresql+psycopg`` driver so
    the service always speaks psycopg 3.
    """
    raw = get_settings().database_url
    if not raw:
        raise DatabaseNotConfigured(
            "DATABASE_URL is not set. Export it (see .env.example) before using recon.db."
        )
    url = make_url(raw)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername=_ASYNC_SAFE_DRIVER)
    return url


def role_password(role: str) -> str:
    """Password for ``role``, from ``<ROLE>_PASSWORD`` in the environment.

    Falls back to the role name, matching the local-development default the
    roles migration uses. Production sets the variables explicitly.
    """
    return os.environ.get(f"{role.upper()}_PASSWORD") or role


def role_url(role: str) -> URL:
    """``DATABASE_URL`` with its credentials swapped for ``role``'s.

    Host/port/database are inherited, so a role connection always lands on the
    same database the application is configured for.
    """
    return database_url().set(username=role, password=role_password(role))


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Process-wide engine for the principal named by ``DATABASE_URL``."""
    return create_engine(database_url(), pool_pre_ping=True, future=True)


@lru_cache(maxsize=len(WRITER_ROLES))
def engine_for_role(role: str) -> Engine:
    """Cached engine that authenticates **as** ``role``.

    ``SET ROLE`` is deliberately not used: it is reversible from inside the
    session (``RESET ROLE``) and it leaves the connection owned by the schema
    owner, who bypasses grants. Only a real login as the restricted role makes
    the privilege boundary an actual boundary -- and the status-transition
    trigger keys on ``current_user``, which ``SET ROLE`` would also satisfy
    while leaving the grants wide open.
    """
    if role not in WRITER_ROLES:
        raise ValueError(f"unknown writer role {role!r}; expected one of {WRITER_ROLES}")
    return create_engine(role_url(role), pool_pre_ping=True, future=True)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    """Session factory bound to the application engine."""
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session scope: commit on success, roll back on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def role_connection(role: str, *, commit: bool = True) -> Iterator[Connection]:
    """Open a transaction on a connection authenticated as ``role``.

    Usage::

        with role_connection(ROLE_RECON_WRITER) as conn:
            conn.execute(insert_proposal, params)

    The transaction commits on clean exit unless ``commit=False`` (used by the
    schema tests, which must leave no rows behind). Any exception rolls back.
    """
    engine = engine_for_role(role)
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        except Exception:
            transaction.rollback()
            raise
        else:
            if commit:
                transaction.commit()
            else:
                transaction.rollback()


def api_key_hash(key: str) -> str:
    """Hex sha256 of ``key`` under the committed salt.

    The only representation of an API key that ever reaches the database.
    """
    return hashlib.sha256(f"{API_KEY_SALT}:{key}".encode()).hexdigest()


def reset_engine_cache() -> None:
    """Drop cached engines. Only for a process that changes a DSN variable.

    Three of the four caches live here. The fourth is
    ``recon.budget._ops_engine_for``, which holds an engine per explicit
    ``OPS_DATABASE_URL`` -- the DSN ``recon.budget.ops_engine`` *prefers*. While
    that one was not cleared, "drop cached engines" was simply untrue of a
    quarter of them.

    **What that did not cost is a stale identity**, and this docstring used to
    claim it did. ``_ops_engine_for`` is an ``lru_cache`` keyed by the DSN
    string and ``ops_engine`` re-reads the environment on every call, so a
    switch to a different principal always got a different cache key and a fresh
    engine; no caller ever transacted as the identity a switch had left behind.
    What it cost is the engines a switch **orphans** -- up to four of them, each
    holding a live pool against a database the process has walked away from and,
    for the scratch-database helpers, is about to ``DROP ... WITH (FORCE)`` --
    and the plain truth of this function's name, which
    ``tests/budget/test_ledger.py::test_reset_engine_cache_drops_the_ops_engine_too``
    pins by asking for two engines on one DSN across a reset.

    It is reached through :data:`sys.modules` rather than imported, because
    ``recon.budget`` imports *this* module: there is nothing to clear if it was
    never loaded, and nothing to clear yet if it is still being loaded.
    """
    get_engine.cache_clear()
    engine_for_role.cache_clear()
    get_sessionmaker.cache_clear()
    ops_engine_cache = getattr(sys.modules.get("recon.budget"), "_ops_engine_for", None)
    if ops_engine_cache is not None:
        ops_engine_cache.cache_clear()


#: Every environment variable that can name a database principal for this
#: process. ``DATABASE_URL`` is what the process serves as; ``OPS_DATABASE_URL``
#: is the one ``recon.budget.ops_engine`` and
#: ``recon.api.internal._invariant_dsn`` *prefer* whenever it is set, falling
#: back to the first only when it is not.
#:
#: **Both have to move together or a switch is not a switch.** Repointing
#: ``DATABASE_URL`` alone at a scratch database leaves every ops call -- a
#: provisioned ledger scope, a lease sweep, the entire invariant pass --
#: transacting against whatever ``OPS_DATABASE_URL`` names, which on the
#: deployed service (``infra/render.yaml``) is the production owner DSN.
#: Measured, before this was one helper: a single run of
#: ``tests/integration/test_sync_pipeline.py`` put 752,000 ``invariant_results``
#: rows and three ``budget_ledger`` scopes into that database.
#:
#: Spelled here rather than imported from :mod:`recon.budget`, which imports
#: *this* module; ``tests/budget/test_principal_switch.py`` pins the two equal.
PRINCIPAL_ENV_VARS = ("DATABASE_URL", "OPS_DATABASE_URL")


def switch_principal(dsn: str) -> dict[str, str | None]:
    """Point every variable in :data:`PRINCIPAL_ENV_VARS` at ``dsn``.

    Returns the environment it replaced -- one entry per variable, ``None``
    where the variable was unset -- for :func:`restore_principal` to put back.
    Both ``lru_cache``s keyed on those variables are dropped, so a module that
    took an engine before the switch cannot go on using it.
    """
    previous: dict[str, str | None] = {name: os.environ.get(name) for name in PRINCIPAL_ENV_VARS}
    for name in PRINCIPAL_ENV_VARS:
        os.environ[name] = dsn
    get_settings.cache_clear()
    reset_engine_cache()
    return previous


def restore_principal(previous: Mapping[str, str | None]) -> None:
    """Put back exactly what :func:`switch_principal` returned.

    A variable that was unset is **popped**, not blanked: "restored" has to mean
    restored.
    """
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_settings.cache_clear()
    reset_engine_cache()


@contextmanager
def connected_to(dsn: str) -> Iterator[str]:
    """:func:`switch_principal` for the duration of the block, then restored.

    The one helper behind every principal switch in this repository: the spend
    burst's ``_connected_as`` and both scratch-database fixtures
    (``tests/er/scratchdb``, ``tests/invariants/scratchdb``). They were three
    copies, and two of them had drifted to ``DATABASE_URL`` alone.
    """
    previous = switch_principal(dsn)
    try:
        yield dsn
    finally:
        restore_principal(previous)
