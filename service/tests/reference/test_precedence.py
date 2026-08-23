"""`apply_precedence` -- the SS5.7 filter, as ONE function (contract SS5.7, `G32`).

`golden/conflicts.json` is written through this filter and the detector runs the
same call before materialising conflicts. The contract suppresses C7 from a raw
population of 875 to 300 through three separate rules; two slightly different
implementations of that filter would be up to 575 false positives against a golden
count of 300, so every rule gets its own test asserting BOTH halves: the suppressed
entry is gone, and the surviving entry is untouched.
"""

from __future__ import annotations

import copy
import itertools
import random
from typing import Any

import pytest

from recon.reference import (
    MECHANICAL_SUPPRESSIONS,
    PRECEDENCE,
    apply_precedence,
    assert_unique_conflict_keys,
    conflict_key,
    conflict_refs,
    conflict_type_for_paths,
)

STUDENT_A = "appdb:student:sA"
STUDENT_B = "appdb:student:sB"
CONTACT_A = "crm:contact:CRM-0000001"
CONTACT_B = "crm:contact:CRM-0000002"
ENROLL_A = "appdb:enrollment:eA"
ENROLL_B = "appdb:enrollment:eB"
PAY_1 = "payments:payment:pi_0000001"
PAY_2 = "payments:payment:pi_0000002"
PAY_3 = "payments:payment:pi_0000003"

NAME_PATHS = ["crm.contact.first_name", "appdb.student.first_name"]
GRADE_PATHS = ["crm.contact.grade", "appdb.student.grade"]


def entry(conflict_type: str, refs: tuple[str, ...], paths: list[str] | None = None) -> dict:
    return {
        "type": conflict_type,
        "entity_refs": list(refs),
        "disagreeing_fields": sorted(paths or []),
    }


def person(*refs: str) -> tuple[str, ...]:
    return tuple(sorted(refs))


def types(entries: list[Any]) -> list[str]:
    return [item["type"] for item in entries]


# --------------------------------------------------------------------------- rule 1


def test_rule_1_c14_over_c6_when_the_disagreeing_set_is_wholly_sensitive() -> None:
    refs = person(STUDENT_A, CONTACT_A)
    c6 = entry("C6", refs, NAME_PATHS)
    c14 = entry("C14", refs, NAME_PATHS)

    survivors = apply_precedence([c6, c14])

    assert types(survivors) == ["C14"]
    assert survivors[0] is c14
    assert survivors[0] == entry("C14", refs, NAME_PATHS)


def test_rule_1_mixed_sets_emit_c6_only_with_the_sensitive_paths_retained() -> None:
    refs = person(STUDENT_A, CONTACT_A)
    mixed = sorted(NAME_PATHS + GRADE_PATHS)
    c6 = entry("C6", refs, mixed)
    c14 = entry("C14", refs, NAME_PATHS)

    survivors = apply_precedence([c6, c14])

    assert types(survivors) == ["C6"]
    assert survivors[0] is c6
    assert survivors[0]["disagreeing_fields"] == mixed  # sensitive paths still listed


def test_rule_1_does_not_touch_a_lone_entry_or_two_different_persons() -> None:
    lone_c6 = entry("C6", person(STUDENT_A, CONTACT_A), NAME_PATHS)
    other_c14 = entry("C14", person(STUDENT_B, CONTACT_B), NAME_PATHS)

    survivors = apply_precedence([lone_c6, other_c14])

    assert types(survivors) == ["C14", "C6"]


# --------------------------------------------------------------------------- rule 2


def test_rule_2_c10_suppresses_c6_c14_and_c4_on_the_collapsed_contact() -> None:
    c10 = entry(
        "C10", conflict_refs("C10", contact_refs=[CONTACT_A], student_refs=[STUDENT_A, STUDENT_B])
    )
    collapsed_person = person(STUDENT_A, CONTACT_A)
    suppressed = [
        entry("C6", collapsed_person, GRADE_PATHS),
        entry("C14", collapsed_person, NAME_PATHS),
        entry("C4", collapsed_person),
    ]
    untouched = entry("C6", person(STUDENT_B, CONTACT_B), GRADE_PATHS)

    survivors = apply_precedence([c10, *suppressed, untouched])

    assert types(survivors) == ["C10", "C6"]
    assert survivors[0] is c10
    assert survivors[1] is untouched
    assert untouched == entry("C6", person(STUDENT_B, CONTACT_B), GRADE_PATHS)


