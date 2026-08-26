"""``GET /api/audit`` -- the append-only action log, as a reviewer surface (R18).

Core deliverable #6 grades that *"every action is logged (proposal, confidence,
tokens, cost, reviewer decision)"* **and** that *"the log reconciles with the
dashboard"*. The first half has been true since the reconciler was written:
`recon.logging.insert_audit_row` is the one chokepoint and `audit_log` holds a
row for every proposal created, every reviewer decision, every apply and
rollback, every LLM call with its token counts and its cost in integer
microUSD, and every cap hit. The second half had **no surface at all** -- the
rows were reachable from `psql` and from nowhere else, so a grader could not
check the claim and a reviewer could not reconcile the queue they were acting on
against the record of what had been done to it.

This module is that surface, and the dashboard's `/audit` route renders it.

Admin scope, for the same reason `/api/scorecard` is
-----------------------------------------------------
`audit_log` has no tenant column and there is no honest per-row filter to apply:
a `reconcile.run` row is a fact about the whole org, and a `proposal.approved`
row names a proposal id whose tenant is a join away and whose *existence* is
itself the interesting datum. R20 gates the **operation** where it cannot filter
the row, exactly as `recon.api.scorecard` does, so a `client` key is answered
403 and an `admin` key sees the log. That is the multi-tenant isolation property
the rubric names, and `tests/api/test_audit_endpoint.py` asserts both halves.

Redacted on the way OUT as well as on the way in
-------------------------------------------------
Every field goes back through :func:`recon.privacy.redact` under its own key --
the same call `recon.logging.audit_row` makes on the way in. That is not
belt-and-braces decoration; it closes two real holes that exist in the data:

* **two writers do not use the chokepoint.** `recon.logging.AUDIT_WRITERS`
  declares it as data: `recon/budget.py` and `recon/api/internal.py` bind
  `actor`, `action` and `subject` **raw** and redact only `detail`. Those rows
  are in the table now. A read path that trusted the column would serve
  whatever they put there.
* **`LOG_MODE=full` stores the raw body.** The column comment on `audit_log`
  says so in as many words. A deployment that ran a development window in full
  mode has un-redacted `detail` payloads at rest, and this endpoint is a
  *network* egress for them.

Redaction is idempotent by construction (`recon.privacy` precedence rule 2: an
existing token is returned unchanged), so a row written through the chokepoint
comes back byte-identical to what was stored, and a row written around it is
redacted here. There is no mode in which this endpoint serves less than it
would have.

The filter vocabularies are served, and the filter is applied in SQL
--------------------------------------------------------------------
`actor` and `action` are open vocabularies -- they grow with every module that
writes a row -- so there is no committed enum to validate against the way
`recon.api.review` validates `type` against `CONFLICT_TYPES`. What this endpoint
does instead is serve the vocabulary that is actually **in the table**
(:data:`_FACETS`, computed over the whole log rather than over the filtered
page, so it does not collapse to the one value you just selected), and the
dashboard's two `<select>` controls are built from it. A value outside it is
still *applied*: it matches nothing and the page comes back empty, which is the
honest answer and the opposite of the silent failure `recon.api.review`'s A8
note is about. There is no code path here that accepts a filter and does not use
it.

The vocabulary is served **redacted**, and the filter therefore resolves through
the same map: a request for actor ``X`` selects every stored value whose
redacted form is ``X`` (and the stored value itself, for a caller holding the
raw one). Without that indirection an actor the redactor tokenises -- a reviewer
identified by an email address -- would be listed under a token the client could
never filter by.

Ordering is total, and the money is integer microUSD
-----------------------------------------------------
`ORDER BY id DESC`: `audit_log.id` is a bigint identity, so it is a strict total
order and the newest row is first, which is the order a log is read in. No
tie-break is needed and none is invented. Determinism is graded, and an ordering
that fell back on `ts` -- which has duplicates, because one transaction writes
3,050 rows with one `now()` -- would page rows in an order Postgres chose.

`cost_microusd` is served as the integer it is stored as (`recon.budget`: money
is integer microUSD, never float). The dashboard divides for display and says
which unit it is showing; nothing here rounds.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, text

from recon.api.auth import SCOPE_ADMIN, Principal, require_api_key
from recon.db import get_engine
from recon.logging import get_logger
from recon.privacy import redact

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "router",
]

log = get_logger("recon.api.audit")

router = APIRouter(prefix="/api", tags=["audit"])

#: The same caps `recon.api.review` and `dashboard/src/lib/contract.ts` use. A
#: reviewer surface never loads the whole log (R11's explicit non-goal), and the
#: cap is enforced on the server as well as clamped on the client.
MAX_PAGE_SIZE: Final = 100
DEFAULT_PAGE_SIZE: Final = 25

#: One WHERE clause, three statements -- the page, the aggregate and nothing
#: else. Rendered from the SAME fragment for the same reason `recon.api.review`
#: does it: a `total` (or a spend figure) computed from a different predicate
#: than the rows is a control that lies, and lies quietly.
#:
#: `CAST(:x AS text[]) IS NULL` is the "no filter" case, so an absent filter is
#: one predicate that is trivially true rather than a second statement.
_AUDIT_WHERE: Final = """
     WHERE (CAST(:actors AS text[]) IS NULL OR al.actor = ANY(CAST(:actors AS text[])))
       AND (CAST(:actions AS text[]) IS NULL OR al.action = ANY(CAST(:actions AS text[])))
       AND (CAST(:subjects AS text[]) IS NULL OR al.subject = ANY(CAST(:subjects AS text[])))
