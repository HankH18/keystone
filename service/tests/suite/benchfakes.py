"""Injectable stand-ins for the six benchmark gates' inputs.

Not a test module (no ``test_`` prefix, so pytest does not collect it): it is the
rig ``test_bench_gates.py`` and ``test_golden_floor.py`` drive the real check
functions with.

What is faked, and what is emphatically not
--------------------------------------------
The six gates in :mod:`recon.bench.suite` each read a measurement and then
**decide**. The measurement needs the fully loaded graded database -- 360,400
landed records and a materialized identity layer, ~55s of ``POST /internal/sync``
before the first row can be produced -- which is why no test has ever driven
them, and why none of them had a test proving it can return ``FAILED``.

So the *measurement* is injected here and the *decision* is the real code path:
the real :func:`recon.bench.suite.check_cross_source_query` loop, the real
:func:`recon.bench.suite.percentile`, the real threshold comparison, the real
:class:`~recon.suite.checks.CheckResult`. A gate that cannot be made to fail is
not a gate, and the only way to find out is to hand it a sample that should fail
it.

:class:`FakeClock` is the reason this can run in milliseconds rather than in
40 seconds of real sleeping: the latency loops read
``recon.bench.suite.time.perf_counter``, so replacing that module attribute with
a clock the fake HTTP client advances gives a *scripted* per-request latency
without any wall time passing. The arithmetic under test -- warm-up discarded,
nearest-rank p95, ``>=`` against the budget -- is untouched.

The fake client answers the URL it was given
---------------------------------------------
:class:`_FakeClient` used to ignore its ``url`` argument outright and hand back a
200 for anything. That made both latency gates unfalsifiable in the one dimension
that matters most about them: pointing ``check_cross_source_query`` at
``/api/BROKEN/{key}``, or ``check_dashboard_api`` at ``/api/NOT-A-ROUTE``, left
the suite fully green while the real path would have 404'd every request and
turned both rows red. A stand-in that agrees with any URL is a stand-in that
proves the gate runs, not that the gate measures the endpoint it names.

So the route table here is read off the **real application** --
``recon.app.create_app().openapi()["paths"]``, the same document a client is
offered -- and a URL that matches none of those templates comes back ``404``, the
status the mounted app really answers. It is not a list kept in this file: a list
would be a second spelling of the route table, free to drift from it, and the
drift would restore exactly the hole this closes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from recon.invariants.runner import DetectedConflict, InvariantRun
from recon.reconciler import ReconcileReport
from recon.resolve import MaterializeReport
from recon.suite.mirror import MirrorDigest
from recon.suite.pipeline import PipelineRun, Precondition

__all__ = [
    "EMPTY_MIRROR",
    "MATERIALIZE_REPORT",
    "FakeClock",
    "FakeMaterialize",
    "FakeResponse",
    "Timings",
    "conflict",
    "fake_pipeline_run",
    "fake_probe_client",
    "invariant_run",
    "is_served",
    "served_route_matchers",
]

#: Two of these compare equal, so ``changed_tables`` reports nothing changed --
#: which is the "the rollback restored the mirror" half of the ingestion gate.
EMPTY_MIRROR = MirrorDigest(digests={}, row_counts={})

#: The full profile's real landing shape, so a detail string built from it
#: carries the number the scorecard carries rather than a round one.
LANDING: Mapping[str, int] = {
    "appdb.person@gen3": 47_000,
    "crm.contact@gen3": 43_000,
    "billing.invoice@gen3": 30_000,
}


class FakeClock:
    """A ``time`` stand-in whose only movement is what a fake request costs.

    Exposes the two names :mod:`recon.bench.suite` reads off the module --
    ``perf_counter`` (the latency loops) and ``monotonic`` (the ingestion loop) --
    from one cursor, so a test scripts elapsed seconds by advancing it.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def perf_counter(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _matcher(template: str) -> re.Pattern[str]:
    """One OpenAPI path template as a matcher: ``/api/entities/{key}`` -> one segment."""
    parts = re.split(r"(\{[^/}]+\})", template)
    body = "".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts)
    return re.compile(f"^{body}$")


