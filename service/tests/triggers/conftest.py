"""Fixtures for the triggers tests: the same live-database harness the budget tests use.

Plus one repair that is not about triggers at all, and is documented here rather
than left as a mystery.

**structlog can arrive at this package already bound to a closed file.**
``tests/privacy/test_logging_installed.py``'s ``pristine_process`` fixture calls
``configure_logging_once()`` in its *teardown*, while pytest's per-test capture
is still installed -- so ``structlog.WriteLoggerFactory(file=sys.stderr)``
captures pytest's capture object, and pytest closes it when that test ends. Every
log line emitted afterwards in the same process raises
``ValueError: I/O operation on closed file``, and an endpoint that logs a
rejection before returning its 4xx therefore answers **500**.

This is a pre-existing defect in the test harness, not in the service, and it is
not confined to this package: ``pytest tests/privacy tests/ingest/test_http_rejections.py``
turns five ingest tests red the same way, on code this ticket never touched. It
is invisible in the default full-suite order only because ``tests/ingest`` sorts
before ``tests/privacy`` and ``tests/triggers`` sorts after.

``tests/privacy`` belongs to another ticket, so the fix goes there and not here.
What this package does instead is refuse to *inherit* the poison: every test
starts with structlog bound to a stream this fixture owns and does not close.
It is a repair of the harness, never of an assertion -- no test below asserts on
log output, and each one still exercises the real logging path.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from recon.logging import configure_logging, configure_logging_once, reset_logging_configuration
from tests.budget.support import (  # noqa: F401  (re-exported as fixtures)
    _settings_cache_isolation,
    configured_url,
    make_scope,
    owner_engine,
)


def _uncache_recon_loggers() -> None:
    """Forget the bound logger structlog memoised on every `recon.*` proxy.

    `configure_logging(cache=True)` -- the production setting -- makes
    `structlog.get_logger(...)` memoise the assembled bound logger on the lazy
    proxy the first time it is used, by rebinding `proxy.bind` in the instance
    dict. A module-level `log = get_logger("recon.api.internal")` is therefore
    pinned to whatever stream the FIRST test in the session to touch it
    configured, and re-configuring structlog afterwards does not move it.

    So the proxies are found by walking the already-imported `recon.*` modules
    rather than by listing them: a module that grows a logger tomorrow inherits
    this instead of quietly re-acquiring the defect.
    """
    for name, module in list(sys.modules.items()):
        if not name.startswith("recon"):
            continue
        for value in vars(module).values():
            if isinstance(value, structlog._config.BoundLoggerLazyProxy):
                cast: Any = value
                cast.__dict__.pop("bind", None)


@pytest.fixture(autouse=True)
def _live_log_stream() -> Iterator[None]:
    """Bind structlog to a stream that is open for the whole test.

    Not a silencer: the full chain -- redaction processor included -- is
    installed exactly as `configure_logging_once` installs it, so every log call
    these tests drive really runs. Only the destination is ours.
    """
    # Never closed: closing it would recreate, on our own terms, the exact
    # failure this fixture exists to keep out of the package.
    sink = io.StringIO()
    configure_logging(stream=sink)
    _uncache_recon_loggers()
    try:
        yield
    finally:
        reset_logging_configuration()
        configure_logging_once()
        _uncache_recon_loggers()
