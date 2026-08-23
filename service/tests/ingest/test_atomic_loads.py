"""A load is atomic, idempotent, and materialized in full (R1, R2, R4).

Two data-corruption defects live here, and both were invisible from the outside:
every request answered 200 and every number the pipeline reported reconciled with
every other number the pipeline reported. Only the database disagreed.

**Concurrent posts sharing a `run_id` doubled the landing table.** The `already`
pre-check and the COPY were two statements in two transactions. Under READ
COMMITTED neither sees the other's uncommitted rows, so twelve concurrent
requests each read `already == 0`, each COPYed, and `raw_records` held twelve
copies of a 200-record slice. The landing table is append-only and is the mirror
the whole "read-only, unchanged by a run" guarantee rests on: doubling it
corrupts every downstream count, including the invariant engine's absence tests
(SS5.3 sizes C7's and C1's populations from a raw sweep). Measured before the
fix: 12 requests, 200 records each, `raw_records` held **1600** -- and four of the
twelve answered a bare 500 when the landing read-back disagreed with itself.

**A second slice silently wiped the first slice's staging rows.**
`_materialize` deleted `WHERE source_id = ... AND generation = ...` and then
re-inserted only the slice in hand -- a DELETE scoped to the generation and an
INSERT scoped to the load. Posting two slices into one generation therefore left
both in `raw_records`, only the second in `stg_*`, and the completeness ledger
reporting the generation `complete`. Every rule reads `stg_*` and trusts
`complete`, so the detector was handed a half dataset it believed was whole:
thousands of absence-rule false positives, with every count reconciling.
Measured before the fix: 100 landed, **50** staged, ledger `loaded_count = 50`,
`complete = true`.

The tests below are the property, not the call site. They run against the real
database, through the real HTTP endpoint and through real concurrent
`recon_writer` connections, and each one fails if either guard is removed:

* the advisory lock, so the check and the write are one decision;
* the replay no-op, so a load that already landed reports what is there instead
  of landing again;
* the slice-scoped DELETE, so materialization is additive per generation;
* `STAGING_INVARIANT`, asserted after every write from two measurements taken in
  the database -- the same shape as `read == landed + rejected`.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from recon.adapters import RawRecord, canonical_json, row_hash
from recon.app import create_app
from recon.db import ROLE_RECON_WRITER, role_connection
from recon.ingest import STAGING_INVARIANT, IngestStagingError, _land_records, slice_lock_key
from tests.ingest.conftest import TRIGGER_HEADERS

#: Generations this module owns. Inside the >= 900 range `conftest._purge`
#: removes, and outside the 901-903 the seed-tree modules ingest.
GEN_RACE = 971
GEN_SLICES = 972
GEN_DIRECT = 973
GEN_INVARIANT = 974

ALL_GENERATIONS = (GEN_RACE, GEN_SLICES, GEN_DIRECT, GEN_INVARIANT)


def contact(index: int) -> str:
    """One well-formed CRM contact payload, as the literal line the API takes."""
    return json.dumps(
        {
            "crm_id": f"CRM-ATOMIC-{index:06d}",
            "email": f"person{index}@example.invalid",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "lifecycle_stage": "lead",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        },
        sort_keys=True,
    )


def contact_record(index: int, generation: int) -> RawRecord:
    """The same payload as an already-validated `RawRecord`, for the direct path."""
    payload = json.loads(contact(index))
    payload_json = canonical_json(payload)
    return RawRecord(
        source_id="crm",
        entity_type="contact",
        natural_key=payload["crm_id"],
        generation=generation,
        payload=payload,
        row_hash=row_hash("crm", "contact", payload["crm_id"], payload_json),
        payload_json=payload_json,
    )


@pytest.fixture
def api(trigger_secret: str, owner_engine: Engine) -> Iterator[TestClient]:
    """The real app, with this module's generations cleared before and after.

    Cleared before as well as after: these tests count rows for a generation, so
    a leftover row from a previous failed run would make a broken build look
    fixed (or a fixed one look broken).
    """
    _clear(owner_engine)
    with TestClient(create_app()) as client:
        yield client
    _clear(owner_engine)


def _clear(engine: Engine) -> None:
    with engine.begin() as conn:
        for table in ("stg_crm_contact", "raw_records", "source_generations", "ingest_runs"):
            conn.execute(
                text(f"DELETE FROM {table} WHERE generation = ANY(:generations)"),
                {"generations": list(ALL_GENERATIONS)},
            )


def _post(
    client: TestClient,
    records: Sequence[str],
    *,
    generation: int,
    run_id: str,
) -> tuple[int, dict]:
    response = client.post(
        "/internal/ingest/records",
        json={
            "source": "crm",
            "entity_type": "contact",
            "generation": generation,
            "records": list(records),
            "run_id": run_id,
        },
        headers=TRIGGER_HEADERS,
    )
    return response.status_code, response.json()


def _counts(engine: Engine, generation: int) -> tuple[int, int]:
    with engine.connect() as conn:
        landed = conn.execute(
            text("SELECT count(*) FROM raw_records WHERE generation = :g"),
            {"g": generation},
        ).scalar_one()
        staged = conn.execute(
            text("SELECT count(*) FROM stg_crm_contact WHERE generation = :g"),
            {"g": generation},
        ).scalar_one()
    return int(landed), int(staged)


# ======================================================================================
# the race: concurrent posts sharing a run id
# ======================================================================================


def test_twelve_concurrent_posts_of_one_run_id_land_the_slice_exactly_once(
    api: TestClient, owner_engine: Engine
) -> None:
    """The measured defect, reproduced at the same width and then refuted.

    Twelve real threads, one `run_id`, 200 records. Before the fix this landed
    1600 rows and answered four bare 500s. Exactly one caller may land; every
    other must be told the load already exists, and `raw_records` must hold the
    slice once.
    """
    records = [contact(index) for index in range(200)]
    run_id = "atomic-race"
    outcomes: list[tuple[int, dict]] = []
    barrier = threading.Barrier(12)
    lock = threading.Lock()

    def fire() -> None:
        barrier.wait(timeout=60)
        result = _post(api, records, generation=GEN_RACE, run_id=run_id)
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=fire) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)
    assert not any(thread.is_alive() for thread in threads), "a request never returned"

    statuses = sorted(status for status, _ in outcomes)
    assert len(outcomes) == 12
    assert all(status < 500 for status in statuses), (
        f"a concurrent replay answered a 5xx: {statuses}. Losing a race is not a "
        "server fault; it is a load that already exists."
    )
    assert statuses.count(200) == 1, (
        f"{statuses.count(200)} callers were told they landed the slice; exactly "
        f"one may. Statuses: {statuses}"
    )
    assert statuses.count(409) == 11

    landed, staged = _counts(owner_engine, GEN_RACE)
    assert landed == 200, (
        f"the append-only landing table holds {landed} rows for a 200-record "
        "slice posted twelve times concurrently; the check and the write are not "
        "one decision"
    )
    assert staged == 200, f"staging holds {staged} rows for {landed} landed records"


def test_the_replay_reports_what_already_landed_and_writes_nothing(
    api: TestClient, owner_engine: Engine
) -> None:
    """A repeated load id is a no-op, and says how many records are already there."""
    records = [contact(index) for index in range(5)]
    first_status, first_body = _post(api, records, generation=GEN_RACE, run_id="atomic-replay")
    assert first_status == 200
    assert first_body["accepted"] == 5

    second_status, second_body = _post(api, records, generation=GEN_RACE, run_id="atomic-replay")
    assert second_status == 409
    assert second_body["type"].endswith("duplicate_load")
    assert second_body["already_landed"] == 5, (
        "a replay must report what already landed, not merely refuse"
    )
    assert second_body["persisted"] is False

    with owner_engine.connect() as conn:
        landed = conn.execute(
            text("SELECT count(*) FROM raw_records WHERE run_id = 'atomic-replay'")
        ).scalar_one()
    assert landed == 5, "the replay added rows"


def test_concurrent_direct_landings_of_one_load_write_the_slice_once() -> None:
    """The same property one layer down, on real `recon_writer` transactions.

    The HTTP test proves the endpoint. This proves the *function*, so a future
    caller that lands records without going through the endpoint inherits the
    guarantee rather than re-inventing it.
    """
    records = [contact_record(index, GEN_DIRECT) for index in range(50)]
    landed: list[int] = []
    replayed: list[bool] = []
    barrier = threading.Barrier(6)
    lock = threading.Lock()

    def land() -> None:
        barrier.wait(timeout=60)
        with role_connection(ROLE_RECON_WRITER) as conn:
            result = _land_records(
                conn,
                records,
                source_id="crm",
                entity_type="contact",
                generation=GEN_DIRECT,
                run_id="atomic-direct",
                persist=True,
                staged=True,
            )
        with lock:
            landed.append(result.landed)
            replayed.append(result.replayed)

    threads = [threading.Thread(target=land) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)

    assert len(landed) == 6, "a landing thread raised"
    assert replayed.count(False) == 1, (
        f"{replayed.count(False)} callers believed they wrote the load; exactly one did"
    )
    assert set(landed) == {50}, f"every caller must see 50 rows for the load, saw {landed}"


def test_the_lock_key_names_the_generation_slice() -> None:
    """The serialised unit is `(source, entity_type, generation)`.

    Which is the `(source, entity_type, generation, run_id)` load -- two requests
    sharing a run id necessarily share the slice -- **and** the two different run
    ids that write the same generation's staging slice and ledger row. Keying on
    the `load_id` alone would leave those unserialised.
    """
    assert slice_lock_key("crm", "contact", 972) == "keystone:ingest:crm:contact:g972"
    assert slice_lock_key("crm", "contact", 972) != slice_lock_key("crm", "deal", 972)
    assert slice_lock_key("crm", "contact", 972) != slice_lock_key("crm", "contact", 973)


# ======================================================================================
# slices: two posts, one generation
# ======================================================================================


def test_a_second_slice_does_not_wipe_the_first_slices_staging_rows(
    api: TestClient, owner_engine: Engine
) -> None:
    """The measured defect: 100 landed, 50 staged, ledger `complete = true`."""
    first = [contact(index) for index in range(0, 50)]
    second = [contact(index) for index in range(50, 100)]

    assert _post(api, first, generation=GEN_SLICES, run_id="slice-a")[0] == 200
    landed, staged = _counts(owner_engine, GEN_SLICES)
    assert (landed, staged) == (50, 50)

    assert _post(api, second, generation=GEN_SLICES, run_id="slice-b")[0] == 200
    landed, staged = _counts(owner_engine, GEN_SLICES)
    assert landed == 100
    assert staged == 100, (
        f"{landed} records landed and only {staged} are staged; every rule reads "
        "stg_* and the ledger says the generation is complete, so the detector "
        "is handed a truncated dataset it believes is whole"
    )

    with owner_engine.connect() as conn:
        keys = conn.execute(
            text("SELECT count(DISTINCT crm_id) FROM stg_crm_contact WHERE generation = :g"),
            {"g": GEN_SLICES},
        ).scalar_one()
    assert keys == 100, "both slices' natural keys must survive, not just the later one"


def test_the_ledger_counts_the_generation_not_the_last_slice(
    api: TestClient, owner_engine: Engine
) -> None:
    """`source_generations` is keyed on the generation, so it must describe it.

    Binding one slice's row count to a generation-keyed ledger is what made a
    100-record generation report `loaded_count = 50` while claiming `complete`.
    """
    _post(api, [contact(index) for index in range(0, 50)], generation=GEN_SLICES, run_id="led-a")
    _post(api, [contact(index) for index in range(50, 100)], generation=GEN_SLICES, run_id="led-b")

    with owner_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT loaded_count, rejected_count, complete FROM source_generations "
                "WHERE source_id = 'crm' AND entity_type = 'contact' AND generation = :g"
            ),
            {"g": GEN_SLICES},
        ).one()
        landed = conn.execute(
            text("SELECT count(*) FROM raw_records WHERE generation = :g"),
            {"g": GEN_SLICES},
        ).scalar_one()
    assert row.loaded_count == landed == 100, (
        f"the ledger reports {row.loaded_count} loaded for a generation holding {landed}"
    )


def test_re_asserting_a_record_replaces_its_staging_row_rather_than_doubling_it(
    api: TestClient, owner_engine: Engine
) -> None:
    """Additive across keys, replacing within a key -- both readings at once.

    `raw_records` is append-only and legitimately keeps both copies (that is the
    R4 history); `stg_*` is a derived cache (migration 0002) and holds the
    current materialization of each key. Doubling it would double every rule's
    population.
    """
    records = [contact(index) for index in range(10)]
    assert _post(api, records, generation=GEN_INVARIANT, run_id="reassert-a")[0] == 200
    assert _post(api, records, generation=GEN_INVARIANT, run_id="reassert-b")[0] == 200

    landed, staged = _counts(owner_engine, GEN_INVARIANT)
    assert landed == 20, "landing is append-only; both arrivals are history"
    assert staged == 10, "staging holds one row per landed natural key, not one per arrival"


# ======================================================================================
# the invariant itself
# ======================================================================================


def test_the_staging_invariant_is_stated_where_it_is_enforced() -> None:
    assert STAGING_INVARIANT == (
        "for every (source, entity_type, generation) with a stg_* table: "
        "count(stg_*) == count(DISTINCT natural_key in raw_records)"
    )


def test_a_materialization_that_drops_rows_is_refused_rather_than_reported(
    api: TestClient, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sabotage: restore the old generation-wide DELETE and the load must fail.

    This is the binding test. `_materialize` is replaced with the exact shape the
    defect had -- DELETE the whole generation, insert only this slice -- and the
    second slice must now be **refused**, loudly, instead of answering 200 over a
    half-empty staging table. Delete the `_check_staging` call and this test goes
    red.
    """
    import recon.ingest as ingest_module

    first = [contact(index) for index in range(0, 20)]
    second = [contact(index) for index in range(20, 40)]
    assert _post(api, first, generation=GEN_INVARIANT, run_id="sabotage-a")[0] == 200

    original = ingest_module._materialize

    def wipe_the_generation(conn, source_id, entity_type, generation, records, raw_ids, *, run_id):
        conn.execute(
            text('DELETE FROM "stg_crm_contact" WHERE source_id = :s AND generation = :g'),
            {"s": source_id, "g": generation},
        )
        return original(conn, source_id, entity_type, generation, records, raw_ids, run_id=run_id)

    monkeypatch.setattr(ingest_module, "_materialize", wipe_the_generation)

    status, body = _post(api, second, generation=GEN_INVARIANT, run_id="sabotage-b")
    assert status >= 500, (
        "a materialization that dropped the first slice answered "
        f"{status}; a truncated staging slice must never be reported as a load"
    )
    assert body["type"].endswith("accounting_violation")
    assert body["invariant"] == STAGING_INVARIANT

    landed, staged = _counts(owner_engine, GEN_INVARIANT)
    assert landed == 20, (
        f"the refused load left {landed} rows landed; a load that cannot be "
        "materialized must be rolled back, not half-committed"
    )
    assert staged == 20


