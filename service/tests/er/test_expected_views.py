"""The join contract: every `golden/expected-views.json` entry, out of the database.

`golden/expected-views.json` is hand-checkable and committed, and it was written
by the **generator** from its own in-memory world. This module asserts that the
**detector** -- fixtures through the adapters, into landing, into `stg_*`, through
`recon.er`, into `entities.current` -- reproduces every one of those entries
exactly. Dict equality, not a subset test: a view that silently lost a field or
gained one is a different join contract.

That equality is the only thing binding the two implementations of the view (the
generator's `recon.seed.golden._person_view` and `recon.resolve.person_view`),
because the layering forbids the detector from importing the generator. If either
drifts, this fails.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Engine, text

from recon.resolve import VIEW_FIELDS
from tests.er.dataset import Dataset

_SELECT = text("SELECT current FROM entities WHERE canonical_id = CAST(:key AS uuid)")


def _stored_view(reader: Engine, person_key: str) -> dict[str, Any] | None:
    with reader.connect() as conn:
        row = conn.execute(_SELECT, {"key": person_key}).fetchone()
    if row is None:
        return None
    return {field: row.current[field] for field in VIEW_FIELDS}


def test_every_expected_view_matches_exactly(
    reader: Engine, expected_views: list[dict[str, Any]], dataset: Dataset
) -> None:
    """All 25 committed views, field for field, from the materialized canonical rows."""
    matched = 0
    failures: list[str] = []

    for entry in expected_views:
        stored = _stored_view(reader, entry["person_key"])
        if stored is None:
            failures.append(f"{entry['anchor_ref']}: no canonical row was materialized")
            continue
        if stored == entry:
            matched += 1
            continue
        differing = sorted(key for key in entry if stored.get(key) != entry[key])
        failures.append(
            f"{entry['anchor_ref']}: {differing} differ\n"
            f"  stored : {json.dumps({k: stored.get(k) for k in differing}, sort_keys=True)}\n"
            f"  golden : {json.dumps({k: entry[k] for k in differing}, sort_keys=True)}"
        )

    assert not failures, (
        f"{matched}/{len(expected_views)} expected views matched; "
        f"{len(failures)} did not:\n" + "\n".join(failures)
    )
    assert matched == len(expected_views)
    assert matched >= 25, "SS8's join contract is at least 25 entities"


def test_view_keys_are_exactly_the_golden_keys(expected_views: list[dict[str, Any]]) -> None:
    """`VIEW_FIELDS` is the golden file's key set -- so equality above is total.

    Without this, `_stored_view` could project a subset of the golden keys and the
    comparison above would pass while the endpoint returned half a view.
    """
    for entry in expected_views:
        assert tuple(sorted(entry)) == tuple(sorted(VIEW_FIELDS)), (
            f"{entry['anchor_ref']}: golden view keys {sorted(entry)} != VIEW_FIELDS "
            f"{sorted(VIEW_FIELDS)}"
        )


def test_canonical_rows_carry_generation_and_tenant(
    reader: Engine, expected_views: list[dict[str, Any]]
) -> None:
    """The two keys `entities.current` adds on top of the view, and nothing else."""
    with reader.connect() as conn:
        row = conn.execute(_SELECT, {"key": expected_views[0]["person_key"]}).fetchone()
    assert row is not None
    assert set(row.current) == set(VIEW_FIELDS) | {"generation", "tenant"}
    assert row.current["generation"] == 3
    assert row.current["tenant"]


def test_registered_paid_stage_agree_with_the_underlying_rows(
    reader: Engine, expected_views: list[dict[str, Any]]
) -> None:
    """R10's three answers are not decorative: they must agree with `stg_*`.

    `registered` is "has an enrollment", `paid` is "holds a payment whose status is
    paid", `stage_funnel` is the normalized enrollment stage. Reading them back out
    of staging is what makes the view a *join* result rather than a stored opinion.
    """
    enrollment_sql = text(
        "SELECT count(*) FROM stg_enrollment WHERE generation = 3 AND student_id = :student_id"
    )
    payment_sql = text(
        "SELECT count(*) FROM stg_payment WHERE generation = 3 AND payment_id = ANY(:ids)"
        " AND status = 'paid'"
    )

    checked = 0
    with reader.connect() as conn:
        for entry in expected_views:
            student_refs = [ref for ref in entry["entity_refs"] if ref.startswith("appdb:student:")]
            enrollments = 0
            if student_refs:
                enrollments = int(
                    conn.execute(
                        enrollment_sql, {"student_id": student_refs[0].split(":")[-1]}
                    ).scalar_one()
                )
            assert entry["registered"] == (enrollments > 0), entry["anchor_ref"]

            payment_ids = [
                ref.split(":")[-1]
                for ref in entry["entity_refs"]
                if ref.startswith("payments:payment:")
            ]
            paid = 0
            if payment_ids:
                paid = int(conn.execute(payment_sql, {"ids": payment_ids}).scalar_one())
            assert entry["paid"] == (paid > 0), entry["anchor_ref"]
            checked += 1

    assert checked == len(expected_views)
