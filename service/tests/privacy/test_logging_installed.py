"""The redaction processor must be installed in the process that actually runs.

This module exists because of a defect the other tests in this package could not
see. ``configure_logging()`` was called by exactly two files -- both of them
tests -- so every test here installed the chain itself, asserted that the chain
redacted, and passed. Meanwhile ``create_app()``, which is what ``make serve``
and ``uvicorn recon.app:create_app --factory`` run, never called it. structlog
ran its DEFAULT processor chain in the running service and **nothing was
redacted**.

So nothing here calls :func:`recon.logging.configure_logging`. Every test either
imports the real application and inspects the configuration it produced, or runs
a real entry point in a subprocess and asks the resulting process what its
structlog configuration is.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from recon.logging import (
    ENTRY_POINTS,
    configure_logging_once,
    redaction_processor,
    reset_logging_configuration,
)
from recon.privacy import is_token, redact
from tests.privacy.conftest import uncache_logger

SERVICE_ROOT = Path(__file__).resolve().parents[2]

#: Something that is unambiguously personal and unambiguously in the dataset's
#: shape, used where a fixture record would be overkill.
PROBE_EMAIL = "amriyo.fairbank@keystone.test"


@pytest.fixture
def pristine_process() -> Iterator[None]:
    """A process with NO structlog configuration, exactly as one starts.

    ``structlog.reset_defaults()`` is what makes these tests honest: it puts the
    interpreter back into the state a freshly started service is in, so a pass
    cannot come from configuration another test left behind. What the entry
    point then installs writes to ``sys.stderr``, which pytest captures --
    ``capsys.readouterr().err`` is the service's real output, not a stream a
    test handed it.
    """
    structlog.reset_defaults()
    reset_logging_configuration()
    assert not _has_redaction(), "precondition: the default chain does not redact"
    yield
    structlog.reset_defaults()
    reset_logging_configuration()
    configure_logging_once()


def _has_redaction() -> bool:
    return any(p is redaction_processor for p in structlog.get_config()["processors"])


# ---------------------------------------------------------------------------
# the real application
# ---------------------------------------------------------------------------


def test_create_app_installs_the_redaction_processor(pristine_process: None) -> None:
    """The demanded test: import the REAL app, assert the ACTIVE config redacts."""
    from recon.app import create_app

    create_app()

    processors = structlog.get_config()["processors"]
    assert redaction_processor in processors, (
        "create_app() did not install the redaction processor, so the running "
        f"service redacts nothing. Active chain: {[getattr(p, '__name__', p) for p in processors]}"
    )


def test_a_logger_the_service_already_holds_redacts_after_create_app(
    pristine_process: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not a fresh logger -- the module-level one `recon.ingest` bound at import.

    ``recon/ingest.py`` does ``log = structlog.get_logger("recon.ingest")`` at
    import time. That object is a lazy proxy, so what matters is whether the
    configuration is in place by the time it emits. This drives that exact
    object.
    """
    import recon.ingest
    from recon.app import create_app

    create_app()
    uncache_logger(recon.ingest.log)
    recon.ingest.log.error(
        "ingest.record_rejected",
        run_id="r1",
        error=ValueError(f"cannot land {{'guardian_email': '{PROBE_EMAIL}'}}"),
    )

    emitted = capsys.readouterr().err
    assert emitted, "the service logger wrote nothing"
    assert PROBE_EMAIL not in emitted, f"the running service leaked it: {emitted}"
    assert "[pii:email:" in emitted


