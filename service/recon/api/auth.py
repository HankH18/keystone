"""API authentication: per-job trigger secrets and scoped client keys (R19, R20).

Two mechanisms, deliberately separate, because they answer different questions.

``X-Trigger-Secret`` (R19) -- *is this the scheduler?*
    Per **job**, not per service. ``/internal/sync`` and ``/internal/ingest/*``
    are both the *sync* job and accept only its secret; ``/internal/reconcile``
    accepts only ``TRIGGER_SECRET_RECONCILE``. Presenting the other job's secret
    is a 401, so a leaked cron environment surrenders one endpoint rather than
    all of them, and the two rotate independently. Missing header, wrong secret,
    **and unconfigured secret** are all 401 -- see the fail-closed note below.

One implementation, or it is not a check
----------------------------------------
:func:`verify_trigger_secret` is the **only** place that decides whether a
presented header is the scheduler's, and :func:`trigger_secret_for` is the only
place that decides whether a configured value is usable. There was briefly a
second answer to the second question in ``recon.ingest``, and the two diverged
exactly where divergence is a vulnerability: this module asked ``if not
configured``, which is ``False`` for ``"   "``, so ``TRIGGER_SECRET_SYNC="   "``
counted as configured and a caller who guessed the header ``"   "`` was
*authenticated*. ``recon.ingest`` had it right (whitespace-only is unusable) and
that reading is what survives here.

Two implementations of one security check are not redundancy; they are a
50 % chance of getting the weaker one. ``tests/triggers/test_single_trigger_guard.py``
fails if a second one reappears -- it enumerates the mounted routes from the app
itself and asserts that no module outside this one reads a ``trigger_secret``
setting or compares one.

``X-Api-Key`` (R20) -- *which tenant is this, and may it see org-wide rows?*
    Resolves to a scope: ``client`` sees only its own rows, ``admin`` sees
    org-wide. Missing/invalid is 401; authenticated-but-insufficient is 403.
    That distinction is not cosmetic -- 403 tells a caller its key is fine and
    its scope is not, which is the message that stops it from rotating a working
    key in response to a permissions problem.

Fail closed, always
-------------------
An unset secret does **not** disable the check. It cannot: an endpoint that
authenticates everyone when its secret is missing is worse than one that
authenticates nobody, because the failure is silent, survives review, and looks
identical to a working deployment until someone finds it. Every unconfigured
path here returns 401 and logs ``auth.secret_not_configured``.

Keys are never logged and never returned
----------------------------------------
Only ``sha256(salt:key)`` ever leaves this module's comparison, and only into a
``WHERE``-less scan compared with :func:`hmac.compare_digest`. The plaintext key
appears in no log line, no error body, and no ``Principal``. RFC7807 bodies here
say *what* failed, never *with what value*.

Constant time
-------------
Both mechanisms compare with :func:`hmac.compare_digest`. For API keys that
means fetching the candidate hashes and comparing each in constant time rather
than asking Postgres for an equality match: an indexed lookup's timing is a
function of the stored data, and taking the comparison into Python makes the
"which prefix matched" side channel structurally absent instead of merely
unlikely. The table holds two committed demo rows, so the scan is free.
"""

from __future__ import annotations

import hmac
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from fastapi import Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from recon.config import get_settings
from recon.db import api_key_hash, get_engine
from recon.logging import get_logger

__all__ = [
    "API_KEY_HEADER",
    "JOB_RECONCILE",
    "JOB_SYNC",
    "TRIGGER_JOBS",
    "TRIGGER_SECRET_FIELDS",
    "TRIGGER_SECRET_HEADER",
    "Principal",
    "ProblemException",
    "api_key_guard",
    "install_problem_handler",
    "problem",
    "problem_body",
    "problem_exception_handler",
    "require_api_key",
    "require_trigger_secret",
    "resolve_api_key",
    "trigger_guard",
    "trigger_secret_for",
    "verify_trigger_secret",
    "visible_scope",
]

log = get_logger("recon.api.auth")

TRIGGER_SECRET_HEADER: Final = "X-Trigger-Secret"
API_KEY_HEADER: Final = "X-Api-Key"

JOB_SYNC: Final = "sync"
JOB_RECONCILE: Final = "reconcile"

#: The scheduled jobs, each with its own secret.
TRIGGER_JOBS: Final[tuple[str, ...]] = (JOB_SYNC, JOB_RECONCILE)

#: Per job, the `Settings` fields consulted for its secret, **in order**.
#:
#: `trigger_secret` is the deprecated single shared secret. It is a fallback for
#: `sync` only, and it is a property of the *job* rather than of one endpoint --
#: which is the entire point of this table. `/internal/ingest/*` honoured it while
#: `/internal/sync` did not, so the same job had two different sets of acceptable
#: credentials depending on which URL a caller used. It is listed second, so a
#: deployment that has set the per-job secret is never authenticated by the
#: deprecated one; and it is not offered to `reconcile`, because widening a second
#: job's accepted credentials is not a thing a de-duplication may quietly do.
TRIGGER_SECRET_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    JOB_SYNC: ("trigger_secret_sync", "trigger_secret"),
    JOB_RECONCILE: ("trigger_secret_reconcile",),
}

