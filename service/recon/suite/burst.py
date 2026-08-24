"""``spend-cap-burst``: the graded proof that the spend cap cannot be bypassed.

SPEC gate 1 requires the burst to appear in ``recon.suite``: *"burst test halts
exactly at cap"* is a scorecard row, not only a pytest run. This module is that
row, and it runs the **real** thing -- the same
:func:`recon.llm.generate_rationale` the reconciler calls, the same
``budget_reservations`` INSERT, the same ``BEFORE INSERT`` trigger, the same
ledger. Nothing about the cap is simulated; the only thing the mock provider
replaces is the network call whose *cost* is being capped.

Why this check was rebuilt: it could not fail
----------------------------------------------
Every one of the five fixes this check was supposed to guard was removed in turn
and the row still reported **PASS**. The reason was structural, not accidental:
the burst exercised exactly two paths -- a successful call and a cap refusal --
so it never reached ``settle_failed_call``, never reached the pre-send
classification, never reached the overspend halt and never reached the sweeper.
A check whose failure mode has never been observed is not a check; it is a
demonstration with a status column.

So the burst now has two halves, and the second is the money half:

* **the cap phase** -- 120 concurrent requests against a cap sized for 6, every
  granted call parked in flight while the ledger is read;
* **the evidence phases** -- one deliberate trip down each path that releases
  money, asserted on the ledger afterwards: a post-send failure, a pre-send
  failure, a success whose usage is not evidence, an overspend, and the TTL
  sweeper. Plus a structural count of the release sites themselves.

Each phase is a separate, two-sided dimension of :class:`BurstOutcome`, and each
one has been sabotaged and observed going RED.

Two-sided, always
-----------------
Every assertion is **two-sided**, because each one alone has a way of passing
while the product is broken: **exactly** ``ADMITTED`` grants and not "at most";
spend landing **exactly** on the cap; a pre-send failure releasing **everything**
next to a post-send failure releasing **nothing**; the ``spent <= cap`` CHECK
still present while all of it happened.

Any one failing is a **FAIL** row carrying the observed vector, which is what
"it must be able to fail" means: the check reports what it saw, and what it saw
is what decides the status.

Its own scopes, never the real budget
--------------------------------------
The harness provisions throwaway ledger scopes through the ops principal and
points :data:`~recon.budget.DAILY_SCOPE_ENV` at one of them. It does **not** drop
the mandated daily cap -- there is no longer any way to, from any caller: the
``scopes`` argument and the stack-guarded ``IsolatedScopes`` type that a red team
constructed out of ``exec(compile(...))`` are both gone. What the harness does is
choose *which ledger row* carries the mandated cap, which is deployment
configuration and not an argument. That is correct here and nowhere else:
charging a verification burst against the real ``daily`` scope would spend the
day's budget to prove the day's budget works, and would make the result depend on
whatever else ran today. The scopes and their rows are deleted afterwards,
ops-side, whatever the outcome.

The boundary phases run as ``recon_writer``
--------------------------------------------
Three of the dimensions below deliberately connect as the **capped party** and
try to get its money back: by issuing the release ``UPDATE`` by hand in spellings
no source-level counter matches, by settling a replay receipt, and by running the
TTL sweeper under the wrong principal. Those are the attacks four red-team passes
actually used, and a check that has never watched them fail is not a check.
"""

from __future__ import annotations

import ast
import os
import re
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from recon.budget import (
    AUDIT_CAP_HIT,
    DAILY_SCOPE_ENV,
    KS_CAP_EXCEEDED,
    KS_RESERVATION_LIFECYCLE,
    BudgetError,
    NeverSent,
    PreSendProof,
    ProviderReportedUsage,
    SettlementRefused,
    Usage,
    cost_microusd,
    provision_scope,
    reserve,
    run_scope,
    settle,
    settle_failed_call,
    sweep_expired_reservations,
    worst_case_input_tokens,
    worst_case_microusd,
)
from recon.db import ROLE_RECON_WRITER, get_engine, role_connection, role_url
from recon.llm import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    MOCK_MODEL_ID,
    STATUS_CAP_HIT,
    STATUS_OK,
    STATUS_OVERSPEND,
    STATUS_PROVIDER_ERROR,
    STATUS_SCOPE_HALTED,
    SYSTEM_PROMPT,
    MockProvider,
    ProviderError,
    ProviderNotSent,
    ProviderResult,
    RationaleOutcome,
    RationaleRequest,
    generate_rationale,
)
from recon.logging import get_logger
from recon.suite.checks import CheckResult

__all__ = [
    "ADMITTED",
    "CHECK_NAME",
    "CONTENDERS",
    "RETRY_WAVE",
    "BurstOutcome",
    "burst_outcome",
    "check_spend_cap_burst",
    "release_sites",
    "reset_burst_cache",
    "run_burst",
]

log = get_logger("recon.suite.burst")

#: The name this check keeps in the scorecard and in ``--only``.
CHECK_NAME = "spend-cap-burst"

#: R17's burst: 120 concurrent requests against a cap sized for 6.
CONTENDERS = 120
ADMITTED = 6
RETRY_WAVE = 10
MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS

#: Seconds a parked worker waits before giving up. Generous: the only thing it
#: guards is a hung harness, and a real stall shows up as a failed assertion on
#: the outcome vector rather than as a silent pass.
PARK_TIMEOUT = 120.0
GATHER_TIMEOUT = 120.0

#: How long to wait for the admitted calls to reach the provider. Short on
#: purpose: they arrive in milliseconds, and the previous 120-second wait meant a
#: BROKEN cap took ~4 minutes to time out and then reported the wrong reason
#: ("only some of the admitted calls ever reached the provider") for what was
#: actually "the cap admitted everybody".
ADMIT_TIMEOUT = 30.0

#: How long to wait for the *next* park after the admitted ones. If one arrives,
#: the cap let more calls through than it allows, which is diagnosed immediately
#: instead of being inferred from a timeout minutes later.
OVER_ADMIT_PROBE = 2.0

