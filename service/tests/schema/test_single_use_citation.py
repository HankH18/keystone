"""Round four: an approval is spent, not held; and pg_temp is not a code store.

Round three closed eight of ten demonstrated exploits. This module proves the
two blockers and four lesser findings that survived it are closed, in the same
shape the previous rounds use:

* every negative asserts an **exact SQLSTATE** -- ``42501`` for a privilege,
  ``23505``/``23514`` for an index or a check, or one of the project codes
  ``KS001``-``KS009``, none of which an unrelated failure can produce;
* every negative is paired with a **positive control** doing, over the same
  connection path and as the same role, the legitimate thing the fix must not
  have broken;
* the attacks are run the way the red team ran them, against **committed**
  state, not simulated inside one rolled-back transaction.

The headline property, stated once: *one human approval authorises exactly one
canonical write, and its reversal.* Migration 0005 did not have it -- an
``applied`` proposal authorised an unbounded series of further rewrites of its
target entity, forever. Two independent mechanisms now make a citation
single-use, and each is proved on its own below:

1. the partial UNIQUE indexes, ``UNIQUE(proposal_id) WHERE event = 'applied'``
   and the same for ``rolled_back``, so the authorising row cannot be written
   twice; and
2. the event/status pairing in ``keystone_require_proposal_event``, so a
   proposal that was applied in an *earlier* transaction authorises nothing at
   all -- proved separately, on a proposal for which no ``applied`` event
   exists, so the index cannot be the thing doing the refusing.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from recon.db import (
    ROLE_APPLY_WRITER,
    ROLE_RECON_WRITER,
    ROLE_REVIEW_WRITER,
    role_connection,
)
from recon.suite.mirror import mirror_digest
from tests.schema.conftest import (
    INSERT_RAW_RECORD,
    TEST_TAG,
    RoleTxn,
    assert_insufficient_privilege,
    assert_sqlstate,
    raw_record_params,
)

CANONICAL_WRITE_UNAUTHORISED = "KS001"
ACTOR_OUT_OF_SCOPE = "KS003"
ILLEGAL_STATUS_TRANSITION = "KS004"
LINK_WITHOUT_INGESTED_RECORD = "KS009"
UNIQUE_VIOLATION = "23505"
CHECK_VIOLATION = "23514"

#: This module's generation, so its landing rows never collide with the
#: fixtures in conftest (99, 98) or the provenance tests (97, 96).
GENERATION = 95

INSERT_EVENT = (
    "INSERT INTO proposal_events (proposal_id, canonical_id, event, before, after, actor) "
    "VALUES (:pid, :cid, :event, CAST(:before AS jsonb), CAST(:after AS jsonb), 'system:apply')"
)
REWRITE = (
    "UPDATE entities SET current = CAST(:after AS jsonb), updated_at = now() "
    "WHERE canonical_id = :cid"
)
SET_STATUS = "UPDATE proposals SET status = CAST(:s AS proposal_status) WHERE id = :pid"


# ===========================================================================
# fixtures -- committed state, because a replay is a second transaction
# ===========================================================================
@dataclass
class World:
    """One committed entity with provenance, plus a proposal factory.

    Everything here is COMMITTED. The blocker this module exists for is a
    *replay*: a second transaction reusing a citation the first one spent, so an
    attack staged inside a single rolled-back transaction would prove nothing.
    """

    engine: Engine
    canonical_id: uuid.UUID
    conflict_id: int
    tag: str
    proposals: list[int] = field(default_factory=list)

    def proposal(
        self, label: str, *, approve: bool = True, sets: dict[str, object] | None = None
    ) -> int:
        """Commit a proposal targeting this entity, decided by a real reviewer.

        The approval goes over a ``review_writer`` connection rather than an
        owner UPDATE: manufacturing an approval as the owner would sidestep the
        transition graph these tests are partly about.

        ``sets`` is the content the approval authorises -- migration 0007 binds
        the canonical write to ``OLD.current || action->'set'``, so a proposal
        that does not declare what it writes cannot authorise any write at all.
        Every negative below that is *not* about content passes the same value
        it then attempts to write, so the content rule is never the clause doing
        the refusing and each test still proves the clause it names.
        """
        action = json.dumps({"set": dict(sets or {})}, sort_keys=True)
        with self.engine.begin() as conn:
            proposal_id = conn.execute(
                text(
                    "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, "
                    "evidence, created_run, target_canonical_id) "
                    "VALUES (:cid, :fp, CAST(:action AS jsonb), 0.9, '{}'::jsonb, :run, "
                    ":target) RETURNING id"
                ),
                {
                    "cid": self.conflict_id,
                    "fp": f"{self.tag}-{label}",
                    "run": TEST_TAG,
                    "target": self.canonical_id,
                    "action": action,
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
        every test using it fails loudly rather than passing on a dead path.
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


@pytest.fixture
def world(owner_engine: Engine) -> Iterator[World]:
    """A fresh committed entity, its provenance, and its conflict.

    Function-scoped and uniquely keyed, because most tests here spend the
    entity's one apply and one reversal, and a spent citation is exactly what
    cannot be reused.
    """
    tag = f"{TEST_TAG}-citation-{uuid.uuid4()}"
    canonical_id = uuid.uuid5(uuid.NAMESPACE_URL, f"keystone/tests/schema/citation/{tag}")

    with owner_engine.begin() as conn:
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
                "VALUES (:c, 'person', '{}'::jsonb)"
            ),
            {"c": canonical_id},
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

    created = World(
        engine=owner_engine, canonical_id=canonical_id, conflict_id=conflict_id, tag=tag
    )
    yield created

    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM proposal_events WHERE canonical_id = :c"), {"c": canonical_id}
        )
        conn.execute(text("DELETE FROM proposals WHERE conflict_id = :c"), {"c": conflict_id})
        conn.execute(text("DELETE FROM conflicts WHERE id = :c"), {"c": conflict_id})
        conn.execute(text("DELETE FROM entities WHERE canonical_id = :c"), {"c": canonical_id})
        conn.execute(text("DELETE FROM entity_links WHERE canonical_id = :c"), {"c": canonical_id})
        conn.execute(
            text("DELETE FROM raw_records WHERE generation = :g AND natural_key = :k"),
            {"g": GENERATION, "k": tag},
        )


# ===========================================================================
# BLOCKER 1 -- a citation is consumed exactly once
# ===========================================================================
def test_a_spent_citation_cannot_authorise_a_second_write(world: World) -> None:
    """THE replay, run exactly as the ruling specifies it.

    Legitimately approve and apply a proposal -- committed, over the real roles
    -- and then, in a **new transaction**, attempt a second arbitrary write into
    the same entity citing that same now-applied proposal.

    Under 0005 this succeeded without limit: the cited proposal sat at
    ``applied``, ``applied`` was an authorising status, and nothing recorded
    that the citation had already been used. One human approval was a permanent
    licence to rewrite that entity's canonical state.

    It now fails at the first statement of the replay: the authorising event
    must carry the current transaction id, so a second apply needs a second
    ``applied`` row for that proposal, and ``uq_proposal_events_applied_once``
    permits exactly one for all time.
    """
    proposal_id = world.proposal("replay", sets={"grade": "9"})
    world.apply(proposal_id, before="{}", after='{"grade": "9"}')
    assert world.current() == '{"grade": "9"}', "the legitimate apply must really have landed"
    assert world.status(proposal_id) == "applied"

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
                "before": '{"grade": "9"}',
                "after": '{"grade": "stolen"}',
            },
        )
        conn.execute(text(REWRITE), {"cid": world.canonical_id, "after": '{"grade": "stolen"}'})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, UNIQUE_VIOLATION)

    assert world.current() == '{"grade": "9"}', "the replay must have changed nothing"


def test_an_applied_proposal_does_not_authorise_a_further_apply(world: World) -> None:
    """The status layer of the same rule, proved where the index cannot be it.

    This proposal is moved ``approved -> applied`` in its own committed
    transaction and writes **no** canonical row, so no ``applied`` event exists
    for it and the partial unique index has nothing to catch. The only thing
    that can refuse the write is the citation trigger's event/status pairing:
    ``applied`` authorises a rollback, never another apply, unless the
    ``approved -> applied`` move happened in this very transaction.

    Without this half, the ruling's "(a) ``applied`` must NOT authorise a new
    apply" would rest entirely on an index -- one ``DROP INDEX`` from gone.
    """
    proposal_id = world.proposal("status-only", sets={"grade": "stolen"})
    with role_connection(ROLE_APPLY_WRITER) as conn:
        conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "applied"})
    assert world.status(proposal_id) == "applied"

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
                "before": "{}",
                "after": '{"grade": "stolen"}',
            },
        )
        conn.execute(text(REWRITE), {"cid": world.canonical_id, "after": '{"grade": "stolen"}'})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED)


def test_an_approved_proposal_still_authorises_its_one_apply(world: World) -> None:
    """Positive control for both halves above, on the same role and path.

    If either mechanism were implemented as "refuse applies", this is the test
    that goes red -- a boundary that blocks the legitimate path is a failure,
    not a pass.
    """
    proposal_id = world.proposal("legitimate", sets={"grade": "7"})
    world.apply(proposal_id, before="{}", after='{"grade": "7"}')
    assert world.current() == '{"grade": "7"}'
    assert world.status(proposal_id) == "applied"


def test_a_reversal_is_also_consumed_exactly_once(world: World) -> None:
    """One approval buys one apply and one reversal -- then it is spent.

    The rollback below is the real one: it restores the value the ``applied``
    event captured and moves the proposal ``applied -> rolled_back`` in the same
    transaction. The second rollback attempt is refused by
    ``uq_proposal_events_rolled_back_once``.
    """
    proposal_id = world.proposal("reversal", sets={"grade": "9"})
    world.apply(proposal_id, before="{}", after='{"grade": "9"}')

    with role_connection(ROLE_APPLY_WRITER) as conn:  # control: the reversal works
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": proposal_id,
                "cid": world.canonical_id,
                "event": "rolled_back",
                "before": '{"grade": "9"}',
                "after": "{}",
            },
        )
        conn.execute(text(REWRITE), {"cid": world.canonical_id, "after": "{}"})
        conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "rolled_back"})
    assert world.current() == "{}", "the rollback must restore what the apply overwrote"

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
                "before": "{}",
                "after": '{"grade": "stolen"}',
            },
        )
    assert_sqlstate(excinfo.value, UNIQUE_VIOLATION)


def test_a_rollback_must_restore_what_the_apply_overwrote(world: World) -> None:
    """A reversal that writes something new is a second arbitrary write.

    Without this clause, "one approval, one write" was still false by one: the
    reversal leg could put any value at all into the entity so long as it
    reported it honestly. The rule is that ``rolled_back`` is authorised only
    when the proposal's own ``applied`` event captured exactly the value being
    written back.
    """
    proposal_id = world.proposal("dishonest-reversal", sets={"grade": "9"})
    world.apply(proposal_id, before="{}", after='{"grade": "9"}')

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
                "before": '{"grade": "9"}',
                "after": '{"grade": "not-what-was-overwritten"}',
            },
        )
        conn.execute(
            text(REWRITE),
            {"cid": world.canonical_id, "after": '{"grade": "not-what-was-overwritten"}'},
        )
        conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "rolled_back"})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED)

    assert world.current() == '{"grade": "9"}'


@pytest.mark.parametrize(
    ("before", "after", "why"),
    [
        pytest.param('{"grade": "wrong"}', '{"grade": "9"}', "before", id="before-is-a-lie"),
        pytest.param("{}", '{"grade": "misreported"}', "after", id="after-is-a-lie"),
    ],
)
def test_before_and_after_are_still_enforced(
    world: World, before: str, after: str, why: str
) -> None:
    """Ruling (c): the ``before``/``after`` clauses are kept and still bite.

    A citation rule that stopped checking these would let the ledger claim a
    change that never happened, or omit the one that did -- and the rollback
    path is built entirely on ``before``.
    """
    proposal_id = world.proposal(f"ledger-{why}", sets={"grade": "9"})
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
                "before": before,
                "after": after,
            },
        )
        conn.execute(text(REWRITE), {"cid": world.canonical_id, "after": '{"grade": "9"}'})
        conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "applied"})
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, CANONICAL_WRITE_UNAUTHORISED), why


def test_the_single_use_indexes_are_real_partial_unique_indexes(owner_engine: Engine) -> None:
    """Catalog assertion, so "single use" cannot quietly become a non-unique index."""
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT i.relname, x.indisunique, pg_get_expr(x.indpred, x.indrelid) "
                "FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
                "JOIN pg_class t ON t.oid = x.indrelid "
                "WHERE t.relname = 'proposal_events' AND i.relname = ANY(:names)"
            ),
            {
                "names": [
                    "uq_proposal_events_applied_once",
                    "uq_proposal_events_rolled_back_once",
                ]
            },
        ).all()
    found = {name: (unique, predicate) for name, unique, predicate in rows}
    assert set(found) == {
        "uq_proposal_events_applied_once",
        "uq_proposal_events_rolled_back_once",
    }, found
    for name, (unique, predicate) in found.items():
        assert unique is True, f"{name} exists but is not unique -- it enforces nothing"
        assert predicate is not None, f"{name} is not partial: {predicate}"


# ===========================================================================
# BLOCKER 1 (d) -- the event vocabulary is closed
# ===========================================================================
@pytest.mark.parametrize(
    "event",
    [
        pytest.param("approved", id="the-decision-word-the-red-team-forged"),
        pytest.param("rejected", id="the-other-decision-word"),
        pytest.param("pending", id="a-status-masquerading-as-an-event"),
        pytest.param("Applied", id="wrong-case"),
        pytest.param("", id="empty"),
    ],
)
def test_apply_writer_cannot_invent_an_event_label(
    world: World, role_txn: RoleTxn, event: str
) -> None:
    """``apply_writer`` wrote whatever it liked into the reversal ledger.

    Including ``approved`` -- a decision word, in the one table a reader
    consults to find out what happened to a proposal, written by the role that
    is structurally forbidden from deciding anything.
    """
    proposal_id = world.proposal("vocabulary", approve=False)
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": proposal_id,
                "cid": world.canonical_id,
                "event": event,
                "before": "{}",
                "after": "{}",
            },
        )
    assert_sqlstate(excinfo.value, CHECK_VIOLATION)


@pytest.mark.parametrize("event", ["applied", "rolled_back", "noted"])
def test_the_vocabulary_admits_exactly_the_ledger_words(
    world: World, role_txn: RoleTxn, event: str
) -> None:
    """Positive control: the closed set is the ledger's own vocabulary.

    ``noted`` is the only non-authorising label and is deliberately kept -- it
    is what makes "an event label is not an authorisation" provable on its own
    (see ``test_write_boundary_hardening``'s wrong-event case) rather than
    provable only by absence. It is also, pointedly, not a decision word.
    """
    proposal_id = world.proposal("vocabulary-ok", approve=False)
    with role_txn(ROLE_APPLY_WRITER) as conn:
        stored = conn.execute(
            text(INSERT_EVENT + " RETURNING event"),
            {
                "pid": proposal_id,
                "cid": world.canonical_id,
                "event": event,
                "before": "{}",
                "after": "{}",
            },
        ).scalar_one()
    assert stored == event


# ===========================================================================
# MAJOR 4 -- the reversal ledger is actor-scoped
# ===========================================================================
@pytest.mark.parametrize(
    "actor",
    [
        pytest.param("reviewer:alice", id="the-forgery-the-red-team-committed"),
        pytest.param("alice", id="a-bare-name"),
        pytest.param("apply", id="the-old-default"),
        pytest.param("System:apply", id="wrong-case"),
        pytest.param(" system:apply", id="leading-space-defeats-the-anchor"),
    ],
)
def test_apply_writer_cannot_forge_a_human_actor_in_the_reversal_ledger(
    world: World, role_txn: RoleTxn, actor: str
) -> None:
    """RULING 5 scoped ``audit_log`` and stopped there.

    ``proposal_events`` had no equivalent and ``apply_writer``'s column grant
    includes ``actor``, so the automation attributed entries in the reversal
    ledger to a named human. Same rule, both ledgers, now.
    """
    proposal_id = world.proposal("actor", approve=False)
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(
                "INSERT INTO proposal_events (proposal_id, canonical_id, event, before, "
                "after, actor) VALUES (:pid, :cid, 'noted', '{}'::jsonb, '{}'::jsonb, :actor)"
            ),
            {"pid": proposal_id, "cid": world.canonical_id, "actor": actor},
        )
    assert_sqlstate(excinfo.value, ACTOR_OUT_OF_SCOPE)


@pytest.mark.parametrize("actor", ["system:apply", "system:apply/run-7"])
def test_apply_writer_may_write_a_machine_scoped_reversal_actor(
    world: World, role_txn: RoleTxn, actor: str
) -> None:
    """Positive control: the apply path's own actor still lands."""
    proposal_id = world.proposal("actor-ok", approve=False)
    with role_txn(ROLE_APPLY_WRITER) as conn:
        stored = conn.execute(
            text(
                "INSERT INTO proposal_events (proposal_id, canonical_id, event, before, "
                "after, actor) VALUES (:pid, :cid, 'noted', '{}'::jsonb, '{}'::jsonb, :actor) "
                "RETURNING actor"
            ),
            {"pid": proposal_id, "cid": world.canonical_id, "actor": actor},
        ).scalar_one()
    assert stored == actor


def test_the_forged_approval_entry_is_dead_at_both_layers(world: World, role_txn: RoleTxn) -> None:
    """The finding executed literally: ``(event='approved', actor='reviewer:alice')``.

    Two independent rules refuse it now -- the actor scope and the closed
    vocabulary -- so this asserts the actor layer (the BEFORE trigger runs
    first) and the vocabulary layer is asserted on its own above.
    """
    proposal_id = world.proposal("forgery", approve=False)
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(
                "INSERT INTO proposal_events (proposal_id, canonical_id, event, before, "
                "after, actor) VALUES (:pid, :cid, 'approved', '{}'::jsonb, '{}'::jsonb, "
                "'reviewer:alice')"
            ),
            {"pid": proposal_id, "cid": world.canonical_id},
        )
    assert_sqlstate(excinfo.value, ACTOR_OUT_OF_SCOPE)


