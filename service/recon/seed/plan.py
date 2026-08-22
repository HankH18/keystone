"""SS11's worked volume allocation, parameterised by profile (SS9).

Every number in SS11 is derived here rather than hard-coded, and the derivation is
the contract's own arithmetic read forwards:

* the household/presence mix fixes the tri-source student fraction (SS11.3);
* **contacts** absorb their slack in the deal-less lead count (SS11.4);
* **enrollments** absorb theirs in the partial-presence enrollment split (SS11.5);
* **payments** absorb theirs in the fee+deposit second-payment count (SS11.6);
* **deals** absorb theirs in the `{appdb, crm}` deal-bearing household count (SS11.7).

`Plan.validate()` re-asserts every identity, so a profile whose scaling makes the
allocation unsatisfiable fails here -- before a single record is materialised --
instead of surfacing as a mystery count at manifest time.

`--profile full` reproduces SS11 exactly; `--profile dev` is the same code path at
one twentieth the volume (SS9).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from recon.reference import A1_VOLUMES, CONFLICT_MINIMUMS

__all__ = ["PROFILES", "Plan", "build_plan"]

#: SS9 -- the two profiles. `full` is the graded 120,000-record Appendix-A dataset;
#: `dev` is the same code path at 1/20 scale (~6,000 records) for the inner loop.
PROFILES: tuple[str, ...] = ("dev", "full")

_DEV_DIVISOR = 20

#: SS9 -- the per-class floor that keeps all fourteen classes exercised in `dev`.
_CONFLICT_FLOOR = 5

#: SS12 D-5 -- the pinned tri-source student fraction. 0.710 is the constructive
#: ceiling (every tri-source child costs one payment record); 0.690 leaves margin
#: against the asserted [0.68, 0.72] band on both sides.
TRI_SOURCE_FRACTION = 0.690


def _round(value: float) -> int:
    """Half-up rounding. Deterministic and independent of `round()`'s half-to-even."""
    return int(value + 0.5)


@dataclass(frozen=True)
class HouseholdMix:
    """Household counts for one presence bucket (SS11.2, SS11.3)."""

    two: int
    three: int
    four: int
    single: int

    @property
    def multi(self) -> int:
        return self.two + self.three + self.four

    @property
    def households(self) -> int:
        return self.multi + self.single

    @property
    def multi_children(self) -> int:
        return 2 * self.two + 3 * self.three + 4 * self.four

    @property
    def children(self) -> int:
        return self.multi_children + self.single