#: `api_clients.scope` is a Postgres enum with exactly these values.
SCOPE_CLIENT: Final = "client"
SCOPE_ADMIN: Final = "admin"

_PROBLEM_BASE: Final = "https://keystone.invalid/problems"
_PROBLEM_MEDIA_TYPE: Final = "application/problem+json"


# ===========================================================================
# RFC7807 problems
# ===========================================================================
def problem_body(kind: str, title: str, status: int, detail: str) -> dict[str, Any]:
    """An RFC7807 body: ``{type, title, status, detail}`` (DESIGN §HTTP API)."""
    return {
        "type": f"{_PROBLEM_BASE}/{kind}",
        "title": title,
        "status": status,
        "detail": detail,
    }


def problem(kind: str, title: str, status: int, detail: str) -> JSONResponse:
    """An RFC7807 :class:`JSONResponse`, with the problem media type."""
    body = problem_body(kind, title, status, detail)
    return JSONResponse(status_code=status, content=body, media_type=_PROBLEM_MEDIA_TYPE)


class ProblemException(HTTPException):
    """An :class:`HTTPException` carrying a full RFC7807 body.

    FastAPI's default handler would render ``{"detail": ...}``, which is not
    RFC7807. :func:`install_problem_handler` registers the handler that renders
    the real thing; until an app installs it, use the ``*_guard`` functions,
    which return a response directly and need no app-level wiring.
    """

    def __init__(self, kind: str, title: str, status: int, detail: str) -> None:
        super().__init__(status_code=status, detail=detail)
        self.problem = problem_body(kind, title, status, detail)


