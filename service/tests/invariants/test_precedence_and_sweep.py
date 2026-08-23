"""SS9.1(b)'s construction sweep, seen from the detector side.

The table below SS9.1(b) pins, per conflict class, the **raw** population over every
generation-3 entity and what survives `PRECEDENCE`. It is the generator's self-check;
this asserts the *detector* lands on the same two columns. A surplus of one in the raw
column is a construction bug on the generator side and a false positive on this side,
and the raw/surviving split is what proves the suppression is doing the work rather
than the predicate being quietly narrowed to fit.
"""

from __future__ import annotations

from recon.reference import PRECEDENCE, apply_precedence

#: SS9.1(b), "asserted raw count" -- over EVERY generation-3 entity.
RAW_SWEEP = {
    "C1": 575,
    "C2": 200,
    "C3": 300,
    "C4": 250,
    "C5": 400,
    "C6": 500,
    "C7": 875,
    "C8": 150,
    "C9": 100,
    "C10": 50,
    "C11": 50,
    "C12": 100,
    "C13": 100,
    "C14": 100,
}

#: SS9.1(b), "survives `PRECEDENCE`".
SURVIVING = {**RAW_SWEEP, "C1": 500, "C7": 300, "C14": 50}


def _by_type(conflicts) -> dict[str, int]:
    counts: dict[str, int] = {}
    for conflict in conflicts:
        counts[conflict.type] = counts.get(conflict.type, 0) + 1
    return dict(sorted(counts.items()))


def test_raw_detection_matches_the_construction_sweep(invariant_run) -> None:
    """Before `PRECEDENCE`: 575 C1, 875 C7, 100 C14 -- the mechanically-implied
    conflicts SS12 D-6 and D-2 say are constructible only as overlaps.

    **This assertion is the ONLY grade on `R-006`'s C6/C14 sensitivity partition.**
    Removing `AND NOT rolled.wholly_sensitive` from `rules/006_field_disagreement.v1.sql`
    raises raw C6 from 500 to 600 -- the 100 wholly-sensitive persons -- but
    `PRECEDENCE` rule 1 (C14 over C6) and rule 2 (C10 over C6/C14) then remove all
    100, so the SURVIVING set is unchanged and the golden diff stays at 0 FN / 0 FP.
    The engine's answer is correct either way; the SQL partition is defence in depth.
    It is graded here, through `raw_conflicts`, and nowhere else -- which is why this
    test asserts the whole raw dict rather than a total.
    """
    assert _by_type(invariant_run.raw_conflicts) == dict(sorted(RAW_SWEEP.items()))
    # Stated separately from the dict comparison above so a future edit that relaxes
    # the dict cannot quietly take these two with it.
    assert _by_type(invariant_run.raw_conflicts)["C6"] == 500
    assert _by_type(invariant_run.raw_conflicts)["C14"] == 100


def test_surviving_detection_matches_the_precedence_column(invariant_run) -> None:
    assert _by_type(invariant_run.conflicts) == dict(sorted(SURVIVING.items()))


def test_precedence_removes_exactly_the_contract_populations(invariant_run) -> None:
    """C1 575->500 (rule 8, the 75 C8 `crm`-drops), C7 875->300 (rules 4, 5, 8),
    C14 100->50 (rule 2, the 50 C10-induced)."""
    raw, surviving = _by_type(invariant_run.raw_conflicts), _by_type(invariant_run.conflicts)
    assert raw["C1"] - surviving["C1"] == 75
    assert raw["C7"] - surviving["C7"] == 575
    assert raw["C14"] - surviving["C14"] == 50


def test_rules_six_and_seven_fire_zero_times(invariant_run) -> None:
    """SS9.1(b): `PRECEDENCE` 6 (C9 over C1) and 7 (C10 over C5) are vacuous under
    `G9`/`G21`; a non-zero count is a construction bug, not a legitimate overlap."""
    report: dict[int, int] = {}
    apply_precedence(invariant_run.raw_conflicts, report=report)
    assert report[6] == 0
    assert report[7] == 0


def test_the_runner_uses_the_committed_precedence_function(invariant_run) -> None:
    """SS5.7(10)/`G32`: `golden/` is written through the SAME filter the detector
    applies. Re-running it over the detector's own output is a fixed point."""
    again = apply_precedence(invariant_run.raw_conflicts)
    assert [conflict.key for conflict in again] == [
        conflict.key for conflict in invariant_run.conflicts
    ]
    assert len(PRECEDENCE) == 11