# ===========================================================================
# MAJOR 3 -- the signature on a decided proposal is frozen
# ===========================================================================
@pytest.mark.parametrize(
    "assignment",
    [
        pytest.param("decided_by = NULL", id="erase-the-name"),
        pytest.param("decided_by = 'reviewer:mallory'", id="rewrite-the-name"),
        pytest.param("decided_at = NULL", id="erase-the-clock"),
        pytest.param("decided_at = now() + interval '1 day'", id="re-date-the-decision"),
    ],
)
def test_review_writer_cannot_rewrite_the_signature_on_a_decided_proposal(
    world: World, role_txn: RoleTxn, assignment: str
) -> None:
    """The exploit, run as the red team ran it.

    ``keystone_proposal_status_transition`` early-returned when the status was
    unchanged, **before** any role check, and ``review_writer`` holds
    ``UPDATE(decided_by, decided_at)`` -- so it could erase or rewrite the
    signature on an already-decided proposal, and ``apply_writer`` would still
    apply it. An approval nobody is attributable for is exactly what the third
    role exists to prevent.
    """
    proposal_id = world.proposal("signature")
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_REVIEW_WRITER) as conn:
        conn.execute(
            text(f"UPDATE proposals SET {assignment} WHERE id = :pid"), {"pid": proposal_id}
        )
    assert_sqlstate(excinfo.value, ILLEGAL_STATUS_TRANSITION)


