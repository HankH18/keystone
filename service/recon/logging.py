"""Structured, privacy-safe logging (SPEC R21; DESIGN `audit_log`).

``LOG_MODE`` has exactly two values and **`safe` is the default** -- it is the
default in :class:`recon.config.Settings`, the default here, and the value
``.env.example`` ships. `full` is reachable only by putting ``LOG_MODE=full`` in
the process environment; it is a **development-only** switch and every
configuration of it emits a warning event saying so.

===========  ==========================================================
``safe``     Every log event and every ``audit_log.detail`` body goes
             through :func:`recon.privacy.redact` first. DESIGN's
             "``detail`` stores hash+preview": the *hash* is
             ``body_sha256``, a SHA-256 over the canonical JSON of the
             raw body, which proves two entries described the same thing
             without storing it; the *preview* is the redacted body,
             which keeps the structure, the field names and each value's
             kind, digest and shape.
``full``     Raw bodies. Development only. Never for a deployment holding
             real personal data.
===========  ==========================================================

**Redaction is applied to the whole event, to the leaves.** That is the point:
in this schema the personal data is not in the top-level message, it is inside
the jsonb -- an evidence packet's ``observed_values``, an action payload's
``set``, an error detail quoting the row it rejected. A redactor that only
looked at top-level strings would pass every one of those through untouched.

**Redaction is applied at the SINK, and there are FOUR sinks.** This paragraph
used to say "exactly two", and the two it did not name were the two nobody was
watching: a direct ``print`` (three entry points had one, and
``recon/suite/__main__.py`` dumped a raw Python traceback for every exception
escaping a check) and the standard library's ``logging``, through which uvicorn
writes every request path and query string to the same terminal. The list is
:data:`SINKS`, it is data rather than prose, and
``tests/privacy/test_sinks.py`` walks it against the source so a fifth cannot
appear quietly:

=========================  ===============================================
structlog event            :func:`redaction_processor` in the chain
``audit_log`` row          :func:`audit_row` / :func:`insert_audit_row`
direct terminal write      :func:`console`
stdlib ``logging`` record  :func:`_install_stdlib_bridge`
=========================  ===============================================

The ``audit_log`` row is the one with a **known gap**, and it is recorded rather
than described: :data:`AUDIT_WRITERS` lists all three writers and marks the two
that still bind ``actor``/``action``/``subject`` raw, with the exact change each
needs. Those two files belong to other tickets.

A processor chain is process-wide state, so it only covers anything if the
process installs it: :func:`configure_logging_once` is called by every entry
point in :data:`ENTRY_POINTS`, and a test asserts that each one still does. (It
did not used to be. ``configure_logging()`` was called by two test modules and
by no entry point at all, so the tests exercised a chain the running service
never had.)

Choosing a mode is a deployment decision, so the only supported way to set it is
the environment. The ``mode=`` parameter on the functions below exists so a test
can exercise the `full` branch without mutating process state; production code
should leave it alone and let :func:`log_mode` read the setting.
"""

from __future__ import annotations

import hashlib
import logging as stdlib_logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import IO, Any, Final, Literal

import structlog

from recon.config import get_settings
from recon.privacy import Redactor, canonical_json, default_redactor, redact, scrub_text

__all__ = [
    "AUDIT_INSERT_SQL",
    "AUDIT_WRITERS",
    "CAPTURED_STDLIB_LOGGERS",
    "ENTRY_POINTS",
    "LOG_MODES",
    "LOG_MODE_FULL",
    "LOG_MODE_SAFE",
    "SINKS",
    "UNROUTED_TERMINAL_WRITERS",
    "AuditWriter",
    "Sink",
    "TerminalWriter",
    "audit_detail",
    "audit_row",
    "body_sha256",
    "configure_logging",
    "configure_logging_once",
    "console",
    "get_logger",
    "insert_audit_row",
    "is_safe_mode",
    "log_mode",
    "redaction_processor",
    "reset_logging_configuration",
    "resolve_mode",
    "uvicorn_log_config",
]

LOG_MODE_SAFE: Final = "safe"
LOG_MODE_FULL: Final = "full"
LOG_MODES: Final[tuple[str, str]] = (LOG_MODE_SAFE, LOG_MODE_FULL)

LogMode = Literal["safe", "full"]

#: Emitted once per `full`-mode configuration so a deployment that turned raw
#: logging on cannot do it quietly.
FULL_MODE_WARNING: Final = "log_mode.full_is_development_only"


