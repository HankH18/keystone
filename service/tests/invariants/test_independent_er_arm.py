"""A hand-asserted answer key for "which source records are the same human".

Why this file exists
--------------------
The join-check headline ("25/25") and the golden conflict result are both graded
against `golden/expected-views.json`, and that file is *written by* the cascade it
grades: `recon/seed/run.py` resolves the snapshot with `recon.er.resolve` and hands
the result to `recon.seed.golden.build_golden`. Every one of the 25 entries agrees
with the detector because both descend from one run of one cascade. Nothing in the
repository states, independently of that code, which records belong to the same
person.

This file states it. Twenty groupings are written out literally below -- person key,
anchor, and the exact set of source refs -- derived by hand from the raw
generation-3 fixture JSONL against the normative rules in
`docs/invariant-contract.md` SS4. They are checked against the **persisted** entity
layer (`entities.current`, `entity_links`) that `recon.resolve.materialize` wrote,
so a cascade that starts merging two children of one household, or drops a
duplicate CRM contact, or stops attributing a payment, turns this red.

The literal constants are also re-justified from the fixture bytes on every run
(`test_every_grouping_is_justified_by_the_raw_records`), using the SS4 cascade
implemented in `tests/er/test_independent_join.py`. That module is the one import
here that touches fixture parsing; it deliberately imports no detector entity code
either, so the whole arm stays independent of `recon.er` / `recon.resolve` /
`recon.reference`.

The twenty are chosen to cover every shape the cascade can produce: `L1`, `L2` and
`L3` contact links; `P1` and `P2` payment attributions; `E1` and `E2` enrollment
attributions; `D2` deals including one household deal shared by three siblings; a
student carrying **two** CRM contacts (a planted C3 duplicate pair); a person with
two payments; app-DB-only persons with nothing to join to; lead contacts that are
their own person; and unattributed payments, which SS5.2 also calls an entity.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, create_engine, text

from recon.normalize import norm_dob, norm_email, norm_name
from tests.er.dataset import Dataset, ensure_dataset
from tests.er.test_independent_join import (
    RawWorld,
    contacts_of,
    deals_of,
    load_world,
    payments_of,
    rebind_logging_to_a_durable_stream,
)

GEN = 3


@dataclass(frozen=True)
class Grouping:
    """One human (SS5.2 entity), as a person key and the refs that belong to it."""

    why: str
    person_key: str
    anchor_ref: str
    refs: tuple[str, ...]
    methods: tuple[str, ...]

    @property
    def student_id(self) -> str | None:
        if not self.anchor_ref.startswith("appdb:student:"):
            return None
        return self.anchor_ref.split(":", 2)[2]


#: Deals a household shares. SS4.5: a household deal names 2-4 siblings, so its ref
#: sits in every sibling's ref set while `entity_links` -- one row per source record
#: per generation -- can only name one of them. Listed here so the reverse lookup
#: below can hold deals to a weaker, still-meaningful rule.
SHARED_DEAL_REFS = frozenset({"crm:deal:DEAL-0002124"})

#: The three Everton-Dane children, in ascending student ref. `DEAL-0002124` names
#: all three of their contacts (`CRM-0004540/41/42`) and no others.
EVERTON_DANE_HOUSEHOLD = (
    "8e4221af-1f38-5b87-bc97-cb85b9c6f3bc",  # 0001e46b Tarael
    "5d862ea2-30a8-56aa-9212-2d264f7adb1f",  # 69ed710b Belaar
    "ab0316aa-d07b-5a7d-bd7d-527e01ff4176",  # 78a52dc0 Finaor
)


GROUPINGS: tuple[Grouping, ...] = (
    Grouping(
        why="L1 hard key holds although the CRM contact contradicts the app DB on identity",
        person_key="ed9ace12-db92-5136-a730-2ace98d67eee",
        anchor_ref="appdb:student:08076f0d-6287-5d8e-b329-5ee5518dc53a",
        refs=(
            "appdb:enrollment:3cf5c140-6e60-58f6-a229-ace2a6b5e361",
            "appdb:student:08076f0d-6287-5d8e-b329-5ee5518dc53a",
            "crm:contact:CRM-0015897",
            "crm:deal:DEAL-0011461",
            "payments:payment:pi_0015362",
        ),
        methods=("D2", "E1", "L1", "P1"),
    ),
    Grouping(
        why="L2: the contact reaches the student only after gmail dot/+alias folding",
        person_key="da46219d-da05-55ce-ac8b-2a598d29a2a6",
        anchor_ref="appdb:student:00109aca-b448-56b3-83d5-828fed48f0da",
        refs=(
            "appdb:enrollment:7559f5b4-fb57-501b-9de6-6415da368ead",
            "appdb:student:00109aca-b448-56b3-83d5-828fed48f0da",
            "crm:contact:CRM-0011266",
            "crm:deal:DEAL-0007037",
            "payments:payment:pi_0010497",
        ),
        methods=("D2", "E1", "L2", "P2"),
    ),
    Grouping(
        why="Everton-Dane child 1 of 3: P2 attributes pi_0004593 on the metadata name",
        person_key="8e4221af-1f38-5b87-bc97-cb85b9c6f3bc",
        anchor_ref="appdb:student:0001e46b-096a-563a-afe4-49d5fefb2756",
        refs=(
            "appdb:enrollment:cbd3cc92-0b05-5178-83de-30a1c7767b7e",
            "appdb:student:0001e46b-096a-563a-afe4-49d5fefb2756",
            "crm:contact:CRM-0004542",
            "crm:deal:DEAL-0002124",
            "payments:payment:pi_0004593",
        ),
        methods=("D2", "E1", "L1", "P2"),
    ),
    Grouping(
        why="Everton-Dane child 2 of 3: P1 hard key, and E2 because the metadata program is null",
        person_key="5d862ea2-30a8-56aa-9212-2d264f7adb1f",
        anchor_ref="appdb:student:69ed710b-5656-5a8e-9eb1-8730a138b0b9",
        refs=(
            "appdb:enrollment:71de2911-aee9-54a4-b39e-bf4dfe67f73f",
            "appdb:student:69ed710b-5656-5a8e-9eb1-8730a138b0b9",
            "crm:contact:CRM-0004541",
            "crm:deal:DEAL-0002124",
            "payments:payment:pi_0004592",
        ),
        methods=("D2", "E2", "L2", "P1"),
    ),
    Grouping(
        why="Everton-Dane child 3 of 3: same household key, its own person and its own payment",
        person_key="ab0316aa-d07b-5a7d-bd7d-527e01ff4176",
        anchor_ref="appdb:student:78a52dc0-2392-56d3-961f-53ee249a540d",
        refs=(
            "appdb:enrollment:fdb215bf-4fd9-55ac-b876-1b5f9e7912e5",
            "appdb:student:78a52dc0-2392-56d3-961f-53ee249a540d",
            "crm:contact:CRM-0004540",
            "crm:deal:DEAL-0002124",
            "payments:payment:pi_0004591",
        ),
        methods=("D2", "E1", "L1", "P2"),
    ),
    Grouping(
        why="one student, TWO CRM contacts -- a planted duplicate pair, one person",
        person_key="021f22d6-69c9-5bd6-9814-de697d986824",
        anchor_ref="appdb:student:0092c89c-e969-5a19-96fa-72952787e40a",
        refs=(
            "appdb:enrollment:9a4b7d8e-dfe1-59e7-8b0c-318c9bb67ddf",
            "appdb:student:0092c89c-e969-5a19-96fa-72952787e40a",
            "crm:contact:CRM-0010341",
            "crm:contact:CRM-0021699",
            "crm:deal:DEAL-0006145",
            "payments:payment:pi_0009515",
        ),
        methods=("D2", "E1", "L1", "P1"),
    ),
    Grouping(
        why="L3: name + dob link, with no email in common",
        person_key="81604999-ede6-54ba-8b1e-86cd09bc6354",
        anchor_ref="appdb:student:01c83d05-b8a9-5bbb-8a97-6d5fdd6343ae",
        refs=(
            "appdb:enrollment:f58308c4-0e25-5ac1-b54b-2563301dc13e",
            "appdb:student:01c83d05-b8a9-5bbb-8a97-6d5fdd6343ae",
            "crm:contact:CRM-0009362",
            "crm:deal:DEAL-0005217",
            "payments:payment:pi_0008484",
        ),
        methods=("D2", "E1", "L3", "P1"),
    ),
    Grouping(
        why="two payments land on one person",
        person_key="ff08c13f-d159-5bf7-a491-0e49a4a66c42",
        anchor_ref="appdb:student:0004e7c8-b9d4-5231-af40-626c0afce62c",
        refs=(
            "appdb:enrollment:2b16510a-9470-564d-bf28-8f45564623ec",
            "appdb:student:0004e7c8-b9d4-5231-af40-626c0afce62c",
            "crm:contact:CRM-0010616",
            "crm:deal:DEAL-0006412",
            "payments:payment:pi_0009806",
            "payments:payment:pi_0009807",
        ),
        methods=("D2", "E1", "L1", "P2"),
    ),
    Grouping(
        why="app-DB-only human: no contact, no payment, no deal, no enrollment",
        person_key="65dc2b8c-a27f-5bed-aa7a-48bd4a907f0b",
        anchor_ref="appdb:student:00103042-aa90-5301-8409-30216a6f86d6",
        refs=("appdb:student:00103042-aa90-5301-8409-30216a6f86d6",),
        methods=(),
    ),
    Grouping(
        why="a second app-DB-only human",
        person_key="9794e7cd-348e-5dcd-b225-2dc3ee64ab16",
        anchor_ref="appdb:student:0017d010-ad52-53eb-a267-9b0d405191f6",
        refs=("appdb:student:0017d010-ad52-53eb-a267-9b0d405191f6",),
        methods=(),
    ),
    Grouping(
        why="a third app-DB-only human",
        person_key="7d6852b5-963f-5f5a-8fd3-c31970269f2f",
        anchor_ref="appdb:student:33a6deb7-4821-548f-9e4d-e1396f025087",
        refs=("appdb:student:33a6deb7-4821-548f-9e4d-e1396f025087",),
        methods=(),
    ),
    Grouping(
        why="app DB only, but registered -- the enrollment joins by student_id, not by a rule",
        person_key="2fff3170-3e6c-5ab5-8d39-61534da216bd",
        anchor_ref="appdb:student:40a9a32d-5200-5222-bf0f-f90bf1e93776",
        refs=(
            "appdb:enrollment:835a95f7-7b34-5ec8-a0a3-15ab9d47e4e3",
            "appdb:student:40a9a32d-5200-5222-bf0f-f90bf1e93776",
        ),
        methods=(),
    ),
    Grouping(
        why="lead contact: a CRM-only human, anchored on its own contact ref",
        person_key="e117502f-a85d-5944-a900-1558ae6d188b",
        anchor_ref="crm:contact:CRM-0021901",
        refs=("crm:contact:CRM-0021901",),
        methods=(),
    ),
    Grouping(
        why="a second lead contact",
        person_key="9da34eb3-c3ac-5954-ad81-7de9194e1bfe",
        anchor_ref="crm:contact:CRM-0021902",
        refs=("crm:contact:CRM-0021902",),
        methods=(),
    ),
    Grouping(
        why="unattributed payment: SS5.2 calls it an entity, anchored on its own ref",
        person_key="09ef2122-d8e4-5066-880e-8062f7ac7ba4",
        anchor_ref="payments:payment:pi_0000031",
        refs=("payments:payment:pi_0000031",),
        methods=(),
    ),
    Grouping(
        why="a second unattributed payment",
        person_key="ca8bde24-e6fb-5e7e-a8d0-33b4eafc02b5",
        anchor_ref="payments:payment:pi_0000138",
        refs=("payments:payment:pi_0000138",),
        methods=(),
    ),
    Grouping(
        why="L1 + a household deal, but no payment ever arrived",
        person_key="468232b8-43ec-5b2d-83da-8112622cb26d",
        anchor_ref="appdb:student:0cccd091-109c-516f-b6f4-1d4fd9ac058d",
        refs=(
            "appdb:enrollment:d3f0edf7-9ec0-5e75-bd6b-f43281492b21",
            "appdb:student:0cccd091-109c-516f-b6f4-1d4fd9ac058d",
            "crm:contact:CRM-0006334",
            "crm:deal:DEAL-0002722",
        ),
        methods=("D2", "L1"),
    ),
    Grouping(
        why="L2 contact plus a P1 payment on a four-child household's deal",
        person_key="26217990-663c-5863-8783-6d0e5c51d82c",
        anchor_ref="appdb:student:1957aad4-fcdf-586c-848f-05f4910dd0bc",
        refs=(
            "appdb:enrollment:a70c0dde-ed85-5ea2-b847-9aa78cbbc84c",
            "appdb:student:1957aad4-fcdf-586c-848f-05f4910dd0bc",
            "crm:contact:CRM-0005821",
            "crm:deal:DEAL-0002508",
            "payments:payment:pi_0005885",
        ),
        methods=("D2", "E1", "L2", "P1"),
    ),
    Grouping(
        why="another L2 + P1 person, from a different part of the id space",
        person_key="16086950-8971-53e9-a1a1-ed70b14e33e2",
        anchor_ref="appdb:student:26afe96b-a797-59e5-b410-f5c95f915689",
        refs=(
            "appdb:enrollment:cbac364d-1b9f-57ef-b5b9-e0e4518dcd26",
            "appdb:student:26afe96b-a797-59e5-b410-f5c95f915689",
            "crm:contact:CRM-0012953",
            "crm:deal:DEAL-0008652",
            "payments:payment:pi_0012279",
        ),
        methods=("D2", "E1", "L2", "P1"),
    ),
    Grouping(
        why="E2: the payment carries no usable program, so it falls to the one enrollment",
        person_key="69e13ace-aad4-5f04-8071-3fbe2bf655bc",
        anchor_ref="appdb:student:4d0b3d7d-aea6-5e4f-a85e-2a1f23c08bc5",
        refs=(
            "appdb:enrollment:93aa1c6c-2ba6-58cc-847e-23b738f14dde",
            "appdb:student:4d0b3d7d-aea6-5e4f-a85e-2a1f23c08bc5",
            "crm:contact:CRM-0014556",
            "crm:deal:DEAL-0010177",
            "payments:payment:pi_0013952",
        ),
        methods=("D2", "E2", "L1", "P1"),
    ),
)

IDS = [g.person_key[:8] for g in GROUPINGS]


# ======================================================================================
# fixtures -- read the materialized layer through an engine of this module's own
# ======================================================================================


@pytest.fixture(scope="session")
def materialized() -> Dataset:
    """The full-profile generation-3 tree, ingested and resolved (`tests.er.dataset`).

    The rebind on the first line is load-bearing, not tidiness. `ensure_dataset()`
    runs `recon.ingest.ingest_generation`, which logs `ingest.source_done`; when this
    module runs *inside its own package* rather than on its own, an earlier test has
    already left structlog's `WriteLogger` holding a stream pytest has since closed
    (`tests/invariants/test_cli.py` drives `recon.invariants.__main__.main()` under
    `capsys`, and every entry point calls `configure_logging_once()`). The log call
    then raises ``ValueError: I/O operation on closed file`` *in fixture setup*, so
    all 45 database-backed tests in this file ERROR and the half of this arm that
    checks the **persisted** entity layer -- the half that makes it independent --
    silently does not execute. `pytest tests/invariants` measured exactly that: 45
    errors, every one of them here. Running the file alone hid it, because nothing
    had poisoned the chain yet.

    `rebind_logging_to_a_durable_stream` is documented where it lives
    (`tests/er/test_independent_join.py`); it re-installs the full production chain,
    redaction processor included, on `sys.__stderr__`, so the ingest below really
    logs and no assertion here is weakened or skipped to get green.
    """
    rebind_logging_to_a_durable_stream()
    return ensure_dataset()


@pytest.fixture(scope="session")
def entity_reader(materialized: Dataset) -> Iterator[Engine]:
    """An engine bound to the materialized database by DSN, not by process state.

    `tests/invariants/conftest.py` and `tests/er/dataset.py` both re-point the
    process at a scratch database of their own; naming the DSN explicitly here means
    the order the two suites happen to run in cannot decide which database these
    assertions read.
    """
    engine = create_engine(
        materialized.dsn.replace("postgresql://", "postgresql+psycopg://"), future=True
    )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def world() -> RawWorld:
    return load_world()


_ENTITY = text("SELECT current FROM entities WHERE canonical_id = CAST(:key AS uuid)")

_LINK = text(
    """
    SELECT canonical_id::text AS canonical_id, method
      FROM entity_links
     WHERE generation = :generation AND source_ref = :ref
    """
)

_CARRIERS = text(
    """
    SELECT canonical_id::text AS canonical_id
      FROM entities
     WHERE current -> 'entity_refs' @> CAST(:ref AS jsonb)
     ORDER BY canonical_id
    """
)


# ======================================================================================
# 1. the persisted entity layer says what the answer key says
# ======================================================================================


@pytest.mark.parametrize("group", GROUPINGS, ids=IDS)
def test_persisted_entity_matches_the_hand_written_grouping(
    entity_reader: Engine, group: Grouping
) -> None:
    """`entities.current` for this person carries exactly the hand-derived ref set."""
    with entity_reader.connect() as conn:
        row = conn.execute(_ENTITY, {"key": group.person_key}).fetchone()

    assert row is not None, f"no entity row for {group.person_key} ({group.why})"
    current = dict(row.current)
    assert current["anchor_ref"] == group.anchor_ref, group.why
    assert current["person_key"] == group.person_key
    assert current["canonical_id"] == group.person_key
    assert tuple(current["entity_refs"]) == group.refs, group.why
    assert tuple(current["link_methods"]) == group.methods, group.why


@pytest.mark.parametrize("group", GROUPINGS, ids=IDS)
def test_every_record_in_the_grouping_links_back_to_it(
    entity_reader: Engine, group: Grouping
) -> None:
    """The reverse direction: each source record names this person and no other.

    `entity_links` holds one row per source record per generation, so this is the
    check that catches a record being re-homed onto a different human -- the failure
    an `entity_refs`-only comparison could miss if the cascade merged two people.
    Shared household deals are the documented exception (SS4.5): the ref belongs to
    every sibling but only one row can name it, so a shared deal is asserted to land
    on one of the siblings this file lists rather than on this exact one.
    """
    with entity_reader.connect() as conn:
        for ref in group.refs:
            row = conn.execute(_LINK, {"generation": GEN, "ref": ref}).fetchone()
            assert row is not None, f"{ref} has no entity_links row ({group.why})"
            if ref in SHARED_DEAL_REFS:
                assert row.canonical_id in EVERTON_DANE_HOUSEHOLD, ref
            else:
                assert row.canonical_id == group.person_key, f"{ref} was re-homed ({group.why})"


def test_the_household_deal_reaches_exactly_the_three_siblings(entity_reader: Engine) -> None:
    """`DEAL-0002124` names three sibling contacts, so exactly three persons carry it.

    A cascade that collapsed the household into one person, or that fanned the deal
    out to every contact sharing the guardian address, changes this count.
    """
    with entity_reader.connect() as conn:
        rows = conn.execute(_CARRIERS, {"ref": json.dumps(["crm:deal:DEAL-0002124"])}).fetchall()

    assert sorted(row.canonical_id for row in rows) == sorted(EVERTON_DANE_HOUSEHOLD)


def test_the_twenty_groupings_are_twenty_distinct_humans(entity_reader: Engine) -> None:
    """No two of them share a person key, and no two share a non-deal record."""
    keys = [g.person_key for g in GROUPINGS]
    assert len(set(keys)) == len(GROUPINGS) == 20, keys

    seen: dict[str, str] = {}
    for group in GROUPINGS:
        for ref in group.refs:
            if ref.startswith("crm:deal:"):
                continue
            assert ref not in seen, f"{ref} appears in both {seen.get(ref)} and {group.person_key}"
            seen[ref] = group.person_key

    with entity_reader.connect() as conn:
        found = conn.execute(
            text(
                "SELECT canonical_id::text AS canonical_id FROM entities "
                "WHERE canonical_id = ANY(CAST(:keys AS uuid[]))"
            ),
            {"keys": keys},
        ).fetchall()
    assert sorted(row.canonical_id for row in found) == sorted(keys)


# ======================================================================================
# 1b. the same arm, over the whole dataset rather than twenty samples
# ======================================================================================


def test_the_population_of_humans_is_what_the_raw_records_imply(
    world: RawWorld, entity_reader: Engine
) -> None:
    """SS5.2's entity population, counted twice: from the JSONL, and from `entities`.

    Twenty hand-checked groupings prove the cascade right about twenty people; this
    proves it did not invent or lose anybody across the other forty-three thousand.
    The count is a closed form under SS4/SS5.2 -- one person per app-DB student, one
    per lead contact (a contact no rule reaches a student by), one per unattributed
    payment -- so it can be derived from the fixtures with no cascade output in hand.
    """
    linked = {c["crm_id"] for rows in world.student_contacts.values() for c, _ in rows}
    leads = [c for c in world.contacts if c["crm_id"] not in linked]
    attributed = {p["payment_id"] for rows in world.student_payments.values() for p, _ in rows}
    orphans = [p for p in world.payments if p["payment_id"] not in attributed]

    assert (len(world.students), len(leads), len(orphans)) == (25000, 18175, 200)
    expected = len(world.students) + len(leads) + len(orphans)

    with entity_reader.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM entities")).scalar() == expected


def test_every_source_record_belongs_to_exactly_one_person(
    world: RawWorld, entity_reader: Engine
) -> None:
    """All 120,000 generation-3 records are attached, and none is attached twice.

    `entity_links` holds one row per source record per generation, so "linked once"
    and "linked at all" are both readable off its cardinality -- and a cascade that
    dropped a class of record (or double-counted one) cannot keep both numbers.
    """
    records = (
        len(world.students)
        + len(world.enrollments)
        + len(world.contacts)
        + len(world.deals)
        + len(world.payments)
    )
    assert records == 120000

    with entity_reader.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT count(*) AS total, count(DISTINCT source_ref) AS distinct_refs "
                "FROM entity_links WHERE generation = :generation"
            ),
            {"generation": GEN},
        ).one()
    assert rows.total == records
    assert rows.distinct_refs == records


def test_the_staging_tables_hold_the_full_profile_tree(entity_reader: Engine) -> None:
    """A dev-profile or half-ingested database would make every assertion vacuous."""
    expected = {
        "stg_student": 25000,
        "stg_crm_contact": 40000,
        "stg_crm_deal": 15000,
        "stg_enrollment": 22000,
        "stg_payment": 18000,
    }
    with entity_reader.connect() as conn:
        counts = {
            table: conn.execute(
                text(f"SELECT count(*) FROM {table} WHERE generation = :generation"),
                {"generation": GEN},
            ).scalar()
            for table in expected
        }
    assert counts == expected


# ======================================================================================
# 2. the answer key is still what the raw fixture bytes say
# ======================================================================================


def _refs_from_raw(world: RawWorld, group: Grouping) -> tuple[str, ...]:
    """Re-derive one grouping's ref set from the JSONL, by SS4, with no DB involved."""
    student_id = group.student_id
    if student_id is None:
        return group.refs  # lead contacts / unattributed payments: handled separately

    contacts = contacts_of(world, student_id)
    payments = payments_of(world, student_id)
    enrollments = world.enrollments_by_student.get(student_id, ())
    deals = deals_of(world, (c["crm_id"] for c, _ in contacts))

    return tuple(
        sorted(
            {f"appdb:student:{student_id}"}
            | {f"appdb:enrollment:{e['id']}" for e in enrollments}
            | {f"crm:contact:{c['crm_id']}" for c, _ in contacts}
            | {f"crm:deal:{d['deal_id']}" for d in deals}
            | {f"payments:payment:{p['payment_id']}" for p, _ in payments}
        )
    )