PROMPT = (
    "Source crm says the enrollment status is 'active'; source sis says "
    "'withdrawn'. The sis record was loaded in a later generation."
)

#: Suffix that marks an observed raw-UPDATE outcome as the trigger refusing it.
KS_LIFECYCLE = f":{KS_RESERVATION_LIFECYCLE}"

#: What a release site looks like in the source: a *statement* that UPDATEs the
#: one table whose settle trigger can hand budget back, or a direct call to the
#: privileged release helper. Anchored to the start of a line so that prose
#: mentioning the table -- including this comment and the pattern itself -- is
#: not counted as a release site. See :func:`release_sites`.
_RELEASE_SQL = re.compile(r"(?im)^\s*update\s+budget_reservations\b|keystone_budget_release\s*\(")


#: This module. It contains release statements ON PURPOSE -- the six spellings
#: in :data:`_RAW_RELEASE_SPELLINGS` that the ``raw_update`` dimension issues as
#: the capped party and proves the database refuses. It is excluded from the
#: PRODUCT count for that reason and no other, it is one named file rather than a
#: pattern, and ``tests/budget/test_release_chokepoint.py`` asserts that every
#: release literal in it is one of the registered, refused attack spellings.
HARNESS_MODULE = "suite/burst.py"


def release_sites() -> tuple[str, ...]:
    """Every place in the PRODUCT that can lower ``budget_ledger.spent_microusd``.

    Walks the AST of every module in the package looking for SQL that can perform
    the release -- an ``UPDATE`` on ``budget_reservations`` (whose settle trigger
    releases ``reserve - actual``) or a direct call to
    ``keystone_budget_release``. The design promise is that there is **exactly
    one**, and that it demands a typed evidence value.

    This is defence in depth and is no longer the boundary, which is the whole
    lesson of the round that added the ``raw_update`` dimension below: a red team
    released money with ``UPDATE public.budget_reservations``, ``UPDATE
    "budget_reservations"`` and ``UPDATE ONLY budget_reservations`` -- three
    spellings this regex does not match and, far more to the point, three
    statements the GRANT permitted. The boundary is migration 0010's settle
    trigger. This count is a smell test that a second release site has appeared,
    and it is kept because a second one would still be worth knowing about.

    Returns ``"<module>:<line>"`` for each, sorted.
    """
    package = Path(__file__).resolve().parents[1]
    found: list[str] = []
    for path in sorted(package.rglob("*.py")):
        if path.relative_to(package).as_posix() == HARNESS_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _RELEASE_SQL.search(node.value)
            ):
                found.append(f"{path.relative_to(package)}:{node.lineno}")
    return tuple(sorted(found))


def _unique(hint: str) -> str:
    """A collision-proof throwaway scope name.

    Deliberately **not** ``uuid4()``: the project bans it outright so it cannot
    drift onto a graded deterministic path. Process id plus a nanosecond clock
    is unique for this purpose and is obviously a runtime artefact rather than
    dataset content.
    """
    return f"suite-burst-{hint}-{os.getpid()}-{time.time_ns()}"


@contextmanager
def _daily(scope: str) -> Iterator[None]:
    """Point the mandated daily cap at ``scope`` for the duration of a phase.

    This does not remove a cap; it moves it onto a row this harness owns. The
    reservation still lands on the daily scope and the run scope exactly as a
    production call does, and every rule the trigger enforces still fires.
    """
    previous = os.environ.get(DAILY_SCOPE_ENV)
    os.environ[DAILY_SCOPE_ENV] = scope
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(DAILY_SCOPE_ENV, None)
        else:
            os.environ[DAILY_SCOPE_ENV] = previous


@contextmanager
def _connected_as(role: str) -> Iterator[None]:
    """Run the ops entry points with **every** DSN variable pointed at ``role``.

    This is how the sweeper's ``_refuse_capped_principal`` guard is exercised
    through the real entry point rather than by calling the guard directly: a
    misconfigured ops DSN is precisely the failure it exists for, so the check
    misconfigures one.

    It misconfigures *both* variables in :data:`recon.db.PRINCIPAL_ENV_VARS`,
    and that is the whole point of this function. Overriding ``DATABASE_URL``
    alone made the phase a check that could not **pass** wherever
    ``OPS_DATABASE_URL`` is set -- which ``infra/render.yaml`` does for the
    deployed web service, and which the invariant-sync stage now requires:
    :func:`recon.budget.ops_engine` reads that variable first, so the sweeper
    stayed connected as the **owner**, swept the reservation it was supposed to
    refuse to touch, and the row came out
    ``sweep_as_capped=refused:False/open:0``. Measured, both ways: a loud red
    and exit 1 with the variable set, ``refused:True/open:1`` with it unset.
    Nothing was ever silently green -- the row was simply *unobtainable* in one
    of the two configurations this project runs in.

    And neither configuration is the wrong one to be in. ``OPS_DATABASE_URL``
    unset is not a degenerate local setup: it is what ``infra/render.yaml``
    hands the deployed ``keystone-sweeper`` cron on purpose, because there
    ``DATABASE_URL`` already names the owner. Set is the deployed *web
    service*. The guard has to hold in both, so the check has to be runnable in
    both, so the switch has to be complete.

    The previous environment is restored exactly, including restoring a variable
    that was **unset** to unset rather than to the empty string -- "restored" has
    to mean restored (:func:`recon.db.restore_principal`).
    """
    from recon.db import connected_to

    with connected_to(role_url(role).render_as_string(hide_password=False)):
        yield


def _reserve_one(scope: str, *, lease_seconds: int = 300, hint: str = "phase"):
    """One real reservation on ``scope``, through the code production uses."""
    with _daily(scope):
        return reserve(
            idempotency_key=_unique(hint),
            model=MOCK_MODEL_ID,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            max_input_tokens=worst_case_input_tokens(SYSTEM_PROMPT + PROMPT),
            run_id=scope.removeprefix("run:"),
            lease_seconds=lease_seconds,
        )


