"""Siblings share a guardian email and are still different children (R9).

The brief names this failure explicitly, and the generator builds the trap: at
least 1,000 multi-child households whose children carry the **same**
`guardian_email`. `L2` is the rule that could collapse them -- it matches a
contact's email against a student's guardian addresses -- and it is guarded by the
name equality the contract pins (SS4.2), while `P2`/`P3` guard payment
attribution by requiring the metadata name pair or a single-child household.

So this module does not test a rule. It takes the generator's **real** households
out of `stg_student`, enumerates every sibling pair in them, and asserts each pair
resolved to two different canonical entities that nonetheless agree on the
household. The pair count is asserted against the brief's floor and reported in
the assertion messages.
"""

from __future__ import annotations

from itertools import combinations

import pytest
from sqlalchemy import Engine, text

from tests.er.dataset import Dataset

#: A.4 -- the brief's floor for multi-child households in the full profile.
MULTI_CHILD_HOUSEHOLD_MINIMUM = 1000

_HOUSEHOLDS = text(
    """
    SELECT email_norm AS household_key,
           array_agg(student_id ORDER BY student_id) AS student_ids
      FROM stg_student
     WHERE generation = :generation
       AND email_norm IS NOT NULL
     GROUP BY email_norm
    HAVING count(*) > 1
     ORDER BY email_norm
    """
)

_STUDENT_TO_CANONICAL = text(
    """
    SELECT source_key AS student_id, canonical_id::text AS canonical_id
      FROM entity_links
     WHERE generation = :generation
       AND source_id = 'appdb'
       AND starts_with(source_ref, 'appdb:student:')
    """
)

_ENTITY_STUDENT_REFS = text(
    """
    SELECT count(*) AS collapsed
      FROM entities e
     WHERE (
        SELECT count(*)
          FROM jsonb_array_elements_text(e.current -> 'entity_refs') AS ref
         WHERE starts_with(ref, 'appdb:student:')
     ) > 1
    """
)


@pytest.fixture(scope="module")
def households(reader: Engine, dataset: Dataset) -> list[tuple[str, list[str]]]:
    with reader.connect() as conn:
        rows = conn.execute(_HOUSEHOLDS, {"generation": dataset.generation}).fetchall()
    return [(row.household_key, list(row.student_ids)) for row in rows]


@pytest.fixture(scope="module")
def student_person(reader: Engine, dataset: Dataset) -> dict[str, str]:
    with reader.connect() as conn:
        rows = conn.execute(_STUDENT_TO_CANONICAL, {"generation": dataset.generation}).fetchall()
    return {row.student_id: row.canonical_id for row in rows}


def test_the_dataset_actually_contains_the_trap(
    households: list[tuple[str, list[str]]],
) -> None:
    """Without the multi-child households, everything below proves nothing."""
    assert len(households) >= MULTI_CHILD_HOUSEHOLD_MINIMUM, (
        f"only {len(households)} multi-child households in the ingested generation; "
        f"A.4 requires at least {MULTI_CHILD_HOUSEHOLD_MINIMUM}"
    )
    assert any(len(members) >= 3 for _, members in households), (
        "no household with three or more children: the pair enumeration below would "
        "never exercise a three-way collapse"
    )


def test_no_sibling_pair_was_merged(
    households: list[tuple[str, list[str]]], student_person: dict[str, str]
) -> None:
    """Every pair of children sharing a guardian email resolved to two entities."""
    pairs = 0
    merged: list[str] = []

    for household_key, members in households:
        for left, right in combinations(members, 2):
            pairs += 1
            left_key = student_person.get(left)
            right_key = student_person.get(right)
            assert left_key and right_key, (
                f"student {left if not left_key else right} has no canonical entity; "
                "the merge check cannot be evaluated"
            )
            if left_key == right_key:
                merged.append(f"{household_key}: {left} and {right} -> {left_key}")

    assert not merged, (
        f"{len(merged)} of {pairs} sibling pairs were merged into one entity, e.g. {merged[:3]}"
    )
    assert pairs >= MULTI_CHILD_HOUSEHOLD_MINIMUM, f"only {pairs} sibling pairs were checked"
    print(f"\nsibling pairs checked: {pairs} across {len(households)} multi-child households")


def test_no_entity_holds_two_student_refs(reader: Engine) -> None:
    """The merge failure, stated as a property of the canonical layer itself.

    The pair walk above could in principle miss a collapse that happened outside a
    shared-guardian household (a name/DOB collision, say). This one cannot: two
    `appdb:student:` refs on one entity **is** a merged pair, whatever produced it.
    """
    with reader.connect() as conn:
        collapsed = int(conn.execute(_ENTITY_STUDENT_REFS).scalar_one())
    assert collapsed == 0, f"{collapsed} canonical entities hold more than one student ref"


def test_siblings_share_a_household_and_differ_in_identity(
    reader: Engine, households: list[tuple[str, list[str]]], student_person: dict[str, str]
) -> None:
    """The positive half: same `household_key`, different `person_key`.

    "Not merged" would also be satisfied by an entity resolution that simply
    failed to see the household at all, so the sample is checked from both sides.
    """
    sample = households[:200]
    sql = text("SELECT current FROM entities WHERE canonical_id = CAST(:key AS uuid)")

    checked = 0
    with reader.connect() as conn:
        for household_key, members in sample:
            keys = {student_person[member] for member in members}
            assert len(keys) == len(members)
            for member in members:
                current = conn.execute(sql, {"key": student_person[member]}).scalar_one()
                assert current["household_key"] == household_key
                assert f"appdb:student:{member}" in current["entity_refs"]
                checked += 1

    assert checked >= 2 * len(sample)