def test_rule_2_matches_on_the_contact_ref_not_the_student_refs() -> None:
    """C10's `entity_refs` also name two students; suppression keys on the contact."""
    c10 = entry(
        "C10", conflict_refs("C10", contact_refs=[CONTACT_A], student_refs=[STUDENT_A, STUDENT_B])
    )
    # Student B's own person carries its own separate contact (`G21`), so its C6
    # must survive even though student B's ref appears in C10's entity_refs.
    student_b_person = entry("C6", person(STUDENT_B, CONTACT_B), GRADE_PATHS)

    survivors = apply_precedence([c10, student_b_person])

    assert types(survivors) == ["C10", "C6"]


# --------------------------------------------------------------------------- rule 3


def test_rule_3_c2_suppresses_c12_and_c11_on_the_unattributable_payment() -> None:
    c2 = entry("C2", conflict_refs("C2", payment_refs=[PAY_1]))
    c12 = entry("C12", conflict_refs("C12", identity_refs=[STUDENT_A], payment_refs=[PAY_1]))
    c11 = entry("C11", conflict_refs("C11", payment_refs=[PAY_1, PAY_2]))
    unrelated = entry("C11", conflict_refs("C11", payment_refs=[PAY_2, PAY_3]))

    survivors = apply_precedence([c2, c12, c11, unrelated])

    assert types(survivors) == ["C11", "C2"]
    assert survivors[0] is unrelated
    assert survivors[1] is c2


# --------------------------------------------------------------------------- rule 4


def test_rule_4_c5_suppresses_c1_and_c7_for_that_student() -> None:
    c5 = entry("C5", conflict_refs("C5", identity_refs=[STUDENT_A]))
    c1 = entry("C1", conflict_refs("C1", identity_refs=[STUDENT_A]))
    c7 = entry("C7", conflict_refs("C7", identity_refs=[STUDENT_A], enrollment_refs=[ENROLL_A]))
    other_c7 = entry(
        "C7", conflict_refs("C7", identity_refs=[STUDENT_B], enrollment_refs=[ENROLL_B])
    )

    survivors = apply_precedence([c5, c1, c7, other_c7])

    assert types(survivors) == ["C5", "C7"]
    assert survivors[1] is other_c7
    assert other_c7 == entry(
        "C7", conflict_refs("C7", identity_refs=[STUDENT_B], enrollment_refs=[ENROLL_B])
    )


# --------------------------------------------------------------------------- rule 5


def test_rule_5_c13_suppresses_c7_on_the_same_enrollment() -> None:
    c13 = entry(
        "C13",
        conflict_refs(
            "C13", identity_refs=[STUDENT_A], payment_refs=[PAY_1], enrollment_refs=[ENROLL_A]
        ),
    )
    c7 = entry("C7", conflict_refs("C7", identity_refs=[STUDENT_A], enrollment_refs=[ENROLL_A]))

    survivors = apply_precedence([c13, c7])

    assert types(survivors) == ["C13"]
    assert survivors[0] is c13


# --------------------------------------------------------------------------- rule 6


def test_rule_6_c9_suppresses_c1_but_is_vacuous_in_a_well_formed_dataset() -> None:
    c9 = entry("C9", conflict_refs("C9", identity_refs=[STUDENT_A], enrollment_refs=[ENROLL_A]))
    c1 = entry("C1", conflict_refs("C1", identity_refs=[STUDENT_A]))

    report: dict[int, int] = {}
    survivors = apply_precedence([c9, c1], report=report)
    assert types(survivors) == ["C9"]
    assert report[6] == 1

    # `G9`: every C9 plant's household holds a live D2 deal, so no C1 co-exists and
    # `sc_construction_sweep` asserts this rule fires zero times.
    clean_report: dict[int, int] = {}
    clean = apply_precedence([c9], report=clean_report)
    assert types(clean) == ["C9"]
    assert clean_report[6] == 0


# --------------------------------------------------------------------------- rule 7


def test_rule_7_c10_suppresses_c5_but_is_expected_to_fire_zero_times() -> None:
    c10 = entry(
        "C10", conflict_refs("C10", contact_refs=[CONTACT_A], student_refs=[STUDENT_A, STUDENT_B])
    )
    c5 = entry("C5", conflict_refs("C5", identity_refs=[STUDENT_B]))

    report: dict[int, int] = {}
    survivors = apply_precedence([c10, c5], report=report)
    assert types(survivors) == ["C10"]
    assert report[7] == 1

    clean_report: dict[int, int] = {}
    apply_precedence([c10], report=clean_report)
    assert clean_report[7] == 0


