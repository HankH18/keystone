"""Each of the six benchmark gates, pushed past its threshold. Twice.

A benchmark that has only ever been observed passing is a number, not a gate. All
six rows in :mod:`recon.bench.suite` return a :class:`~recon.suite.checks
.CheckResult` precisely so a missed threshold is a red row and a non-zero exit --
and until this module existed, **not one of those six red branches had ever
run**. The scorecard's evidence for "the gates gate" was that they had never
fired, which is the same evidence a gate with the comparison inverted would
produce.

So every gate is driven twice here: once with a measurement over its threshold
(expect ``FAIL``), once with one under it (expect ``PASS``). Where the threshold
is ``EXACT`` rather than a number, "over" means one element of the exactness
vector wrong.

What is injected, and what is real
------------------------------------
Injected: the *measurement*. The real one needs the fully loaded graded database
-- 360,400 landed records plus a materialized identity layer, ~55s of
``POST /internal/sync`` before the first sample exists -- which is exactly why no
test drove these before. :mod:`tests.suite.benchfakes` supplies a real
:class:`~recon.suite.pipeline.PipelineRun` with injected clocks, a fake HTTP
client that charges a scripted number of seconds to a fake monotonic clock, a
:class:`~tests.suite.benchfakes.FakeMaterialize` that charges a scripted number of
seconds for the identity-layer cascade, and a real
:class:`~recon.suite.burst.BurstOutcome` with an injected vector.

Real: the *decision*. The warm-up discard, the nearest-rank
:func:`~recon.bench.suite.percentile`, the ``>=`` against the budget, the
rec/s arithmetic, the rollback-restored-the-mirror check, the exactness
predicates, and the :class:`CheckResult` each gate returns are all the shipped
code path, unmodified.

Also real: **the URL each gate names.** The fake client resolves every URL against
the route table ``recon.app.create_app()`` really serves and answers ``404`` to
anything else (:func:`tests.suite.benchfakes.is_served`), so a gate pointed at an
endpoint the service does not mount fails here exactly as it would in a real run.
Before that, ``_FakeClient.get`` ignored its ``url`` outright: repointing
``check_cross_source_query`` at ``/api/BROKEN/{key}`` and ``check_dashboard_api``
at ``/api/NOT-A-ROUTE`` left this whole module green.

**What this does NOT prove**: that the underlying system is fast. These tests say
nothing about the real p95, the real rec/s or the real pass duration -- they prove
only that a bad measurement is reported as ``FAIL`` and a good one as ``PASS``.
The real numbers come from `python -m recon.suite` against the loaded database,
and only from there.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest

from recon.bench import suite as bench
from recon.suite.burst import BurstOutcome
from recon.suite.checks import FAIL, PASS
from recon.suite.mirror import MirrorDigest
from tests.suite.benchfakes import (
    EMPTY_MIRROR,
    FakeClock,
    FakeMaterialize,
    Timings,
    conflict,
    fake_pipeline_run,
    fake_probe_client,
    is_served,
)

# ======================================================================================
# shared rigging
# ======================================================================================


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Replace the module's ``time`` handle so a request's cost is scripted.

    Patched on :mod:`recon.bench.suite` rather than on :mod:`time` itself: the
    gates read ``time.perf_counter`` / ``time.monotonic`` off the module they
    imported, so this is local to the code under test and cannot perturb pytest's
    own timing.
    """
    fake = FakeClock()
    monkeypatch.setattr(bench, "time", fake)
    return fake


def _latency_gate(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock, *, seconds_per_call: float
) -> list[Any]:
    """Point both latency gates at a client whose every call costs the same.

    Returns the list every fake client built during the gate is appended to, so a
    test can read back the URLs the gate actually issued rather than assuming it
    issued the right ones.
    """
    monkeypatch.setattr(bench, "pipeline", lambda: fake_pipeline_run(conflicts=()))
    keys = [{"person_key": f"p{index}"} for index in range(4)]
    monkeypatch.setattr(bench, "expected_views", lambda: keys)
    monkeypatch.setattr(bench, "admin_headers", dict)

    clients: list[Any] = []

    @contextmanager
    def probe() -> Iterator[Any]:
        with fake_probe_client(clock, seconds_per_call=[seconds_per_call]) as client:
            clients.append(client)
            yield client

    monkeypatch.setattr(bench, "probe_client", probe)
    return clients


