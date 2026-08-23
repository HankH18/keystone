"""``GET /api/scorecard`` -- the latest suite results, for dashboard reconciliation.

DESIGN pins the endpoint ("latest suite results, for reconciliation") and the
dashboard pins the body: ``dashboard/src/lib/contract.ts`` assumption **A4**,
``{generated_at, run_id, conflicts:{total,by_type}, proposals:{total,by_status},
checks}``. The Overview route fetches every conflict-type figure twice -- once
from here and once as the ``total`` of ``/api/conflicts?type=..`` -- and prints
"Mismatch" on any row where the two disagree.

**Admin scope.** This is an org-wide operational surface: it reports what a run
found across every tenant, so a client key must not read it. R20's per-row filter
does not apply -- there is no row here to filter -- which makes this one of the
operations a scope genuinely gates, and the answer to a client key is 403.

What is served, and why it is a file
--------------------------------------
The body is the artifact ``python -m recon.suite`` wrote (``docs/scorecard.json``),
served as it was written. It is deliberately **not** recomputed from the
database: the reconciliation the dashboard performs is only meaningful because
this side is the harness's record of a run and the other side is the service's
live view. Recomputing here would compare a query with itself and print "Match"
forever.

The corollary is that the numbers can be **stale**, and staleness has to be
visible rather than implied -- so the response carries ``generated_at`` from the
run itself and ``artifact_modified_at`` from the file's mtime, and a body that
cannot be found or parsed is a loud RFC7807 problem, never an empty scorecard
with zeroes in it. Zeroes would render as a clean overview reporting that nothing
is wrong.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from recon.api.auth import SCOPE_ADMIN, Principal, problem, require_api_key
from recon.logging import get_logger
from recon.suite.report import SCORECARD_JSON, scorecard_dir

__all__ = ["SCORECARD_PROBLEM", "load_scorecard", "router", "scorecard_path"]

log = get_logger("recon.api.scorecard")

router = APIRouter(prefix="/api", tags=["scorecard"])

#: RFC7807 `type` slug when no readable scorecard artifact exists.
SCORECARD_PROBLEM = "scorecard-unavailable"

#: The A4 keys the dashboard reads. A body missing any of them would render as a
#: broken overview, so the endpoint refuses to serve one.
REQUIRED_KEYS = ("generated_at", "run_id", "conflicts", "proposals", "checks")


def scorecard_path() -> Path:
    """Where ``python -m recon.suite`` writes the machine-readable scorecard."""
    return scorecard_dir() / SCORECARD_JSON


def load_scorecard() -> dict[str, Any]:
    """Read and shape-check the artifact. Raises ``FileNotFoundError``/``ValueError``."""
    path = scorecard_path()
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    missing = [key for key in REQUIRED_KEYS if key not in body]
    if missing:
        raise ValueError(f"{path} is missing the A4 keys {missing}")
    body["artifact_modified_at"] = _mtime(path)
    return body


def _mtime(path: Path) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(timespec="seconds")


@router.get("/scorecard")
def get_scorecard(
    principal: Annotated[Principal, Depends(require_api_key(SCOPE_ADMIN))],
) -> JSONResponse:
    """The latest ``python -m recon.suite`` scorecard. **Admin scope only.**"""
    try:
        body = load_scorecard()
    except FileNotFoundError:
        log.warning("scorecard.absent", path=str(scorecard_path()), status=503)
        return problem(
            SCORECARD_PROBLEM,
            "no scorecard has been generated",
            503,
            "the verification suite has not written a scorecard on this deployment. "
            "Run `python -m recon.suite`; it writes docs/scorecard.txt and "
            "docs/scorecard.json. An empty scorecard is not served in its place, "
            "because zeroes would render as an overview reporting that nothing is wrong.",
        )
    except (ValueError, json.JSONDecodeError) as exc:
        log.error("scorecard.unreadable", path=str(scorecard_path()), status=503)
        return problem(
            SCORECARD_PROBLEM,
            "the scorecard artifact is unreadable",
            503,
            f"the stored scorecard could not be used: {exc}",
        )

    log.info(
        "scorecard.read",
        scope=principal.scope,
        run_id=str(body.get("run_id")),
        status=200,
    )
    return JSONResponse(status_code=200, content=body)