# --------------------------------------------------------------------------- rule 8


def test_rule_8_c8_suppresses_c1_and_c7_on_the_dropped_child() -> None:
    c8 = entry("C8", conflict_refs("C8", identity_refs=[STUDENT_A]))
    c1 = entry("C1", conflict_refs("C1", identity_refs=[STUDENT_A]))
    c7 = entry("C7", conflict_refs("C7", identity_refs=[STUDENT_A], enrollment_refs=[ENROLL_A]))

    survivors = apply_precedence([c8, c1, c7])

    assert types(survivors) == ["C8"]
    assert survivors[0] is c8


# ------------------------------------------------------------------- composite shape


def test_the_c7_suppression_stack_matches_the_construction_sweep_shape() -> None:
    """SS9.1(b): the raw C7 population is suppressed by rules 4, 5 and 8 alone."""
    survivor = entry(
        "C7",
        conflict_refs(
            "C7", identity_refs=["appdb:student:s0"], enrollment_refs=["appdb:enrollment:e0"]
        ),
    )
    entries = [
        survivor,
        entry("C7", conflict_refs("C7", identity_refs=[STUDENT_A], enrollment_refs=[ENROLL_A])),
        entry("C5", conflict_refs("C5", identity_refs=[STUDENT_A])),
        entry("C7", conflict_refs("C7", identity_refs=[STUDENT_B], enrollment_refs=[ENROLL_B])),
        entry(
            "C13",
            conflict_refs(
                "C13", identity_refs=[STUDENT_B], payment_refs=[PAY_1], enrollment_refs=[ENROLL_B]
            ),
        ),
        entry(
            "C7",
            conflict_refs(
                "C7", identity_refs=["appdb:student:sC"], enrollment_refs=["appdb:enrollment:eC"]
            ),
        ),
        entry("C8", conflict_refs("C8", identity_refs=["appdb:student:sC"])),
    ]

    report: dict[int, int] = {}
    survivors = apply_precedence(entries, report=report)

    assert [item["type"] for item in survivors].count("C7") == 1
    assert survivor in survivors
    assert report[4] == 1
    assert report[5] == 1
    assert report[8] == 1


# ------------------------------------------------------------- structural properties


def _mixed_population() -> list[dict]:
    return [
        entry("C6", person(STUDENT_A, CONTACT_A), NAME_PATHS),
        entry("C14", person(STUDENT_A, CONTACT_A), NAME_PATHS),
        entry(
            "C10",
            conflict_refs(
                "C10", contact_refs=[CONTACT_B], student_refs=[STUDENT_B, "appdb:student:sC"]
            ),
        ),
        entry("C6", person(STUDENT_B, CONTACT_B), GRADE_PATHS),
        entry("C2", conflict_refs("C2", payment_refs=[PAY_1])),
        entry("C12", conflict_refs("C12", identity_refs=[STUDENT_A], payment_refs=[PAY_1])),
        entry("C5", conflict_refs("C5", identity_refs=["appdb:student:sD"])),
        entry(
            "C7",
            conflict_refs(
                "C7", identity_refs=["appdb:student:sD"], enrollment_refs=["appdb:enrollment:eD"]
            ),
        ),
        entry(
            "C13",
            conflict_refs(
                "C13",
                identity_refs=["appdb:student:sE"],
                payment_refs=[PAY_2],
                enrollment_refs=["appdb:enrollment:eE"],
            ),
        ),
        entry(
            "C7",
            conflict_refs(
                "C7", identity_refs=["appdb:student:sE"], enrollment_refs=["appdb:enrollment:eE"]
            ),
        ),
        entry("C3", conflict_refs("C3", contact_refs=[CONTACT_A, CONTACT_B])),
    ]


def test_apply_precedence_is_idempotent() -> None:
    once = apply_precedence(_mixed_population())
    twice = apply_precedence(once)
    assert twice == once
    assert [id(item) for item in twice] == [id(item) for item in once]


def test_output_is_independent_of_input_order() -> None:
    population = _mixed_population()
    expected = [conflict_key(item) for item in apply_precedence(population)]

    rng = random.Random(20260822)
    for _ in range(25):
        shuffled = population[:]
        rng.shuffle(shuffled)
        assert [conflict_key(item) for item in apply_precedence(shuffled)] == expected

    for permutation in itertools.islice(itertools.permutations(population[:6]), 60):
        subset_expected = [conflict_key(i) for i in apply_precedence(list(population[:6]))]
        assert [conflict_key(i) for i in apply_precedence(list(permutation))] == subset_expected


