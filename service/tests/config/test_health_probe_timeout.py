"""`HEALTH_PROBE_TIMEOUT_SECONDS` has to *bind*, not merely be readable.

`recon/health.py` carried ``HEALTH_PROBE_TIMEOUT_SECONDS = 2.0`` as a module
constant with no override. That is shorter than a cold start of a scale-to-zero
Postgres, and the consequence is not a slow probe -- it is a failed deploy:
`probe_database` opens a real connection, reports `timeout` when it overruns,
`timeout` is at or above `_FATAL`, so `/health` answers **503**;
`infra/render.yaml` sets ``healthCheckPath: /health`` and Render's blueprint spec
exposes no health-check timeout of its own, so the instance is marked unhealthy
and never receives traffic.

The fix is `Settings.health_probe_timeout_seconds`. The whole risk of a fix like
that is a knob that is read and never used -- a check that cannot fail, which
this project has already shipped more than once -- so the tests here are
deliberately **behavioural and adversarial**:

* :func:`test_a_configured_bound_binds_the_real_database_probe` points
  `DATABASE_URL` at a real listening socket that accepts the connection and then
  never speaks the Postgres protocol, so `probe_database` genuinely hangs, and
  measures the wall clock. Real engine, real psycopg, real libpq, no stub. The
  ``2.6`` case is the deploy scenario: it is **longer than the old hardcoded
  2.0**, so it fails against the previous code and passes only if the variable
  is actually in force;
* :func:`test_a_configured_bound_binds_the_source_probe` does the same for a
  source that never returns, at two values both *below* 2.0, so a silent
  fallback to the default is a failure rather than a rounding difference;
* :func:`test_the_inner_source_read_uses_the_configured_bound` pins the half of
  this that is easy to miss: `_probe_via_port` passed the module constant to
  `read_bounded` while the surrounding watchdog used the caller's bound, so
  raising the bound would have widened the watchdog and left the inner read
  exactly as tight.
"""

from __future__ import annotations

import re
import socket
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from pydantic import ValidationError

import recon.db
import recon.health
from recon.adapters import FaultInjectingAdapter, stub_records
from recon.config import (
    DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS,
    REPO_ROOT,
    Settings,
    get_settings,
)
from recon.health import health_probe_timeout, probe_database, probe_source

#: The field this is all about.
FIELD = "health_probe_timeout_seconds"

ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _env_var(field: str) -> str:
    """The environment variable a `Settings` field is populated from."""
    prefix = Settings.model_config.get("env_prefix") or ""
    return f"{prefix}{field}".upper()


ENV_VAR = _env_var(FIELD)


