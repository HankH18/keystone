"""Every field type in contract SS1, audited for "accepted here, refused there" (R2).

The reported defect was one instance of a class. `created_at` is typed `StrictStr`
by the SS1 model and `timestamptz` by the staging table, so ``created_at: "x"``
validated, reached the COPY, and came back as an HTTP 500 with a raw psycopg
`InvalidDatetimeFormat` behind it. Nothing about that is specific to timestamps:
**any value the validator accepts and the database refuses is a latent 500**, and
R2 forbids all of them.

So this module is a table over the whole surface, not a regression test for the
one report. Each row is a payload that the pipeline would previously have accepted
and then failed to store; each is asserted to produce a structured 4xx from the
real endpoint, with the same assertion applied to `validate_payload` directly so
the file path (`ingest_source`) is covered too and not only HTTP.

Three things keep the table honest:

* **the negative control** -- the same fields at their extremes *inside* the
  storable range are accepted and land. A validator that rejected everything would
  pass the hostile half of this file and fail here;
* **the structural check** -- `TIMESTAMP_FIELDS` is reconciled against the live
  `information_schema`, so a `timestamptz` column added later without a validator
  fails this test instead of waiting to be discovered as a 500;
* **the landing assertion** -- nothing rejected here reaches `raw_records`.

Numeric bounds are read from the destination column, never invented: `integer` for
`enrollment_year`, and for `amount_cents` the `bigint` range divided by 10,000,
because migration 0002 stores a generated `amount_cents * 10000` beside it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from recon.adapters import (
    INT32_MAX,
    MAX_AMOUNT_CENTS,
    TIMESTAMP_FIELDS,
    AdapterError,
    validate_payload,
)
from recon.app import create_app
from recon.ingest import STAGING_TABLES
from tests.ingest.conftest import TRIGGER_HEADERS

GENERATION = 971

BASE: dict[tuple[str, str], dict[str, Any]] = {
    ("crm", "contact"): {
        "crm_id": "CRM-9710001",
        "email": "guardian.9710001@example.test",
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
    },
    ("crm", "deal"): {
        "deal_id": "DEAL-9710001",
        "name": "Byron Admissions 2026",
        "pipeline": "Lower School",
        "stage": "New Lead",
        "amount": 5000.0,
        "associated_contact_ids": ["CRM-9710001"],
        "created_at": "2026-02-01T00:00:00Z",
        "updated_at": "2026-02-02T00:00:00Z",
    },
    ("appdb", "student"): {
        "id": "6d9f0d2c-0000-5000-8000-000000971001",
        "first_name": "Ada",
        "last_name": "Byron",
        "dob": "2012-05-04",
        "grade": "4",
        "guardian_email": "guardian.9710001@example.test",
        "status": "applied",
        "enrollment_year": 2026,
        "created_at": "2026-02-01T00:00:00Z",
        "updated_at": "2026-02-02T00:00:00Z",
    },
    ("appdb", "enrollment"): {
        "id": "6d9f0d2c-0000-5000-8000-000000971002",
        "student_id": "6d9f0d2c-0000-5000-8000-000000971001",
        "program": "Lower School",
        "stage": "applied",
        "created_at": "2026-02-01T00:00:00Z",
        "updated_at": "2026-02-02T00:00:00Z",
        "deposit_paid_at": None,
    },
    ("payments", "payment"): {
        "payment_id": "pi_9710001",
        "payer_email": "guardian.9710001@example.test",
        "payer_name": "G Byron",
        "amount_cents": 50000,
        "currency": "usd",
        "type": "deposit",
        "status": "paid",
        "occurred_at": "2026-02-01T00:00:00Z",
        "external_ref": None,
        "refunded_at": None,
        "metadata": {
            "student_first_name": "Ada",
            "student_last_name": "Byron",
            "program": "Lower School",
        },
    },
}

NUL = chr(0)
LONE_SURROGATE = chr(0xD800)


def _raw(source: str, entity_type: str, key: str, **over: Any) -> str:
    """One base payload with `over` applied and a unique primary key."""
    body = dict(BASE[(source, entity_type)])
    body.update(over)
    pk = {
        ("crm", "contact"): "crm_id",
        ("crm", "deal"): "deal_id",
        ("appdb", "student"): "id",
        ("appdb", "enrollment"): "id",
        ("payments", "payment"): "payment_id",
    }[(source, entity_type)]
    body[pk] = f"{body[pk]}-{key}"
    return json.dumps(body)


#: `(case_id, source, entity_type, raw payload, expected status, expected kind)`.
#: Every row 500'd before this ticket; the probe that produced them is recorded in
#: the ticket. Grouped by the defect class rather than by entity, because the class
#: is the thing being fixed.
UNSTORABLE: list[tuple[str, str, str, str, int, str]] = [
    # --- unparseable timestamps: the reported blocker, on every timestamp field --
    (
        "ts-contact-created",
        "crm",
        "contact",
        _raw("crm", "contact", "ts1", created_at="x"),
        422,
        "wrong_scalar_type",
    ),
    (
        "ts-contact-impossible",
        "crm",
        "contact",
        _raw("crm", "contact", "ts2", created_at="2026-13-45T99:99:99Z"),
        422,
        "wrong_scalar_type",
    ),
    (
        "ts-contact-empty",
        "crm",
        "contact",
        _raw("crm", "contact", "ts3", updated_at=""),
        422,
        "wrong_scalar_type",
    ),
    (
        "ts-deal-created",
        "crm",
        "deal",
        _raw("crm", "deal", "ts4", created_at="x"),
        422,
        "wrong_scalar_type",
    ),
    (
        "ts-student-updated",
        "appdb",
        "student",
        _raw("appdb", "student", "ts5", updated_at="last tuesday"),
        422,
        "wrong_scalar_type",
    ),
    (
        "ts-enrollment-deposit",
        "appdb",
        "enrollment",
        _raw("appdb", "enrollment", "ts6", deposit_paid_at="tomorrow"),
        422,
        "wrong_scalar_type",
    ),
    (
        "ts-payment-occurred",
        "payments",
        "payment",
        _raw("payments", "payment", "ts7", occurred_at="x"),
        422,
        "wrong_scalar_type",
    ),
    (
        "ts-payment-refunded",
        "payments",
        "payment",
        _raw("payments", "payment", "ts8", refunded_at="2026-02-30T00:00:00Z"),
        422,
        "wrong_scalar_type",
    ),
    # --- integers wider than the column they land in ----------------------------
    (
        "int-enrollment-year",
        "appdb",
        "student",
        _raw("appdb", "student", "n1", enrollment_year=INT32_MAX + 1),
        422,
        "wrong_scalar_type",
    ),
    (
        "int-amount-cents-bigint",
        "payments",
        "payment",
        _raw("payments", "payment", "n2", amount_cents=10**19),
        422,
        "wrong_scalar_type",
    ),
    (
        # fits `bigint` but overflows the stored generated `amount_cents * 10000`
        "int-amount-cents-generated",
        "payments",
        "payment",
        _raw("payments", "payment", "n3", amount_cents=MAX_AMOUNT_CENTS + 1),
        422,
        "wrong_scalar_type",
    ),
    (
        "float-deal-amount-huge",
        "crm",
        "deal",
        _raw("crm", "deal", "n4", amount=1e308),
        422,
        "wrong_scalar_type",
    ),
    # --- non-finite JSON numbers: not JSON at all, and not storable as jsonb -----
    (
        "nan-deal-amount",
        "crm",
        "deal",
        _raw("crm", "deal", "f1").replace('"amount": 5000.0', '"amount": NaN'),
        400,
        "unparseable_json",
    ),
    (
        "nan-extra-field",
        "crm",
        "contact",
        _raw("crm", "contact", "f2")[:-1] + ', "score": NaN}',
        400,
        "unparseable_json",
    ),
    (
        "infinity-extra-field",
        "crm",
        "contact",
        _raw("crm", "contact", "f3")[:-1] + ', "score": Infinity}',
        400,
        "unparseable_json",
    ),
    (
        "negative-infinity-payment",
        "payments",
        "payment",
        _raw("payments", "payment", "f4")[:-1] + ', "fee": -Infinity}',
        400,
        "unparseable_json",
    ),
    # --- text Postgres cannot hold, in text columns and in jsonb ----------------
    (
        "nul-in-text-column",
        "crm",
        "contact",
        _raw("crm", "contact", "t1", first_name=f"Ada{NUL}Byron"),
        422,
        "unstorable_value",
    ),
    (
        "nul-in-jsonb-metadata",
        "payments",
        "payment",
        _raw(
            "payments",
            "payment",
            "t2",
            metadata={"student_first_name": f"Ada{NUL}", "student_last_name": "Byron"},
        ),
        422,
        "unstorable_value",
    ),
    (
        # `extra="allow"`: an undeclared field still lands in the payload jsonb
        "nul-in-undeclared-field",
        "crm",
        "contact",
        _raw("crm", "contact", "t3", note=f"a{NUL}b"),
        422,
        "unstorable_value",
    ),
    (
        "nul-in-key",
        "crm",
        "contact",
        _raw("crm", "contact", "t4", **{f"no{NUL}te": "x"}),
        422,
        "unstorable_value",
    ),
    (
        "unpaired-surrogate",
        "crm",
        "contact",
        _raw("crm", "contact", "t5", last_name=LONE_SURROGATE),
        422,
        "unstorable_value",
    ),
]

#: The other half of the audit: values at the *edge* of what the store can hold,
#: which must be accepted. Without these the table above is satisfied by a
#: validator that rejects everything.
STORABLE: list[tuple[str, str, str, str]] = [
    ("edge-ts-offset", "crm", "contact", _raw("crm", "contact", "k1", created_at="2026-02-01")),
    (
        "edge-ts-explicit-offset",
        "crm",
        "contact",
        _raw("crm", "contact", "k2", created_at="2026-02-01T00:00:00+02:00"),
    ),
    (
        "edge-ts-microseconds",
        "crm",
        "contact",
        _raw("crm", "contact", "k3", created_at="2026-02-01T00:00:00.123456Z"),
    ),
    (
        "edge-int32-max",
        "appdb",
        "student",
        _raw("appdb", "student", "k4", enrollment_year=INT32_MAX),
    ),
    (
        "edge-amount-cents-max",
        "payments",
        "payment",
        _raw("payments", "payment", "k5", amount_cents=MAX_AMOUNT_CENTS),
    ),
    (
        "edge-negative-amount",
        "crm",
        "deal",
        _raw("crm", "deal", "k6", amount=-1250.5),
    ),
    (
        # an astral character is encoded as a surrogate *pair* by `ensure_ascii`
        # json, which is exactly what the cheap pre-scan trips on -- it must not be
        # mistaken for the unpaired surrogate two rows up
        "edge-astral-character",
        "crm",
        "contact",
        _raw("crm", "contact", "k7", first_name="Ada \U0001f600"),
    ),
    (
        "edge-accented-name",
        "crm",
        "contact",
        _raw("crm", "contact", "k8", first_name="Renée", last_name="O\u2019Brien"),
    ),
    (
        # SS7 / G27: an unrecognised enum *value* is well-formed and must ingest
        "edge-unmapped-enum",
        "crm",
        "contact",
        _raw("crm", "contact", "k9", lifecycle_stage="galactic_overlord"),
    ),
]


@pytest.fixture
def api(owner_engine, trigger_secret) -> TestClient:
    with TestClient(create_app()) as client:
        yield client


def _post(client: TestClient, source: str, entity_type: str, raws: list[str], run_id: str):
    return client.post(
        "/internal/ingest/records",
        json={
            "source": source,
            "entity_type": entity_type,
            "generation": GENERATION,
            "records": raws,
            "run_id": run_id,
        },
        headers=TRIGGER_HEADERS,
    )


@pytest.mark.parametrize(
    ("case_id", "source", "entity_type", "raw", "status", "kind"),
    UNSTORABLE,
    ids=[case[0] for case in UNSTORABLE],
)
def test_a_value_the_store_cannot_hold_is_a_structured_4xx_not_a_500(
    api: TestClient,
    owner_engine,
    case_id: str,
    source: str,
    entity_type: str,
    raw: str,
    status: int,
    kind: str,
) -> None:
    """The whole point of R2: the client learns its payload is wrong, not that we broke."""
    response = _post(api, source, entity_type, [raw], run_id=f"types-{case_id}")

    assert response.status_code < 500, (
        f"{case_id} produced {response.status_code}: a payload the database cannot "
        "store is still the payload's problem, never the server's"
    )
    assert response.status_code == status
    problem = response.json()
    assert {"type", "title", "status", "detail"} <= set(problem)
    assert problem["type"].endswith(kind), f"{case_id} was classified {problem['type']}"
    assert problem["accepted"] == 0 and problem["rejected"] == 1

    with owner_engine.connect() as connection:
        landed = connection.execute(
            text("SELECT count(*) FROM raw_records WHERE run_id = :run_id"),
            {"run_id": f"types-{case_id}"},
        ).scalar()
    assert landed == 0, "a rejected payload must not reach the landing table"


@pytest.mark.parametrize(
    ("case_id", "source", "entity_type", "raw", "status", "kind"),
    UNSTORABLE,
    ids=[case[0] for case in UNSTORABLE],
)
def test_the_same_payload_is_refused_on_the_file_path_too(
    case_id: str, source: str, entity_type: str, raw: str, status: int, kind: str
) -> None:
    """`ingest_source` reads through the same validator, so it cannot 500 either.

    The reported bug had two faces -- an HTTP 500 and a raw `InvalidDatetimeFormat`
    out of `ingest_source` -- and one cause. This is the second face.
    """
    with pytest.raises(AdapterError) as excinfo:
        validate_payload(source, entity_type, GENERATION, raw, line_no=1)
    error = excinfo.value
    assert error.kind == kind
    assert error.status == status
    assert error.status < 500
    assert error.problem()["line"] == 1


@pytest.mark.parametrize(
    ("case_id", "source", "entity_type", "raw"),
    STORABLE,
    ids=[case[0] for case in STORABLE],
)
def test_a_storable_edge_value_is_accepted_and_lands(
    api: TestClient, owner_engine, case_id: str, source: str, entity_type: str, raw: str
) -> None:
    """The negative control: the bounds admit everything the column can hold."""
    run_id = f"types-ok-{case_id}"
    response = _post(api, source, entity_type, [raw], run_id=run_id)

    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 1
    assert response.json()["rejected"] == 0

    with owner_engine.connect() as connection:
        landed = connection.execute(
            text("SELECT count(*) FROM raw_records WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar()
        staged = connection.execute(
            text(
                f"SELECT count(*) FROM {STAGING_TABLES[(source, entity_type)]} "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        ).scalar()
    assert landed == 1, "an accepted payload must actually be stored"
    assert staged == 1, "and must be materialized, or no rule will ever see it"


def test_every_timestamptz_column_has_a_validated_source_field(owner_engine) -> None:
    """The structural half: the table cannot silently grow a column nobody validates.

    `TIMESTAMP_FIELDS` is what the models enforce. The live `information_schema` is
    what the database will refuse. Reconciling them here means a new `timestamptz`
    staging column fed from the payload fails *this* test rather than surfacing as
    a 500 the way `created_at` did.
    """
    with owner_engine.connect() as connection:
        for (source, entity_type), table in sorted(STAGING_TABLES.items()):
            columns = set(
                connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = :table "
                        "AND data_type = 'timestamp with time zone'"
                    ),
                    {"table": table},
                )
                .scalars()
                .all()
            )
            # `materialized_at` is the row's own write time (a server default), not
            # a payload field, and is the only timestamptz column not sourced.
            payload_sourced = columns - {"materialized_at"}
            assert payload_sourced == set(TIMESTAMP_FIELDS[(source, entity_type)]), (
                f"{table} timestamptz columns {sorted(payload_sourced)} do not match the "
                f"validated fields {sorted(TIMESTAMP_FIELDS[(source, entity_type)])}; an "
                "unvalidated timestamp field is a latent 500 at the COPY"
            )


@pytest.mark.parametrize(("source", "entity_type"), sorted(TIMESTAMP_FIELDS))
def test_every_declared_timestamp_field_rejects_an_unparseable_value(
    source: str, entity_type: str
) -> None:
    """And each declared field really is validated, one field at a time."""
    for field in TIMESTAMP_FIELDS[(source, entity_type)]:
        raw = _raw(source, entity_type, f"sweep-{field}", **{field: "not-a-timestamp"})
        with pytest.raises(AdapterError) as excinfo:
            validate_payload(source, entity_type, GENERATION, raw)
        assert excinfo.value.status == 422
        assert field in excinfo.value.detail, (
            f"{source}/{entity_type}.{field} was not the field named in the rejection"
        )
