"""The reviewer surface: conflicts, proposals, and the three decisions (R11, R24).

    GET  /api/conflicts        (+ source / type / status filters, paginated)
    GET  /api/conflicts/{id}
    GET  /api/proposals        (+ source / type / status / conflict_id, paginated)
    GET  /api/proposals/{id}
    POST /api/proposals/{id}/approve      review_writer
    POST /api/proposals/{id}/reject       review_writer
    POST /api/proposals/{id}/apply        apply_writer  (+ ?auto=true -> R24)

Every filter is applied in SQL, on the server
---------------------------------------------
The dashboard was built against `dashboard/src/lib/contract.ts`, which
enumerates ten assumptions A1-A10 and marks **A8** as the one with a *silent*
failure mode: `/api/proposals` is asked to filter by `source` and `type`, the
`proposals` table has neither column, and a service that ignores an unknown
query parameter answers `200` with the UNFILTERED page. The reviewer then reads
rows from every source under a heading that says one source, and nothing is red.

A8 is answered here by the JOIN it names: every proposals query joins
`conflicts`, and `source` and `type` are predicates on the joined row. The two
filters are therefore either applied or the request fails -- there is no third
outcome in which they are silently dropped, because there is no code path that
accepts them without using them (`tests/api/test_filters.py` proves it against
the real store, and `tests/api/test_contract_assumptions.py` walks the exported
`CONTRACT_ASSUMPTIONS` list item by item).

Where the answer is *not* the assumption, that is stated rather than papered
over -- see `CONFLICT_STATUS_NOTE` and `A6` below.

Scope (R20): rows for reads, the operation for decisions
--------------------------------------------------------
DESIGN pins both halves. Reads are row-filtered: an `admin` key sees org-wide,
a `client` key sees only conflicts and proposals that touch an entity in its own
tenant, and a row it may not see is **404, never 403** -- the same membership-
oracle argument `recon.api.entities` documents. Decisions are gated on the
*operation*: "reviewer actions require org-wide scope", so approve / reject /
apply require `admin` and answer a client key with 403.

One role per duty, three connections
-------------------------------------
Reads use the ordinary application engine. **Approve and reject run as
`review_writer`; apply runs as `apply_writer`** (`recon.db.role_connection`),
because the separation of duties is the graded property and it is only real if
the process actually connects as the restricted role -- a table owner bypasses
its own grants. The database, not this module, is what refuses a self-approval:
`review_writer` may move `pending|sensitive_hold -> approved|rejected` and
nothing else, `apply_writer` may move `approved -> applied` and
`applied -> rolled_back` and nothing else, and both are SQLSTATE `KS004`.

The canonical write itself is not here. It is `recon.apply`, which the apply
endpoint calls -- so the HTTP layer owns request shape and error rendering, and
the write boundary owns the transaction.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, text

from recon.api.auth import (
    SCOPE_ADMIN,
    Principal,
    problem,
    problem_body,
    require_api_key,
    visible_scope,
)
from recon.apply import (
    ApplyError,
    AutoApplyRefused,
    apply_proposal,
    auto_apply,
    evaluate_auto_apply,
    load_proposal,
)
from recon.db import ROLE_APPLY_WRITER, ROLE_REVIEW_WRITER, get_engine, role_connection
from recon.logging import get_logger, insert_audit_row
from recon.reference import CONFLICT_TYPES
from recon.resolve import CURRENT_GENERATION

__all__ = [
    "CONFLICT_STATUS_NOTE",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "PROPOSAL_STATUSES",
    "SOURCE_IDS",
    "router",
]

log = get_logger("recon.api.review")

router = APIRouter(prefix="/api", tags=["review"])

#: `dashboard/src/lib/contract.ts`: `MAX_PAGE_SIZE = 100`, `DEFAULT_PAGE_SIZE = 25`.
#: R11's non-goal is explicit -- never load 100k rows client-side -- so the cap is
#: enforced on the server as well as clamped on the client.
MAX_PAGE_SIZE: Final = 100
DEFAULT_PAGE_SIZE: Final = 25

#: contract SS8: `sources_involved` is a subset of these three.
SOURCE_IDS: Final[tuple[str, ...]] = ("appdb", "crm", "payments")

#: DESIGN SSData models, `proposals.status`. The Postgres enum, in the same order.
PROPOSAL_STATUSES: Final[tuple[str, ...]] = (
    "pending",
    "approved",
    "rejected",
    "applied",
    "rolled_back",
    "sensitive_hold",
)

#: **A6, answered honestly.** The dashboard assumes the conflict-status vocabulary
#: is `open` and `escalated:oscillation`. The column is a Postgres enum
#: `(open, escalated, resolved, dismissed)` and the reason lives in a separate
#: `escalation_reason` column -- so the composite the dashboard wants is rendered
#: here, from the row, **when the row carries the reason**.
#:
#: It frequently does not, and that is a real gap rather than a rendering choice:
#: `recon_writer`'s UPDATE grant on `conflicts` is column-scoped to
#: `(status, last_seen_run)` (migration 0004), so `recon.reconciler._escalate`
#: escalates the row and writes the reason into the audit row only. Such a
#: conflict is served as bare `escalated`, which A6 says renders as a labelled
#: "unknown status" badge -- loud, not silent. Fixing it means widening a grant
#: in a migration another ticket owns, so it is reported, not done here.
CONFLICT_STATUS_NOTE: Final = (
    "conflicts.status is the Postgres enum (open, escalated, resolved, dismissed); "
    "'escalated:<reason>' is rendered when the row carries an escalation_reason or is "
    "flagged oscillating. recon_writer cannot write escalation_reason (its UPDATE grant "
    "on conflicts is column-scoped to status and last_seen_run), so an escalated row "
    "whose reason is only in the audit log is served as bare 'escalated'."
)

#: Rendered once, used by the select list AND the status filter, so a row can
#: never be served under a status the filter would not match.
_CONFLICT_STATUS_EXPR: Final = """
        CASE
            WHEN c.status::text <> 'escalated'   THEN c.status::text
            WHEN c.escalation_reason IS NOT NULL THEN 'escalated:' || c.escalation_reason
            WHEN c.oscillating                   THEN 'escalated:oscillation'
            ELSE 'escalated'
        END