def test_the_signature_is_frozen_even_on_a_legal_transition(
    owner_engine: Engine, world: World
) -> None:
    """The freeze is not a side effect of the no-op rule below it.

    Here the status genuinely moves ``approved -> applied`` -- a transition the
    graph allows -- while the same statement rewrites ``decided_by``. Only the
    freeze can be refusing this, which is what makes it independently provable.
    Exercised as the **owner** because no role's column grant reaches both
    columns at once, and because a rule that evaporated for the principal a
    careless script connects as would guard nothing.
    """
    proposal_id = world.proposal("frozen-on-move")

    with owner_engine.connect() as conn:  # control: the same move, signature untouched
        transaction = conn.begin()
        moved = conn.execute(
            text(SET_STATUS + " RETURNING status"), {"pid": proposal_id, "s": "applied"}
        ).scalar_one()
        transaction.rollback()
    assert moved == "applied"

    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(
                    "UPDATE proposals SET status = 'applied', decided_by = 'reviewer:mallory' "
                    "WHERE id = :pid"
                ),
                {"pid": proposal_id},
            )
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, ILLEGAL_STATUS_TRANSITION)


def test_a_boundary_role_may_not_update_a_proposal_without_moving_it(
    world: World, role_txn: RoleTxn
) -> None:
    """The role check now runs ahead of the unchanged-status early return.

    Proved where the freeze cannot be the thing refusing: this proposal is
    ``pending`` and its signature is still NULL, so nothing is frozen yet.
    ``review_writer`` signing a proposal it has not decided is refused because
    the three boundary roles hold column grants over the decision and apply
    surface only -- an UPDATE that moves no status can only be an attempt to
    rewrite a decision in place.
    """
    proposal_id = world.proposal("unsigned", approve=False)

    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_REVIEW_WRITER) as conn:
        conn.execute(
            text("UPDATE proposals SET decided_by = 'reviewer:mallory' WHERE id = :pid"),
            {"pid": proposal_id},
        )
    assert_sqlstate(excinfo.value, ILLEGAL_STATUS_TRANSITION)

    with role_txn(ROLE_REVIEW_WRITER) as conn:  # control: deciding it works
        decided = conn.execute(
            text(
                "UPDATE proposals SET status = 'approved', decided_by = 'reviewer:alice', "
                "decided_at = now() WHERE id = :pid RETURNING status, decided_by"
            ),
            {"pid": proposal_id},
        ).one()
    assert (decided.status, decided.decided_by) == ("approved", "reviewer:alice")