@dataclass(frozen=True)
class Plan:
    """The whole SS11 allocation for one profile."""

    profile: str
    scale: float
    volumes: dict[tuple[str, str], int]
    conflicts: dict[str, int]

    tri: HouseholdMix
    partial_crm: HouseholdMix  # `{appdb, crm}`
    appdb_only: int  # single-child `{appdb}`-only households

    leads: int
    partial_crm_deal_households: int
    partial_crm_enrollments: int
    appdb_only_enrollments: int

    # -- payments composition (SS11.6) -----------------------------------------------
    fee_deposit_persons: int
    refund_supersede: int  # `G14` shape (i)
    refund_closed: int  # `G14` shape (ii)
    base_paid_dt: int

    # -- conflict composition ----------------------------------------------------------
    c6_grade: int
    c6_lifecycle: int
    c6_mixed: int
    c14_name: int
    c14_dob: int
    c14_stage: int
    c8_crm: int
    c8_payments: int
    c9_missing: int
    c9_other_person: int

    compounds: dict[str, int] = field(default_factory=dict)

    # -- structural minimums that SS9 does not scale ------------------------------------
    malformed_cases: int = 24
    oscillating_fields: int = 25

    #: SS5.5 C11 / `G7` -- two-child tri-source households whose two base `deposit`
    #: payments deliberately share `(payer_email_norm, amount_cents, type)` INSIDE
    #: C11's 600s window. They are the FP-guard population for C11's "both resolve to
    #: the SAME person" clause and cost **no** extra payment record: the two base
    #: payments already exist, only their type and spacing change.
    sibling_window_households: int = 0

    # ---------------------------------------------------------------------------------
    @property
    def tri_children(self) -> int:
        """Tri-source children **before** the C8 drops (SS11.3)."""
        return self.tri.children

    @property
    def students(self) -> int:
        return self.volumes[("appdb", "student")]

    @property
    def payments_present_children(self) -> int:
        return self.tri_children - self.c8_payments

    @property
    def contact_present_children(self) -> int:
        return self.tri_children - self.c8_crm

    @property
    def tri_source_student_fraction(self) -> float:
        return (self.tri_children - self.c8_crm - self.c8_payments) / self.students

    @property
    def golden_entry_total(self) -> int:
        return sum(self.conflicts.values())

    # ---------------------------------------------------------------------------------
    def validate(self) -> None:
        """Re-assert every SS11 identity. A profile that cannot close fails here."""
        problems: list[str] = []

        def need(condition: bool, message: str) -> None:
            if not condition:
                problems.append(message)

        students = self.tri.children + self.partial_crm.children + self.appdb_only
        need(
            students == self.students,
            f"SS11.2 students {students} != A.1 volume {self.students}",
        )

        contacts = (
            self.contact_present_children
            + self.partial_crm.children
            + self.conflicts["C3"]
            + self.leads
        )
        need(
            contacts == self.volumes[("crm", "contact")],
            f"SS11.4 contacts {contacts} != A.1 volume {self.volumes[('crm', 'contact')]}",
        )

        deals = (
            self.tri.multi
            + (self.tri.single - self.conflicts["C1"])
            + self.partial_crm_deal_households
        )
        need(
            deals == self.volumes[("crm", "deal")],
            f"SS11.7 deals {deals} != A.1 volume {self.volumes[('crm', 'deal')]}",
        )

        enrollments = self.tri_children + self.partial_crm_enrollments + self.appdb_only_enrollments
        need(
            enrollments == self.volumes[("appdb", "enrollment")],
            f"SS11.5 enrollments {enrollments} != A.1 volume "
            f"{self.volumes[('appdb', 'enrollment')]}",
        )

        payments = (
            self.payments_present_children
            + self.conflicts["C2"]
            + self.conflicts["C11"]
            + self.refund_supersede
            + self.fee_deposit_persons
        )
        need(
            payments == self.volumes[("payments", "payment")],
            f"SS11.6 payments {payments} != A.1 volume {self.volumes[('payments', 'payment')]}",
        )

        base = (
            self.payments_present_children
            - self.conflicts["C7"]
            - self.fee_deposit_persons
            - self.conflicts["C13"]
            - self.refund_closed
            - self.refund_supersede
        )
        need(
            base == self.base_paid_dt,
            f"SS11.6 base paid deposit/tuition {base} != {self.base_paid_dt}",
        )
        need(self.base_paid_dt > 0, "SS11.6 base paid deposit/tuition population is empty")

        need(
            self.partial_crm_enrollments <= self.partial_crm.children,
            "SS11.5 more {appdb,crm} enrollments than {appdb,crm} students",
        )
        need(
            self.appdb_only_enrollments <= self.appdb_only,
            "SS11.5 more {appdb}-only enrollments than {appdb}-only students",
        )
        need(
            self.appdb_only_enrollments >= self.conflicts["C5"],
            "SS11.5 not enough {appdb}-only enrollments to carry the C5 plants",
        )
        need(
            self.partial_crm_deal_households <= self.partial_crm.households,
            "SS11.7 more deal-bearing {appdb,crm} households than exist",
        )
        need(self.leads > 0, "SS11.4 lead count is not positive")

        reserved = (
            self.conflicts["C1"]
            + self.conflicts["C7"]
            + self.conflicts["C13"]
            + self.refund_supersede
            + self.refund_closed
            + self.fee_deposit_persons
        )
        need(
            reserved <= self.tri.single,
            f"SS11.6 tri-source single-child households ({self.tri.single}) cannot carry "
            f"the {reserved} single-child-only roles",
        )
        need(
            self.conflicts["C8"] <= self.tri.multi,
            "SS11.8 not enough tri-source multi-child households for the C8 plants",
        )

        fraction = self.tri_source_student_fraction
        need(
            0.68 <= fraction <= 0.72,
            f"SS11.3 tri_source_student_fraction {fraction:.4f} outside [0.68, 0.72]",
        )

        need(
            self.c6_grade + self.c6_lifecycle + self.c6_mixed == self.conflicts["C6"],
            "SS5.6 C6 composition does not sum to the C6 count",
        )
        need(
            self.c14_name + self.c14_dob + self.c14_stage == self.conflicts["C14"],
            "SS5.6 C14 composition does not sum to the C14 count",
        )
        need(
            self.c8_crm + self.c8_payments == self.conflicts["C8"],
            "SS5.6 C8 composition does not sum to the C8 count",
        )
        need(
            self.c9_missing + self.c9_other_person == self.conflicts["C9"],
            "SS5.6 C9 composition does not sum to the C9 count",
        )

        for conflict_type, minimum in CONFLICT_MINIMUMS.items():
            planned = self.conflicts[conflict_type]
            floor = minimum if self.profile == "full" else min(minimum, _CONFLICT_FLOOR)
            need(
                planned >= floor,
                f"A.4 {conflict_type} planned {planned} below floor {floor}",
            )

        if problems:
            raise ValueError(
                "SS11 allocation is not simultaneously satisfiable for profile "
                f"{self.profile!r}:\n  - " + "\n  - ".join(problems)
            )


