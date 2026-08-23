"""The benchmark the constraints name: cross-source query p95 < 1s on 100k, 20 runs.

Measured against the materialized generation-3 dataset -- 120,000 records, 43,375
canonical entities -- with the warm-up discarded, as the constraint says. Both the
`person_key` form (a primary-key read) and the `source_ref` form (an index lookup
on `entity_links` followed by that read) are timed, because a benchmark that only
measures the fastest path is measuring the wrong thing: a reviewer holds a source
id far more often than a UUID.

The numbers are printed, not just asserted, so a run that passes at 950ms is
distinguishable from one that passes at 4ms.
"""

from __future__ import annotations

import math
import time
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tests.api.conftest import ADMIN_HEADERS
from tests.er.dataset import Dataset

#: The committed benchmark: 20 runs, warm-up discarded, p95 under one second.
RUNS = 20
WARMUP = 3
P95_BUDGET_SECONDS = 1.0

_SAMPLE = text(
    """
    SELECT canonical_id::text AS canonical_id, current ->> 'anchor_ref' AS anchor_ref
      FROM entities
     ORDER BY canonical_id
     LIMIT :limit
    """
)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _measure(api: TestClient, keys: list[str]) -> dict[str, Any]:
    for key in keys[:WARMUP]:
        assert api.get(f"/api/entities/{key}", headers=ADMIN_HEADERS).status_code == 200

    samples: list[float] = []
    for index in range(RUNS):
        key = keys[index % len(keys)]
        started = time.perf_counter()
        response = api.get(f"/api/entities/{key}", headers=ADMIN_HEADERS)
        samples.append(time.perf_counter() - started)
        assert response.status_code == 200, response.text
    return {
        "runs": len(samples),
        "p50": _percentile(samples, 0.50),
        "p95": _percentile(samples, 0.95),
        "max": max(samples),
    }


def test_entity_query_p95_under_one_second(
    api: TestClient, reader: Engine, dataset: Dataset
) -> None:
    """R10's benchmark, both key forms, on the full generation-3 dataset."""
    with reader.connect() as conn:
        rows = conn.execute(_SAMPLE, {"limit": RUNS + WARMUP}).fetchall()
    assert len(rows) == RUNS + WARMUP

    by_person_key = _measure(api, [row.canonical_id for row in rows])
    by_source_ref = _measure(api, [row.anchor_ref for row in rows])

    print(
        f"\nentities queried: {dataset.report.persons} over {dataset.report.links} links\n"
        f"  person_key form: p50={by_person_key['p50'] * 1000:.1f}ms "
        f"p95={by_person_key['p95'] * 1000:.1f}ms max={by_person_key['max'] * 1000:.1f}ms\n"
        f"  source_ref form: p50={by_source_ref['p50'] * 1000:.1f}ms "
        f"p95={by_source_ref['p95'] * 1000:.1f}ms max={by_source_ref['max'] * 1000:.1f}ms"
    )

    for label, measured in (("person_key", by_person_key), ("source_ref", by_source_ref)):
        assert measured["p95"] < P95_BUDGET_SECONDS, (
            f"{label} p95 is {measured['p95'] * 1000:.1f}ms over {RUNS} runs, "
            f"budget {P95_BUDGET_SECONDS * 1000:.0f}ms"
        )


def test_the_query_plan_is_an_index_read(reader: Engine) -> None:
    """The p95 above is a property of the plan, not of a warm cache.

    A sequential scan of `entities` would still come in under a second at 43,000
    rows on a laptop and would fall over at ten times that. This asserts the plan
    itself, so the benchmark cannot pass for the wrong reason.
    """
    with reader.connect() as conn:
        row = conn.execute(text("SELECT canonical_id::text AS id FROM entities LIMIT 1")).one()
        plan = "\n".join(
            line[0]
            for line in conn.execute(
                text(
                    "EXPLAIN SELECT canonical_id, current FROM entities "
                    "WHERE canonical_id = CAST(:id AS uuid)"
                ),
                {"id": row.id},
            )
        )
    assert "Seq Scan" not in plan, plan
    assert "Index Scan" in plan or "Index Only Scan" in plan, plan
