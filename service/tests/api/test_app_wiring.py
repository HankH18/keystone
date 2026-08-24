"""What `recon.app.create_app()` actually wires: both job handlers, and CORS.

Everything here drives the **real factory**. Not a bare `FastAPI()` with a router
added, not a fixture that registers a handler -- `create_app()`, the function
`make serve` and `infra/Dockerfile` call, because the two defects this file
covers were both invisible to a test that assembled its own app:

* `POST /internal/reconcile` was mounted and *unbound*. The route existed, the
  secret was checked, `claim_run` consumed the run id, the budget scope was
  provisioned -- and `_handler_for("reconcile")` returned `None`, so the endpoint
  logged `internal.handler_unbound` and answered HTTP 200
  `{"status": "started", "handler": "unbound"}`. `infra/render.yaml` has an
  hourly cron pointed at it, and "200 started" is what a cron health check reads
  as success. Every trigger test that covered reconcile registered its own
  handler first, so none of them could see it;
* there was no CORS middleware at all. The dashboard is a static site on a
  different origin, so every request it makes is refused **by the browser**,
  before it is sent -- which means the service logs show nothing whatsoever, and
  no server-side test can notice.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from recon.api.internal import APP_HANDLER_STATE, JOB_RECONCILE, JOB_SYNC, SYNC_STAGES, sync_job
from recon.app import CORS_ORIGINS_ENV, DEFAULT_CORS_ORIGINS, allowed_origins, create_app
from recon.reconciler import reconcile_job
from tests.budget.support import env_settings

DASHBOARD_ORIGIN = "https://keystone-dashboard.example.test"


@pytest.fixture(autouse=True)
def _settings_isolation() -> Iterator[None]:
    """No test here may leave a cached `Settings` behind. This is not optional.

    `recon.config.get_settings` is `lru_cache`d, so `env_settings` clears the
    cache to make an environment change visible -- and the *next* `get_settings()`
    then rebuilds from whatever the environment says at that moment. `monkeypatch`
    restores the variables at teardown, but nothing rebuilds the cached object,
    so a test that points `DATABASE_URL` at another principal leaves that
    principal cached **for the rest of the pytest session**.

    Measured, before this fixture existed: the `TEMPORARY`-probe test below sets
    `DATABASE_URL` to the `recon_writer` login, and every later suite that
    resolves its DSN lazily got it -- `tests/budget` failed with `permission
    denied for table budget_ledger` on a fixture that had nothing to do with
    this file. `tests/budget/support.py` carries the same guard
    (`_settings_cache_isolation`) and `tests/llm/conftest.py` re-exports it;
    `tests/api` does not, so this module brings its own.
    """
    from recon.config import get_settings
    from recon.db import reset_engine_cache

    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


@pytest.fixture
def factory_env() -> Iterator[None]:
    """Restore `KEYSTONE_CORS_ORIGINS` after a test changes it."""
    previous = os.environ.get(CORS_ORIGINS_ENV)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(CORS_ORIGINS_ENV, None)
        else:
            os.environ[CORS_ORIGINS_ENV] = previous


def _handlers(app: FastAPI) -> dict[str, object]:
    return dict(getattr(app.state, APP_HANDLER_STATE, {}))


# ===========================================================================
# W1 -- the reconcile trigger has a body
# ===========================================================================
def test_the_factory_binds_both_job_handlers() -> None:
    """The exact assertion that was false: `reconcile` had no entry at all.

    Identity, not truthiness: a handler that is *some* callable would pass a
    `is not None` check while running the wrong job.
    """
    handlers = _handlers(create_app())

    assert handlers.get(JOB_SYNC) is sync_job
    assert handlers.get(JOB_RECONCILE) is reconcile_job, (
        f"create_app() bound {sorted(handlers)} -- `reconcile` must map to "
        "recon.reconciler.reconcile_job, or POST /internal/reconcile authenticates, "
        "consumes the run id, provisions a budget scope and answers "
        '200 {"status": "started", "handler": "unbound"} to an hourly cron'
    )


def test_the_reconcile_route_resolves_to_a_handler_on_this_app() -> None:
    """Through `_handler_for`, the lookup the endpoint itself performs.

    The previous test reads `app.state`; this one asks the same question the way
    `_trigger` asks it, so a change to the lookup cannot leave the binding
    unreachable while the state dict still looks right.
    """
    from recon.api.internal import _handler_for

    app = create_app()
    assert _handler_for(JOB_RECONCILE, app) is reconcile_job
    assert _handler_for(JOB_SYNC, app) is sync_job


def test_binding_is_scoped_to_the_application_not_the_interpreter() -> None:
    """`create_app()` must not bind into the module-global registry.

    `register_job_handler`'s own docstring is the requirement: a factory writing
    into `_HANDLERS` would make every bare `FastAPI()` in the process -- every
    trigger test's app -- start running the real pipeline.
    """
    from recon.api.internal import _HANDLERS

    create_app()
    assert JOB_RECONCILE not in _HANDLERS
    assert JOB_SYNC not in _HANDLERS


# ===========================================================================
# W2 -- the sync pipeline's stage list
# ===========================================================================
def test_sync_stages_names_the_invariant_pass() -> None:
    """R5: a completed sync runs the committed rule set.

    `SYNC_STAGES` was `("ingest", "materialize")`, so a successful sync left
    `invariant_results` and `conflicts` at zero and the rule set ran only from
    the CLI and the offline grading harness.
    """
    assert SYNC_STAGES == ("ingest", "materialize", "invariants")


def test_every_sync_return_path_reports_the_full_stage_list() -> None:
    """The response's `stages` is `SYNC_STAGES`, on every branch that returns.

    Read off the source rather than asserted on one live run, because the branch
    that is easy to forget is the cheap one -- the `already_current` early return
    -- and it is also the one a re-fired cron takes almost every time.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(sync_job).lstrip())
    returns = [node for node in ast.walk(tree) if isinstance(node, ast.Return)]
    assert returns, "sync_job returns nothing; this test is looking at the wrong function"

    for node in returns:
        assert isinstance(node.value, ast.Dict), ast.dump(node)
        keys = {key.value for key in node.value.keys if isinstance(key, ast.Constant)}
        assert "stages" in keys
        for stage in SYNC_STAGES:
            assert stage in keys, (
                f"a sync_job return path reports {sorted(keys)} and does not carry "
                f"{stage!r}. Every stage SYNC_STAGES claims must appear in the "
                "summary, or the response advertises work whose result is absent"
            )