# ===========================================================================
# MINOR 6 -- the owner is bound to the transition graph too
# ===========================================================================
def test_the_owner_cannot_jump_a_proposal_straight_to_applied(
    owner_engine: Engine, world: World
) -> None:
    """KS002/KS005/KS008 bind the owner; KS004 did not, and the gap was the finding.

    The owner could move a proposal ``pending -> applied`` in one statement
    naming an arbitrary human decider -- the whole holds-before-writes graph,
    skipped, by the principal a local script connects as.

    Stated plainly, because it should not be oversold: the owner can drop this
    trigger, so this is **defence in depth, not a boundary**. It is here because
    an inconsistency between four rules that bind the owner and one that does
    not is worse than either choice.
    """
    proposal_id = world.proposal("owner-jump", approve=False)
    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(
                text(
                    "UPDATE proposals SET status = 'applied', decided_by = 'reviewer:mallory', "
                    "decided_at = now() WHERE id = :pid"
                ),
                {"pid": proposal_id},
            )
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, ILLEGAL_STATUS_TRANSITION)


def test_the_owner_must_name_a_decider_like_anybody_else(
    owner_engine: Engine, world: World
) -> None:
    """An unsigned decision is indistinguishable from an automated one."""
    proposal_id = world.proposal("owner-unsigned", approve=False)
    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "approved"})
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, ILLEGAL_STATUS_TRANSITION)


