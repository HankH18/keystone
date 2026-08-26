"""An email that appears ONLY in `stg_payment` still resolves (R10).

`GET /api/entities/{key}` accepts an email as one of its four key forms, and the
question the brief asks -- *did this person pay?* -- is most often asked with the
address off the receipt. The union behind that lookup read
`stg_crm_contact.email_norm` and the two `stg_student` guardian addresses and
**stopped there**, so the one source that is definitionally about paying was not
searched: a payer with no CRM contact and no app-DB student row -- the generator
plants 200 of them in generation 3, each a person whose only ref is its own
`payments:payment:` ref (contract SS4.1, `recon.er`) -- answered 404.

The failure is not a missing feature, it is a *silent wrong answer*: 404 on this
endpoint means "no such person", and the person exists, is linked, and has a
canonical row. A reviewer holding the payer's address concludes the payment
belongs to nobody.

`ix_stg_payment_email` -- `(generation, email_norm)`, migration 0001 -- has
existed since the first schema, so the arm is an index read like the other two
and this is not a scan traded for a feature.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from recon.resolve import CURRENT_GENERATION
from tests.api.conftest import ADMIN_HEADERS

#: An address that is in `stg_payment` and in neither of the two sources the
#: union already searched, whose payment ref resolves to exactly one person.
#:
#: Discovered from the materialized dataset rather than written down: the point
#: is that the endpoint answers for whatever the *committed* fixtures contain, and
#: a hard-coded address would pass while the union was still wrong for every
#: other one. `HAVING count(DISTINCT ...) = 1` keeps the case unambiguous -- an
#: email reaching two persons is a 409 by design (contract SS4.8) and is a
#: different test.
_PAYMENT_ONLY_EMAILS = text(
    """
    SELECT p.email_norm AS email,
           min(el.canonical_id::text) AS canonical_id,
           min('payments:payment:' || p.payment_id) AS source_ref
      FROM stg_payment p
      JOIN entity_links el
        ON el.generation = p.generation
       AND el.source_ref = 'payments:payment:' || p.payment_id
     WHERE p.generation = :generation
       AND p.email_norm IS NOT NULL
       AND NOT EXISTS (
             SELECT 1 FROM stg_crm_contact c
              WHERE c.generation = p.generation AND c.email_norm = p.email_norm)
       AND NOT EXISTS (
             SELECT 1 FROM stg_student s
              WHERE s.generation = p.generation
                AND (s.email_norm = p.email_norm OR s.guardian2_email_norm = p.email_norm))
     GROUP BY p.email_norm
    HAVING count(DISTINCT el.canonical_id) = 1
     ORDER BY p.email_norm
     LIMIT 5
    """
)


@pytest.fixture(scope="module")
def payment_only(reader: Engine) -> list[dict[str, Any]]:
    """Payer addresses the old union could not see, straight out of the dataset."""
    with reader.connect() as conn:
        rows = conn.execute(_PAYMENT_ONLY_EMAILS, {"generation": CURRENT_GENERATION}).fetchall()
    cases = [
        {"email": row.email, "canonical_id": row.canonical_id, "source_ref": row.source_ref}
        for row in rows
    ]
    assert cases, (
        "no payment-only payer email in the materialized generation: the committed "
        "fixtures plant 200 orphan payments, so an empty result means the dataset "
        "is not the graded one and this test would prove nothing"
    )
    return cases


def test_an_email_that_appears_only_in_payments_resolves_to_its_person(
    api: TestClient, payment_only: list[dict[str, Any]]
) -> None:
    """The whole gap, at the endpoint: 404 before, the person's own row after."""
    failures: list[str] = []
    for case in payment_only:
        response = api.get(f"/api/entities/{case['email']}", headers=ADMIN_HEADERS)
        if response.status_code != 200:
            failures.append(f"{case['source_ref']}: HTTP {response.status_code} {response.text}")
            continue
        body = response.json()
        if body["key"]["canonical_id"] != case["canonical_id"]:
            failures.append(
                f"{case['source_ref']}: resolved to {body['key']['canonical_id']}, "
                f"expected {case['canonical_id']}"
            )
    assert not failures, "payment-only emails did not resolve:\n" + "\n".join(failures)


def test_the_resolution_is_reported_as_the_email_form(
    api: TestClient, payment_only: list[dict[str, Any]]
) -> None:
    """`key.form` must say `email` -- it is not a natural key that happens to work."""
    case = payment_only[0]
    body = api.get(f"/api/entities/{case['email']}", headers=ADMIN_HEADERS).json()
    assert body["key"]["form"] == "email"
    assert body["key"]["requested"] == case["email"]


def test_the_payment_ref_and_the_email_reach_the_same_person(
    api: TestClient, payment_only: list[dict[str, Any]]
) -> None:
    """Two identifiers for one payment must not disagree about who paid.

    The `source_ref` arm always worked; the email arm is the one that was blind.
    Asserting they agree is what makes this about the *union* rather than about
    one address happening to return a 200.
    """
    for case in payment_only[:3]:
        by_email = api.get(f"/api/entities/{case['email']}", headers=ADMIN_HEADERS)
        by_ref = api.get(f"/api/entities/{case['source_ref']}", headers=ADMIN_HEADERS)
        assert by_email.status_code == 200, by_email.text
        assert by_ref.status_code == 200, by_ref.text
        assert by_email.json()["view"] == by_ref.json()["view"]


def test_the_payments_arm_did_not_break_the_two_arms_that_already_worked(
    api: TestClient, reader: Engine
) -> None:
    """A regression guard on the union, not a restatement of `test_entities_view`.

    Adding a third `UNION` arm can only go wrong in two ways: it drops a row the
    old union returned, or it merges persons that were distinct. A CRM address
    that is *also* a payer address is exactly where both would show, so it is the
    one asserted here.
    """
    shared = text(
        """
        SELECT c.email_norm AS email
          FROM stg_crm_contact c
          JOIN stg_payment p
            ON p.generation = c.generation AND p.email_norm = c.email_norm
         WHERE c.generation = :generation
           AND c.email_norm IS NOT NULL
         ORDER BY c.email_norm
         LIMIT 5
        """
    )
    with reader.connect() as conn:
        rows = conn.execute(shared, {"generation": CURRENT_GENERATION}).fetchall()
    assert rows, "no address is shared between the CRM and payments in this generation"

    for row in rows:
        response = api.get(f"/api/entities/{row.email}", headers=ADMIN_HEADERS)
        assert response.status_code in {200, 409}, response.text
        if response.status_code == 409:
            # Ambiguity is the designed answer, and it must still name candidates.
            assert response.json()["candidates"]
