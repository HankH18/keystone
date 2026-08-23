"""A whole sync is time-bounded, not just each load in it (R3).

R3: "WHEN a source times out or 5xxs, THE SYSTEM SHALL bound the retry/timeout,
emit a structured error with status + latency, and never hang a sync."

`ADAPTER_LOAD_DEADLINE_SECONDS` (300s) bounds **one** `(source, generation)` load,
and `ingest_all` runs nine of them back to back. Nine wedged loads is ~45 minutes
before a caller hears anything -- every per-load bound satisfied, the useful
reading of "never hang a sync" violated. `SYNC_BUDGET_SECONDS` is the cumulative
bound over the sequence.

What the budget must *not* do is skip the remaining sources: dropping them from
the loop would return an `IngestReport` with fewer sources than there are
adapters, which is a silent skip with a stopwatch. So a load past the budget is
given a zero deadline and fails through the ordinary path -- its own
`SourceResult`, its own `source_timeout` with a latency, its own ledger row with
`complete = false`.

The negative control matters as much as the timing assertion: a healthy tree under
a generous budget still ingests everything, or "fast" would just mean "broken".
"""

from __future__ import annotations

import time

from recon.adapters import FaultInjectingAdapter, build_adapters, stub_records
from recon.ingest import SYNC_BUDGET_SECONDS, expected_counts_from_manifest, ingest_all

GENERATIONS = (961, 962, 963)


def _wedged_adapters() -> dict[str, FaultInjectingAdapter]:
    """Three sources that drip forever, over three generations.

    `slow_drip`, not `hang`, and deliberately: a source that stops is caught by the
    *stall* bound in well under a second, so a hanging tree would return quickly
    even with no budget at all and the test would prove nothing. A source that
    hands over a record every 50ms never trips a stall bound -- only the per-load
    deadline stops it, and only the cumulative budget stops the sequence of them.
    """
    return {
        source_id: FaultInjectingAdapter(
            source_id=source_id,
            mode="slow_drip",
            records=stub_records(4, source_id=source_id, entity_type="contact", generation=961),
            available_generations=GENERATIONS,
            gap_seconds=0.05,
        )
        for source_id in ("appdb", "crm", "payments")
    }


def test_the_default_budget_is_smaller_than_running_every_load_to_its_own_deadline() -> None:
    """Nine loads x 300s is 45 minutes; the budget has to be the smaller number."""
    from recon.adapters import ADAPTER_LOAD_DEADLINE_SECONDS

    assert SYNC_BUDGET_SECONDS < 9 * ADAPTER_LOAD_DEADLINE_SECONDS


def test_a_wedged_tree_returns_inside_the_sync_budget() -> None:
    """Nine hanging loads, one budget: the sync returns in about the budget, once."""
    adapters = _wedged_adapters()
    budget = 1.0

    started = time.monotonic()
    report = ingest_all(
        adapters,
        GENERATIONS,
        run_id="budget-wedged",
        persist=False,
        stall_timeout=2.0,
        deadline_seconds=5.0,
        sync_budget_seconds=budget,
    )
    elapsed = time.monotonic() - started

    # Without the cumulative budget this is 9 loads x a 5s per-load deadline = 45s,
    # every per-load bound respected.
    assert elapsed < 5.0, f"the sync took {elapsed:.2f}s against a {budget:g}s budget"
    assert elapsed >= budget * 0.5, "the budget must actually be spent, not short-circuited"

    assert len(report.sources) == len(adapters) * len(GENERATIONS), (
        "every (source, generation) must still be reported; dropping the ones past "
        "the budget is a silent skip wearing a timeout's clothes"
    )
    for source in report.sources:
        assert source.status in {"failed", "partial"}
        assert source.error is not None
        assert source.error.kind == "source_timeout"
        assert source.error.status == 504
        assert source.error.latency_ms is not None
        assert source.complete is False
    assert report.degraded is True


def test_the_budget_leaves_a_healthy_sync_alone(seed_tree, owner_engine) -> None:
    """The negative control: a real tree under a real budget ingests completely."""
    adapters = build_adapters(seed_tree.root)
    expected = expected_counts_from_manifest(seed_tree.root)

    report = ingest_all(
        adapters,
        seed_tree.generations,
        run_id="budget-healthy",
        root=seed_tree.root,
        persist=False,
        sync_budget_seconds=SYNC_BUDGET_SECONDS,
    )

    assert len(report.sources) == 3 * len(seed_tree.generations)
    assert report.records_ok > 0
    assert report.records_rejected == 0
    for source in report.sources:
        assert source.error is None
        for load in source.loads:
            load.check()
            assert load.loaded == expected[(load.source_id, load.entity_type, load.generation)]


def test_a_disabled_budget_is_spelled_none() -> None:
    """The benchmark measures throughput and must not be racing a sync budget."""
    adapters = {
        "crm": FaultInjectingAdapter(
            source_id="crm",
            mode="ok",
            records=stub_records(3, source_id="crm", entity_type="contact", generation=964),
            available_generations=(964,),
        )
    }
    adapters["crm"].entity_types = ("contact",)

    report = ingest_all(
        adapters,
        (964,),
        run_id="budget-none",
        persist=False,
        sync_budget_seconds=None,
    )
    assert report.records_ok == 3