# ===========================================================================
# W2 -- which principal the detection pass connects as
# ===========================================================================
def test_the_invariant_stage_prefers_the_ops_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """`OPS_DATABASE_URL` wins over `DATABASE_URL`, and this is not cosmetic.

    On the deployed service `DATABASE_URL` names `recon_writer` -- the capped
    party (`infra/render.yaml`) -- and migration 0006 revoked `TEMPORARY` on the
    database from all three boundary roles, so the invariant engine's `TEMP`
    tables cannot exist on that connection. Resolving this from `DATABASE_URL`
    passes locally, where the configured principal *is* the owner, and fails on
    every firing in production.
    """
    from recon.api.internal import _invariant_dsn

    env_settings(
        monkeypatch,
        DATABASE_URL="postgresql://recon_writer@db.example:5432/keystone",
        OPS_DATABASE_URL="postgresql://owner:pw@db.example:5432/keystone",
    )
    assert _invariant_dsn() == "postgresql://owner:pw@db.example:5432/keystone"


def test_the_invariant_stage_falls_back_to_the_configured_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local development and the CLI: the configured principal *is* the owner."""
    from recon.api.internal import _invariant_dsn

    env_settings(
        monkeypatch,
        DATABASE_URL="postgresql://owner:pw@localhost:55432/keystone",
        OPS_DATABASE_URL=None,
    )
    assert _invariant_dsn() == "postgresql://owner:pw@localhost:55432/keystone"


def test_a_principal_without_temporary_is_reported_as_a_configuration_fault(
    dataset: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure is legible, and it is a stage failure rather than a bare 500.

    Driven against a real `recon_writer` login on the real migrated database, so
    what is asserted is the actual privilege migration 0006 removed -- not a
    patched `has_database_privilege`.
    """
    from recon.api.internal import SyncFailed, run_invariant_stage
    from recon.db import ROLE_RECON_WRITER, role_url

    del dataset
    writer_dsn = role_url(ROLE_RECON_WRITER).render_as_string(hide_password=False)
    env_settings(monkeypatch, DATABASE_URL=writer_dsn, OPS_DATABASE_URL=None)

    with pytest.raises(SyncFailed) as excinfo:
        run_invariant_stage("app-wiring-temp-probe")

    assert excinfo.value.stage == "invariants"
    message = str(excinfo.value)
    assert "TEMPORARY" in message
    assert "OPS_DATABASE_URL" in message