def test_surviving_entries_are_returned_untouched_and_the_input_is_not_mutated() -> None:
    population = _mixed_population()
    snapshot = copy.deepcopy(population)

    survivors = apply_precedence(population)

    assert population == snapshot
    for item in survivors:
        assert any(item is original for original in population)


def test_output_is_sorted_by_the_harness_key() -> None:
    survivors = apply_precedence(_mixed_population())
    keys = [conflict_key(item) for item in survivors]
    assert keys == sorted(keys)


def test_empty_input_is_empty_output() -> None:
    assert apply_precedence([]) == []


def test_entries_may_be_objects_as_well_as_mappings() -> None:
    class Detected:
        def __init__(self, conflict_type: str, refs: tuple[str, ...]) -> None:
            self.type = conflict_type
            self.entity_refs = list(refs)
            self.disagreeing_fields: list[str] = []

    c8 = Detected("C8", conflict_refs("C8", identity_refs=[STUDENT_A]))
    c1 = Detected("C1", conflict_refs("C1", identity_refs=[STUDENT_A]))

    survivors = apply_precedence([c8, c1])

    assert survivors == [c8]


def test_entries_without_the_required_fields_are_refused() -> None:
    with pytest.raises(ValueError, match="no `type`"):
        apply_precedence([{"entity_refs": []}])
    with pytest.raises(ValueError, match="no `entity_refs`"):
        apply_precedence([{"type": "C1"}])


# ------------------------------------------------------------------ the table itself


def test_precedence_table_covers_all_eleven_committed_rules() -> None:
    assert [rule.index for rule in PRECEDENCE] == list(range(1, 12))
    kinds = {rule.index: rule.kind for rule in PRECEDENCE}
    assert kinds[1] == "partition"
    assert all(kinds[i] == "suppress" for i in range(2, 9))
    assert all(kinds[i] == "invariant" for i in (9, 10, 11))


def test_mechanical_suppressions_are_rules_2_and_4_to_8() -> None:
    """SS5.7(10): pairs removed by these never enter `compound_with` (`G32`)."""
    assert MECHANICAL_SUPPRESSIONS == (2, 4, 5, 6, 7, 8)


def test_rules_expected_to_fire_zero_times_are_marked() -> None:
    zero = {rule.index for rule in PRECEDENCE if rule.expected_fire_count == 0}
    assert zero == {6, 7}


def test_unique_conflict_keys_are_asserted_not_deduped() -> None:
    """SS5.7(11): the manifest self-check fails on a duplicate key."""
    population = _mixed_population()
    assert_unique_conflict_keys(apply_precedence(population))

    duplicate = entry("C3", conflict_refs("C3", contact_refs=[CONTACT_B, CONTACT_A]))
    with pytest.raises(ValueError, match="duplicate conflict keys"):
        assert_unique_conflict_keys([*population, duplicate])


# =====================================================================================
# SS5.7 ruling 9 -- the matching predicate is `entity_refs` set INTERSECTION
# =====================================================================================


def test_suppression_fires_on_a_PARTIAL_ref_overlap_not_on_ref_set_equality() -> None:
    """SS5.7 ruling 9. Equality would fail immediately: C7 carries
    `identity refs + appdb:enrollment:<id>` while C13 carries
    `identity refs + payment ref + enrollment ref` (SS5.5), so the two ref sets are
    never equal and rule 5 would never fire -- leaving the C7 population at 400 against
    a golden count of 300, i.e. 100 false positives.
    """
    c13 = {
        "type": "C13",
        "entity_refs": [
            "appdb:student:s1",
            "payments:payment:pi_1",
            "appdb:enrollment:e1",
        ],
        "disagreeing_fields": [],
    }
    c7 = {
        "type": "C7",
        "entity_refs": ["appdb:student:s1", "appdb:enrollment:e1"],
        "disagreeing_fields": [],
    }
    assert set(c13["entity_refs"]) != set(c7["entity_refs"])  # equality would not fire
    assert set(c13["entity_refs"]) & set(c7["entity_refs"])  # intersection does
    survivors = apply_precedence([c13, c7])
    assert [entry["type"] for entry in survivors] == ["C13"]


