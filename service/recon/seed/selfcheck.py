"""SS9.1 -- the manifest self-check: one named assertion per SS10 construction constraint.

It runs **after** pass 2 and **before** `golden/` is written. Any failure fails the seed
run loudly and no golden tree is produced (`G31`). That is the whole point of the
section: every false-positive guard in SS5.5 is a construction constraint on the
generator, and a constraint with no assertion behind it is a hope.

Read the report top-down: SS9.1(a) volumes/generations/links first, then the SS9.1(b)
construction sweep (the raw population per class, before and after `PRECEDENCE`), then
`G1..G39` in order. Each row prints the check name the contract names it by, so a
failure is greppable straight back into `docs/invariant-contract.md`.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from recon.er import Resolution, Snapshot, resolve
from recon.normalize import GMAIL_DOMAINS, QUOTE_CHARS, norm_dob, norm_email, norm_enum, norm_name
from recon.reference import (
    A1_VOLUMES,
    C11_PLANT_MAX_SECONDS,
    C11_WINDOW_SECONDS,
    CONFLICT_MINIMUMS,
    ENROLLMENT_GRADE_FLOOR,
    FEE_SCHEDULE,
    GRADE_ORDER,
    LEGIT_REPEAT_MIN_SECONDS,
    LIFECYCLE_TO_FUNNEL,
    MAX_PAYLOAD_BYTES,
    PAID_IMPLYING_STAGES,
    PRECEDENCE,
    STATUS_TO_FUNNEL,
    grade_ord,
    make_ref,
)

from .build import Dataset
from .emit import dumps
from .generations import snapshot_records
from .golden import GoldenSet, build_golden
from .plan import Plan
from .rng import Rng
from .sweep import SweepResult, World, _seconds, run_sweep

__all__ = [
    "MALFORMED_PK_FIELDS",
    "TIMESTAMP_DIRT_BAND",
    "TIMESTAMP_DIRT_RATE",
    "CheckResult",
    "SelfCheckReport",
    "declared_primary_keys",
    "fixture_primary_keys",
    "malformed_pk_collisions",
    "run_self_check",
    "timestamp_dirt_problems",
]

_MASK_FLOOR = GRADE_ORDER[ENROLLMENT_GRADE_FLOOR]

# ---------------------------------------------------------------------------------------
# G26 -- the A.3 out-of-order-timestamp rate
# ---------------------------------------------------------------------------------------

#: A.3: `updated_at < created_at` on **~0.5%** of records, per entity type. This is the
#: rate `recon.seed.build._apply_timestamp_dirt` draws at, and the number A.3 asks for.
TIMESTAMP_DIRT_RATE: float = 0.005

#: The band `sc_timestamp_dirt_spread` asserts, and the reason it is a band rather than
#: the rate itself.
#:
#: The generator skews `int(len(records) * 0.005)` records, so the achieved rate is
#: `0.005` minus at most one record's worth of flooring -- which at `dev` volumes is
#: visible (`crm.deal`: 3/753 = 0.398%). Enrollments come in lower again on purpose:
#: their pool excludes every enrollment whose student holds a refunded payment, so that
#: C13 clause (c) -- the single permitted read of `updated_at` by any rule -- stays
#: dirt-free, while this rate is measured against *all* enrollments.
#:
#: `[0.3%, 0.7%]` is 0.5% +/- 0.2pp. Measured at seed 20260822 the widest excursions are
#: `dev crm.deal` at 0.398% and `appdb.student` at 0.500%, so both profiles sit inside it
#: with margin -- and a bucket that lost its dirt entirely (0%) or gained ten times too
#: much (5%) is outside it, which the assertion this replaced (`count > 0`) was not.
TIMESTAMP_DIRT_BAND: tuple[float, float] = (0.003, 0.007)


def _pct(fraction: float) -> str:
    return f"{fraction * 100:.1f}%"


def timestamp_dirt_problems(skewed: Mapping[str, int], totals: Mapping[str, int]) -> list[str]:
    """Entity types whose out-of-order-timestamp rate is outside :data:`TIMESTAMP_DIRT_BAND`.

    Split out of the check so the band can be exercised directly against counts the
    generator did not produce: a rule that has only ever been shown its own happy path
    is not known to be able to fail.
    """
    low, high = TIMESTAMP_DIRT_BAND
    problems: list[str] = []
    for label in sorted(skewed):
        total = totals.get(label, 0)
        if total <= 0:
            problems.append(f"{label}: no records at all")
            continue
        rate = skewed[label] / total
        if not low <= rate <= high:
            problems.append(
                f"{label}: {skewed[label]}/{total} = {_pct(rate)} out-of-order timestamps, "
                f"outside A.3's {_pct(low)}-{_pct(high)} band"
            )
    return problems


# ---------------------------------------------------------------------------------------
# G27 -- malformed-corpus isolation
# ---------------------------------------------------------------------------------------

#: SS7 -- the field each source's payload declares its primary key under. The malformed
#: corpus keys itself in a 9,000,000 band (`CRM-90000NN`, `DEAL-90000NN`, `pi_90000NN`,
#: `6d9f0d2c-0000-5000-8000-0000000000NN`) precisely so a rejected payload can never name
#: a record the generator emitted; :func:`malformed_pk_collisions` is what holds that.
MALFORMED_PK_FIELDS: Mapping[str, str] = {
    "contact": "crm_id",
    "deal": "deal_id",
    "student": "id",
    "enrollment": "id",
    "payment": "payment_id",
}


def declared_primary_keys(case: Mapping[str, Any]) -> list[str]:
    """The primary-key values a malformed payload declares, in payload order.

    Read with a regex rather than `json.loads`, because **most of this corpus does not
    parse** -- that is what makes it the malformed corpus. A truncated body
    (`MAL-012`), a trailing comma (`MAL-015`) and a non-object line (`MAL-021`) all
    still have to be searched, and a parser gives up on the first two and returns no
    mapping for the third.

    Only the key's own field is read. The deal cases deliberately carry a **real**
    `associated_contact_ids` (`CRM-0000001`) so the payload is realistic apart from the
    one thing that is broken about it, and that is a foreign key on a record the adapter
    rejects before it can land -- not a claim to be that record. Searching the whole raw
    string for any fixture id, which is what the guard here was originally shaped like,
    would flag those three cases on every run.
    """
    field = MALFORMED_PK_FIELDS.get(str(case.get("entity_type", "")))
    if field is None:
        return []
    pattern = rf'"{re.escape(field)}"\s*:\s*"([^"]*)"'
    return re.findall(pattern, str(case.get("raw", "")))


def fixture_primary_keys(dataset: Dataset) -> dict[str, frozenset[str]]:
    """Every primary key the generator emitted, by entity type."""
    return {
        "contact": frozenset(str(row["crm_id"]) for row in dataset.contacts),
        "deal": frozenset(str(row["deal_id"]) for row in dataset.deals),
        "student": frozenset(str(row["id"]) for row in dataset.students),
        "enrollment": frozenset(str(row["id"]) for row in dataset.enrollments),
        "payment": frozenset(str(row["payment_id"]) for row in dataset.payments),
    }


def malformed_pk_collisions(
    fixture_keys: Mapping[str, frozenset[str]],
    malformed: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Malformed cases whose own primary key is a key the generator also emitted.

    `G27`'s isolation clause. SS7's corpus exists to prove the adapter boundary rejects
    a structurally broken payload; a case that claimed a *real* record's primary key
    would stop being a boundary test and become a collision with the dataset -- and
    `MAL-019`/`MAL-020`, the pair whose whole purpose is to produce a `409` against each
    other, would produce it against a generated contact instead.

    Deterministic: the result is built by walking `malformed` in its committed order and
    each payload left to right. `fixture_keys` is only ever membership-tested, never
    iterated, so no set ordering reaches the output.
    """
    collisions: list[str] = []
    for case in malformed:
        entity = str(case.get("entity_type", ""))
        known = fixture_keys.get(entity, frozenset())
        for value in declared_primary_keys(case):
            if value in known:
                collisions.append(f"{case.get('case_id')} reuses {entity} PK {value!r}")
    return collisions


@dataclass(frozen=True)
class CheckResult:
    name: str
    constraint: str
    passed: bool
    detail: str


@dataclass
class SelfCheckReport:
    profile: str
    seed: int
    results: list[CheckResult] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def failures(self) -> list[CheckResult]:
        return [result for result in self.results if not result.passed]

    @property
    def passed(self) -> bool:
        return not self.failures

    def render(self) -> str:
        width = max((len(result.name) for result in self.results), default=10)
        lines = [
            "=" * 78,
            f"manifest self-check  profile={self.profile}  seed={self.seed}",
            "=" * 78,
        ]
        for result in self.results:
            mark = "PASS" if result.passed else "FAIL"
            lines.append(
                f"  [{mark}] {result.name:<{width}}  {result.constraint:<5} {result.detail}"
            )
        lines.append("-" * 78)
        for key in sorted(self.facts):
            lines.append(f"  {key} = {self.facts[key]}")
        lines.append("-" * 78)
        lines.append(
            f"  {len(self.results) - len(self.failures)}/{len(self.results)} checks passed"
            + ("" if self.passed else f"  ({len(self.failures)} FAILED)")
        )
        lines.append("=" * 78)
        return "\n".join(lines)