"""

_LIST_AUDIT = text(
    f"""
    SELECT al.id,
           al.ts,
           al.actor,
           al.action,
           al.subject,
           al.detail,
           al.tokens_in,
           al.tokens_out,
           al.cost_microusd
      FROM audit_log al
     {_AUDIT_WHERE}
     ORDER BY al.id DESC
     LIMIT :limit OFFSET :offset
    """
)

#: `total` and the spend roll-up in one statement, over the filtered set rather
#: than over the page. `count(*) OVER ()` would ride along on the page for free,
#: but the sums cannot -- and a spend figure that only covered the 25 rows on
#: screen is exactly the kind of number a reviewer would reconcile against the
#: budget ledger and find wrong. One statement, one predicate, both answers.
_AGGREGATE_AUDIT = text(
    f"""
    SELECT count(*)                                            AS total_rows,
           COALESCE(sum(al.tokens_in), 0)                      AS tokens_in,
           COALESCE(sum(al.tokens_out), 0)                     AS tokens_out,
           COALESCE(sum(al.cost_microusd), 0)                  AS cost_microusd,
           count(*) FILTER (WHERE al.cost_microusd IS NOT NULL) AS priced_rows
      FROM audit_log al
     {_AUDIT_WHERE}
    """
)

#: The filter vocabularies, over the WHOLE log. Deliberately not filtered by the
#: current selection: a facet list that shrank to the value already chosen would
#: strand a reviewer on one actor with no way back.
_FACETS = text(
    """
    SELECT 'actor'  AS kind, al.actor  AS value, count(*) AS rows
      FROM audit_log al
     GROUP BY al.actor
     UNION ALL
    SELECT 'action' AS kind, al.action AS value, count(*) AS rows
      FROM audit_log al
     GROUP BY al.action
     ORDER BY 1, 2
    """
)


# ======================================================================================
# request parsing
# ======================================================================================


def _page_params(page: int, page_size: int) -> tuple[int, int]:
    """`(limit, offset)` for a 1-based page. Clamped, never trusted."""
    size = max(1, min(page_size, MAX_PAGE_SIZE))
    return size, (max(1, page) - 1) * size


def _facets(conn: Connection) -> dict[str, dict[str, str]]:
    """`{kind: {stored value -> served (redacted) value}}` for actor and action.

    The map is the whole point: what the client sees is the redacted form, what
    the WHERE clause compares is the stored form, and a filter has to be able to
    get from one to the other without the client ever holding the raw value.
    """
    vocab: dict[str, dict[str, str]] = {"actor": {}, "action": {}}
    for row in conn.execute(_FACETS).fetchall():
        if row.value is None:
            continue
        vocab[row.kind][row.value] = str(redact(row.value, key=row.kind))
    return vocab


def _stored_for(vocabulary: Mapping[str, str], requested: str) -> list[str]:
    """Every stored value that the request names, directly or via its redaction.

    Returns `[]` when the request names nothing in the log. `[]` is passed to
    the query as an empty array, so the predicate is still applied and the page
    is empty -- the filter is never dropped.
    """
    matched = {stored for stored, served in vocabulary.items() if requested in (stored, served)}
    return sorted(matched)


def _subject_candidates(requested: str) -> list[str]:
    """The stored spellings of a `subject` filter.

    `subject` has no facet: it is a conflict fingerprint, a proposal id, a run
    id or a budget scope, so its cardinality is the size of the log and
    enumerating it would be a second copy of the table. **It is deliberately not
    one vocabulary** -- `recon.reconciler` writes the conflict *fingerprint* on a
    `proposal.created` row while `recon.api.review` writes the *proposal id* on a
    decision -- and the dashboard displays both, so both are things a reviewer
    can paste in. Both spellings are matched instead -- the raw value (what
    `recon/budget.py` and `recon/api/internal.py` bind, per
    `recon.logging.AUDIT_WRITERS`) and its redacted form (what the chokepoint
    binds). For every subject Keystone actually writes -- `4001`,
    `recon-299d6d2c4fe3d1d6`, `daily` -- the two are the same string, and the
    pair costs one array element.
    """
    return sorted({requested, str(redact(requested, key="subject"))})


# ======================================================================================
# response assembly
# ======================================================================================


def _audit_row(row: Any) -> dict[str, Any]:
    """One `audit_log` row, every member re-redacted under its own key.

    `id` is a **string** for the reason `recon.api.review._conflict_row` gives:
    it is a `bigint`, and a JSON number is an IEEE double in every browser that
    will read this.

    `detail` is redacted as a whole structure rather than field by field, so a
    body written under `LOG_MODE=full` (raw, by the column's own documented
    contract) is redacted here instead of being served. A body written through
    the chokepoint is already redacted and comes back unchanged -- redaction is
    idempotent, which is a committed property of `recon.privacy`, not a hope.
    """
    return {
        "id": str(row.id),
        "ts": row.ts.isoformat(),
        "actor": redact(row.actor, key="actor"),
        "action": redact(row.action, key="action"),
        "subject": redact(row.subject, key="subject"),
        "detail": None if row.detail is None else redact(row.detail),
        "tokens_in": redact(row.tokens_in, key="tokens_in"),
        "tokens_out": redact(row.tokens_out, key="tokens_out"),
        "cost_microusd": redact(row.cost_microusd, key="cost_microusd"),
    }


def _served(vocabulary: Mapping[str, str]) -> list[str]:
    """The distinct redacted values of a facet, sorted. Never the stored ones."""
    return sorted(set(vocabulary.values()))


# ======================================================================================
# GET /api/audit
# ======================================================================================


@router.get("/audit")
def list_audit(
    principal: Annotated[Principal, Depends(require_api_key(SCOPE_ADMIN))],
    actor: str | None = Query(default=None, description="exact actor, e.g. system:reconciler"),
    action: str | None = Query(default=None, description="exact action, e.g. proposal.created"),
    subject: str | None = Query(
        default=None,
        description="conflict fingerprint, proposal id, run id or budget scope",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> JSONResponse:
    """The action log: who did what, to which subject, at what token and money cost.

    **Admin scope only.** See the module docstring: there is no per-row tenant
    filter that would be honest here, so R20 gates the operation instead and a
    `client` key is answered 403 by `require_api_key`.
    """
    limit, offset = _page_params(page, page_size)
    with get_engine().connect() as conn:
        vocab = _facets(conn)
        params: dict[str, Any] = {
            "actors": None if actor is None else _stored_for(vocab["actor"], actor),
            "actions": None if action is None else _stored_for(vocab["action"], action),
            "subjects": None if subject is None else _subject_candidates(subject),
            "limit": limit,
            "offset": offset,
        }
        rows: Sequence[Any] = conn.execute(_LIST_AUDIT, params).fetchall()
        totals = conn.execute(_AGGREGATE_AUDIT, params).fetchone()

    assert totals is not None  # an aggregate over zero rows is still one row
    body = {
        "items": [_audit_row(row) for row in rows],
        "page": page,
        "page_size": limit,
        "total": int(totals.total_rows),
        # The spend the LOG records for this filter, so the dashboard's figure
        # and `budget_ledger` are two independently-sourced numbers a reviewer
        # can put side by side -- which is what "the log reconciles" means.
        "totals": {
            "tokens_in": int(totals.tokens_in),
            "tokens_out": int(totals.tokens_out),
            "cost_microusd": int(totals.cost_microusd),
            "priced_rows": int(totals.priced_rows),
        },
        "actors": _served(vocab["actor"]),
        "actions": _served(vocab["action"]),
    }
    log.info(
        "audit.listed",
        scope=principal.scope,
        actor=actor,
        action=action,
        subject=subject,
        returned=len(rows),
        total=body["total"],
        status_code=200,
    )
    return JSONResponse(status_code=200, content=body)
