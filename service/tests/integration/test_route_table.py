"""The route table of the REAL `create_app()`, against DESIGN §HTTP API.

Two routers in this repository were built, tested and left unmounted. Neither
failure was visible to the suites that covered them, because both suites got the
router from somewhere other than the application:
`tests/triggers/test_internal_endpoints.py` builds a bare `FastAPI()` and includes
`recon.api.internal.router`; `tests/api/conftest.py` calls `create_app()` and then
adds `recon.api.entities.router` itself. Every assertion in both files was true,
and `/internal/sync` and `/api/entities/{key}` both 404'd in the running service.

So this module asserts the one thing those cannot: what the factory the service
actually runs (`uvicorn recon.app:create_app --factory`, `make serve`) serves.
Nothing here imports a router.

Two gates, and they fail in opposite directions on purpose:

* every endpoint DESIGN pins **and this repository has built** must be in the
  table -- a router that exists but is not mounted is the defect;
* every endpoint DESIGN pins that is **not** built yet is listed in
  :data:`NOT_BUILT_YET` with the reason, and must still be absent. The day
  someone builds one, this test goes red until they delete the line -- so the
  list cannot quietly become a graveyard that excuses a missing mount.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from recon.app import create_app

#: Endpoint -> the DESIGN §HTTP API line and the requirement it serves.
#: Paths are FastAPI's own templates, so `{key}` is compared as written.
DESIGN_ENDPOINTS: dict[tuple[str, str], str] = {
    ("POST", "/internal/sync"): "R19 -- cron trigger, per-job shared secret",
    ("POST", "/internal/reconcile"): "R19 -- cron trigger, per-job shared secret",
    ("GET", "/health"): "service + per-source adapter + DB reachability",
    ("GET", "/api/entities/{key}"): "R10 -- the unified cross-source view",
    ("GET", "/api/conflicts"): "R11 -- conflict list, filters, pagination",
    ("GET", "/api/proposals"): "R11 -- proposal list, filters",
    ("POST", "/api/proposals/{id}/approve"): "R11 -- reviewer decision",
    ("POST", "/api/proposals/{id}/reject"): "R11 -- reviewer decision",
    ("POST", "/api/proposals/{id}/apply"): "R24 -- the auto-apply path",
    ("GET", "/api/incidents"): "R25 -- stretch #8 incident clusters",
    ("GET", "/api/scorecard"): "R11 -- dashboard reconciliation",
}

#: Pinned by DESIGN, not built by any ticket yet. Each entry names the module
#: that would own it. **This is a statement about the repository, not a waiver**:
#: `test_the_unbuilt_endpoints_are_still_unbuilt` fails the moment one appears.
NOT_BUILT_YET: dict[tuple[str, str], str] = {
    ("GET", "/api/conflicts"): "no conflicts router exists in recon/api/",
    ("GET", "/api/proposals"): "no proposals router exists in recon/api/",
    ("POST", "/api/proposals/{id}/approve"): "no proposals router exists in recon/api/",
    ("POST", "/api/proposals/{id}/reject"): "no proposals router exists in recon/api/",
    ("POST", "/api/proposals/{id}/apply"): "no proposals router exists in recon/api/",
    ("GET", "/api/incidents"): "no incidents router exists in recon/api/",
    ("GET", "/api/scorecard"): "no scorecard router exists in recon/api/",
}

#: Mounted, and not in DESIGN's list because DESIGN describes the client API:
#: these are the landing endpoints `recon.ingest` owns (R2) and the entity index
#: the scope rule needs a 403 case for (R20).
ALSO_EXPECTED: dict[tuple[str, str], str] = {
    ("POST", "/internal/ingest/records"): "R2 -- validated landing of a payload batch",
    ("GET", "/api/entities"): "R20 -- org-wide index, admin scope only",
}


def api_routes(app: FastAPI) -> list[APIRoute]:
    """Every `APIRoute` the app really serves.

    Walks whatever container FastAPI wraps an included router in rather than
    assuming `app.routes` is flat: an enumeration that silently returns fewer
    routes than the app serves would make every assertion here vacuous. Same walk
    as `tests/triggers/test_single_trigger_guard.py`, for the same reason.
    """

    def walk(routes: object) -> Iterator[APIRoute]:
        for route in routes:  # type: ignore[union-attr]
            if isinstance(route, APIRoute):
                yield route
                continue
            original = getattr(route, "original_router", None)
            if original is not None:
                yield from walk(original.routes)
                continue
            nested = getattr(route, "routes", None)
            if nested:
                yield from walk(nested)

    return list(walk(app.routes))


def route_table(app: FastAPI) -> set[tuple[str, str]]:
    """`{(method, path)}` for everything the app serves."""
    return {
        (method, route.path)
        for route in api_routes(app)
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    }


@pytest.fixture(scope="module")
def table() -> set[tuple[str, str]]:
    return route_table(create_app())


@pytest.mark.parametrize(
    ("endpoint", "why"),
    sorted(
        (
            (endpoint, why)
            for endpoint, why in DESIGN_ENDPOINTS.items()
            if endpoint not in NOT_BUILT_YET
        ),
        key=lambda item: item[0],
    ),
)
def test_every_built_design_endpoint_is_mounted(
    table: set[tuple[str, str]], endpoint: tuple[str, str], why: str
) -> None:
    """A router this repository ships and `create_app()` does not mount is a 404."""
    method, path = endpoint
    assert endpoint in table, (
        f"{method} {path} is pinned by DESIGN §HTTP API ({why}) and the real "
        f"create_app() does not serve it. Mount its router in recon/app.py -- a "
        f"router covered only by a test fixture is unreachable in the service. "
        f"Route table: {sorted(table)}"
    )


@pytest.mark.parametrize(("endpoint", "why"), sorted(ALSO_EXPECTED.items()))
def test_the_other_mounted_endpoints_are_mounted(
    table: set[tuple[str, str]], endpoint: tuple[str, str], why: str
) -> None:
    method, path = endpoint
    assert endpoint in table, f"{method} {path} ({why}) is missing: {sorted(table)}"


@pytest.mark.parametrize(("endpoint", "why"), sorted(NOT_BUILT_YET.items()))
def test_the_unbuilt_endpoints_are_still_unbuilt(
    table: set[tuple[str, str]], endpoint: tuple[str, str], why: str
) -> None:
    """The other direction: this list must shrink by being built, never by rotting.

    If this fails, the endpoint now exists -- delete its line from `NOT_BUILT_YET`
    so `test_every_built_design_endpoint_is_mounted` starts guarding it.
    """
    method, path = endpoint
    assert endpoint not in table, (
        f"{method} {path} is now mounted, but it is still listed as not built "
        f"({why}). Remove it from NOT_BUILT_YET so the mount is guarded."
    )


def test_the_app_serves_nothing_the_table_does_not_account_for(
    table: set[tuple[str, str]],
) -> None:
    """Derived, not recited: a route added tomorrow must be classified here.

    The failure mode this catches is the mirror of an unmounted router -- an
    endpoint that ships without ever appearing in a contract document.
    """
    accounted = (set(DESIGN_ENDPOINTS) - set(NOT_BUILT_YET)) | set(ALSO_EXPECTED)
    unexpected = table - accounted
    assert not unexpected, (
        f"create_app() serves {sorted(unexpected)}, which DESIGN §HTTP API does not "
        "pin and this module does not classify. Add it to DESIGN and to "
        "DESIGN_ENDPOINTS (or to ALSO_EXPECTED with the requirement it serves)."
    )


def test_the_two_previously_unmounted_routers_are_the_ones_this_guards() -> None:
    """The regression, named. Both routers exist; both must come from the factory.

    Asserted against freshly imported router objects rather than against the
    literals above, so renaming a path in either module cannot make this pass by
    accident.
    """
    from recon.api.entities import router as entities_router
    from recon.api.internal import router as internal_router

    served = route_table(create_app())
    for name, router in (("entities", entities_router), ("internal", internal_router)):
        declared = {
            (method, route.path)
            for route in router.routes
            if isinstance(route, APIRoute)
            for method in route.methods
            if method not in {"HEAD", "OPTIONS"}
        }
        assert declared <= served, (
            f"recon.api.{name} declares {sorted(declared - served)} which the real "
            f"application does not serve: the router is built and not mounted"
        )
