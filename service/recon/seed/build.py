"""Pass 1 -- materialise every source record, clean and planted (SS10 `G31`).

Pass 1 constructs the world; it never authors an `entity_refs` list and never runs an
invariant rule. It records *what it planted* as a list of `Plant` anchors (natural
keys, not refs); pass 2 runs the real `recon.er` cascade over the emitted fixtures
and derives every ref in `golden/` from what ER actually produced.

The build order is fixed and every phase iterates an explicit list, so the whole
module is a pure function of `(seed, profile)`.

    households -> roles -> students -> enrollments -> contacts -> payments -> deals
              -> generation deltas -> timestamp dirt

Reading order note: the *role* assignment (`_assign_roles`) is where SS11's
allocation becomes concrete. Every later phase only reads roles; nothing downstream
decides who is a plant.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from recon.normalize import (
    DEAL_STAGE_VALUES,
    GRADE_VALUES,
    LIFECYCLE_VALUES,
    PROGRAM_VALUES,
    STATE_VALUES,
    norm_email,
    norm_name,
)
from recon.reference import (
    DEAL_STAGE_TO_FUNNEL,
    ENROLLMENT_GRADE_FLOOR,
    FEE_SCHEDULE,
    GRADE_ORDER,
    KEYSTONE_NS,
    LIFECYCLE_TO_FUNNEL,
    PAID_IMPLYING_STAGES,
    STATUS_TO_FUNNEL,
)

from .corpora import (
    FIRST_NAMES,
    GMAIL_DOMAINS_ORDERED,
    LAST_NAMES,
    NON_GMAIL_DOMAINS,
    PARENT_FIRST_NAMES,
    WORDS,
)
from .dirt import (
    assert_email_dirt_is_lossless,
    assert_enum_dirt_is_closed,
    assert_name_dirt_is_lossless,
    dirty_email,
    dirty_enum,
    dirty_grade,
    dirty_name,
    gmail_variant,
)
from .plan import Plan
from .rng import Rng, amount_dollars, day_seconds, iso_date, iso_timestamp

__all__ = ["Child", "Dataset", "Household", "Plant", "build_dataset"]


# SS2.3 -- deal stage spelled from the funnel it maps to; the map is bijective.
_FUNNEL_TO_DEAL_STAGE: dict[str, str] = {
    DEAL_STAGE_TO_FUNNEL[stage]: stage for stage in DEAL_STAGE_VALUES
}

# SS2.3 -- the `LIFECYCLE_TO_FUNNEL` pre-image, which `G18` draws every clean
# `lifecycle_stage` from. `withdrawn` has an EMPTY pre-image on purpose: no lifecycle
# value maps to it, so a withdrawn student carries a `None`-mapping value and the
# comparison is `unchecked`, never a disagreement.
_LIFECYCLE_PREIMAGE: dict[str | None, tuple[str, ...]] = {}
for _value in LIFECYCLE_VALUES:
    _LIFECYCLE_PREIMAGE.setdefault(LIFECYCLE_TO_FUNNEL[_value], ())
    _LIFECYCLE_PREIMAGE[LIFECYCLE_TO_FUNNEL[_value]] += (_value,)

_NONE_LIFECYCLE: tuple[str, ...] = _LIFECYCLE_PREIMAGE[None]
_OPINIONATED_FUNNELS: tuple[str, ...] = ("prospect", "applied", "enrolled")

_STATUS_FOR_FUNNEL: dict[str, tuple[str, ...]] = {
    "prospect": ("prospect",),
    "applied": ("applied",),
    "waitlisted": ("applied",),
    "deposit_paid": ("enrolled", "active"),
    "enrolled": ("enrolled", "active"),
    "withdrawn": ("withdrawn",),
    "refunded": ("withdrawn",),
}

#: SS11.6 -- payment types a base record may take. `fee` is reserved for the C7 plants
#: and the fee+deposit persons, so the base population is deposit/tuition only.
_BASE_PAID_TYPES: tuple[str, ...] = ("deposit", "tuition")

#: SS5.6 C12 -- offsets that cannot coincide with any other `(program, type)` cell.
_C12_OFFSETS: tuple[int, ...] = (13, 137, 1013)

_MASK_ELIGIBLE_FLOOR = GRADE_ORDER[ENROLLMENT_GRADE_FLOOR]


@dataclass
class Plant:
    """One planted conflict, anchored on **natural keys** -- never on refs.

    Pass 2 turns these into `entity_refs` through `conflict_refs` applied to the real
    cascade output (`G31`), so a plant whose links the cascade declined to make fails
    the run instead of being written into `golden/` with refs nobody can reach.
    """

    conflict_type: str
    student_id: str | None = None
    student_ids: tuple[str, ...] = ()
    contact_ids: tuple[str, ...] = ()
    payment_ids: tuple[str, ...] = ()
    enrollment_id: str | None = None
    expected_methods: tuple[str, ...] = ()
    detail: dict[str, object] = field(default_factory=dict)


@dataclass
class Household:
    """One household: SS4.8's key is `norm_email` of the **primary** guardian email."""

    index: int
    bucket: str  # "tri" | "partial_crm" | "appdb_only"
    size: int
    guardian_email: str
    guardian2_email: str | None
    domain: str
    last_name: str
    parent_first: str
    program: str
    funnel: str
    role: str = "plain"
    has_deal: bool = False
    deal_id: str | None = None
    #: `G7` / SS5.5 C11 FP guard -- siblings whose base payments deliberately share
    #: `(payer_email_norm, amount_cents, type)` INSIDE C11's 600s window. They resolve
    #: to two different persons, so C11 must not fire; a detector that drops C11's
    #: "same person" clause turns every one of them into a false positive.
    sibling_window: bool = False
    children: list[Child] = field(default_factory=list)

    @property
    def in_crm(self) -> bool:
        return self.bucket in {"tri", "partial_crm"}

    @property
    def in_payments(self) -> bool:
        return self.bucket == "tri"


@dataclass
class Child:
    """One app-DB student plus everything the other sources say about them."""

    index: int
    student_id: str
    household: Household
    first_name: str
    last_name: str
    dob: str
    grade: str
    status: str
    roles: set[str] = field(default_factory=set)
    crm_present: bool = True
    payments_present: bool = True
    student: dict[str, object] = field(default_factory=dict)
    contact: dict[str, object] | None = None
    duplicate_contact: dict[str, object] | None = None
    enrollment: dict[str, object] | None = None
    payments: list[dict[str, object]] = field(default_factory=list)


@dataclass
class Dataset:
    """Everything pass 1 produced, before any generation is projected out of it."""

    seed: int
    plan: Plan
    households: list[Household]
    children: list[Child]
    contacts: list[dict[str, object]]
    deals: list[dict[str, object]]
    students: list[dict[str, object]]
    enrollments: list[dict[str, object]]
    payments: list[dict[str, object]]
    deleted_contact_ids: set[str]
    deleted_deal_ids: set[str]
    deleted_payment_ids: set[str]
    oscillations: list[dict[str, object]]
    plants: list[Plant]
    name_collision_allowlist: set[tuple[str, str, str]]


# ======================================================================================


