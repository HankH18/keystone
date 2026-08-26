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
    # DESIGN writes these as `{id}`; FastAPI's template carries the parameter's
    # own name, and `recon.api.review` names it `proposal_id` because the module
    # also serves `/api/conflicts/{conflict_id}` and one-letter path params are
    # how two endpoints end up sharing a handler by accident.
    ("POST", "/api/proposals/{proposal_id}/approve"): "R11 -- reviewer decision",
    ("POST", "/api/proposals/{proposal_id}/reject"): "R11 -- reviewer decision",
    ("POST", "/api/proposals/{proposal_id}/apply"): "R24 -- the auto-apply path",
    ("GET", "/api/incidents"): "R25 -- stretch #8 incident clusters",
    ("GET", "/api/scorecard"): "R11 -- dashboard reconciliation",
}

#: Pinned by DESIGN, not built by any ticket yet. Each entry names the module
#: that would own it. **This is a statement about the repository, not a waiver**:
#: `test_the_unbuilt_endpoints_are_still_unbuilt` fails the moment one appears.
NOT_BUILT_YET: dict[tuple[str, str], str] = {
    # `/api/scorecard` was here until T-14 built `recon.api.scorecard` and mounted
    # it; this file's own rule is that the line goes when the endpoint arrives, so
    # that the list cannot become a graveyard excusing a missing mount.
    #
    # `/api/incidents` was here until `recon.api.incidents` was mounted in
    # `create_app`. It went the way this module's docstring says it must: the
    # router was built (R25, stretch #8), `test_the_unbuilt_endpoints_are_still_
    # unbuilt` went red, and the line was deleted rather than the mount being
    # left dangling -- so `test_every_built_design_endpoint_is_mounted` guards it
    # from now on, and `test_the_incidents_router_is_served_by_the_factory` binds
    # it to the router object itself.
    #
    # The dict is now EMPTY, which is the end state this file was written for:
    # every endpoint DESIGN §HTTP API pins is built and mounted. An empty
    # parametrize makes `test_the_unbuilt_endpoints_are_still_unbuilt` a single
    # skip, and nothing is weakened by that -- the endpoints it used to exempt
    # are all being asserted by the other direction now. A future DESIGN endpoint
    # that is pinned but not built goes here, with its reason, on the day it is
    # pinned.
}

#: Mounted, and not in DESIGN's list because DESIGN describes the client API:
#: these are the landing endpoints `recon.ingest` owns (R2) and the entity index
#: the scope rule needs a 403 case for (R20).
ALSO_EXPECTED: dict[tuple[str, str], str] = {
    ("POST", "/internal/ingest/records"): "R2 -- validated landing of a payload batch",
    ("GET", "/api/entities"): "R20 -- org-wide index, admin scope only",
    # The dashboard's detail routes. DESIGN pins the collection endpoints and the
    # per-id POST actions but no per-id GET; `dashboard/src/lib/contract.ts`
    # records that as ASSUMED item A2 and builds the conflict- and
    # proposal-detail pages on it, so T-11 serves them.
    ("GET", "/api/conflicts/{conflict_id}"): "contract.ts A2 -- conflict detail",
    ("GET", "/api/proposals/{proposal_id}"): "contract.ts A2 -- proposal detail",
    # DESIGN §HTTP API lists approve/reject/apply and pins `proposal_events` as
    # "the rollback path" under §Data models, but never spells the reversal as an
    # endpoint -- so `recon.apply.rollback_proposal` was reachable only from an
    # interpreter holding the `apply_writer` credentials. R24 requires a *recorded
    # rollback path* and the rubric requires the automation to be reversible; a
    # reversal a reviewer cannot invoke is neither. Classified here rather than in
    # DESIGN_ENDPOINTS because DESIGN's own list does not name it.
    ("POST", "/api/proposals/{proposal_id}/rollback"): "R24 -- the reversal leg, over HTTP",
    ("GET", "/api/audit"): "Core #6 -- the action log, admin scope, redacted (recon.api.audit)",
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


def test_the_incidents_router_is_served_by_the_factory() -> None:
    """The third instance of the same defect, pinned the same way.

    `recon.api.incidents` was built for R25 (stretch #8) and its only mount was
    `tests/incidents/conftest.py`, so `GET /api/incidents` 404'd in the running
    service while that package was green -- exactly the shape of the two routers
    above. Asserted against the freshly imported router object rather than
    against the literal in :data:`DESIGN_ENDPOINTS`, so renaming the path in that
    module cannot make this pass by accident.
    """
    from recon.api.incidents import router as incidents_router

    declared = {
        (method, route.path)
        for route in incidents_router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if method not in {"HEAD", "OPTIONS"}
    }
    assert declared, "recon.api.incidents declares no routes at all"
    served = route_table(create_app())
    assert declared <= served, (
        f"recon.api.incidents declares {sorted(declared - served)} which the real "
        f"application does not serve: the router is built and not mounted"
    )