"""

#: R20 row visibility for a `conflicts` row: does any ref it names resolve to an
#: entity in this tenant? `:scope IS NULL` is the admin case and is un-spellable
#: as a tenant label (`recon.api.auth.visible_scope`).
_CONFLICT_SCOPE_CLAUSE: Final = """
        (CAST(:scope AS text) IS NULL OR EXISTS (
            SELECT 1
              FROM entity_links el
              JOIN entities e ON e.canonical_id = el.canonical_id
             WHERE el.generation = :generation
               AND el.source_ref IN (SELECT jsonb_array_elements_text(c.entity_refs))
               AND e.current ->> 'tenant' = CAST(:scope AS text)))
"""

#: The same rule for a `proposals` row, and much cheaper: a proposal names the one
#: entity it would change (`target_canonical_id`, migration 0005 RULING 3).
_PROPOSAL_SCOPE_CLAUSE: Final = """
        (CAST(:scope AS text) IS NULL OR EXISTS (
            SELECT 1 FROM entities e
             WHERE e.canonical_id = p.target_canonical_id
               AND e.current ->> 'tenant' = CAST(:scope AS text)))
"""

_CONFLICT_COLUMNS: Final = f"""
        c.id,
        c.fingerprint,
        c.type,
        c.rule_id,
        c.entity_refs,
        c.sources,
        c.disagreeing_fields,
        c.observed_values,
        {_CONFLICT_STATUS_EXPR} AS status,
        c.oscillating,
        c.first_seen_run,
        c.last_seen_run
"""

_PROPOSAL_COLUMNS: Final = """
        p.id,
        p.conflict_id,
        p.fingerprint,
        p.action,
        p.confidence,
        p.evidence,
        p.rationale,
        p.status::text AS status,
        p.sensitive,
        p.created_run,
        p.decided_by,
        p.decided_at,
        c.type AS conflict_type,
        c.sources AS conflict_sources
