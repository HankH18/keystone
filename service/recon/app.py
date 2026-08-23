"""FastAPI application factory.

Routers are mounted here and defined next to the code they expose, so a module
owns both its behaviour and its HTTP surface: `/health` lives in `recon.health`
(real DB + per-source probes, all bounded), `/internal/ingest/*` lives in
`recon.ingest` (validated landing, RFC7807 rejections), `/internal/sync` +
`/internal/reconcile` live in `recon.api.internal` (the R19 cron triggers),
`/api/entities*` lives in `recon.api.entities` (R10's unified cross-source view
and R20's per-row scope filter) and `/api/conflicts*` + `/api/proposals*` live
in `recon.api.review` (R11's reviewer surface and R24's apply path) and
`/api/scorecard` lives in `recon.api.scorecard` (the latest `python -m
recon.suite` results, which the dashboard's overview reconciles against).

**A router that is not mounted here does not exist.** Two of them were built,
tested and left unreachable, because the tests that covered them imported the
router object directly (or mounted it in a `conftest.py`) and so passed against
a surface the running service never served. `tests/integration/test_route_table.py`
asserts the route table of *this* factory against the endpoints DESIGN §HTTP API
pins, so the next router cannot be built and left dangling the same way.

**The HTTP response body is a sink too.** FastAPI's own
`RequestValidationError` handler serialises pydantic's `input` member, which is
the entire rejected object -- so a payload error answered a caller with the
record it had just refused, personal data included, through a channel none of
the log-side controls touch. R2/R3 grade that a bad payload gets a structured
4xx, not that the 4xx is verbose, so the handler installed below keeps the
structure (loc, type, msg) and drops the echo. See `_validation_handler`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from recon import __version__
from recon.api.auth import install_problem_handler
from recon.api.entities import router as entities_router
from recon.api.internal import JOB_SYNC, register_job_handler, sync_job
from recon.api.internal import router as internal_router
from recon.api.review import router as review_router
from recon.api.scorecard import router as scorecard_router
from recon.health import SERVICE_NAME
from recon.health import router as health_router
from recon.ingest import router as ingest_router
from recon.logging import configure_logging_once, get_logger
from recon.privacy import redact, scrub_text

__all__ = ["SERVICE_NAME", "VALIDATION_PROBLEM_TYPE", "create_app"]

log = get_logger("recon.app")

#: RFC7807 `type` for a rejected request envelope.
VALIDATION_PROBLEM_TYPE = "https://keystone.invalid/problems/invalid_request"

#: The members of a pydantic error that are safe to return: where the error is,
#: what kind it is, and what it says. **`input` and `ctx` are deliberately
#: absent** -- `input` is the offending value (for a missing top-level field it
#: is the whole body), and `ctx` can quote it too.
_SAFE_ERROR_MEMBERS = ("loc", "type", "msg")


def _validation_error(error: Any) -> dict[str, Any]:
    """One pydantic error, reduced to its non-echoing members and scrubbed.

    `msg` is pydantic's own English, which for some validators quotes the value
    it rejected, so it goes through the redactor's free-text scrub rather than
    being trusted. `loc` is a path of field names and list indices; each part is
    judged by :func:`_loc_part`.
    """
    reduced: dict[str, Any] = {}
    for member in _SAFE_ERROR_MEMBERS:
        if member not in error:
            continue
        value = error[member]
        if member == "msg":
            reduced[member] = scrub_text(str(value))
        elif member == "loc":
            reduced[member] = [_loc_part(part) for part in value]
        else:
            reduced[member] = str(value)
    return reduced


def _loc_part(part: Any) -> str:
    """One element of a pydantic `loc`, judged as a field NAME, not as a value.

    A `loc` is a path: field names and list indices. Names go through the
    redactor in **key** position, so a name on the committed vocabulary survives
    (`body`, `records`, `generation`) and one the vocabulary has never met comes
    back as a token instead of being echoed -- a `loc` can address a key from the
    payload itself, and a payload key can be a datum. An index is a position, not
    personal data, and is kept so `records[3]` still tells the caller which line
    was refused.
    """
    text = str(part)
    if isinstance(part, int) or text.isdigit():
        return text
    return next(iter(redact({text: None})))


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    """422 as an RFC7807 problem that names the fields but quotes no value."""
    assert isinstance(exc, RequestValidationError)
    errors = [_validation_error(error) for error in exc.errors()]
    log.warning(
        "http.request_invalid",
        path=str(request.url.path),
        status=422,
        count=len(errors),
        rejections=[error.get("type", "invalid") for error in errors],
    )
    return JSONResponse(
        status_code=422,
        media_type="application/problem+json",
        content={
            "type": VALIDATION_PROBLEM_TYPE,
            "title": "invalid request",
            "status": 422,
            "detail": (
                f"the request body failed validation in {len(errors)} place(s); "
                f"the rejected values are deliberately not echoed"
            ),
            "errors": errors,
        },
    )


def create_app() -> FastAPI:
    """Build and return the Keystone FastAPI application.

    Logging is configured **here**, before any router is mounted, because this
    factory is what actually runs the service (`make serve`, `uvicorn
    recon.app:create_app --factory`). Nothing else in the process installs the
    structlog chain or the stdlib-logging bridge, so without this call neither
    the redaction processor nor uvicorn's access-log capture is present and
    every `recon.ingest` / `recon.health` event -- and every request line -- is
    rendered unredacted. See `recon.logging.ENTRY_POINTS` and
    `recon.logging.SINKS`.
    """
    configure_logging_once()
    app = FastAPI(title="Keystone reconciliation service", version=__version__)
    install_problem_handler(app)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.include_router(health_router)
    app.include_router(ingest_router)
    # `/internal/sync` and `/internal/reconcile` (R19). The router was defined
    # and never mounted, so both 404'd in the running service while the tests
    # that covered them imported the router directly and passed.
    app.include_router(internal_router)
    # `GET /api/entities/{key}` and `GET /api/entities` (R10, R20). Same defect,
    # one ticket later: the router was complete and its only mount was a pytest
    # fixture, so the unified query endpoint -- Core deliverable #3 -- 404'd
    # everywhere except the test suite.
    app.include_router(entities_router)
    # The reviewer surface (R11) and the apply path (R24): `/api/conflicts*`,
    # `/api/proposals*` and the three decision endpoints. The dashboard is built
    # against exactly these and has been talking to a 404 until now.
    app.include_router(review_router)
    # `GET /api/scorecard` (T-14). DESIGN pins it and the dashboard's overview
    # route is built against it: `dashboard/src/lib/contract.ts` A4 is the body
    # shape, and the route reconciles every conflict-type figure against it. It
    # served 404 until this line existed, which `docs/proposal-policy.md` had
    # already recorded as a known gap rather than a hypothetical one.
    app.include_router(scorecard_router)
    # The sync trigger's body: ingest every generation, then materialize the
    # canonical layer (`recon.api.internal.sync_job`). Bound to *this app*, not
    # to the module-global registry, so building an application never reaches
    # into another one -- see `register_job_handler`.
    register_job_handler(JOB_SYNC, sync_job, app=app)
    return app