def _request(index: int) -> RationaleRequest:
    # One prompt for every worker: the mock is deterministic, so identical
    # prompts mean identical usage, which is what lets spend be asserted as an
    # exact integer instead of a range.
    return RationaleRequest(subject=f"suite-burst-{index}", prompt=PROMPT)


def expected_reserve() -> int:
    """The worst case one attempt reserves, from the code production uses."""
    return worst_case_microusd(
        MOCK_MODEL_ID,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        max_input_tokens=worst_case_input_tokens(SYSTEM_PROMPT + PROMPT),
    )


def expected_actual() -> int:
    """The provider-reported cost of one granted call, from the price table."""
    result = MockProvider().complete(_request(0), max_output_tokens=MAX_OUTPUT_TOKENS)
    return cost_microusd(MOCK_MODEL_ID, result.usage)


# ===========================================================================
# the providers each evidence phase needs
# ===========================================================================
class _PostSendFailure:
    """Fails in a way that says nothing about where. Charged in full."""

    model = MOCK_MODEL_ID

    def complete(self, request: RationaleRequest, *, max_output_tokens: int) -> ProviderResult:
        raise ProviderError("APITimeoutError: read timed out after generation")


class _PreSendFailure:
    """Fails provably before the request left the process. Released."""

    model = MOCK_MODEL_ID

    def complete(self, request: RationaleRequest, *, max_output_tokens: int) -> ProviderResult:
        raise ProviderNotSent("ConnectionRefusedError: [Errno 61] connection refused")


class _SilentUsage:
    """Returns real text and reports nothing about what it billed."""

    model = MOCK_MODEL_ID

    def complete(self, request: RationaleRequest, *, max_output_tokens: int) -> ProviderResult:
        real = MockProvider().complete(request, max_output_tokens=max_output_tokens)
        return ProviderResult(text=real.text, usage=Usage(), model=self.model)


class _OverReporting:
    """Returns text, then reports far more usage than was reserved."""

    model = MOCK_MODEL_ID

    def complete(self, request: RationaleRequest, *, max_output_tokens: int) -> ProviderResult:
        real = MockProvider().complete(request, max_output_tokens=max_output_tokens)
        return ProviderResult(
            text=real.text,
            usage=Usage(input_tokens=10**7, output_tokens=10**7),
            model=self.model,
        )


@dataclass
class BurstOutcome:
    """The observed vector, plus the reasons it failed (empty when it passed)."""

    contenders: int
    admitted_expected: int
    cap_microusd: int
    reserve_each: int
    actual_each: int
    granted: int = 0
    refused: int = 0
    other: tuple[str, ...] = ()
    refusal_sqlstates: tuple[str, ...] = ()
    spend_while_open: int = -1
    final_spend: int = -1
    open_reservations: int = -1
    retries_granted: int = -1
    reservations_after_retries: int = -1
    cap_hit_audit_rows: int = -1
    alerts_fired: int = -1
    backstop_present: bool = False
    ledger_violations: int = -1
    over_admitted: bool = False
    # -- the evidence phases -------------------------------------------------
    release_sites: tuple[str, ...] = ()
    post_send_status: str = ""
    post_send_spend: int = -1
    pre_send_status: str = ""
    pre_send_spend: int = -1
    silent_usage_status: str = ""
    silent_usage_spend: int = -1
    overspend_status: str = ""
    after_overspend_status: str = ""
    overspend_spend: int = -1
    sweeper_charged: int = -1
    sweeper_released: int = -1
    sweeper_spend: int = -1
    # -- the boundary phases: the attacks four red teams actually used --------
    raw_update_outcomes: tuple[str, ...] = ()
    raw_update_spend: int = -1
    raw_update_control_spend: int = -1
    replay_receipt: bool = False
    replay_settle_refused: bool = False
    replay_spend: int = -1
    sweep_as_capped_refused: bool = False
    sweep_as_capped_spend: int = -1
    sweep_as_capped_open: int = -1
    failed_call_priced_refused: bool = False
    failed_call_priced_spend: int = -1
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def vector(self) -> str:
        """One line of evidence, printed whether the check passed or failed."""
        return (
            f"contenders={self.contenders} granted={self.granted} refused={self.refused} "
            f"other={len(self.other)} refusal_sqlstates={sorted(set(self.refusal_sqlstates))} "
            f"cap={self.cap_microusd} reserve_each={self.reserve_each} "
            f"spend_while_open={self.spend_while_open} actual_each={self.actual_each} "
            f"final_spend={self.final_spend} cap_hit_audit_rows={self.cap_hit_audit_rows} "
            f"alerts_fired={self.alerts_fired} retry_wave={RETRY_WAVE} "
            f"retries_granted={self.retries_granted} backstop={self.backstop_present} "
            f"ledger_violations={self.ledger_violations} "
            f"release_sites={len(self.release_sites)}{list(self.release_sites)} "
            f"post_send={self.post_send_status}/{self.post_send_spend} "
            f"pre_send={self.pre_send_status}/{self.pre_send_spend} "
            f"silent_usage={self.silent_usage_status}/{self.silent_usage_spend} "
            f"overspend={self.overspend_status}->{self.after_overspend_status}"
            f"/{self.overspend_spend} "
            f"sweeper=charged:{self.sweeper_charged}/released:{self.sweeper_released}"
            f"/spend:{self.sweeper_spend} "
            f"raw_update={list(self.raw_update_outcomes)}/{self.raw_update_spend}"
            f"->{self.raw_update_control_spend} "
            f"replay=receipt:{self.replay_receipt}/refused:{self.replay_settle_refused}"
            f"/{self.replay_spend} "
            f"sweep_as_capped=refused:{self.sweep_as_capped_refused}"
            f"/open:{self.sweep_as_capped_open}/{self.sweep_as_capped_spend} "
            f"failed_call_priced=refused:{self.failed_call_priced_refused}"
            f"/{self.failed_call_priced_spend}"
        )