class _Checker:
    def __init__(
        self,
        dataset: Dataset,
        snapshots: Mapping[int, Snapshot],
        resolutions: Mapping[int, Resolution],
        sweep: SweepResult,
        golden: GoldenSet,
        malformed: Sequence[Mapping[str, Any]],
        fixtures_dir: Path | None = None,
        file_manifest: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.dataset = dataset
        self.fixtures_dir = fixtures_dir
        self.file_manifest = file_manifest
        self.plan: Plan = dataset.plan
        self.snapshots = snapshots
        self.resolutions = resolutions
        self.sweep = sweep
        self.golden = golden
        self.malformed = malformed
        self.world: World = sweep.world
        self.gen3: Resolution = resolutions[3]
        self.report = SelfCheckReport(profile=self.plan.profile, seed=dataset.seed)
        self.child_by_student_ref = {
            make_ref("appdb", "student", child.student_id): child for child in dataset.children
        }

    # ---------------------------------------------------------------------------------
    def check(self, name: str, constraint: str, passed: bool, detail: str) -> None:
        self.report.results.append(CheckResult(name, constraint, bool(passed), detail))

    def fact(self, key: str, value: Any) -> None:
        self.report.facts[key] = value

    # ---------------------------------------------------------------------------------
    def run(self) -> SelfCheckReport:
        self._volumes_generations_links()
        self._construction_sweep()
        self._g1_g5()
        self._g6_g12()
        self._g13_g18()
        self._g19_g23()
        self._g24_g30()
        self._structural_minimums()
        self._plantability()
        self._g32_g39()
        return self.report

    # == SS9.1(a) =====================================================================
    def _volumes_generations_links(self) -> None:
        plan = self.plan
        counts: dict[str, int] = {}
        for generation in (1, 2, 3):
            for source, entity in (
                ("crm", "contact"),
                ("crm", "deal"),
                ("appdb", "student"),
                ("appdb", "enrollment"),
                ("payments", "payment"),
            ):
                counts[f"gen{generation}.{source}.{entity}"] = len(
                    snapshot_records(self.dataset, source, entity, generation)
                )
        self.fact("gen3_counts", {k[5:]: v for k, v in counts.items() if k.startswith("gen3")})
        self.fact("gen1_counts", {k[5:]: v for k, v in counts.items() if k.startswith("gen1")})

        volume_problems = [
            f"{source}.{entity}={counts[f'gen3.{source}.{entity}']} != {expected}"
            for (source, entity), expected in sorted(plan.volumes.items())
            if counts[f"gen3.{source}.{entity}"] != expected
        ]
        self.check(
            "sc_gen3_volumes",
            "SS9.1a",
            not volume_problems,
            "generation-3 record counts equal the A.1 volumes exactly"
            if not volume_problems
            else "; ".join(volume_problems),
        )

        gen1_expected = {
            "crm.contact": plan.volumes[("crm", "contact")] + plan.c8_crm,
            "crm.deal": plan.volumes[("crm", "deal")] + plan.c9_missing,
            "appdb.student": plan.volumes[("appdb", "student")],
            "appdb.enrollment": plan.volumes[("appdb", "enrollment")],
            "payments.payment": plan.volumes[("payments", "payment")] + plan.c8_payments,
        }
        gen1_problems = [
            f"{key}={counts[f'gen1.{key}']} != {expected}"
            for key, expected in sorted(gen1_expected.items())
            if counts[f"gen1.{key}"] != expected
        ]
        self.check(
            "sc_gen1_carries_deletions",
            "D-12",
            not gen1_problems,
            "generations 1-2 carry the records deleted before gen 3"
            if not gen1_problems
            else "; ".join(gen1_problems),
        )

        methods: dict[str, int] = {}
        for link in self.gen3.links:
            if link.link_class == "contact_student":
                methods[link.method] = methods.get(link.method, 0) + 1
        self.fact("contact_link_methods", dict(sorted(methods.items())))
        unlinked_contacts = len(self.world.contacts) - sum(methods.values())
        contactless_students = sum(
            1
            for person in self.gen3.persons
            if person.student_ref is not None and not person.contact_refs
        )
        self.check(
            "sc_link_path_distribution",
            "G3",
            methods.get("L3", 0) == plan.conflicts["C4"]
            and unlinked_contacts == plan.leads
            and contactless_students == plan.appdb_only + plan.c8_crm,
            f"L3={methods.get('L3', 0)} (== C4 plants), unlinked contacts={unlinked_contacts} "
            f"(== {plan.leads} leads), contact-less students={contactless_students} "
            f"(== {plan.appdb_only} {{appdb}}-only + {plan.c8_crm} C8 crm-drops)",
        )

    # == SS9.1(b) =====================================================================
    def _construction_sweep(self) -> None:
        plan = self.plan
        expected_raw = {
            "C1": plan.conflicts["C1"] + plan.c8_crm,
            "C2": plan.conflicts["C2"],
            "C3": plan.conflicts["C3"],
            "C4": plan.conflicts["C4"],
            "C5": plan.conflicts["C5"],
            "C6": plan.conflicts["C6"],
            "C7": plan.conflicts["C7"]
            + plan.conflicts["C13"]
            + plan.conflicts["C5"]
            + plan.c8_payments,
            "C8": plan.conflicts["C8"],
            "C9": plan.conflicts["C9"],
            "C10": plan.conflicts["C10"],
            "C11": plan.conflicts["C11"],
            "C12": plan.conflicts["C12"],
            "C13": plan.conflicts["C13"],
            "C14": plan.conflicts["C14"] + plan.conflicts["C10"],
        }
        raw = {key: self.sweep.counts.get(key, 0) for key in expected_raw}
        surviving: dict[str, int] = {}
        for entry in self.golden.survivors:
            surviving[entry.type] = surviving.get(entry.type, 0) + 1
        expected_surviving = dict(plan.conflicts)

        raw_problems = [
            f"{key}: raw {raw[key]} != {expected_raw[key]}"
            for key in sorted(expected_raw)
            if raw[key] != expected_raw[key]
        ]
        surv_problems = [
            f"{key}: surviving {surviving.get(key, 0)} != {expected_surviving[key]}"
            for key in sorted(expected_surviving)
            if surviving.get(key, 0) != expected_surviving[key]
        ]
        self.fact("sweep_raw", dict(sorted(raw.items())))
        self.fact("sweep_surviving", dict(sorted(surviving.items())))
        self.check(
            "sc_construction_sweep",
            "G31c",
            not raw_problems and not surv_problems,
            "every class finds exactly the planted population, before and after PRECEDENCE"
            if not (raw_problems or surv_problems)
            else "; ".join([*raw_problems, *surv_problems]),
        )

        zero_fire = [
            rule.index
            for rule in PRECEDENCE
            if rule.expected_fire_count == 0
            and self.golden.suppression_report.get(rule.index, 0) != 0
        ]
        self.fact("precedence_fired", dict(sorted(self.golden.suppression_report.items())))
        self.check(
            "sc_precedence_zero_fire",
            "SS9.1b",
            not zero_fire,
            "PRECEDENCE 6 (C9>C1) and 7 (C10>C5) fire zero times, as the contract asserts"
            if not zero_fire
            else f"rules {zero_fire} fired but must not",
        )

    # == G1..G5 =======================================================================
    def _g1_g5(self) -> None:
        dataset = self.dataset
        problems: list[str] = []
        for household in dataset.households:
            key = norm_email(household.guardian_email)
            for child in household.children:
                if norm_email(child.student.get("guardian_email")) != key:
                    problems.append(f"student {child.student_id} guardian_email off household key")
                contact = child.contact
                clean_contact = contact is not None and "c4" not in child.roles
                if clean_contact and norm_email(contact.get("email")) != key:
                    problems.append(f"contact {contact['crm_id']} email off household key")
                for payment in child.payments:
                    if norm_email(payment.get("payer_email")) != key:
                        problems.append(f"payment {payment['payment_id']} payer off household key")
        self.check(
            "sc_household_key_exact",
            "G1",
            not problems,
            "every child of a household shares one guardian_email; contact.email and "
            "payer_email are drawn from it except on planted C4 / C2"
            if not problems
            else f"{len(problems)} violations, first: {problems[0]}",
        )

        # G2 -- presence masks are drawn at the household level.
        mask_problems: list[str] = []
        for household in dataset.households:
            if household.size < 2 or household.role in {"c8_crm", "c8_payments"}:
                continue
            masks = set()
            for child in household.children:
                person = self.world.person_of_student(
                    make_ref("appdb", "student", child.student_id)
                )
                if person is None:  # pragma: no cover
                    continue
                masks.add((bool(person.contact_refs), bool(person.payment_refs)))
            if len(masks) > 1:
                mask_problems.append(f"household {household.index}")
        self.check(
            "sc_household_mask_uniform",
            "G2",
            not mask_problems,
            "all children of every clean multi-child household share one {crm, payments} mask"
            if not mask_problems
            else f"{len(mask_problems)} non-uniform households",
        )

        # G4 -- dot / `+alias` variation only on gmail domains.
        variant_problems: list[str] = []
        for value in self._all_addresses():
            if value is None:
                continue
            text = value.strip().strip(QUOTE_CHARS).strip().casefold()
            if "@" not in text:
                continue
            local, _, domain = text.rpartition("@")
            if domain in GMAIL_DOMAINS:
                continue
            if "+" in local or norm_email(value) != text:
                variant_problems.append(value)
        self.check(
            "sc_email_variant_domain",
            "G4",
            not variant_problems,
            "local-part variation appears on gmail.com / googlemail.com only"
            if not variant_problems
            else f"{len(variant_problems)} non-gmail variants, first: {variant_problems[0]!r}",
        )

        # G5 -- the `namedob` key resolves to at most one PERSON.
        by_tuple: dict[tuple[str, str, str | None], set[str]] = {}
        for person in self.gen3.persons:
            if person.student_ref is not None:
                student = self.world.students[person.student_ref]
                self._register_tuple(by_tuple, person.person_key, student, "student")
            for contact_ref in person.contact_refs:
                contact = self.world.contacts[contact_ref]
                self._register_tuple(by_tuple, person.person_key, contact, "contact")
        allow = {
            (norm_name(first), norm_name(last), norm_dob(dob))
            for first, last, dob in dataset.name_collision_allowlist
        }
        allow |= {(first, last, None) for first, last, _dob in allow}
        collisions = [
            key
            for key, persons in sorted(by_tuple.items(), key=lambda item: tuple(map(str, item[0])))
            if len(persons) > 1 and key not in allow
        ]
        self.check(
            "sc_namekey_unique",
            "G5",
            not collisions,
            "every (first, last, dob) tuple resolves to at most one person outside the "
            "C3 / C10 allowlist"
            if not collisions
            else f"{len(collisions)} unallowlisted collisions, first: {collisions[0]}",
        )

    def _register_tuple(
        self,
        table: dict[tuple[str, str, str | None], set[str]],
        person_key: str,
        record: Mapping[str, Any],
        _kind: str,
    ) -> None:
        first = norm_name(record.get("first_name"))
        last = norm_name(record.get("last_name"))
        if first is None or last is None:
            return
        dob = norm_dob(record.get("dob"))
        table.setdefault((first, last, dob), set()).add(person_key)

    def _all_addresses(self) -> Iterable[str | None]:
        for contact in self.dataset.contacts:
            yield contact.get("email")  # type: ignore[misc]
        for student in self.dataset.students:
            yield student.get("guardian_email")  # type: ignore[misc]
            yield student.get("guardian2_email")  # type: ignore[misc]
        for payment in self.dataset.payments:
            yield payment.get("payer_email")  # type: ignore[misc]

    # == G6..G12 ======================================================================
    def _g6_g12(self) -> None:
        plan = self.plan
        dataset = self.dataset

        unattributable = len(self.gen3.unattributed_payment_refs)
        joint_gap = [
            str(payment["payment_id"])
            for payment in snapshot_records(dataset, "payments", "payment", 3)
            if payment.get("external_ref") is None
            and not (
                (payment.get("metadata") or {}).get("student_first_name") is not None
                and (payment.get("metadata") or {}).get("student_last_name") is not None
            )
        ]
        self.check(
            "sc_payment_attributable",
            "G6",
            unattributable == plan.conflicts["C2"] and len(joint_gap) == plan.conflicts["C2"],
            f"unattributable payments={unattributable} == planted C2; "
            f"joint external_ref+metadata gaps={len(joint_gap)} == planted C2",
        )

        # G7 -- the repeat guard band.
        planted_pairs = {
            tuple(sorted(plant.payment_ids))
            for plant in dataset.plants
            if plant.conflict_type == "C11"
        }
        band_problems: list[str] = []
        by_person: dict[tuple[str, int, str, str], list[str]] = {}
        for ref, payment in sorted(self.world.payments.items()):
            person = self.gen3.payment_person.get(ref)
            if person is None:
                continue
            key = (
                person,
                int(payment["amount_cents"]),
                str(payment["type"]),
                norm_email(payment.get("payer_email")) or "",
            )
            by_person.setdefault(key, []).append(ref)
        for refs in by_person.values():
            ordered = sorted(refs)
            for i, left in enumerate(ordered):
                for right in ordered[i + 1 :]:
                    delta = abs(
                        (_seconds(self.world.payments[left]["occurred_at"]) or 0)
                        - (_seconds(self.world.payments[right]["occurred_at"]) or 0)
                    )
                    pair = (
                        self.world.payments[left]["payment_id"],
                        self.world.payments[right]["payment_id"],
                    )
                    planted = tuple(sorted(str(p) for p in pair)) in planted_pairs
                    if planted and delta > C11_PLANT_MAX_SECONDS:
                        band_problems.append(f"planted pair {pair} {delta}s > 300s")
                    if not planted and delta < LEGIT_REPEAT_MIN_SECONDS:
                        band_problems.append(f"legit repeat {pair} {delta}s < 1200s")
        self.check(
            "sc_repeat_guard_band",
            "G7",
            not band_problems,
            "planted C11 pairs <= 300s; every legitimate same-person repeat >= 1200s"
            if not band_problems
            else f"{len(band_problems)} violations, first: {band_problems[0]}",
        )

        # SS5.5's C11 FP guard -- "a sibling pair resolves to two different persons and
        # is therefore never C11" -- is only a guard if its population is non-empty.
        # It was: exactly ONE different-person pair in the whole dataset landed inside
        # the 600s window, so the clause was effectively untested. Count it, report it,
        # and fail the run if it ever silently goes to zero again.
        by_key: dict[tuple[str, int, str], list[str]] = {}
        for ref, payment in sorted(self.world.payments.items()):
            by_key.setdefault(
                (
                    norm_email(payment.get("payer_email")) or "",
                    int(payment["amount_cents"]),
                    str(payment["type"]),
                ),
                [],
            ).append(ref)
        cross_person_window = 0
        for refs in by_key.values():
            ordered = sorted(refs)
            for i, left in enumerate(ordered):
                for right in ordered[i + 1 :]:
                    left_person = self.gen3.payment_person.get(left)
                    right_person = self.gen3.payment_person.get(right)
                    if left_person is None or right_person is None:
                        continue
                    if left_person == right_person:
                        continue
                    delta = abs(
                        (_seconds(self.world.payments[left]["occurred_at"]) or 0)
                        - (_seconds(self.world.payments[right]["occurred_at"]) or 0)
                    )
                    if delta < C11_WINDOW_SECONDS:
                        cross_person_window += 1
        self.fact("c11_guard_population", cross_person_window)
        self.check(
            "sc_c11_guard_population",
            "G7",
            cross_person_window >= self.plan.sibling_window_households,
            f"{cross_person_window} different-person payment pairs share "
            "(payer_email_norm, amount_cents, type) inside C11's 600s window and must "
            "NOT be flagged"
            if cross_person_window >= self.plan.sibling_window_households
            else f"only {cross_person_window} such pairs exist against a planned "
            f"{self.plan.sibling_window_households}: C11's 'both resolve to the same "
            "person' clause has no false-positive population behind it",
        )

        nway = [key for key, refs in sorted(by_person.items()) if len(refs) > 2]
        contact_groups: dict[tuple[str, str, str], int] = {}
        for contact in snapshot_records(dataset, "crm", "contact", 3):
            email = norm_email(contact.get("email"))
            first = norm_name(contact.get("first_name"))
            last = norm_name(contact.get("last_name"))
            if email is None or first is None or last is None:
                continue
            contact_groups[(email, first, last)] = contact_groups.get((email, first, last), 0) + 1
        nway_contacts = [key for key, count in sorted(contact_groups.items()) if count > 2]
        self.check(
            "sc_no_nway_collision",
            "G8",
            not nway and not nway_contacts,
            "exactly two contacts per C3 collision and two payments per C11 key collision"
            if not (nway or nway_contacts)
            else f"payment groups>2={len(nway)}, contact groups>2={len(nway_contacts)}",
        )

        dealless = [
            person
            for person in self.gen3.persons
            if person.student_ref is not None
            and person.enrollment_refs
            and not person.deal_refs
            and any(self.world.payments[ref].get("status") == "paid" for ref in person.payment_refs)
        ]
        self.check(
            "sc_deal_coverage",
            "G9",
            len(dealless) == plan.conflicts["C1"] + plan.c8_crm,
            f"paid+enrolled persons with zero D2 deals={len(dealless)} == "
            f"{plan.conflicts['C1']} C1 + {plan.c8_crm} C8 crm-drops",
        )

        # SS5.2's "entity" includes each payment attributed to NO person; `G10` is about
        # resolved persons, so an unattributed-payment entity (a planted C2) is not one.
        no_enrollment_paid = [
            person
            for person in self.gen3.persons
            if person.student_ref is not None
            and not person.enrollment_refs
            and any(
                ref in self.world.payments and self.world.payments[ref].get("status") == "paid"
                for ref in person.payment_refs
            )
        ]
        self.check(
            "sc_paid_implies_enrollment",
            "G10",
            not no_enrollment_paid,
            "no person holds a paid payment with zero enrollments"
            if not no_enrollment_paid
            else f"{len(no_enrollment_paid)} violations",
        )

        associated: set[str] = set()
        for deal in snapshot_records(dataset, "crm", "deal", 3):
            for crm_id in deal.get("associated_contact_ids") or ():
                associated.add(str(crm_id))
        leads = [
            person
            for person in self.gen3.persons
            if person.student_ref is None and person.contact_refs
        ]
        lead_problems = [
            person.anchor_ref
            for person in leads
            if person.payment_refs
            or person.enrollment_refs
            or any(ref.split(":")[-1] in associated for ref in person.contact_refs)
        ]
        self.check(
            "sc_lead_purity",
            "G11",
            len(leads) == plan.leads and not lead_problems,
            f"{len(leads)} deal-less leads (== {plan.leads}), none with a payment, an "
            "enrollment or a place in any associated_contact_ids"
            if not lead_problems
            else f"{len(lead_problems)} impure leads",
        )

        per_student: dict[str, int] = {}
        for enrollment in snapshot_records(dataset, "appdb", "enrollment", 3):
            ref = make_ref("appdb", "student", enrollment["student_id"])
            per_student[ref] = per_student.get(ref, 0) + 1
        over = [ref for ref, count in sorted(per_student.items()) if count > 1]
        with_one = sum(1 for count in per_student.values() if count == 1)
        self.check(
            "sc_enrollment_cardinality",
            "G12",
            not over and with_one == plan.volumes[("appdb", "enrollment")],
            f"every student has at most one enrollment; {with_one} have exactly one, "
            f"{plan.students - with_one} have none",
        )

    # == G13..G18 =====================================================================
    def _g13_g18(self) -> None:
        dataset = self.dataset
        plan = self.plan

        program_problems: list[str] = []
        for ref, enrollment_ref in sorted(self.gen3.payment_enrollment.items()):
            payment = self.world.payments[ref]
            metadata = payment.get("metadata") or {}
            program = norm_enum("program", metadata.get("program"))
            if program is None:
                continue
            enrollment_program = norm_enum(
                "program", self.world.enrollments[enrollment_ref].get("program")
            )
            if program != enrollment_program:
                program_problems.append(ref)
        self.check(
            "sc_program_consistency",
            "G13",
            not program_problems,
            "metadata.program equals the attributed enrollment's program on every payment"
            if not program_problems
            else f"{len(program_problems)} mismatches",
        )

        # G14 -- the refund closure partition.
        c13_payment_ids = {
            payment_id
            for plant in dataset.plants
            if plant.conflict_type == "C13"
            for payment_id in plant.payment_ids
        }
        shape_i = shape_ii = 0
        refund_problems: list[str] = []
        for ref, payment in sorted(self.world.payments.items()):
            if payment.get("status") != "refunded":
                continue
            person_key = self.gen3.payment_person.get(ref)
            person = None if person_key is None else self.gen3.person_by_key[person_key]
            if person is None:
                refund_problems.append(f"{ref} unattributed")
                continue
            student = self.world.students[person.student_ref] if person.student_ref else None
            household = (
                self.child_by_student_ref[person.student_ref].household
                if person.student_ref in self.child_by_student_ref
                else None
            )
            if household is not None and household.size != 1:
                refund_problems.append(f"{ref} in a multi-child household")
            if str(payment["payment_id"]) in c13_payment_ids:
                continue
            occurred = _seconds(payment.get("occurred_at")) or 0
            superseded = any(
                self.world.payments[other].get("status") == "paid"
                and self.world.payments[other].get("type") == payment.get("type")
                and (_seconds(self.world.payments[other].get("occurred_at")) or 0)
                >= occurred + LEGIT_REPEAT_MIN_SECONDS
                for other in person.payment_refs
                if other in self.world.payments
            )
            enrollment = self.world.survived_enrollment(person)
            closed = (
                enrollment is not None
                and norm_enum("stage", enrollment.get("stage")) in {"refunded", "withdrawn"}
                and student is not None
                and norm_enum("status", student.get("status")) not in {"enrolled", "active"}
            )
            if superseded:
                shape_i += 1
            elif closed:
                shape_ii += 1
            else:
                refund_problems.append(f"{ref} is neither superseded nor closed")
        self.check(
            "sc_refund_closure",
            "G14",
            not refund_problems and shape_i > 0 and shape_ii > 0,
            f"every clean refund is superseded (shape i: {shape_i}) or closed "
            f"(shape ii: {shape_ii}); every refund sits in a single-child household"
            if not refund_problems
            else f"{len(refund_problems)} violations, first: {refund_problems[0]}",
        )

        stale_problems: list[str] = []
        for plant in dataset.plants:
            if plant.conflict_type != "C13":
                continue
            child = self.child_by_student_ref[make_ref("appdb", "student", plant.student_id)]
            enrollment = child.enrollment
            payment = next(
                p for p in child.payments if str(p["payment_id"]) in set(plant.payment_ids)
            )
            if enrollment is None:  # pragma: no cover
                stale_problems.append("no enrollment")
                continue
            refunded_at = _seconds(payment["refunded_at"])
            updated_at = _seconds(enrollment["updated_at"])
            if norm_enum("stage", enrollment["stage"]) not in PAID_IMPLYING_STAGES:
                stale_problems.append(f"{plant.student_id}: enrollment not paid-implying")
            if norm_enum("status", child.student["status"]) not in {"enrolled", "active"}:
                stale_problems.append(f"{plant.student_id}: status not enrolled/active")
            if refunded_at is None or updated_at is None or refunded_at <= updated_at:
                stale_problems.append(f"{plant.student_id}: refunded_at not after updated_at")
        self.check(
            "sc_c13_plant_staleness",
            "G15",
            not stale_problems,
            "every C13 plant leaves both downstream fields stale with refunded_at > updated_at"
            if not stale_problems
            else f"{len(stale_problems)} violations, first: {stale_problems[0]}",
        )

        status_problems: list[str] = []
        for child in dataset.children:
            if child.household.bucket == "tri":
                continue
            if "c5" in child.roles:
                continue
            if norm_enum("status", child.student["status"]) not in {
                "prospect",
                "applied",
                "withdrawn",
            }:
                status_problems.append(child.student_id)
        self.check(
            "sc_partial_presence_status",
            "G16",
            not status_problems,
            "partial-presence students carry status in {prospect, applied, withdrawn} only; "
            "enrolled/active with no contact and no payment is the C5 plant and nothing else"
            if not status_problems
            else f"{len(status_problems)} violations",
        )

        operand_problems: list[str] = []
        for entry in self.sweep.entries:
            if entry.type not in {"C6", "C14"}:
                continue
            for path in entry.disagreeing_fields:
                if entry.observed_values.get(path) is None:
                    operand_problems.append(f"{entry.anchor}:{path}")
        self.check(
            "sc_compare_operands_present",
            "G17",
            not operand_problems,
            "every planted C6/C14 has both operands non-null and normalizing to distinct "
            "non-None canonicals -- a conflict is never created by nulling a field"
            if not operand_problems
            else f"{len(operand_problems)} null operands",
        )

        funnel_problems: list[str] = []
        for household in dataset.households:
            funnels = {
                norm_enum("stage", child.enrollment["stage"])
                for child in household.children
                if child.enrollment is not None
            }
            if len(funnels) > 1:
                funnel_problems.append(f"household {household.index} enrollments {funnels}")
            for child in household.children:
                contact = child.contact
                if contact is None or "c6_lifecycle" in child.roles:
                    continue
                mapped = LIFECYCLE_TO_FUNNEL[
                    norm_enum("lifecycle_stage", contact["lifecycle_stage"])
                ]
                target = STATUS_TO_FUNNEL[norm_enum("status", child.student["status"])]
                if mapped is not None and mapped != target:
                    funnel_problems.append(f"contact {contact['crm_id']} lifecycle off pre-image")
        self.check(
            "sc_household_funnel_uniform",
            "G18",
            not funnel_problems,
            "every household is funnel-uniform and every lifecycle_stage sits in the "
            "LIFECYCLE_TO_FUNNEL pre-image, outside the four named relaxations"
            if not funnel_problems
            else f"{len(funnel_problems)} violations, first: {funnel_problems[0]}",
        )
        self.fact("multi_child_households", sum(1 for h in dataset.households if h.size > 1))
        self.fact("deal_less_leads", plan.leads)

    # == G19..G23 =====================================================================
    def _g19_g23(self) -> None:
        dataset = self.dataset
        problems: list[str] = []
        for plant in dataset.plants:
            if plant.conflict_type != "C4":
                continue
            child = self.child_by_student_ref[make_ref("appdb", "student", plant.student_id)]
            contact = child.contact
            if contact is None:  # pragma: no cover
                problems.append("no contact")
                continue
            guardians = {
                norm_email(child.student.get("guardian_email")),
                norm_email(child.student.get("guardian2_email")),
            }
            domain = str(contact["email"]).rpartition("@")[2].casefold()
            if contact.get("external_id") is not None:
                problems.append(f"{contact['crm_id']} has external_id")
            if norm_dob(contact.get("dob")) is None or norm_dob(contact["dob"]) != norm_dob(
                child.student["dob"]
            ):
                problems.append(f"{contact['crm_id']} dob not equal/non-null")
            if norm_name(contact["first_name"]) != norm_name(child.student["first_name"]):
                problems.append(f"{contact['crm_id']} first name differs")
            if norm_email(contact["email"]) in guardians:
                problems.append(f"{contact['crm_id']} email is a guardian address")
            if domain in GMAIL_DOMAINS:
                problems.append(f"{contact['crm_id']} variant sits on a gmail domain")
        self.check(
            "sc_c4_preconditions",
            "G19",
            not problems,
            "every C4 plant: no external_id, equal non-null DOBs, equal names, a non-gmail "
            "variant address outside the guardian set -- so only L3 can link it"
            if not problems
            else f"{len(problems)} violations, first: {problems[0]}",
        )

        c9_problems: list[str] = []
        for plant in dataset.plants:
            if plant.conflict_type != "C9":
                continue
            deal_id = str(plant.detail["crm_deal_id"])
            deal_ref = make_ref("crm", "deal", deal_id)
            if plant.detail["branch"] == "deal_absent_gen3":
                if deal_ref in self.world.deals:
                    c9_problems.append(f"{deal_id} still present at gen 3")
                child = self.child_by_student_ref[make_ref("appdb", "student", plant.student_id)]
                if child.household.deal_id is None:
                    c9_problems.append(f"{deal_id}: pointing household lost its live deal")
            else:
                persons = self.gen3.deal_persons.get(deal_ref, ())
                if len(persons) != 1:
                    c9_problems.append(f"{deal_id} resolves to {len(persons)} persons, not 1")
                else:
                    other = self.gen3.person_by_key[persons[0]]
                    methods = {
                        self.gen3.contact_method[ref]
                        for ref in other.contact_refs
                        if ref in self.gen3.contact_method
                    }
                    if not methods <= {"L1", "L2"} or not methods:
                        c9_problems.append(f"{deal_id} target contact links by {methods}")
        self.check(
            "sc_c9_preconditions",
            "G20",
            not c9_problems,
            "branch-1 targets are absent from gen 3 while their household keeps a live D2 "
            "deal; branch-2 targets resolve by D2 to exactly one other L1/L2-linked person"
            if not c9_problems
            else f"{len(c9_problems)} violations, first: {c9_problems[0]}",
        )

        c10_problems: list[str] = []
        collapsed_refs: set[str] = set()
        for plant in dataset.plants:
            if plant.conflict_type != "C10":
                continue
            contact_ref = make_ref("crm", "contact", plant.contact_ids[0])
            collapsed_refs.add(contact_ref)
            per_class = self.gen3.candidates_by_contact.get(contact_ref, {})
            ext = per_class.get("ext", ())
            namedob = per_class.get("namedob", ())
            if len(ext) != 1 or len(namedob) != 1 or ext[0] == namedob[0]:
                c10_problems.append(f"{contact_ref} candidates {ext} / {namedob}")
            if self.gen3.contact_method.get(contact_ref) != "L1":
                c10_problems.append(f"{contact_ref} does not link by L1")
            student_b = make_ref("appdb", "student", plant.detail["student_b"])
            person_b = self.world.person_of_student(student_b)
            if person_b is None or not person_b.contact_refs:
                c10_problems.append(f"{student_b} (student B) is contact-less")
            child_a = self.child_by_student_ref[make_ref("appdb", "student", plant.student_id)]
            contact = self.world.contacts[contact_ref]
            if norm_enum("grade", contact["grade"]) != norm_enum("grade", child_a.student["grade"]):
                c10_problems.append(f"{contact_ref} grade differs from student A")
        survivors_on_collapsed = [
            entry
            for entry in self.golden.survivors
            if entry.type in {"C6", "C14"} and set(entry.entity_refs) & collapsed_refs
        ]
        self.check(
            "sc_c10_preconditions",
            "G21",
            not c10_problems and not survivors_on_collapsed,
            "both key classes resolve to two distinct students, student B keeps its own "
            "contact, and no C6/C14 survives PRECEDENCE for a collapsed-contact person"
            if not (c10_problems or survivors_on_collapsed)
            else f"{len(c10_problems)} precondition failures, "
            f"{len(survivors_on_collapsed)} unbudgeted C6/C14 survivors",
        )

        c8_problems: list[str] = []
        for plant in dataset.plants:
            if plant.conflict_type != "C8":
                continue
            child = self.child_by_student_ref[make_ref("appdb", "student", plant.student_id)]
            source = str(plant.detail["dropped_source"])
            ordinal = grade_ord(child.student["grade"])
            if ordinal is None or ordinal < _MASK_FLOOR:
                c8_problems.append(f"{plant.student_id} below the grade floor")
            if norm_enum("status", child.student["status"]) == "withdrawn":
                c8_problems.append(f"{plant.student_id} is withdrawn")
            for sibling in child.household.children:
                if sibling is child:
                    continue
                person = self.world.person_of_student(
                    make_ref("appdb", "student", sibling.student_id)
                )
                present = bool(person.contact_refs if source == "crm" else person.payment_refs)
                if not present:
                    c8_problems.append(f"sibling {sibling.student_id} also absent from {source}")
        self.check(
            "sc_c8_preconditions",
            "G22",
            not c8_problems,
            "every dropped child is mask-eligible and every sibling is present in the "
            "source it was dropped from"
            if not c8_problems
            else f"{len(c8_problems)} violations, first: {c8_problems[0]}",
        )

        # G23 -- in-source email uniqueness.
        by_email: dict[str, list[Mapping[str, Any]]] = {}
        for contact in snapshot_records(dataset, "crm", "contact", 3):
            email = norm_email(contact.get("email"))
            if email is None:
                continue
            by_email.setdefault(email, []).append(contact)
        c3_pairs = {
            tuple(sorted(plant.contact_ids))
            for plant in dataset.plants
            if plant.conflict_type == "C3"
        }
        email_problems: list[str] = []
        for email, group in sorted(by_email.items()):
            if len(group) < 2:
                continue
            for i, left in enumerate(group):
                for right in group[i + 1 :]:
                    same_name = norm_name(left["first_name"]) == norm_name(
                        right["first_name"]
                    ) and norm_name(left["last_name"]) == norm_name(right["last_name"])
                    pair = tuple(sorted((str(left["crm_id"]), str(right["crm_id"]))))
                    if same_name and pair not in c3_pairs:
                        email_problems.append(f"{email}: {pair} share a name but are not a C3 pair")
        c3_field_problems: list[str] = []
        for child in dataset.children:
            if "c3" not in child.roles or child.duplicate_contact is None:
                continue
            original, duplicate = child.contact, child.duplicate_contact
            for field_name in ("grade", "lifecycle_stage"):
                if norm_enum(
                    "grade" if field_name == "grade" else "lifecycle_stage", original[field_name]
                ) != norm_enum(
                    "grade" if field_name == "grade" else "lifecycle_stage", duplicate[field_name]
                ):
                    c3_field_problems.append(f"{duplicate['crm_id']} {field_name} differs")
            if (
                original.get("external_id") != child.student_id
                or duplicate.get("external_id") != child.student_id
            ):
                c3_field_problems.append(f"{duplicate['crm_id']} external_id not the student id")
        self.check(
            "sc_insource_email_unique",
            "G23",
            not email_problems and not c3_field_problems,
            "no two gen-3 contacts share email_norm unless they are a golden C3 pair or a "
            "sibling set differing in (first, last); both C3 contacts L1-link to one student "
            "and carry identical COMPARED_FIELDS values"
            if not (email_problems or c3_field_problems)
            else f"{len(email_problems)} email collisions, "
            f"{len(c3_field_problems)} C3 field drifts",
        )

    # == G24..G30 =====================================================================
    def _g24_g30(self) -> None:
        dataset = self.dataset
        index = (
            set(self.world.contacts)
            | set(self.world.deals)
            | set(self.world.students)
            | set(self.world.enrollments)
            | set(self.world.payments)
        )
        dangling = sorted(
            {ref for row in self.golden.conflicts for ref in row["entity_refs"] if ref not in index}
        )
        self.check(
            "sc_refs_resolve_gen3",
            "G24",
            not dangling,
            "every entity_ref in golden/conflicts.json resolves against the gen-3 fixture index"
            if not dangling
            else f"{len(dangling)} dangling refs, first: {dangling[0]}",
        )

        seen: dict[str, tuple[str, str]] = {}
        key_problems: list[str] = []
        for generation in (1, 2, 3):
            for person in self.resolutions[generation].persons:
                seed = person.student_ref or person.anchor_ref
                anchor_class = person.anchor_ref.rsplit(":", 1)[0]
                previous = seen.get(seed)
                if previous is None:
                    seen[seed] = (person.person_key, anchor_class)
                elif previous != (person.person_key, anchor_class):
                    current = (person.person_key, anchor_class)
                    key_problems.append(f"{seed}: {previous} -> {current}")
        self.check(
            "sc_person_key_stable",
            "G25",
            not key_problems,
            "person_key and anchor source class are stable across generations 1-3"
            if not key_problems
            else f"{len(key_problems)} re-anchored persons, first: {key_problems[0]}",
        )

        refund_students = {
            make_ref("appdb", "student", child.student_id)
            for child in dataset.children
            if any(p["status"] == "refunded" for p in child.payments)
        }
        skewed: dict[str, int] = {}
        totals: dict[str, int] = {}
        dirty_refund_enrollments = 0
        for label, records in (
            ("crm.contact", dataset.contacts),
            ("crm.deal", dataset.deals),
            ("appdb.student", dataset.students),
            ("payments.payment", dataset.payments),
            ("appdb.enrollment", dataset.enrollments),
        ):
            count = 0
            for record in records:
                if str(record.get("updated_at")) < str(record.get("created_at")):
                    count += 1
                    if label == "appdb.enrollment" and (
                        make_ref("appdb", "student", record["student_id"]) in refund_students
                    ):
                        dirty_refund_enrollments += 1
            skewed[label] = count
            totals[label] = len(records)
        self.fact("out_of_order_timestamps", dict(sorted(skewed.items())))
        self.fact(
            "out_of_order_timestamp_rates",
            {label: round(skewed[label] / totals[label], 5) for label in sorted(skewed)},
        )
        timestamp_problems = timestamp_dirt_problems(skewed, totals)
        if dirty_refund_enrollments:
            timestamp_problems.append(
                f"{dirty_refund_enrollments} refund enrollments carry timestamp dirt"
            )
        self.check(
            "sc_timestamp_dirt_spread",
            "G26",
            not timestamp_problems,
            f"updated_at < created_at on {_pct(TIMESTAMP_DIRT_BAND[0])}-"
            f"{_pct(TIMESTAMP_DIRT_BAND[1])} of each entity type (A.3 asks ~0.5%), and on "
            "no enrollment whose student holds a refunded payment (C13 clause (c) stays "
            "dirt-free)"
            if not timestamp_problems
            else "; ".join(timestamp_problems),
        )

        malformed_problems: list[str] = []
        oversized = [case for case in self.malformed if case["kind"] == "oversized_body"]
        duplicates = [case for case in self.malformed if case["kind"] == "duplicate_primary_key"]
        if len(self.malformed) < 20:
            malformed_problems.append(f"only {len(self.malformed)} cases")
        if not oversized or len(str(oversized[0]["raw"]).encode("utf-8")) != MAX_PAYLOAD_BYTES + 1:
            malformed_problems.append("oversized case is not exactly MAX_PAYLOAD_BYTES + 1 bytes")
        if any(case["entity_type"] != "contact" for case in duplicates):
            malformed_problems.append("duplicate PK exercised outside crm.contact")
        collisions = malformed_pk_collisions(fixture_primary_keys(dataset), self.malformed)
        if collisions:
            malformed_problems.append(
                f"{len(collisions)} malformed cases reuse a fixture PK, first: {collisions[0]}"
            )
        self.check(
            "sc_malformed_isolation",
            "G27",
            not malformed_problems,
            f"{len(self.malformed)} structural-only cases; duplicate PK on crm.contact only; "
            f"oversized case is exactly {MAX_PAYLOAD_BYTES + 1} bytes; none in any SS5 total"
            if not malformed_problems
            else "; ".join(malformed_problems),
        )

        sampled = {ref for row in self.golden.clean_sample for ref in row["identity_refs"]}
        conflicted = {ref for row in self.golden.conflicts for ref in row["entity_refs"]}
        overlap = sorted(sampled & conflicted)
        self.check(
            "sc_clean_sample_disjoint",
            "G28",
            not overlap,
            f"none of the {len(self.golden.clean_sample)} sampled entities' identity refs "
            "appears in any golden conflict"
            if not overlap
            else f"{len(overlap)} overlapping refs, first: {overlap[0]}",
        )

        pipeline_problems: list[str] = []
        for household in dataset.households:
            if household.deal_id is None:
                continue
            deal = next((d for d in dataset.deals if str(d["deal_id"]) == household.deal_id), None)
            if deal is None:  # pragma: no cover
                continue
            anchor = min(
                household.children,
                key=lambda c: make_ref("appdb", "student", c.student_id),
            )
            expected = (
                None
                if anchor.enrollment is None
                else norm_enum("program", anchor.enrollment["program"])
            )
            if norm_enum("pipeline", deal["pipeline"]) != (expected or household.program):
                pipeline_problems.append(str(deal["deal_id"]))
        self.check(
            "sc_pipeline_consistency",
            "G29",
            not pipeline_problems,
            "deal.pipeline equals the program of the household's anchor enrollment on every deal"
            if not pipeline_problems
            else f"{len(pipeline_problems)} mismatches",
        )

        self._determinism()

    # == G30 ==========================================================================
    def _determinism(self) -> None:
        """`G30` -- evaluated, not asserted.

        This row used to be `self.check("sc_determinism", "G30", True, ...)`: a literal
        that could not fail, printed as PASS and written into the graded
        `golden/manifest-summary.json`. It now evaluates all three clauses SS8/`G30`
        actually names, at run time, over the bytes this run emitted:

        (a) `PYTHONHASHSEED == "0"` -- the clause SS8 states and nothing implemented.
            `recon/seed/__main__.py` sets it (re-`exec`ing once if needed); this is the
            assertion half.
        (b) every file in the tree just written re-hashes to the `sha256` that
            `fixtures/manifest.json` is about to claim for it, read back off disk.
        (c) **hash- and iteration-order independence of the whole of pass 2**: the
            entire gen-3 snapshot is re-shuffled, `recon.er.resolve`, `run_sweep` and
            `build_golden` are re-run over it, and the three golden documents must come
            back byte-identical under the pinned encoder. An unsorted `set`/`dict`
            iteration reaching an output or a selection decision moves a byte here.

        Byte-identity across two *subprocesses* remains `tests/seed/test_determinism.py`'s
        job -- a single process cannot observe another process's hash seed -- but that is
        now the only clause delegated, and (c) is a strictly stronger in-run probe of the
        property the delegation was standing in for.
        """
        problems: list[str] = []
        hash_seed = os.environ.get("PYTHONHASHSEED", "<unset>")
        self.fact("PYTHONHASHSEED", hash_seed)
        if hash_seed != "0":
            problems.append(f"PYTHONHASHSEED is {hash_seed!r}, not '0' (SS8)")

        if self.fixtures_dir is not None and self.file_manifest is not None:
            for relative, entry in sorted(self.file_manifest.items()):
                path = self.fixtures_dir / relative
                if not path.is_file():
                    problems.append(f"{relative}: emitted file is missing from the tree")
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest != entry["sha256"]:
                    problems.append(f"{relative}: bytes on disk != manifest sha256")

        probe = Rng(self.dataset.seed).fork("determinism-probe")
        base = self.snapshots[3]
        shuffled = Snapshot(
            generation=base.generation,
            contacts=probe.shuffled(base.contacts),
            deals=probe.shuffled(base.deals),
            students=probe.shuffled(base.students),
            enrollments=probe.shuffled(base.enrollments),
            payments=probe.shuffled(base.payments),
        )
        replay_resolution = resolve(shuffled)
        replay = build_golden(
            self.dataset, run_sweep(shuffled, replay_resolution), replay_resolution
        )
        for name, left, right in (
            ("golden/conflicts.json", self.golden.conflicts, replay.conflicts),
            ("golden/clean-sample.json", self.golden.clean_sample, replay.clean_sample),
            ("golden/expected-views.json", self.golden.expected_views, replay.expected_views),
        ):
            if dumps(left) != dumps(right):
                problems.append(f"{name} moved when the gen-3 input order was shuffled")

        self.check(
            "sc_determinism",
            "G30",
            not problems,
            "PYTHONHASHSEED=0 asserted; every emitted file re-hashes to its manifest "
            "sha256; the golden tree is byte-identical when pass 2 is replayed over a "
            "shuffled gen-3 snapshot"
            if not problems
            else "; ".join(problems),
        )

    # == SS9 profiles =================================================================
    def _structural_minimums(self) -> None:
        """SS9 -- the structural minimums the dev profile may NOT scale.

        SS9 pins two of them as identical in both profiles (malformed cases, the
        oscillation set) and says the same of multi-child households and deal-less
        leads. The latter two are **not constructible** at dev volumes -- A.4's floors
        (>=1,000 households of 2-4 children, >=3,000 leads) exceed the dev profile's
        entire 1,250-student budget -- so they scale with volume and the divergence is
        recorded in SS12 D-13. This check binds whichever rule applies rather than
        leaving the clause narrated: the two unscaled constants are asserted equal to
        their pinned values in **both** profiles, and the two scaled populations are
        asserted against A.4's floors on `full`.
        """
        plan = self.plan
        multi_child = sum(1 for household in self.dataset.households if household.size > 1)
        leads = plan.leads
        self.fact(
            "structural_minimums",
            {
                "malformed_cases": len(self.malformed),
                "oscillating_fields": len(self.dataset.oscillations),
                "multi_child_households": multi_child,
                "deal_less_leads": leads,
            },
        )

        problems: list[str] = []
        if plan.malformed_cases != 24 or len(self.malformed) < 20:
            problems.append(
                f"malformed cases: planned {plan.malformed_cases}, emitted "
                f"{len(self.malformed)} (SS9 pins this identical in both profiles, A.4 >= 20)"
            )
        if plan.oscillating_fields != 25 or len(self.dataset.oscillations) < 25:
            problems.append(
                f"oscillating fields: planned {plan.oscillating_fields}, emitted "
                f"{len(self.dataset.oscillations)} (SS9 pins this identical in both profiles)"
            )
        if plan.profile == "full":
            if multi_child < 1000:
                problems.append(f"multi-child households {multi_child} < A.4's floor of 1,000")
            if leads < 3000:
                problems.append(f"deal-less leads {leads} < A.4's floor of 3,000")
        else:
            if multi_child < 2:
                problems.append(f"multi-child households {multi_child} < 2 (structure inert)")
            if leads < 1:
                problems.append(f"deal-less leads {leads} < 1 (structure inert)")

        self.check(
            "sc_structural_minimums",
            "SS9",
            not problems,
            f"malformed={len(self.malformed)} oscillating={len(self.dataset.oscillations)} "
            f"(both profile-invariant); multi-child households={multi_child}, "
            f"deal-less leads={leads} (scaled, SS12 D-13)"
            if not problems
            else "; ".join(problems),
        )

        paths = sorted({str(row["field_path"]) for row in self.dataset.oscillations})
        self.fact("oscillating_field_paths", paths)
        self.check(
            "sc_oscillation_spread",
            "SS7",
            len(paths) >= 2,
            f"the A -> B -> A set spans {len(paths)} field paths: {', '.join(paths)}"
            if len(paths) >= 2
            else f"the oscillation set covers only {paths} -- R4/R16 is exercised on one "
            "field path, so an oscillation detector keyed on any other path is untested",
        )

    # == G31 ==========================================================================
    def _plantability(self) -> None:
        """`G31`(a)/(b) -- every planted conflict is addressable by the real cascade.

        An unplantable conflict fails the seed run with a diagnostic naming the conflict
        and the missing link. It is **never** written into `golden/`.
        """
        by_anchor: dict[tuple[str, str], list[Any]] = {}
        for entry in self.sweep.entries:
            if entry.anchor is None:  # pragma: no cover
                continue
            by_anchor.setdefault((entry.type, entry.anchor), []).append(entry)

        problems: list[str] = []
        checked = 0
        for plant in self.dataset.plants:
            checked += 1
            anchor = self._plant_anchor(plant)
            found = by_anchor.get((plant.conflict_type, anchor))
            if not found:
                problems.append(
                    f"{plant.conflict_type} anchored at {anchor} is UNPLANTABLE: the sweep "
                    "found no conflict there -- the link its rule presumes was never made"
                )
                continue
            for method in plant.expected_methods:
                if plant.conflict_type in {"C3", "C4", "C10"}:
                    actual = {
                        self.gen3.contact_method.get(make_ref("crm", "contact", crm_id))
                        for crm_id in plant.contact_ids
                    }
                    if method not in actual:
                        problems.append(
                            f"{plant.conflict_type} at {anchor}: expected link method "
                            f"{method}, cascade produced {sorted(m for m in actual if m)}"
                        )
        self.check(
            "sc_plantability",
            "G31ab",
            not problems,
            f"{checked} planted conflicts checked; every one is addressable by the links "
            "recon.er actually produced, with the expected method"
            if not problems
            else f"{len(problems)} UNPLANTABLE, first: {problems[0]}",
        )
        self.report.facts["plants_checked"] = checked
        self.report.facts["plants_unplantable"] = len(problems)

    def _plant_anchor(self, plant: Any) -> str:
        if plant.conflict_type in {"C1", "C4", "C5", "C6", "C8", "C14"}:
            return make_ref("appdb", "student", plant.student_id)
        if plant.conflict_type == "C2":
            return make_ref("payments", "payment", plant.payment_ids[0])
        if plant.conflict_type == "C3":
            return "|".join(
                sorted(make_ref("crm", "contact", crm_id) for crm_id in plant.contact_ids)
            )
        if plant.conflict_type in {"C7", "C9"}:
            return make_ref("appdb", "enrollment", plant.enrollment_id)
        if plant.conflict_type == "C10":
            return make_ref("crm", "contact", plant.contact_ids[0])
        if plant.conflict_type == "C11":
            return "|".join(
                sorted(make_ref("payments", "payment", pid) for pid in plant.payment_ids)
            )
        if plant.conflict_type in {"C12", "C13"}:
            return make_ref("payments", "payment", plant.payment_ids[0])
        raise ValueError(f"no anchor rule for {plant.conflict_type}")  # pragma: no cover

    # == G32..G39 =====================================================================
    def _g32_g39(self) -> None:
        plan = self.plan
        keys = [(row["type"], tuple(row["entity_refs"])) for row in self.golden.conflicts]
        self.check(
            "sc_golden_key_unique",
            "G32",
            len(keys) == len(set(keys)),
            f"(type, sorted(entity_refs)) is unique across {len(keys)} golden entries",
        )
        self.check(
            "sc_precedence_filtered",
            "G32",
            all(row["expected_verdict"] == "conflict" for row in self.golden.conflicts),
            "golden/conflicts.json is written through apply_precedence and every entry "
            "carries expected_verdict='conflict'",
        )

        fraction = plan.tri_source_student_fraction
        minimum_problems = [
            f"{key} {len(self.golden_by_type(key))} < {minimum}"
            for key, minimum in sorted(CONFLICT_MINIMUMS.items())
            if plan.profile == "full" and len(self.golden_by_type(key)) < minimum
        ]
        volume_problems = [
            f"{source}.{entity}"
            for (source, entity), expected in sorted(A1_VOLUMES.items())
            if plan.profile == "full"
            and len(snapshot_records(self.dataset, source, entity, 3)) != expected
        ]
        self.fact("tri_source_student_fraction", round(fraction, 4))
        self.fact(
            "fully_consistent_entity_fraction",
            round(self.golden.fully_consistent_fraction, 4),
        )
        self.fact("compound_ratio", round(self.golden.compound_ratio, 4))
        self.fact("golden_entries", len(self.golden.conflicts))
        self.check(
            "sc_volumes_and_ratios",
            "G33",
            not minimum_problems
            and not volume_problems
            and 0.68 <= fraction <= 0.72
            and self.golden.fully_consistent_fraction >= 0.85
            and self.golden.compound_ratio >= 0.10,
            f"tri_source_student_fraction={fraction:.4f} in [0.68, 0.72]; "
            f"fully_consistent={self.golden.fully_consistent_fraction:.4f} >= 0.85; "
            f"compound_ratio={self.golden.compound_ratio:.4f} >= 0.10; "
            f"all fourteen A.4 minimums met"
            if not (minimum_problems or volume_problems)
            else "; ".join([*minimum_problems, *volume_problems]),
        )

        fee_problems: list[str] = []
        c12_ids = {
            pid
            for plant in self.dataset.plants
            if plant.conflict_type == "C12"
            for pid in plant.payment_ids
        }
        for child in self.dataset.children:
            for payment in child.payments:
                if str(payment["payment_id"]) in c12_ids:
                    continue
                expected = FEE_SCHEDULE[(child.household.program, str(payment["type"]))]
                if int(payment["amount_cents"]) != expected:
                    fee_problems.append(str(payment["payment_id"]))
        c12_collisions = [
            pid
            for pid in sorted(c12_ids)
            for payment in [next(p for p in self.dataset.payments if str(p["payment_id"]) == pid)]
            if int(payment["amount_cents"]) in set(FEE_SCHEDULE.values())
        ]
        self.check(
            "sc_fee_schedule_exact",
            "G34",
            not fee_problems and not c12_collisions,
            "every payment carries the exact fee-schedule amount for its (program, type) "
            "except the planted C12, whose amounts coincide with no other schedule cell"
            if not (fee_problems or c12_collisions)
            else f"{len(fee_problems)} off-schedule, {len(c12_collisions)} C12 collisions",
        )

        c7_problems: list[str] = []
        for household in self.dataset.households:
            if household.role != "c7":
                continue
            child = household.children[0]
            types = {(p["type"], p["status"]) for p in child.payments}
            if ("fee", "paid") not in types:
                c7_problems.append(f"{child.student_id} has no paid fee")
            if any(t in {"deposit", "tuition"} and s == "paid" for t, s in types):
                c7_problems.append(f"{child.student_id} holds a paid deposit/tuition")
            if household.deal_id is None:
                c7_problems.append(f"{child.student_id} has no D2 deal")
        self.check(
            "sc_c7_preconditions",
            "G35",
            not c7_problems,
            "every C7 plant holds a paid fee, no paid deposit/tuition, and a D2-linked deal"
            if not c7_problems
            else f"{len(c7_problems)} violations, first: {c7_problems[0]}",
        )

        c1_problems: list[str] = []
        for household in self.dataset.households:
            if household.role != "c1":
                continue
            child = household.children[0]
            if household.size != 1:
                c1_problems.append(f"{child.student_id} not single-child")
            if household.deal_id is not None:
                c1_problems.append(f"{child.student_id} has a deal")
            if child.enrollment is None or child.enrollment.get("crm_deal_id") is not None:
                c1_problems.append(f"{child.student_id} crm_deal_id is not NULL")
            deposits = [
                p
                for p in child.payments
                if p["type"] == "deposit"
                and p["status"] == "paid"
                and int(p["amount_cents"]) == FEE_SCHEDULE[(household.program, "deposit")]
            ]
            if not deposits:
                c1_problems.append(f"{child.student_id} has no paid deposit at schedule amount")
            if (
                child.enrollment is not None
                and norm_enum("stage", child.enrollment["stage"]) != "deposit_paid"
            ):
                c1_problems.append(f"{child.student_id} enrollment not at deposit_paid")
        self.check(
            "sc_c1_preconditions",
            "G36",
            not c1_problems,
            "every C1 plant: single-child household, paid deposit at the schedule amount, one "
            "enrollment at deposit_paid, crm_deal_id NULL, zero D2 deals"
            if not c1_problems
            else f"{len(c1_problems)} violations, first: {c1_problems[0]}",
        )

        shapes = {"c6_grade": 0, "c6_lifecycle": 0, "c6_mixed": 0}
        c14_shapes = {"c14_name": 0, "c14_dob": 0, "c14_stage": 0}
        for child in self.dataset.children:
            for role in shapes:
                if role in child.roles:
                    shapes[role] += 1
            for role in c14_shapes:
                if role in child.roles:
                    c14_shapes[role] += 1
        self.fact("c6_composition", shapes)
        self.fact("c14_composition", c14_shapes)
        mixed_ok = all(
            any(
                path in entry.disagreeing_fields
                for path in ("crm.contact.grade", "crm.contact.lifecycle_stage")
            )
            and any(
                path in entry.disagreeing_fields
                for path in (
                    "crm.contact.first_name",
                    "crm.contact.last_name",
                    "crm.contact.dob",
                )
            )
            for entry in self._mixed_entries()
        )
        self.check(
            "sc_c6_c14_composition",
            "G37",
            shapes["c6_grade"] == plan.c6_grade
            and shapes["c6_lifecycle"] == plan.c6_lifecycle
            and shapes["c6_mixed"] == plan.c6_mixed
            and c14_shapes["c14_name"] == plan.c14_name
            and c14_shapes["c14_dob"] == plan.c14_dob
            and c14_shapes["c14_stage"] == plan.c14_stage
            and mixed_ok,
            f"C6 = {shapes['c6_grade']} grade + {shapes['c6_lifecycle']} lifecycle + "
            f"{shapes['c6_mixed']} mixed (each mixed plant combines a name/DOB path with a "
            f"grade or lifecycle path); C14 = {c14_shapes['c14_name']} name + "
            f"{c14_shapes['c14_dob']} dob + {c14_shapes['c14_stage']} stage",
        )

        paid_stage_problems: list[str] = []
        budgeted = 0
        for enrollment_ref, enrollment in sorted(self.world.enrollments.items()):
            if norm_enum("stage", enrollment.get("stage")) not in PAID_IMPLYING_STAGES:
                continue
            student_ref = make_ref("appdb", "student", enrollment["student_id"])
            person = self.world.person_of_student(student_ref)
            backed = any(
                self.gen3.payment_enrollment.get(ref) == enrollment_ref
                and self.world.payments[ref].get("status") == "paid"
                and self.world.payments[ref].get("type") in {"deposit", "tuition"}
                for ref in (person.payment_refs if person else ())
            )
            if backed:
                continue
            child = self.child_by_student_ref.get(student_ref)
            role = child.household.role if child else "?"
            if role in {"c7", "c13", "c5"} or (child and "c8_dropped_payments" in child.roles):
                budgeted += 1
            else:
                paid_stage_problems.append(f"{enrollment_ref} (household role {role})")
        expected_budget = (
            plan.conflicts["C7"] + plan.conflicts["C13"] + plan.conflicts["C5"] + plan.c8_payments
        )
        self.check(
            "sc_paid_stage_has_payment",
            "G38",
            not paid_stage_problems and budgeted == expected_budget,
            f"every paid-implying enrollment is backed by a paid deposit/tuition except the "
            f"{budgeted} budgeted plants (== {expected_budget})"
            if not paid_stage_problems
            else f"{len(paid_stage_problems)} unbudgeted, first: {paid_stage_problems[0]}",
        )

        half_cent = [
            str(deal["deal_id"])
            for deal in self.dataset.deals
            if abs(float(deal["amount"]) * 100 - int(float(deal["amount"]) * 100) - 0.5) < 1e-9
        ]
        self.check(
            "sc_amount_no_half_cent",
            "G39",
            not half_cent,
            "no crm.deal.amount sits on a half-cent boundary, so Money's half-to-even "
            "tie-break can never decide a graded byte"
            if not half_cent
            else f"{len(half_cent)} amounts on the boundary",
        )

    def golden_by_type(self, conflict_type: str) -> list[Mapping[str, Any]]:
        return [row for row in self.golden.conflicts if row["type"] == conflict_type]

    def _mixed_entries(self) -> list[Any]:
        mixed_students = {
            make_ref("appdb", "student", child.student_id)
            for child in self.dataset.children
            if "c6_mixed" in child.roles
        }
        return [
            entry
            for entry in self.golden.survivors
            if entry.type == "C6" and entry.anchor in mixed_students
        ]


def run_self_check(
    dataset: Dataset,
    snapshots: Mapping[int, Snapshot],
    resolutions: Mapping[int, Resolution],
    sweep: SweepResult,
    golden: GoldenSet,
    malformed: Sequence[Mapping[str, Any]],
    *,
    fixtures_dir: Path | None = None,
    file_manifest: Mapping[str, Mapping[str, Any]] | None = None,
) -> SelfCheckReport:
    """Run every named check. The caller fails the run if `report.passed` is False.

    `fixtures_dir` / `file_manifest` are the tree this run just wrote; `sc_determinism`
    re-reads those bytes and re-hashes them (`G30` clause (b)).
    """
    return _Checker(
        dataset,
        snapshots,
        resolutions,
        sweep,
        golden,
        malformed,
        fixtures_dir=fixtures_dir,
        file_manifest=file_manifest,
    ).run()