def log_mode() -> str:
    """The configured mode. ``safe`` unless the environment says otherwise."""
    return get_settings().log_mode


def resolve_mode(mode: str | None = None) -> str:
    """Validate ``mode``, falling back to the environment-configured one."""
    resolved = mode if mode is not None else log_mode()
    if resolved not in LOG_MODES:
        raise ValueError(f"LOG_MODE must be one of {LOG_MODES}, got {resolved!r}")
    return resolved


def is_safe_mode(mode: str | None = None) -> bool:
    """True when personal data must be redacted before it is stored."""
    return resolve_mode(mode) == LOG_MODE_SAFE


# ---------------------------------------------------------------------------
# structlog wiring
# ---------------------------------------------------------------------------


def redaction_processor(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor: redact the whole event, nested values included."""
    return default_redactor.redact(event_dict)


def _render_json(_logger: Any, _name: str, event_dict: dict[str, Any]) -> str:
    """Render with the project's one JSON spelling, so two runs compare byte-wise."""
    return canonical_json(_jsonable(event_dict))


def _jsonable(value: Any) -> Any:
    """Coerce the few non-JSON types a log event carries (UUID, datetime, Decimal).

    **In ``safe`` mode this is an identity function, and that is a property the
    tests pin.** :meth:`recon.privacy.Redactor._leaf` stringifies every value
    with no JSON spelling *before* it decides what to do with it, so by the time
    a redacted event reaches here there is nothing left to coerce. The order
    matters: while this function did the stringifying, an exception carrying a
    rejected record was passed over by the redactor (not a ``str``) and then
    written into the log by ``str(value)`` here -- after the only thing that
    could have cleaned it had finished. It survives for ``full`` mode, where
    nothing is redacted and something still has to render a ``UUID``.
    """
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _tail_processors(resolved: str) -> list[Any]:
    """Redaction (in `safe`) then rendering -- the last two steps of every chain.

    Shared by the structlog chain and by the stdlib bridge, deliberately: two
    renderers that were "kept in step by hand" is how a second sink ends up
    unredacted, which is exactly the defect this module now has a test for.
    """
    tail: list[Any] = []
    if resolved == LOG_MODE_SAFE:
        tail.append(redaction_processor)
    tail.append(_render_json)
    return tail


def configure_logging(
    *, mode: str | None = None, stream: IO[str] | None = None, cache: bool = True
) -> str:
    """Install **every** sink's chain and return the mode that was installed.

    The redaction processor is the last thing before rendering, so context
    variables and anything a caller bound onto the logger are redacted too --
    not just the keyword arguments of the final call.

    Two chains are installed, not one: structlog's, and a stdlib ``logging``
    bridge (:func:`_install_stdlib_bridge`) that ends in the *same* redaction
    processor. Without the second one, ``uvicorn.access`` printed every request
    path and query string verbatim to the same terminal -- a sink nobody was
    watching, in the process that serves real traffic.
    """
    resolved = resolve_mode(mode)
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.extend(_tail_processors(resolved))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        logger_factory=structlog.WriteLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=cache,
    )
    _install_stdlib_bridge(resolved, stream)
    global _CONSOLE_MODE
    _CONSOLE_MODE = resolved
    if resolved == LOG_MODE_FULL:
        structlog.get_logger("recon.logging").warning(
            FULL_MODE_WARNING,
            mode=resolved,
            note=(
                "LOG_MODE=full stores raw request and evidence bodies, including "
                "personal data, in the audit log. Use it in development only."
            ),
        )
    return resolved


# ---------------------------------------------------------------------------
# sink (d): the standard library's logging module
# ---------------------------------------------------------------------------

#: Marks the handler this module owns, so re-configuring replaces it instead of
#: stacking a second copy (and so a foreign handler is never silently removed
#: from the root logger by accident -- see :func:`_install_stdlib_bridge`).
_HANDLER_TAG: Final = "keystone.redacting"