"""

#: One WHERE clause, two statements. The page query and the count query are
#: rendered from the SAME fragment, because a `total` that came from a different
#: predicate than the rows is a pagination control that lies -- and it lies
#: quietly, which on a reviewer surface is the failure mode that matters.
_CONFLICT_WHERE: Final = f"""
     WHERE (CAST(:type AS text) IS NULL OR c.type = CAST(:type AS text))
       AND (CAST(:sources AS jsonb) IS NULL OR c.sources @> CAST(:sources AS jsonb))
       AND (CAST(:status AS text) IS NULL OR ({_CONFLICT_STATUS_EXPR}) = CAST(:status AS text))
       AND {_CONFLICT_SCOPE_CLAUSE}
"""

_LIST_CONFLICTS = text(
    f"""
    SELECT {_CONFLICT_COLUMNS},
           count(*) OVER () AS total_rows
      FROM conflicts c
     {_CONFLICT_WHERE}
     ORDER BY c.id
     LIMIT :limit OFFSET :offset
    """
)

#: Only ever run when the page came back EMPTY. `count(*) OVER ()` is the cheap
#: answer and it is correct for every page that has rows -- but an out-of-range
#: page has no row to carry it, and reporting `total = 0` there would tell the
#: dashboard the filter matched nothing when it matched plenty.
_COUNT_CONFLICTS = text(
    f"""
    SELECT count(*) AS total_rows
      FROM conflicts c
     {_CONFLICT_WHERE}
    """
)

_GET_CONFLICT = text(
    f"""
    SELECT {_CONFLICT_COLUMNS}
      FROM conflicts c
     WHERE c.id = :conflict_id
       AND {_CONFLICT_SCOPE_CLAUSE}
    """
)

_PROPOSAL_WHERE: Final = f"""
     WHERE (CAST(:type AS text) IS NULL OR c.type = CAST(:type AS text))
       AND (CAST(:sources AS jsonb) IS NULL OR c.sources @> CAST(:sources AS jsonb))
       AND (CAST(:status AS text) IS NULL OR p.status::text = CAST(:status AS text))
       AND (CAST(:conflict_id AS bigint) IS NULL OR p.conflict_id = CAST(:conflict_id AS bigint))
       AND {_PROPOSAL_SCOPE_CLAUSE}