def test_health_logger_redacts_after_create_app(
    pristine_process: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same for `recon.health`, the other module-level structlog logger."""
    import recon.health
    from recon.app import create_app

    create_app()
    uncache_logger(recon.health.log)
    recon.health.log.error("health.adapters_unavailable", detail=f"probe {PROBE_EMAIL} failed")
    emitted = capsys.readouterr().err
    assert emitted and PROBE_EMAIL not in emitted


def test_create_app_is_idempotent_about_logging(pristine_process: None) -> None:
    """Building the app twice must not stack two redaction processors."""
    from recon.app import create_app

    create_app()
    create_app()
    processors = structlog.get_config()["processors"]
    assert sum(1 for p in processors if p is redaction_processor) == 1


# ---------------------------------------------------------------------------
# every other entry point
# ---------------------------------------------------------------------------


def _calls_configure_once(path: Path) -> bool:
    """True when ``path`` contains a call to ``configure_logging_once()``."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == "configure_logging_once":
                return True
    return False


@pytest.mark.parametrize("relative", ENTRY_POINTS)
def test_every_entry_point_installs_logging(relative: str) -> None:
    """Enumerated, not spot-checked: each way a Keystone process starts.

    ``recon.logging.ENTRY_POINTS`` is the published list, so a new entry point
    that forgets the call is a one-line addition away from being caught -- and a
    file that stops calling it fails here immediately.
    """
    path = SERVICE_ROOT / relative
    assert path.exists(), f"{relative} is listed in ENTRY_POINTS but does not exist"
    assert _calls_configure_once(path), (
        f"{relative} never calls configure_logging_once(), so a process started "
        f"through it runs structlog's default chain and redacts nothing"
    )


#: ``(label, python -c source)``. Each snippet drives a REAL entry point in a
#: fresh interpreter and prints whether that process ended up with the redaction
#: processor installed. Nothing stubs anything.
_ENTRY_POINT_DRIVERS: tuple[tuple[str, str], ...] = (
    (
        "recon CLI",
        "import recon.__main__ as m\ntry:\n    m.cli(['version'])\nexcept SystemExit:\n    pass\n",
    ),
    (
        "recon.suite",
        "import recon.suite.__main__ as m\nm.main(['--list'])\n",
    ),
    (
        "recon.bench",
        "import recon.bench.__main__ as m\ntry:\n    m.main([])\nexcept SystemExit:\n    pass\n",
    ),
    (
        "recon.seed",
        "import recon.seed.__main__ as m\n"
        "try:\n    m.main(['--profile', 'not-a-profile'])\nexcept SystemExit:\n    pass\n",
    ),
    (
        # No DATABASE_URL in the probe's environment, so `main` reaches
        # `parser.error` and exits -- after `configure_logging_once()`, which is
        # the point. The cheapest path that still installs the chain.
        "recon.invariants",
        "import recon.invariants.__main__ as m\n"
        "try:\n    m.main([])\nexcept SystemExit:\n    pass\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "driver"), _ENTRY_POINT_DRIVERS, ids=[label for label, _ in _ENTRY_POINT_DRIVERS]
)
def test_entry_point_process_really_ends_up_configured(label: str, driver: str) -> None:
    """Run the entry point for real and ask the process what it configured.

    The AST test above proves the call is written down; this proves it actually
    runs. Each driver takes the cheapest path through the entry point that still
    reaches the configuration (``--list``, ``version``, an argparse error).
    """
    probe = (
        driver + "import json, structlog, recon.logging as L\n"
        "print('RESULT ' + json.dumps("
        "any(p is L.redaction_processor for p in structlog.get_config()['processors'])))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0", "HOME": str(SERVICE_ROOT)},
    )
    line = next(
        (ln for ln in result.stdout.splitlines() if ln.startswith("RESULT ")),
        None,
    )
    assert line is not None, f"{label} driver produced no result:\n{result.stdout}\n{result.stderr}"
    assert json.loads(line.removeprefix("RESULT ")) is True, (
        f"{label} ran without installing the redaction processor"
    )


# ---------------------------------------------------------------------------
# no bypass
# ---------------------------------------------------------------------------


def _recon_sources() -> list[Path]:
    return sorted(p for p in (SERVICE_ROOT / "recon").rglob("*.py"))


#: The one module allowed to touch the standard library's ``logging``: it is the
#: module that BRIDGES stdlib logging into the redacting chain
#: (``_install_stdlib_bridge``), and it cannot do that without naming the root
#: logger and the loggers whose handlers it strips.
_STDLIB_LOGGING_CHOKEPOINT = "recon/logging.py"


def test_every_logger_in_recon_comes_from_structlog() -> None:
    """Enumerate every logger the package acquires; all must be structlog's.

    That is the property that makes "redaction is applied at the sink" true: a
    structlog logger is bound to the process-wide processor chain, so installing
    the chain covers every call site. A stdlib ``logging.getLogger`` anywhere
    ELSE would be a sink with no redaction in it, and this fails if one appears.

    The exemption is the bridge itself, and it is one file wide. It is not a
    hole: the bridge exists precisely because stdlib records were reaching the
    terminal unredacted (uvicorn's access log), and
    ``test_the_stdlib_bridge_is_installed_and_redacts`` below asserts that what
    it installs actually redacts. Acquiring a stdlib logger in order to strip its
    handlers is the opposite of bypassing the chain.
    """
    offenders: list[str] = []
    for path in _recon_sources():
        relative = str(path.relative_to(SERVICE_ROOT))
        if relative == _STDLIB_LOGGING_CHOKEPOINT:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "getLogger":
                continue
            offenders.append(f"{relative}:{node.lineno}")
    assert not offenders, (
        "stdlib logging.getLogger bypasses the structlog processor chain, so "
        f"nothing redacts it: {offenders}"
    )


def test_the_stdlib_bridge_is_installed_and_redacts(
    pristine_process: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real service's OTHER log sink: a stdlib record, e.g. uvicorn's access log.

    ``uvicorn recon.app:create_app --factory`` writes its access line -- path and
    query string, both caller-controlled -- through ``logging``, which structlog's
    processor chain does not touch. So the running service printed every request
    URL verbatim next to redacted structlog events, on the same terminal.
    """
    import logging as stdlib_logging

    from recon.app import create_app
    from recon.logging import CAPTURED_STDLIB_LOGGERS

    create_app()
    access = stdlib_logging.getLogger("uvicorn.access")
    assert "uvicorn.access" in CAPTURED_STDLIB_LOGGERS
    assert access.handlers == [], "uvicorn.access kept a handler of its own"
    assert access.propagate is True

    access.info('127.0.0.1 - "GET /internal/x?guardian_email=%s HTTP/1.1" 200', PROBE_EMAIL)
    emitted = capsys.readouterr().err
    assert emitted, "the stdlib bridge wrote nothing"
    assert PROBE_EMAIL not in emitted, f"the stdlib sink leaked it: {emitted}"
    assert "[pii:email:" in emitted
    # and it is the project's JSON rendering, not a second format nobody parses
    assert json.loads(emitted.strip().splitlines()[-1])["logger"] == "uvicorn.access"


def test_the_only_logger_factories_are_the_two_known_ones() -> None:
    """`structlog.get_logger` or `recon.logging.get_logger` -- nothing else."""
    seen: set[str] = set()
    for path in _recon_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "get_logger":
                owner = func.value
                seen.add(f"{getattr(owner, 'id', '?')}.get_logger")
            elif isinstance(func, ast.Name) and func.id == "get_logger":
                seen.add("get_logger")
    assert seen <= {"structlog.get_logger", "get_logger"}, seen


def test_the_redacting_chain_is_what_a_default_process_gets(
    pristine_process: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """No mode argument anywhere: the default environment must produce `safe`."""
    configure_logging_once()
    structlog.get_logger("tests.privacy").info("probe", guardian_email=PROBE_EMAIL)
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert is_token(line["guardian_email"])


# ---------------------------------------------------------------------------
# the log has to stay readable, or default-deny gets quietly widened instead
# ---------------------------------------------------------------------------


_LEVELS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})

#: structlog keyword arguments that are machinery, not data.
_NOT_DATA = frozenset({"exc_info", "stack_info"})


def _is_logger_expression(node: ast.expr) -> bool:
    """True when ``node`` evaluates to something this package logs through.

    The walk used to require the receiver to be spelled exactly ``log``, so a
    ``logger.info(...)``, a ``self.log.info(...)`` or the chained
    ``log.bind(...).info(...)`` -- which the leak hunt itself drives, because it
    is how context is attached in this package -- passed the vocabulary check
    without being checked. This resolves the chain instead of matching a name:

    * a bare name or attribute whose last component contains ``log``;
    * any ``....bind(...)`` / ``....new(...)`` call, whose result is a logger;
    * ``get_logger(...)`` / ``structlog.get_logger(...)``.
    """
    if isinstance(node, ast.Name):
        return "log" in node.id
    if isinstance(node, ast.Attribute):
        return "log" in node.attr or _is_logger_expression(node.value)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in {"bind", "new", "unbind", "try_unbind"}:
                return _is_logger_expression(func.value)
            if func.attr == "get_logger":
                return True
        return isinstance(func, ast.Name) and func.id == "get_logger"
    return False


def _logged_keyword_names() -> dict[str, list[str]]:
    """Every keyword name the package attaches to a log event, from any receiver.

    Both positions count, because both reach the sink: the keyword arguments of
    the ``<level>(...)`` call, and the keyword arguments of a ``.bind(...)`` --
    a bound value is rendered into the event exactly like a passed one.
    """
    found: dict[str, list[str]] = {}
    for path in _recon_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            attr = node.func.attr
            if attr not in _LEVELS and attr not in {"bind", "new"}:
                continue
            if not _is_logger_expression(node.func.value):
                continue
            for keyword in node.keywords:
                if keyword.arg and keyword.arg not in _NOT_DATA:
                    found.setdefault(keyword.arg, []).append(
                        f"{path.relative_to(SERVICE_ROOT)}:{node.lineno}"
                    )
    return found


def test_the_vocabulary_walk_sees_more_than_a_receiver_named_log() -> None:
    """The walk's own competence, pinned: it used to match only ``log.<level>``.

    A rule that silently stops matching is worse than no rule, because the green
    reads the same. These are the five spellings a logger is reached through in
    this package (and in any structlog codebase); the walk has to see the keyword
    in each one, and still has to reject a receiver that is not a logger at all.
    """
    module = ast.parse(
        "log.info('e', alpha=1)\n"
        "logger.warning('e', beta=2)\n"
        "self.log.error('e', gamma=3)\n"
        "log.bind(delta=4).info('e', epsilon=5)\n"
        "get_logger('x').info('e', zeta=6)\n"
        "queue.error('e', eta=7)\n"
    )
    seen: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _LEVELS and node.func.attr not in {"bind", "new"}:
            continue
        if not _is_logger_expression(node.func.value):
            continue
        seen.update(kw.arg for kw in node.keywords if kw.arg)
    assert seen == {"alpha", "beta", "gamma", "delta", "epsilon", "zeta"}, seen


def test_every_key_the_package_logs_is_on_the_committed_vocabulary() -> None:
    """Default-deny on keys fails SAFE, and this is what keeps it from failing SILENT.

    A key the vocabulary has not met comes out as `[pii:opaque:…]`: nothing
    leaks, but the line stops being readable, and an unreadable log invites
    somebody to widen the allow-list under pressure instead of deliberately.
    So the keys the package actually logs are enumerated from the source and
    required to be on the vocabulary — the fix for a red here is one line in
    `recon.privacy.SAFE_KEYS` (or `TEXT_KEYS`, if the value is free text), added
    on purpose.
    """
    unknown = {
        name: sites
        for name, sites in _logged_keyword_names().items()
        if name not in redact({name: 1})
    }
    assert not unknown, (
        "these logged keys are not on the committed key vocabulary, so they are "
        f"emitted as opaque tokens: {unknown}"
    )
