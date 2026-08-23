"""Round six: the ledger cannot describe a write that never happened.

Round five's headline was *"one human approval authorises one canonical write of
the content that was approved, and one reversal of it, and then nothing"*. The
second half was false, and the way it was false is the point of this module.

Every rule in migrations 0004-0007 fires on **UPDATE of ``entities``**. Nothing
required a ledger row to co-occur with a write. So ``apply_writer`` could INSERT
an ``applied`` ``proposal_events`` row describing a write that never happened,
choosing its ``before`` freely, and commit it -- no canonical UPDATE, therefore
no trigger, therefore no rule. The rollback arm then treated that
attacker-authored ``before`` as the value to restore, and wrote it. Reproduced
against the live database before migration 0008::

    entity before attack: {"grade": "4"}
    LEG 1 (forge applied event, no UPDATE): ACCEPTED
    LEG 2 (rollback writes the forged value): ACCEPTED
    entity AFTER attack: {"grade": "ATTACKER-CHOSEN",
                          "crm.contact.email": "pwned@example.invalid"}

One approval bought one *arbitrary* canonical write, laundered through the
reversal leg, and ``KS010`` never ran because no apply ever happened.

Note what ``after`` was in that reproduction: the entity's own current value.
That is deliberate, and ``test_the_forgery_cannot_hide_behind_an_honest_after``
below pins it -- the fix cannot be "the entity must end the transaction holding
the event's ``after``", because an attacker satisfies that by forging only
``before``. What binds is that a canonical write of that row must have actually
happened here (``KS011``, RULING 15), that it is the only ledger event for that
row in the transaction (RULING 16), and that a reversal may only undo the write
that is currently on top (``KS012``, RULING 17).

Shape of every test here, unchanged from the previous rounds: an exact SQLSTATE
on the negative, a positive control on the same role and connection path, and
committed state wherever the demonstrated attack used committed state.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Barrier

import psycopg
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from recon.db import (
    ROLE_APPLY_WRITER,
    ROLE_RECON_WRITER,
    ROLE_REVIEW_WRITER,
    role_connection,
)
from tests.schema.conftest import (
    INSERT_RAW_RECORD,
    TEST_TAG,
    RoleTxn,
    assert_sqlstate,
    psycopg_dsn,
    raw_record_params,
)

#: RULING 15: an ``applied``/``rolled_back`` event describing a canonical write
#: this transaction did not perform. Produced by exactly one trigger.
EVENT_DESCRIBES_NO_WRITE = "KS011"

#: RULING 17: a reversal that does not restore the state the cited apply left.
#: Produced by exactly one clause of the citation trigger.
REVERSAL_NOT_ON_TOP = "KS012"

#: 0007: the canonical write is not the approved action applied to the old value.
CONTENT_NOT_APPROVED = "KS010"

#: The 0004-0006 catch-all: this citation is unauthorised.
CANONICAL_WRITE_UNAUTHORISED = "KS001"

UNIQUE_VIOLATION = "23505"
CHECK_VIOLATION = "23514"

#: This module's landing generation, distinct from conftest (99, 98), the
#: citation tests (95), the lifecycle test (96), the content tests (94) and the
#: mirror tests (93).
GENERATION = 92

INSERT_EVENT = (
    "INSERT INTO proposal_events (proposal_id, canonical_id, event, before, after, actor) "
    "VALUES (:pid, :cid, :event, CAST(:before AS jsonb), CAST(:after AS jsonb), 'system:apply')"
)
REWRITE = (
    "UPDATE entities SET current = CAST(:after AS jsonb), updated_at = now() "
    "WHERE canonical_id = :cid"
)
SET_STATUS = "UPDATE proposals SET status = CAST(:s AS proposal_status) WHERE id = :pid"
INSERT_PROPOSAL = (
    "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence, "
    "created_run, target_canonical_id, sensitive, status) "
    "VALUES (:cid, :fp, CAST(:action AS jsonb), 0.95, '{}'::jsonb, :run, :target, "
    "        :sensitive, CAST(:status AS proposal_status)) RETURNING id"
)


def _honest_birth(sets: str) -> dict[str, object]:
    """The ``sensitive`` / birth-``status`` pair a row writing ``sets`` must carry.

    Migration 0012's ``ck_proposals_sensitive_covers_write_set`` refuses a
    proposal that claims ``sensitive = false`` while ``action->'set'`` names a
    contract SS6 ``SENSITIVE_FIELDS`` path, and ``KS002`` then requires such a
    row to be born ``sensitive_hold``. Derived from ``recon.reference`` rather
    than hard-coded per case, so a row here is honest for the same reason the
    reconciler's rows are and stays honest if SS6 changes.
    """
    from recon.reference import SENSITIVE_FIELDS

    written = json.loads(sets) if sets.strip() else {}
    sensitive = any(path in SENSITIVE_FIELDS for path in written)
    return {"sensitive": sensitive, "status": "sensitive_hold" if sensitive else "pending"}


# ===========================================================================
# fixtures -- committed, because the demonstrated attack ran on committed state
# ===========================================================================
@dataclass
class World:
    """One committed canonical row with provenance, plus proposal helpers."""

    engine: Engine
    canonical_id: uuid.UUID
    conflict_id: int
    tag: str
    proposals: list[int] = field(default_factory=list)

    def proposal(self, label: str, sets: str = "{}", *, approve: bool = True) -> int:
        """Commit a proposal for ``{"set": <sets>}``, decided by a real reviewer.

        The approval goes over a ``review_writer`` connection: an approval
        manufactured by an owner UPDATE would sidestep the transition graph, and
        every apply below would then be proving nothing about the guarded path.
        """
        with self.engine.begin() as conn:
            proposal_id = conn.execute(
                text(INSERT_PROPOSAL),
                {
                    "cid": self.conflict_id,
                    "fp": f"{self.tag}-{label}",
                    "run": TEST_TAG,
                    "target": self.canonical_id,
                    "action": f'{{"set": {sets}}}',
                    **_honest_birth(sets),
                },
            ).scalar_one()
        self.proposals.append(proposal_id)
        if approve:
            with role_connection(ROLE_REVIEW_WRITER) as conn:
                conn.execute(
                    text(
                        "UPDATE proposals SET status = 'approved', decided_by = :who, "
                        "decided_at = now() WHERE id = :pid"
                    ),
                    {"pid": proposal_id, "who": f"reviewer:{TEST_TAG}"},
                )
        return proposal_id

    def apply(self, proposal_id: int, before: str, after: str) -> None:
        """The legitimate apply, committed: ledger row, canonical write, status.

        All three in ONE transaction as ``apply_writer``, exactly as the product
        does it. If any rule in this round broke the real path, this raises and
        every test that uses it fails loudly rather than passing on a dead path.
        """
        with role_connection(ROLE_APPLY_WRITER) as conn:
            conn.execute(
                text(INSERT_EVENT),
                {
                    "pid": proposal_id,
                    "cid": self.canonical_id,
                    "event": "applied",
                    "before": before,
                    "after": after,
                },
            )
            conn.execute(text(REWRITE), {"cid": self.canonical_id, "after": after})
            conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "applied"})

    def roll_back(self, proposal_id: int, before: str, after: str) -> None:
        """The legitimate reversal, committed, on the same one-transaction shape."""
        with role_connection(ROLE_APPLY_WRITER) as conn:
            conn.execute(
                text(INSERT_EVENT),
                {
                    "pid": proposal_id,
                    "cid": self.canonical_id,
                    "event": "rolled_back",
                    "before": before,
                    "after": after,
                },
            )
            conn.execute(text(REWRITE), {"cid": self.canonical_id, "after": after})
            conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "rolled_back"})

    def current(self) -> str:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT current::text FROM entities WHERE canonical_id = :c"),
                {"c": self.canonical_id},
            ).scalar_one()

    def status(self, proposal_id: int) -> str:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT status FROM proposals WHERE id = :p"), {"p": proposal_id}
            ).scalar_one()

    def events(self, proposal_id: int) -> list[str]:
        with self.engine.connect() as conn:
            return list(
                conn.execute(
                    text("SELECT event FROM proposal_events WHERE proposal_id = :p ORDER BY id"),
                    {"p": proposal_id},
                ).scalars()
            )


def _new_world(engine: Engine, current: str) -> World:
    tag = f"{TEST_TAG}-honesty-{uuid.uuid4()}"
    canonical_id = uuid.uuid5(uuid.NAMESPACE_URL, f"keystone/tests/schema/honesty/{tag}")
    with engine.begin() as conn:
        conn.execute(INSERT_RAW_RECORD, raw_record_params("crm", tag, GENERATION))
        conn.execute(
            text(
                "INSERT INTO entity_links (canonical_id, source_id, source_key, source_ref, "
                "method, generation) VALUES (:c, 'crm', :k, :r, 'L1', :g)"
            ),
            {"c": canonical_id, "k": tag, "r": f"crm:contact:{tag}", "g": GENERATION},
        )
        conn.execute(
            text(
                "INSERT INTO entities (canonical_id, entity_type, current) "
                "VALUES (:c, 'person', CAST(:v AS jsonb))"
            ),
            {"c": canonical_id, "v": current},
        )
        conflict_id = conn.execute(
            text(
                "INSERT INTO conflicts (fingerprint, type, entity_refs, sources, "
                "disagreeing_fields, first_seen_run, last_seen_run) "
                "VALUES (:fp, 'field-disagreement', '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, "
                ":run, :run) RETURNING id"
            ),
            {"fp": tag, "run": TEST_TAG},
        ).scalar_one()
    return World(engine=engine, canonical_id=canonical_id, conflict_id=conflict_id, tag=tag)


def _tear_down(world: World) -> None:
    with world.engine.begin() as conn:
        conn.execute(
            text("DELETE FROM proposal_events WHERE canonical_id = :c"),
            {"c": world.canonical_id},
        )
        conn.execute(text("DELETE FROM proposals WHERE conflict_id = :c"), {"c": world.conflict_id})
        conn.execute(text("DELETE FROM conflicts WHERE id = :c"), {"c": world.conflict_id})
        conn.execute(
            text("DELETE FROM entities WHERE canonical_id = :c"), {"c": world.canonical_id}
        )
        conn.execute(
            text("DELETE FROM entity_links WHERE canonical_id = :c"), {"c": world.canonical_id}
        )
        conn.execute(
            text("DELETE FROM raw_records WHERE generation = :g AND natural_key = :k"),
            {"g": GENERATION, "k": world.tag},
        )


@pytest.fixture
def world(owner_engine: Engine) -> Iterator[World]:
    """A fresh committed entity holding ``{"grade": "4"}``."""
    built = _new_world(owner_engine, '{"grade": "4"}')
    yield built
    _tear_down(built)


# ===========================================================================
# BLOCKER -- the demonstrated attack, both legs
# ===========================================================================
def _forge_applied_event(
    proposal_id: int, canonical_id: uuid.UUID, before: str, after: str
) -> None:
    """The forgery, executed literally: a ledger row and NO canonical UPDATE."""
    with role_connection(ROLE_APPLY_WRITER, commit=False) as conn:
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": proposal_id,
                "cid": canonical_id,
                "event": "applied",
                "before": before,
                "after": after,
            },
        )
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


FORGED_BEFORE = '{"grade": "ATTACKER-CHOSEN", "crm.contact.email": "pwned@example.invalid"}'


def test_an_applied_event_with_no_canonical_write_behind_it_is_refused(world: World) -> None:
    """Leg one of the demonstrated attack, run verbatim.

    ``apply_writer`` inserts an ``applied`` event whose ``before`` is whatever it
    likes and performs no canonical UPDATE at all. Under 0007 this committed:
    every rule was on ``entities``, and ``entities`` was never touched.

    ``KS011`` is the assertion because exactly one trigger in the schema produces
    it. Asserting ``KS001`` here would stay green if RULING 15 were deleted and
    some unrelated clause happened to refuse.
    """
    proposal_id = world.proposal("forgery", '{"grade": "6"}')

    with pytest.raises(DBAPIError) as excinfo:
        _forge_applied_event(
            proposal_id, world.canonical_id, before=FORGED_BEFORE, after='{"grade": "4"}'
        )
    assert_sqlstate(excinfo.value, EVENT_DESCRIBES_NO_WRITE)

    assert world.current() == '{"grade": "4"}', "the forgery must have moved nothing"
    assert world.events(proposal_id) == [], "no forged event may survive the transaction"


def test_the_forgery_cannot_hide_behind_an_honest_after(world: World) -> None:
    """The detail that decides the design, pinned so it cannot be lost.

    The obvious fix -- "at end of transaction the cited entity must hold the
    event's ``after``" -- does not catch this attack, because the attacker sets
    ``after`` to the value the row already holds and forges only ``before``.
    That is exactly what the reproduction did, and it is what this case asserts:
    an ``after`` that matches reality perfectly still buys nothing, because the
    rule is about the **write**, not the value.
    """
    proposal_id = world.proposal("honest-after", '{"grade": "6"}')
    held = world.current()

    with pytest.raises(DBAPIError) as excinfo:
        _forge_applied_event(proposal_id, world.canonical_id, before=FORGED_BEFORE, after=held)
    assert_sqlstate(excinfo.value, EVENT_DESCRIBES_NO_WRITE)
    assert world.current() == held


def test_the_rollback_the_forgery_was_for_is_refused_too(world: World) -> None:
    """Leg two: the whole attack chain, end to end, on committed transactions.

    Both sides of the fix are load-bearing and this runs both. The forgery is
    refused (``KS011``), so no ``applied`` event exists. The attacker then does
    the one thing still open to it -- move the proposal ``approved -> applied``
    in its own transaction, which the transition graph permits -- and attempts
    the reversal that would have written its chosen ``before`` into the
    canonical row. That is refused with ``KS001``: the reversal arm requires an
    ``applied`` event of this proposal to restore from, and the forgery never
    became one.

    Each leg carries its own SQLSTATE, and neither is the other's.
    """
    proposal_id = world.proposal("chain", '{"grade": "6"}')

    with pytest.raises(DBAPIError) as forgery:
        _forge_applied_event(
            proposal_id, world.canonical_id, before=FORGED_BEFORE, after='{"grade": "4"}'
        )
    assert_sqlstate(forgery.value, EVENT_DESCRIBES_NO_WRITE)

    with role_connection(ROLE_APPLY_WRITER) as conn:  # legal move, committed
        conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "applied"})
    assert world.status(proposal_id) == "applied"

    with (
        pytest.raises(DBAPIError) as rollback,
        role_connection(ROLE_APPLY_WRITER, commit=False) as conn,
    ):
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": proposal_id,
                "cid": world.canonical_id,
                "event": "rolled_back",
                "before": '{"grade": "4"}',
                "after": FORGED_BEFORE,
            },
        )
        conn.execute(text(REWRITE), {"cid": world.canonical_id, "after": FORGED_BEFORE})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(rollback.value, CANONICAL_WRITE_UNAUTHORISED)

    assert world.current() == '{"grade": "4"}', "the attack must have moved nothing"


def test_a_real_apply_still_writes_its_event_and_its_row(world: World) -> None:
    """The positive control for RULING 15, on the same role and path.

    If the rule were implemented as "refuse ledger rows", this is the test that
    goes red. A boundary that blocks the legitimate path is a failure, not a
    pass -- and this is the exact shape the product's apply path uses: ledger
    row first, canonical UPDATE second, status move third, one transaction.
    """
    proposal_id = world.proposal("legitimate", '{"grade": "6"}')

    world.apply(proposal_id, before='{"grade": "4"}', after='{"grade": "6"}')

    assert world.current() == '{"grade": "6"}'
    assert world.status(proposal_id) == "applied"
    assert world.events(proposal_id) == ["applied"]


def test_an_event_naming_no_canonical_row_is_refused(world: World) -> None:
    """``canonical_id`` is nullable, and a NULL one describes nothing.

    An ``applied`` event with no subject cannot be checked against any row, so
    RULING 15 would be partial without this clause: the ledger would carry rows
    claiming a canonical write that name nothing.

    ``noted`` is the control, because it is the ledger's one non-authorising
    word and is allowed to carry no canonical row (0006, RULING 8).
    """
    proposal_id = world.proposal("no-subject", '{"grade": "6"}')

    with (
        pytest.raises(DBAPIError) as excinfo,
        role_connection(ROLE_APPLY_WRITER, commit=False) as conn,
    ):
        conn.execute(
            text(
                "INSERT INTO proposal_events (proposal_id, canonical_id, event, before, "
                "after, actor) VALUES (:pid, NULL, 'applied', '{}'::jsonb, '{}'::jsonb, "
                "'system:apply')"
            ),
            {"pid": proposal_id},
        )
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, EVENT_DESCRIBES_NO_WRITE)

    with role_connection(ROLE_APPLY_WRITER, commit=False) as conn:  # control: a note
        conn.execute(
            text(
                "INSERT INTO proposal_events (proposal_id, canonical_id, event, before, "
                "after, actor) VALUES (:pid, NULL, 'noted', '{}'::jsonb, '{}'::jsonb, "
                "'system:apply')"
            ),
            {"pid": proposal_id},
        )
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_an_event_naming_a_canonical_row_that_does_not_exist_is_refused(world: World) -> None:
    """The other total-ness clause: the row named has to be there."""
    proposal_id = world.proposal("ghost", '{"grade": "6"}')
    ghost = uuid.uuid5(uuid.NAMESPACE_URL, f"keystone/tests/schema/honesty/ghost/{world.tag}")

    with pytest.raises(DBAPIError) as excinfo:
        _forge_applied_event(proposal_id, ghost, before="{}", after='{"grade": "6"}')
    assert_sqlstate(excinfo.value, EVENT_DESCRIBES_NO_WRITE)


# ===========================================================================
# RULING 16 -- one canonical-mutating event per entity per transaction
# ===========================================================================
def test_a_decoy_write_cannot_cover_for_a_forged_event(world: World) -> None:
    """Why RULING 15 alone is not enough, executed as the attack it prevents.

    RULING 15 asks "did this transaction write that row?". Without RULING 16 the
    answer can be made YES by a decoy: perform one *genuine* apply of the entity
    citing proposal A, and in the same transaction insert a forged ``applied``
    event for proposal B naming the same entity with an arbitrary ``before``.
    The row then carries this transaction's xmin, RULING 15 passes, and B's
    forged event commits -- ready to authorise an arbitrary reversal later.

    ``uq_proposal_events_canonical_write_once`` refuses it at the index, at
    statement time, so two backends cannot both pass a check and then both
    insert. The SQLSTATE is the unique violation and the index name is asserted
    with it, so this cannot be satisfied by some *other* unique index firing.
    """
    honest = world.proposal("decoy-honest", '{"grade": "6"}')
    forged = world.proposal("decoy-forged", '{"grade": "6"}')

    with (
        pytest.raises(DBAPIError) as excinfo,
        role_connection(ROLE_APPLY_WRITER, commit=False) as conn,
    ):
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": honest,
                "cid": world.canonical_id,
                "event": "applied",
                "before": '{"grade": "4"}',
                "after": '{"grade": "6"}',
            },
        )
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": forged,
                "cid": world.canonical_id,
                "event": "applied",
                "before": FORGED_BEFORE,
                "after": '{"grade": "6"}',
            },
        )
        conn.execute(text(REWRITE), {"cid": world.canonical_id, "after": '{"grade": "6"}'})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, UNIQUE_VIOLATION)
    assert "uq_proposal_events_canonical_write_once" in str(excinfo.value.orig)

    assert world.current() == '{"grade": "4"}', "the decoy transaction must have moved nothing"


def test_a_reversal_cannot_cover_for_a_forged_apply_in_the_same_transaction(
    world: World,
) -> None:
    """The mixed pair, which is the version of the decoy that is easiest to miss.

    RULING 16's index is keyed on ``event IN ('applied','rolled_back')`` rather
    than on ``applied`` alone, and this is why. A ``rolled_back`` event
    authorises a canonical UPDATE just as an ``applied`` one does, so a genuine
    reversal in the transaction would give the row this transaction's xmin and
    satisfy RULING 15 on behalf of a forged ``applied`` event sitting beside it.
    An index scoped to ``applied`` would leave that open.
    """
    forged = world.proposal("mixed-forged", '{"grade": "6"}')
    reversed_one = world.proposal("mixed-reversal", '{"grade": "6"}')

    with (
        pytest.raises(DBAPIError) as excinfo,
        role_connection(ROLE_APPLY_WRITER, commit=False) as conn,
    ):
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": reversed_one,
                "cid": world.canonical_id,
                "event": "rolled_back",
                "before": '{"grade": "6"}',
                "after": '{"grade": "4"}',
            },
        )
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": forged,
                "cid": world.canonical_id,
                "event": "applied",
                "before": FORGED_BEFORE,
                "after": '{"grade": "4"}',
            },
        )
    assert_sqlstate(excinfo.value, UNIQUE_VIOLATION)
    assert "uq_proposal_events_canonical_write_once" in str(excinfo.value.orig)


def test_two_entities_still_apply_in_one_transaction(owner_engine: Engine) -> None:
    """Positive control for RULING 16: the rule is per entity, not per statement.

    A batched apply across *different* canonical rows is legitimate and must
    keep working -- one approved proposal and one ledger event per row, all in
    one transaction. If RULING 16 had been written as "one canonical event per
    transaction" this is the test that goes red.
    """
    first = _new_world(owner_engine, '{"grade": "4"}')
    second = _new_world(owner_engine, '{"grade": "7"}')
    try:
        p_first = first.proposal("batch-a", '{"grade": "6"}')
        p_second = second.proposal("batch-b", '{"grade": "9"}')

        with role_connection(ROLE_APPLY_WRITER) as conn:
            for target, proposal_id, before, after in (
                (first, p_first, '{"grade": "4"}', '{"grade": "6"}'),
                (second, p_second, '{"grade": "7"}', '{"grade": "9"}'),
            ):
                conn.execute(
                    text(INSERT_EVENT),
                    {
                        "pid": proposal_id,
                        "cid": target.canonical_id,
                        "event": "applied",
                        "before": before,
                        "after": after,
                    },
                )
                conn.execute(text(REWRITE), {"cid": target.canonical_id, "after": after})
                conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "applied"})

        assert first.current() == '{"grade": "6"}'
        assert second.current() == '{"grade": "9"}'
    finally:
        _tear_down(second)
        _tear_down(first)


def test_the_canonical_write_once_index_is_a_real_partial_unique_index(
    owner_engine: Engine,
) -> None:
    """Catalog assertion, so "one per transaction" cannot quietly become advisory."""
    with owner_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT x.indisunique, pg_get_expr(x.indpred, x.indrelid) AS pred, "
                "pg_get_indexdef(x.indexrelid) AS definition "
                "FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
                "JOIN pg_class t ON t.oid = x.indrelid "
                "WHERE t.relname = 'proposal_events' "
                "AND i.relname = 'uq_proposal_events_canonical_write_once'"
            )
        ).one_or_none()
    assert row is not None, "the per-transaction uniqueness index does not exist"
    assert row.indisunique is True, "the index is not UNIQUE, so it enforces nothing"
    assert "applied" in row.pred and "rolled_back" in row.pred, row.pred
    assert "canonical_id" in row.definition, row.definition
    assert "txid" in row.definition, row.definition


# ===========================================================================
# RULING 17 -- a reversal may only undo the write that is on top
# ===========================================================================
def test_a_reversal_cannot_undo_a_write_that_is_no_longer_on_top(world: World) -> None:
    """The second hole the reversal arm had, and the clause that closes it.

    Apply P1 (``4 -> 6``). Apply P2 (``6 -> 9``). Now roll back P1. Under 0007
    that was authorised: the arm asked only whether P1's ``applied`` event had
    captured the value being written back, and it had. The canonical row went to
    ``4``, silently discarding P2 -- an approved, applied, never-reversed write.

    ``KS012`` is producible by exactly one clause, ``ap.after = OLD.current``.
    Asserting ``KS001`` here would stay green if that clause were deleted and
    some other clause happened to refuse.
    """
    p_first = world.proposal("stack-1", '{"grade": "6"}')
    p_second = world.proposal("stack-2", '{"grade": "9"}')
    world.apply(p_first, before='{"grade": "4"}', after='{"grade": "6"}')
    world.apply(p_second, before='{"grade": "6"}', after='{"grade": "9"}')
    assert world.current() == '{"grade": "9"}'

    with (
        pytest.raises(DBAPIError) as excinfo,
        role_connection(ROLE_APPLY_WRITER, commit=False) as conn,
    ):
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": p_first,
                "cid": world.canonical_id,
                "event": "rolled_back",
                "before": '{"grade": "9"}',
                "after": '{"grade": "4"}',
            },
        )
        conn.execute(text(REWRITE), {"cid": world.canonical_id, "after": '{"grade": "4"}'})
        conn.execute(text(SET_STATUS), {"pid": p_first, "s": "rolled_back"})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, REVERSAL_NOT_ON_TOP)

    assert world.current() == '{"grade": "9"}', "the stale reversal must have moved nothing"
    assert world.status(p_second) == "applied", "the later approved write must survive"


def test_the_write_on_top_is_still_reversible(world: World) -> None:
    """Positive control for RULING 17: reversals still work, in stack order.

    Roll back P2 first -- it is on top -- and then P1 becomes reversible in its
    turn. If RULING 17 had been written as "reversals are refused once a second
    apply exists", both halves of this go red.
    """
    p_first = world.proposal("order-1", '{"grade": "6"}')
    p_second = world.proposal("order-2", '{"grade": "9"}')
    world.apply(p_first, before='{"grade": "4"}', after='{"grade": "6"}')
    world.apply(p_second, before='{"grade": "6"}', after='{"grade": "9"}')

    world.roll_back(p_second, before='{"grade": "9"}', after='{"grade": "6"}')
    assert world.current() == '{"grade": "6"}'

    world.roll_back(p_first, before='{"grade": "6"}', after='{"grade": "4"}')
    assert world.current() == '{"grade": "4"}'
    assert world.status(p_first) == "rolled_back"
    assert world.status(p_second) == "rolled_back"


def test_a_reversal_to_a_value_the_apply_never_captured_is_still_ks001(world: World) -> None:
    """The two reversal clauses are two codes, and cannot be confused.

    0006's clause -- the value written back must be one this proposal's apply
    captured in ``before`` -- keeps raising ``KS001``, which is what the tests
    written for it assert. ``KS012`` is scoped to the clause 0008 adds. If the
    trigger reported ``KS012`` for every reversal it refused, the assertion
    above would be satisfied by any refusal at all and would prove nothing.
    """
    proposal_id = world.proposal("invented-reversal", '{"grade": "6"}')
    world.apply(proposal_id, before='{"grade": "4"}', after='{"grade": "6"}')

    with (
        pytest.raises(DBAPIError) as excinfo,
        role_connection(ROLE_APPLY_WRITER, commit=False) as conn,
    ):
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": proposal_id,
                "cid": world.canonical_id,
                "event": "rolled_back",
                "before": '{"grade": "6"}',
                "after": '{"grade": "invented"}',
            },
        )
        conn.execute(text(REWRITE), {"cid": world.canonical_id, "after": '{"grade": "invented"}'})
        conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "rolled_back"})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED)


# ===========================================================================
# MINOR 18 -- jsonb equality is not textual equality
# ===========================================================================
def test_an_approved_number_cannot_land_at_a_different_scale(world: World) -> None:
    """``'{"amount": 1}'::jsonb = '{"amount": 1.0}'::jsonb`` is TRUE in Postgres.

    Their ``::text`` renderings are not, because jsonb keeps a numeric's scale.
    So an approval for ``1`` could land as ``1.0`` or ``1.000`` with every
    constraint satisfied -- and ``recon.suite.mirror`` hashes ``md5(t::text)``,
    with determinism graded, so two runs of the same approved action could
    produce different hashes while the write boundary called both correct.

    The negative is ``KS010``, which only the content comparison produces; the
    control writes the approved scale and asserts the stored **text**, because
    asserting the parsed value would be satisfied by exactly the bug.
    """
    p_wrong_scale = world.proposal("scale", '{"amount": 1}')

    with (
        pytest.raises(DBAPIError) as excinfo,
        role_connection(ROLE_APPLY_WRITER, commit=False) as conn,
    ):
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": p_wrong_scale,
                "cid": world.canonical_id,
                "event": "applied",
                "before": '{"grade": "4"}',
                "after": '{"grade": "4", "amount": 1.000}',
            },
        )
        conn.execute(
            text(REWRITE), {"cid": world.canonical_id, "after": '{"grade": "4", "amount": 1.000}'}
        )
        conn.execute(text(SET_STATUS), {"pid": p_wrong_scale, "s": "applied"})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CONTENT_NOT_APPROVED)

    world.apply(
        p_wrong_scale, before='{"grade": "4"}', after='{"grade": "4", "amount": 1}'
    )  # control
    assert world.current() == '{"grade": "4", "amount": 1}', (
        "the stored TEXT must be the approved scale, not merely an equal number"
    )


def test_the_ledger_cannot_report_a_write_at_a_different_scale(world: World) -> None:
    """The same rule on the correlation clauses, not only on the content one.

    ``pe.after = NEW.current`` was jsonb equality too, so the ledger could record
    ``1.0`` for a write of ``1``. The reversal leg reads that recorded text back,
    and the mirror digest hashes it, so an honest-looking ledger at the wrong
    scale is the same determinism bug one table over.
    """
    proposal_id = world.proposal("ledger-scale", '{"amount": 1}')

    with (
        pytest.raises(DBAPIError) as excinfo,
        role_connection(ROLE_APPLY_WRITER, commit=False) as conn,
    ):
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": proposal_id,
                "cid": world.canonical_id,
                "event": "applied",
                "before": '{"grade": "4"}',
                "after": '{"grade": "4", "amount": 1.0}',
            },
        )
        conn.execute(
            text(REWRITE), {"cid": world.canonical_id, "after": '{"grade": "4", "amount": 1}'}
        )
        conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "applied"})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED)


# ===========================================================================
# MINOR 19 -- entities.current is a JSON object
# ===========================================================================
INSERT_ENTITY = (
    "INSERT INTO entities (canonical_id, entity_type, current) "
    "VALUES (:c, 'person', CAST(:v AS jsonb))"
)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("[1, 2]", id="an-array-the-merge-would-append-to"),
        pytest.param('"a string"', id="a-bare-string"),
        pytest.param("42", id="a-number"),
        pytest.param("null", id="json-null"),
        pytest.param("true", id="a-boolean"),
    ],
)
def test_a_canonical_value_that_is_not_an_object_is_refused(
    role_txn: RoleTxn, world: World, value: str
) -> None:
    """``OLD.current || '{}'::jsonb`` is only a no-op on an object.

    ``'[1,2]'::jsonb || '{}'::jsonb`` is ``[1, 2, {}]``, so on a non-object the
    evidence-only action of contract §6 would *append* rather than change
    nothing -- the documented meaning of that action would simply be wrong. The
    CHECK makes the merge in RULING 14 total.
    """
    ghost = uuid.uuid5(uuid.NAMESPACE_URL, f"keystone/tests/schema/honesty/shape/{value}")
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text(INSERT_ENTITY), {"c": ghost, "v": value})
    assert_sqlstate(excinfo.value, CHECK_VIOLATION)
    assert "ck_entities_current_is_object" in str(excinfo.value.orig)


def test_the_object_rule_binds_the_schema_owner_too(owner_engine: Engine) -> None:
    """A table CHECK, so no principal holds a shape the merge cannot handle."""
    ghost = uuid.uuid5(uuid.NAMESPACE_URL, "keystone/tests/schema/honesty/shape/owner")
    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(INSERT_ENTITY), {"c": ghost, "v": "[1, 2]"})
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, CHECK_VIOLATION)


def test_the_object_constraint_is_validated(owner_engine: Engine) -> None:
    """A ``NOT VALID`` constraint would leave every pre-0008 row outside the rule."""
    with owner_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT contype, convalidated FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = 'entities' AND c.conname = 'ck_entities_current_is_object'"
            )
        ).one_or_none()
    assert row is not None, "the object constraint does not exist"
    assert row.contype == "c", row
    assert row.convalidated is True, "the constraint is NOT VALID: old rows escape it"


def test_an_object_canonical_value_still_lands(role_txn: RoleTxn, world: World) -> None:
    """Positive control: the shape entity resolution actually produces."""
    ghost = uuid.uuid5(uuid.NAMESPACE_URL, f"keystone/tests/schema/honesty/shape/ok/{world.tag}")
    with role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(INSERT_RAW_RECORD, raw_record_params("crm", f"{world.tag}-ok", GENERATION))
        conn.execute(text(INSERT_ENTITY), {"c": ghost, "v": '{"crm.contact.grade": "4"}'})
        conn.execute(
            text(
                "INSERT INTO entity_links (canonical_id, source_id, source_key, source_ref, "
                "method, generation) VALUES (:c, 'crm', :k, :r, 'L1', :g)"
            ),
            {
                "c": ghost,
                "k": f"{world.tag}-ok",
                "r": f"crm:contact:{world.tag}-ok",
                "g": GENERATION,
            },
        )
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


# ===========================================================================
# MINOR 20 -- the diagnostics name paths, never values
# ===========================================================================
APPROVED_EMAIL = "approved-address@example.invalid"
ATTEMPTED_EMAIL = "attacker-address@example.invalid"


def test_the_ks010_diagnostic_names_the_path_and_never_the_value(world: World) -> None:
    """A refusal must not leak the record it is refusing.

    0007's ``KS010`` message embedded the cited action, the authorised result and
    the attempted value -- the whole canonical record, which carries
    ``crm.contact.email``, legal names and ``dob``. That string is returned to
    the client *and* written to the Postgres server log, so a refused write
    published the personal data of the person it was about, twice.

    The message must stay useful, so the path is asserted present as firmly as
    the values are asserted absent.
    """
    proposal_id = world.proposal("privacy", f'{{"crm.contact.email": "{APPROVED_EMAIL}"}}')

    with (
        pytest.raises(DBAPIError) as excinfo,
        role_connection(ROLE_APPLY_WRITER, commit=False) as conn,
    ):
        attempted = f'{{"grade": "4", "crm.contact.email": "{ATTEMPTED_EMAIL}"}}'
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": proposal_id,
                "cid": world.canonical_id,
                "event": "applied",
                "before": '{"grade": "4"}',
                "after": attempted,
            },
        )
        conn.execute(text(REWRITE), {"cid": world.canonical_id, "after": attempted})
        conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "applied"})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CONTENT_NOT_APPROVED)

    message = str(excinfo.value.orig)
    assert "crm.contact.email" in message, "the diagnostic must still say WHICH path differs"
    assert str(proposal_id) in message, "the diagnostic must still name the cited proposal"
    for leaked in (APPROVED_EMAIL, ATTEMPTED_EMAIL):
        assert leaked not in message, f"the refusal leaked a canonical value: {leaked}"


def test_the_ks012_diagnostic_names_the_path_and_never_the_value(world: World) -> None:
    """The reversal diagnostic is built the same way from the start."""
    p_first = world.proposal("privacy-1", f'{{"crm.contact.email": "{APPROVED_EMAIL}"}}')
    p_second = world.proposal("privacy-2", f'{{"crm.contact.email": "{ATTEMPTED_EMAIL}"}}')
    first_result = f'{{"grade": "4", "crm.contact.email": "{APPROVED_EMAIL}"}}'
    second_result = f'{{"grade": "4", "crm.contact.email": "{ATTEMPTED_EMAIL}"}}'
    world.apply(p_first, before='{"grade": "4"}', after=first_result)
    world.apply(p_second, before=first_result, after=second_result)

    with (
        pytest.raises(DBAPIError) as excinfo,
        role_connection(ROLE_APPLY_WRITER, commit=False) as conn,
    ):
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": p_first,
                "cid": world.canonical_id,
                "event": "rolled_back",
                "before": second_result,
                "after": '{"grade": "4"}',
            },
        )
        conn.execute(text(REWRITE), {"cid": world.canonical_id, "after": '{"grade": "4"}'})
        conn.execute(text(SET_STATUS), {"pid": p_first, "s": "rolled_back"})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, REVERSAL_NOT_ON_TOP)

    message = str(excinfo.value.orig)
    assert "crm.contact.email" in message
    for leaked in (APPROVED_EMAIL, ATTEMPTED_EMAIL):
        assert leaked not in message, f"the refusal leaked a canonical value: {leaked}"


def test_the_ledger_honesty_trigger_is_a_deferred_constraint_trigger(
    owner_engine: Engine,
) -> None:
    """Catalog assertion: DEFERRED, or the pipeline's statement order becomes law.

    The apply path writes its ledger row before its canonical UPDATE. A trigger
    that was not deferred would fire on the INSERT, see no write yet, and refuse
    the legitimate path -- so "deferred" is a correctness property here, not a
    performance one.
    """
    with owner_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT t.tgdeferrable, t.tginitdeferred, t.tgconstraint <> 0 AS is_constraint "
                "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE c.relname = 'proposal_events' "
                "AND t.tgname = 'proposal_events_describe_a_real_write'"
            )
        ).one_or_none()
    assert row is not None, "the ledger-honesty trigger does not exist"
    assert row.is_constraint is True, "not a constraint trigger, so it cannot be deferred"
    assert row.tgdeferrable is True
    assert row.tginitdeferred is True, "not INITIALLY DEFERRED: the apply path would break"


# ===========================================================================
# Concurrency: twelve real backends racing the same approval
# ===========================================================================
def _race_one_apply(dsn: str, barrier: Barrier, proposal_id: int, canonical_id: str) -> str:
    """One backend's whole apply, released with the others. Returns the outcome."""
    try:
        with psycopg.connect(dsn, autocommit=False) as conn:
            barrier.wait(timeout=30)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO proposal_events (proposal_id, canonical_id, event, "
                        "before, after, actor) VALUES (%s, %s, 'applied', "
                        '\'{"grade": "4"}\'::jsonb, \'{"grade": "6"}\'::jsonb, '
                        "'system:apply')",
                        (proposal_id, canonical_id),
                    )
                    cur.execute(
                        'UPDATE entities SET current = \'{"grade": "6"}\'::jsonb, '
                        "updated_at = now() WHERE canonical_id = %s",
                        (canonical_id,),
                    )
                    cur.execute(
                        "UPDATE proposals SET status = 'applied' WHERE id = %s", (proposal_id,)
                    )
                conn.commit()
            except psycopg.Error as exc:
                conn.rollback()
                return exc.sqlstate or "unknown"
            return "applied"
    except Exception as exc:  # pragma: no cover - diagnostic path only
        return f"error:{type(exc).__name__}:{exc}"


