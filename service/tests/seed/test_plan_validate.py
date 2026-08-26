"""`Plan.validate()` -- twenty-odd assertions no test had ever seen raise (SS11).

`build_plan` is the only caller, and it calls `validate()` on an allocation it has
just derived from the contract's own arithmetic. So on every run of the suite, every
`need(...)` in that method was satisfied by construction, and a `need` whose condition
had been inverted, or whose message named the wrong identity, would have gone on
passing silently. The method's own docstring makes the claim this file has to hold up:
"a profile whose scaling makes the allocation unsatisfiable fails **here** -- before a
single record is materialised -- instead of surfacing as a mystery count at manifest
time."

Each test breaks exactly one identity of a **real** plan with `dataclasses.replace`,
so what is under test is the assertion and not a hand-built object that was never
plausible. `Plan` is frozen, `replace` re-runs `__init__` and not `validate`, and every
derived property (`students`, `tri_children`, `contact_present_children`, ...) is
recomputed from the field that moved -- which is what makes a one-field edit reach the
identity it is supposed to reach.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from recon.reference import CONFLICT_MINIMUMS
from recon.seed.plan import PROFILES, HouseholdMix, Plan, build_plan


@pytest.fixture(params=PROFILES)
def plan(request: pytest.FixtureRequest) -> Plan:
    """A real, satisfiable allocation. `build_plan` already validated it."""
    return build_plan(request.param)


# ---------------------------------------------------------------------------
# the happy path, stated so the failures below mean something
# ---------------------------------------------------------------------------


def test_both_committed_profiles_validate(plan: Plan) -> None:
    """SS9: `dev` is the same code path at 1/20 scale, so both must close."""
    plan.validate()


def test_an_unknown_profile_is_refused_before_anything_is_derived() -> None:
    with pytest.raises(ValueError, match="unknown profile"):
        build_plan("not-a-profile")


# ---------------------------------------------------------------------------
# one broken identity at a time
# ---------------------------------------------------------------------------


def test_a_student_population_that_does_not_sum_raises(plan: Plan) -> None:
    """SS11.2: tri + {appdb,crm} + {appdb}-only must be exactly the A.1 volume."""
    broken = replace(plan, appdb_only=plan.appdb_only + 1)
    with pytest.raises(ValueError, match=r"SS11\.2 students") as excinfo:
        broken.validate()
    assert "not simultaneously satisfiable" in str(excinfo.value)
    assert plan.profile in str(excinfo.value)


def test_a_contact_population_that_does_not_sum_raises(plan: Plan) -> None:
    """SS11.4: contacts absorb their slack in the lead count, so leads is the lever."""
    with pytest.raises(ValueError, match=r"SS11\.4 contacts"):
        replace(plan, leads=plan.leads + 5).validate()


def test_a_non_positive_lead_count_raises(plan: Plan) -> None:
    """A profile with no deal-less leads has no C3 population to draw from."""
    with pytest.raises(ValueError, match=r"SS11\.4 lead count is not positive"):
        replace(plan, leads=0).validate()


def test_a_deal_population_that_does_not_sum_raises(plan: Plan) -> None:
    with pytest.raises(ValueError, match=r"SS11\.7 deals"):
        replace(plan, partial_crm_deal_households=plan.partial_crm_deal_households + 3).validate()


def test_an_enrollment_population_that_does_not_sum_raises(plan: Plan) -> None:
    with pytest.raises(ValueError, match=r"SS11\.5 enrollments"):
        replace(plan, appdb_only_enrollments=plan.appdb_only_enrollments + 7).validate()


def test_a_payment_population_that_does_not_sum_raises(plan: Plan) -> None:
    with pytest.raises(ValueError, match=r"SS11\.6 payments"):
        replace(plan, fee_deposit_persons=plan.fee_deposit_persons + 2).validate()


def test_an_empty_base_paid_population_raises(plan: Plan) -> None:
    """`base_paid_dt` is the population every unplanted payment comes from."""
    with pytest.raises(ValueError, match="base paid deposit/tuition"):
        replace(plan, base_paid_dt=0).validate()


def test_more_appdb_only_enrollments_than_appdb_only_students_raises(plan: Plan) -> None:
    """SS11.5: an enrollment needs a student, so the split cannot exceed the pool."""
    with pytest.raises(ValueError, match=r"more \{appdb\}-only enrollments than"):
        replace(
            plan,
            appdb_only=plan.appdb_only_enrollments - 1,
            appdb_only_enrollments=plan.appdb_only_enrollments,
        ).validate()


def test_too_few_appdb_only_enrollments_to_carry_the_c5_plants_raises(plan: Plan) -> None:
    """C5 is scoped to `{appdb}`-only enrollments; fewer than the plants is unplantable."""
    with pytest.raises(ValueError, match=r"not enough \{appdb\}-only enrollments"):
        replace(plan, appdb_only_enrollments=plan.conflicts["C5"] - 1).validate()


def test_a_tri_source_fraction_outside_the_band_raises(plan: Plan) -> None:
    """SS12 D-5 pins the fraction to [0.68, 0.72]; this moves it out on purpose.

    Shrinking the tri-source household mix moves `tri_children` and therefore the
    fraction. Other identities go red at the same time -- deliberately, because it is
    the accumulation that is under test in `test_every_broken_identity_is_reported`;
    here it is enough that the band's own message is one of them.
    """
    small = HouseholdMix(two=1, three=1, four=1, single=1)
    with pytest.raises(ValueError, match="tri_source_student_fraction"):
        replace(plan, tri=small).validate()


def test_a_conflict_class_below_its_floor_raises(plan: Plan) -> None:
    """A.4: every class has a minimum, scaled for `dev` and exact for `full`."""
    starved = dict(plan.conflicts)
    starved["C4"] = 0
    with pytest.raises(ValueError, match=r"A\.4 C4 planned 0 below floor"):
        replace(plan, conflicts=starved).validate()


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("c6_grade", r"SS5\.6 C6 composition"),
        ("c14_name", r"SS5\.6 C14 composition"),
        ("c8_crm", r"SS5\.6 C8 composition"),
        ("c9_missing", r"SS5\.6 C9 composition"),
    ),
)
def test_a_conflict_composition_that_does_not_sum_raises(
    plan: Plan, field: str, message: str
) -> None:
    """Each planted class is split into branches, and the branches must total the class."""
    with pytest.raises(ValueError, match=message):
        replace(plan, **{field: getattr(plan, field) + 1}).validate()


def test_more_c8_plants_than_tri_source_multi_child_households_raises(plan: Plan) -> None:
    """SS11.8: a C8 drop needs a multi-child tri-source household to drop from."""
    conflicts = dict(plan.conflicts)
    conflicts["C8"] = plan.tri.multi + 1
    broken = replace(
        plan,
        conflicts=conflicts,
        c8_crm=conflicts["C8"] - conflicts["C8"] // 2,
        c8_payments=conflicts["C8"] // 2,
    )
    with pytest.raises(ValueError, match="not enough tri-source multi-child households"):
        broken.validate()


# ---------------------------------------------------------------------------
# the report, not just the first failure
# ---------------------------------------------------------------------------


def test_every_broken_identity_is_reported_not_just_the_first(plan: Plan) -> None:
    """`validate` collects problems and raises once, so a bad profile is diagnosable.

    Two independent breakages, and both messages have to be in the exception -- an
    implementation that returned at the first `need` would pass every test above and
    still make a real profile take several runs to fix.
    """
    broken = replace(plan, leads=0, base_paid_dt=0)
    with pytest.raises(ValueError) as excinfo:
        broken.validate()
    message = str(excinfo.value)
    assert "SS11.4 contacts" in message
    assert "SS11.4 lead count is not positive" in message
    assert "SS11.6 base paid deposit/tuition" in message
    assert message.count("\n  - ") >= 3


def test_the_floor_message_names_the_class_and_both_numbers(plan: Plan) -> None:
    """A diagnostic that does not say what was wrong costs a debugging round trip."""
    starved = dict(plan.conflicts)
    starved["C11"] = 1
    with pytest.raises(ValueError) as excinfo:
        replace(plan, conflicts=starved).validate()
    message = str(excinfo.value)
    floor = CONFLICT_MINIMUMS["C11"] if plan.profile == "full" else min(CONFLICT_MINIMUMS["C11"], 5)
    assert f"A.4 C11 planned 1 below floor {floor}" in message