@pytest.mark.parametrize("group", GROUPINGS, ids=IDS)
def test_every_grouping_is_justified_by_the_raw_records(world: RawWorld, group: Grouping) -> None:
    """The literal ref set falls out of the fixture JSONL under SS4's rules.

    Without this, a fixture reroll could leave the constants above describing people
    who no longer exist while the database happily agreed with the cascade.
    """
    if group.student_id is not None:
        assert _refs_from_raw(world, group) == group.refs, group.why
        return

    ref = group.anchor_ref
    if ref.startswith("crm:contact:"):
        # SS4.2 / SS11.4 G11: a lead is a contact no cascade rule reaches a student by.
        assert _is_lead_contact(world, ref.split(":", 2)[2]), f"{ref} does reach a student"
        return

    # SS4.3: an unattributable payment is C2, never a guess -- and its own entity.
    payment = next(p for p in world.payments if f"payments:payment:{p['payment_id']}" == ref)
    assert payment.get("external_ref") not in world.student_by_id, "P1 would have fired"
    payer = norm_email(payment.get("payer_email"))
    assert payer not in world.households, "P2/P3 would have had a household to attribute to"


def _is_lead_contact(world: RawWorld, crm_id: str) -> bool:
    """SS4.2 read backwards: no `L1` hard key and no free student in either bucket.

    "Free" is SS4.2's rejection clause -- a student a hard key already owns is not
    reachable by `L2`/`L3`, so a bucket holding only such students is still a miss.
    """
    contact = world.contact_by_id[crm_id]
    if contact.get("external_id") in world.student_by_id:
        return False
    first = norm_name(contact.get("first_name"))
    last = norm_name(contact.get("last_name"))
    mail = norm_email(contact.get("email"))
    born = norm_dob(contact.get("dob"))
    buckets = (
        world.by_email_name.get((mail, first, last), ()),
        world.by_namedob.get((first, last, born), ()),
    )
    return all(all(student["id"] in world.l1_students for student in bucket) for bucket in buckets)


def test_the_lead_contacts_reach_no_student_at_all(world: RawWorld) -> None:
    """Spelled out for the two leads: no hard key, and both blocking keys miss entirely."""
    for crm_id in ("CRM-0021901", "CRM-0021902"):
        contact = world.contact_by_id[crm_id]
        assert contact["external_id"] is None
        first = norm_name(contact["first_name"])
        last = norm_name(contact["last_name"])
        mail = norm_email(contact["email"])
        born = norm_dob(contact["dob"])
        assert world.by_email_name.get((mail, first, last), ()) == (), crm_id
        assert world.by_namedob.get((first, last, born), ()) == (), crm_id
        assert _is_lead_contact(world, crm_id)


def test_the_duplicate_pair_really_is_two_contact_records(world: RawWorld) -> None:
    """The C3 grouping is only meaningful while both contacts exist and both link."""
    student_id = "0092c89c-e969-5a19-96fa-72952787e40a"
    linked = contacts_of(world, student_id)
    assert sorted(c["crm_id"] for c, _ in linked) == ["CRM-0010341", "CRM-0021699"]
    assert {method for _, method in linked} == {"L1"}
    for contact, _ in linked:
        assert contact["external_id"] == student_id