class _Builder:
    def __init__(self, seed: int, plan: Plan) -> None:
        self.seed = seed
        self.plan = plan
        self.rng = Rng(seed)
        self.households: list[Household] = []
        self.children: list[Child] = []
        self.contacts: list[dict[str, object]] = []
        self.deals: list[dict[str, object]] = []
        self.students: list[dict[str, object]] = []
        self.enrollments: list[dict[str, object]] = []
        self.payments: list[dict[str, object]] = []
        self.deleted_contact_ids: set[str] = set()
        self.deleted_deal_ids: set[str] = set()
        self.deleted_payment_ids: set[str] = set()
        self.oscillations: list[dict[str, object]] = []
        self.plants: list[Plant] = []
        self.name_collision_allowlist: set[tuple[str, str, str]] = set()
        self._sibling_anchor: dict[int, int] = {}
        self._used_names: set[tuple[str, str]] = set()
        self._used_emails: set[str] = set()
        self._contact_seq = 0
        self._deal_seq = 0
        self._payment_seq = 0
        self._enrollment_seq = 0

    # -- id allocation ------------------------------------------------------------------
    def _next_contact_id(self) -> str:
        self._contact_seq += 1
        return f"CRM-{self._contact_seq:07d}"

    def _next_deal_id(self) -> str:
        self._deal_seq += 1
        return f"DEAL-{self._deal_seq:07d}"

    def _next_payment_id(self) -> str:
        self._payment_seq += 1
        return f"pi_{self._payment_seq:07d}"

    def _next_enrollment_id(self) -> str:
        self._enrollment_seq += 1
        return str(uuid.uuid5(KEYSTONE_NS, f"enrollment:{self._enrollment_seq}"))

    # -- name / email allocation ---------------------------------------------------------
    def _fresh_name(self, last_name: str | None = None) -> tuple[str, str]:
        """A globally unused `(first, last)` pair (`G5`).

        Rejection sampling against a 2x10^6-pair corpus at a 2.2% load factor, so it
        terminates in a couple of draws; the loop is bounded so a corpus that is ever
        shrunk fails loudly instead of hanging.
        """
        for _ in range(64):
            first = self.rng.pick(FIRST_NAMES)
            last = last_name if last_name is not None else self.rng.pick(LAST_NAMES)
            key = (norm_name(first) or "", norm_name(last) or "")
            if key not in self._used_names:
                self._used_names.add(key)
                return first, last
        raise RuntimeError("name corpus exhausted; NAME_CORPUS_MIN is no longer satisfied (G5)")

    def _fresh_email(self, local_stem: str, domain: str) -> str:
        """An address whose `norm_email` is globally unused (`G11`, `G23`).

        The local part deliberately contains **no dot**: on a gmail domain
        `norm_email` strips dots from the local part, so uniqueness has to hold on the
        dot-free form or two households collide after normalization.
        """
        base = "".join(ch for ch in local_stem.lower() if ch.isalnum() or ch == "-")
        for attempt in range(1024):
            candidate = f"{base}{'' if attempt == 0 else attempt}@{domain}"
            normalized = norm_email(candidate)
            if normalized is not None and normalized not in self._used_emails:
                self._used_emails.add(normalized)
                return candidate
        raise RuntimeError(f"could not allocate a unique address for {local_stem!r}")

    # ==================================================================================
    # Phase 1 -- households and children
    # ==================================================================================
    def build_households(self) -> None:
        plan = self.plan
        specs: list[tuple[str, int, int]] = []
        for bucket, mix in (("tri", plan.tri), ("partial_crm", plan.partial_crm)):
            specs.append((bucket, 2, mix.two))
            specs.append((bucket, 3, mix.three))
            specs.append((bucket, 4, mix.four))
        # Single-child households follow the multi-child ones so that a bucket's
        # deal-bearing prefix (SS11.7) always covers whole households.
        specs.append(("tri", 1, plan.tri.single))
        specs.append(("partial_crm", 1, plan.partial_crm.single))
        specs.append(("appdb_only", 1, plan.appdb_only))

        index = 0
        for bucket, size, count in specs:
            for _ in range(count):
                self.households.append(self._make_household(index, bucket, size))
                index += 1

        student_index = 0
        for household in self.households:
            for _ in range(household.size):
                child = self._make_child(student_index, household)
                household.children.append(child)
                self.children.append(child)
                student_index += 1

    def _make_household(self, index: int, bucket: str, size: int) -> Household:
        parent_first = self.rng.pick(PARENT_FIRST_NAMES)
        last_name = self.rng.pick(LAST_NAMES)
        # ~40% of households sit on a gmail domain, which is the only place A.3's dot
        # and `+alias` variants may appear (`G4`).
        domain = (
            self.rng.pick(GMAIL_DOMAINS_ORDERED)
            if self.rng.chance(0.40)
            else self.rng.pick(NON_GMAIL_DOMAINS)
        )
        guardian_email = self._fresh_email(f"{parent_first}-{last_name}", domain)
        guardian2 = None
        if self.rng.chance(0.40):  # A.3: `guardian2_email` ~60% null
            guardian2 = self._fresh_email(
                f"{self.rng.pick(PARENT_FIRST_NAMES)}-{last_name}",
                self.rng.pick(NON_GMAIL_DOMAINS),
            )
        program = self.rng.pick(PROGRAM_VALUES)
        return Household(
            index=index,
            bucket=bucket,
            size=size,
            guardian_email=guardian_email,
            guardian2_email=guardian2,
            domain=domain,
            last_name=last_name,
            parent_first=parent_first,
            program=program,
            funnel="prospect",
        )

    def _make_child(self, index: int, household: Household) -> Child:
        first, last = self._fresh_name(household.last_name)
        grade = self.rng.pick(GRADE_VALUES)
        enrollment_year = 2026 + self.rng.randint(0, 1)
        birth_year = enrollment_year - (GRADE_ORDER[grade] + 6)
        dob = iso_date(birth_year, self.rng.randint(0, 364))
        return Child(
            index=index,
            student_id=str(uuid.uuid5(KEYSTONE_NS, str(index))),
            household=household,
            first_name=first,
            last_name=last,
            dob=dob,
            grade=grade,
            status="prospect",
        )

    # ==================================================================================
    # Phase 2 -- roles: SS11's allocation made concrete
    # ==================================================================================
    def assign_roles(self) -> None:
        """Hand out every conflict role, drawing the SLOTS through the seeded PRNG.

        The four bucket pools are **shuffled once** before anything is taken from them.
        That is the whole difference between a dataset whose values move with `--seed`
        and one whose *addressing* does too. Previously every role was a positional
        slice of an index-ordered list, and since every PK is a pure function of that
        index (`uuid5(NS, str(index))`, `CRM-%07d`, `pi_%07d`) and every file is emitted
        in PK order, the consequences were three, all bad:

        * `golden/conflicts.json`'s `(type, entity_refs)` key set was identical at every
          seed, so a detector that hard-coded it would score perfectly at any seed;
        * `golden/clean-sample.json` carried no seed entropy at all;
        * each plant occupied an exactly contiguous block of record indices, i.e. a
          contiguous run of lines in the fixture file -- A.5's "uniformly distributed
          ... or resolvable by one clever join" at the addressing level.

        Shuffling costs nothing structurally: the pools are homogeneous by construction
        (a tri-source single-child household is interchangeable with any other), and
        `sc_construction_sweep` re-proves every count over the emitted records either
        way. Draw order stays a pure function of the seed, so the run is as
        reproducible as it was.
        """
        plan = self.plan
        tri_single = self.rng.shuffled(
            [h for h in self.households if h.bucket == "tri" and h.size == 1]
        )
        tri_multi = self.rng.shuffled(
            [h for h in self.households if h.bucket == "tri" and h.size > 1]
        )
        appdb_only = self.rng.shuffled([h for h in self.households if h.bucket == "appdb_only"])
        # `partial_crm` is deliberately NOT shuffled: it carries no conflict role at all
        # (only `has_deal`), so shuffling it would add no seed entropy to the plant
        # addressing, and SS11.7 wants the deal-bearing `{appdb, crm}` households to be a
        # contiguous prefix so a deal-bearing household is always inside the enrolled
        # prefix of `build_students_and_enrollments`.
        partial = [h for h in self.households if h.bucket == "partial_crm"]

        cursor = 0

        def take(count: int) -> list[Household]:
            nonlocal cursor
            chunk = tri_single[cursor : cursor + count]
            if len(chunk) != count:  # pragma: no cover - Plan.validate() precludes this
                raise RuntimeError("tri-source single-child households exhausted")
            cursor += count
            return chunk

        for role, count in (
            ("c1", plan.conflicts["C1"]),
            ("c7", plan.conflicts["C7"]),
            ("c13", plan.conflicts["C13"]),
            ("refund_i", plan.refund_supersede),
            ("refund_ii", plan.refund_closed),
            ("fee_deposit", plan.fee_deposit_persons),
        ):
            for household in take(count):
                household.role = role

        self._plain_tri_single = tri_single[cursor:]

        for household in tri_multi[: plan.c8_crm]:
            household.role = "c8_crm"
        for household in tri_multi[plan.c8_crm : plan.c8_crm + plan.c8_payments]:
            household.role = "c8_payments"

        # SS5.5's C11 false-positive guard is "a sibling pair resolves to two different
        # persons and is therefore never C11", and `G7` explicitly permits "sibling
        # simultaneous fee payments". That guard had an EMPTY population: siblings were
        # spaced 1800s apart by construction, so exactly one different-person pair in
        # the whole 18,000-payment dataset ever landed inside the 600s window. These
        # households put a real population behind it, at zero cost to the SS11.6 budget
        # -- the two base payments already exist; only their type and spacing change.
        c8_end = plan.c8_crm + plan.c8_payments
        window_pool = [h for h in tri_multi[c8_end:] if h.size == 2]
        if len(window_pool) < plan.sibling_window_households:  # pragma: no cover
            raise RuntimeError(
                f"need {plan.sibling_window_households} two-child tri households for the "
                f"C11 sibling guard band, have {len(window_pool)}"
            )
        for household in window_pool[: plan.sibling_window_households]:
            household.sibling_window = True

        for household in appdb_only[: plan.conflicts["C5"]]:
            household.role = "c5"

        # SS11.7: the deal-bearing `{appdb, crm}` households are the leading prefix, so
        # every deal-bearing household's children are inside the enrolled prefix too.
        for household in partial[: plan.partial_crm_deal_households]:
            household.has_deal = True

        self._assign_funnels(partial)
        self._assign_child_plants()

    def _assign_funnels(self, partial: list[Household]) -> None:
        """`G18`: every household -- planted or not -- is funnel-uniform.

        `G38` is enforced right here: a paid-implying `stage_funnel` is drawn **only**
        for tri-source (payments-present) households and for the four budgeted
        exception populations. Every `{appdb, crm}` and `{appdb}`-only household that
        is not a C5 plant is capped below `deposit_paid`, which is what stops thousands
        of partial-presence enrollments from firing C7.
        """
        partial_deal = set()
        for household in partial:
            if household.has_deal:
                partial_deal.add(household.index)

        for household in self.households:
            if household.bucket == "tri":
                if household.role == "refund_ii":
                    household.funnel = self.rng.pick(("refunded", "withdrawn"))
                elif household.role == "c1":
                    household.funnel = "deposit_paid"
                else:
                    household.funnel = self.rng.pick(("deposit_paid", "enrolled"))
            elif household.bucket == "partial_crm":
                if household.index in partial_deal:
                    household.funnel = self.rng.pick(("prospect", "applied"))
                else:
                    household.funnel = self.rng.pick(
                        ("prospect", "applied", "waitlisted", "withdrawn")
                    )
            elif household.role == "c5":
                household.funnel = self.rng.pick(tuple(sorted(PAID_IMPLYING_STAGES)))
            else:
                household.funnel = self.rng.pick(("prospect", "applied", "withdrawn"))

            for child in household.children:
                child.status = self.rng.pick(_STATUS_FOR_FUNNEL[household.funnel])

    def _assign_child_plants(self) -> None:
        """Draw the child-scoped plants from the unclaimed tri-source single pool.

        Deliberate compounds (A.5) attach a *second* role to a child that already
        carries one; everything else consumes a fresh household, so no two plants
        collide by accident.
        """
        plan = self.plan
        pool = [h.children[0] for h in self._plain_tri_single]
        cursor = 0

        def take(count: int) -> list[Child]:
            nonlocal cursor
            chunk = pool[cursor : cursor + count]
            if len(chunk) != count:
                raise RuntimeError(
                    f"unclaimed tri-source single-child pool exhausted: needed {count}, "
                    f"had {len(pool) - cursor}"
                )
            cursor += count
            return chunk

        def role_holders(role: str) -> list[Child]:
            """Children of the single-child households already carrying `role`."""
            return [h.children[0] for h in self.households if h.role == role and h.size == 1]

        comp = plan.compounds

        # -- C3: a duplicate contact for one student; `C3+C9` also gets a stale pointer.
        c3_children = take(plan.conflicts["C3"])
        for child in c3_children:
            child.roles.add("c3")
        for child in c3_children[: comp["C3+C9"]]:
            child.roles.add("c9_other")

        # -- C9: the remaining stale pointers.
        c9_remaining = plan.conflicts["C9"] - comp["C3+C9"]
        c9_children = take(c9_remaining)
        missing_quota = plan.c9_missing
        for child in c9_children:
            if missing_quota > 0:
                child.roles.add("c9_missing")
                missing_quota -= 1
            else:
                child.roles.add("c9_other")
        for child in c9_children[: comp["C9+C6grade"]]:
            child.roles.add("c6_grade")

        # -- C10: two clean students per plant, neither carrying any other plant.
        for child in take(plan.conflicts["C10"]):
            child.roles.add("c10_a")
        for child in take(plan.conflicts["C10"]):
            child.roles.add("c10_b")

        # -- C4: `C8pay+C4` lands on C8 payments-dropped children, the rest here.
        c4_here = plan.conflicts["C4"] - comp["C8pay+C4"]
        c4_children = take(c4_here)
        for child in c4_children:
            child.roles.add("c4")
        for child in c4_children[: comp["C4+C6grade"]]:
            child.roles.add("c6_grade")

        # -- C12: half compound with a grade disagreement.
        c12_children = take(plan.conflicts["C12"])
        for child in c12_children:
            child.roles.add("c12")
        for child in c12_children[: comp["C12+C6grade"]]:
            child.roles.add("c6_grade")

        # -- C11: duplicate payment pairs.
        for child in take(plan.conflicts["C11"]):
            child.roles.add("c11")

        # -- C6 / C14 standalone remainder.
        grade_used = (
            comp["C4+C6grade"]
            + comp["C9+C6grade"]
            + comp["C12+C6grade"]
            + comp["C7+C6grade"]
            + comp["C1+C6grade"]
        )
        for child in take(plan.c6_grade - grade_used):
            child.roles.add("c6_grade")
        for child in take(plan.c6_lifecycle - comp["C13+C6life"]):
            child.roles.add("c6_lifecycle")
        for child in take(plan.c6_mixed):
            child.roles.add("c6_mixed")
        for child in take(plan.c14_name):
            child.roles.add("c14_name")
        for child in take(plan.c14_dob):
            child.roles.add("c14_dob")
        for child in take(plan.c14_stage):
            child.roles.add("c14_stage")

        # -- compounds that ride on an already-allocated household role.
        for child in role_holders("c7")[: comp["C7+C6grade"]]:
            child.roles.add("c6_grade")
        for child in role_holders("c1")[: comp["C1+C6grade"]]:
            child.roles.add("c6_grade")
        for child in role_holders("c13")[: comp["C13+C6life"]]:
            child.roles.add("c6_lifecycle")

        # -- C8: pick the dropped child, mask-eligible per `G22`.
        for household in self.households:
            if household.role not in {"c8_crm", "c8_payments"}:
                continue
            # C8's mask excludes a child below `ENROLLMENT_GRADE_FLOOR`, so a PK sibling
            # would shrink the eligible set and make the plant undetectable -- one child
            # absent out of one eligible member is not "exactly one of several". Lift the
            # whole household above the floor rather than plant something no rule can see.
            for child in household.children:
                if GRADE_ORDER[child.grade] < _MASK_ELIGIBLE_FLOOR:
                    child.grade = "K"
            eligible = [
                child
                for child in household.children
                if GRADE_ORDER[child.grade] >= _MASK_ELIGIBLE_FLOOR
                and child.status != "withdrawn"
                and household.funnel not in {"withdrawn", "refunded"}
            ]
            if len(eligible) < 2:  # pragma: no cover - tri households are never withdrawn
                raise RuntimeError(
                    f"C8 household {household.index} has {len(eligible)} mask-eligible "
                    "children; the plant would be undetectable (G22)"
                )
            dropped = eligible[0]
            if household.role == "c8_crm":
                dropped.roles.add("c8_dropped_crm")
                dropped.crm_present = False
            else:
                dropped.roles.add("c8_dropped_payments")
                dropped.payments_present = False

        c8_pay_children = [child for child in self.children if "c8_dropped_payments" in child.roles]
        for child in c8_pay_children[: comp["C8pay+C4"]]:
            child.roles.add("c4")

        for household in self.households:
            if household.role == "c5":
                household.children[0].roles.add("c5")

    # ==================================================================================
    # Phase 3 -- app DB
    # ==================================================================================
    def build_students_and_enrollments(self) -> None:
        plan = self.plan
        partial_enrolled = 0
        appdb_enrolled = 0

        # SS11.5: the `{appdb}`-only enrollment budget covers the 400 C5 plants FIRST and
        # then fills to the budget with plain children. C5 plants no longer sit in a
        # leading prefix of the bucket (`assign_roles` now draws their slots through the
        # PRNG), so the enrolled set is computed up front rather than by an index-order
        # counter that a scattered plant could push past the budget.
        appdb_only_children = [
            child for child in self.children if child.household.bucket == "appdb_only"
        ]
        c5_children = [child for child in appdb_only_children if "c5" in child.roles]
        remaining = plan.appdb_only_enrollments - len(c5_children)
        if remaining < 0:  # pragma: no cover - Plan.validate() precludes this
            raise RuntimeError(
                f"{len(c5_children)} C5 plants exceed the {{appdb}}-only enrollment "
                f"budget of {plan.appdb_only_enrollments}"
            )
        plain_appdb_only = [child for child in appdb_only_children if "c5" not in child.roles]
        appdb_only_enrolled_ids = {child.student_id for child in c5_children}
        appdb_only_enrolled_ids.update(child.student_id for child in plain_appdb_only[:remaining])

        for child in self.children:
            household = child.household
            created = self._created_offset()
            updated = created + day_seconds(self.rng.randint(1, 200))
            enrollment_year = int(child.dob[:4]) + GRADE_ORDER[child.grade] + 6

            child.student = {
                "id": child.student_id,
                "first_name": dirty_name(self.rng, child.first_name)
                if self.rng.chance(0.25)
                else child.first_name,
                "last_name": dirty_name(self.rng, child.last_name)
                if self.rng.chance(0.20)
                else child.last_name,
                "dob": child.dob,
                "grade": dirty_grade(self.rng, child.grade)
                if self.rng.chance(0.30)
                else child.grade,
                "guardian_email": self._guardian_spelling(household.guardian_email),
                "guardian2_email": household.guardian2_email,
                "status": child.status,
                "enrollment_year": enrollment_year,
                "created_at": iso_timestamp(created),
                "updated_at": iso_timestamp(updated),
                "student_number": f"S-{child.index + 1:06d}",
                "household_id": f"HH-{household.index + 1:06d}",
                "communication_opt_out": self.rng.chance(0.08),
            }
            assert_name_dirt_is_lossless(str(child.student["first_name"]), child.first_name)
            assert_name_dirt_is_lossless(str(child.student["last_name"]), child.last_name)
            assert_enum_dirt_is_closed("grade", str(child.student["grade"]), child.grade)
            assert_email_dirt_is_lossless(
                str(child.student["guardian_email"]), household.guardian_email
            )
            self.students.append(child.student)

            wants_enrollment = household.bucket == "tri"
            if household.bucket == "partial_crm":
                wants_enrollment = partial_enrolled < plan.partial_crm_enrollments
                if wants_enrollment:
                    partial_enrolled += 1
            elif household.bucket == "appdb_only":
                wants_enrollment = child.student_id in appdb_only_enrolled_ids
                if wants_enrollment:
                    appdb_enrolled += 1
            if wants_enrollment:
                child.enrollment = self._make_enrollment(child, created)
                self.enrollments.append(child.enrollment)

        if partial_enrolled != plan.partial_crm_enrollments:  # pragma: no cover
            raise RuntimeError(
                f"{{appdb,crm}} enrollments {partial_enrolled} != {plan.partial_crm_enrollments}"
            )
        if appdb_enrolled != plan.appdb_only_enrollments:  # pragma: no cover
            raise RuntimeError(
                f"{{appdb}} enrollments {appdb_enrolled} != {plan.appdb_only_enrollments}"
            )

    def _make_enrollment(self, child: Child, student_created: int) -> dict[str, object]:
        household = child.household
        created = student_created + day_seconds(self.rng.randint(0, 30))
        updated = created + day_seconds(self.rng.randint(1, 120))
        deposit_paid_at = None
        settled = household.funnel in PAID_IMPLYING_STAGES or household.funnel in {
            "refunded",
            "withdrawn",
        }
        if settled and household.bucket == "tri":
            # Retained historical fact -- never cleared, never a conflict trigger (SS1.4).
            deposit_paid_at = iso_timestamp(created + day_seconds(self.rng.randint(1, 20)))
        stage = household.funnel
        return {
            "id": self._next_enrollment_id(),
            "student_id": child.student_id,
            "program": dirty_enum(self.rng, "program", household.program)
            if self.rng.chance(0.25)
            else household.program,
            "stage": dirty_enum(self.rng, "stage", stage) if self.rng.chance(0.25) else stage,
            "deposit_paid_at": deposit_paid_at,
            "crm_deal_id": None,
            "created_at": iso_timestamp(created),
            "updated_at": iso_timestamp(updated),
            "billing_owner_email": household.guardian_email,
        }

    def _guardian_spelling(self, address: str) -> str:
        """One household address, spelled differently per record where that is safe.

        Dot / `+alias` variation is emitted on gmail domains only; every other domain
        gets surrounding-quote / whitespace / case dirt, which `norm_email` undoes
        without collapsing two distinct mailboxes (`G4`, SS2.1 ruling 6).
        """
        domain = address.rpartition("@")[2]
        if domain in GMAIL_DOMAINS_ORDERED and self.rng.chance(0.45):
            return gmail_variant(self.rng, address)
        if self.rng.chance(0.20):
            return dirty_email(self.rng, address)
        return address

    def _created_offset(self) -> int:
        return day_seconds(self.rng.randint(0, 300), self.rng.randint(0, 86399))

    # ==================================================================================
    # Phase 4 -- CRM contacts
    # ==================================================================================
    def build_contacts(self) -> None:
        for child in self.children:
            household = child.household
            if not household.in_crm:
                continue
            contact = self._make_contact(child)
            child.contact = contact
            self.contacts.append(contact)
            if not child.crm_present:
                self.deleted_contact_ids.add(str(contact["crm_id"]))

        self._apply_c10()
        self._build_c3_duplicates()
        self._build_leads()

    def _make_contact(self, child: Child) -> dict[str, object]:
        household = child.household
        rng = self.rng
        created = self._created_offset()
        updated = created + day_seconds(rng.randint(1, 200))

        is_c4 = "c4" in child.roles
        needs_l1 = bool(child.roles & {"c6_mixed", "c14_name", "c14_dob", "c10_a", "c3"})
        external_id: str | None
        if is_c4:
            external_id = None  # SS5.6 C4: `L2` must fail too, so no hard key either
        elif needs_l1 or rng.chance(0.60):
            external_id = child.student_id
        else:
            external_id = None

        first_name = child.first_name
        last_name = child.last_name
        dob_value: str | None = child.dob
        if not (is_c4 or needs_l1) and rng.chance(0.30):
            dob_value = None  # A.3: `dob` present on ~70% of contacts

        grade_canonical = child.grade
        lifecycle_funnel = STATUS_TO_FUNNEL[child.status]

        if "c6_grade" in child.roles:
            grade_canonical = self._other_grade(child.grade)
        if "c6_mixed" in child.roles:
            first_name, last_name = self._fresh_name()
            grade_canonical = self._other_grade(child.grade)
        if "c14_name" in child.roles:
            first_name, last_name = self._fresh_name()
        if "c14_dob" in child.roles:
            dob_value = self._other_dob(child.dob)

        lifecycle_pool = _LIFECYCLE_PREIMAGE.get(lifecycle_funnel) or _NONE_LIFECYCLE
        if "c6_lifecycle" in child.roles:
            others = tuple(
                funnel
                for funnel in _OPINIONATED_FUNNELS
                if funnel != lifecycle_funnel and _LIFECYCLE_PREIMAGE.get(funnel)
            )
            lifecycle_pool = _LIFECYCLE_PREIMAGE[rng.pick(others)]
        lifecycle = rng.pick(lifecycle_pool)

        if is_c4:
            email = self._fresh_email(
                f"{household.parent_first}-{household.last_name}-alt",
                rng.pick(NON_GMAIL_DOMAINS),
            )
        else:
            email = self._guardian_spelling(household.guardian_email)

        state = rng.pick(STATE_VALUES)
        contact: dict[str, object] = {
            "crm_id": self._next_contact_id(),
            "email": email,
            "first_name": dirty_name(rng, first_name) if rng.chance(0.30) else first_name,
            "last_name": dirty_name(rng, last_name) if rng.chance(0.25) else last_name,
            "lifecycle_stage": dirty_enum(rng, "lifecycle_stage", lifecycle)
            if rng.chance(0.35)
            else lifecycle,
            "created_at": iso_timestamp(created),
            "updated_at": iso_timestamp(updated),
            "external_id": external_id,
            "dob": dob_value,
            "grade": dirty_grade(rng, grade_canonical) if rng.chance(0.40) else grade_canonical,
            "state": dirty_enum(rng, "state", state) if rng.chance(0.50) else state,
            "marketing_consent": None if rng.chance(0.10) else rng.chance(0.7),
        }
        assert_enum_dirt_is_closed("grade", str(contact["grade"]), grade_canonical)
        assert_enum_dirt_is_closed("state", str(contact["state"]), state)
        assert_enum_dirt_is_closed("lifecycle_stage", str(contact["lifecycle_stage"]), lifecycle)
        assert_name_dirt_is_lossless(str(contact["first_name"]), first_name)
        assert_name_dirt_is_lossless(str(contact["last_name"]), last_name)
        return contact

    def _other_grade(self, grade: str) -> str:
        options = tuple(value for value in GRADE_VALUES if value != grade)
        return self.rng.pick(options)

    def _other_dob(self, dob: str) -> str:
        year = int(dob[:4]) + self.rng.pick((-2, -1, 1, 2))
        return iso_date(year, self.rng.randint(0, 364))

    def _apply_c10(self) -> None:
        """SS5.6 C10: one contact carrying student B's identity on student A's record.

        A's contact keeps `external_id == A.id` (so `L1` links it and `PRECEDENCE` 7
        can never fire) and keeps A's `grade`/`lifecycle_stage` (so the *only*
        disagreeing paths are the three sensitive ones -- `G21`). B keeps its own
        contact, so no student is left contact-less.
        """
        a_children = [c for c in self.children if "c10_a" in c.roles]
        b_children = [c for c in self.children if "c10_b" in c.roles]
        for child_a, child_b in zip(a_children, b_children, strict=True):
            contact = child_a.contact
            if contact is None:  # pragma: no cover - C10 plants are tri-source
                raise RuntimeError("C10 plant has no CRM contact")
            contact["first_name"] = child_b.first_name
            contact["last_name"] = child_b.last_name
            contact["dob"] = child_b.dob
            contact["external_id"] = child_a.student_id
            self.name_collision_allowlist.add((child_b.first_name, child_b.last_name, child_b.dob))
            self.plants.append(
                Plant(
                    conflict_type="C10",
                    student_id=child_a.student_id,
                    student_ids=(child_a.student_id, child_b.student_id),
                    contact_ids=(str(contact["crm_id"]),),
                    expected_methods=("L1",),
                    detail={"student_b": child_b.student_id},
                )
            )

    def _build_c3_duplicates(self) -> None:
        """SS5.6 C3: exactly two contacts per email collision, both `L1` to one student.

        Both carry `external_id == student.id`, so the pair contributes **one** person
        (SS11.9) and SS4.6's lowest-`crm_id` tiebreak has nothing to choose between --
        `G23` requires their `COMPARED_FIELDS` values to be identical and to agree with
        the student, so survivorship cannot manufacture a C6/C14 on a C3 person.
        """
        for child in self.children:
            if "c3" not in child.roles or child.contact is None:
                continue
            original = child.contact
            original["external_id"] = child.student_id
            duplicate = dict(original)
            duplicate["crm_id"] = self._next_contact_id()
            duplicate["email"] = self._guardian_spelling(child.household.guardian_email)
            duplicate["first_name"] = dirty_name(self.rng, child.first_name)
            duplicate["last_name"] = dirty_name(self.rng, child.last_name)
            # C3's predicate accepts `dob_norm` equal **or either null**; exercise both.
            duplicate["dob"] = None if self.rng.chance(0.35) else original["dob"]
            duplicate["created_at"] = original["created_at"]
            duplicate["updated_at"] = original["updated_at"]
            child.duplicate_contact = duplicate
            self.contacts.append(duplicate)
            self.name_collision_allowlist.add((child.first_name, child.last_name, child.dob))
            self.plants.append(
                Plant(
                    conflict_type="C3",
                    student_id=child.student_id,
                    contact_ids=(str(original["crm_id"]), str(duplicate["crm_id"])),
                    expected_methods=("L1", "L1"),
                )
            )

    def _build_leads(self) -> None:
        """SS11.4 / `G11`: deal-less leads -- no payment, no enrollment, no student link.

        They are the false-positive test at scale: 45% of the CRM export is a lead, and
        every one of them carries a globally unique `email_norm` so C3 cannot see them
        and no `associated_contact_ids` names them so C1 cannot either.
        """
        for _ in range(self.plan.leads):
            first, last = self._fresh_name()
            created = self._created_offset()
            lifecycle = self.rng.pick(LIFECYCLE_VALUES)
            grade = self.rng.pick(GRADE_VALUES)
            state = self.rng.pick(STATE_VALUES)
            email = self._fresh_email(f"{first}-{last}", self.rng.pick(NON_GMAIL_DOMAINS))
            contact: dict[str, object] = {
                "crm_id": self._next_contact_id(),
                "email": email,
                "first_name": dirty_name(self.rng, first) if self.rng.chance(0.25) else first,
                "last_name": last,
                "lifecycle_stage": dirty_enum(self.rng, "lifecycle_stage", lifecycle)
                if self.rng.chance(0.30)
                else lifecycle,
                "created_at": iso_timestamp(created),
                "updated_at": iso_timestamp(created + day_seconds(self.rng.randint(1, 150))),
                "external_id": None,
                "dob": iso_date(2008 + self.rng.randint(0, 10), self.rng.randint(0, 364))
                if self.rng.chance(0.55)
                else None,
                "grade": dirty_grade(self.rng, grade) if self.rng.chance(0.35) else grade,
                "state": dirty_enum(self.rng, "state", state) if self.rng.chance(0.50) else state,
                "marketing_consent": None if self.rng.chance(0.15) else self.rng.chance(0.5),
            }
            self.contacts.append(contact)

    # ==================================================================================
    # Phase 5 -- payments
    # ==================================================================================
    def build_payments(self) -> None:
        plan = self.plan
        c12_targets = {c.student_id for c in self.children if "c12" in c.roles}

        # SS5.6 C2 plants are woven into the payment stream at PRNG-drawn slots so their
        # `pi_%07d` ids -- and the golden keys built from them -- move with the seed.
        payments_children = [c for c in self.children if c.household.in_payments]
        c2_slots = set(self.rng.sample(range(len(payments_children)), plan.conflicts["C2"]))
        c2_emitted = 0
        slot = -1

        for child in self.children:
            household = child.household
            if not household.in_payments:
                continue
            slot += 1
            if slot in c2_slots:
                self._build_c2_payment(c2_emitted)
                c2_emitted += 1
            role = household.role
            base_type: str
            if role in {"c7", "fee_deposit"}:
                base_type = "fee"
            elif role in {"c1", "c13", "refund_i", "refund_ii"}:
                # SS5.6 C1: the plant holds a `paid` `deposit` at the exact schedule amount.
                base_type = "deposit"
            elif household.sibling_window:
                # Both siblings pay the same `deposit` (same program => same amount)
                # within C11's window -- the FP-guard population of SS5.5 C11 / `G7`.
                base_type = "deposit"
            else:
                base_type = self.rng.pick(_BASE_PAID_TYPES)

            base_offset = self._created_offset() + day_seconds(child.index % 7, 0)
            if household.sibling_window:
                # Both siblings hang off ONE household anchor, so the pair is
                # deliberately INSIDE the 600s window: a detector that drops C11's
                # "both resolve to the same person" clause flags these; a correct one
                # does not, because siblings are two different persons.
                base_offset = self._sibling_anchor.setdefault(household.index, base_offset)
                sibling_gap = 30 * household.children.index(child)
            else:
                # Siblings are otherwise spaced beyond `LEGIT_REPEAT_MIN_SECONDS` so a
                # shared payer/amount/type key can never sit inside C11's 600s window.
                sibling_gap = day_seconds(0, 1800) * household.children.index(child)
            occurred = base_offset + sibling_gap

            amount = FEE_SCHEDULE[(household.program, base_type)]
            if child.student_id in c12_targets and base_type in _BASE_PAID_TYPES:
                amount += self.rng.pick(_C12_OFFSETS)

            status = "paid"
            refunded_at = None
            if role in {"c13", "refund_i", "refund_ii"}:
                status = "refunded"
                refunded_at = occurred + day_seconds(self.rng.randint(3, 25))

            payment = self._make_payment(
                child,
                payment_type=base_type,
                amount_cents=amount,
                status=status,
                occurred=occurred,
                refunded_at=refunded_at,
            )
            child.payments.append(payment)
            self.payments.append(payment)
            if not child.payments_present:
                self.deleted_payment_ids.add(str(payment["payment_id"]))

            if role == "fee_deposit":
                second = self._make_payment(
                    child,
                    payment_type="deposit",
                    amount_cents=FEE_SCHEDULE[(household.program, "deposit")],
                    status="paid",
                    occurred=occurred + day_seconds(self.rng.randint(2, 40)),
                    refunded_at=None,
                )
                child.payments.append(second)
                self.payments.append(second)
            elif role == "refund_i":
                # `G14` shape (i): superseded by a later `paid` payment of the same type,
                # comfortably beyond `LEGIT_REPEAT_MIN_SECONDS`.
                supersede = self._make_payment(
                    child,
                    payment_type=base_type,
                    amount_cents=amount,
                    status="paid",
                    occurred=occurred + day_seconds(self.rng.randint(2, 30)),
                    refunded_at=None,
                )
                child.payments.append(supersede)
                self.payments.append(supersede)

            if "c11" in child.roles:
                first = child.payments[0]
                delta = self.rng.randint(30, 300)  # `G7`: <= C11_PLANT_MAX_SECONDS
                duplicate = self._make_payment(
                    child,
                    payment_type=str(first["type"]),
                    amount_cents=int(first["amount_cents"]),
                    status="paid",
                    occurred=occurred + delta,
                    refunded_at=None,
                )
                child.payments.append(duplicate)
                self.payments.append(duplicate)
                self.plants.append(
                    Plant(
                        conflict_type="C11",
                        student_id=child.student_id,
                        payment_ids=(str(first["payment_id"]), str(duplicate["payment_id"])),
                        detail={"delta_seconds": delta},
                    )
                )

        if c2_emitted != plan.conflicts["C2"]:  # pragma: no cover - the sample is exact
            raise RuntimeError(f"emitted {c2_emitted} C2 plants, planned {plan.conflicts['C2']}")

    def _make_payment(
        self,
        child: Child,
        *,
        payment_type: str,
        amount_cents: int,
        status: str,
        occurred: int,
        refunded_at: int | None,
    ) -> dict[str, object]:
        household = child.household
        rng = self.rng
        # SS10 `G6`: the joint distribution keeps A.2's ~60% / ~85% marginals while
        # making the joint gap impossible -- 45% both keys, 15% hard id only, 40%
        # metadata names only, 0% neither.
        draw = rng.randint(1, 100)
        has_external = draw <= 60
        has_names = draw > 15
        metadata: dict[str, object] = {
            "student_first_name": dirty_name(rng, child.first_name)
            if has_names and rng.chance(0.30)
            else (child.first_name if has_names else None),
            "student_last_name": dirty_name(rng, child.last_name)
            if has_names and rng.chance(0.25)
            else (child.last_name if has_names else None),
            "program": (
                dirty_enum(rng, "program", household.program)
                if rng.chance(0.25)
                else household.program
            )
            if rng.chance(0.90)
            else None,
        }
        return {
            "payment_id": self._next_payment_id(),
            "payer_email": self._guardian_spelling(household.guardian_email),
            "payer_name": f"{household.parent_first} {household.last_name}",
            "amount_cents": amount_cents,
            "currency": "usd",
            "type": payment_type,
            "status": status,
            "occurred_at": iso_timestamp(occurred),
            "created_at": iso_timestamp(occurred),
            "updated_at": iso_timestamp(occurred + day_seconds(rng.randint(1, 60))),
            "external_ref": child.student_id if has_external else None,
            "refunded_at": None if refunded_at is None else iso_timestamp(refunded_at),
            "metadata": metadata,
        }

    def _build_c2_payment(self, index: int) -> None:
        """SS5.6 C2: one payment that omits both attribution keys -- the plant.

        `payer_email` is drawn from an address used by no student and no contact, so
        `P2`/`P3` cannot reach a household key either.

        Emitted **interleaved** with the attributable payments rather than appended in
        one block at the end. `payment_id` is a pure function of the emission counter
        (`pi_%07d`) and files are written in PK order, so appending them made all 200
        C2 refs -- and therefore 200 `golden/conflicts.json` keys -- identical at every
        seed, and put every C2 on 200 consecutive lines of `payments/gen3/payment.jsonl`.
        """
        first, last = self._fresh_name()
        email = self._fresh_email(f"orphan-{first}-{last}", self.rng.pick(NON_GMAIL_DOMAINS))
        program = self.rng.pick(PROGRAM_VALUES)
        payment_type = self.rng.pick(("fee", "deposit", "tuition"))
        occurred = self._created_offset() + day_seconds(index % 30)
        payment: dict[str, object] = {
            "payment_id": self._next_payment_id(),
            "payer_email": email,
            "payer_name": f"{first} {last}",
            "amount_cents": FEE_SCHEDULE[(program, payment_type)],
            "currency": "usd",
            "type": payment_type,
            "status": "paid",
            "occurred_at": iso_timestamp(occurred),
            "created_at": iso_timestamp(occurred),
            "updated_at": iso_timestamp(occurred + day_seconds(self.rng.randint(1, 60))),
            "external_ref": None,
            "refunded_at": None,
            "metadata": {
                "student_first_name": None,
                "student_last_name": None,
                "program": None,
            },
        }
        self.payments.append(payment)
        self.plants.append(Plant(conflict_type="C2", payment_ids=(str(payment["payment_id"]),)))

    # ==================================================================================
    # Phase 6 -- CRM deals and the enrollment -> deal pointer
    # ==================================================================================
    def build_deals(self) -> None:
        for household in self.households:
            if household.bucket == "tri":
                household.has_deal = household.role != "c1"
            elif household.bucket == "appdb_only":
                household.has_deal = False
            if not household.has_deal:
                continue
            household.deal_id = self._make_deal(household)

        self._wire_deal_pointers()

    def _make_deal(self, household: Household) -> str:
        rng = self.rng
        # `G29`: pipeline is the program of the household's ANCHOR enrollment -- the
        # enrollment of `household_anchor_student(k)`, which is deterministic for a
        # 2-4 child household where "primary" is not.
        pipeline = household.program
        contacts = [
            str(child.contact["crm_id"])
            for child in household.children
            if child.contact is not None
        ]
        if not contacts:  # pragma: no cover - a deal-bearing household always has contacts
            raise RuntimeError(f"household {household.index} has a deal but no contacts")
        deal_id = self._next_deal_id()
        funnel = household.funnel
        stage_canonical = _FUNNEL_TO_DEAL_STAGE[funnel]
        cents = FEE_SCHEDULE[(household.program, "deposit")] * household.size
        created = self._created_offset()
        self.deals.append(
            {
                "deal_id": deal_id,
                "name": f"{household.last_name} {rng.pick(WORDS)} {2026 + rng.randint(0, 1)}",
                "pipeline": dirty_enum(rng, "pipeline", pipeline) if rng.chance(0.25) else pipeline,
                "stage": dirty_enum(rng, "deal_stage", stage_canonical)
                if rng.chance(0.30)
                else stage_canonical,
                "amount": amount_dollars(cents),
                "associated_contact_ids": sorted(contacts),
                "created_at": iso_timestamp(created),
                "updated_at": iso_timestamp(created + day_seconds(rng.randint(1, 180))),
            }
        )
        return deal_id

    def _wire_deal_pointers(self) -> None:
        """`enrollment.crm_deal_id` on ~60% of linkable enrollments, plus the C9 plants.

        `crm_deal_id` is the pointer **under test** by C9 and is never a link rule
        (SS4.5), so a stale value cannot change which deal a person resolves to.
        """
        rng = self.rng
        deal_by_deal_id = {str(deal["deal_id"]): deal for deal in self.deals}
        # `G20` requires every branch-2 target deal to resolve by `D2` to exactly one
        # OTHER person "whose contact is `L1`- or `L2`-linked so the resolution is
        # guaranteed". `household.role` is a *household* role and says nothing about the
        # child-scoped plants, so a household holding a C4 plant (forced onto `L3` by
        # `G19`) or a C10 collapsed contact was an eligible target -- it only never got
        # picked because the roles used to be laid out in contiguous index blocks the
        # cursor walked past. Excluding them by the roles themselves is what makes the
        # precondition hold by construction rather than by accident of ordering.
        _link_altering_roles = {"c4", "c3", "c10_a", "c10_b", "c14_name", "c14_dob"}
        single_targets = [
            household
            for household in self.households
            if household.size == 1
            and household.deal_id is not None
            and household.role == "plain"
            and not (household.children[0].roles & _link_altering_roles)
        ]
        target_cursor = 0

        for child in self.children:
            enrollment = child.enrollment
            household = child.household
            if enrollment is None:
                continue

            if "c9_missing" in child.roles:
                stale_id = self._make_supernumerary_deal(household)
                enrollment["crm_deal_id"] = stale_id
                self.deleted_deal_ids.add(stale_id)
                self.plants.append(
                    Plant(
                        conflict_type="C9",
                        student_id=child.student_id,
                        enrollment_id=str(enrollment["id"]),
                        detail={"branch": "deal_absent_gen3", "crm_deal_id": stale_id},
                    )
                )
                continue

            if "c9_other" in child.roles:
                target = None
                while target_cursor < len(single_targets):
                    candidate = single_targets[target_cursor]
                    target_cursor += 1
                    if candidate.index != household.index:
                        target = candidate
                        break
                if target is None or target.deal_id is None:  # pragma: no cover
                    raise RuntimeError(
                        "no single-child-household deal is left for a C9 branch-2 plant"
                    )
                enrollment["crm_deal_id"] = target.deal_id
                self.plants.append(
                    Plant(
                        conflict_type="C9",
                        student_id=child.student_id,
                        enrollment_id=str(enrollment["id"]),
                        detail={
                            "branch": "deal_other_person",
                            "crm_deal_id": target.deal_id,
                            "target_household": target.index,
                        },
                    )
                )
                continue

            if household.deal_id is not None and rng.chance(0.60):
                enrollment["crm_deal_id"] = household.deal_id

        # `G39`: no emitted amount may sit on a half-cent boundary.
        for deal in deal_by_deal_id.values():
            scaled = float(deal["amount"]) * 100  # type: ignore[arg-type]
            if abs(scaled - int(scaled) - 0.5) < 1e-9:  # pragma: no cover - built from cents
                raise RuntimeError(f"deal {deal['deal_id']} sits on a half-cent boundary (G39)")

    def _make_supernumerary_deal(self, household: Household) -> str:
        """SS11.7: a deal that exists in generations 1-2 and is deleted before gen 3.

        Supernumerary on purpose -- the pointing household keeps its own live gen-3
        `D2` deal, so the deletion creates a stale pointer without turning the person
        into an unplanted C1 (`G9`).
        """
        deal_id = self._make_deal(household)
        return deal_id

    # ==================================================================================
    # Phase 7 -- the C14 stage plant, oscillation, timestamp dirt, plant registry
    # ==================================================================================
    def finalize(self) -> None:
        self._apply_c14_stage()
        self._enforce_c13_staleness()
        self._detach_dropped_child_pointer()
        self._register_person_plants()
        self._apply_oscillation()
        self._apply_timestamp_dirt()

    def _enforce_c13_staleness(self) -> None:
        """`G15`: every C13 plant has `refunded_at > enrollment.updated_at`, with room.

        Clause (c) of C13 is the **single** permitted read of `updated_at` by any rule,
        so the relationship is set here rather than left to two independent draws --
        and `G26` keeps the ~0.5% out-of-order dirt off exactly these enrollments.
        """
        for child in self.children:
            if child.household.role != "c13" or child.enrollment is None:
                continue
            refunded = [p for p in child.payments if p["status"] == "refunded"]
            if not refunded:  # pragma: no cover - a C13 household always refunds
                raise RuntimeError("C13 plant has no refunded payment")
            refunded_at = str(refunded[0]["refunded_at"])
            enrollment = child.enrollment
            enrollment["updated_at"] = _plus_days(refunded_at, -2)
            enrollment["created_at"] = _plus_days(refunded_at, -30)

    def _detach_dropped_child_pointer(self) -> None:
        """A C8 `crm`-dropped child may not also carry a stale `crm_deal_id`.

        Its household deal resolves by `D2` to the *siblings* -- a non-empty person set
        that does not contain the dropped child -- which is C9's second branch verbatim.
        That would be an unbudgeted C9 the sweep would (correctly) refuse, so the
        pointer is nulled: a null `crm_deal_id` is explicitly not a conflict (SS5.5 C9).
        """
        for child in self.children:
            if "c8_dropped_crm" in child.roles and child.enrollment is not None:
                child.enrollment["crm_deal_id"] = None

    def _apply_c14_stage(self) -> None:
        """SS5.6 C14 stage-only: `crm.deal.stage` off the enrollment funnel.

        `G18` relaxation (ii): the plants sit in single-child households so no sibling
        inherits the disagreement.
        """
        deal_index = {str(deal["deal_id"]): deal for deal in self.deals}
        for child in self.children:
            if "c14_stage" not in child.roles:
                continue
            household = child.household
            if household.deal_id is None:  # pragma: no cover - plants are deal-bearing
                raise RuntimeError("C14 stage plant has no deal")
            deal = deal_index[household.deal_id]
            others = tuple(funnel for funnel in _FUNNEL_TO_DEAL_STAGE if funnel != household.funnel)
            deal["stage"] = _FUNNEL_TO_DEAL_STAGE[self.rng.pick(others)]

    def _register_person_plants(self) -> None:
        """Record every person-scoped plant. Refs are derived in pass 2, never here."""
        for child in self.children:
            roles = child.roles
            household = child.household
            enrollment_id = None if child.enrollment is None else str(child.enrollment["id"])
            contact_id = None if child.contact is None else str(child.contact["crm_id"])

            if household.role == "c1" and household.size == 1:
                self.plants.append(
                    Plant(
                        conflict_type="C1",
                        student_id=child.student_id,
                        enrollment_id=enrollment_id,
                    )
                )
            if household.role == "c7" and household.size == 1:
                self.plants.append(
                    Plant(
                        conflict_type="C7",
                        student_id=child.student_id,
                        enrollment_id=enrollment_id,
                    )
                )
            if household.role == "c13" and household.size == 1:
                refunded = [p for p in child.payments if p["status"] == "refunded"]
                self.plants.append(
                    Plant(
                        conflict_type="C13",
                        student_id=child.student_id,
                        enrollment_id=enrollment_id,
                        payment_ids=(str(refunded[0]["payment_id"]),),
                    )
                )
            if "c5" in roles:
                self.plants.append(Plant(conflict_type="C5", student_id=child.student_id))
            if "c4" in roles and contact_id is not None:
                self.plants.append(
                    Plant(
                        conflict_type="C4",
                        student_id=child.student_id,
                        contact_ids=(contact_id,),
                        expected_methods=("L3",),
                    )
                )
            if "c12" in roles:
                offending = [
                    str(p["payment_id"])
                    for p in child.payments
                    if p["type"] in _BASE_PAID_TYPES
                    and int(p["amount_cents"]) != FEE_SCHEDULE[(household.program, str(p["type"]))]
                ]
                if offending:
                    self.plants.append(
                        Plant(
                            conflict_type="C12",
                            student_id=child.student_id,
                            payment_ids=(offending[0],),
                        )
                    )
            if roles & {"c6_grade", "c6_lifecycle", "c6_mixed"}:
                self.plants.append(
                    Plant(
                        conflict_type="C6",
                        student_id=child.student_id,
                        contact_ids=() if contact_id is None else (contact_id,),
                        detail={"shape": sorted(roles & {"c6_grade", "c6_lifecycle", "c6_mixed"})},
                    )
                )
            if roles & {"c14_name", "c14_dob", "c14_stage"}:
                self.plants.append(
                    Plant(
                        conflict_type="C14",
                        student_id=child.student_id,
                        contact_ids=() if contact_id is None else (contact_id,),
                        detail={"shape": sorted(roles & {"c14_name", "c14_dob", "c14_stage"})},
                    )
                )
            if "c8_dropped_crm" in roles:
                self.plants.append(
                    Plant(
                        conflict_type="C8",
                        student_id=child.student_id,
                        detail={"dropped_source": "crm", "household": household.index},
                    )
                )
            if "c8_dropped_payments" in roles:
                self.plants.append(
                    Plant(
                        conflict_type="C8",
                        student_id=child.student_id,
                        detail={"dropped_source": "payments", "household": household.index},
                    )
                )

    def _apply_oscillation(self) -> None:
        """SS7: >=25 fields re-assert their gen-1 value in gen 3 (A -> B -> A).

        The field has to be one the app DB disagrees with, so the set is drawn from the
        planted C6 persons: gen 1 carries the wrong value, gen 2 the corrected one, gen 3
        the wrong value again. That is exactly A.4's "integration that re-asserts stale
        data after correction".
        """
        wanted = self.plan.oscillating_fields
        # The set is drawn from BOTH oscillatable paths, roughly half and half. Taking
        # them in child order filled the whole quota from `crm.contact.grade` (the grade
        # plants outnumber the lifecycle plants 380:120 and come first), which left the
        # `lifecycle_stage` branch dead code and exercised R4/R16's A -> B -> A scan on a
        # single field path. `sc_oscillation_spread` binds the result.
        grade_quota = wanted // 2
        lifecycle_quota = wanted - grade_quota
        grade_rows: list[dict[str, object]] = []
        lifecycle_rows: list[dict[str, object]] = []
        for child in self.children:
            if len(grade_rows) >= grade_quota and len(lifecycle_rows) >= lifecycle_quota:
                break
            contact = child.contact
            if contact is None:
                continue
            if ("c6_grade" in child.roles or "c6_mixed" in child.roles) and len(
                grade_rows
            ) < grade_quota:
                grade_rows.append(
                    {
                        "crm_id": str(contact["crm_id"]),
                        "field_path": "crm.contact.grade",
                        "student_id": child.student_id,
                        "gen1": contact["grade"],
                        "gen2": child.grade,
                        "gen3": contact["grade"],
                    }
                )
            elif "c6_lifecycle" in child.roles and len(lifecycle_rows) < lifecycle_quota:
                agreeing = _LIFECYCLE_PREIMAGE.get(STATUS_TO_FUNNEL[child.status])
                if not agreeing:
                    continue
                lifecycle_rows.append(
                    {
                        "crm_id": str(contact["crm_id"]),
                        "field_path": "crm.contact.lifecycle_stage",
                        "student_id": child.student_id,
                        "gen1": contact["lifecycle_stage"],
                        "gen2": agreeing[0],
                        "gen3": contact["lifecycle_stage"],
                    }
                )
        # A profile whose lifecycle pool cannot fill its half tops up from grade rather
        # than emitting fewer than `wanted` fields -- A.4's >=25 floor is not negotiable.
        self.oscillations.extend(grade_rows)
        self.oscillations.extend(lifecycle_rows)
        if len(self.oscillations) < wanted:
            for child in self.children:
                if len(self.oscillations) >= wanted:
                    break
                contact = child.contact
                if contact is None:
                    continue
                if "c6_grade" not in child.roles and "c6_mixed" not in child.roles:
                    continue
                crm_id = str(contact["crm_id"])
                if any(row["crm_id"] == crm_id for row in self.oscillations):
                    continue
                self.oscillations.append(
                    {
                        "crm_id": crm_id,
                        "field_path": "crm.contact.grade",
                        "student_id": child.student_id,
                        "gen1": contact["grade"],
                        "gen2": child.grade,
                        "gen3": contact["grade"],
                    }
                )
        if len(self.oscillations) < wanted:  # pragma: no cover - C6 budget always covers it
            raise RuntimeError(
                f"only {len(self.oscillations)} oscillating fields; SS7 requires >= {wanted}"
            )

    def _apply_timestamp_dirt(self) -> None:
        """A.3 / `G26`: `updated_at < created_at` on ~0.5% of **each** entity type.

        Enrollments whose student holds a `refunded` payment are excluded, which is
        what keeps C13 clause (c) -- the single permitted read of `updated_at` by any
        rule -- dirt-free by construction.
        """
        refund_students = {
            str(child.student_id)
            for child in self.children
            if any(p["status"] == "refunded" for p in child.payments)
        }
        rng = self.rng.fork("timestamp-dirt")

        def skew(record: dict[str, object]) -> None:
            created = str(record["created_at"])
            record["updated_at"] = created
            record["created_at"] = _plus_days(created, rng.randint(1, 30))

        buckets: list[tuple[str, list[dict[str, object]]]] = [
            ("crm.contact", self.contacts),
            ("crm.deal", self.deals),
            ("appdb.student", self.students),
            ("payments.payment", self.payments),
            (
                "appdb.enrollment",
                [
                    enrollment
                    for enrollment in self.enrollments
                    if str(enrollment["student_id"]) not in refund_students
                ],
            ),
        ]
        self.timestamp_dirt: dict[str, int] = {}
        for name, records in buckets:
            count = max(1, int(len(records) * 0.005))
            for record in rng.sample(records, count):
                skew(record)
            self.timestamp_dirt[name] = count


def _plus_days(timestamp: str, days: int) -> str:
    from datetime import datetime, timedelta

    parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
    return (parsed + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_dataset(seed: int, plan: Plan) -> Dataset:
    """Run pass 1 end to end and return everything it materialised."""
    builder = _Builder(seed, plan)
    builder.build_households()
    builder.assign_roles()
    builder.build_students_and_enrollments()
    builder.build_contacts()
    builder.build_payments()
    builder.build_deals()
    builder.finalize()

    return Dataset(
        seed=seed,
        plan=plan,
        households=builder.households,
        children=builder.children,
        contacts=builder.contacts,
        deals=builder.deals,
        students=builder.students,
        enrollments=builder.enrollments,
        payments=builder.payments,
        deleted_contact_ids=builder.deleted_contact_ids,
        deleted_deal_ids=builder.deleted_deal_ids,
        deleted_payment_ids=builder.deleted_payment_ids,
        oscillations=builder.oscillations,
        plants=builder.plants,
        name_collision_allowlist=builder.name_collision_allowlist,
    )