def run_burst(*, contenders: int = CONTENDERS, admitted: int = ADMITTED) -> BurstOutcome:
    """Run the real burst against real Postgres and return what was observed.

    Raises nothing for a *failed* burst -- a failure is data, recorded in
    :attr:`BurstOutcome.failures`. Only an infrastructure fault (no database, no
    schema) escapes, and :func:`recon.suite.__main__.run_check` turns that into a
    FAIL row too.
    """
    reserve_each = expected_reserve()
    actual_each = expected_actual()
    cap = reserve_each * admitted

    outcome = BurstOutcome(
        contenders=contenders,
        admitted_expected=admitted,
        cap_microusd=cap,
        reserve_each=reserve_each,
        actual_each=actual_each,
    )
    if actual_each >= reserve_each:
        outcome.failures.append(
            f"the settlement must release something: actual_each={actual_each} "
            f"is not below reserve_each={reserve_each}"
        )

    outcome.release_sites = release_sites()
    _run_cap_phase(outcome, contenders=contenders, admitted=admitted, cap=cap)
    _run_evidence_phases(outcome)
    _run_boundary_phases(outcome)
    _assess(outcome, admitted=admitted, contenders=contenders, cap=cap)
    return outcome


# ===========================================================================
# phase 1 -- the cap under a concurrent burst
# ===========================================================================
def _run_cap_phase(outcome: BurstOutcome, *, contenders: int, admitted: int, cap: int) -> None:
    """120 concurrent requests, every grant parked in flight, against one cap."""
    from recon.budget import register_alert_sink, unregister_alert_sink

    # Two DISTINCT scopes, so the burst walks the real two-scope path R17
    # mandates: the daily stand-in carries the binding cap and the run scope is
    # roomy, which is exactly the shape the daily cap exists to catch (a run
    # inside its own cap and over the day's).
    daily = _unique("daily")
    run_id = _unique("run")
    provision_scope(daily, cap)
    provision_scope(run_scope(run_id), cap * 100)

    start = threading.Barrier(contenders, timeout=GATHER_TIMEOUT)
    release = threading.Event()
    parked = threading.Semaphore(0)
    alerts: list[dict] = []
    register_alert_sink(alerts.append)

    def hold(_request: RationaleRequest) -> None:
        """Park inside the provider so the reservation stays open and committed."""
        parked.release()
        if not release.wait(timeout=PARK_TIMEOUT):
            raise TimeoutError("burst worker was never released")

    provider = MockProvider(on_call=hold)

    def worker(index: int) -> RationaleOutcome:
        start.wait()
        return generate_rationale(
            _request(index),
            run_id=run_id,
            idempotency_key=_unique(f"c{index}"),
            provider=provider,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            max_attempts=1,
        )

    daily_ctx = _daily(daily)
    daily_ctx.__enter__()
    try:
        with ThreadPoolExecutor(max_workers=contenders) as pool:
            futures = [pool.submit(worker, index) for index in range(contenders)]

            # -- wait until every granted call is parked in-flight -------------
            deadline = time.monotonic() + ADMIT_TIMEOUT
            admitted_parked = 0
            for _ in range(admitted):
                if not parked.acquire(timeout=max(0.0, deadline - time.monotonic())):
                    break
                admitted_parked += 1

            # -- diagnose an OVER-admitting cap immediately --------------------
            # This is the fast path that matters: with the cap broken, every
            # contender parks, and waiting for `contenders - admitted` refusals
            # that will never arrive used to take ~4 minutes and then report the
            # wrong reason. One extra park is proof, and it arrives in
            # milliseconds.
            outcome.over_admitted = parked.acquire(timeout=OVER_ADMIT_PROBE)
            if outcome.over_admitted:
                outcome.failures.append(
                    f"the cap admitted MORE than {admitted} concurrent calls: a "
                    f"{admitted + 1}th call reached the provider while the ledger was "
                    "already at its cap"
                )
            elif admitted_parked < admitted:
                outcome.failures.append(
                    f"only {admitted_parked} of the {admitted} admitted calls ever "
                    "reached the provider"
                )

            if not outcome.over_admitted:
                refused_deadline = time.monotonic() + ADMIT_TIMEOUT
                while time.monotonic() < refused_deadline:
                    if sum(1 for future in futures if future.done()) == contenders - admitted:
                        break
                    time.sleep(0.05)

            # -- the ledger, with every grant still open -----------------------
            outcome.spend_while_open = _spent(daily)
            outcome.open_reservations = _open_count(daily)

            # -- a retry wave against the exhausted cap ------------------------
            retries = [
                generate_rationale(
                    _request(1000 + index),
                    run_id=run_id,
                    idempotency_key=_unique(f"retry{index}"),
                    provider=MockProvider(),  # would succeed instantly if it ran
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                    max_attempts=3,
                )
                for index in range(RETRY_WAVE)
            ]
            outcome.retries_granted = sum(1 for item in retries if item.status == STATUS_OK)
            outcome.reservations_after_retries = _reservation_count(daily)

            # -- release and let the granted calls settle ----------------------
            release.set()
            outcomes = [future.result(timeout=GATHER_TIMEOUT) for future in futures]
    finally:
        release.set()
        unregister_alert_sink(alerts.append)
        daily_ctx.__exit__(None, None, None)

    granted = [item for item in outcomes if item.status == STATUS_OK]
    refused = [item for item in outcomes if item.status == STATUS_CAP_HIT]
    other = [item for item in outcomes if item.status not in (STATUS_OK, STATUS_CAP_HIT)]

    outcome.granted = len(granted)
    outcome.refused = len(refused)
    outcome.other = tuple(sorted(f"{item.status}:{item.detail}" for item in other))
    outcome.refusal_sqlstates = tuple(str(item.sqlstate) for item in refused)
    outcome.final_spend = _spent(daily)
    outcome.cap_hit_audit_rows = _audit_count(AUDIT_CAP_HIT, daily)
    outcome.alerts_fired = sum(1 for event in alerts if event.get("scope") == daily)
    outcome.backstop_present = _backstop_present()
    outcome.ledger_violations = _ledger_violations()
    _cleanup(daily, run_scope(run_id))