def test_the_owner_may_still_walk_the_legal_graph(owner_engine: Engine, world: World) -> None:
    """Positive control: the ops principal keeps a real repair path.

    Without this, "bind the owner" could have been implemented as "the owner may
    not touch proposals", which would leave a stuck proposal unrecoverable.
    """
    proposal_id = world.proposal("owner-legal", approve=False)
    with owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            approved = conn.execute(
                text(
                    "UPDATE proposals SET status = 'approved', decided_by = 'reviewer:alice', "
                    "decided_at = now() WHERE id = :pid RETURNING status"
                ),
                {"pid": proposal_id},
            ).scalar_one()
            applied = conn.execute(
                text(SET_STATUS + " RETURNING status"), {"pid": proposal_id, "s": "applied"}
            ).scalar_one()
            rolled_back = conn.execute(
                text(SET_STATUS + " RETURNING status"), {"pid": proposal_id, "s": "rolled_back"}
            ).scalar_one()
            # An owner UPDATE that is not a transition is still allowed: it
            # holds columns (created_run) whose update moves no status.
            touched = conn.execute(
                text("UPDATE proposals SET created_run = :run WHERE id = :pid RETURNING status"),
                {"pid": proposal_id, "run": f"{TEST_TAG}-repair"},
            ).scalar_one()
        finally:
            transaction.rollback()
    assert [approved, applied, rolled_back, touched] == [
        "approved",
        "applied",
        "rolled_back",
        "rolled_back",
    ]


