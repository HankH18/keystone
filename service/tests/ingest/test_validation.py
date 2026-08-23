"""Every committed malformed payload is rejected with the code it was built for (R2).

The corpus is not written here. It is read from `fixtures/malformed/cases.jsonl`,
where each of the 24 cases carries the **literal payload string** and the
`expect_code` the generator committed. So these tests cannot be made to pass by
adjusting an expectation to whatever the validator happens to do: the expectation
is a committed artifact of a different module, and `recon.seed.malformed`'s own
`EXPECT_CODES` table is the single vocabulary both sides import.

Both halves of contract SS7 are asserted, because they are easy to conflate and
getting either wrong is a graded failure:

* **structural breakage is rejected** -- 4xx, never a 500, never a silent skip;
* **an unrecognised enum *value* is not breakage** -- it ingests, `norm_enum`
  returns `None`, and the field is recorded as `unchecked`. Rejecting it would
  delete a mandated `unchecked` path (SS5.8) and invent a rejection the contract
  forbids.

The last test makes the two *distinguishable* on the same field of the same
entity, which is the property that actually matters: it is not enough that each
behaves correctly in isolation, they have to be told apart.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from recon.adapters import (
    MAX_PAYLOAD_BYTES,
    AdapterError,
    CrmAdapter,
    RawRecord,
    canonical_json,
    validate_batch,
    validate_payload,
)
from recon.ingest import _contact_row, ingest_source
from recon.seed.malformed import EXPECT_CODES


def _grouped(cases: list[dict]) -> dict[tuple[str, str], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for case in cases:
        groups[(case["source"], case["entity_type"])].append(case)
    return groups


def test_the_corpus_covers_every_breakage_class_the_contract_names(
    malformed_cases: list[dict],
) -> None:
    kinds = {case["kind"] for case in malformed_cases}
    assert kinds == set(EXPECT_CODES), (
        "the committed corpus must exercise every kind in the shared EXPECT_CODES table"
    )


def test_every_committed_case_is_rejected_with_its_committed_code(
    malformed_cases: list[dict],
) -> None:
    """All 24 cases: right status, right kind, none accepted, nothing 5xx."""
    seen: set[str] = set()
    for (source, entity_type), cases in sorted(_grouped(malformed_cases).items()):
        results = validate_batch(source, entity_type, 3, [case["raw"] for case in cases])
        assert len(results) == len(cases), "every input line must produce exactly one outcome"
        for case, result in zip(cases, results, strict=True):
            seen.add(case["case_id"])
            assert isinstance(result, AdapterError), (
                f"{case['case_id']} ({case['kind']}) was ACCEPTED -- a silent skip is "
                "the failure R2 names explicitly"
            )
            assert result.status == case["expect_code"], (
                f"{case['case_id']}: expected {case['expect_code']}, got {result.status}"
            )
            assert result.kind == case["kind"], f"{case['case_id']} classified as {result.kind}"
            assert 400 <= result.status < 500, f"{case['case_id']} produced a non-4xx status"
            assert result.detail, f"{case['case_id']} has no detail to log"
    assert len(seen) == len(malformed_cases)


def test_every_rejection_renders_an_rfc7807_problem(malformed_cases: list[dict]) -> None:
    for (source, entity_type), cases in sorted(_grouped(malformed_cases).items()):
        for case, result in zip(
            cases,
            validate_batch(source, entity_type, 3, [c["raw"] for c in cases]),
            strict=True,
        ):
            assert isinstance(result, AdapterError)
            problem = result.problem()
            assert {"type", "title", "status", "detail"} <= set(problem)
            assert problem["status"] == case["expect_code"]
            assert problem["type"].endswith(case["kind"])
            assert problem["source"] == source


def test_both_members_of_a_duplicate_primary_key_are_rejected(
    malformed_cases: list[dict],
) -> None:
    """SS7 makes a repeated PK structural, and the corpus expects 409 on BOTH lines.

    Which of two contradictory rows "came first" is a fact about file order, not
    about the source, so admitting one of them would land an arbitrary winner in
    an append-only table.
    """
    duplicates = [c for c in malformed_cases if c["kind"] == "duplicate_primary_key"]
    assert len(duplicates) >= 2

    results = validate_batch("crm", "contact", 3, [c["raw"] for c in duplicates])
    assert all(isinstance(r, AdapterError) and r.status == 409 for r in results)

    # ... and duplicate-ness is contextual, not a property of either payload alone.
    alone = validate_batch("crm", "contact", 3, [duplicates[0]["raw"]])
    assert isinstance(alone[0], RawRecord), (
        "a single well-formed contact must be accepted; the 409 comes from the "
        "collision, not from the record"
    )


def test_the_whole_corpus_rejects_through_the_adapter_and_is_counted(
    malformed_cases: list[dict], tmp_path: Path
) -> None:
    """R2 says "arrives via the adapter path", so drive it there, not just at a helper."""
    generation = 970
    by_entity = _grouped(malformed_cases)
    for (source, entity_type), cases in by_entity.items():
        path = tmp_path / source / f"gen{generation}" / f"{entity_type}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{case['raw']}\n" for case in cases), encoding="utf-8")

    expected_by_code: dict[int, int] = defaultdict(int)
    for case in malformed_cases:
        expected_by_code[case["expect_code"]] += 1

    rejections: list[AdapterError] = []
    total_ok = 0
    for source in sorted({source for source, _ in by_entity}):
        adapter = CrmAdapter(
            tmp_path,
            source_id=source,
            entity_types=tuple(entity for (src, entity) in by_entity if src == source),
        )
        result = ingest_source(adapter, generation, run_id="malformed-adapter", persist=False)
        total_ok += result.records_ok
        # The rejections come off the run, not off a sink the test installed:
        # `ingest_source` owns the sink precisely so it can COUNT them.
        rejections.extend(result.rejections)
        assert result.records_rejected == len(result.rejections)
        assert result.status == "partial"
        assert all(not load.complete for load in result.loads), (
            "a load that rejected records is not complete (SS5.3)"
        )

    assert total_ok == 0, "no malformed payload may land"
    assert len(rejections) == len(malformed_cases)
    got_by_code: dict[int, int] = defaultdict(int)
    for rejection in rejections:
        got_by_code[rejection.status] += 1
    assert dict(got_by_code) == dict(expected_by_code)


# ----------------------------------------------------------------------------------
# an unknown enum VALUE is not malformed (SS7 `G27`, SS5.8)
# ----------------------------------------------------------------------------------

_VALID_CONTACT = {
    "crm_id": "CRM-9500001",
    "email": "enum.case@example.test",
    "first_name": "Ada",
    "last_name": "Byron",
    "lifecycle_stage": "lead",
    "created_at": "2026-02-01T00:00:00Z",
    "updated_at": "2026-02-02T00:00:00Z",
    "external_id": None,
    "dob": "2012-05-04",
    "grade": "4",
    "state": "TX",
    "marketing_consent": True,
}


def _contact(**overrides: object) -> str:
    return canonical_json({**_VALID_CONTACT, **overrides})


def test_an_unknown_enum_value_ingests_and_normalizes_to_none() -> None:
    record = validate_payload(
        "crm", "contact", 3, _contact(lifecycle_stage="galactic_overlord", grade="Grade Omega")
    )
    assert isinstance(record, RawRecord)

    row = dict(
        zip(
            (
                "crm_id",
                "email",
                "first_name",
                "last_name",
                "lifecycle_stage",
                "external_id",
                "dob",
                "grade",
                "state",
                "marketing_consent",
                "created_at",
                "updated_at",
                "email_norm",
                "first_norm",
                "last_norm",
                "dob_norm",
                "grade_norm",
                "grade_ord",
                "state_norm",
                "lifecycle_norm",
                "unchecked_fields",
            ),
            _contact_row(record),
            strict=True,
        )
    )
    assert row["lifecycle_stage"] == "galactic_overlord", "the raw value is retained verbatim"
    assert row["lifecycle_norm"] is None
    assert row["grade_norm"] is None and row["grade_ord"] is None
    assert row["unchecked_fields"] == {
        "crm.contact.grade": "unmapped_enum",
        "crm.contact.lifecycle_stage": "unmapped_enum",
    }


def test_structural_breakage_and_an_unknown_enum_value_are_distinguishable() -> None:
    """Same field, same entity: one rejects, the other ingests as `unchecked`."""
    unknown_value = validate_batch("crm", "contact", 3, [_contact(lifecycle_stage="not_a_stage")])
    wrong_type = validate_batch("crm", "contact", 3, [_contact(lifecycle_stage=42)])

    assert isinstance(unknown_value[0], RawRecord)
    assert isinstance(wrong_type[0], AdapterError)
    assert wrong_type[0].kind == "wrong_scalar_type"
    assert wrong_type[0].status == 422


# ----------------------------------------------------------------------------------
# the oversize boundary
# ----------------------------------------------------------------------------------


def _padded_contact(total_bytes: int) -> str:
    """A valid contact padded to exactly `total_bytes`."""
    body = json.loads(_contact())
    body["note"] = ""
    base = len(canonical_json(body).encode("utf-8"))
    body["note"] = "x" * (total_bytes - base)
    payload = canonical_json(body)
    assert len(payload.encode("utf-8")) == total_bytes
    return payload


def test_the_size_limit_is_a_boundary_not_a_vibe() -> None:
    at_limit = validate_payload("crm", "contact", 3, _padded_contact(MAX_PAYLOAD_BYTES))
    assert isinstance(at_limit, RawRecord)

    with pytest.raises(AdapterError) as excinfo:
        validate_payload("crm", "contact", 3, _padded_contact(MAX_PAYLOAD_BYTES + 1))
    assert excinfo.value.kind == "oversized_body"
    assert excinfo.value.status == 413


def test_an_oversized_body_is_rejected_before_it_is_parsed() -> None:
    """The limit exists to avoid parsing the payload, so it must come first."""
    oversized_and_broken = "x" * (MAX_PAYLOAD_BYTES + 1)
    with pytest.raises(AdapterError) as excinfo:
        validate_payload("crm", "contact", 3, oversized_and_broken)
    assert excinfo.value.kind == "oversized_body"