# ===========================================================================
# phase 2 -- every path that can release money, walked once each
# ===========================================================================
def _run_evidence_phases(outcome: BurstOutcome) -> None:
    """One deliberate trip down each release path, asserted on the ledger.

    These are the paths the old burst never reached, which is why all five of
    its fixes could be deleted without turning the row red.
    """
    reserve_each = outcome.reserve_each

    # -- a failure that says nothing about where: charged in FULL -------------
    scope = _provision(reserve_each * 4)
    outcome.post_send_status = _one_call(scope, _PostSendFailure()).status
    outcome.post_send_spend = _spent(scope)
    _cleanup(scope)

    # -- a failure that provably never left: RELEASED ------------------------
    scope = _provision(reserve_each * 4)
    outcome.pre_send_status = _one_call(scope, _PreSendFailure()).status
    outcome.pre_send_spend = _spent(scope)
    _cleanup(scope)

    # -- a SUCCESS whose usage is not evidence: charged in FULL ---------------
    scope = _provision(reserve_each * 4)
    outcome.silent_usage_status = _one_call(scope, _SilentUsage()).status
    outcome.silent_usage_spend = _spent(scope)
    _cleanup(scope)

    # -- an overspend HALTS the scope for everyone afterwards -----------------
    scope = _provision(reserve_each * 4)
    outcome.overspend_status = _one_call(scope, _OverReporting()).status
    outcome.after_overspend_status = _one_call(scope, MockProvider()).status
    outcome.overspend_spend = _spent(scope)
    _cleanup(scope)

    # -- a dead lease is CHARGED, not refunded --------------------------------
    scope = _provision(reserve_each * 4)
    reservation = _reserve_one(scope, lease_seconds=1, hint="abandoned")
    swept = [
        item
        for item in sweep_expired_reservations(
            grace_seconds=0, now=reservation.lease_expires_at + timedelta(seconds=1)
        )
        if item.scope == scope
    ]
    outcome.sweeper_charged = sum(item.charged_microusd for item in swept)
    outcome.sweeper_released = sum(item.released_microusd for item in swept)
    outcome.sweeper_spend = _spent(scope)
    _cleanup(scope)


# ===========================================================================
# phase 3 -- the guards a source-level release count cannot see
# ===========================================================================
#: The release ``UPDATE``, issued by hand as the capped party. The first three
#: are the spellings a red team used to settle open reservations at
#: ``actual = 0`` while ``recon.suite.burst.release_sites`` counted exactly one
#: release site and the row stayed green: a regex over the source cannot be the
#: boundary, because the GRANT permits the statement however it is written.
#: The last three name an amount the row does not justify. Every one of them is
#: migration 0010's settle trigger's problem now, and every one must be KS007.
_RAW_RELEASE_SPELLINGS: tuple[tuple[str, str], ...] = (
    (
        "schema-qualified",
        "UPDATE public.budget_reservations SET actual_microusd = 0, state = 'settled' "
        "WHERE idempotency_key = :key",
    ),
    (
        "quoted",
        "UPDATE \"budget_reservations\" SET actual_microusd = 0, state = 'settled' "
        "WHERE idempotency_key = :key",
    ),
    (
        "only",
        "UPDATE ONLY budget_reservations SET actual_microusd = 0, state = 'settled' "
        "WHERE idempotency_key = :key",
    ),
    (
        "priced-at-a-number-of-its-own",
        "UPDATE budget_reservations SET actual_microusd = 1, state = 'settled', "
        "settle_evidence = 'provider_reported_usage', usage_input_tokens = 1, "
        "usage_output_tokens = 1, usage_cache_read_tokens = 0, usage_cache_write_tokens = 0 "
        "WHERE idempotency_key = :key",
    ),
    (
        "unknown-outcome-refunded",
        "UPDATE budget_reservations SET actual_microusd = 0, state = 'settled', "
        "settle_evidence = 'outcome_unknown' WHERE idempotency_key = :key",
    ),
    (
        "self-attested-outage",
        "UPDATE budget_reservations SET actual_microusd = 0, state = 'settled', "
        "settle_evidence = 'never_sent', settle_proof = 'ops_attested_outage' "
        "WHERE idempotency_key = :key",
    ),
)


def _run_boundary_phases(outcome: BurstOutcome) -> None:
    """Every release-side guard the evidence phases never reach, walked once each.

    Each of these was removed in turn and the row still reported PASS, which is
    the same structural failure that got the evidence phases built: a guard whose
    failure has never been observed is a comment with a code shape.
    """
    _run_grant_boundary_phase(outcome)
    _run_replay_phase(outcome)
    _run_sweeper_principal_phase(outcome)
    _run_failed_call_evidence_phase(outcome)


def _run_grant_boundary_phase(outcome: BurstOutcome) -> None:
    """THE blocker: the release is refused by the DATABASE, in every spelling.

    Two-sided, and the second side is the point: after six hand-written
    statements have all been refused and the ledger has not moved a microusd, the
    *legitimate* settlement through :func:`recon.budget.settle` still releases
    what the committed rates say the call cost. A boundary that refused
    everything would be equally green and completely broken.
    """
    scope = _provision(outcome.reserve_each * 4, "grant")
    reservation = _reserve_one(scope, hint="grant")
    key = reservation.scope_keys[scope]

    observed: list[str] = []
    for name, statement in _RAW_RELEASE_SPELLINGS:
        try:
            with role_connection(ROLE_RECON_WRITER) as conn:
                conn.execute(text(statement), {"key": key})
        except DBAPIError as exc:
            observed.append(f"{name}:{getattr(getattr(exc, 'orig', None), 'sqlstate', None)}")
        else:
            observed.append(f"{name}:ALLOWED")
    outcome.raw_update_outcomes = tuple(observed)
    outcome.raw_update_spend = _spent(scope)

    try:
        settle(
            reservation,
            ProviderReportedUsage(
                MockProvider().complete(_request(0), max_output_tokens=MAX_OUTPUT_TOKENS).usage
            ),
        )
    except SettlementRefused:
        # The row is already closed, which means one of the statements above got
        # through. Swallowed rather than raised so the check reports *that* --
        # the reason it exists -- instead of dying with a stack trace. The
        # assertion is on the ledger either way: a control that did not settle
        # leaves the reservation's full worst case charged, which is not
        # `actual_each`, so this cannot hide a broken control.
        log.error(
            "burst.grant_boundary_control_refused",
            detail="a hand-written release closed the reservation before the control could",
        )
    outcome.raw_update_control_spend = _spent(scope)
    _cleanup(scope)