# ===========================================================================
# MAJOR 5 -- the provenance floor stops being self-satisfiable
# ===========================================================================
LINK = (
    "INSERT INTO entity_links (canonical_id, source_id, source_key, source_ref, method, "
    "generation) VALUES (:cid, 'crm', :key, :ref, 'L1', :g)"
)


def _link_args(tag: str) -> dict[str, object]:
    key = f"{TEST_TAG}-floor-{tag}-{uuid.uuid4()}"
    return {
        "cid": uuid.uuid5(uuid.NAMESPACE_URL, f"keystone/tests/schema/floor/{key}"),
        "key": key,
        "ref": f"crm:contact:{key}",
        "g": GENERATION,
    }


def test_a_link_that_names_no_ingested_record_is_rejected(role_txn: RoleTxn) -> None:
    """0005's provenance floor was self-satisfiable in two INSERTs.

    ``entity_links`` had no reference to anything ingested, so ``recon_writer``
    wrote a link of its own invention and then a canonical row that "descended"
    from it. Every link must now name a ``raw_records`` row with the same
    ``(source_id, natural_key, generation)``.
    """
    args = _link_args("orphan")
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text(LINK), args)
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
    assert_sqlstate(excinfo.value, LINK_WITHOUT_INGESTED_RECORD)


def test_a_link_that_names_an_ingested_record_is_accepted(role_txn: RoleTxn) -> None:
    """Positive control: the real pipeline order, ingest then resolve.

    Deliberately deferred to end of transaction, so ER need not care whether the
    link or the record it names is written first within one transaction -- a
    rule that demanded an order would break the pipeline while looking like a
    boundary.
    """
    args = _link_args("linked")
    with role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text(LINK), args)
        conn.execute(INSERT_RAW_RECORD, raw_record_params("crm", str(args["key"]), GENERATION))
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        stored = conn.execute(
            text("SELECT count(*) FROM entity_links WHERE canonical_id = :cid"),
            {"cid": args["cid"]},
        ).scalar_one()
    assert stored == 1