@pytest.fixture
def configure_bound(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Set ``HEALTH_PROBE_TIMEOUT_SECONDS`` in the real process environment.

    `get_settings` is `lru_cache`d, so the cache is dropped on both sides: on the
    way in so the new value is seen, and on the way out so the next test reads
    the environment `monkeypatch` has restored rather than this one's.
    """

    def _set(value: str) -> None:
        monkeypatch.setenv(ENV_VAR, value)
        get_settings.cache_clear()

    get_settings.cache_clear()
    yield _set
    get_settings.cache_clear()


@pytest.fixture
def black_hole_dsn() -> Iterator[str]:
    """A DSN whose host accepts TCP and then says nothing, ever.

    A listening socket that never calls `accept()` still completes the client's
    handshake out of the listen backlog, so libpq connects and then waits for a
    server greeting that never comes. That is a *real* indefinite hang on the
    real connection path -- no patched engine, no fake connection object -- which
    is what makes the measured elapsed time evidence about `probe_database`
    rather than about a stub.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    try:
        yield f"postgresql://nobody:nothing@127.0.0.1:{port}/void"
    finally:
        listener.close()


@pytest.fixture
def real_engine_at(
    monkeypatch: pytest.MonkeyPatch, configure_bound: Callable[[str], None]
) -> Iterator[Callable[[str], None]]:
    """Point `recon.db.get_engine` at a DSN, and put it back afterwards."""

    def _point_at(dsn: str) -> None:
        monkeypatch.setenv("DATABASE_URL", dsn)
        get_settings.cache_clear()
        recon.db.get_engine.cache_clear()

    recon.db.get_engine.cache_clear()
    yield _point_at
    recon.db.get_engine.cache_clear()


# ===========================================================================
# the default, and the name


def test_the_default_is_the_two_seconds_the_module_used_to_hardcode() -> None:
    """`Nothing changes locally` is a claim, so it is pinned."""
    assert DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS == 2.0
    assert Settings.model_fields[FIELD].default == 2.0


def test_the_variable_is_named_what_the_docs_and_the_blueprint_call_it() -> None:
    """A rename here silently un-documents the variable; that is a test's job."""
    assert ENV_VAR == "HEALTH_PROBE_TIMEOUT_SECONDS"


def test_env_example_documents_the_variable_with_the_real_default() -> None:
    """The example file must not describe a different default from the code.

    `test_env_example_contract.py` proves the variable is *mentioned*. It cannot
    know whether the number next to it is still the one `Settings` uses, and a
    documented default that drifts is how an operator ends up tuning against a
    value that no longer exists.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    match = re.search(rf"^#\s*{ENV_VAR}=(?P<value>\S+)\s*$", text, flags=re.MULTILINE)
    assert match, f"{ENV_EXAMPLE.name} does not document {ENV_VAR} as a commented default"
    assert float(match.group("value")) == DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS


# ===========================================================================
# the value reaches the resolver


@pytest.mark.parametrize("value", ["0.35", "1.75", "12.5", "30"])
def test_the_resolver_reads_the_environment(
    configure_bound: Callable[[str], None], value: str
) -> None:
    configure_bound(value)
    assert health_probe_timeout() == float(value)


@pytest.mark.parametrize("value", ["0", "0.0", "-1", "-0.5"])
def test_a_non_positive_bound_is_refused_instead_of_breaking_health_forever(
    configure_bound: Callable[[str], None], value: str
) -> None:
    """`join(0)` returns at once, so every probe would report `timeout`.

    A bound of zero is not a fast health check; it is a permanent 503 wearing the
    costume of a configured value -- the exact failure this field exists to
    prevent. Pydantic refuses it, which names the field instead of hiding.
    """
    configure_bound(value)
    with pytest.raises(ValidationError) as raised:
        get_settings()
    assert FIELD in str(raised.value)


# ===========================================================================
# ...and is what the probes actually enforce


@pytest.mark.parametrize("bound", [0.4, 1.0, 2.6])
def test_a_configured_bound_binds_the_real_database_probe(
    configure_bound: Callable[[str], None],
    real_engine_at: Callable[[str], None],
    black_hole_dsn: str,
    bound: float,
) -> None:
    """The deploy-breaking case, measured on the real connection path.

    ``2.6`` is the whole point: it is longer than the 2.0 this module used to
    hardcode, so under the previous code the probe would have been abandoned at
    2.0 and this case would fail. A Neon compute waking from suspend is exactly
    that -- a connection that is going to succeed, given a few more seconds than
    a constant nobody could change was willing to wait.
    """
    configure_bound(str(bound))
    real_engine_at(black_hole_dsn)

    started = time.monotonic()
    result = probe_database()
    elapsed = time.monotonic() - started

    assert result["status"] == "timeout", result
    assert elapsed >= bound * 0.9, f"probe gave up after {elapsed:.2f}s, before its {bound}s bound"
    assert elapsed < bound + 1.0, f"probe ran {elapsed:.2f}s under a {bound}s bound"


@pytest.mark.parametrize("bound", [0.25, 0.75])
def test_a_configured_bound_binds_the_source_probe(
    configure_bound: Callable[[str], None], bound: float
) -> None:
    """Both values are well under 2.0, so falling back to the default fails."""
    configure_bound(str(bound))
    adapter = FaultInjectingAdapter(source_id="crm", mode="hang", records=stub_records(1))

    started = time.monotonic()
    result = probe_source(adapter)
    elapsed = time.monotonic() - started

    assert result["status"] == "timeout", result
    assert elapsed >= bound * 0.9, f"source probe gave up after {elapsed:.2f}s"
    assert elapsed < 1.5, (
        f"source probe ran {elapsed:.2f}s under a {bound}s bound; it is still using the "
        "2.0s default, so the variable is read and not used"
    )


def test_an_explicit_timeout_argument_still_wins_over_the_environment(
    configure_bound: Callable[[str], None],
) -> None:
    """The environment is the *default*, not an override of the caller.

    `tests/ingest/test_health.py` bounds its own probes explicitly to keep the
    suite fast; a variable that overruled the argument would make those tests
    hostage to whatever the ambient environment says.
    """
    configure_bound("30")
    adapter = FaultInjectingAdapter(source_id="crm", mode="hang", records=stub_records(1))

    started = time.monotonic()
    result = probe_source(adapter, timeout=0.2)
    elapsed = time.monotonic() - started

    assert result["status"] == "timeout"
    assert elapsed < 1.0, f"the explicit 0.2s argument was ignored: {elapsed:.2f}s"


def test_the_inner_source_read_uses_the_configured_bound(
    configure_bound: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_probe_via_port` fed `read_bounded` the module constant, not the bound.

    So the outer watchdog would have widened with the variable while the inner
    read stayed pinned at 2.0 -- a source that needs four seconds would still be
    cut off at two, and `/health` would still say `down`. The argument itself is
    what is under test, so `read_bounded` is replaced by a recorder; the real
    read path is covered by the timing tests above.
    """
    recorded: dict[str, Any] = {}

    class _Record:
        source_id = "crm"
        entity_type = "contact"
        natural_key = "k"

    def _recorder(adapter: Any, generation: int, **kwargs: Any) -> Iterator[Any]:
        recorded.update(kwargs)
        recorded["generation"] = generation

        def _stream() -> Iterator[Any]:
            """A generator, because `_probe_via_port` closes the stream it opens."""
            yield _Record()

        return _stream()

    class _Port:
        """A `ReadOnlyAdapter` with no `probe()`, so the port fallback is used."""

        def generations(self) -> list[int]:
            return [7, 9]

        def read(self, generation: int) -> Iterator[Any]:  # pragma: no cover - recorder replaces it
            return iter(())

    monkeypatch.setattr(recon.health, "read_bounded", _recorder)
    configure_bound("4.5")

    result = probe_source(_Port())

    assert result["status"] == "ok", result
    assert recorded["generation"] == 9
    assert recorded["stall_timeout"] == 4.5, (
        "the adapter read is still bounded by the old module constant, so raising "
        "HEALTH_PROBE_TIMEOUT_SECONDS widens only the watchdog"
    )