def _run_replay_phase(outcome: BurstOutcome) -> None:
    """A replay receipt is not a grant, and settling one must not release money.

    ``reserve`` returns ``replayed=True`` for a key that already reserved, and
    that receipt names rows somebody else is holding. Settling it releases budget
    this call never charged.
    """
    scope = _provision(outcome.reserve_each * 4, "replay")
    key = _unique("replay")
    common = {
        "model": MOCK_MODEL_ID,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "max_input_tokens": worst_case_input_tokens(SYSTEM_PROMPT + PROMPT),
        "run_id": scope.removeprefix("run:"),
    }
    with _daily(scope):
        reserve(idempotency_key=key, **common)
        receipt = reserve(idempotency_key=key, **common)
    outcome.replay_receipt = receipt.replayed

    try:
        settle(receipt, NeverSent(PreSendProof.CONNECTION_REFUSED, "the harness never called"))
    except SettlementRefused:
        outcome.replay_settle_refused = True
    else:
        outcome.replay_settle_refused = False
    outcome.replay_spend = _spent(scope)
    _cleanup(scope)


def _run_sweeper_principal_phase(outcome: BurstOutcome) -> None:
    """The TTL sweeper refuses to run as the capped party, before touching a row.

    The sweep closes *somebody else's* reservation, which is an ops decision. Its
    own transition is ``open -> settled``, which ``recon_writer`` may perform, so
    the principal check is the thing standing between the capped party and every
    open reservation in the database.
    """
    scope = _provision(outcome.reserve_each * 4, "sweepprincipal")
    reservation = _reserve_one(scope, lease_seconds=1, hint="sweepprincipal")
    horizon = reservation.lease_expires_at + timedelta(seconds=1)

    with _connected_as(ROLE_RECON_WRITER):
        try:
            sweep_expired_reservations(grace_seconds=0, now=horizon)
        except BudgetError:
            outcome.sweep_as_capped_refused = True
        except Exception:  # any other failure is not the guard firing
            outcome.sweep_as_capped_refused = False
        else:
            outcome.sweep_as_capped_refused = False
    outcome.sweep_as_capped_open = _open_count(scope)
    outcome.sweep_as_capped_spend = _spent(scope)

    # Control, ops-side: the same sweep DOES close it, and charges it in full.
    sweep_expired_reservations(grace_seconds=0, now=horizon)
    _cleanup(scope)


def _run_failed_call_evidence_phase(outcome: BurstOutcome) -> None:
    """A FAILED call has no provider-reported usage, and may not borrow one.

    ``settle_failed_call`` refuses :class:`~recon.budget.ProviderReportedUsage`
    outright. Without that refusal a failed call settles at whatever usage a
    caller hands it -- one input token and one output token buys a 99.8% refund
    on a reservation the provider may well have billed in full.
    """
    scope = _provision(outcome.reserve_each * 4, "failedpriced")
    reservation = _reserve_one(scope, hint="failedpriced")
    try:
        settle_failed_call(
            reservation, ProviderReportedUsage(Usage(input_tokens=1, output_tokens=1))
        )
    except TypeError:
        outcome.failed_call_priced_refused = True
    else:
        outcome.failed_call_priced_refused = False
    outcome.failed_call_priced_spend = _spent(scope)
    _cleanup(scope)


def _provision(cap: int, hint: str = "evidence") -> str:
    """A throwaway ``run:`` scope, provisioned ops-side.

    Named ``run:<id>`` on purpose: pointing :data:`DAILY_SCOPE_ENV` at the same
    row the run scope resolves to collapses the two mandated scopes onto one
    ledger row, which is how a phase gets a single number to assert on without
    anybody being able to *drop* a cap to get it.
    """
    scope = f"run:{_unique(hint)}"
    provision_scope(scope, cap)
    return scope


def _one_call(scope: str, provider: object) -> RationaleOutcome:
    with _daily(scope):
        return generate_rationale(
            _request(0),
            run_id=scope.removeprefix("run:"),
            idempotency_key=_unique("evidence"),
            provider=provider,  # type: ignore[arg-type]
            max_output_tokens=MAX_OUTPUT_TOKENS,
            max_attempts=1,
        )


