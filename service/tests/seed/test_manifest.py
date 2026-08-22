"""`manifest` marker -- every A.1 volume, A.4 minimum and A.5 ratio, at BOTH profiles.

These are the assertions the brief grades the generator on: "a generator that plants
fewer conflicts than mandated, or doesn't export the golden set, fails the Correctness
criterion regardless of code quality". They read the *emitted tree*, not the
generator's in-memory state, so a bug between the self-check and the writer cannot
hide behind a green self-check.
"""

from __future__ import annotations

import hashlib

import pytest

from recon.reference import A1_VOLUMES, CONFLICT_MINIMUMS, CONFLICT_TYPES
from recon.seed.plan import build_plan

from .conftest import SeedTree

pytestmark = pytest.mark.manifest

DEV_CONFLICT_FLOOR = 5


def test_a1_volumes_are_exact_on_generation_3(any_tree: SeedTree) -> None:
    plan = build_plan(any_tree.profile)
    gen3 = any_tree.summary["record_counts_gen3"]
    for (source, entity), _volume in sorted(A1_VOLUMES.items()):
        assert gen3[f"{source}.{entity}"] == plan.volumes[(source, entity)], (
            f"{source}.{entity} generation-3 count must equal the profile's A.1 volume"
        )


def test_full_profile_hits_the_literal_appendix_a_volumes(full_tree: SeedTree) -> None:
    gen3 = full_tree.summary["record_counts_gen3"]
    assert gen3 == {
        "crm.contact": 40000,
        "crm.deal": 15000,
        "appdb.student": 25000,
        "appdb.enrollment": 22000,
        "payments.payment": 18000,
    }
    assert sum(gen3.values()) == 120000, "A.1 requires >= 100,000 records in total"


def test_generations_one_and_two_carry_the_deleted_records(any_tree: SeedTree) -> None:
    plan = build_plan(any_tree.profile)
    gen1 = any_tree.summary["record_counts_gen1"]
    gen3 = any_tree.summary["record_counts_gen3"]
    assert gen1["crm.contact"] == gen3["crm.contact"] + plan.c8_crm
    assert gen1["crm.deal"] == gen3["crm.deal"] + plan.c9_missing
    assert gen1["payments.payment"] == gen3["payments.payment"] + plan.c8_payments
    assert gen1["appdb.student"] == gen3["appdb.student"]
    assert gen1["appdb.enrollment"] == gen3["appdb.enrollment"]


def test_every_conflict_class_meets_its_minimum(any_tree: SeedTree) -> None:
    counts = any_tree.summary["conflict_counts"]
    for conflict_type in CONFLICT_TYPES:
        minimum = (
            CONFLICT_MINIMUMS[conflict_type]
            if any_tree.profile == "full"
            else min(CONFLICT_MINIMUMS[conflict_type], DEV_CONFLICT_FLOOR)
        )
        assert counts[conflict_type] >= minimum, (
            f"{conflict_type} planted {counts[conflict_type]}, below the {minimum} floor"
        )


def test_full_profile_meets_the_literal_a4_minimums(full_tree: SeedTree) -> None:
    counts = full_tree.summary["conflict_counts"]
    assert counts == dict(sorted(CONFLICT_MINIMUMS.items()))
    assert sum(counts.values()) == 3050


def test_a5_compound_ratio_and_consistency_gates(any_tree: SeedTree) -> None:
    summary = any_tree.summary
    assert summary["compound_ratio"] >= 0.10, "A.5: >= 10% of conflicts must compound"
    assert summary["fully_consistent_entity_fraction"] >= 0.85, "A.1: >= 85% fully consistent"
    assert 0.68 <= summary["tri_source_student_fraction"] <= 0.72, "A.1: ~70% tri-source"


def test_structural_minimums_that_do_not_scale(any_tree: SeedTree) -> None:
    assert any_tree.summary["malformed_cases"] >= 20, "A.4: >= 20 malformed payloads"
    assert any_tree.summary["oscillating_fields"] >= 25, "A.4: >= 25 re-asserting fields"
    assert len(any_tree.clean_sample) == 1000, "A.6: 1,000 sampled clean entities"
    assert len(any_tree.expected_views) >= 25, "SS8: >= 25 hand-checkable entity views"


def test_full_profile_structural_floors(full_tree: SeedTree) -> None:
    assert full_tree.summary["multi_child_households"] >= 1000
    assert full_tree.summary["deal_less_leads"] >= 3000


def test_every_named_self_check_passed(any_tree: SeedTree) -> None:
    failures = [name for name, ok in any_tree.summary["self_check"].items() if not ok]
    assert not failures, f"manifest self-check reported failures: {failures}"


def test_golden_conflicts_have_the_pinned_shape(any_tree: SeedTree) -> None:
    expected_keys = {
        "type",
        "rule_id",
        "entity_refs",
        "sources_involved",
        "disagreeing_fields",
        "observed_values",
        "expected_verdict",
        "compound_with",
        "oscillating",
    }
    keys = {(row["type"], tuple(row["entity_refs"])) for row in any_tree.conflicts}
    assert len(keys) == len(any_tree.conflicts), "(type, entity_refs) must be unique (SS5.7 r11)"
    for row in any_tree.conflicts:
        assert set(row) == expected_keys
        assert row["expected_verdict"] == "conflict"
        assert row["entity_refs"] == sorted(row["entity_refs"])
        assert row["sources_involved"] and set(row["sources_involved"]) <= {
            "crm",
            "appdb",
            "payments",
        }
        if row["type"] not in {"C6", "C14"}:
            assert row["disagreeing_fields"] == []


def test_clean_sample_is_disjoint_from_every_conflict(any_tree: SeedTree) -> None:
    conflicted = {ref for row in any_tree.conflicts for ref in row["entity_refs"]}
    sampled = {ref for row in any_tree.clean_sample for ref in row["identity_refs"]}
    assert not (conflicted & sampled), "G28: a sampled entity may never appear in a conflict"


def test_manifest_records_a_sha256_per_emitted_file(any_tree: SeedTree) -> None:
    files = any_tree.manifest["files"]
    assert files, "fixtures/manifest.json must record every emitted file"
    for relative, entry in sorted(files.items()):
        path = any_tree.root / "fixtures" / relative
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert entry["sha256"] == digest, f"{relative} sha256 does not match the file on disk"
        assert entry["bytes"] == path.stat().st_size


def test_expected_counts_match_the_emitted_generations(any_tree: SeedTree) -> None:
    for generation in (1, 2, 3):
        ledger = any_tree.manifest["expected_counts"][f"gen{generation}"]
        for key, expected in sorted(ledger.items()):
            source, entity = key.split(".")
            records = any_tree.read_jsonl(f"{source}/gen{generation}/{entity}.jsonl")
            assert len(records) == expected, f"gen{generation} {key} ledger count is wrong"


def test_malformed_cases_are_structural_and_isolated(any_tree: SeedTree) -> None:
    cases = any_tree.read_jsonl("malformed/cases.jsonl")
    assert len(cases) >= 20
    assert all(isinstance(case["raw"], str) for case in cases), "raw is the LITERAL payload string"
    assert {case["case_id"] for case in cases} == {case["case_id"] for case in cases}
    duplicates = [case for case in cases if case["kind"] == "duplicate_primary_key"]
    assert duplicates and all(case["entity_type"] == "contact" for case in duplicates), (
        "SS12 D-3: duplicate PK is exercised on a CRM contact only, never on a payment"
    )
    assert all(400 <= case["expect_code"] < 500 for case in cases)