#: Third-party loggers that write to the service's terminal and would otherwise
#: keep their OWN handlers, bypassing the root handler installed below.
#: ``uvicorn.access`` is the one that mattered: it renders the request line --
#: path and query string included -- and a query string is caller-controlled
#: text. Stripping the handler and turning propagation on routes the record
#: through the root handler, which redacts.
#: The level each is pinned to. Levels are set explicitly so that installing the
#: bridge changes **routing, not verbosity**: the root handler has to sit at
#: INFO for uvicorn's access line to reach it, and without a pin that would
#: silently switch on every chatty client library at INFO as a side effect of a
#: privacy fix. A record that IS emitted is redacted either way.
CAPTURED_STDLIB_LOGGERS: Final[Mapping[str, int]] = {
    "uvicorn": stdlib_logging.INFO,
    "uvicorn.access": stdlib_logging.INFO,
    "uvicorn.error": stdlib_logging.INFO,
    "uvicorn.asgi": stdlib_logging.INFO,
    "gunicorn.error": stdlib_logging.INFO,
    "gunicorn.access": stdlib_logging.INFO,
    "fastapi": stdlib_logging.INFO,
    "py.warnings": stdlib_logging.WARNING,
    "sqlalchemy": stdlib_logging.WARNING,
    "sqlalchemy.engine": stdlib_logging.WARNING,
    "alembic": stdlib_logging.INFO,
    "httpx": stdlib_logging.WARNING,
    "httpcore": stdlib_logging.WARNING,
    "anthropic": stdlib_logging.WARNING,
}


class _KeystoneHandler(stdlib_logging.StreamHandler):
    """A ``StreamHandler`` this module can recognise as its own."""


def _install_stdlib_bridge(resolved: str, stream: IO[str] | None) -> None:
    """Route stdlib ``logging`` records through the same redaction chain.

    structlog's processor chain covers structlog loggers and nothing else. Every
    library in this service's dependency tree -- uvicorn first among them --
    logs through ``logging``, and those records went to a handler that had never
    heard of the redactor. That is the second unwatched sink the docstring above
    used to deny existed.

    ``ProcessorFormatter`` is the join: a stdlib ``LogRecord`` is turned into an
    event dict, given the same level/timestamp keys, and then passed through the
    *same* :func:`redaction_processor` and renderer as a structlog event.
    """
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *_tail_processors(resolved),
        ],
    )
    handler = _KeystoneHandler(stream or sys.stderr)
    handler.set_name(_HANDLER_TAG)
    handler.setFormatter(formatter)

    root = stdlib_logging.getLogger()
    root.handlers = [handler]
    root.setLevel(stdlib_logging.INFO)
    for name, level in CAPTURED_STDLIB_LOGGERS.items():
        logger = stdlib_logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
        logger.setLevel(level)
    # `warnings.warn` is a terminal writer too, and it bypasses logging entirely
    # until this is on. With it on, a DeprecationWarning quoting a value is
    # redacted like anything else.
    stdlib_logging.captureWarnings(True)


def uvicorn_log_config(mode: str | None = None) -> dict[str, Any]:
    """A uvicorn ``log_config`` that gives uvicorn no handlers of its own.

    ``uvicorn recon.app:create_app --factory`` configures logging *before* it
    imports the application, so uvicorn's default handlers exist by the time
    :func:`configure_logging` runs -- which is why
    :func:`_install_stdlib_bridge` strips them at import time and why the
    running service is covered without this function. This exists for the
    embedded case (``uvicorn.run(..., log_config=uvicorn_log_config())``), where
    it is better to never install the unredacted handlers at all.
    """
    resolve_mode(mode)  # validate early: a bad LOG_MODE must not reach a server
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {},
        "handlers": {},
        "loggers": {
            name: {
                "handlers": [],
                "level": stdlib_logging.getLevelName(level),
                "propagate": True,
            }
            for name, level in CAPTURED_STDLIB_LOGGERS.items()
        },
        "root": {"level": "INFO", "handlers": []},
    }


# ---------------------------------------------------------------------------
# sink (c): a direct write to the terminal
# ---------------------------------------------------------------------------

#: The mode :func:`console` scrubs under. Set by :func:`configure_logging` so a
#: console write is governed by the same switch as every other sink, and
#: defaults to `safe` for a process that writes before it configures anything.
_CONSOLE_MODE: str = LOG_MODE_SAFE


def console(*values: object, stream: IO[str] | None = None, mode: str | None = None) -> None:
    """Write a line to the terminal **through the redactor**. The only ``print``.

    Some output is not a log event: `python -m recon.suite` prints a scorecard
    that a human and a grader read, and rendering it as JSON log lines would
    destroy the deliverable. But a ``print`` is still a way personal data leaves
    the process -- a check's detail string quotes what it compared -- and the
    package used to have three entry points writing to the terminal with nothing
    between them and it, including a raw ``traceback.print_exc()`` for every
    exception escaping a check.

    So the direct-write path is a chokepoint too: text goes through
    :func:`recon.privacy.scrub_text` in `safe` mode, which removes an address, a
    student number, a bare ISO date, a household id and any keyed pair, and
    leaves the layout alone. ``tests/privacy/test_sinks.py`` enumerates the
    package's terminal writers and fails if a bare ``print`` reappears.
    """
    resolved = mode if mode is not None else _CONSOLE_MODE
    text = " ".join(str(value) for value in values)
    if resolve_mode(resolved) == LOG_MODE_SAFE:
        text = scrub_text(text)
    print(text, file=stream or sys.stdout)