def _assess(outcome: BurstOutcome, *, admitted: int, contenders: int, cap: int) -> None:
    """Turn the observed vector into failure reasons. This is the whole check."""
    if outcome.other:
        outcome.failures.append(f"unexpected outcomes: {list(outcome.other)}")
    if outcome.granted != admitted:
        outcome.failures.append(
            f"expected exactly {admitted} grants, got {outcome.granted} "
            "(a cap that admits fewer calls than it allows is broken too)"
        )
    if outcome.refused != contenders - admitted:
        outcome.failures.append(f"expected {contenders - admitted} refusals, got {outcome.refused}")
    states = set(outcome.refusal_sqlstates)
    if states and states != {KS_CAP_EXCEEDED}:
        outcome.failures.append(
            f"a refusal was not the cap refusing: sqlstates={sorted(states)}; a dropped "
            "connection or a deadlock must not masquerade as the cap holding"
        )
    if outcome.spend_while_open != cap:
        outcome.failures.append(
            f"the burst must land exactly on the cap while in flight: "
            f"spent={outcome.spend_while_open}, cap={cap}"
        )
    if outcome.open_reservations != admitted:
        outcome.failures.append(
            f"expected {admitted} open reservations in flight, got {outcome.open_reservations}"
        )
    if outcome.retries_granted != 0:
        outcome.failures.append(f"{outcome.retries_granted} retries got through the cap")
    if outcome.reservations_after_retries != admitted:
        outcome.failures.append(
            f"a retry stored a reservation past the cap: "
            f"{outcome.reservations_after_retries} rows, expected {admitted}"
        )
    expected_final = outcome.actual_each * admitted
    if outcome.final_spend != expected_final:
        outcome.failures.append(
            f"after settlement the ledger must hold the reported cost of the {admitted} "
            f"calls that happened: spent={outcome.final_spend}, expected={expected_final}"
        )
    if outcome.final_spend > cap:
        outcome.failures.append(f"final spend {outcome.final_spend} is above the cap {cap}")
    if outcome.cap_hit_audit_rows != outcome.refused + RETRY_WAVE:
        outcome.failures.append(
            f"every refusal must leave a cap_hit audit row: {outcome.cap_hit_audit_rows} rows "
            f"for {outcome.refused} refusals plus {RETRY_WAVE} retries"
        )
    if outcome.alerts_fired != outcome.cap_hit_audit_rows:
        outcome.failures.append(
            f"the stubbed alert must fire for every cap hit: {outcome.alerts_fired} alerts "
            f"for {outcome.cap_hit_audit_rows} audit rows"
        )
    if not outcome.backstop_present:
        outcome.failures.append(
            "ck_budget_spent_within_cap is gone; a burst that passed with the backstop "
            "dropped proves nothing"
        )
    if outcome.ledger_violations != 0:
        outcome.failures.append(f"{outcome.ledger_violations} ledger row(s) are over their cap")

    _assess_evidence(outcome)


def _assess_evidence(outcome: BurstOutcome) -> None:
    """The money half: one rule per path that can release a reservation."""
    reserve_each = outcome.reserve_each

    if len(outcome.release_sites) != 1:
        outcome.failures.append(
            f"spend must be reducible in exactly ONE place in the product; found "
            f"{len(outcome.release_sites)}: {list(outcome.release_sites)}. This is the "
            "defence-in-depth signal, not the boundary -- see the raw_update dimension"
        )

    # A failure that says nothing about where it happened is post-send.
    if outcome.post_send_status != STATUS_PROVIDER_ERROR:
        outcome.failures.append(
            f"a post-send failure must report {STATUS_PROVIDER_ERROR}, got "
            f"{outcome.post_send_status!r}"
        )
    if outcome.post_send_spend != reserve_each:
        outcome.failures.append(
            f"a failure that may have reached the provider must stay CHARGED IN FULL: "
            f"spent={outcome.post_send_spend}, expected={reserve_each}"
        )

    # And the other side, so the rule is not merely "charge everything".
    if outcome.pre_send_status != STATUS_PROVIDER_ERROR:
        outcome.failures.append(
            f"a pre-send failure must report {STATUS_PROVIDER_ERROR}, got "
            f"{outcome.pre_send_status!r}"
        )
    if outcome.pre_send_spend != 0:
        outcome.failures.append(
            f"a failure that provably never reached the provider must be RELEASED: "
            f"spent={outcome.pre_send_spend}, expected=0"
        )

    # A success that reports no usage bills you and must not be free.
    if outcome.silent_usage_status != STATUS_OK:
        outcome.failures.append(
            f"a call that returned text must still report {STATUS_OK}, got "
            f"{outcome.silent_usage_status!r}"
        )
    if outcome.silent_usage_spend != reserve_each:
        outcome.failures.append(
            f"a successful call whose usage is absent or zeroed has an UNKNOWN actual "
            f"and must be charged the full reservation: spent={outcome.silent_usage_spend}, "
            f"expected={reserve_each}"
        )

    # An overspend halts the scope itself, not just the call.
    if outcome.overspend_status != STATUS_OVERSPEND:
        outcome.failures.append(
            f"an overspend must report {STATUS_OVERSPEND}, got {outcome.overspend_status!r}"
        )
    if outcome.after_overspend_status != STATUS_SCOPE_HALTED:
        outcome.failures.append(
            f"after an overspend the scope must REFUSE further reservations "
            f"({STATUS_SCOPE_HALTED}), got {outcome.after_overspend_status!r}: a halt "
            "nothing consumes is not a halt"
        )
    if outcome.overspend_spend != reserve_each:
        outcome.failures.append(
            f"an overspending scope holds every microusd it can and takes no more: "
            f"spent={outcome.overspend_spend}, expected={reserve_each}"
        )

    # A dead lease proves the holder died, never that the call did not happen.
    if outcome.sweeper_charged != reserve_each:
        outcome.failures.append(
            f"the sweeper must CHARGE an abandoned reservation in full: "
            f"charged={outcome.sweeper_charged}, expected={reserve_each}"
        )
    if outcome.sweeper_released != 0:
        outcome.failures.append(
            f"the sweeper released {outcome.sweeper_released} microusd for a call it "
            "cannot prove did not happen"
        )
    if outcome.sweeper_spend != reserve_each:
        outcome.failures.append(
            f"after the sweep the ledger must still hold the abandoned reservation: "
            f"spent={outcome.sweeper_spend}, expected={reserve_each}"
        )

    _assess_boundary(outcome)


