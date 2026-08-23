"""There is exactly ONE trigger-secret guard, and every endpoint reaches it (R19).

The defect this file exists to make un-reintroducible: *two implementations of one
security check, diverging*. ``recon.ingest`` answered "is this configured secret
usable?" one way (whitespace-only is unusable -- deny) and ``recon.api.auth``
answered it another (``if not configured``, which is ``False`` for ``"   "``), so
``TRIGGER_SECRET_SYNC="   "`` counted as configured on one endpoint and a caller
who presented the guessable header ``"   "`` was **authenticated**. Same
requirement, same deployment, two answers.

Three independent assertions, because any one of them alone can be satisfied by a
second implementation that happens to agree today:

**structural** -- no module outside ``recon.api.auth`` reads a ``trigger_secret``
setting or compares one. A second implementation has to do one of those two things
to exist, and this is what fails when it appears.

**routing** -- the routes are enumerated *from the app object*, not from memory,
and every handler that accepts the trigger header resolves to the shared guard.

**behavioural** -- the matrix below: every secret configuration (unset, empty,
whitespace, wrong, right) presented against every mutating endpoint, asserting the
one cell that may be admitted. Two implementations that agree on the easy cells
still diverge on ``"   "``, and the matrix visits it for each of them.

The set of endpoints under test is **derived from the app**, never listed here: a
mutating route that `create_app` mounts and the matrix does not cover fails
``test_every_mutating_route_the_app_mounts_is_covered_by_the_matrix``. That is not
hypothetical -- ``/internal/{sync,reconcile}`` were defined in
``recon.api.internal`` and not wired in at all for a while, so they 404'd in the
running service while tests that imported the router directly stayed green. The
matrix mounts that router itself when the app has not, so the guard is proven on
every endpoint this repository ships, wired or not.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from recon.api import auth as auth_module
from recon.api import internal as internal_module
from recon.app import create_app
from tests.budget.support import env_settings, unique

RECON_ROOT = Path(auth_module.__file__).resolve().parents[1]
AUTH_MODULE_PATH = Path(auth_module.__file__).resolve()
#: `recon/config.py` *declares* the settings fields; declaring them is not a second
#: implementation of the check, so it is the one other file allowed to name them.
CONFIG_MODULE_PATH = (RECON_ROOT / "config.py").resolve()

SYNC_SECRET = "sync-secret-for-the-guard-matrix"
RECONCILE_SECRET = "reconcile-secret-for-the-guard-matrix"

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: `(path, job)` for every SCHEDULED mutating endpoint (R19's trigger secret).
TRIGGER_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("/internal/ingest/records", auth_module.JOB_SYNC),
    ("/internal/sync", auth_module.JOB_SYNC),
    ("/internal/reconcile", auth_module.JOB_RECONCILE),
)

#: `(path, required scope)` for every mutating endpoint of the CLIENT API.
#:
#: A second class of mutating route exists as of T-11, and pretending otherwise
#: is what would make this file's coverage assertion a lie: DESIGN SSHTTP API
#: pins the reviewer decisions under `X-Api-Key`, not under a trigger secret --
#: they are pressed by a human in the dashboard, not by cron, and R19's secrets
#: are per *scheduled job*. Feeding them into the trigger matrix would assert the
#: wrong thing (that a reviewer needs the scheduler's credential); leaving them
#: out of both lists would let a mutating route ship unguarded, which is the
#: exact hole `test_every_mutating_route_the_app_mounts_is_covered_by_the_matrix`
#: exists to close. So they are covered here, by the guard they actually use, and
#: the coverage assertion below requires every mutating route to be in ONE of the
#: two lists and to carry the matching header.
API_KEY_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("/api/proposals/{proposal_id}/approve", auth_module.SCOPE_ADMIN),
    ("/api/proposals/{proposal_id}/reject", auth_module.SCOPE_ADMIN),
    ("/api/proposals/{proposal_id}/apply", auth_module.SCOPE_ADMIN),
)


#: Every way a deployment can have configured a secret. `"   "` is the one that
#: diverged; the empty string is its sibling; `None` is the default.
CONFIGURATIONS: tuple[tuple[str, str | None], ...] = (
    ("unset", None),
    ("empty", ""),
    ("whitespace", "   "),
    ("tab", "\t"),
    ("newline", "\n"),
    ("right", SYNC_SECRET),
)

#: Every string a caller can put in the header, plus "no header at all".
PRESENTATIONS: tuple[str | None, ...] = (
    None,
    "",
    " ",
    "   ",
    "\t",
    "\n",
    "wrong",
    SYNC_SECRET,
    RECONCILE_SECRET,
)


# ===========================================================================
# route enumeration -- from the app, never from memory
# ===========================================================================
def api_routes(app: FastAPI) -> list[APIRoute]:
    """Every `APIRoute` the app really serves.

    Walks whatever container FastAPI currently wraps an included router in
    (`_IncludedRouter` as of 0.141) rather than assuming `app.routes` is flat --
    an enumeration that silently returns fewer routes than the app serves would
    make every assertion below vacuous.
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