async def problem_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Render :class:`ProblemException` as ``application/problem+json``."""
    assert isinstance(exc, ProblemException)
    return JSONResponse(
        status_code=exc.status_code, content=exc.problem, media_type=_PROBLEM_MEDIA_TYPE
    )


def install_problem_handler(app: Any) -> None:
    """Register :func:`problem_exception_handler` on a FastAPI app."""
    app.add_exception_handler(ProblemException, problem_exception_handler)


# ===========================================================================
# (a) per-job trigger secret -- R19
# ===========================================================================
def trigger_secret_for(job: str) -> str | None:
    """The **usable** secret for ``job``, or ``None`` when it has none.

    ``None`` means "refuse everything", never "allow everything". Callers must
    not treat it as an off switch -- :func:`verify_trigger_secret` is the only
    place that decision is made, and it makes it once.

    Unset, ``""`` and whitespace-only all resolve to ``None``. That last one is
    not pedantry: ``if not configured`` is ``False`` for ``"   "``, so a
    ``TRIGGER_SECRET_SYNC="   "`` left behind by a here-doc or a YAML quoting
    accident counted as *configured* -- and the header a caller had to guess to be
    authenticated was three spaces. A secret whose entire content is whitespace is
    a misconfiguration, and a misconfiguration denies.

    The value is returned **unstripped**. Only its usability is judged here;
    trimming it would quietly accept a credential different from the one the
    deployment configured.
    """
    if job not in TRIGGER_JOBS:
        raise ValueError(f"unknown trigger job {job!r}; expected one of {TRIGGER_JOBS}")
    settings = get_settings()
    for field in TRIGGER_SECRET_FIELDS[job]:
        configured = getattr(settings, field, None)
        if isinstance(configured, str) and configured.strip():
            return configured
    return None


def verify_trigger_secret(job: str, presented: str | None) -> bool:
    """Constant-time check of ``presented`` against ``job``'s secret.

    **The one implementation.** Every endpoint that requires a trigger secret --
    inline via :func:`trigger_guard` or as a dependency via
    :func:`require_trigger_secret` -- reaches this function, and nothing else
    anywhere compares a trigger secret.

    False when the secret is unusable (unset, empty or whitespace-only: fail
    closed), when the header is absent, and when it is the *other* job's secret --
    the per-job split is only real if each endpoint rejects the sibling credential.
    """
    configured = trigger_secret_for(job)
    if configured is None:
        log.error(
            "auth.secret_not_configured",
            job=job,
            env_vars=[field.upper() for field in TRIGGER_SECRET_FIELDS[job]],
            outcome="401",
            detail=(
                "no usable trigger secret is configured, so every caller is "
                "refused; an unconfigured secret denies, it never disables the "
                "check (R19). Empty and whitespace-only count as unconfigured."
            ),
        )
        return False
    if presented is None:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), configured.encode("utf-8"))


def trigger_guard(job: str, presented: str | None) -> JSONResponse | None:
    """``None`` when authorised, else a 401 RFC7807 response.

    Inline form, for endpoints that must be correct without any app-level
    exception handler being wired in.
    """
    if verify_trigger_secret(job, presented):
        return None
    log.warning("auth.trigger_denied", job=job, presented=presented is not None, status=401)
    return problem(
        "unauthorized",
        "unauthorized",
        401,
        f"{TRIGGER_SECRET_HEADER} is missing or is not the secret for the "
        f"{job!r} job (R19). Each scheduled job has its own secret.",
    )


def require_trigger_secret(job: str) -> Any:
    """FastAPI dependency factory enforcing ``job``'s secret.

    Usage::

        @router.post("/sync", dependencies=[Depends(require_trigger_secret("sync"))])
    """
    if job not in TRIGGER_JOBS:
        raise ValueError(f"unknown trigger job {job!r}; expected one of {TRIGGER_JOBS}")

    def dependency(
        x_trigger_secret: str | None = Header(default=None, alias=TRIGGER_SECRET_HEADER),
    ) -> None:
        if not verify_trigger_secret(job, x_trigger_secret):
            log.warning(
                "auth.trigger_denied", job=job, presented=x_trigger_secret is not None, status=401
            )
            raise ProblemException(
                "unauthorized",
                "unauthorized",
                401,
                f"{TRIGGER_SECRET_HEADER} is missing or is not the secret for the "
                f"{job!r} job (R19).",
            )

    return dependency


# ===========================================================================
# (b) scoped API keys -- R20
# ===========================================================================
@dataclass(frozen=True)
class Principal:
    """An authenticated API client. **Never carries the key.**"""

    scope: str
    label: str

    @property
    def is_admin(self) -> bool:
        return self.scope == SCOPE_ADMIN


_SELECT_CLIENTS = text("SELECT key_hash, scope::text AS scope, label FROM api_clients")


def resolve_api_key(key: str | None) -> Principal | None:
    """Resolve a plaintext key to its :class:`Principal`, or ``None``.

    The key is hashed with the committed salt and compared against every stored
    hash with :func:`hmac.compare_digest`. Neither the key nor its hash is
    logged; the returned principal carries only the scope and the row label.
    """
    if not key:
        return None
    presented = api_key_hash(key).encode("ascii")
    with get_engine().connect() as conn:
        rows = conn.execute(_SELECT_CLIENTS).fetchall()

    matched: Principal | None = None
    for row in rows:
        # No early break: the loop cost must not depend on which row matched.
        if hmac.compare_digest(presented, row.key_hash.encode("ascii")):
            matched = Principal(scope=row.scope, label=row.label)
    return matched


def api_key_guard(
    presented: str | None,
    *,
    required_scope: str | None = None,
) -> tuple[Principal | None, JSONResponse | None]:
    """Resolve and authorise in one step: ``(principal, error_response)``.

    401 for missing/invalid, 403 for authenticated-with-the-wrong-scope.
    """
    principal = resolve_api_key(presented)
    if principal is None:
        log.warning("auth.api_key_denied", presented=presented is not None, status=401)
        return None, problem(
            "unauthorized",
            "unauthorized",
            401,
            f"{API_KEY_HEADER} is missing or is not a known client key (R20).",
        )
    if required_scope is not None and principal.scope != required_scope:
        log.warning("auth.scope_denied", label=principal.label, scope=principal.scope, status=403)
        return principal, problem(
            "forbidden",
            "forbidden",
            403,
            f"scope {principal.scope!r} may not use this endpoint; {required_scope!r} is "
            "required (R20).",
        )
    return principal, None


def require_api_key(required_scope: str | None = None) -> Any:
    """FastAPI dependency factory returning the authenticated :class:`Principal`.

    Usage::

        @router.get("/conflicts")
        def conflicts(principal: Principal = Depends(require_api_key())): ...
        # org-wide endpoints:
        Depends(require_api_key("admin"))
    """

    def dependency(
        x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
    ) -> Principal:
        principal = resolve_api_key(x_api_key)
        if principal is None:
            log.warning("auth.api_key_denied", presented=x_api_key is not None, status=401)
            raise ProblemException(
                "unauthorized",
                "unauthorized",
                401,
                f"{API_KEY_HEADER} is missing or is not a known client key (R20).",
            )
        if required_scope is not None and principal.scope != required_scope:
            log.warning(
                "auth.scope_denied", label=principal.label, scope=principal.scope, status=403
            )
            raise ProblemException(
                "forbidden",
                "forbidden",
                403,
                f"scope {principal.scope!r} may not use this endpoint; "
                f"{required_scope!r} is required (R20).",
            )
        return principal

    return dependency


def visible_scope(principal: Principal) -> str | None:
    """Row-visibility filter for ``principal``.

    ``None`` means org-wide (the ``admin`` scope). Anything else is the tenant
    label a query must filter on. Returning ``None`` for admin rather than a
    magic string keeps "no filter" un-spellable as a tenant value: a client
    label can never accidentally equal it.
    """
    return None if principal.is_admin else principal.label


def scopes_allowed(*scopes: str) -> Sequence[str]:
    """Small helper so routers name scopes instead of repeating literals."""
    for scope in scopes:
        if scope not in (SCOPE_CLIENT, SCOPE_ADMIN):
            raise ValueError(f"unknown api client scope {scope!r}")
    return scopes