# ===========================================================================
# CORS
# ===========================================================================
def test_the_default_origin_list_is_not_a_wildcard() -> None:
    """`*` is a permission nobody chose; naming an origin is one env variable."""
    assert "*" not in DEFAULT_CORS_ORIGINS
    assert allowed_origins("") == list(DEFAULT_CORS_ORIGINS)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://a.test", ["https://a.test"]),
        ("https://a.test,https://b.test", ["https://a.test", "https://b.test"]),
        ("  https://a.test ,, https://b.test  ", ["https://a.test", "https://b.test"]),
        ("https://a.test,https://a.test", ["https://a.test"]),
        ("   ", list(DEFAULT_CORS_ORIGINS)),
    ],
)
def test_the_origin_list_is_parsed_from_the_environment(raw: str, expected: list[str]) -> None:
    assert allowed_origins(raw) == expected


def test_a_configured_origin_is_answered_on_a_preflight(factory_env: None) -> None:
    """The OPTIONS the browser sends before any `X-Api-Key` request.

    A custom request header is not a CORS-simple header, so *every* dashboard
    call is preflighted. This is answered by the middleware and never reaches a
    route, which is why it needs no database.
    """
    os.environ[CORS_ORIGINS_ENV] = DASHBOARD_ORIGIN
    with TestClient(create_app()) as client:
        response = client.options(
            "/api/conflicts",
            headers={
                "Origin": DASHBOARD_ORIGIN,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-api-key",
            },
        )

    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == DASHBOARD_ORIGIN
    assert "x-api-key" in response.headers["access-control-allow-headers"].lower()
    # Never credentialed: the dashboard authenticates with a header, not a cookie,
    # and `allow_origins=["*"]` plus credentials is the combination that turns any
    # page on the internet into an authenticated client.
    assert "access-control-allow-credentials" not in {name.lower() for name in response.headers}


def test_an_unconfigured_origin_is_refused_on_a_preflight(factory_env: None) -> None:
    """The allow-list is an allow-list. This is what makes it worth having."""
    os.environ[CORS_ORIGINS_ENV] = DASHBOARD_ORIGIN
    with TestClient(create_app()) as client:
        response = client.options(
            "/api/conflicts",
            headers={
                "Origin": "https://evil.example.test",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-api-key",
            },
        )

    assert "access-control-allow-origin" not in {name.lower() for name in response.headers}


def test_an_actual_response_carries_the_allow_origin_header(factory_env: None) -> None:
    """A preflight that passes is useless if the real response omits the header.

    `/openapi.json` is used because it needs no database and no credential: the
    subject here is the middleware, not the route.
    """
    os.environ[CORS_ORIGINS_ENV] = DASHBOARD_ORIGIN
    with TestClient(create_app()) as client:
        response = client.get("/openapi.json", headers={"Origin": DASHBOARD_ORIGIN})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == DASHBOARD_ORIGIN