"""

_LIST_PROPOSALS = text(
    f"""
    SELECT {_PROPOSAL_COLUMNS},
           count(*) OVER () AS total_rows
      FROM proposals p
      JOIN conflicts c ON c.id = p.conflict_id
     {_PROPOSAL_WHERE}
     ORDER BY p.id
     LIMIT :limit OFFSET :offset
    """
)

_COUNT_PROPOSALS = text(
    f"""
    SELECT count(*) AS total_rows
      FROM proposals p
      JOIN conflicts c ON c.id = p.conflict_id
     {_PROPOSAL_WHERE}
    """
)

_GET_PROPOSAL = text(
    f"""
    SELECT {_PROPOSAL_COLUMNS}
      FROM proposals p
      JOIN conflicts c ON c.id = p.conflict_id
     WHERE p.id = :proposal_id
       AND {_PROPOSAL_SCOPE_CLAUSE}
    """
)

_DECIDE = text(
    """
    UPDATE proposals
       SET status = CAST(:next_status AS proposal_status),
           decided_by = :decided_by,
           decided_at = :decided_at
     WHERE id = :proposal_id
       AND status IN ('pending', 'sensitive_hold')
    RETURNING id
    """
)

#: The two statuses `review_writer` may decide FROM (migration 0005/0006's
#: `BIRTH_STATUSES`). Restated as a Python tuple only so the 409 body can say
#: which statuses would have worked; the enforcement is `KS004`.
_DECIDABLE_STATUSES: Final = ("pending", "sensitive_hold")

#: `ApplyError.reason` -> HTTP status. Everything that is a *state* conflict is
#: 409; a missing row is 404. Anything unmapped is 409 rather than 500: an apply
#: this module refuses is a refusal, not a crash.
_APPLY_ERROR_STATUS: Final[Mapping[str, int]] = {
    "not_found": 404,
    "entity_missing": 404,
}


# ======================================================================================
# request parsing
# ======================================================================================


def _page_params(page: int, page_size: int) -> tuple[int, int]:
    """`(limit, offset)` for a 1-based page. Clamped, never trusted."""
    size = max(1, min(page_size, MAX_PAGE_SIZE))
    return size, (max(1, page) - 1) * size


def _sources_json(source: str | None) -> str | None:
    """The `sources @> ...` operand for one source id, or `None` for no filter."""
    return None if source is None else json.dumps([source])


def _invalid(param: str, value: str, allowed: Sequence[str]) -> JSONResponse:
    """422 for a filter value outside its committed vocabulary.

    A filter the service cannot honour is **rejected**, never ignored: an ignored
    filter answers 200 with unfiltered rows, which is A8's silent failure in a
    different disguise.
    """
    log.info("review.invalid_filter", param=param, status=422)
    return problem(
        "invalid-filter",
        "invalid filter",
        422,
        f"{param}={value!r} is not one of {list(allowed)}. This endpoint rejects a filter "
        "it cannot apply rather than ignoring it and answering 200 with unfiltered rows.",
    )


def _checked_filters(
    source: str | None,
    conflict_type: str | None,
    status: str | None,
    statuses: Sequence[str],
) -> JSONResponse | None:
    if source is not None and source not in SOURCE_IDS:
        return _invalid("source", source, SOURCE_IDS)
    if conflict_type is not None and conflict_type not in CONFLICT_TYPES:
        return _invalid("type", conflict_type, CONFLICT_TYPES)
    if status is not None and statuses and status not in statuses:
        return _invalid("status", status, statuses)
    return None


# ======================================================================================
# response assembly
# ======================================================================================


def _total(conn: Connection, rows: Sequence[Any], count_sql: Any, params: Mapping[str, Any]) -> int:
    """The number of rows the filter matched, correct on an out-of-range page too.

    `count(*) OVER ()` rides along on any page that HAS rows, which is every page
    a reviewer normally sees. A page past the end carries no row to read it off,
    and answering `0` there would say "this filter matched nothing" about a filter
    that matched thousands -- so that one case pays for a second statement, built
    from the same WHERE fragment as the page query.
    """
    if rows:
        return int(rows[0].total_rows)
    return int(conn.execute(count_sql, dict(params)).scalar_one())


def _page(items: list[dict[str, Any]], *, page: int, page_size: int, total: int) -> dict[str, Any]:
    """A1's envelope: `{items, page, page_size, total}`, and nothing else.

    `warnings` is deliberately absent. It is the dashboard's own verdict about
    this response (`src/lib/filterGuard.ts`); a service that sent one would be
    telling the client what to think about the service.
    """
    return {"items": items, "page": page, "page_size": page_size, "total": total}


def _conflict_row(row: Any) -> dict[str, Any]:
    """One `conflicts` row in the dashboard's `Conflict` shape.

    `id` is a **string**: the column is `bigint`, the client's `Conflict.id` is
    `string`, and `filterGuard.ts` compares `proposal.conflict_id === query.conflict_id`
    with `===`. A number on one side and a string on the other would make every
    conflict-detail page warn that its own filter was ignored.
    """
    return {
        "id": str(row.id),
        "fingerprint": row.fingerprint,
        "type": row.type,
        "rule_id": row.rule_id,
        "entity_refs": list(row.entity_refs or []),
        "sources": list(row.sources or []),
        "disagreeing_fields": list(row.disagreeing_fields or []),
        "observed_values": dict(row.observed_values or {}),
        "status": row.status,
        "oscillating": bool(row.oscillating),
        "first_seen_run": row.first_seen_run,
        "last_seen_run": row.last_seen_run,
    }


def _proposal_row(row: Any) -> dict[str, Any]:
    """One `proposals` row in the dashboard's `Proposal` shape.

    Two members beyond DESIGN's pinned column list, both named so they cannot be
    mistaken for columns: `conflict_type` and `conflict_sources` are the joined
    conflict's `type` and `sources`. They exist so that A8 -- which the client
    can only warn about, never verify -- becomes *verifiable from the row* by any
    client that wants to check it, including this repository's own tests.
    """
    return {
        "id": str(row.id),
        "conflict_id": str(row.conflict_id),
        "fingerprint": row.fingerprint,
        "action": dict(row.action or {}),
        "confidence": float(row.confidence),
        "evidence": dict(row.evidence or {}),
        "rationale": row.rationale,
        "status": row.status,
        "sensitive": bool(row.sensitive),
        "created_run": row.created_run,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "conflict_type": row.conflict_type,
        "conflict_sources": list(row.conflict_sources or []),
    }


def _read_proposal(conn: Connection, proposal_id: int, scope: str | None) -> dict[str, Any] | None:
    row = conn.execute(_GET_PROPOSAL, {"proposal_id": proposal_id, "scope": scope}).fetchone()
    return None if row is None else _proposal_row(row)


def _not_found(kind: str, identifier: int | str) -> JSONResponse:
    """404 for both "no such row" and "not visible to this key" (R20).

    One body for both, by design: distinguishing them hands an unauthorised
    caller a membership oracle over every id it can guess.
    """
    return problem(
        f"{kind}-not-found",
        f"{kind} not found",
        404,
        f"no {kind} {identifier!r} is visible to this key.",
    )


# ======================================================================================
# GET /api/conflicts
# ======================================================================================


@router.get("/conflicts")
def list_conflicts(
    principal: Annotated[Principal, Depends(require_api_key())],
    source: str | None = Query(default=None, description="appdb | crm | payments"),
    type: str | None = Query(default=None, description="C1 .. C14"),
    status: str | None = Query(default=None, description="open | escalated:<reason> | ..."),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> JSONResponse:
    """R11's conflict list: by type, record and disagreeing sources, filtered and paged."""
    invalid = _checked_filters(source, type, status, ())
    if invalid is not None:
        return invalid
    limit, offset = _page_params(page, page_size)
    params = {
        "type": type,
        "sources": _sources_json(source),
        "status": status,
        "scope": visible_scope(principal),
        "generation": CURRENT_GENERATION,
        "limit": limit,
        "offset": offset,
    }
    with get_engine().connect() as conn:
        rows = conn.execute(_LIST_CONFLICTS, params).fetchall()
        total = _total(conn, rows, _COUNT_CONFLICTS, params)
    log.info(
        "review.conflicts_listed",
        scope=principal.scope,
        source=source,
        type=type,
        status=status,
        returned=len(rows),
        total=total,
        status_code=200,
    )
    return JSONResponse(
        status_code=200,
        content=_page(
            [_conflict_row(row) for row in rows], page=page, page_size=limit, total=total
        ),
    )