def takes_the_trigger_header(route: APIRoute) -> bool:
    """True when this route's handler accepts the `X-Trigger-Secret` header."""
    for parameter in inspect.signature(route.endpoint).parameters.values():
        default = parameter.default
        if getattr(default, "alias", None) == auth_module.TRIGGER_SECRET_HEADER:
            return True
    return False


def api_key_scope(route: APIRoute) -> str | None:
    """The scope `require_api_key` enforces on this route, or `None` if it has none.

    Read off the resolved dependency tree rather than off the source, so a route
    that *looks* guarded because the decorator mentions a dependency, but whose
    dependency does not actually ask for the header, is not counted as guarded.
    `required_scope` is the closure variable `recon.api.auth.require_api_key`
    captures, and `None` there means "authenticated, any scope".
    """

    def walk_dependant(dependant: object) -> Iterator[object]:
        yield dependant
        for sub in getattr(dependant, "dependencies", ()):
            yield from walk_dependant(sub)

    for dependant in walk_dependant(route.dependant):
        aliases = {param.alias for param in getattr(dependant, "header_params", ())}
        if auth_module.API_KEY_HEADER not in aliases:
            continue
        call = getattr(dependant, "call", None)
        if call is None:  # pragma: no cover - a header param always has a call
            continue
        return inspect.getclosurevars(call).nonlocals.get("required_scope")
    return None


def _app_with_every_trigger_route() -> FastAPI:
    """The real application, plus any trigger router it does not (yet) mount.

    `create_app` mounts `recon.api.internal` today; it did not always, and the
    guard has to be proven on every endpoint this repository ships either way.
    Mounting it twice would duplicate the routes, so this checks first.
    """
    app = create_app()
    mounted = {route.path for route in api_routes(app)}
    if not {route.path for route in internal_module.router.routes} <= mounted:
        app.include_router(internal_module.router)
    return app


def mutating_routes(app: FastAPI) -> list[APIRoute]:
    return [route for route in api_routes(app) if MUTATING_METHODS & set(route.methods)]