def test_a_single_shared_ref_is_enough_and_zero_shared_refs_is_not() -> None:
    """Intersection, exactly: one ref in common suppresses; none in common does not.
    This is the same predicate SS8 uses to flag a clean-sample entity, which is what
    keeps the suppression count and the false-positive count consistent."""
    c5 = {"type": "C5", "entity_refs": ["appdb:student:s1"], "disagreeing_fields": []}
    overlapping = {
        "type": "C7",
        "entity_refs": ["appdb:student:s1", "appdb:enrollment:e1"],
        "disagreeing_fields": [],
    }
    disjoint = {
        "type": "C7",
        "entity_refs": ["appdb:student:s2", "appdb:enrollment:e2"],
        "disagreeing_fields": [],
    }
    survivors = apply_precedence([c5, overlapping, disjoint])
    assert sorted(entry["entity_refs"][0] for entry in survivors if entry["type"] == "C7") == [
        "appdb:student:s2"
    ]
    assert len(survivors) == 2


def test_rule_2_is_keyed_ONLY_on_the_collapsed_contact_ref_never_the_student_refs() -> None:
    """SS5.7 ruling 9, second half. C10's `entity_refs` are exactly three (SS5.5): the
    collapsed contact and TWO DIFFERENT students' refs.

    Student B is a person the collapse did not damage -- it retains its own separate
    linked contact (`G21`) and its conflicts are ordinary golden entries. Letting the
    student refs into `winner_refs` suppresses them, which is a false-negative machine.
    This is why rule 2 alone carries a ref-class filter while rules 3-8 take the
    winner's whole ref set.
    """
    c10 = {
        "type": "C10",
        "entity_refs": ["crm:contact:CRM-1", "appdb:student:sA", "appdb:student:sB"],
        "disagreeing_fields": [],
    }
    on_the_contact = {
        "type": "C6",
        "entity_refs": ["crm:contact:CRM-1", "appdb:student:sA"],
        "disagreeing_fields": ["crm.contact.grade", "appdb.student.grade"],
    }
    on_student_b_only = {
        "type": "C6",
        "entity_refs": ["crm:contact:CRM-9", "appdb:student:sB"],
        "disagreeing_fields": ["crm.contact.grade", "appdb.student.grade"],
    }
    survivors = apply_precedence([c10, on_the_contact, on_student_b_only])
    types_and_refs = {(entry["type"], tuple(sorted(entry["entity_refs"]))) for entry in survivors}
    assert ("C10", tuple(sorted(c10["entity_refs"]))) in types_and_refs
    # student B's own C6 SURVIVES -- it shares only a STUDENT ref with the C10
    assert ("C6", tuple(sorted(on_student_b_only["entity_refs"]))) in types_and_refs
    # ...while the one sharing the COLLAPSED CONTACT ref is suppressed
    assert ("C6", tuple(sorted(on_the_contact["entity_refs"]))) not in types_and_refs


def test_the_ref_class_filter_is_declared_on_rule_2_and_on_no_other_rule() -> None:
    for rule in PRECEDENCE:
        if rule.index == 2:
            assert rule.winner_ref_prefix == "crm:contact:"
        else:
            assert rule.winner_ref_prefix is None


# =====================================================================================
# SS5.7 -- RULE ISOLATION: every rule bound on its own, never by accident of an
# earlier rule firing first
# =====================================================================================
#
# A precedence test is only evidence for the rule it names if that rule is the one that
# did the work. The rule-2 test above supplies a C14 that CO-OCCURS with a mixed C6 on
# the same refs -- so rule 1 (the C14/C6 partition) drops the C14 before rule 2 is ever
# reached, and `C14` could be deleted from rule 2's `losers` set with the whole suite
# still green. §5.6 (C10) and `G21` depend on that clause: it is what suppresses the 50
# mechanically-induced C14 entries on a C10 person against a C14 budget of exactly 50
# that contains none of them.
#
# Each test below feeds a population in which exactly ONE rule can act, and asserts the
# `report` shows exactly that one rule firing.


def _only_rule_fired(report: dict[int, int], index: int, count: int = 1) -> None:
    """Assert `index` fired `count` times and every other rule fired zero times."""
    assert report[index] == count, report
    assert {rule: fired for rule, fired in report.items() if fired} == {index: count}, report


# --------------------------------------------------------------- rule 1, in isolation


def test_rule_1_in_isolation_keeps_the_c14_for_a_wholly_sensitive_set() -> None:
    refs = person(STUDENT_A, CONTACT_A)
    report: dict[int, int] = {}
    survivors = apply_precedence(
        [entry("C6", refs, NAME_PATHS), entry("C14", refs, NAME_PATHS)], report=report
    )
    assert types(survivors) == ["C14"]
    _only_rule_fired(report, 1)


