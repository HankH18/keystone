"""SS7 -- `fixtures/malformed/cases.jsonl`: >=20 structurally broken payloads.

`raw` is the **literal payload string**, not a nested object, which is the only way a
truncated body or a non-object line is representable at all. Every case must produce
the documented 4xx plus a structured log at the adapter boundary -- never a 500 and
never a silent skip.

**Malformedness is structural only** (`G27`). A well-formed record carrying an
unrecognised enum *value* is emphatically not malformed: it ingests normally,
`norm_enum` returns `None`, and every rule scoping it yields `verdict='unchecked'`
with `detail.reason='unmapped_enum'`. Putting one here instead would delete a real
`unchecked` path from the suite and add a rejection the adapter must not perform.

`duplicate PK` is exercised on a **CRM contact** and never on a payment, so it cannot
collide with `R-011` (SS12 D-3): the brief mandates both a 4xx rejection and a C11
conflict for a repeated `payment_id`, and those are mutually exclusive.

The file is **static** -- no PRNG draw reaches it -- so it is byte-identical across
profiles and across seeds, which is what SS9 requires of the structural minimums.
"""

from __future__ import annotations

from typing import Any

from recon.reference import MAX_PAYLOAD_BYTES

__all__ = ["EXPECT_CODES", "build_malformed_cases"]

#: The documented 4xx per breakage class. Committed here so the adapter (T-4) and the
#: fixture agree on one vocabulary rather than two.
EXPECT_CODES: dict[str, int] = {
    "unparseable_json": 400,
    "non_object_line": 400,
    "missing_required_field": 422,
    "wrong_scalar_type": 422,
    "null_primary_key": 422,
    "duplicate_primary_key": 409,
    "oversized_body": 413,
}

_CONTACT = (
    '{"crm_id":"CRM-9000001","email":"malformed@example.test","first_name":"Ada",'
    '"last_name":"Byron","lifecycle_stage":"lead","created_at":"2026-02-01T00:00:00Z",'
    '"updated_at":"2026-02-02T00:00:00Z","external_id":null,"dob":"2012-05-04",'
    '"grade":"4","state":"TX","marketing_consent":true}'
)


def _case(case_id: str, source: str, entity_type: str, kind: str, raw: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "source": source,
        "entity_type": entity_type,
        "kind": kind,
        "expect_code": EXPECT_CODES[kind],
        "raw": raw,
    }


def _oversized_payload() -> str:
    """Exactly `MAX_PAYLOAD_BYTES + 1` bytes (SS2.2), so the boundary is the thing tested."""
    prefix = '{"crm_id":"CRM-9000024","email":"oversized@example.test","note":"'
    suffix = '"}'
    padding = MAX_PAYLOAD_BYTES + 1 - len(prefix) - len(suffix)
    if padding < 1:  # pragma: no cover - MAX_PAYLOAD_BYTES is 256 KiB
        raise ValueError("MAX_PAYLOAD_BYTES is too small to build the oversized case")
    payload = f"{prefix}{'x' * padding}{suffix}"
    if len(payload.encode("utf-8")) != MAX_PAYLOAD_BYTES + 1:  # pragma: no cover
        raise ValueError("oversized case is not exactly MAX_PAYLOAD_BYTES + 1 bytes")
    return payload