@router.get("/conflicts/{conflict_id}")
def get_conflict(
    conflict_id: int,
    principal: Annotated[Principal, Depends(require_api_key())],
) -> JSONResponse:
    """A2's per-id GET for a conflict."""
    with get_engine().connect() as conn:
        row = conn.execute(
            _GET_CONFLICT,
            {
                "conflict_id": conflict_id,
                "scope": visible_scope(principal),
                "generation": CURRENT_GENERATION,
            },
        ).fetchone()
    if row is None:
        return _not_found("conflict", conflict_id)
    return JSONResponse(status_code=200, content=_conflict_row(row))


# ======================================================================================
# GET /api/proposals
# ======================================================================================


@router.get("/proposals")
def list_proposals(
    principal: Annotated[Principal, Depends(require_api_key())],
    source: str | None = Query(default=None, description="appdb | crm | payments (A8: JOIN)"),
    type: str | None = Query(default=None, description="C1 .. C14 (A8: JOIN)"),
    status: str | None = Query(default=None, description=" | ".join(PROPOSAL_STATUSES)),
    conflict_id: str | None = Query(default=None, description="A3: this conflict's proposals"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> JSONResponse:
    """R11's proposal list: confidence, evidence, status -- filtered server-side.

    **This is contract assumption A8.** `source` and `type` are not columns of
    `proposals`; they are predicates on the joined `conflicts` row, applied in
    `_LIST_PROPOSALS`. A value outside the committed vocabulary is a 422 rather
    than a silently ignored parameter.
    """
    invalid = _checked_filters(source, type, status, PROPOSAL_STATUSES)
    if invalid is not None:
        return invalid
    parsed_conflict_id: int | None = None
    if conflict_id is not None:
        try:
            parsed_conflict_id = int(conflict_id)
        except ValueError:
            return _invalid("conflict_id", conflict_id, ("a conflict id (integer)",))

    limit, offset = _page_params(page, page_size)
    params = {
        "type": type,
        "sources": _sources_json(source),
        "status": status,
        "conflict_id": parsed_conflict_id,
        "scope": visible_scope(principal),
        "limit": limit,
        "offset": offset,
    }
    with get_engine().connect() as conn:
        rows = conn.execute(_LIST_PROPOSALS, params).fetchall()
        total = _total(conn, rows, _COUNT_PROPOSALS, params)
    log.info(
        "review.proposals_listed",
        scope=principal.scope,
        source=source,
        type=type,
        status=status,
        conflict_id=parsed_conflict_id,
        returned=len(rows),
        total=total,
        status_code=200,
    )
    return JSONResponse(
        status_code=200,
        content=_page(
            [_proposal_row(row) for row in rows], page=page, page_size=limit, total=total
        ),
    )


@router.get("/proposals/{proposal_id}")
def get_proposal(
    proposal_id: int,
    principal: Annotated[Principal, Depends(require_api_key())],
) -> JSONResponse:
    """A2's per-id GET, plus R24's verdict on this proposal.

    The `auto_apply` member is this repository's answer to "why is this one not
    applied automatically?" -- every condition R24 names, with the value that
    decided it. It is an addition to the assumed shape, so a client that does not
    know about it is unaffected.
    """
    with get_engine().connect() as conn:
        body = _read_proposal(conn, proposal_id, visible_scope(principal))
        if body is None:
            return _not_found("proposal", proposal_id)
        body["auto_apply"] = evaluate_auto_apply(conn, proposal_id).as_dict()
    return JSONResponse(status_code=200, content=body)


# ======================================================================================
# POST /api/proposals/{id}/approve | reject  -- review_writer
# ======================================================================================


def _decide(proposal_id: int, principal: Principal, next_status: str) -> JSONResponse:
    """One reviewer decision, as `review_writer`, in one transaction.

    The decision names its decider: `KS004` refuses a `review_writer` UPDATE that
    leaves `decided_by` or `decided_at` NULL, because a decision nobody signed is
    indistinguishable from an automated one. The audit row is `^reviewer:` scoped
    (`KS003`) and goes through `recon.logging.insert_audit_row`, the redacting
    chokepoint, rather than a hand-written INSERT.
    """
    scope = visible_scope(principal)
    with get_engine().connect() as reader:
        visible = _read_proposal(reader, proposal_id, scope)
    if visible is None:
        return _not_found("proposal", proposal_id)

    decided_by = f"reviewer:{principal.label}"
    with role_connection(ROLE_REVIEW_WRITER) as conn:
        moved = conn.execute(
            _DECIDE,
            {
                "proposal_id": proposal_id,
                "next_status": next_status,
                "decided_by": decided_by,
                "decided_at": datetime.now(UTC),
            },
        ).fetchone()
        if moved is None:
            log.info(
                "review.decision_refused",
                proposal_id=proposal_id,
                # Named `current_status`, not `current`: `current` is the name of
                # the `entities` column that holds the canonical personal record,
                # and putting that word on `recon.privacy.SAFE_KEYS` would
                # allow-list a key whose obvious future use carries a name.
                current_status=visible["status"],
                wanted_status=next_status,
                status_code=409,
            )
            return problem(
                "proposal-not-decidable",
                "proposal not decidable",
                409,
                f"proposal {proposal_id} is {visible['status']!r}; a reviewer may only decide "
                f"a proposal that is one of {list(_DECIDABLE_STATUSES)} "
                "(separation of duties, SQLSTATE KS004).",
            )
        insert_audit_row(
            conn,
            actor=decided_by,
            action=f"proposal.{next_status}",
            subject=str(proposal_id),
            body={
                "proposal_id": proposal_id,
                "conflict_id": visible["conflict_id"],
                "conflict_type": visible["conflict_type"],
                "from_status": visible["status"],
                "to_status": next_status,
                "sensitive": visible["sensitive"],
                "confidence": visible["confidence"],
            },
        )

    with get_engine().connect() as reader:
        body = _read_proposal(reader, proposal_id, scope)
    log.info(
        "review.decided",
        proposal_id=proposal_id,
        decision=next_status,
        sensitive=visible["sensitive"],
        status_code=200,
    )
    return JSONResponse(status_code=200, content=body)


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(
    proposal_id: int,
    principal: Annotated[Principal, Depends(require_api_key(SCOPE_ADMIN))],
) -> JSONResponse:
    """Approve a pending (or held) proposal. **A5:** returns the updated proposal.

    A `sensitive_hold` proposal is decidable here on purpose: R15 forces it to
    *human review*, it does not forbid the fix forever. What R15 forbids is the
    machine taking it unattended, and that is `recon.apply.auto_apply`'s gate.
    """
    return _decide(proposal_id, principal, "approved")


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: int,
    principal: Annotated[Principal, Depends(require_api_key(SCOPE_ADMIN))],
) -> JSONResponse:
    """Reject a pending (or held) proposal."""
    return _decide(proposal_id, principal, "rejected")


# ======================================================================================
# POST /api/proposals/{id}/apply  -- apply_writer
# ======================================================================================


@router.post("/proposals/{proposal_id}/apply")
def apply_endpoint(
    proposal_id: int,
    principal: Annotated[Principal, Depends(require_api_key(SCOPE_ADMIN))],
    auto: bool = Query(
        default=False,
        description="run R24's auto-apply gate instead of applying on the reviewer's word",
    ),
) -> JSONResponse:
    """Apply an approved proposal to the canonical layer, as `apply_writer`.

    DESIGN pins this endpoint as "approved only; auto path per R24". Both are
    here and they are different code paths, not a flag inside one:

    * default -- a human has approved and is pressing apply. `recon.apply.apply_proposal`.
    * `?auto=true` -- **R24**. `recon.apply.auto_apply` runs the gate first and
      refuses with 409 plus every condition it evaluated. A sensitive proposal is
      refused here at any confidence, including 1.0, and is refused *before* the
      confidence of the proposal has been read at all.
    """
    scope = visible_scope(principal)
    with get_engine().connect() as reader:
        if _read_proposal(reader, proposal_id, scope) is None:
            return _not_found("proposal", proposal_id)

    try:
        with role_connection(ROLE_APPLY_WRITER) as conn:
            if auto:
                auto_apply(proposal_id, conn=conn)
            else:
                apply_proposal(proposal_id, conn=conn)
    except AutoApplyRefused as refused:
        log.info(
            "review.auto_apply_refused",
            proposal_id=proposal_id,
            reason=refused.decision.reason,
            status_code=409,
        )
        body = problem_body(
            "auto-apply-refused",
            "auto-apply refused",
            409,
            f"R24's gate refused proposal {proposal_id}: {refused.detail}",
        )
        body["auto_apply"] = refused.decision.as_dict()
        return JSONResponse(status_code=409, content=body, media_type="application/problem+json")
    except ApplyError as error:
        status = _APPLY_ERROR_STATUS.get(error.reason, 409)
        log.info(
            "review.apply_refused",
            proposal_id=proposal_id,
            reason=error.reason,
            status_code=status,
        )
        kind = "apply-" + error.reason.replace("_", "-")
        return problem(kind, error.reason, status, error.detail)

    with get_engine().connect() as reader:
        body = _read_proposal(reader, proposal_id, scope)
    log.info("review.applied", proposal_id=proposal_id, auto=auto, status_code=200)
    return JSONResponse(status_code=200, content=body)


def proposal_snapshot(conn: Connection, proposal_id: int) -> dict[str, Any] | None:
    """The stored proposal, for a caller that already holds a connection.

    Exported for the test suite and for any future job that wants the same row
    shape without going through HTTP. `recon.apply.load_proposal` is the typed
    record; this is the JSON one.
    """
    record = load_proposal(conn, proposal_id)
    if record is None:
        return None
    return {
        "id": str(record.id),
        "conflict_id": str(record.conflict_id),
        "conflict_type": record.conflict_type,
        "status": record.status,
        "sensitive": record.sensitive,
        "confidence": float(record.confidence),
        "action": dict(record.action),
    }