def test_the_link_provenance_rule_binds_the_schema_owner_too(owner_engine: Engine) -> None:
    """An invariant of the table, not a privilege."""
    args = _link_args("owner")
    with pytest.raises(DBAPIError) as excinfo, owner_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(LINK), args)
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        finally:
            transaction.rollback()
    assert_sqlstate(excinfo.value, LINK_WITHOUT_INGESTED_RECORD)


def test_the_floor_is_a_floor_and_this_test_says_what_it_does_not_prove(
    role_txn: RoleTxn,
) -> None:
    """The honest limit, asserted rather than left to a docstring nobody reads.

    ``recon_writer`` holds INSERT on ``raw_records`` -- it must, ingestion is
    its job -- so fabricating a canonical entity is now **three** INSERTs
    instead of two, not zero. What the rule buys is not impossibility, it is
    that the fabrication must leave a row in the landing table.

    This docstring used to finish that sentence with "which is where
    ``recon.suite``'s mirror-unchanged hash check reads". When it was written,
    ``recon.suite``'s check registry was empty and nothing read it. The control
    now exists (``recon.suite.mirror``, registered as ``mirror-unchanged``) and
    this test no longer takes it on trust: it takes a real mirror digest before
    and after the fabrication and asserts the digest moved and names
    ``raw_records``. What the control does **not** yet do -- assert the mirror
    is unchanged across a *reconciler* run -- fails loudly as not-yet-
    implemented, and is asserted as such in
    ``tests/schema/test_suite_mirror_check.py``.

    This test asserts the limitation on purpose: a red team should find it here
    rather than discover it against a docstring that claimed more.
    """
    args = _link_args("three-inserts")
    with role_connection(ROLE_RECON_WRITER, commit=False) as probe:
        before = mirror_digest(probe)

    with role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(INSERT_RAW_RECORD, raw_record_params("crm", str(args["key"]), GENERATION))
        conn.execute(text(LINK), args)
        conn.execute(
            text(
                "INSERT INTO entities (canonical_id, entity_type, current) "
                "VALUES (:cid, 'person', '{\"grade\": \"invented\"}'::jsonb)"
            ),
            {"cid": args["cid"]},
        )
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        landed = conn.execute(
            text("SELECT count(*) FROM raw_records WHERE natural_key = :k"),
            {"k": args["key"]},
        ).scalar_one()
        # Read the digest inside the fabricating transaction: the point is that
        # the evidence is in the landing table, and this connection is the one
        # that put it there.
        after = mirror_digest(conn)

    assert landed == 1, (
        "the fabrication path still exists and costs three INSERTs; what changed "
        "is that the third one lands in the landing table where the suite reads it"
    )
    assert before.changed_tables(after) == ("raw_records",), (
        "the fabrication must be visible to the mirror digest -- that is the whole "
        "value of forcing it to leave a landing row"
    )


def test_recon_writer_cannot_delete_the_evidence_it_had_to_leave(role_txn: RoleTxn) -> None:
    """The floor would be worthless if the landing row could be swept up after.

    ``recon_writer`` holds DELETE on staging only -- never on ``raw_records`` --
    so the evidence the previous test forces it to leave is evidence it cannot
    remove.
    """
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_RECON_WRITER) as conn:
        conn.execute(text("DELETE FROM raw_records WHERE generation = :g"), {"g": GENERATION})
    assert_insufficient_privilege(excinfo.value)


# ===========================================================================
# The property, end to end, on committed state
# ===========================================================================
def test_one_approval_authorises_one_write_and_one_reversal_and_no_more(
    world: World,
) -> None:
    """The graded property in a single test, run over real committed transactions.

    Approve once. Apply once -- it lands. Roll back once -- it restores. Then
    every further attempt to use that approval, in either direction, is refused.
    """
    proposal_id = world.proposal("the-property", sets={"grade": "9"})

    world.apply(proposal_id, before="{}", after='{"grade": "9"}')
    assert world.current() == '{"grade": "9"}'

    with role_connection(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(INSERT_EVENT),
            {
                "pid": proposal_id,
                "cid": world.canonical_id,
                "event": "rolled_back",
                "before": '{"grade": "9"}',
                "after": "{}",
            },
        )
        conn.execute(text(REWRITE), {"cid": world.canonical_id, "after": "{}"})
        conn.execute(text(SET_STATUS), {"pid": proposal_id, "s": "rolled_back"})
    assert world.current() == "{}"
    assert world.status(proposal_id) == "rolled_back"

    for event, before, after in (
        ("applied", "{}", '{"grade": "again"}'),
        ("rolled_back", "{}", '{"grade": "again"}'),
    ):
        with (
            pytest.raises(DBAPIError) as excinfo,
            role_connection(ROLE_APPLY_WRITER, commit=False) as conn,
        ):
            conn.execute(
                text(INSERT_EVENT),
                {
                    "pid": proposal_id,
                    "cid": world.canonical_id,
                    "event": event,
                    "before": before,
                    "after": after,
                },
            )
            conn.execute(text(REWRITE), {"cid": world.canonical_id, "after": after})
            conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        assert_sqlstate(excinfo.value, UNIQUE_VIOLATION), event

    assert world.current() == "{}", "the spent approval must have moved nothing"