def test_rule_1_in_isolation_keeps_the_c6_for_a_mixed_set() -> None:
    refs = person(STUDENT_A, CONTACT_A)
    report: dict[int, int] = {}
    survivors = apply_precedence(
        [entry("C6", refs, sorted(NAME_PATHS + GRADE_PATHS)), entry("C14", refs, NAME_PATHS)],
        report=report,
    )
    assert types(survivors) == ["C6"]
    _only_rule_fired(report, 1)


def test_rule_1_keeps_the_c6_when_the_co_located_pair_lists_no_paths_at_all() -> None:
    """SS5.5 (C14): "The empty set never fires C14", so the partition's fallback is C6.

    A co-located C6/C14 pair carrying no `disagreeing_fields` is degenerate -- nothing in
    `golden/` is built that way -- but rule 1 is a total function and the fallback it
    lands on is a real decision, not a formality: C14 forces `sensitive_hold` while C6 is
    the auto-apply-eligible type, so keeping the C14 here would hold a proposal on the
    strength of an EMPTY sensitive set. `conflict_type_for_paths` returns `None` for the
    empty set precisely so this choice is made once, by SS5.5's predicate.

    Nothing else in the suite reaches the fallback, and coverage cannot report the gap:
    `coverage.py` measures the `or` expression as one statement/branch pair and does not
    distinguish its short-circuit arms, so `... or "C6"` reads as fully covered while
    only the left arm has ever produced the value.
    """
    refs = person(STUDENT_A, CONTACT_A)
    assert conflict_type_for_paths(()) is None  # the fallback is what decides

    c6 = entry("C6", refs, [])
    c14 = entry("C14", refs, [])
    report: dict[int, int] = {}
    survivors = apply_precedence([c6, c14], report=report)

    assert types(survivors) == ["C6"]
    assert survivors[0] is c6
    _only_rule_fired(report, 1)


def test_rule_1_keeps_the_c6_when_only_one_side_lists_paths() -> None:
    """The union of the pair's paths is what rule 1 classifies (SS5.7(1)), so a C14 that
    lists nothing beside a mixed C6 is still a mixed set -- C6 survives."""
    refs = person(STUDENT_A, CONTACT_A)
    c6 = entry("C6", refs, sorted(NAME_PATHS + GRADE_PATHS))
    c14 = entry("C14", refs, [])
    report: dict[int, int] = {}
    survivors = apply_precedence([c6, c14], report=report)

    assert types(survivors) == ["C6"]
    assert survivors[0] is c6
    _only_rule_fired(report, 1)


# --------------------------------------------------------------- rule 2, in isolation


def _collapsed_c10() -> dict:
    return entry(
        "C10", conflict_refs("C10", contact_refs=[CONTACT_A], student_refs=[STUDENT_A, STUDENT_B])
    )


def test_rule_2_suppresses_a_LONE_C14_sharing_the_collapsed_contact_ref() -> None:
    """SS5.7 rule 2 names **C14** explicitly, and SS5.6 (C10) / `G21` rest on it.

    The C10 contact `L1`-links to student A while its `(first_norm, last_norm, dob_norm)`
    equals student **B**'s, so `name_first`, `name_last` and `dob` necessarily disagree
    on person A. All three paths are in `SENSITIVE_FIELDS`, so §2.4's partition makes the
    disagreeing set wholly sensitive and `R-014` emits a C14 -- **alone**, with no C6
    beside it, because rule 1 only ever fires on a C6/C14 CO-OCCURRENCE. Nothing but rule
    2 can remove it. Without this clause the golden set gains 50 C14 entries against a
    §11.8 budget of exactly 50 that contains none of them.
    """
    c10 = _collapsed_c10()
    lone_c14 = entry("C14", person(STUDENT_A, CONTACT_A), NAME_PATHS)

    report: dict[int, int] = {}
    survivors = apply_precedence([c10, lone_c14], report=report)

    assert types(survivors) == ["C10"]
    assert survivors[0] is c10
    _only_rule_fired(report, 2)
    # rule 1 is NOT what removed it: a lone C14 is never partitioned away
    assert report[1] == 0
    assert types(apply_precedence([lone_c14])) == ["C14"]