# ======================================================================================
# 0. the rig itself -- the fake client must not agree with a URL the app 404s
# ======================================================================================


def test_the_fake_client_404s_a_url_the_real_app_does_not_serve(clock: FakeClock) -> None:
    """The hole this rig had: ``get`` ignored ``url`` and answered 200 to anything.

    Everything below is worthless if the stand-in agrees with any URL, because
    then no gate is bound to the endpoint it claims to measure. The three URLs the
    two latency gates issue must come back 200; the sabotage URLs a verifier
    substituted for them (``/api/BROKEN/{key}``, ``/api/NOT-A-ROUTE``,
    ``/api/ALSO-BROKEN``) must come back 404, and the scripted status must not be
    able to buy a 200 for a route that does not exist.
    """
    with fake_probe_client(clock, seconds_per_call=[0.01]) as client:
        assert client.get("/api/entities/p1").status_code == 200
        assert client.get("/api/scorecard").status_code == 200
        assert client.get("/api/conflicts", params={"type": "C1"}).status_code == 200

        assert client.get("/api/BROKEN/p1").status_code == 404
        assert client.get("/api/NOT-A-ROUTE").status_code == 404
        assert client.get("/api/ALSO-BROKEN").status_code == 404

    with fake_probe_client(clock, seconds_per_call=[0.01], status=200) as forced:
        assert forced.get("/api/NOT-A-ROUTE").status_code == 404


def test_a_404_still_costs_the_clock_and_reports_the_url_it_was_asked_for(
    clock: FakeClock,
) -> None:
    """A 404 is a round trip, and the failure branch renders the URL that got it."""
    with fake_probe_client(clock, seconds_per_call=[0.02]) as client:
        before = clock.now
        response = client.get("/api/NOT-A-ROUTE", params={"type": "C1", "page": 1})

    assert clock.now == pytest.approx(before + 0.02)
    assert response.request.url.path == "/api/NOT-A-ROUTE"
    assert response.request.url.query.decode() == "type=C1&page=1"