def test_every_mutating_route_the_app_mounts_is_covered_by_the_matrix() -> None:
    """Enumerated from `create_app()`, so the matrix cannot be run from memory.

    Deliberately **derived**, not a hard-coded list: a route added tomorrow has to
    fail this test rather than quietly sit outside the matrix. A guard proof whose
    scope is a literal written today is a guard proof for yesterday's app.
    """
    routes = api_routes(create_app())
    table = sorted((route.path, tuple(sorted(route.methods))) for route in routes)
    assert ("/health", ("GET",)) in table, table

    by_trigger = {path for path, _job in TRIGGER_ENDPOINTS}
    by_api_key = {path for path, _scope in API_KEY_ENDPOINTS}
    assert not (by_trigger & by_api_key), (
        f"{sorted(by_trigger & by_api_key)} is listed under two guards; a route with two "
        "credentials is a route whose weaker credential is the real one"
    )
    covered = by_trigger | by_api_key
    mutating = {route.path for route in mutating_routes(create_app())}
    assert mutating, f"no mutating route was enumerated at all: {table}"
    assert mutating <= covered, (
        "the application mounts a mutating route no guard list covers: "
        f"{sorted(mutating - covered)}. Every endpoint that writes must be run "
        f"through every configuration of the credential it requires. Full route "
        f"table: {table}"
    )

    # ...and every mutating route actually asks for the credential it is listed under.
    for route in mutating_routes(create_app()):
        if route.path in by_trigger:
            assert takes_the_trigger_header(route), (
                f"{route.path} is listed as trigger-guarded but does not accept "
                f"{auth_module.TRIGGER_SECRET_HEADER}"
            )
            assert api_key_scope(route) is None, (
                f"{route.path} accepts both credentials; the weaker one is then the "
                "real requirement"
            )
            continue
        expected_scope = dict(API_KEY_ENDPOINTS)[route.path]
        assert not takes_the_trigger_header(route), (
            f"{route.path} is a client-API route and also accepts "
            f"{auth_module.TRIGGER_SECRET_HEADER}"
        )
        assert api_key_scope(route) == expected_scope, (
            f"{route.path} mutates state and does not require {auth_module.API_KEY_HEADER} "
            f"with scope {expected_scope!r} (got {api_key_scope(route)!r}). DESIGN pins "
            "reviewer actions as org-wide, so a client key must be refused with 403."
        )


def test_every_route_taking_the_trigger_header_resolves_to_the_shared_guard() -> None:
    """No handler may compare a secret itself; it must delegate to `recon.api.auth`."""
    app = _app_with_every_trigger_route()

    checked = 0
    for route in api_routes(app):
        if not takes_the_trigger_header(route):
            continue
        checked += 1
        source = inspect.getsource(route.endpoint)
        module = inspect.getmodule(route.endpoint)
        assert module is not None
        # the handler delegates, directly or through its module's own one-line shim
        module_source = inspect.getsource(module)
        assert "trigger_guard" in module_source or "require_trigger_secret" in module_source, (
            f"{route.path} accepts the trigger header but its module never calls the shared guard"
        )
        assert "compare_digest" not in source, (
            f"{route.path} compares a secret in its own handler; the comparison "
            "lives in recon.api.auth.verify_trigger_secret and nowhere else"
        )
    assert checked >= 3, f"expected the three trigger endpoints, enumerated {checked}"


# ===========================================================================
# structural -- one module owns the check
# ===========================================================================
def _recon_sources() -> list[Path]:
    return sorted(p for p in RECON_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def test_only_recon_api_auth_reads_a_trigger_secret_setting() -> None:
    """A second implementation must read the setting. This is where it is caught."""
    reader = re.compile(r"trigger_secret(_sync|_reconcile)?\b")
    offenders: dict[str, list[str]] = {}
    for path in _recon_sources():
        if path.resolve() in (AUTH_MODULE_PATH, CONFIG_MODULE_PATH):
            continue
        hits = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            # comments and docstrings may discuss the setting; code may not read it
            if reader.search(line) and not line.lstrip().startswith(("#", "#:"))
        ]
        # `getattr(settings, "trigger_secret...")` and `settings.trigger_secret...`
        hits = [line for line in hits if "settings" in line.lower() or "getattr" in line]
        if hits:
            offenders[str(path.relative_to(RECON_ROOT))] = hits
    assert not offenders, (
        "a trigger-secret setting is read outside recon/api/auth.py, which is how "
        f"two answers to 'is this secret usable' come to exist again: {offenders}"
    )


