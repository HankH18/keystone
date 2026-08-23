"""`source_generations` describes ONE scope, and `complete` cannot lie about it (SS5.3).

The defect this file is the answer to was a **scope mismatch inside one row**:
`loaded_count` was measured over the generation while `complete` and
`rejected_count` were measured over the slice in hand, and
`INSERT ... ON CONFLICT DO UPDATE SET complete = EXCLUDED.complete` then let
whichever slice committed last publish its own verdict as the generation's.
Measured before the fix, on the real endpoint: slice A landed 20 payments and
rejected 5, slice B landed 5 more, and the generation came out
`complete = true, rejected_count = 0` over a dataset missing five records.

That is the worst reachable bug in this module rather than a bookkeeping slip.
Contract SS5.3 makes the invariant engine **skip** every absence rule (C1, C2,
C5, C7, C8, C9, C13) on an incomplete generation, precisely because running an
absence test over missing rows manufactures false positives. A `complete` that is
true while rows are missing hands the detector a truncated dataset it believes is
whole -- and the correctness grade is zero false positives against the clean
majority.

**Every test here reconciles three views of the same generation**, read back from
the database after the write rather than reported by the code that did it:

* `raw_records` -- the append-only mirror (arrivals, and distinct natural keys);
* `stg_*` -- what `rules/*.sql` can actually see;
* the `source_generations` row -- `expected_count`, `loaded_count`,
  `rejected_count`, `complete`.

Seven scenarios, one per way a generation can be assembled: one slice; two
disjoint slices; a re-asserted slice; a slice that rejects records; a slice that
fails mid-way; two entity types sharing a run id; and two slices racing. Each one
asserts that the three views agree and that `complete` is exactly
`LEDGER_COMPLETE_RULE` over the numbers the database measured.

The last section **re-installs the defect** -- a legacy stamp that writes
`loaded_count`, `rejected_count` and `complete` straight from the slice in hand --
and asserts each scenario catches it, with a green no-op control alongside.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import recon.ingest as ingest_module
from recon.adapters import FaultInjectingAdapter, RawRecord, stub_records
from recon.app import create_app
from recon.db import ROLE_RECON_WRITER, role_connection
from recon.ingest import (
    LEDGER_COMPLETE_RULE,
    LEDGER_SCOPE,
    STAGING,
    ingest_source,
    ledger_complete,
    load_key,
)
from tests.ingest.conftest import TRIGGER_HEADERS

#: Generations this module owns. Inside the `>= 900` range `conftest._purge`
#: removes, and outside every range another ingest module uses.
GEN_ONE_SLICE = 931
GEN_TWO_SLICES = 932
GEN_REASSERTED = 933
GEN_REJECTED = 934
GEN_MIDWAY = 935
GEN_TWO_TYPES = 936
GEN_RACE = 937
GEN_NO_EXPECTATION = 938

ALL_GENERATIONS = (
    GEN_ONE_SLICE,
    GEN_TWO_SLICES,
    GEN_REASSERTED,
    GEN_REJECTED,
    GEN_MIDWAY,
    GEN_TWO_TYPES,
    GEN_RACE,
    GEN_NO_EXPECTATION,
)

TABLES = ("stg_crm_contact", "stg_crm_deal", "stg_payment", "raw_records")


# ======================================================================================
# payloads and helpers
# ======================================================================================


def contact(index: int) -> str:
    """One well-formed CRM contact, as the literal line the endpoint takes."""
    return json.dumps(
        {
            "crm_id": f"CRM-LEDGER-{index:06d}",
            "email": f"ledger{index}@example.invalid",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "lifecycle_stage": "lead",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        },
        sort_keys=True,
    )


def deal(index: int) -> str:
    """One well-formed CRM deal."""
    return json.dumps(
        {
            "deal_id": f"DEAL-LEDGER-{index:06d}",
            "name": "Household deal",
            "pipeline": "Lower School",
            "stage": "Closed Won",
            "amount": 1200.00,
            "associated_contact_ids": [f"CRM-LEDGER-{index:06d}"],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        },
        sort_keys=True,
    )


def payment(index: int) -> str:
    """One well-formed payment (SS1.5)."""
    return json.dumps(
        {
            "payment_id": f"pi_ledger_{index:07d}",
            "payer_email": f"guardian{index}@example.invalid",
            "payer_name": "Ada Lovelace",
            "amount_cents": 50000,
            "currency": "usd",
            "type": "deposit",
            "status": "paid",
            "occurred_at": "2026-02-01T00:00:00Z",
            "metadata": {
                "student_first_name": "Ada",
                "student_last_name": "Lovelace",
                "program": "Lower School",
            },
        },
        sort_keys=True,
    )


def contact_records(indices: Sequence[int], generation: int) -> tuple[RawRecord, ...]:
    """The same contacts as already-validated records, for the file path."""
    records = stub_records(
        max(indices) + 1, source_id="crm", entity_type="contact", generation=generation
    )
    return tuple(records[index] for index in indices)


@dataclass(frozen=True)
class Reconciliation:
    """The three views of one `(source, entity_type, generation)`, read back."""

    source_id: str
    entity_type: str
    generation: int
    #: `raw_records`: rows, and distinct natural keys. They differ by design when
    #: a slice is re-asserted -- landing is append-only history.
    arrivals: int
    keys: int
    #: `stg_*`: what `rules/*.sql` can see. `None` for a pair with no staging table.
    staged: int | None
    expected: int | None
    loaded_count: int
    rejected_count: int
    complete: bool
    slices: dict[str, Any]

    @property
    def visible(self) -> int:
        return 0 if self.staged is None else self.staged

    def row(self) -> str:
        """One line of the reconciliation table."""
        return (
            f"{self.source_id}/{self.entity_type} g{self.generation} | "
            f"raw={self.arrivals} keys={self.keys} "
            f"stg={'-' if self.staged is None else self.staged} | "
            f"expected={self.expected} loaded={self.loaded_count} "
            f"rejected={self.rejected_count} complete={self.complete}"
        )


def reconcile(engine: Engine, source_id: str, entity_type: str, generation: int) -> Reconciliation:
    """Read all three views out of the database. Nothing here is reported by ingest."""
    spec = STAGING.get((source_id, entity_type))
    scope = {"s": source_id, "e": entity_type, "g": generation}
    with engine.connect() as conn:
        arrivals = conn.execute(
            text(
                "SELECT count(*) FROM raw_records WHERE source_id = :s "
                "AND entity_type = :e AND generation = :g"
            ),
            scope,
        ).scalar_one()
        keys = conn.execute(
            text(
                "SELECT count(DISTINCT natural_key) FROM raw_records WHERE source_id = :s "
                "AND entity_type = :e AND generation = :g"
            ),
            scope,
        ).scalar_one()
        staged = (
            None
            if spec is None
            else conn.execute(
                text(
                    f'SELECT count(*) FROM "{spec.table}" WHERE source_id = :s AND generation = :g'
                ),
                scope,
            ).scalar_one()
        )
        row = conn.execute(
            text(
                "SELECT expected_count, loaded_count, rejected_count, complete, error_detail "
                "FROM source_generations WHERE source_id = :s AND entity_type = :e "
                "AND generation = :g"
            ),
            scope,
        ).one()
    detail = row.error_detail or {}
    return Reconciliation(
        source_id=source_id,
        entity_type=entity_type,
        generation=generation,
        arrivals=int(arrivals),
        keys=int(keys),
        staged=None if staged is None else int(staged),
        expected=row.expected_count,
        loaded_count=row.loaded_count,
        rejected_count=row.rejected_count,
        complete=row.complete,
        slices=dict(detail.get("slices") or {}),
    )


def assert_views_agree(view: Reconciliation) -> None:
    """The property every scenario shares, whatever produced the generation.

    Stated once so a scenario cannot quietly assert less than the others: the
    ledger's `loaded_count` is the generation's distinct landed keys, staging
    holds exactly those keys, and `complete` is `LEDGER_COMPLETE_RULE` over the
    two numbers the database measured -- never a flag a writer chose.
    """
    assert view.loaded_count == view.keys, (
        f"the ledger reports {view.loaded_count} loaded for a generation holding "
        f"{view.keys} distinct landed keys: {view.row()}"
    )
    if view.staged is not None:
        assert view.staged == view.keys, (
            f"{view.staged} staged rows for {view.keys} landed keys; every rule reads "
            f"stg_* and would be handed a truncated generation: {view.row()}"
        )
    assert view.complete is ledger_complete(view.expected, view.loaded_count, view.visible), (
        f"complete is not {LEDGER_COMPLETE_RULE}: {view.row()}"
    )


@pytest.fixture
def api(trigger_secret: str, owner_engine: Engine) -> Iterator[TestClient]:
    _clear(owner_engine)
    with TestClient(create_app()) as client:
        yield client
    _clear(owner_engine)


@pytest.fixture
def clean(owner_engine: Engine) -> Iterator[Engine]:
    """The file-path scenarios' cleanup: this module's generations, before and after."""
    _clear(owner_engine)
    yield owner_engine
    _clear(owner_engine)


def _clear(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in (*TABLES, "source_generations", "ingest_runs"):
            conn.execute(
                text(f"DELETE FROM {table} WHERE generation = ANY(:generations)"),
                {"generations": list(ALL_GENERATIONS)},
            )


def post(
    client: TestClient,
    records: Sequence[str],
    *,
    generation: int,
    run_id: str,
    source: str = "crm",
    entity_type: str = "contact",
) -> tuple[int, dict]:
    response = client.post(
        "/internal/ingest/records",
        json={
            "source": source,
            "entity_type": entity_type,
            "generation": generation,
            "records": list(records),
            "run_id": run_id,
        },
        headers=TRIGGER_HEADERS,
    )
    return response.status_code, response.json()


def land(
    records: Sequence[RawRecord],
    *,
    generation: int,
    run_id: str,
    expected: int | None = None,
    mode: str = "ok",
    fail_after: int = 0,
):
    """One real `ingest_source` call over the real `recon_writer` connection."""
    adapter = FaultInjectingAdapter(
        source_id="crm",
        mode=mode,
        records=records,
        available_generations=(generation,),
        fail_after=fail_after,
    )
    adapter.entity_types = ("contact",)
    counts = {} if expected is None else {("crm", "contact", generation): expected}
    with role_connection(ROLE_RECON_WRITER) as conn:
        return ingest_source(
            adapter,
            generation,
            run_id=run_id,
            conn=conn,
            expected=counts,
            stall_timeout=2.0,
            deadline_seconds=15.0,
        )


# ======================================================================================
# the scope is stated where it is enforced
# ======================================================================================


def test_the_row_scope_and_the_completeness_rule_are_stated_once() -> None:
    """Both are consumed by SS5.3, so both are committed strings, not folklore."""
    assert "(source_id, entity_type, generation)" in LEDGER_SCOPE
    assert LEDGER_COMPLETE_RULE == (
        "complete = expected_count IS NOT NULL AND loaded_count = expected_count "
        "AND visible = expected_count"
    )


def test_completeness_is_a_function_of_the_counts_and_nothing_else() -> None:
    """The Python twin of the SQL, over the matrix that matters."""
    assert ledger_complete(10, 10, 10) is True
    assert ledger_complete(10, 9, 9) is False, "short of the expectation"
    assert ledger_complete(10, 10, 9) is False, "landed but not all visible to a rule"
    assert ledger_complete(10, 10, 0) is False, "nothing materialized: no rule can see it"
    assert ledger_complete(None, 10, 10) is False, (
        "absence of an expectation is not evidence of completeness: a one-record "
        "POST must not mark a whole generation complete"
    )
    assert ledger_complete(0, 0, 0) is True, "nothing expected and nothing missing"


# ======================================================================================
# scenario 1: one slice
# ======================================================================================


def test_one_slice_reconciles(clean: Engine) -> None:
    result = land(
        contact_records(range(12), GEN_ONE_SLICE),
        generation=GEN_ONE_SLICE,
        run_id="ledger-one",
        expected=12,
    )
    view = reconcile(clean, "crm", "contact", GEN_ONE_SLICE)
    print(view.row())

    assert result.status == "ok"
    assert_views_agree(view)
    assert (view.arrivals, view.keys, view.staged) == (12, 12, 12)
    assert (view.expected, view.loaded_count, view.rejected_count) == (12, 12, 0)
    assert view.complete is True


# ======================================================================================
# scenario 2: two disjoint slices
# ======================================================================================


def test_two_disjoint_slices_combine_rather_than_overwrite(clean: Engine) -> None:
    """The first slice's numbers survive the second slice's commit.

    And `complete` follows the generation, not the slice: false while half of it
    is missing, true once the whole of it is visible -- which is the only reading
    of "what is visible equals what is expected" that SS5.3 can act on.
    """
    records = contact_records(range(20), GEN_TWO_SLICES)
    land(records[:10], generation=GEN_TWO_SLICES, run_id="ledger-slice-a", expected=20)
    first = reconcile(clean, "crm", "contact", GEN_TWO_SLICES)
    print(first.row())
    assert_views_agree(first)
    assert (first.loaded_count, first.complete) == (10, False), (
        "half a generation may never be reported complete"
    )

    land(records[10:], generation=GEN_TWO_SLICES, run_id="ledger-slice-b", expected=20)
    second = reconcile(clean, "crm", "contact", GEN_TWO_SLICES)
    print(second.row())
    assert_views_agree(second)
    assert (second.arrivals, second.keys, second.staged) == (20, 20, 20)
    assert second.loaded_count == 20, (
        f"the second slice reported {second.loaded_count} for a 20-record generation; "
        "a slice's own row count is not the generation's"
    )
    assert second.complete is True
    assert set(second.slices) == {
        load_key("ledger-slice-a", "crm", "contact", GEN_TWO_SLICES),
        load_key("ledger-slice-b", "crm", "contact", GEN_TWO_SLICES),
    }, "both contributing loads must be recorded on the row, not just the last one"


# ======================================================================================
# scenario 3: a re-asserted slice
# ======================================================================================


def test_a_re_asserted_slice_is_history_in_landing_and_one_row_in_staging(
    clean: Engine,
) -> None:
    """Arrivals double, the generation does not. The ledger counts the generation."""
    records = contact_records(range(10), GEN_REASSERTED)
    land(records, generation=GEN_REASSERTED, run_id="ledger-assert-a", expected=10)
    land(records, generation=GEN_REASSERTED, run_id="ledger-assert-b", expected=10)

    view = reconcile(clean, "crm", "contact", GEN_REASSERTED)
    print(view.row())
    assert_views_agree(view)
    assert view.arrivals == 20, "landing is append-only; both arrivals are R4 history"
    assert (view.keys, view.staged, view.loaded_count) == (10, 10, 10)
    assert view.complete is True
    assert view.rejected_count == 0


def test_a_replayed_load_contributes_exactly_once(clean: Engine) -> None:
    """The same load id twice is a no-op, and the row does not move.

    The contribution map is keyed by `load_id`, so a replay overwrites its own
    entry instead of appending a second one -- the property that makes the
    aggregate additive across slices without being additive across retries.
    """
    records = contact_records(range(6), GEN_REASSERTED)
    land(records, generation=GEN_REASSERTED, run_id="ledger-replay", expected=6)
    before = reconcile(clean, "crm", "contact", GEN_REASSERTED)
    land(records, generation=GEN_REASSERTED, run_id="ledger-replay", expected=6)
    after = reconcile(clean, "crm", "contact", GEN_REASSERTED)
    print(after.row())

    assert (after.arrivals, after.keys) == (before.arrivals, before.keys) == (6, 6)
    assert after.loaded_count == 6
    assert len(after.slices) == 1
    assert_views_agree(after)


# ======================================================================================
# scenario 4: a slice that rejects records -- the measured defect
# ======================================================================================


def test_a_later_slice_cannot_erase_an_earlier_slices_rejections(api: TestClient) -> None:
    """The blocker, reproduced at its original width and refuted.

    Slice A lands 20 payments and rejects 5; slice B lands 5 more and commits
    last. Before the fix the generation reported `complete = true` with
    `rejected_count = 0`: B's verdict, over A's missing records. SS5.3 then lets
    every absence rule run against it.
    """
    valid = [payment(index) for index in range(20)]
    malformed = ['{"payment_id": ', "not json at all", "[]", '{"payment_id": null}', "{}"]

    status, body = post(
        api,
        [*valid, *malformed],
        generation=GEN_REJECTED,
        run_id="ledger-reject-a",
        source="payments",
        entity_type="payment",
    )
    assert status >= 400, "a batch carrying malformed lines answers with their 4xx"
    assert body["accepted"] == 20 and body["rejected"] == 5

    status, body = post(
        api,
        [payment(index) for index in range(20, 25)],
        generation=GEN_REJECTED,
        run_id="ledger-reject-b",
        source="payments",
        entity_type="payment",
    )
    assert status == 200
    assert body["generation_complete"] is False, (
        "the slice that committed last must not be able to publish its own verdict "
        "as the generation's"
    )
    assert body["generation_rejected"] == 5, (
        "the earlier slice's five rejections are the evidence that five records of "
        "this generation could not be read; a later slice may not erase them"
    )


def test_the_rejected_generation_reconciles(api: TestClient, owner_engine: Engine) -> None:
    """The same scenario, read straight out of the database."""
    valid = [payment(index) for index in range(20)]
    malformed = ['{"payment_id": ', "not json at all", "[]", '{"payment_id": null}', "{}"]
    post(
        api,
        [*valid, *malformed],
        generation=GEN_REJECTED,
        run_id="ledger-reject-a",
        source="payments",
        entity_type="payment",
    )
    first = reconcile(owner_engine, "payments", "payment", GEN_REJECTED)
    print(first.row())
    assert (first.arrivals, first.keys, first.staged) == (20, 20, 20)
    assert first.rejected_count == 5
    assert_views_agree(first)

    post(
        api,
        [payment(index) for index in range(20, 25)],
        generation=GEN_REJECTED,
        run_id="ledger-reject-b",
        source="payments",
        entity_type="payment",
    )
    second = reconcile(owner_engine, "payments", "payment", GEN_REJECTED)
    print(second.row())
    assert (second.arrivals, second.keys, second.staged) == (25, 25, 25)
    assert second.loaded_count == 25
    assert second.rejected_count == 5, (
        f"rejected_count is {second.rejected_count} after a clean slice committed "
        "over a slice that rejected five records"
    )
    assert second.complete is False
    assert_views_agree(second)


# ======================================================================================
# scenario 5: a slice that fails mid-way
# ======================================================================================


def test_a_slice_that_fails_midway_reconciles_short(clean: Engine) -> None:
    """Eight of twenty landed: the row says eight, and says it is not complete."""
    result = land(
        contact_records(range(20), GEN_MIDWAY),
        generation=GEN_MIDWAY,
        run_id="ledger-midway",
        expected=20,
        mode="midstream_error",
        fail_after=8,
    )
    view = reconcile(clean, "crm", "contact", GEN_MIDWAY)
    print(view.row())

    assert result.status == "partial"
    assert result.error is not None
    assert (view.arrivals, view.keys, view.staged) == (8, 8, 8)
    assert (view.expected, view.loaded_count) == (20, 8)
    assert view.complete is False
    assert_views_agree(view)

    # The rest of the generation arrives later: the row completes, without any
    # slice ever having asserted that it did.
    land(
        contact_records(range(8, 20), GEN_MIDWAY),
        generation=GEN_MIDWAY,
        run_id="ledger-midway-rest",
        expected=20,
    )
    healed = reconcile(clean, "crm", "contact", GEN_MIDWAY)
    print(healed.row())
    assert (healed.keys, healed.staged, healed.loaded_count) == (20, 20, 20)
    assert healed.complete is True
    assert_views_agree(healed)


# ======================================================================================
# scenario 6: two entity types sharing a run id
# ======================================================================================


def test_two_entity_types_sharing_a_run_id_both_count(
    api: TestClient, owner_engine: Engine
) -> None:
    """`ingest_runs` is keyed `(run_id, source_id)`, so it counts both of them.

    `records_ok = EXCLUDED.records_ok` discarded the first entity type's count --
    the blocker's scope mismatch, one table over. The count is now
    `count(*)` of the landing rows carrying this run id and source, which is the
    row's own scope and is additive by construction.
    """
    run_id = "ledger-two-types"
    assert (
        post(api, [contact(i) for i in range(3)], generation=GEN_TWO_TYPES, run_id=run_id)[0] == 200
    )
    assert (
        post(
            api,
            [deal(i) for i in range(5)],
            generation=GEN_TWO_TYPES,
            run_id=run_id,
            entity_type="deal",
        )[0]
        == 200
    )

    with owner_engine.connect() as conn:
        run = conn.execute(
            text(
                "SELECT records_ok, records_rejected, generation FROM ingest_runs "
                "WHERE run_id = :r AND source_id = 'crm'"
            ),
            {"r": run_id},
        ).one()
    assert run.records_ok == 8, (
        f"the run row reports {run.records_ok} records for 3 contacts and 5 deals "
        "landed under one run id; the second entity type overwrote the first"
    )
    assert run.records_rejected == 0

    contacts = reconcile(owner_engine, "crm", "contact", GEN_TWO_TYPES)
    deals = reconcile(owner_engine, "crm", "deal", GEN_TWO_TYPES)
    print(contacts.row())
    print(deals.row())
    assert (contacts.keys, contacts.staged, contacts.loaded_count) == (3, 3, 3)
    assert (deals.keys, deals.staged, deals.loaded_count) == (5, 5, 5)
    assert_views_agree(contacts)
    assert_views_agree(deals)


# ======================================================================================
# scenario 7: two slices racing
# ======================================================================================


def test_two_slices_racing_land_both_and_the_row_counts_both(
    api: TestClient, owner_engine: Engine
) -> None:
    """Disjoint slices, posted concurrently under different run ids.

    The advisory lock serialises them (T-4d), so this is the ledger question the
    lock leaves open: whichever commits second must add to the row rather than
    replace it.
    """
    first = [contact(index) for index in range(0, 50)]
    second = [contact(index) for index in range(50, 100)]
    outcomes: list[tuple[int, dict]] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def fire(records: Sequence[str], run_id: str) -> None:
        barrier.wait(timeout=60)
        outcome = post(api, records, generation=GEN_RACE, run_id=run_id)
        with lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=fire, args=(first, "ledger-race-a")),
        threading.Thread(target=fire, args=(second, "ledger-race-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
    assert not any(thread.is_alive() for thread in threads), "a request never returned"

    assert [status for status, _ in outcomes] == [200, 200], (
        f"two disjoint slices must both land: {[status for status, _ in outcomes]}"
    )
    view = reconcile(owner_engine, "crm", "contact", GEN_RACE)
    print(view.row())
    assert (view.arrivals, view.keys, view.staged) == (100, 100, 100)
    assert view.loaded_count == 100
    assert len(view.slices) == 2
    assert_views_agree(view)


# ======================================================================================
# no expectation: the fail-safe
# ======================================================================================


def test_a_single_record_post_does_not_mark_a_generation_complete(
    api: TestClient, owner_engine: Engine
) -> None:
    """Absence of an expectation is not evidence of completeness.

    `/internal/ingest/records` carries no manifest, so a one-record POST used to
    land one contact and report the whole generation complete -- and SS5.3 then
    runs every absence rule against a generation of one.
    """
    status, body = post(api, [contact(0)], generation=GEN_NO_EXPECTATION, run_id="ledger-single")
    assert status == 200
    assert body["generation_complete"] is False
    assert body["generation_expected"] is None

    view = reconcile(owner_engine, "crm", "contact", GEN_NO_EXPECTATION)
    print(view.row())
    assert (view.keys, view.staged, view.loaded_count) == (1, 1, 1)
    assert view.expected is None
    assert view.complete is False
    assert_views_agree(view)


# ======================================================================================
# sabotage: re-install the defect and watch every scenario catch it
# ======================================================================================


def _legacy_stamp_ledger(conn, load, *, run_id, error=None):
    """The pre-fix write, restored exactly: the slice states the verdict.

    `loaded_count`, `rejected_count` and `complete` all come from the slice in
    hand and all are written with `DO UPDATE SET ... = EXCLUDED. ...`, so the
    last slice to commit publishes its own numbers as the generation's.
    """
    conn.execute(
        text(
            """
            INSERT INTO source_generations
                (source_id, generation, entity_type, expected_count, loaded_count,
                 rejected_count, complete, run_id, updated_at)
            VALUES
                (:source_id, :generation, :entity_type, :expected_count, :loaded_count,
                 :rejected_count, :complete, :run_id, now())
            ON CONFLICT (source_id, generation, entity_type) DO UPDATE SET
                expected_count = EXCLUDED.expected_count,
                loaded_count = EXCLUDED.loaded_count,
                rejected_count = EXCLUDED.rejected_count,
                complete = EXCLUDED.complete,
                run_id = EXCLUDED.run_id,
                updated_at = now()
            """
        ),
        {
            "source_id": load.source_id,
            "generation": load.generation,
            "entity_type": load.entity_type,
            "expected_count": load.expected,
            "loaded_count": load.visible,
            "rejected_count": load.rejected,
            "complete": not load.rejected and load.staged and load.expected in (None, load.visible),
            "run_id": run_id,
        },
    )
    return ingest_module.LedgerVerdict(
        expected=load.expected,
        loaded=load.visible,
        rejected=load.rejected,
        visible=load.visible,
        complete=not load.rejected and load.staged and load.expected in (None, load.visible),
    )


def test_sabotage_the_last_slice_publishing_its_own_verdict_is_caught(
    api: TestClient, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the defect re-installed, scenario 4's reconciliation must fail.

    This is the binding test: revert `stamp_ledger` to a slice-scoped write and
    the suite goes red here rather than staying green over a `complete` that
    lies.
    """
    monkeypatch.setattr(ingest_module, "stamp_ledger", _legacy_stamp_ledger)

    malformed = ['{"payment_id": ', "not json at all", "[]", '{"payment_id": null}', "{}"]
    post(
        api,
        [*[payment(index) for index in range(20)], *malformed],
        generation=GEN_REJECTED,
        run_id="sabotage-reject-a",
        source="payments",
        entity_type="payment",
    )
    post(
        api,
        [payment(index) for index in range(20, 25)],
        generation=GEN_REJECTED,
        run_id="sabotage-reject-b",
        source="payments",
        entity_type="payment",
    )
    view = reconcile(owner_engine, "payments", "payment", GEN_REJECTED)
    print("SABOTAGE " + view.row())

    assert view.rejected_count == 0 and view.complete is True, (
        "the sabotage did not reproduce the defect, so this test proves nothing"
    )
    with pytest.raises(AssertionError):
        assert_views_agree(view)