@lru_cache(maxsize=1)
def served_route_matchers() -> tuple[re.Pattern[str], ...]:
    """Every path the **real** application serves, compiled to a matcher.

    Built from ``create_app().openapi()["paths"]`` rather than from
    ``app.routes``: FastAPI wraps each ``include_router`` call in a container
    whose own ``path`` is ``None``, so a flat walk of ``app.routes`` returns four
    docs paths and no API path at all -- an enumeration that would make every
    check below vacuous in the quiet direction (``tests/incidents
    /test_api_contract.py`` records that exact bug). Cached because building the
    app costs about half a second and the route table cannot change mid-run.
    """
    from recon.app import create_app

    return tuple(_matcher(template) for template in sorted(create_app().openapi()["paths"]))


def is_served(url: str) -> bool:
    """Does the real application serve this URL's path?"""
    path = url.split("?", 1)[0]
    return any(matcher.match(path) for matcher in served_route_matchers())


@dataclass
class _FakeURL:
    """``httpx.URL``'s two attributes the non-200 branch renders."""

    path: str = "/api/conflicts"
    query: bytes = b"type=C1&page=1&page_size=1"


@dataclass
class _FakeRequest:
    url: _FakeURL = field(default_factory=_FakeURL)


@dataclass
class FakeResponse:
    """Only what the gates actually read off a response.

    ``request`` is populated because the *failure* branch renders
    ``response.request.url.path`` and ``.query.decode()`` -- a stand-in that
    omitted it would make the red branch untestable, which is the exact hole this
    rig exists to close.
    """

    status_code: int = 200
    request: _FakeRequest = field(default_factory=_FakeRequest)


class _FakeClient:
    """A ``TestClient`` stand-in that charges the clock instead of the network.

    It charges for a 404 too, because a real one costs a round trip: the gate's
    own non-200 branch is what has to notice, and it must notice from the status,
    not from a request that mysteriously took no time.
    """

    def __init__(self, clock: FakeClock, seconds_per_call: Sequence[float], status: int) -> None:
        self._clock = clock
        self._plan = list(seconds_per_call)
        self._status = status
        self.calls = 0
        #: Every URL asked for, in order, so a test can assert which endpoints a
        #: gate actually names rather than trusting that it named the right ones.
        self.requested: list[str] = []

    def get(self, url: str, *, params: Mapping[str, Any] | None = None, **_: Any) -> FakeResponse:
        cost = self._plan[min(self.calls, len(self._plan) - 1)]
        self.calls += 1
        self.requested.append(url)
        self._clock.advance(cost)
        path, _, inline_query = url.partition("?")
        query = urlencode(dict(params)) if params else inline_query
        request = _FakeRequest(_FakeURL(path=path, query=query.encode()))
        if not is_served(path):
            # What the mounted application answers for a path it does not serve.
            # The scripted `status` is deliberately NOT honoured here: a gate
            # pointed at a route that does not exist must not be able to buy a
            # 200 from the rig.
            return FakeResponse(status_code=404, request=request)
        return FakeResponse(status_code=self._status, request=request)


@contextmanager
def fake_probe_client(
    clock: FakeClock, *, seconds_per_call: Sequence[float], status: int = 200
) -> Iterator[_FakeClient]:
    """``probe_client()`` stand-in: every ``.get`` costs a scripted number of seconds."""
    yield _FakeClient(clock, seconds_per_call, status)


def conflict(index: int, conflict_type: str = "C1") -> DetectedConflict:
    """One detected conflict, distinct by ref so a set of them does not collapse."""
    return DetectedConflict(
        type=conflict_type,
        rule_id="001_synthetic",
        entity_refs=(f"appdb:person:{index}",),
        sources_involved=("appdb", "crm"),
        disagreeing_fields=("appdb.person.email",),
        observed_values={"appdb.person.email": "a@example.invalid"},
        fingerprint=f"{index:064d}",
    )