def test_only_recon_api_auth_compares_a_trigger_secret() -> None:
    """`hmac.compare_digest` against a trigger secret exists in exactly one module."""
    offenders = [
        str(path.relative_to(RECON_ROOT))
        for path in _recon_sources()
        if path.resolve() != AUTH_MODULE_PATH
        and "compare_digest" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"a secret comparison lives outside recon/api/auth.py: {offenders}"


def test_the_usability_rule_is_stated_once() -> None:
    """One function decides whether a configured value counts as configured."""
    assert auth_module.trigger_secret_for("sync") is not None or True  # smoke
    source = inspect.getsource(auth_module.verify_trigger_secret)
    assert "trigger_secret_for(job)" in source, (
        "verify_trigger_secret must resolve the configured secret through "
        "trigger_secret_for, not re-derive it"
    )
    assert ".strip()" in inspect.getsource(auth_module.trigger_secret_for), (
        "the whitespace-only rule has been deleted: a secret of '   ' would count "
        "as configured and the header '   ' would authenticate a caller"
    )


# ===========================================================================
# behavioural -- the matrix
# ===========================================================================
@pytest.fixture
def matrix_client(owner_engine: Engine) -> Iterator[TestClient]:
    """The real application, plus the trigger router `create_app` does not mount."""
    app = _app_with_every_trigger_route()
    with TestClient(app) as client:
        yield client
    with owner_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM audit_log WHERE action LIKE 'trigger.%' AND subject LIKE :p"),
            {"p": "%guardmatrix%"},
        )
        conn.execute(
            text("DELETE FROM budget_reservations WHERE scope LIKE :p"), {"p": "%guardmatrix%"}
        )
        conn.execute(text("DELETE FROM budget_ledger WHERE scope LIKE :p"), {"p": "%guardmatrix%"})
        # A body that carries no `run_id` is legitimate and gets the generated
        # `<job>-<stamp>` fallback, so the ordering tests below fire real claims
        # that no `guardmatrix` pattern matches. Swept by action + shape rather
        # than left behind: a test that seeds the shared tables is a test that
        # makes some other suite fail on a different day.
        for job in auth_module.TRIGGER_JOBS:
            conn.execute(
                text(
                    "DELETE FROM audit_log WHERE action = :action "
                    "AND subject ~ ('^' || :job || '-[0-9]{8}T[0-9]{6}[0-9]*Z$')"
                ),
                {"action": f"trigger.{job}", "job": job},
            )
            for table in ("budget_reservations", "budget_ledger"):
                conn.execute(
                    text(
                        f"DELETE FROM {table} "
                        "WHERE scope ~ ('^run:' || :job || '-[0-9]{8}T[0-9]{6}[0-9]*Z$')"
                    ),
                    {"job": job},
                )


def _fire(client: TestClient, path: str, presented: str | None) -> int:
    headers = {} if presented is None else {auth_module.TRIGGER_SECRET_HEADER: presented}
    run_id = unique("guardmatrix").replace("-", "")[:60]
    if path.endswith("/records"):
        body = {
            "source": "crm",
            "entity_type": "contact",
            "generation": 970,
            "records": [],
            "run_id": run_id,
            # authorised cells must not leave rows behind: the question here is the
            # status code, not the landing.
            "persist": False,
        }
    else:
        body = {"run_id": run_id}
    return client.post(path, json=body, headers=headers).status_code


@pytest.mark.parametrize(("path", "job"), TRIGGER_ENDPOINTS, ids=lambda v: str(v).strip("/"))
@pytest.mark.parametrize(("case", "configured"), CONFIGURATIONS, ids=[c for c, _ in CONFIGURATIONS])
def test_the_guard_matrix(
    matrix_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    job: str,
    case: str,
    configured: str | None,
) -> None:
    """Every configuration x every presentation, on every mutating endpoint.

    Exactly one cell per row may be admitted: a *usable* configured secret
    presented back verbatim. Everything else -- including a caller who presents
    the very whitespace string the deployment configured -- is 401.
    """
    right = SYNC_SECRET if job == auth_module.JOB_SYNC else RECONCILE_SECRET
    value = right if case == "right" else configured
    env_settings(
        monkeypatch,
        TRIGGER_SECRET_SYNC=value if job == auth_module.JOB_SYNC else None,
        TRIGGER_SECRET_RECONCILE=value if job == auth_module.JOB_RECONCILE else None,
        TRIGGER_SECRET=None,
    )
    usable = isinstance(value, str) and bool(value.strip())

    for presented in {*PRESENTATIONS, value}:
        status = _fire(matrix_client, path, presented)
        admitted = usable and presented == value
        if admitted:
            assert status != 401, (
                f"{path}: the configured secret {value!r} was refused -- this guard "
                "is a closed door, not authentication"
            )
        else:
            assert status == 401, (
                f"{path}: configured={value!r} ({case}) admitted presented="
                f"{presented!r} with {status}. A mutating endpoint whose "
                "authentication depends on which of two implementations it reached "
                "is the drift this project exists to prevent."
            )