# ===========================================================================
# The citation clock itself -- the column the whole of RULING 7 reads
# ===========================================================================
def test_the_citation_clock_is_not_writable_by_any_deciding_role(owner_engine: Engine) -> None:
    """``status_txid`` decides whether a citation is fresh; nobody may set it.

    ``review_writer`` holds ``UPDATE(status, decided_by, decided_at)`` and
    ``apply_writer`` holds ``UPDATE(status)``, so neither column grant reaches
    it. Read from ``information_schema.column_privileges`` because
    ``role_table_grants`` lists only table-level privileges and would show
    nothing at all here -- a green that proves the opposite of the claim.
    """
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT grantee, privilege_type FROM information_schema.column_privileges "
                "WHERE table_name = 'proposals' AND column_name = 'status_txid' "
                "AND grantee = ANY(:roles)"
            ),
            {"roles": [ROLE_RECON_WRITER, ROLE_REVIEW_WRITER, ROLE_APPLY_WRITER]},
        ).all()
    granted = {(grantee, privilege) for grantee, privilege in rows}
    updates = {pair for pair in granted if pair[1] == "UPDATE"}
    assert updates == set(), f"a role can rewrite the citation clock: {updates}"


def test_apply_writer_cannot_set_the_citation_clock(world: World, role_txn: RoleTxn) -> None:
    """The live half of the assertion above, on the role that would want to.

    Setting ``status_txid`` to the current transaction on an already-applied
    proposal would make a spent citation look fresh again -- the whole of
    RULING 7 in one UPDATE.
    """
    proposal_id = world.proposal("clock")
    with pytest.raises(DBAPIError) as excinfo, role_txn(ROLE_APPLY_WRITER) as conn:
        conn.execute(
            text(
                "UPDATE proposals SET status_txid = pg_current_xact_id()::text::bigint "
                "WHERE id = :pid"
            ),
            {"pid": proposal_id},
        )
    assert_insufficient_privilege(excinfo.value)


def test_a_proposal_is_born_with_no_citation_clock(
    world: World, role_txn: RoleTxn, owner_engine: Engine
) -> None:
    """``recon_writer`` holds table-level INSERT on proposals, so it can *name*
    ``status_txid`` -- and the birth trigger overwrites it with NULL regardless.

    A column the citation rule reads must not be caller-writable at any point in
    the row's life, and "no role's grant reaches it" is not true at INSERT.
    """
    with role_txn(ROLE_RECON_WRITER) as conn:
        stored = conn.execute(
            text(
                "INSERT INTO proposals (conflict_id, fingerprint, action, confidence, "
                "evidence, created_run, target_canonical_id, status_txid) "
                "VALUES (:cid, :fp, '{\"set\": {}}'::jsonb, 0.5, '{}'::jsonb, :run, "
                ":target, pg_current_xact_id()::text::bigint) RETURNING status_txid"
            ),
            {
                "cid": world.conflict_id,
                "fp": f"{world.tag}-born-clock",
                "run": TEST_TAG,
                "target": world.canonical_id,
            },
        ).scalar_one()
    assert stored is None, "a newborn proposal must carry no citation clock"


def test_the_citation_clock_advances_only_on_a_real_transition(world: World) -> None:
    """Positive control: the clock the rule reads is actually maintained.

    If it were never stamped, every citation would look stale and the apply path
    would be dead -- which is why this control exists next to the negatives.
    """
    proposal_id = world.proposal("clock-advances", sets={"grade": "9"})
    with world.engine.connect() as conn:
        after_decision = conn.execute(
            text("SELECT status_txid FROM proposals WHERE id = :p"), {"p": proposal_id}
        ).scalar_one()
    assert after_decision is not None, "the decision must have stamped the clock"

    world.apply(proposal_id, before="{}", after='{"grade": "9"}')
    with world.engine.connect() as conn:
        after_apply = conn.execute(
            text("SELECT status_txid FROM proposals WHERE id = :p"), {"p": proposal_id}
        ).scalar_one()
    assert after_apply > after_decision, "the apply must have stamped a later transaction"