def invariant_run(conflicts: Sequence[DetectedConflict] = ()) -> InvariantRun:
    """A real :class:`InvariantRun` carrying exactly the conflicts handed in."""
    return InvariantRun(
        run_id="fake-detect-a",
        generation=3,
        status="ok",
        incomplete=(),
        outcomes=(),
        results=[],
        raw_conflicts=list(conflicts),
        conflicts=list(conflicts),
    )


def _report(run_id: str, proposed: int) -> ReconcileReport:
    return ReconcileReport(
        run_id=run_id,
        generation=3,
        conflicts_seen=proposed,
        proposed=proposed,
        pending=proposed,
        sensitive_hold=0,
        evidence_only=0,
        skipped_fingerprint=0,
        skipped_oscillation=0,
        escalated_oscillation=0,
        rationale_attached=0,
        lineage_rows=1_712_775,
        lineage_generations=3,
        escalation_reason_persisted=True,
        model_version=2,
        model_sha256="0" * 64,
        by_type={},
    )


@dataclass
class Timings:
    """The three clocks ``bench:detect-persist-reconcile`` adds up."""

    invariants: float = 12.04
    persist: float = 2.78
    reconcile: float = 8.52


#: The shape one real ``materialize(persist=False)`` returns on the full profile.
#: The counts are the ones the loaded graded database actually produces, so a
#: detail string built from them carries the numbers a scorecard carries.
MATERIALIZE_REPORT = MaterializeReport(
    generation=3,
    lineage_generations=(1, 2, 3),
    persons=43_375,
    links=120_000,
    candidates=97_980,
    entities=43_375,
    lineage=1_712_775,
    elapsed_ms=0.0,
    persisted=False,
    commit_included=False,
)


class FakeMaterialize:
    """A :func:`recon.resolve.materialize` stand-in that charges the clock.

    The real call resolves three generations and builds 1.9M rows -- about
    fourteen seconds against the loaded graded database, which is the right cost
    for the scorecard row and the wrong cost for a unit test. So the *duration* is
    scripted and the row's *arithmetic* (floor + three stages, compared against
    the 30s budget, rendered into the detail) is the shipped code path.

    Every call's keyword arguments are recorded, because the safety claim the row
    makes -- "wrote none of them" -- is exactly ``persist=False``, and a claim
    nothing asserts is a comment.
    """

    def __init__(self, clock: FakeClock, seconds: float) -> None:
        self._clock = clock
        self._seconds = seconds
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> MaterializeReport:
        self.calls.append(dict(kwargs))
        self._clock.advance(self._seconds)
        return replace(MATERIALIZE_REPORT, elapsed_ms=self._seconds * 1000.0)


def fake_pipeline_run(
    *,
    conflicts: Sequence[DetectedConflict] = (),
    timings: Timings | None = None,
    proposal_count: int = 3050,
) -> PipelineRun:
    """A real :class:`PipelineRun` with injected clocks and an injected conflict set.

    Everything the six gates read off it is a real field of the real dataclass;
    what is synthetic is only the *values*, which is exactly the measurement the
    loaded database would otherwise have to produce.
    """
    clocks = timings or Timings()
    first = _report("fake-first", proposal_count)
    return PipelineRun(
        started_at=datetime(2026, 8, 25, tzinfo=UTC),
        precondition=Precondition(
            landing=dict(LANDING),
            entities=43_375,
            links=120_000,
            lineage=1_712_775,
            lineage_generations=3,
        ),
        run_a=invariant_run(conflicts),
        run_b=invariant_run(conflicts),
        invariants_seconds=clocks.invariants,
        invariants_b_seconds=clocks.invariants,
        persist_seconds=clocks.persist,
        mirror_before=EMPTY_MIRROR,
        mirror_after=EMPTY_MIRROR,
        report_first=first,
        report_second=_report("fake-second", 0),
        dry_a=first,
        dry_b=first,
        reconcile_seconds=clocks.reconcile,
        proposals=(),
        conflict_status={"open": proposal_count},
        fixtures_root=Path("/nonexistent"),
        dsn_database="fake",
    )