def test_both_latency_gates_only_call_urls_the_real_app_serves(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Named from the other side: every URL these two gates issue must be mounted.

    ``check_cross_source_query`` and ``check_dashboard_api`` exist to time real
    endpoints. If either names a path ``create_app()`` does not serve, the number
    it reports is the cost of a 404 -- so the URLs are enumerated from the run and
    each is checked against the application's own route table.
    """
    clients = _latency_gate(monkeypatch, clock, seconds_per_call=0.01)

    assert bench.check_cross_source_query().status == PASS
    assert bench.check_dashboard_api().status == PASS

    issued = sorted({url for client in clients for url in client.requested})
    assert issued, "neither gate issued a request, so this asserts nothing"
    unserved = [url for url in issued if not is_served(url)]
    assert not unserved, f"gates called URLs the real app does not serve: {unserved}"
    assert "/api/scorecard" in issued
    assert "/api/conflicts" in issued
    assert any(url.startswith("/api/entities/") for url in issued)


# ======================================================================================
# 1. bench:cross-source-query-p95   -- threshold p95 < 1.0s
# ======================================================================================


def test_cross_source_query_fails_over_the_1s_budget(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    _latency_gate(monkeypatch, clock, seconds_per_call=1.5)

    result = bench.check_cross_source_query()

    assert result.status == FAIL, result.detail
    assert result.name == "bench:cross-source-query-p95"
    assert "1500.0ms exceeds the 1s budget" in result.detail


def test_cross_source_query_passes_under_the_1s_budget(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    _latency_gate(monkeypatch, clock, seconds_per_call=0.05)

    result = bench.check_cross_source_query()

    assert result.status == PASS, result.detail
    assert "n=20" in result.detail and "p95=50.0ms" in result.detail


def test_cross_source_query_treats_exactly_the_budget_as_a_miss(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """The comparison is `>=`. A p95 of exactly 1.000s is not "under 1s"."""
    _latency_gate(monkeypatch, clock, seconds_per_call=bench.QUERY_P95_SECONDS)

    assert bench.check_cross_source_query().status == FAIL


def test_cross_source_query_refuses_an_empty_key_set(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """No golden keys means no query was timed, which is not a pass."""
    _latency_gate(monkeypatch, clock, seconds_per_call=0.01)
    monkeypatch.setattr(bench, "expected_views", list)

    result = bench.check_cross_source_query()

    assert result.status == FAIL
    assert "no golden expected-view keys" in result.detail


def test_cross_source_query_fails_on_a_non_200(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    _latency_gate(monkeypatch, clock, seconds_per_call=0.01)
    monkeypatch.setattr(
        bench,
        "probe_client",
        lambda: fake_probe_client(clock, seconds_per_call=[0.01], status=500),
    )

    result = bench.check_cross_source_query()

    assert result.status == FAIL
    assert "answered 500" in result.detail


# ======================================================================================
# 2. bench:detect-persist-reconcile   -- threshold total < 30s
# ======================================================================================


def _timed_run(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    timings: Timings,
    *,
    materialize_seconds: float = 14.09,
) -> FakeMaterialize:
    """The real row over an injected pipeline clock and an injected cascade clock."""
    monkeypatch.setattr(
        bench,
        "pipeline",
        lambda: fake_pipeline_run(
            conflicts=[conflict(index) for index in range(3)], timings=timings
        ),
    )
    stub = FakeMaterialize(clock, materialize_seconds)
    monkeypatch.setattr(bench, "materialize", stub)
    return stub


def test_detect_persist_reconcile_fails_over_30s(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    _timed_run(monkeypatch, clock, Timings(invariants=20.0, persist=5.0, reconcile=6.0))

    result = bench.check_detect_persist_reconcile()

    assert result.status == FAIL, result.detail
    assert result.name == "bench:detect-persist-reconcile"
    assert "the three stages took 31.00s" in result.detail


def test_detect_persist_reconcile_passes_under_30s(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    _timed_run(monkeypatch, clock, Timings(invariants=12.04, persist=2.78, reconcile=8.52))

    result = bench.check_detect_persist_reconcile()

    assert result.status == PASS, result.detail
    assert "23.34s total" in result.detail


def test_detect_persist_reconcile_treats_exactly_30s_as_a_miss(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    _timed_run(monkeypatch, clock, Timings(invariants=15.0, persist=5.0, reconcile=10.0))

    assert bench.check_detect_persist_reconcile().status == FAIL


def test_the_row_states_its_own_exclusion_in_both_directions(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """The materialization exclusion is in the DETAIL, not in a harness footnote.

    A reader of one scorecard row must be able to see that this clock is not the
    end-to-end pass, and must see it whether the row is green or red -- a caveat
    that only appears on failure is a caveat nobody reads.
    """
    for timings in (
        Timings(invariants=12.04, persist=2.78, reconcile=8.52),
        Timings(invariants=20.0, persist=5.0, reconcile=6.0),
    ):
        _timed_run(monkeypatch, clock, timings)
        detail = bench.check_detect_persist_reconcile().detail
        assert "EXCLUDES MATERIALIZATION" in detail
        assert "timed IN THIS RUN at 14.09s" in detail
        assert "does NOT fit 30s" in detail


def test_the_exclusion_number_is_measured_in_the_run_that_prints_it(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """The number the row calls "measured" moves when the system it measures moves.

    This replaces ``test_materialization_does_not_fit_inside_the_budget``, which
    asserted ``MATERIALIZE_SECONDS_MEASURED > DETECT_RECONCILE_SECONDS`` -- two
    module constants, 31.24 and 30.0. It compared two literals, so it observed
    nothing: materialization could have become 2s or ten minutes and it stayed
    green, while its docstring called itself "the measured fact the rename rests
    on, pinned so it cannot drift silently". The constant is gone; the row calls
    :func:`recon.bench.suite.measure_materialize_floor` and prints what came back,
    and *that* is what is pinned here -- two different cascade costs must produce
    two different numbers in the detail, and both must be the ones measured.
    """
    stages = Timings(invariants=12.04, persist=2.78, reconcile=8.52)  # 23.34s total

    _timed_run(monkeypatch, clock, stages, materialize_seconds=14.09)
    quick = bench.check_detect_persist_reconcile().detail

    _timed_run(monkeypatch, clock, stages, materialize_seconds=62.5)
    slow = bench.check_detect_persist_reconcile().detail

    assert "timed IN THIS RUN at 14.09s" in quick
    assert ">=37.43s and does NOT fit 30s" in quick

    assert "timed IN THIS RUN at 62.50s" in slow
    assert ">=85.84s and does NOT fit 30s" in slow

    # The counts come off the report the call returned, not off a literal.
    for detail in (quick, slow):
        assert "built 43375 entities / 120000 links / 1712775 lineage rows" in detail


def test_the_row_will_not_claim_an_exclusion_its_own_floor_does_not_establish(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """A floor under the budget proves nothing, and the row has to say so.

    The old constant made "does NOT fit 30s" unconditional, which is how a false
    claim survived: it was printed whatever the system did. Here the three stages
    take 23.34s and the measured cascade 1.5s, so the end-to-end floor is 24.84s
    -- under the budget. The row must report the number and withhold the verdict.
    """
    _timed_run(
        monkeypatch,
        clock,
        Timings(invariants=12.04, persist=2.78, reconcile=8.52),
        materialize_seconds=1.5,
    )

    result = bench.check_detect_persist_reconcile()

    assert result.status == PASS, result.detail
    assert "EXCLUDES MATERIALIZATION" in result.detail
    assert "timed IN THIS RUN at 1.50s" in result.detail
    assert "does NOT fit 30s" not in result.detail
    assert ">=24.84s, which this run's floor does NOT put over the 30s budget" in result.detail
    assert "stated, not established by measurement" in result.detail


def test_the_cascade_is_timed_without_writing_anything(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """The row's `wrote none of them` is a safety claim about a loaded database.

    The row runs :func:`recon.resolve.materialize` against the same database the
    other nine rows are reading. ``persist=False`` is the only reason that is safe
    -- with ``persist=True`` it would either raise (the identity layer is
    append-only to ``recon_writer``, migration 0004) or duplicate the layer. So
    the keyword is asserted, once per run, rather than described in a docstring.
    """
    stub = _timed_run(monkeypatch, clock, Timings())

    assert bench.check_detect_persist_reconcile().status == PASS
    assert stub.calls == [{"persist": False}]


# ======================================================================================
# 3. bench:ingestion-rps   -- threshold >= 500 rec/s
# ======================================================================================


class _FakeAdapter:
    def __init__(self, generations: Sequence[int]) -> None:
        self._generations = tuple(generations)

    def generations(self) -> tuple[int, ...]:
        return self._generations


class _FakeTransaction:
    def __init__(self) -> None:
        self.rolled_back = False

    def rollback(self) -> None:
        self.rolled_back = True


class _FakeConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.transaction = _FakeTransaction()

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))

    def begin(self) -> _FakeTransaction:
        return self.transaction

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _FakeEngine:
    def __init__(self) -> None:
        self.connections: list[_FakeConnection] = []

    def connect(self) -> _FakeConnection:
        conn = _FakeConnection()
        self.connections.append(conn)
        return conn


class _FakeReport:
    def __init__(self, records_ok: int) -> None:
        self.records_ok = records_ok


def _ingestion_gate(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    *,
    seconds_per_generation: float,
    records_per_generation: int = 120_000,
    generations: Sequence[int] = (1, 2, 3),
    digest_after: MirrorDigest = EMPTY_MIRROR,
) -> _FakeEngine:
    """The real ingestion gate over a fake ingest that costs a scripted duration."""
    monkeypatch.setattr(bench, "pipeline", lambda: fake_pipeline_run(conflicts=()))
    monkeypatch.setattr(bench, "default_fixtures_root", lambda: "/nonexistent")
    monkeypatch.setattr(bench, "build_adapters", lambda _root: {"appdb": _FakeAdapter(generations)})
    monkeypatch.setattr(bench, "expected_counts_from_manifest", lambda _root: {})

    engine = _FakeEngine()
    monkeypatch.setattr(bench, "get_engine", lambda: engine)

    def fake_ingest(*_args: Any, **_kwargs: Any) -> _FakeReport:
        clock.advance(seconds_per_generation)
        return _FakeReport(records_per_generation)

    monkeypatch.setattr(bench, "ingest_generation", fake_ingest)
    # Imported inside the function, so it is patched where it is defined.
    monkeypatch.setattr("recon.suite.mirror.mirror_digest", lambda _conn: digest_after)
    return engine


def test_ingestion_fails_below_the_500_rec_per_second_floor(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """120,000 records in 600s is 200 rec/s -- a rate that must be reported red."""
    _ingestion_gate(monkeypatch, clock, seconds_per_generation=600.0)

    result = bench.check_ingestion()

    assert result.status == FAIL, result.detail
    assert result.name == "bench:ingestion-rps"
    assert "200 rec/s is below the 500 rec/s floor" in result.detail


def test_ingestion_passes_above_the_floor(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    _ingestion_gate(monkeypatch, clock, seconds_per_generation=8.0)

    result = bench.check_ingestion()

    assert result.status == PASS, result.detail
    assert "15000 rec/s sustained over 240000 records" in result.detail
    assert "mirror restored=True" in result.detail


def test_ingestion_fails_when_the_rollback_did_not_restore_the_mirror(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """A fast rate bought by destroying the mirror is not a pass."""
    _ingestion_gate(
        monkeypatch,
        clock,
        seconds_per_generation=8.0,
        digest_after=MirrorDigest(digests={"raw_records": "deadbeef"}, row_counts={}),
    )

    result = bench.check_ingestion()

    assert result.status == FAIL, result.detail
    assert "rollback did NOT restore the mirror" in result.detail


def test_ingestion_refuses_a_single_generation(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """With nothing to discard, the "sustained" rate would be a cold-start rate."""
    _ingestion_gate(monkeypatch, clock, seconds_per_generation=8.0, generations=(1,))

    result = bench.check_ingestion()

    assert result.status == FAIL
    assert "no warm-up to discard" in result.detail


def test_ingestion_refuses_a_rate_measured_over_zero_records(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    _ingestion_gate(monkeypatch, clock, seconds_per_generation=8.0, records_per_generation=0)

    result = bench.check_ingestion()

    assert result.status == FAIL
    assert "zero records were ingested" in result.detail


def test_ingestion_measures_inside_a_transaction_it_rolls_back(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """The benchmark's own safety claim, asserted rather than trusted."""
    engine = _ingestion_gate(monkeypatch, clock, seconds_per_generation=8.0)

    assert bench.check_ingestion().status == PASS
    measuring = engine.connections[0]
    assert measuring.transaction.rolled_back
    assert any("TRUNCATE" in statement for statement in measuring.statements)


# ======================================================================================
# 4. bench:conflict-accuracy   -- threshold EXACT
# ======================================================================================


def _golden_entry(index: int) -> dict[str, Any]:
    detected = conflict(index)
    return {
        "type": detected.type,
        "entity_refs": list(detected.entity_refs),
        "sources_involved": list(detected.sources_involved),
        "disagreeing_fields": list(detected.disagreeing_fields),
        "observed_values": dict(detected.observed_values),
        "expected_verdict": detected.expected_verdict,
    }


def _accuracy_gate(
    monkeypatch: pytest.MonkeyPatch, *, golden: int, detected: int, clean_refs: Sequence[str]
) -> None:
    """Inject the golden tree; the real graders in `recon.invariants.grading` run."""
    monkeypatch.setattr(
        "recon.invariants.grading.load_golden",
        lambda *_, **__: [_golden_entry(index) for index in range(golden)],
    )
    monkeypatch.setattr(
        "recon.invariants.grading.load_clean_sample",
        lambda *_, **__: [{"identity_refs": [ref]} for ref in clean_refs],
    )
    monkeypatch.setattr(
        bench,
        "pipeline",
        lambda: fake_pipeline_run(conflicts=[conflict(index) for index in range(detected)]),
    )


def test_conflict_accuracy_fails_on_a_single_false_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _accuracy_gate(monkeypatch, golden=10, detected=9, clean_refs=["appdb:person:999"])

    result = bench.check_conflict_accuracy()

    assert result.status == FAIL, result.detail
    assert result.name == "bench:conflict-accuracy"
    assert "detection is not exact" in result.detail
    assert "FN=1" in result.detail


def test_conflict_accuracy_fails_on_a_single_false_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _accuracy_gate(monkeypatch, golden=9, detected=10, clean_refs=["appdb:person:999"])

    result = bench.check_conflict_accuracy()

    assert result.status == FAIL, result.detail
    assert "FP=1" in result.detail


def test_conflict_accuracy_fails_when_a_clean_entity_is_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact on the golden set is not enough: SS8's intersection probe is separate."""
    _accuracy_gate(monkeypatch, golden=10, detected=10, clean_refs=["appdb:person:3"])

    result = bench.check_conflict_accuracy()

    assert result.status == FAIL, result.detail
    assert "FN=0 FP=0" in result.detail
    assert "0/1 unflagged" in result.detail


def test_conflict_accuracy_fails_on_an_empty_golden_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0 FN and 0 FP over 0 entries is vacuous, and this row already knows it."""
    _accuracy_gate(monkeypatch, golden=0, detected=0, clean_refs=["appdb:person:999"])

    assert bench.check_conflict_accuracy().status == FAIL


def test_conflict_accuracy_passes_when_it_is_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    _accuracy_gate(monkeypatch, golden=10, detected=10, clean_refs=["appdb:person:999"])

    result = bench.check_conflict_accuracy()

    assert result.status == PASS, result.detail
    assert "precision 1.000000 recall 1.000000 on 10 golden entries" in result.detail


# ======================================================================================
# 5. bench:spend-cap-exact   -- threshold EXACT
# ======================================================================================


def _exact_outcome() -> BurstOutcome:
    """The vector `docs/scorecard.txt` reports when the cap holds exactly."""
    return BurstOutcome(
        contenders=120,
        admitted_expected=6,
        cap_microusd=81_600,
        reserve_each=13_600,
        actual_each=1_797,
        granted=6,
        refused=114,
        spend_while_open=81_600,
        final_spend=1_797 * 6,
        ledger_violations=0,
        over_admitted=False,
        failures=[],
    )


@contextmanager
def _burst(monkeypatch: pytest.MonkeyPatch, outcome: BurstOutcome) -> Iterator[None]:
    monkeypatch.setattr(bench, "burst_outcome", lambda: outcome)
    yield


def test_spend_cap_passes_on_the_exact_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    with _burst(monkeypatch, _exact_outcome()):
        result = bench.check_spend_cap()

    assert result.status == PASS, result.detail
    assert result.name == "bench:spend-cap-exact"
    assert "granted=6/6 of 120 contenders" in result.detail


def test_spend_cap_fails_when_one_extra_call_is_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure the cap exists against: one call over the line."""
    over = replace(
        _exact_outcome(),
        granted=7,
        refused=113,
        final_spend=1_797 * 7,
        over_admitted=True,
        failures=["7 calls were granted against an admitted_expected of 6"],
    )
    with _burst(monkeypatch, over):
        result = bench.check_spend_cap()

    assert result.status == FAIL, result.detail
    assert "7 calls were granted" in result.detail
    assert "over-admitted=True" in result.detail


def test_spend_cap_fails_when_the_settled_spend_does_not_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ok` alone is not the gate: the vector is re-derived from the numbers."""
    drifted = replace(_exact_outcome(), final_spend=1_797 * 6 + 1)
    with _burst(monkeypatch, drifted):
        result = bench.check_spend_cap()

    assert result.status == FAIL, result.detail
    assert "the observed vector is not exact" in result.detail


def test_spend_cap_fails_on_a_ledger_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    violated = replace(_exact_outcome(), ledger_violations=1)
    with _burst(monkeypatch, violated):
        assert bench.check_spend_cap().status == FAIL


# ======================================================================================
# 6. bench:dashboard-api-p95   -- threshold p95 < 1.0s
# ======================================================================================


def test_dashboard_api_fails_over_the_1s_budget(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """Fifteen calls at 80ms is 1.2s per Overview load, over the budget."""
    _latency_gate(monkeypatch, clock, seconds_per_call=0.08)

    result = bench.check_dashboard_api()

    assert result.status == FAIL, result.detail
    assert result.name == "bench:dashboard-api-p95"
    assert "1200.0ms exceeds the 1s budget" in result.detail


def test_dashboard_api_passes_under_the_1s_budget(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    _latency_gate(monkeypatch, clock, seconds_per_call=0.01)

    result = bench.check_dashboard_api()

    assert result.status == PASS, result.detail
    assert "p95=150.0ms" in result.detail
    assert "15 server calls per Overview load" in result.detail


def test_dashboard_api_row_says_it_is_service_side_only(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    """The name no longer claims a page load; the detail must still say why."""
    _latency_gate(monkeypatch, clock, seconds_per_call=0.01)

    detail = bench.check_dashboard_api().detail

    assert "SERVICE SIDE ONLY" in detail
    assert "no browser" in detail
    assert "a floor on a page load, not a page load" in detail


def test_dashboard_api_fails_when_an_overview_call_does_not_answer_200(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    _latency_gate(monkeypatch, clock, seconds_per_call=0.01)
    monkeypatch.setattr(
        bench,
        "probe_client",
        lambda: fake_probe_client(clock, seconds_per_call=[0.01], status=503),
    )

    result = bench.check_dashboard_api()

    assert result.status == FAIL
    assert "did not all answer 200" in result.detail


# ======================================================================================
# the registry, so a gate cannot be proven and then quietly unregistered
# ======================================================================================


def test_every_registered_benchmark_has_a_gate_test_here() -> None:
    """Six rows, six gates, and this file drives every one of them.

    Guards the failure mode this module exists against: a seventh row added with
    an unexercised red branch, or one of the six renamed and left untested.
    """
    driven = {
        "bench:cross-source-query-p95",
        "bench:detect-persist-reconcile",
        "bench:ingestion-rps",
        "bench:conflict-accuracy",
        "bench:spend-cap-exact",
        "bench:dashboard-api-p95",
    }

    assert set(bench.BENCHMARKS) == driven, sorted(set(bench.BENCHMARKS) ^ driven)


def test_the_printed_suite_notes_name_only_registered_benchmark_rows() -> None:
    """A row name in the generated report has to be a row that exists.

    :data:`recon.suite.__main__.SUITE_NOTES` is printed under every scorecard and
    written into ``docs/scorecard.txt``, so a stale name there is a false
    statement in the graded artifact -- and it survived a rename precisely because
    nothing read it: a real run printed ``bench:dashboard-api-p95`` as a row and,
    three lines below, a note about ``bench:dashboard-load-p95``, a row the
    scorecard did not contain. Docstrings and comments may recount the old names
    as history; the report may not.
    """
    from recon.suite.__main__ import SUITE_NOTES

    mentioned = {name for note in SUITE_NOTES for name in re.findall(r"bench:[a-z0-9-]+", note)}
    assert mentioned, "no note names a benchmark row, so this asserts nothing"
    unknown = sorted(mentioned - set(bench.BENCHMARKS))
    assert not unknown, (
        f"the scorecard's own notes name benchmark row(s) that are not registered: "
        f"{unknown}. Registered: {sorted(bench.BENCHMARKS)}"
    )
