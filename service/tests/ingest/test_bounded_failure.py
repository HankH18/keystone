"""A failing source is bounded, structured, and marks its generation incomplete (R3).

Every timeout assertion here is a **wall-clock** assertion. "It eventually
returned" is not a timeout test -- an unbounded read also eventually returns, and
a test that only checks the exception type passes just as happily against code
with no bound at all. So each one measures elapsed time and requires it to sit
inside the bound it configured.

The four failure shapes are separate tests because they fail differently:

* a **hang** never produces a first record -- caught by the stall bound;
* a **slow drip** produces records forever, spaced *under* the stall bound, and
  is invisible to it -- only the load deadline stops it. This is the case a
  single-timeout implementation gets wrong;
* a **mid-stream exception** must not lose the records that already arrived, and
  must not surface as a 500;
* an **upstream 5xx** must keep its status; flattening it to "error" throws away
  the one fact an operator needs.

The last group is the one with teeth downstream: a partial load has to leave
`source_generations.complete = false`, because SS5.3 makes absence-style rules
skip on that flag. Marking a truncated load complete does not lose data, it
*invents conflicts* -- SS9.1 puts 875 enrollments in C7's raw population alone.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import text

from recon.adapters import (
    ADAPTER_STALL_TIMEOUT_SECONDS,
    AdapterError,
    FaultInjectingAdapter,
    read_bounded,
    stub_records,
)
from recon.db import ROLE_RECON_WRITER, role_connection
from recon.ingest import ingest_source


def _drain(adapter: FaultInjectingAdapter, **kwargs: float | None) -> tuple[list, float]:
    started = time.monotonic()
    records = list(read_bounded(adapter, 1, **kwargs))
    return records, time.monotonic() - started


def test_design_pins_the_stall_bound_at_ten_seconds() -> None:
    """DESIGN: "Timeouts bounded at 10s -> structured error"."""
    assert ADAPTER_STALL_TIMEOUT_SECONDS == 10.0


def test_a_hanging_source_is_cut_off_within_its_bound() -> None:
    adapter = FaultInjectingAdapter(source_id="crm", mode="hang")

    started = time.monotonic()
    with pytest.raises(AdapterError) as excinfo:
        _drain(adapter, stall_timeout=0.3, deadline_seconds=None)
    elapsed = time.monotonic() - started

    assert excinfo.value.kind == "source_timeout"
    assert excinfo.value.status == 504
    assert excinfo.value.latency_ms is not None
    assert 300 <= excinfo.value.latency_ms < 2000
    assert elapsed < 2.0, f"the read was not bounded: it took {elapsed:.2f}s"


def test_a_slow_drip_that_never_completes_is_stopped_by_the_load_deadline() -> None:
    """Gaps stay under the stall bound forever; only the total deadline ends it."""
    adapter = FaultInjectingAdapter(
        source_id="payments", mode="slow_drip", records=stub_records(3), gap_seconds=0.02
    )

    started = time.monotonic()
    with pytest.raises(AdapterError) as excinfo:
        _drain(adapter, stall_timeout=30.0, deadline_seconds=0.4)
    elapsed = time.monotonic() - started

    assert excinfo.value.kind == "source_timeout"
    assert "deadline" in excinfo.value.detail
    assert elapsed < 2.0, f"the drip was not bounded: it took {elapsed:.2f}s"
    assert adapter.records_handed_over > 0, "the drip really was producing records the whole time"


def test_a_midstream_exception_keeps_what_arrived_and_is_never_a_500() -> None:
    adapter = FaultInjectingAdapter(
        source_id="appdb", mode="midstream_error", records=stub_records(10), fail_after=4
    )

    collected = []
    with pytest.raises(AdapterError) as excinfo:
        for record in read_bounded(adapter, 1, stall_timeout=2.0, deadline_seconds=5.0):
            collected.append(record)

    assert len(collected) == 4, "records read before the failure are real and must survive"
    assert excinfo.value.kind == "source_error"
    assert excinfo.value.status < 600 and excinfo.value.status != 500
    assert excinfo.value.latency_ms is not None
    assert "RuntimeError" in excinfo.value.detail


def test_an_upstream_5xx_keeps_its_status_and_its_latency() -> None:
    adapter = FaultInjectingAdapter(
        source_id="crm",
        mode="http_5xx",
        records=stub_records(6),
        fail_after=2,
        upstream_status=503,
    )

    with pytest.raises(AdapterError) as excinfo:
        _drain(adapter, stall_timeout=2.0, deadline_seconds=5.0)

    error = excinfo.value
    assert error.kind == "source_unavailable"
    assert error.upstream_status == 503
    assert error.latency_ms is not None and error.latency_ms >= 0
    problem = error.problem()
    assert problem["upstream_status"] == 503
    assert problem["latency_ms"] == error.latency_ms
    assert {"type", "title", "status", "detail"} <= set(problem)


def test_a_healthy_stub_reads_to_completion() -> None:
    """The negative control: the harness is not simply always raising."""
    adapter = FaultInjectingAdapter(mode="ok", records=stub_records(7))
    records, elapsed = _drain(adapter, stall_timeout=2.0, deadline_seconds=5.0)
    assert len(records) == 7
    assert elapsed < 2.0


# ----------------------------------------------------------------------------------
# the downstream consequence: an incomplete generation is recorded as incomplete
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["midstream_error", "http_5xx"])
def test_a_partial_ingest_marks_the_generation_incomplete(owner_engine, mode: str) -> None:
    generation = 941 if mode == "midstream_error" else 942
    run_id = f"partial-{mode}"
    adapter = FaultInjectingAdapter(
        source_id="crm",
        mode=mode,
        records=stub_records(20, source_id="crm", entity_type="contact", generation=generation),
        available_generations=(generation,),
        fail_after=8,
    )
    adapter.entity_types = ("contact",)

    with role_connection(ROLE_RECON_WRITER) as connection:
        result = ingest_source(
            adapter,
            generation,
            run_id=run_id,
            conn=connection,
            stall_timeout=2.0,
            deadline_seconds=10.0,
        )

    assert result.error is not None
    assert result.status == "partial"
    assert result.records_ok == 8, "the records that arrived must still land"
    assert result.complete is False

    with owner_engine.connect() as connection:
        ledger = connection.execute(
            text(
                "SELECT loaded_count, complete, error_detail FROM source_generations "
                "WHERE source_id = 'crm' AND generation = :generation AND entity_type = 'contact'"
            ),
            {"generation": generation},
        ).one()
        landed = connection.execute(
            text("SELECT count(*) FROM raw_records WHERE generation = :generation"),
            {"generation": generation},
        ).scalar()
        run_row = connection.execute(
            text(
                "SELECT status, records_ok FROM ingest_runs "
                "WHERE run_id = :run_id AND source_id = 'crm'"
            ),
            {"run_id": run_id},
        ).one()

    assert ledger.loaded_count == 8
    assert ledger.complete is False, (
        "a truncated load reported as complete makes every absence rule fire (SS5.3)"
    )
    assert ledger.error_detail is not None and ledger.error_detail["status"] in (502, 504)
    assert landed == 8
    assert run_row.status == "partial"
    assert run_row.records_ok == 8


def test_a_source_that_never_answers_does_not_hang_the_sync(owner_engine) -> None:
    generation = 943
    adapter = FaultInjectingAdapter(
        source_id="crm", mode="hang", available_generations=(generation,)
    )
    adapter.entity_types = ("contact",)

    started = time.monotonic()
    with role_connection(ROLE_RECON_WRITER) as connection:
        result = ingest_source(
            adapter,
            generation,
            run_id="hang-sync",
            conn=connection,
            stall_timeout=0.3,
            deadline_seconds=1.0,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 3.0, f"the sync hung for {elapsed:.2f}s"
    assert result.status == "failed"
    assert result.error is not None and result.error.kind == "source_timeout"
    assert result.records_ok == 0

    with owner_engine.connect() as connection:
        complete = connection.execute(
            text(
                "SELECT complete FROM source_generations "
                "WHERE source_id = 'crm' AND generation = :generation"
            ),
            {"generation": generation},
        ).scalar()
    assert complete is False