def _assess_boundary(outcome: BurstOutcome) -> None:
    """The guards a source-level release count cannot see. One rule each."""
    reserve_each = outcome.reserve_each

    # -- the release is refused by the DATABASE, in every spelling ------------
    allowed = [item for item in outcome.raw_update_outcomes if not item.endswith(KS_LIFECYCLE)]
    if len(outcome.raw_update_outcomes) != len(_RAW_RELEASE_SPELLINGS) or allowed:
        outcome.failures.append(
            f"the release must be refused by the database with {KS_RESERVATION_LIFECYCLE} "
            f"however it is spelled; observed {list(outcome.raw_update_outcomes)}. A count "
            "of release sites in the source is not a boundary: the grant permits the "
            "statement whatever it looks like"
        )
    if outcome.raw_update_spend != reserve_each:
        outcome.failures.append(
            f"a hand-written settlement moved the ledger: spent={outcome.raw_update_spend}, "
            f"expected={reserve_each}"
        )
    # ...and the other side, so this is not "refuse every settlement".
    if outcome.raw_update_control_spend != outcome.actual_each:
        outcome.failures.append(
            f"the legitimate settlement must still release what the committed rates say "
            f"the call cost: spent={outcome.raw_update_control_spend}, "
            f"expected={outcome.actual_each}"
        )

    # -- a replay receipt is not a grant --------------------------------------
    if not outcome.replay_receipt:
        outcome.failures.append(
            "a second reservation on the same idempotency key must come back as a replay "
            "receipt; the phase never reached the guard it is testing"
        )
    if not outcome.replay_settle_refused:
        outcome.failures.append(
            "settling a replay receipt must be REFUSED: the key was already present, "
            "nothing was charged for it here, and releasing it hands back budget this "
            "call never reserved"
        )
    if outcome.replay_spend != reserve_each:
        outcome.failures.append(
            f"settling a replay receipt released the reservation somebody else is holding: "
            f"spent={outcome.replay_spend}, expected={reserve_each}"
        )

    # -- the sweeper refuses to run as the capped party ------------------------
    if not outcome.sweep_as_capped_refused:
        outcome.failures.append(
            "the TTL sweeper must REFUSE to run as recon_writer: its transition is "
            "open -> settled, which the capped party may perform, so closing somebody "
            "else's reservation is stopped by the principal check or by nothing"
        )
    if outcome.sweep_as_capped_open != 1:
        outcome.failures.append(
            f"a sweep attempted as the capped party must touch no row at all: "
            f"{outcome.sweep_as_capped_open} open reservation(s) left, expected 1"
        )
    if outcome.sweep_as_capped_spend != reserve_each:
        outcome.failures.append(
            f"a sweep attempted as the capped party moved the ledger: "
            f"spent={outcome.sweep_as_capped_spend}, expected={reserve_each}"
        )

    # -- a failed call may not borrow a provider report ------------------------
    if not outcome.failed_call_priced_refused:
        outcome.failures.append(
            "settle_failed_call must REFUSE ProviderReportedUsage: a call that failed "
            "reported no usage, and accepting one lets a caller price its own refund"
        )
    if outcome.failed_call_priced_spend != reserve_each:
        outcome.failures.append(
            f"a failed call settled against borrowed usage released its reservation: "
            f"spent={outcome.failed_call_priced_spend}, expected={reserve_each}"
        )


#: The one burst this process ran. Two scorecard rows are two questions about
#: the same 120-thread contention run -- ``spend-cap-burst`` asks whether the
#: whole evidence vector held, ``bench:spend-cap-exact`` asks whether the cap
#: halted at exactly the cap. Provoking a second burst for the second question
#: would double the slowest part of the suite AND let the two rows describe
#: different runs, so a flake could show as one green and one red with nothing to
#: say which burst either was talking about.
_OUTCOME_CACHE: dict[str, BurstOutcome] = {}


def burst_outcome() -> BurstOutcome:
    """The process-wide burst outcome, run on first use."""
    if "outcome" not in _OUTCOME_CACHE:
        _OUTCOME_CACHE["outcome"] = run_burst()
    return _OUTCOME_CACHE["outcome"]


def reset_burst_cache() -> None:
    """Drop the cached burst. For a test that must provoke a second one."""
    _OUTCOME_CACHE.clear()


def check_spend_cap_burst() -> CheckResult:
    """Scorecard row: run the burst, PASS only on the whole vector."""
    started = datetime.now(tz=UTC)
    outcome = burst_outcome()
    elapsed = (datetime.now(tz=UTC) - started).total_seconds()
    detail = f"{outcome.vector()} elapsed={elapsed:.1f}s"
    if outcome.ok:
        return CheckResult.passed(CHECK_NAME, detail)
    return CheckResult.failed(CHECK_NAME, f"{'; '.join(outcome.failures)} | {detail}")


# ===========================================================================
# ops-side reads and teardown
# ===========================================================================
def _spent(scope: str) -> int:
    with get_engine().connect() as conn:
        return int(
            conn.execute(
                text("SELECT spent_microusd FROM budget_ledger WHERE scope = :s"), {"s": scope}
            ).scalar_one()
        )


def _reservation_count(scope: str) -> int:
    with get_engine().connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM budget_reservations WHERE scope = :s"), {"s": scope}
            ).scalar_one()
        )


def _open_count(scope: str) -> int:
    with get_engine().connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM budget_reservations WHERE scope = :s AND state = 'open'"
                ),
                {"s": scope},
            ).scalar_one()
        )


def _audit_count(action: str, subject: str) -> int:
    with get_engine().connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM audit_log WHERE action = :a AND subject = :s"),
                {"a": action, "s": subject},
            ).scalar_one()
        )


def _backstop_present() -> bool:
    with get_engine().connect() as conn:
        return bool(
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'ck_budget_spent_within_cap' AND contype = 'c'"
                )
            ).scalar_one()
        )


def _ledger_violations() -> int:
    with get_engine().connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM budget_ledger WHERE spent_microusd > cap_microusd")
            ).scalar_one()
        )


def _cleanup(*scopes: str) -> None:
    """Delete the harness's own rows. Ops principal, its own scopes only.

    The halt markers matter here as much as the reservations: an overspend phase
    that left its ``budget_scope_halted`` row behind would halt a scope name
    nobody will ever use again, but it would also leave the audit log claiming a
    production incident that never happened (R18).
    """
    with get_engine().begin() as conn:
        for scope in scopes:
            conn.execute(text("DELETE FROM budget_reservations WHERE scope = :s"), {"s": scope})
            conn.execute(text("DELETE FROM audit_log WHERE subject = :s"), {"s": scope})
            conn.execute(
                text("DELETE FROM audit_log WHERE subject LIKE :p"), {"p": "suite-burst-%"}
            )
            conn.execute(text("DELETE FROM budget_ledger WHERE scope = :s"), {"s": scope})