def test_sabotage_the_run_row_overwriting_its_count_is_caught(
    api: TestClient, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MINOR 1's defect, re-installed: the second entity type erases the first."""

    def legacy_stamp_run(conn, *, run_id, source_id, generation, status, loads, detail=None):
        conn.execute(
            text(
                """
                INSERT INTO ingest_runs
                    (run_id, source_id, generation, status, started_at, finished_at,
                     records_ok, records_rejected)
                VALUES
                    (:run_id, :source_id, :generation, CAST(:status AS ingest_status),
                     now(), now(), :records_ok, :records_rejected)
                ON CONFLICT (run_id, source_id) DO UPDATE SET
                    generation = EXCLUDED.generation,
                    status = EXCLUDED.status,
                    finished_at = now(),
                    records_ok = EXCLUDED.records_ok,
                    records_rejected = EXCLUDED.records_rejected
                """
            ),
            {
                "run_id": run_id,
                "source_id": source_id,
                "generation": generation,
                "status": status,
                "records_ok": sum(load.loaded for load in loads),
                "records_rejected": sum(load.rejected for load in loads),
            },
        )
        return ingest_module.RunVerdict(
            records_ok=sum(load.loaded for load in loads),
            records_rejected=sum(load.rejected for load in loads),
            status=status,
        )

    monkeypatch.setattr(ingest_module, "stamp_run", legacy_stamp_run)

    run_id = "sabotage-two-types"
    post(api, [contact(i) for i in range(3)], generation=GEN_TWO_TYPES, run_id=run_id)
    post(
        api,
        [deal(i) for i in range(5)],
        generation=GEN_TWO_TYPES,
        run_id=run_id,
        entity_type="deal",
    )
    with owner_engine.connect() as conn:
        records_ok = conn.execute(
            text("SELECT records_ok FROM ingest_runs WHERE run_id = :r AND source_id = 'crm'"),
            {"r": run_id},
        ).scalar_one()
    print(f"SABOTAGE ingest_runs({run_id}) records_ok={records_ok}")
    assert records_ok == 5, (
        "the sabotage did not reproduce the defect (the second entity type should "
        "have overwritten the first), so this test proves nothing"
    )


def test_the_green_no_op_control(api: TestClient, owner_engine: Engine) -> None:
    """The same two posts with nothing sabotaged: the row is right and stays right."""
    run_id = "control-two-types"
    post(api, [contact(i) for i in range(3)], generation=GEN_TWO_TYPES, run_id=run_id)
    post(
        api,
        [deal(i) for i in range(5)],
        generation=GEN_TWO_TYPES,
        run_id=run_id,
        entity_type="deal",
    )
    with owner_engine.connect() as conn:
        records_ok = conn.execute(
            text("SELECT records_ok FROM ingest_runs WHERE run_id = :r AND source_id = 'crm'"),
            {"r": run_id},
        ).scalar_one()
    assert records_ok == 8
    for entity_type, count in (("contact", 3), ("deal", 5)):
        view = reconcile(owner_engine, "crm", entity_type, GEN_TWO_TYPES)
        print("CONTROL " + view.row())
        assert view.loaded_count == count
        assert_views_agree(view)
