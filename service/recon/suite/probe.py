"""In-process HTTP probe: the suite talking to the real application.

Both the join check and two of the six benchmarks need to ask the service a
question. They ask it **through the mounted FastAPI application**, over
``TestClient``, with a real ``X-Api-Key`` that a real ``api_clients`` row has to
authenticate -- not by calling the route functions, and not by re-implementing
the query in SQL.

That distinction is the whole point and it has already cost this project two
endpoints. ``recon/app.py``'s docstring records them: routers that were built,
tested and left unmounted, because the tests that covered them imported the
router object directly and so passed against a surface the running service never
served. A join check that called ``recon.resolve.person_view`` would have exactly
that shape -- it would grade the resolver and say nothing about whether
``GET /api/entities/{key}`` answers at all.

**What this is NOT.** ``TestClient`` is an ASGI transport in this process. There
is no socket, no TLS, no uvicorn worker, no browser, and no network. A latency
measured here is *service-side handler + database* time and nothing else; see
:mod:`recon.bench.suite` for how the dashboard benchmark states that limit
instead of quietly calling the number a page load.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from recon.api.auth import SCOPE_ADMIN, resolve_api_key
from recon.app import create_app

__all__ = ["ADMIN_KEY_ENV", "admin_headers", "admin_key", "json_of", "probe_client"]

#: Where an operator may override the demo key. The committed default below is
#: the one migration 0003 seeds and ``.env.example`` publishes; it is a demo
#: credential for synthetic data, not a secret.
ADMIN_KEY_ENV = "DEMO_ADMIN_API_KEY"

_COMMITTED_ADMIN_KEY = "keystone-demo-admin-8c25e0b71a94f36d"


def admin_key() -> str:
    """The admin-scope API key, **verified against ``api_clients`` before use**.

    Resolved, not assumed. An unverified key would turn every probe into a 401
    and every check that depends on one into a red row blaming the endpoint for
    a credentials problem -- so the failure is raised here, naming the real
    cause, instead of being reported one layer down as something else.
    """
    candidate = os.environ.get(ADMIN_KEY_ENV, "").strip() or _COMMITTED_ADMIN_KEY
    principal = resolve_api_key(candidate)
    if principal is None:
        raise RuntimeError(
            f"the admin API key does not authenticate against this database's "
            f"api_clients table. Set {ADMIN_KEY_ENV} to the key whose sha256 is "
            "seeded by migration 0003, or run `alembic upgrade head`."
        )
    if principal.scope != SCOPE_ADMIN:
        raise RuntimeError(
            f"the configured {ADMIN_KEY_ENV} authenticates with scope "
            f"{principal.scope!r}, not {SCOPE_ADMIN!r}. The suite's probes read "
            "org-wide surfaces and a client-scoped key would silently return a "
            "tenant slice -- a smaller answer that still looks like an answer."
        )
    return candidate


def admin_headers() -> dict[str, str]:
    """Request headers carrying the verified admin key."""
    return {"X-Api-Key": admin_key()}


@contextmanager
def probe_client() -> Iterator[TestClient]:
    """A ``TestClient`` bound to a freshly built application.

    Built through :func:`recon.app.create_app`, so the route table under test is
    the deployed one: a route this factory does not mount does not exist here
    either, and a probe against it 404s loudly.

    **``httpx``'s own per-request line is deliberately left ON.** Silencing it
    would cost one ``logging.getLogger("httpx")``, and
    ``tests/privacy/test_logging_installed.py`` forbids exactly that: a stdlib
    logger acquired anywhere but ``recon/logging.py`` is a sink with no redaction
    processor in front of it, and the one-file exemption exists so the bridge can
    strip handlers, not so callers can reach past the chain for cosmetics. The
    lines are noisy -- the dashboard benchmark issues 315 requests -- but they go
    out through the installed bridge and are redacted like every other event, and
    the scorecard is printed after them.
    """
    with TestClient(create_app()) as client:
        yield client


def json_of(response: Any) -> dict[str, Any]:
    """The response body, or a loud error naming the status it actually got."""
    if response.status_code != 200:
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path} answered "
            f"{response.status_code}: {response.text[:300]}"
        )
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"expected a JSON object body, got {type(body).__name__}")
    return body
