"""`GET /api/incidents` -- R25's semantic incident clusters (stretch #8).

    GET /api/incidents        (paginated, admin scope)

DESIGN §HTTP API pins the endpoint as "`GET /api/incidents` (stretch #8
clusters)". It answers with `recon.api.review`'s envelope and nothing new:
`{items, page, page_size, total}`, `page` 1-based, `page_size` clamped to
:data:`recon.api.review.MAX_PAGE_SIZE`. Those two constants are **imported**
from that module rather than restated, because the failure mode of restating
them is that one endpoint caps at 100 and the next caps at 50 and the dashboard
believes both.

One item is one incident: its label, the model that embedded it, and its member
conflicts with the cosine distance that put each one there. `member_count` is
the true membership even when the `members` array is truncated
(:data:`MEMBERS_PER_INCIDENT`) -- a UI that read `members.length` as the size
would under-report an incident of 500 as one of 20, and it would do it quietly.

Admin scope, and why this one is 403 rather than 404
-----------------------------------------------------
`recon.api.review` serves a row a key may not see as **404**, because
distinguishing "does not exist" from "not yours" hands out a membership oracle.
That rule is about *rows*. An incident is not a row about one tenant: it is an
org-wide aggregate over every tenant's conflicts, and its `member_count` leaks
cross-tenant volume even when every member is filtered away. So this endpoint
follows the other org-wide aggregates in this service -- `GET /api/entities`
(the index) and `GET /api/scorecard` -- and requires `admin`, answering a client
key with 403: your key is fine, your scope is not.

Serving only the newest clustering pass
---------------------------------------
`incidents` has no `run_id` column and `recon_writer` holds no DELETE on it, so
clustering runs **accumulate**. `recon.incidents.read_incidents` therefore
serves the newest batch, identified by the `created_at` every incident in one
pass shares (one pass, one transaction, one `now()`). An empty table is an empty
page with `total = 0`, not an error: a service that has never clustered has no
incidents, which is a fact and not a fault.

Mounted, and what pins that
---------------------------
`recon/app.py` includes this router (`app.include_router(incidents_router)`), so
`GET /api/incidents` is served by the real `create_app()` and not only by the
copy `tests/incidents/conftest.py` mounts for itself.
`tests/integration/test_route_table.py::
test_the_incidents_router_is_served_by_the_factory` asserts that against the
factory's own route table, and the endpoint is out of that file's
`NOT_BUILT_YET` list -- which is now empty.

This section said the opposite until 2026-08-24: it was written while the mount
really was missing and left standing after the line was added. That is the same
defect in the other direction -- a shipped file describing a state the tree is no
longer in -- so what keeps this paragraph honest is the test named above, not the
paragraph.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from recon.api.auth import SCOPE_ADMIN, Principal, require_api_key
from recon.api.review import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from recon.db import get_engine
from recon.incidents import read_incidents
from recon.logging import get_logger

__all__ = ["MEMBERS_PER_INCIDENT", "router"]

log = get_logger("recon.api.incidents")

router = APIRouter(prefix="/api", tags=["incidents"])

#: Member conflicts embedded in one incident item. The biggest incident in the
#: committed golden set has 500 members; inlining them all would make a page of
#: 25 incidents a multi-megabyte response. `member_count` always reports the
#: true size, and `/api/conflicts` is where a reviewer pages the rest.
MEMBERS_PER_INCIDENT: Final = 20


def _page_params(page: int, page_size: int) -> tuple[int, int]:
    """`(limit, offset)` for a 1-based page. Clamped, never trusted.

    The same two lines as `recon.api.review._page_params`, deliberately not
    imported from it: that name is private to a module another ticket owns, and
    reaching into it means a rename there breaks this endpoint at import time.
    `tests/incidents/test_api_contract.py` asserts the two agree on every page
    request the API can express, so the copy cannot drift silently either.
    """
    size = max(1, min(page_size, MAX_PAGE_SIZE))
    return size, (max(1, page) - 1) * size


@router.get("/incidents")
def list_incidents(
    principal: Annotated[Principal, Depends(require_api_key(SCOPE_ADMIN))],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    members: int = Query(
        default=MEMBERS_PER_INCIDENT,
        ge=1,
        le=MAX_PAGE_SIZE,
        description="member conflicts inlined per incident; member_count is always the true size",
    ),
) -> JSONResponse:
    """R25's clusters, biggest first, each with its member conflicts.

    Ordering is `member_count DESC, id` -- the incident that is 500 conflicts is
    the one a reviewer needs to see before they notice the pattern themselves,
    which is what R25 asks for. `id` breaks the tie, and incident ids are
    allocated in cluster order (fingerprint order), so the page is stable across
    identical runs.
    """
    limit, offset = _page_params(page, page_size)
    with get_engine().connect() as conn:
        items, total = read_incidents(
            conn, limit=limit, offset=offset, members_per_incident=members
        )
    log.info(
        "incidents.listed",
        scope=principal.scope,
        returned=len(items),
        total=total,
        status_code=200,
    )
    return JSONResponse(
        status_code=200,
        # A1's envelope and nothing else -- no `warnings` member, for the reason
        # `recon.api.review._page` gives: that is the client's verdict about this
        # response, not the service's.
        content={"items": items, "page": page, "page_size": limit, "total": total},
    )