@pytest.mark.parametrize(
    ("loser_type", "paths"),
    [("C6", GRADE_PATHS), ("C14", NAME_PATHS), ("C4", None)],
    ids=["C6", "C14", "C4"],
)
def test_rule_2_suppresses_each_of_its_three_losers_on_its_own(
    loser_type: str, paths: list[str] | None
) -> None:
    """Every loser named in rule 2 is bound individually, so none of the three can be
    dropped from the `losers` set while the suite stays green."""
    c10 = _collapsed_c10()
    loser = entry(loser_type, person(STUDENT_A, CONTACT_A), paths)

    report: dict[int, int] = {}
    survivors = apply_precedence([c10, loser], report=report)

    assert types(survivors) == ["C10"]
    _only_rule_fired(report, 2)


# --------------------------------------------------------------- rule 3, in isolation


@pytest.mark.parametrize("loser_type", ["C12", "C11"])
def test_rule_3_suppresses_each_of_its_losers_on_its_own(loser_type: str) -> None:
    c2 = entry("C2", conflict_refs("C2", payment_refs=[PAY_1]))
    if loser_type == "C12":
        loser = entry("C12", conflict_refs("C12", identity_refs=[STUDENT_A], payment_refs=[PAY_1]))
    else:
        loser = entry("C11", conflict_refs("C11", payment_refs=[PAY_1, PAY_2]))

    report: dict[int, int] = {}
    survivors = apply_precedence([c2, loser], report=report)

    assert types(survivors) == ["C2"]
    _only_rule_fired(report, 3)


# --------------------------------------------------------------- rule 4, in isolation


@pytest.mark.parametrize("loser_type", ["C1", "C7"])
def test_rule_4_suppresses_each_of_its_losers_on_its_own(loser_type: str) -> None:
    """The C5-over-C7 half is the live one (400 plants, `G38`); the C5-over-C1 half is
    vacuous in a well-formed dataset but is a committed clause and is bound here."""
    c5 = entry("C5", conflict_refs("C5", identity_refs=[STUDENT_A]))
    if loser_type == "C1":
        loser = entry("C1", conflict_refs("C1", identity_refs=[STUDENT_A]))
    else:
        loser = entry(
            "C7", conflict_refs("C7", identity_refs=[STUDENT_A], enrollment_refs=[ENROLL_A])
        )

    report: dict[int, int] = {}
    survivors = apply_precedence([c5, loser], report=report)

    assert types(survivors) == ["C5"]
    _only_rule_fired(report, 4)


# --------------------------------------------------------------- rule 5, in isolation


def test_rule_5_in_isolation_c13_suppresses_c7() -> None:
    c13 = entry(
        "C13",
        conflict_refs(
            "C13", identity_refs=[STUDENT_A], payment_refs=[PAY_1], enrollment_refs=[ENROLL_A]
        ),
    )
    c7 = entry("C7", conflict_refs("C7", identity_refs=[STUDENT_A], enrollment_refs=[ENROLL_A]))

    report: dict[int, int] = {}
    survivors = apply_precedence([c13, c7], report=report)

    assert types(survivors) == ["C13"]
    _only_rule_fired(report, 5)


# --------------------------------------------------------------- rule 6, in isolation


def test_rule_6_in_isolation_c9_suppresses_c1() -> None:
    c9 = entry("C9", conflict_refs("C9", identity_refs=[STUDENT_A], enrollment_refs=[ENROLL_A]))
    c1 = entry("C1", conflict_refs("C1", identity_refs=[STUDENT_A]))

    report: dict[int, int] = {}
    survivors = apply_precedence([c9, c1], report=report)

    assert types(survivors) == ["C9"]
    _only_rule_fired(report, 6)


# --------------------------------------------------------------- rule 7, in isolation


def test_rule_7_in_isolation_c10_suppresses_c5() -> None:
    c10 = _collapsed_c10()
    c5 = entry("C5", conflict_refs("C5", identity_refs=[STUDENT_B]))

    report: dict[int, int] = {}
    survivors = apply_precedence([c10, c5], report=report)

    assert types(survivors) == ["C10"]
    _only_rule_fired(report, 7)


# --------------------------------------------------------------- rule 8, in isolation


@pytest.mark.parametrize("loser_type", ["C1", "C7"])
def test_rule_8_suppresses_each_of_its_losers_on_its_own(loser_type: str) -> None:
    c8 = entry("C8", conflict_refs("C8", identity_refs=[STUDENT_A]))
    if loser_type == "C1":
        loser = entry("C1", conflict_refs("C1", identity_refs=[STUDENT_A]))
    else:
        loser = entry(
            "C7", conflict_refs("C7", identity_refs=[STUDENT_A], enrollment_refs=[ENROLL_A])
        )

    report: dict[int, int] = {}
    survivors = apply_precedence([c8, loser], report=report)

    assert types(survivors) == ["C8"]
    _only_rule_fired(report, 8)


