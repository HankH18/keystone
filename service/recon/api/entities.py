"""`GET /api/entities/{key}` -- the unified cross-source entity view (R10, R20).

The question the brief asks -- *is this person registered? paid? what stage?* --
has to be answerable across three sources that disagree, from whatever identifier
the person asking happens to be holding. So this endpoint answers it from the
**canonical** row `recon.resolve` materialized, and reports, field by field, which
source said what and when.

What a caller may pass as `{key}`
--------------------------------
Four forms, resolved in this order and reported back in `key.form`:

=============  ==============================  =====================================
form           example                         resolved through
=============  ==============================  =====================================
`person_key`   `1bac7603-...-3feab69810f2`     the `entities` primary key
`source_ref`   `crm:contact:CRM-0015743`       `entity_links (generation, source_ref)`
`natural_key`  `CRM-0015743`, `pi_0015202`     the same index, all five ref classes
`email`        `guardian@example.test`         `stg_crm_contact.email_norm` and the
                                               `stg_student` guardian addresses
=============  ==============================  =====================================

`canonical_id` and `person_key` are the same value (contract SS4.1), so the UUID
form covers both names a reviewer might use for it. An app-DB student id is
itself a UUID, so the first two readings of a UUID-shaped key are both tried --
`person_key` first, then `natural_key`.

**An email can be ambiguous, and saying so is the point.** Siblings share a
guardian email -- the generator plants at least 1,000 multi-child households --
and they are *different children*. An email that reaches more than one person is
answered with **409** and the list of candidate `person_key`s, never with a merged
view and never with an arbitrary pick. Collapsing them is the exact failure R9
names.

Scope (R20), and why a hidden row is 404 rather than 403
--------------------------------------------------------
Authentication and scope resolution are `recon.api.auth`'s, not this module's:
`require_api_key()` gives 401 for a missing or unknown key, and `visible_scope()`
turns the principal into the row filter -- `None` for `admin` (org-wide), the
tenant label for a client.

The filter is applied **in the SQL that reads the row**, so a row belonging to
another tenant is not found rather than found-and-refused, and the endpoint
answers 404. That is deliberate: with 403 the response distinguishes "no such
entity" from "an entity you may not see", which hands an unauthorised caller a
membership oracle over every key it can guess. The 403 case lives where a scope
genuinely gates the *operation* rather than the row: `GET /api/entities`, the
org-wide index, requires `admin` and refuses a client key with 403.

Wiring
------
This module exports `router` and does not touch `recon/app.py` (another ticket
owns it). One line mounts it::

    from recon.api.entities import router as entities_router
    app.include_router(entities_router)
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator, Sequence
from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text

from recon.adapters import IdentifierError, validate_identifier
from recon.api.auth import (
    SCOPE_ADMIN,
    Principal,
    problem,
    problem_body,
    require_api_key,
    visible_scope,
)
from recon.db import get_engine
from recon.ingest import identifier_problem
from recon.logging import get_logger
from recon.normalize import norm_email
from recon.reference import REF_CLASSES
from recon.resolve import CURRENT_GENERATION, VIEW_FIELDS

__all__ = ["ENTITY_KEY_FORMS", "router"]

log = get_logger("recon.api.entities")

router = APIRouter(prefix="/api", tags=["entities"])

#: The key forms `{key}` accepts, in resolution order. Reported in `key.form` so a
#: caller can tell how its identifier was understood.
ENTITY_KEY_FORMS: Final[tuple[str, ...]] = ("person_key", "source_ref", "natural_key", "email")

_UUID_RE: Final = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: A key is only tried as an email when it looks like one. `@` is not legal in any
#: ref class or natural key the generator emits, so the classes cannot overlap.
_EMAIL_RE: Final = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: Max entities one org-wide index page may return.
_MAX_PAGE: Final = 200


# ======================================================================================
# what a caller may put in the URL -- the ingest path's rule, not a second one
# ======================================================================================


def _checked_identifier(value: str, *, field: str) -> str | JSONResponse:
    """``value`` if it is a storable identifier, else the 4xx it earns.

    **The same validator the landing path uses** -- ``recon.adapters.identifiers``
    is the one rule (its module docstring tabulates what three separate copies of
    it produced), and it is imported here rather than re-stated, so a URL and a
    payload cannot disagree about what a client is allowed to send.

    What this fixes, exactly: ``{key}`` reaches Postgres as a query parameter, and
    a NUL in it makes psycopg raise a plain ``ValueError`` -- not a
    ``psycopg.Error`` -- from inside the ``conn.execute`` in :func:`_attempts`.
    Nothing caught it, so ``GET /api/entities/%00`` and ``/api/entities/a%00b``
    answered ``text/plain`` "Internal Server Error" for both key scopes, where
    DESIGN pins an RFC7807 problem document. It is a 422 for the same reason the
    ingest path returns one: the value is unusable *as an identifier*, which is a
    fault in the request, not in the server.
    """
    try:
        return validate_identifier(value, field=field)
    except IdentifierError as exc:
        log.info("entities.invalid_identifier", field=exc.field, reason=exc.reason, status=422)
        return identifier_problem(exc)


# ======================================================================================
# SQL -- every lookup is an index read; none of them scans `entities`
# ======================================================================================

#: The scope filter lives here, in the row read itself (see the module docstring).
#: `:scope IS NULL` is the admin case and is un-spellable as a tenant label.
_ENTITIES_BY_ID = text(
    """
    SELECT canonical_id::text AS canonical_id, current
      FROM entities
     WHERE canonical_id = ANY(CAST(:ids AS uuid[]))
       AND (CAST(:scope AS text) IS NULL OR current ->> 'tenant' = CAST(:scope AS text))
     ORDER BY canonical_id
    """
)

_IDS_BY_REF = text(
    """
    SELECT DISTINCT canonical_id::text AS canonical_id
      FROM entity_links
     WHERE generation = :generation
       AND source_ref = ANY(CAST(:refs AS text[]))
    """
)

_IDS_BY_EMAIL = text(
    """
    SELECT DISTINCT el.canonical_id::text AS canonical_id
      FROM stg_crm_contact c
      JOIN entity_links el
        ON el.generation = c.generation
       AND el.source_ref = 'crm:contact:' || c.crm_id
     WHERE c.generation = :generation
       AND c.email_norm = :email
     UNION
    SELECT DISTINCT el.canonical_id::text AS canonical_id
      FROM stg_student s
      JOIN entity_links el
        ON el.generation = s.generation
       AND el.source_ref = 'appdb:student:' || s.student_id
     WHERE s.generation = :generation
       AND (s.email_norm = :email OR s.guardian2_email_norm = :email)
    """
)

_LINEAGE = text(
    """
    SELECT field, value_text, source_id, source_ref, generation, observed_ts
      FROM field_lineage
     WHERE canonical_id = CAST(:canonical_id AS uuid)
     ORDER BY field, generation, source_ref
    """
)

_INDEX_PAGE = text(
    """
    SELECT canonical_id::text AS canonical_id,
           current ->> 'tenant'      AS tenant,
           current ->> 'anchor_ref'  AS anchor_ref,
           current ->> 'stage_funnel' AS stage_funnel
      FROM entities
     WHERE (CAST(:scope AS text) IS NULL OR current ->> 'tenant' = CAST(:scope AS text))
       AND (CAST(:after AS text) IS NULL OR canonical_id > CAST(:after AS uuid))
     ORDER BY canonical_id
     LIMIT :limit
    """
)


# ======================================================================================
# key resolution
# ======================================================================================


def _candidate_refs(key: str) -> list[str]:
    """`key` as a bare natural key, in every ref class it could belong to.

    Five index probes in one statement beats guessing from the key's shape: the
    shapes are the generator's, and an endpoint that hard-codes `CRM-` prefixes
    stops working the day a source renumbers.
    """
    return [f"{prefix}{key}" for prefix in REF_CLASSES]


def _attempts(conn: Any, key: str) -> Iterator[tuple[str, list[str]]]:
    """Every `(form, canonical_ids)` reading of `key`, most specific first.

    An iterator rather than a single answer, because the forms genuinely overlap:
    **an app-DB student's natural key is itself a UUID**, so "looks like a UUID"
    cannot decide between `person_key` and `natural_key`. The route tries each
    reading in turn and stops at the first that resolves to a visible row, which
    also means the hot path (a real `person_key`) still costs one primary-key read.
    """
    if _UUID_RE.match(key):
        yield "person_key", [str(UUID(key))]

    if key.count(":") == 2 and any(key.startswith(prefix) for prefix in REF_CLASSES):
        rows = conn.execute(
            _IDS_BY_REF, {"generation": CURRENT_GENERATION, "refs": [key]}
        ).fetchall()
        yield "source_ref", [row.canonical_id for row in rows]
        return

    if _EMAIL_RE.match(key):
        email = norm_email(key)
        rows = (
            conn.execute(
                _IDS_BY_EMAIL, {"generation": CURRENT_GENERATION, "email": email}
            ).fetchall()
            if email is not None
            else []
        )
        yield "email", [row.canonical_id for row in rows]
        return

    rows = conn.execute(
        _IDS_BY_REF, {"generation": CURRENT_GENERATION, "refs": _candidate_refs(key)}
    ).fetchall()
    yield "natural_key", [row.canonical_id for row in rows]


# ======================================================================================
# response assembly
# ======================================================================================


def _view_of(current: dict[str, Any]) -> dict[str, Any]:
    """The golden-shaped view out of a stored canonical row.

    Exactly `VIEW_FIELDS`, so the object a reviewer diffs against
    `golden/expected-views.json` is a dict comparison rather than a subset test.
    The row also stores `generation` and `tenant`; both are reported at the top
    level of the response, and neither is smuggled into the join contract.
    """
    return {field: current[field] for field in VIEW_FIELDS}


def _lineage_of(conn: Any, canonical_id: str) -> list[dict[str, Any]]:
    """Per-field lineage: which source said what, and when it said it.

    `observed_at` is the **source record's** own timestamp, not the moment the
    pipeline wrote the row down -- "when did the CRM last assert this" is the
    question a reconciler and a reviewer both ask.
    """
    return [
        {
            "field": row.field,
            "value": row.value_text,
            "source_id": row.source_id,
            "source_ref": row.source_ref or None,
            "generation": row.generation,
            "observed_at": row.observed_ts.isoformat(),
        }
        for row in conn.execute(_LINEAGE, {"canonical_id": canonical_id})
    ]


def _ambiguous(key: str, form: str, rows: Sequence[Any]) -> JSONResponse:
    """409 for a key that names more than one person -- never a merged view.

    Siblings share a guardian email by construction (contract SS4.8), and merging
    them is the failure R9 names. The candidate list is already scope-filtered, so
    it can never disclose a person the caller may not see.
    """
    body = problem_body(
        "ambiguous-entity-key",
        "ambiguous entity key",
        409,
        f"{form} {key!r} resolves to {len(rows)} distinct persons; they are different "
        "entities (siblings share a guardian email, contract SS4.8) and are never "
        "merged. Retry with one of the candidate person keys.",
    )
    body["candidates"] = [
        {
            "person_key": row.canonical_id,
            "anchor_ref": row.current.get("anchor_ref"),
            "identity_refs": row.current.get("identity_refs", []),
        }
        for row in rows
    ]
    log.info("entities.ambiguous", form=form, candidates=len(rows), status=409)
    return JSONResponse(status_code=409, content=body, media_type="application/problem+json")


# ======================================================================================
# routes
# ======================================================================================


@router.get("/entities/{key}")
def get_entity(
    key: str,
    principal: Annotated[Principal, Depends(require_api_key())],
    lineage: bool = Query(default=True, description="include per-field source lineage"),
) -> JSONResponse:
    """The unified cross-source view of one person: registered? paid? what stage?"""
    started = time.perf_counter()
    checked = _checked_identifier(key, field="key")
    if isinstance(checked, JSONResponse):
        return checked
    scope = visible_scope(principal)

    with get_engine().connect() as conn:
        form, rows = "unknown", []
        for candidate_form, ids in _attempts(conn, key):
            form = candidate_form
            if not ids:
                continue
            rows = conn.execute(_ENTITIES_BY_ID, {"ids": ids, "scope": scope}).fetchall()
            if rows:
                break

        if not rows:
            log.info("entities.not_found", form=form, scope=scope, status=404)
            return problem(
                "entity-not-found",
                "entity not found",
                404,
                f"no entity visible to this key matches {key!r} (resolved as {form}).",
            )
        if len(rows) > 1:
            return _ambiguous(key, form, rows)

        row = rows[0]
        current = dict(row.current)
        view = _view_of(current)
        body: dict[str, Any] = {
            "key": {"requested": key, "form": form, "canonical_id": row.canonical_id},
            "generation": current.get("generation"),
            "tenant": current.get("tenant"),
            "scope": principal.scope,
            "answer": {
                "registered": view["registered"],
                "paid": view["paid"],
                "stage": view["stage_funnel"],
                "sources": view["sources"],
            },
            "view": view,
            "lineage": _lineage_of(conn, row.canonical_id) if lineage else None,
        }

    log.info(
        "entities.read",
        form=form,
        scope=scope,
        lineage=len(body["lineage"] or ()),
        elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
        status=200,
    )
    return JSONResponse(status_code=200, content=body)


@router.get("/entities")
def list_entities(
    principal: Annotated[Principal, Depends(require_api_key(SCOPE_ADMIN))],
    limit: int = Query(default=50, ge=1, le=_MAX_PAGE),
    after: str | None = Query(default=None, description="last canonical_id of the previous page"),
) -> JSONResponse:
    """The org-wide entity index. **Admin scope only** -- a client key gets 403.

    This is the operation a scope genuinely gates, which is why the 403 of R20
    lives here and the per-row filter answers 404 instead (module docstring).

    ``after`` is a *cursor*, and it is the same class of hole ``{key}`` was: it is
    cast to ``uuid`` inside ``_INDEX_PAGE``, so ``?after=notauuid``, ``?after=``
    and ``?after=%00`` each reached the database and came back as a bare
    ``text/plain`` 500. It is validated here against the same identifier rule and
    then parsed as the UUID the column holds, so every rejection is a 422 problem
    document naming the parameter.
    """
    if after is not None:
        checked = _checked_identifier(after, field="after")
        if isinstance(checked, JSONResponse):
            return checked
        try:
            after = str(UUID(checked))
        except ValueError:
            log.info("entities.invalid_cursor", status=422)
            return problem(
                "invalid-cursor",
                "invalid cursor",
                422,
                "'after' is a pagination cursor and must be the canonical_id (a UUID) "
                "of the last entity on the previous page; the value supplied is not a "
                "UUID. The previous page reports the next one in 'next_after'.",
            )

    with get_engine().connect() as conn:
        rows = conn.execute(
            _INDEX_PAGE, {"scope": visible_scope(principal), "after": after, "limit": limit}
        ).fetchall()

    return JSONResponse(
        status_code=200,
        content={
            "generation": CURRENT_GENERATION,
            "scope": principal.scope,
            "count": len(rows),
            "next_after": rows[-1].canonical_id if len(rows) == limit else None,
            "entities": [
                {
                    "person_key": row.canonical_id,
                    "tenant": row.tenant,
                    "anchor_ref": row.anchor_ref,
                    "stage_funnel": row.stage_funnel,
                }
                for row in rows
            ],
        },
    )