#: Set by :func:`configure_logging_once` so repeated entry-point calls (the CLI
#: importing the app factory, a test importing both) install the chain once.
_CONFIGURED: bool = False


def configure_logging_once() -> str:
    """Install the processor chain if this process has not already installed it.

    **Every real entry point calls this**, and that is the fix for the defect
    this function exists because of: ``configure_logging()`` used to be called
    by nothing but tests, so the tests passed while the running service ran
    structlog's DEFAULT chain and redacted nothing. The list of entry points is
    :data:`recon.logging.ENTRY_POINTS`, and
    ``tests/privacy/test_logging_installed.py`` asserts each one still calls
    this.

    Idempotent by design: an entry point must be able to call it without
    knowing whether another one already did, and calling it twice must not
    re-emit the ``full``-mode warning.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return log_mode()
    resolved = configure_logging()
    _CONFIGURED = True
    return resolved


def reset_logging_configuration() -> None:
    """Forget that the chain was installed. For tests only."""
    global _CONFIGURED
    _CONFIGURED = False


#: Every place a Keystone process starts. Each one must call
#: :func:`configure_logging_once` before it can emit a log line, because the
#: redaction processor lives in the structlog configuration and a process that
#: never installs it logs raw personal data. Paths are relative to ``service/``.
ENTRY_POINTS: Final[tuple[str, ...]] = (
    "recon/app.py",  # create_app() -- uvicorn, `make serve`, --factory
    "recon/__main__.py",  # `python -m recon` / the console script
    "recon/seed/__main__.py",  # `python -m recon.seed`
    "recon/suite/__main__.py",  # `python -m recon.suite`
    "recon/bench/__main__.py",  # `python -m recon.bench`
    "migrations/env.py",  # alembic
)


def get_logger(name: str | None = None) -> Any:
    """A bound structlog logger. Configure once at start-up first."""
    return structlog.get_logger(name) if name else structlog.get_logger()


# ---------------------------------------------------------------------------
# audit_log.detail
# ---------------------------------------------------------------------------


def body_sha256(body: Any) -> str:
    """SHA-256 over the canonical JSON of ``body`` -- DESIGN's "hash".

    Taken over the **raw** body, so two audit rows can be shown to describe the
    same thing without either of them storing it.
    """
    return hashlib.sha256(canonical_json(_jsonable(body)).encode()).hexdigest()


def audit_detail(
    body: Any, *, mode: str | None = None, redactor: Redactor | None = None
) -> dict[str, Any]:
    """Build the ``audit_log.detail`` jsonb payload for ``body``.

    ``safe`` (the default) returns ``{"mode", "body_sha256", "body"}`` where
    ``body`` is the redacted structure -- hash plus preview, per DESIGN.
    ``full`` returns the raw body under ``{"mode", "body"}`` and is
    development-only.
    """
    resolved = resolve_mode(mode)
    if resolved == LOG_MODE_FULL:
        return {"mode": LOG_MODE_FULL, "body": _jsonable(body)}
    active = redactor or default_redactor
    return {
        "mode": LOG_MODE_SAFE,
        "body_sha256": body_sha256(body),
        "body": _jsonable(active.redact(body)),
    }


def audit_row(
    *,
    actor: str,
    action: str,
    subject: str | None = None,
    body: Any = None,
    mode: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_microusd: int | None = None,
) -> dict[str, Any]:
    """Bind parameters for one ``audit_log`` INSERT, **every field redacted**.

    Returns parameters only; it opens no connection and issues no statement, so
    the caller keeps control of the transaction and of which principal writes.
    ``detail`` comes back as canonical JSON text for a ``CAST(:detail AS jsonb)``
    bind.

    ``detail`` used to be the only redacted field, which made this a redactor
    you had to remember to route the *rest* of the row past -- and ``subject``
    routinely carries an entity reference. So every field now goes through the
    same redactor under its own key, and the committed allow-list in
    :mod:`recon.privacy` decides, rather than this function deciding per field:

    ``actor`` / ``action`` / ``subject``
        allow-listed (``SAFE_KEYS``), so they are **scrubbed, not tokenised**.
        Scrubbing removes an embedded address, student number or keyed pair
        while leaving the reference itself intact, and that is required, not a
        convenience: migration 0004's trigger (SQLSTATE ``KS003``) matches
        ``actor`` against ``^system:`` for ``recon_writer``, and a tokenised
        ``subject`` would make ``audit_log`` unqueryable -- R15 accountability
        needs to be able to name the staff member and the record. ``actor`` is
        still not *validated* here; KS003 is the enforcement point and
        duplicating it in Python would invite the two to drift.
    ``tokens_in`` / ``tokens_out`` / ``cost_microusd``
        allow-listed counters and money. Non-identifying by construction.

    A caller that puts a personal value in ``subject`` therefore gets it
    scrubbed if it has a shape or an adjacent key, and gets it through if it is
    a bare name -- which is why ``recon.privacy``'s docstring says source values
    belong in ``body``, where they are tokenised.
    """
    return {
        "actor": redact(actor, key="actor"),
        "action": redact(action, key="action"),
        "subject": redact(subject, key="subject"),
        "detail": None if body is None else canonical_json(audit_detail(body, mode=mode)),
        "tokens_in": redact(tokens_in, key="tokens_in"),
        "tokens_out": redact(tokens_out, key="tokens_out"),
        "cost_microusd": redact(cost_microusd, key="cost_microusd"),
    }


# ---------------------------------------------------------------------------
# the audit_log chokepoint
# ---------------------------------------------------------------------------

#: The one INSERT. Published so a writer binds the columns :func:`audit_row`
#: produces rather than inventing its own list and its own redaction (or none).
AUDIT_INSERT_SQL: Final = (
    "INSERT INTO audit_log (actor, action, subject, detail, tokens_in, tokens_out, cost_microusd) "
    "VALUES (:actor, :action, :subject, CAST(:detail AS jsonb), :tokens_in, :tokens_out, "
    ":cost_microusd)"
)


def insert_audit_row(
    conn: Any,
    *,
    actor: str,
    action: str,
    subject: str | None = None,
    body: Any = None,
    mode: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_microusd: int | None = None,
) -> None:
    """Execute one ``audit_log`` INSERT with **every** field redacted.

    :func:`audit_row` builds the parameters and this issues the statement, so a
    caller cannot route half the row past the redactor by hand-writing the SQL.
    It opens no transaction and commits nothing: the caller still chooses the
    principal and owns the transaction, which is the part of the write boundary
    that must stay with the caller.
    """
    from sqlalchemy import text  # local: keeps this module importable without a DB

    params = audit_row(
        actor=actor,
        action=action,
        subject=subject,
        body=body,
        mode=mode,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_microusd=cost_microusd,
    )
    conn.execute(text(AUDIT_INSERT_SQL), params)


@dataclass(frozen=True)
class AuditWriter:
    """One place in the package that writes an ``audit_log`` row."""

    module: str
    #: True when the row's bound fields come from :func:`audit_row`.
    routed: bool
    #: What the writer does today, and -- when ``routed`` is False -- the exact
    #: change that would route it.
    note: str


#: **Every** ``INSERT INTO audit_log`` in the package, and whether it goes
#: through the chokepoint. Published as data, not as prose, because
#: ``docs/retention-policy.md`` used to claim ``audit_row`` covered "every bound
#: field" of "an audit_log row" while two of the three writers bound ``actor``,
#: ``action`` and ``subject`` raw. ``tests/privacy/test_sinks.py`` compares this
#: tuple against the INSERT sites it finds in the source, so a fourth writer
#: cannot appear unnoticed and a writer cannot change status silently.
AUDIT_WRITERS: Final[tuple[AuditWriter, ...]] = (
    AuditWriter(
        module="recon/logging.py",
        routed=True,
        note="the chokepoint itself: AUDIT_INSERT_SQL and insert_audit_row().",
    ),
    AuditWriter(
        module="recon/privacy.py",
        routed=True,
        note="run_purge()'s own row: parameters come from audit_row().",
    ),
    AuditWriter(
        module="recon/budget.py",
        routed=False,
        note=(
            "_audit() binds actor/action/subject raw and redacts only `detail` via "
            "audit_detail(). Required change: replace the _INSERT_AUDIT execute with "
            "recon.logging.insert_audit_row(conn, actor=AUDIT_ACTOR, action=action, "
            "subject=subject, body=body, tokens_in=..., tokens_out=..., "
            "cost_microusd=...) and delete _INSERT_AUDIT and _detail_json. Owned by "
            "another ticket (recon/budget.py is out of this ticket's scope)."
        ),
    ),
    AuditWriter(
        module="recon/api/internal.py",
        routed=False,
        note=(
            "claim_run() binds actor/action/subject raw and redacts only `detail`. "
            "Required change: replace the _CLAIM_INSERT execute with "
            "recon.logging.insert_audit_row(conn, actor=AUDIT_ACTOR, "
            "action=action, subject=run_id, body={'job': job, 'run_id': run_id}); "
            "keep _CLAIM_LOCK and _CLAIM_LOOKUP as they are. NOTE the lookup "
            "compares `subject` against a raw run_id, so it must compare against "
            "redact(run_id, key='subject') once the insert is routed, or a replay "
            "stops being detected. Owned by another ticket."
        ),
    ),
)


@dataclass(frozen=True)
class TerminalWriter:
    """A place in the package that writes to the terminal without going through
    :func:`console`."""

    module: str
    note: str


#: Terminal writers this package still has that are NOT routed through
#: :func:`console`. Declared rather than tolerated: ``tests/privacy/test_sinks.py``
#: requires the set of ``print`` / ``sys.std*.write`` / ``traceback.print_*``
#: sites it finds in ``recon/`` to be exactly the chokepoint plus this list, so a
#: new one cannot appear quietly and a routed one cannot stay declared. Each note
#: is the change that would remove the entry.
UNROUTED_TERMINAL_WRITERS: Final[tuple[TerminalWriter, ...]] = (
    TerminalWriter(
        module="recon/budget.py",
        note=(
            "`_sweep_cli` prints the reclaimed-reservation report. Required change: "
            "`from recon.logging import console` and replace both print() calls. The "
            "lines carry a scope, an idempotency key and an amount, so nothing "
            "personal today -- but no control makes that stay true. Out of this "
            "ticket's scope."
        ),
    ),
    TerminalWriter(
        module="recon/bench/__main__.py",
        note=(
            "prints the benchmark report. Required change: replace "
            "`print(result.render())` with `console(result.render())`. Out of scope."
        ),
    ),
    TerminalWriter(
        module="recon/seed/run.py",
        note=(
            "prints the generator report and its manifest summary. Required change: "
            "replace both print() calls with console(). Out of scope."
        ),
    ),
    TerminalWriter(
        module="recon/seed/__main__.py",
        note=(
            "prints an argparse/profile failure to stderr. Required change: "
            "`console(str(failure), stream=sys.stderr)`. Out of scope."
        ),
    ),
)


@dataclass(frozen=True)
class Sink:
    """One way something Keystone writes can leave the process."""

    name: str
    chokepoint: str
    covers: str


#: **Every** sink, enumerated. This module's docstring claimed "exactly two"
#: while four existed, and the two it forgot were the ones nobody had looked at:
#: a bare ``print``/``traceback.print_exc`` (three entry points did it) and the
#: standard library's ``logging``, which uvicorn's access log uses to write
#: every request path and query string. An enumeration in code that a test walks
#: is the only version of this claim that cannot quietly go stale.
SINKS: Final[tuple[Sink, ...]] = (
    Sink(
        name="structlog event",
        chokepoint="recon.logging.redaction_processor (installed by configure_logging)",
        covers=(
            "every log.debug/info/warning/error/exception in the package, values bound "
            "onto a logger, and context variables"
        ),
    ),
    Sink(
        name="audit_log row",
        chokepoint="recon.logging.audit_row / recon.logging.insert_audit_row",
        covers=(
            "actor, action, subject, detail and the three counters -- for the writers "
            "listed as routed in recon.logging.AUDIT_WRITERS"
        ),
    ),
    Sink(
        name="direct terminal write",
        chokepoint="recon.logging.console",
        covers="the scorecard and any other human-readable line written to stdout/stderr",
    ),
    Sink(
        name="stdlib logging record",
        chokepoint=(
            "recon.logging._install_stdlib_bridge (ProcessorFormatter ending in the "
            "same redaction_processor) plus recon.logging.uvicorn_log_config"
        ),
        covers=(
            "uvicorn's access and error logs, sqlalchemy, alembic, httpx, anthropic and "
            "captured warnings -- every library that logs through the standard library"
        ),
    ),
)