def _scaled_conflicts(scale: float, profile: str) -> dict[str, int]:
    if profile == "full":
        return dict(CONFLICT_MINIMUMS)
    return {
        conflict_type: max(_CONFLICT_FLOOR, _round(minimum * scale))
        for conflict_type, minimum in CONFLICT_MINIMUMS.items()
    }


def build_plan(profile: str) -> Plan:
    """Derive the whole SS11 allocation for `profile`, then `validate()` it."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {PROFILES}")

    divisor = 1 if profile == "full" else _DEV_DIVISOR
    volumes = {key: value // divisor for key, value in A1_VOLUMES.items()}
    students = volumes[("appdb", "student")]
    scale = students / A1_VOLUMES[("appdb", "student")]

    conflicts = _scaled_conflicts(scale, profile)

    c8_total = conflicts["C8"]
    c8_crm = c8_total - c8_total // 2
    c8_payments = c8_total // 2

    c9_total = conflicts["C9"]
    c9_missing = c9_total - c9_total // 2
    c9_other = c9_total // 2

    c6_total = conflicts["C6"]
    c6_grade = _round(c6_total * 0.60)
    c6_lifecycle = _round(c6_total * 0.24)
    c6_mixed = c6_total - c6_grade - c6_lifecycle

    c14_total = conflicts["C14"]
    c14_name = _round(c14_total * 0.60)
    c14_dob = _round(c14_total * 0.20)
    c14_stage = c14_total - c14_name - c14_dob

    tri = HouseholdMix(
        two=_round(1830 * scale),
        three=_round(550 * scale),
        four=_round(170 * scale),
        single=0,
    )
    partial_crm_multi = HouseholdMix(
        two=_round(320 * scale),
        three=_round(100 * scale),
        four=_round(30 * scale),
        single=0,
    )

    # SS11.3: the tri-source fraction is asserted *after* the C8 drops, so the
    # pre-drop child count carries them.
    tri_children = _round(TRI_SOURCE_FRACTION * students) + c8_total
    tri = HouseholdMix(tri.two, tri.three, tri.four, tri_children - tri.multi_children)

    appdb_only = _round(3400 * scale)
    partial_crm_single = students - tri_children - partial_crm_multi.children - appdb_only
    partial_crm = HouseholdMix(
        partial_crm_multi.two,
        partial_crm_multi.three,
        partial_crm_multi.four,
        partial_crm_single,
    )

    leads = (
        volumes[("crm", "contact")]
        - (tri_children - c8_crm)
        - partial_crm.children
        - conflicts["C3"]
    )
    partial_crm_deal_households = (
        volumes[("crm", "deal")] - tri.multi - (tri.single - conflicts["C1"])
    )
    appdb_only_enrollments = _round(1800 * scale)
    partial_crm_enrollments = (
        volumes[("appdb", "enrollment")] - tri_children - appdb_only_enrollments
    )

    payments_present = tri_children - c8_payments
    refund_supersede = _round(75 * scale)
    refund_closed = _round(250 * scale)
    fee_deposit_persons = (
        volumes[("payments", "payment")]
        - payments_present
        - conflicts["C2"]
        - conflicts["C11"]
        - refund_supersede
    )
    base_paid_dt = (
        payments_present
        - conflicts["C7"]
        - fee_deposit_persons
        - conflicts["C13"]
        - refund_closed
        - refund_supersede
    )

    compounds = _compound_plan(conflicts, c6_grade, c6_lifecycle, c8_payments, c9_other, scale)
    sibling_window_households = max(2, _round(40 * scale))

    plan = Plan(
        profile=profile,
        scale=scale,
        volumes=volumes,
        conflicts=conflicts,
        tri=tri,
        partial_crm=partial_crm,
        appdb_only=appdb_only,
        leads=leads,
        partial_crm_deal_households=partial_crm_deal_households,
        partial_crm_enrollments=partial_crm_enrollments,
        appdb_only_enrollments=appdb_only_enrollments,
        fee_deposit_persons=fee_deposit_persons,
        refund_supersede=refund_supersede,
        refund_closed=refund_closed,
        base_paid_dt=base_paid_dt,
        c6_grade=c6_grade,
        c6_lifecycle=c6_lifecycle,
        c6_mixed=c6_mixed,
        c14_name=c14_name,
        c14_dob=c14_dob,
        c14_stage=c14_stage,
        c8_crm=c8_crm,
        c8_payments=c8_payments,
        c9_missing=c9_missing,
        c9_other_person=c9_other,
        compounds=compounds,
        sibling_window_households=sibling_window_households,
    )
    plan.validate()
    return plan


def _compound_plan(
    conflicts: dict[str, int],
    c6_grade: int,
    c6_lifecycle: int,
    c8_payments: int,
    c9_other_person: int,
    scale: float,
) -> dict[str, int]:
    """A.5: which pairs of plants deliberately land on one entity (SS11.8).

    Each pair contributes **two** entries to the `compound_with` numerator, so the
    surviving ratio comes out at roughly 2x A.5's >=10% floor -- deliberate margin,
    because `PRECEDENCE` removes overlaps and the margin has to absorb that.

    Only pairs whose `entity_refs` genuinely **intersect** are planted: C11's refs are
    two payment refs and C2's is one payment ref, so neither can compound with a
    person-scoped entry, and C10's overlaps are suppressed by `PRECEDENCE` 2 by design.
    """
    plan = {
        "C4+C6grade": _round(80 * scale),
        "C9+C6grade": _round(40 * scale),
        "C12+C6grade": _round(40 * scale),
        "C7+C6grade": _round(40 * scale),
        "C1+C6grade": _round(40 * scale),
        "C8pay+C4": _round(50 * scale),
        "C3+C9": _round(50 * scale),
        "C13+C6life": _round(10 * scale),
    }
    plan = {name: max(1, count) for name, count in plan.items()}

    # Clamp to the budgets each pair draws from, in a fixed order.
    grade_budget = c6_grade
    for name in ("C4+C6grade", "C9+C6grade", "C12+C6grade", "C7+C6grade", "C1+C6grade"):
        plan[name] = max(0, min(plan[name], grade_budget))
        grade_budget -= plan[name]

    plan["C4+C6grade"] = min(plan["C4+C6grade"], conflicts["C4"])
    plan["C8pay+C4"] = min(plan["C8pay+C4"], c8_payments, conflicts["C4"] - plan["C4+C6grade"])
    # The C3+C9 compound plants C9's *second* branch (a live deal owned by another
    # person), so it may never claim more than the branch-2 budget -- otherwise the
    # branch-1 supernumerary deal count drifts off SS9.1(a)'s pinned gen-1 deal total.
    plan["C3+C9"] = min(plan["C3+C9"], conflicts["C3"], c9_other_person)
    plan["C9+C6grade"] = min(plan["C9+C6grade"], conflicts["C9"] - plan["C3+C9"])
    plan["C12+C6grade"] = min(plan["C12+C6grade"], conflicts["C12"])
    plan["C7+C6grade"] = min(plan["C7+C6grade"], conflicts["C7"])
    plan["C1+C6grade"] = min(plan["C1+C6grade"], conflicts["C1"])
    plan["C13+C6life"] = min(plan["C13+C6life"], conflicts["C13"], c6_lifecycle)
    return plan