# ------------------------------------------------------- the committed rule table rows


def test_every_suppression_rule_declares_its_committed_winner_and_losers() -> None:
    """SS5.7 rules 2-8, restated literally from the contract.

    A losers set is a committed clause, not an implementation detail: dropping `C14`
    from rule 2 changes 50 golden entries and dropping `C7` from rule 4 changes 400.
    """
    declared = {
        rule.index: (rule.winner, tuple(sorted(rule.losers)))
        for rule in PRECEDENCE
        if rule.kind == "suppress"
    }
    assert declared == {
        2: ("C10", ("C14", "C4", "C6")),
        3: ("C2", ("C11", "C12")),
        4: ("C5", ("C1", "C7")),
        5: ("C13", ("C7",)),
        6: ("C9", ("C1",)),
        7: ("C10", ("C5",)),
        8: ("C8", ("C1", "C7")),
    }


def test_rule_1_is_the_c14_over_c6_partition() -> None:
    rule = PRECEDENCE[0]
    assert (rule.index, rule.kind, rule.winner, rule.losers) == (
        1,
        "partition",
        "C14",
        frozenset({"C6"}),
    )


# =====================================================================================
# SS5.4 / SS5.7(11) -- the harness match key SORTS `entity_refs`
# =====================================================================================


def test_conflict_key_sorts_the_refs_it_is_handed() -> None:
    """`conflict_key` is the key the harness matches a detection to a golden entry on.

    Every other test feeds refs straight out of `conflict_refs`, which returns them
    already sorted -- so the `sorted()` inside `_entry_sort_key` is never exercised and
    could be deleted with the suite green. A detector that assembles `entity_refs` in
    predicate order (C10's three refs are naturally contact-then-students, §5.5) would
    then miss its golden entry: one false negative AND one false positive per conflict.
    """
    permuted = {
        "type": "C10",
        "entity_refs": ["appdb:student:sB", "crm:contact:CRM-1", "appdb:student:sA"],
        "disagreeing_fields": [],
    }
    assert conflict_key(permuted) == (
        "C10",
        ("appdb:student:sA", "appdb:student:sB", "crm:contact:CRM-1"),
    )
    assert conflict_key(permuted) != ("C10", tuple(permuted["entity_refs"]))


def test_the_same_ref_set_in_two_orders_is_ONE_key_not_two() -> None:
    """SS5.7(11): `(type, tuple(sorted(entity_refs)))` is UNIQUE across
    `golden/conflicts.json`, and the self-check fails on a duplicate rather than letting
    the loader dedupe it. Unsorted refs would make the same pair of contacts two
    distinct keys and the duplicate would sail through."""
    forward = {
        "type": "C3",
        "entity_refs": [CONTACT_A, CONTACT_B],
        "disagreeing_fields": [],
    }
    reversed_refs = {
        "type": "C3",
        "entity_refs": [CONTACT_B, CONTACT_A],
        "disagreeing_fields": [],
    }
    assert conflict_key(forward) == conflict_key(reversed_refs)
    with pytest.raises(ValueError, match="duplicate conflict keys"):
        assert_unique_conflict_keys([forward, reversed_refs])


def test_output_order_uses_the_SORTED_refs_not_the_order_they_arrived_in() -> None:
    """`apply_precedence` sorts by `(type, tuple(sorted(entity_refs)))` -- SS8's order for
    `golden/conflicts.json`. These two entries come out in the opposite order if the
    refs are compared in arrival order."""
    first = {
        "type": "C7",
        "entity_refs": ["appdb:student:s2", "appdb:enrollment:e1"],
        "disagreeing_fields": [],
    }
    second = {
        "type": "C7",
        "entity_refs": ["appdb:student:s1", "appdb:enrollment:e9"],
        "disagreeing_fields": [],
    }
    assert tuple(first["entity_refs"]) > tuple(second["entity_refs"])  # arrival order
    assert tuple(sorted(first["entity_refs"])) < tuple(sorted(second["entity_refs"]))

    survivors = apply_precedence([second, first])
    assert [item["entity_refs"] for item in survivors] == [
        first["entity_refs"],
        second["entity_refs"],
    ]