def test_the_deprecated_single_secret_is_the_sync_jobs_alone(
    matrix_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`TRIGGER_SECRET` is a fallback for `sync`, and never widens `reconcile`.

    Folding two implementations into one may not quietly hand a second job a
    credential it did not accept before.
    """
    env_settings(
        monkeypatch,
        TRIGGER_SECRET_SYNC=None,
        TRIGGER_SECRET_RECONCILE=None,
        TRIGGER_SECRET=SYNC_SECRET,
    )
    assert auth_module.trigger_secret_for("sync") == SYNC_SECRET
    assert auth_module.trigger_secret_for("reconcile") is None
    assert _fire(matrix_client, "/internal/ingest/records", SYNC_SECRET) != 401
    assert _fire(matrix_client, "/internal/sync", SYNC_SECRET) != 401
    assert _fire(matrix_client, "/internal/reconcile", SYNC_SECRET) == 401


def test_a_whitespace_only_deprecated_secret_is_also_unusable(
    matrix_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule applies to every field in the table, not only the per-job one."""
    env_settings(
        monkeypatch, TRIGGER_SECRET_SYNC=None, TRIGGER_SECRET_RECONCILE=None, TRIGGER_SECRET="   "
    )
    assert auth_module.trigger_secret_for("sync") is None
    for presented in (None, "", "   ", "\t", SYNC_SECRET):
        assert _fire(matrix_client, "/internal/ingest/records", presented) == 401
        assert _fire(matrix_client, "/internal/sync", presented) == 401


def test_a_usable_secret_is_never_silently_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`"  abc  "` is usable, and the caller must present it *including* the padding.

    The usability rule judges the value; it does not rewrite it. Trimming would
    accept a credential the deployment never configured.
    """
    padded = "  abc  "
    env_settings(monkeypatch, TRIGGER_SECRET_SYNC=padded, TRIGGER_SECRET=None)
    assert auth_module.trigger_secret_for("sync") == padded
    assert auth_module.verify_trigger_secret("sync", padded) is True
    assert auth_module.verify_trigger_secret("sync", "abc") is False
    assert auth_module.verify_trigger_secret("sync", padded.strip()) is False


def test_ingest_and_internal_sync_agree_on_every_configuration(
    matrix_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same job, the same verdict, whichever URL a caller used.

    This is the divergence stated directly: two endpoints of one job must never
    disagree about whether a given (configured, presented) pair authenticates.
    """
    for value in (None, "", "   ", "\t", " x ", SYNC_SECRET):
        env_settings(monkeypatch, TRIGGER_SECRET_SYNC=value, TRIGGER_SECRET=None)
        for presented in {*PRESENTATIONS, value}:
            records = _fire(matrix_client, "/internal/ingest/records", presented) == 401
            sync = _fire(matrix_client, "/internal/sync", presented) == 401
            assert records == sync, (
                f"configured={value!r} presented={presented!r}: "
                f"/internal/ingest/records denied={records}, /internal/sync denied={sync}"
            )


# ===========================================================================
# ordering -- 401 precedes 422 (R19)
# ===========================================================================
#: Bodies that a *good* caller could never send. Each one made FastAPI answer
#: 422 to an **unauthenticated** request, because a declared body model is read
#: and validated before any handler runs -- so the envelope was judged before the
#: credential was, and the shape of the envelope was described to a caller who
#: had presented nothing. R19 is unconditional: "requests without it are 401".
BAD_BODIES: tuple[tuple[str, bytes], ...] = (
    ("empty_object", b"{}"),
    ("wrong_types", b'{"generation": "not-an-int", "records": 7}'),
    ("truncated_json", b"{"),
    ("not_json_at_all", b"<html/>"),
    ("json_array", b"[1, 2, 3]"),
    ("json_scalar", b'"just a string"'),
    ("unknown_field_only", b'{"nonsense": true}'),
    ("run_id_wrong_type", b'{"run_id": []}'),
    ("run_id_unstorable", b'{"run_id": "a\\u0000b"}'),
    ("huge_generation", b'{"generation": 99999999999999999999}'),
)

#: Every way of not being authenticated. `None` is "no header at all"; the rest
#: are headers a caller can present without knowing the secret.
UNAUTHENTICATED: tuple[str | None, ...] = (None, "", " ", "wrong")


@pytest.mark.parametrize(("path", "job"), TRIGGER_ENDPOINTS, ids=lambda v: str(v).strip("/"))
@pytest.mark.parametrize(("case", "body"), BAD_BODIES, ids=[c for c, _ in BAD_BODIES])
def test_authentication_precedes_body_validation(
    matrix_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    job: str,
    case: str,
    body: bytes,
) -> None:
    """A bad body with no credential is 401, never 422 -- on every mutating route.

    Two things are wrong with answering 422 here and only one of them is R19.
    The other is that a 422 describing which envelope fields are missing is a
    schema hint handed to a caller who has not authenticated at all.
    """
    env_settings(
        monkeypatch,
        TRIGGER_SECRET_SYNC=SYNC_SECRET,
        TRIGGER_SECRET_RECONCILE=RECONCILE_SECRET,
        TRIGGER_SECRET=None,
    )
    for presented in UNAUTHENTICATED:
        headers = {"content-type": "application/json"}
        if presented is not None:
            headers[auth_module.TRIGGER_SECRET_HEADER] = presented
        response = matrix_client.post(path, content=body, headers=headers)
        assert response.status_code == 401, (
            f"{path} answered {response.status_code} to the {case!r} body presented "
            f"with {presented!r}. R19: a request without the secret is 401, whatever "
            "its body looks like -- and the body must not be parsed to find that out."
        )
        assert response.json()["type"].endswith("unauthorized")


@pytest.mark.parametrize(("case", "body"), BAD_BODIES, ids=[c for c, _ in BAD_BODIES])
def test_an_authenticated_caller_still_gets_the_body_verdict(
    matrix_client: TestClient, monkeypatch: pytest.MonkeyPatch, case: str, body: bytes
) -> None:
    """The control. Moving auth in front of parsing must not disable parsing.

    Without this, "401 always" would satisfy the test above -- and a guard that
    answers 401 to a valid credential is a closed door, not authentication.
    """
    env_settings(
        monkeypatch,
        TRIGGER_SECRET_SYNC=SYNC_SECRET,
        TRIGGER_SECRET_RECONCILE=RECONCILE_SECRET,
        TRIGGER_SECRET=None,
    )
    for path, job in TRIGGER_ENDPOINTS:
        secret = SYNC_SECRET if job == auth_module.JOB_SYNC else RECONCILE_SECRET
        response = matrix_client.post(
            path,
            content=body,
            headers={
                "content-type": "application/json",
                auth_module.TRIGGER_SECRET_HEADER: secret,
            },
        )
        assert response.status_code != 401, (
            f"{path} refused a valid credential for the {case!r} body"
        )
        assert response.status_code < 500, (
            f"{path} answered {response.status_code} to the {case!r} body; a "
            "malformed envelope is a 4xx, never a server fault (R2)"
        )