def test_twelve_backends_racing_one_approval_produce_exactly_one_write(world: World) -> None:
    """One approval, one canonical write, under real concurrency -- not a simulation.

    Twelve independent psycopg backends, released together by a barrier, each
    running the complete apply of the *same* approved proposal. The rules under
    test are enforced by an index and by triggers that read committed state, so
    the interesting question is whether two backends can both pass and both
    write -- which is exactly what a check-then-act implementation would allow.

    Three-sided on purpose:

    * exactly one apply lands -- not "at most one", because refusing them all
      would be a dead product that still looked safe;
    * every refusal carries a code the boundary actually produces, so a
      deadlock, a dropped connection or a serialization failure cannot
      masquerade as the rule holding;
    * the canonical row holds exactly the approved content afterwards, and the
      ledger holds exactly one ``applied`` event.
    """
    proposal_id = world.proposal("race", '{"grade": "6"}')
    contenders = 12
    dsn = psycopg_dsn(ROLE_APPLY_WRITER)
    barrier = Barrier(contenders)

    with ThreadPoolExecutor(max_workers=contenders) as pool:
        outcomes = list(
            pool.map(
                lambda _: _race_one_apply(dsn, barrier, proposal_id, str(world.canonical_id)),
                range(contenders),
            )
        )

    applied = [outcome for outcome in outcomes if outcome == "applied"]
    refused = [outcome for outcome in outcomes if outcome != "applied"]
    assert len(applied) == 1, f"expected exactly one apply to land, got {outcomes}"
    assert set(refused) <= {UNIQUE_VIOLATION, CANONICAL_WRITE_UNAUTHORISED, "40001", "40P01"}, (
        f"a refusal came from something other than the write boundary: {outcomes}"
    )
    assert set(refused) & {UNIQUE_VIOLATION, CANONICAL_WRITE_UNAUTHORISED}, (
        f"no refusal came from the boundary at all: {outcomes}"
    )

    assert world.current() == '{"grade": "6"}'
    assert world.status(proposal_id) == "applied"
    assert world.events(proposal_id) == ["applied"], "one approval, one ledger event"