def build_malformed_cases() -> list[dict[str, Any]]:
    """The committed structural-breakage corpus, in a fixed order."""
    cases: list[dict[str, Any]] = [
        _case(
            "MAL-001",
            "crm",
            "contact",
            "missing_required_field",
            '{"email":"no-pk@example.test","first_name":"Ada","last_name":"Byron",'
            '"lifecycle_stage":"lead","created_at":"2026-02-01T00:00:00Z",'
            '"updated_at":"2026-02-02T00:00:00Z"}',
        ),
        _case(
            "MAL-002",
            "crm",
            "deal",
            "missing_required_field",
            '{"name":"Byron Admissions 2026","pipeline":"Lower School","stage":"New Lead",'
            '"amount":500.0,"associated_contact_ids":["CRM-0000001"],'
            '"created_at":"2026-02-01T00:00:00Z","updated_at":"2026-02-02T00:00:00Z"}',
        ),
        _case(
            "MAL-003",
            "appdb",
            "student",
            "missing_required_field",
            '{"first_name":"Ada","last_name":"Byron","dob":"2012-05-04","grade":"4",'
            '"guardian_email":"g@example.test","status":"applied","enrollment_year":2026,'
            '"created_at":"2026-02-01T00:00:00Z","updated_at":"2026-02-02T00:00:00Z"}',
        ),
        _case(
            "MAL-004",
            "appdb",
            "enrollment",
            "missing_required_field",
            '{"id":"6d9f0d2c-0000-5000-8000-000000000004","program":"Lower School",'
            '"stage":"applied","deposit_paid_at":null,"crm_deal_id":null,'
            '"created_at":"2026-02-01T00:00:00Z","updated_at":"2026-02-02T00:00:00Z"}',
        ),
        _case(
            "MAL-005",
            "payments",
            "payment",
            "missing_required_field",
            '{"payer_email":"g@example.test","payer_name":"G Byron","amount_cents":50000,'
            '"currency":"usd","type":"deposit","status":"paid",'
            '"occurred_at":"2026-02-01T00:00:00Z"}',
        ),
        _case(
            "MAL-006",
            "crm",
            "contact",
            "wrong_scalar_type",
            _CONTACT.replace('"email":"malformed@example.test"', '"email":42').replace(
                "CRM-9000001", "CRM-9000006"
            ),
        ),
        _case(
            "MAL-007",
            "crm",
            "deal",
            "wrong_scalar_type",
            '{"deal_id":"DEAL-9000007","name":"Byron Admissions 2026","pipeline":"Lower School",'
            '"stage":"New Lead","amount":{"value":500.0,"currency":"usd"},'
            '"associated_contact_ids":["CRM-0000001"],"created_at":"2026-02-01T00:00:00Z",'
            '"updated_at":"2026-02-02T00:00:00Z"}',
        ),
        _case(
            "MAL-008",
            "crm",
            "deal",
            "wrong_scalar_type",
            '{"deal_id":"DEAL-9000008","name":"Byron Admissions 2026","pipeline":"Lower School",'
            '"stage":"New Lead","amount":500.0,"associated_contact_ids":"CRM-0000001",'
            '"created_at":"2026-02-01T00:00:00Z","updated_at":"2026-02-02T00:00:00Z"}',
        ),
        _case(
            "MAL-009",
            "appdb",
            "student",
            "wrong_scalar_type",
            '{"id":"6d9f0d2c-0000-5000-8000-000000000009","first_name":"Ada","last_name":"Byron",'
            '"dob":20120504,"grade":"4","guardian_email":"g@example.test","status":"applied",'
            '"enrollment_year":2026,"created_at":"2026-02-01T00:00:00Z",'
            '"updated_at":"2026-02-02T00:00:00Z"}',
        ),
        _case(
            "MAL-010",
            "payments",
            "payment",
            "wrong_scalar_type",
            '{"payment_id":"pi_9000010","payer_email":"g@example.test","payer_name":"G Byron",'
            '"amount_cents":"50000","currency":"usd","type":"deposit","status":"paid",'
            '"occurred_at":"2026-02-01T00:00:00Z"}',
        ),
        _case(
            "MAL-011",
            "payments",
            "payment",
            "wrong_scalar_type",
            '{"payment_id":"pi_9000011","payer_email":"g@example.test","payer_name":"G Byron",'
            '"amount_cents":50000,"currency":"usd","type":"deposit","status":"paid",'
            '"occurred_at":"2026-02-01T00:00:00Z","metadata":["Ada","Byron"]}',
        ),
        _case(
            "MAL-012",
            "crm",
            "contact",
            "unparseable_json",
            '{"crm_id":"CRM-9000012","email":"truncated@example.test","first_name":"Ada"',
        ),
        _case(
            "MAL-013",
            "appdb",
            "enrollment",
            "unparseable_json",
            '{"id":"6d9f0d2c-0000-5000-8000-000000000013","student_id":"6d9f0d2c-0000-5000-'
            '8000-000000000003","program":"Lower Scho',
        ),
        _case(
            "MAL-014",
            "payments",
            "payment",
            "unparseable_json",
            '{"payment_id":"pi_9000014","payer_email":"g@example.test","amount_cents":500',
        ),
        _case(
            "MAL-015",
            "crm",
            "contact",
            "unparseable_json",
            '{"crm_id":"CRM-9000015","email":"trailing@example.test",}',
        ),
        _case(
            "MAL-016",
            "crm",
            "contact",
            "null_primary_key",
            _CONTACT.replace('"crm_id":"CRM-9000001"', '"crm_id":null'),
        ),
        _case(
            "MAL-017",
            "appdb",
            "student",
            "null_primary_key",
            '{"id":null,"first_name":"Ada","last_name":"Byron","dob":"2012-05-04","grade":"4",'
            '"guardian_email":"g@example.test","status":"applied","enrollment_year":2026,'
            '"created_at":"2026-02-01T00:00:00Z","updated_at":"2026-02-02T00:00:00Z"}',
        ),
        _case(
            "MAL-018",
            "payments",
            "payment",
            "null_primary_key",
            '{"payment_id":null,"payer_email":"g@example.test","payer_name":"G Byron",'
            '"amount_cents":50000,"currency":"usd","type":"deposit","status":"paid",'
            '"occurred_at":"2026-02-01T00:00:00Z"}',
        ),
        _case(
            "MAL-019",
            "crm",
            "contact",
            "duplicate_primary_key",
            _CONTACT.replace("CRM-9000001", "CRM-9000019"),
        ),
        _case(
            "MAL-020",
            "crm",
            "contact",
            "duplicate_primary_key",
            _CONTACT.replace("CRM-9000001", "CRM-9000019").replace('"Ada"', '"Augusta"'),
        ),
        _case("MAL-021", "crm", "contact", "non_object_line", '["CRM-9000021","a@example.test"]'),
        _case("MAL-022", "appdb", "student", "non_object_line", '"6d9f0d2c-0000-5000-8000-22"'),
        _case("MAL-023", "payments", "payment", "non_object_line", "9000023"),
        _case("MAL-024", "crm", "contact", "oversized_body", _oversized_payload()),
    ]
    if len(cases) < 20:  # pragma: no cover - the corpus is committed at 24
        raise ValueError("SS7 requires >= 20 malformed cases")
    return cases
