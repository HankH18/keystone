"""`/health`: what is actually reachable right now (R3, DESIGN SSHTTP API).

DESIGN pins `GET /health` as "service + per-source adapter + DB reachability", and
the only version of that worth serving is one that can come back **unhealthy**. A
handler that returns `{"status": "ok"}` unconditionally is not a health check; it
is a liveness check wearing a health check's name, and it is the reason an
operator learns the payments source has been down for six hours from a user.

So every probe here does real work:

* the **database** probe opens a connection and runs `SELECT 1`. Not "is
  `DATABASE_URL` set" -- a configured DSN pointing at a stopped Postgres is
  exactly the case the probe exists to catch;
* each **source** probe goes through the adapter port: it lists the source's
  generations, then reads and validates the first record of the newest one. A
  source whose snapshot is missing, empty, unreadable or structurally broken
  reports `down` or `degraded`, with the reason.

And every probe is bounded. `/health` is the endpoint a load balancer calls, so
it is the last endpoint allowed to hang: each probe runs on a worker thread with
its own deadline, and a probe that blows it is reported as `timeout` with its
latency rather than being waited on. The whole handler is therefore bounded by
`timeout` regardless of how badly a source misbehaves -- including a source that
never returns at all.

The bound is **configuration**, not a constant. It was
``HEALTH_PROBE_TIMEOUT_SECONDS = 2.0`` at module scope with no override, and
2.0s is shorter than a cold start of a scale-to-zero Postgres -- so the first
probe after an idle period times out, `/health` answers 503, and a platform that
gates a deploy on `/health` (`infra/render.yaml` sets `healthCheckPath`, and
Render's blueprint spec offers no health-check timeout of its own) never routes
traffic at all. `Settings.health_probe_timeout_seconds`, i.e. the environment
variable ``HEALTH_PROBE_TIMEOUT_SECONDS``, is that override; :func:`health_probe_timeout`
resolves it. The default is unchanged, so nothing moves locally.

It is resolved **per call**, not captured in a default argument: a default
argument is evaluated once at import, which would have made the variable
readable and inert -- a knob that cannot fail is the same defect one layer up.

Status vocabulary, smallest that carries the distinction:
``ok`` | ``degraded`` (answering, but not fully) | ``down`` | ``timeout`` |
``unconfigured`` (nothing to reach: no `DATABASE_URL`).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from recon import __version__
from recon.adapters import AdapterError, ReadOnlyAdapter, build_adapters, read_bounded
from recon.config import get_settings
from recon.db import DatabaseNotConfigured, get_engine

__all__ = [
    "SERVICE_NAME",
    "health_probe_timeout",
    "health_report",
    "probe_database",
    "probe_source",
    "router",
]

log = structlog.get_logger("recon.health")

SERVICE_NAME = "keystone"

_OK = "ok"
_DEGRADED = "degraded"
_DOWN = "down"
_TIMEOUT = "timeout"
_UNCONFIGURED = "unconfigured"

#: Worst-to-best, so an overall status is a max() over its parts.
_SEVERITY: dict[str, int] = {
    _OK: 0,
    _UNCONFIGURED: 1,
    _DEGRADED: 2,
    _TIMEOUT: 3,
    _DOWN: 4,
}

#: Anything at or above this severity means the service cannot do its job.
_FATAL = _SEVERITY[_TIMEOUT]


def health_probe_timeout() -> float:
    """The per-probe wall-clock bound, in seconds, from the environment.

    ``HEALTH_PROBE_TIMEOUT_SECONDS`` (`Settings.health_probe_timeout_seconds`),
    defaulting to `recon.config.DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS` -- the 2.0
    this module used to hardcode. Deliberately far below the adapter's 10s read
    bound by default: a health check that takes ten seconds has already failed
    its purpose. A deployment in front of a scale-to-zero Postgres raises it,
    because a cold start it cannot control otherwise reads as `down`.

    Called at probe time rather than bound to a default argument, so the
    variable is honoured by the process that reads it and not by whichever
    process happened to import this module first.
    """
    return get_settings().health_probe_timeout_seconds


def _bounded(probe: Callable[[], dict[str, Any]], timeout: float) -> dict[str, Any]:
    """Run one probe with a hard wall-clock bound.

    A probe that overruns is *abandoned*, not awaited: the worker is a daemon and
    the caller returns a `timeout` result with the real elapsed time. CPython
    cannot cancel a thread, so a wedged probe leaks one daemon thread until its
    call returns -- what is guaranteed is that `/health` answers anyway.
    """
    outcome: dict[str, Any] = {}
    started = time.monotonic()

    def target() -> None:
        try:
            outcome.update(probe())
        except Exception as exc:  # a probe never propagates; it reports
            outcome.update({"status": _DOWN, "detail": f"{type(exc).__name__}: {exc}"})

    worker = threading.Thread(target=target, name="health-probe", daemon=True)
    worker.start()
    worker.join(timeout)
    elapsed_ms = (time.monotonic() - started) * 1000.0

    if worker.is_alive():
        return {
            "status": _TIMEOUT,
            "detail": f"probe exceeded its {timeout:g}s bound",
            "latency_ms": round(elapsed_ms, 3),
        }
    result = dict(outcome) or {"status": _DOWN, "detail": "probe produced no result"}
    result["latency_ms"] = round(elapsed_ms, 3)
    return result


def probe_database(timeout: float | None = None) -> dict[str, Any]:
    """Open a connection and run `SELECT 1`. Bounded by :func:`health_probe_timeout`."""
    if timeout is None:
        timeout = health_probe_timeout()

    def check() -> dict[str, Any]:
        try:
            engine = get_engine()
        except DatabaseNotConfigured:
            return {"status": _UNCONFIGURED, "detail": "DATABASE_URL is not set"}
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": _OK}

    return _bounded(check, timeout)


def _probe_via_port(adapter: ReadOnlyAdapter, stall_timeout: float) -> dict[str, Any]:
    """Fallback probe for any adapter: list generations, read one record.

    Used when an adapter offers no cheaper `probe()` of its own. It exercises the
    same three members the pipeline uses, so it cannot pass while the real read
    path is broken.

    `stall_timeout` is the caller's own bound, threaded through rather than read
    from the module: this ran under the hardcoded 2.0s while the surrounding
    `_bounded` call ran under whatever the caller asked for, so raising the bound
    would have widened the outer watchdog and left the inner read exactly as
    tight -- an override that appears to work and does not.
    """
    generations = list(adapter.generations())
    if not generations:
        return {"status": _DOWN, "detail": "source reports no generations"}
    latest = max(generations)
    stream = read_bounded(adapter, latest, stall_timeout=stall_timeout, deadline_seconds=None)
    try:
        first = next(stream, None)
    finally:
        stream.close()
    if first is None:
        return {
            "status": _DEGRADED,
            "generations": generations,
            "latest_generation": latest,
            "detail": "newest generation is empty",
        }
    return {
        "status": _OK,
        "generations": generations,
        "latest_generation": latest,
        "sample_ref": f"{first.source_id}:{first.entity_type}:{first.natural_key}",
    }


def probe_source(adapter: ReadOnlyAdapter, timeout: float | None = None) -> dict[str, Any]:
    """Reachability of one source, through the adapter port. Bounded."""
    # Resolved once, into a local the closure captures, so the inner adapter read
    # and the outer watchdog are provably the same number rather than two reads
    # that could disagree.
    bound = health_probe_timeout() if timeout is None else timeout

    def check() -> dict[str, Any]:
        own_probe = getattr(adapter, "probe", None)
        try:
            return own_probe() if callable(own_probe) else _probe_via_port(adapter, bound)
        except AdapterError as error:
            return {
                "status": _TIMEOUT if error.kind == "source_timeout" else _DOWN,
                "detail": error.detail,
                "kind": error.kind,
                "upstream_status": error.upstream_status,
                "problem_status": error.status,
            }

    result = _bounded(check, bound)
    return {key: value for key, value in result.items() if value is not None}


def health_report(
    adapters: Mapping[str, ReadOnlyAdapter] | None = None,
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """The full `/health` document: service, DB, and every source."""
    if timeout is None:
        timeout = health_probe_timeout()
    if adapters is None:
        try:
            adapters = build_adapters()
        except Exception as exc:  # a broken adapter registry is itself a health fact
            adapters = {}
            log.error("health.adapters_unavailable", detail=f"{type(exc).__name__}: {exc}")

    checks: dict[str, Any] = {"database": probe_database(timeout)}
    sources: dict[str, Any] = {
        source_id: probe_source(adapter, timeout) for source_id, adapter in sorted(adapters.items())
    }
    checks["sources"] = sources

    severities = [_SEVERITY.get(checks["database"]["status"], _SEVERITY[_DOWN])]
    severities += [_SEVERITY.get(result["status"], _SEVERITY[_DOWN]) for result in sources.values()]
    if not sources:
        severities.append(_SEVERITY[_DOWN])
    worst = max(severities)
    if worst == 0:
        status = _OK
    elif worst >= _FATAL:
        status = _DOWN
    else:
        status = _DEGRADED

    return {
        "status": status,
        "service": SERVICE_NAME,
        "version": __version__,
        "checks": checks,
    }


router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> JSONResponse:
    """Service, DB and per-source reachability. Answers even when everything is down."""
    report = health_report()
    status_code = 503 if report["status"] == _DOWN else 200
    return JSONResponse(status_code=status_code, content=report)