def test_the_staging_check_raises_rather_than_logging(owner_engine: Engine) -> None:
    """`IngestStagingError`, and it is an `IngestAccountingError`.

    A log line about a truncated generation is not enforcement: SS5.3 reads the
    ledger, not the log, and a `complete` row over half a dataset is what fires
    every absence rule.
    """
    from recon.ingest import IngestAccountingError, _check_staging

    assert issubclass(IngestStagingError, IngestAccountingError)

    records = [contact_record(index, GEN_INVARIANT) for index in range(4)]
    with role_connection(ROLE_RECON_WRITER) as conn:
        _land_records(
            conn,
            records,
            source_id="crm",
            entity_type="contact",
            generation=GEN_INVARIANT,
            run_id="check-raises",
            persist=True,
            staged=True,
        )
        assert _check_staging(conn, "crm", "contact", GEN_INVARIANT) == 4
        conn.execute(
            text("DELETE FROM stg_crm_contact WHERE generation = :g AND run_id = 'check-raises'"),
            {"g": GEN_INVARIANT},
        )
        with pytest.raises(IngestStagingError) as excinfo:
            _check_staging(conn, "crm", "contact", GEN_INVARIANT)
    assert STAGING_INVARIANT in str(excinfo.value)
    assert "invisible to every rule" in str(excinfo.value)
