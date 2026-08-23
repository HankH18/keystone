"""Adversarial sweep: grep the emitted log for any raw value in the dataset.

Every other test in this package asks "did the redactor do what it says". This
one asks the only question that matters at the end: **is any personal value from
the generated dataset recoverable from what Keystone actually wrote?** A single
leaked address is a failure.

Every emission path the service has is driven, not a representative one:
structured evidence, an action payload, an interpolated error string, a mapping
keyed BY personal values, a tuple under a free-text key, values bound onto the
logger, an **exception object carrying the record**, a **real formatted
traceback**, an adapter rejection's ``log_fields()``, and an ``audit_log`` row.
The exception paths are the ones that mattered: the redactor used to pass a
non-``str`` through untouched and the renderer stringified it afterwards, so
``log.error(..., error=ValueError(f"cannot land {record}"))`` -- which is what
the ingest path does on a rejection -- wrote the whole record.

**Where a hunt goes wrong is where it looks.** This one reported zero leaks
against a 3,053-needle negative control while an independent hunter put a real
surname, household id and date of birth on the terminal of the real service in
default safe mode. The needles were fine; the *positions* were not. Three were
missing entirely -- a personal value under an **allow-listed key**, a personal
value in an **event name**, and a personal value in **unstructured prose** with
no adjacent key -- and those are exactly the three the redactor was weakest at.
The one ``natural_key`` probe that existed passed
``str(next(iter(record.values())))``, which is ``"False"`` or a timestamp for
every fixture record, so it probed nothing. All four are fixed below and marked
``position N``.

Three controls, all load-bearing:

* ``test_the_hunt_can_see_a_leak`` runs the identical sweep against
  ``LOG_MODE=full`` and requires it to *find* the values. Without it a green
  here would be indistinguishable from a green produced by a broken search.
* ``test_every_entry_point_negative_control_fires`` is the same idea made a
  **precondition, per entry point**. It was the second half of the defect: the
  sweep produced zero needle hits from ``recon.seed``, ``recon.suite`` and
  ``recon.bench`` in ``LOG_MODE=full`` *as well as* safe, so their clean sweep
  was evidence of nothing. Every entry point now has to find its needles in full
  mode; if it does not, the hunt **fails as a broken search** rather than
  passing as a clean one.
* ``test_the_running_service_leaks_nothing`` does the sweep against the
  configuration ``create_app()`` installs, with no ``mode=`` argument anywhere,
  so the green is about the service and not about a chain a test built.

And one test that asserts a **leak**: ``test_the_documented_limit_is_real``
pins the residue the honest limits claim -- a bare name in prose, under no key,
appearing nowhere else in the event. A hunt whose every assertion is "clean" can
drift into proving less than it says; this one states the boundary of its own
claim in executable form.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import structlog

from recon.adapters.base import AdapterError
from recon.logging import (
    LOG_MODE_FULL,
    LOG_MODE_SAFE,
    audit_detail,
    audit_row,
    configure_logging,
    configure_logging_once,
    get_logger,
    reset_logging_configuration,
)
from recon.normalize import norm_email, norm_name
from recon.privacy import PII_KEYS, canonical_json
from tests.privacy.conftest import uncache_logger

SERVICE_ROOT = Path(__file__).resolve().parents[2]

#: How many records per source take the expensive paths (real tracebacks, an
#: adapter rejection, an audit row). The cheap structured paths take all of them.
_DEEP_SAMPLE = 40

#: Allow-listed keys the hunt deliberately stuffs with personal data. Every one
#: is in ``recon.privacy.SAFE_KEYS`` (or matches ``SAFE_KEY_PATTERNS``), which
#: means the redactor emits its value verbatim after a substring scrub -- so
#: whatever the scrub cannot see rides straight out. The hunt never put a
#: personal value in this position at all, and that is where the reported leak
#: was.
_ALLOW_LISTED_KEYS: tuple[str, ...] = (
    "natural_key",
    "source_key",
    "record_ref",
    "source_ref",
    "subject",
    "status",
    "title",
    "label",
    "field",
    "key_class",
)


def _pii_strings(record: dict[str, Any]) -> list[str]:
    """The record's own personal values, in the order the record lists them."""
    out: list[str] = []
    for path, value in _walk(record, ""):
        leaf = path.rsplit(".", 1)[-1]
        if leaf in PII_KEYS and isinstance(value, str) and len(value) >= 4:
            out.append(value)
    return out


def _composite_key(record: dict[str, Any]) -> str:
    """A natural key built out of the record's OWN personal values.

    ``str(next(iter(record.values())))`` -- what this probe used to pass -- is
    ``"False"`` or a timestamp for every fixture record in this dataset, so the
    probe carried nothing to find and its green meant nothing. A source that
    keys on ``surname|household|dob`` is not exotic; it is what the reported
    leak actually looked like.
    """
    parts = _pii_strings(record)[:3]
    return "|".join(parts) if parts else "no-pii-in-record"


def _drive_every_path(log: Any, records: dict[str, list[dict[str, Any]]], mode: str) -> list[str]:
    """Emit every record through every path the service has. Returns side text.

    ``log`` is a bound structlog logger whose chain is *already* configured --
    this function never configures one, so it can be pointed at a test chain or
    at the one ``create_app()`` installed.
    """
    side: list[str] = []
    for label, rows in records.items():
        for index, record in enumerate(rows):
            # 1-3: structured evidence, action payload, interpolated error
            log.info(
                "conflict.detected",
                run_id=f"run-{index}",
                source_id=label,
                evidence={"record": record, "sources": [label], "nested": {"deep": [record]}},
                action={"set": {f"{label}.{key}": value for key, value in record.items()}},
                error={"message": f"rejected {json.dumps(record, sort_keys=True)}"},
            )
            # 4: a mapping keyed BY personal values, and a tuple under a text key
            log.warning(
                "er.candidate_scored",
                observed_values={
                    str(value): key for key, value in record.items() if isinstance(value, str)
                },
                note=tuple(value for value in record.values() if isinstance(value, str)),
            )
            # 5: an exception object carrying the record -- the ingest rejection
            log.error(
                "ingest.record_rejected",
                run_id=f"run-{index}",
                error=ValueError(f"cannot land {record!r}"),
                detail=f"rejected {record}",
            )
            # 6: values bound onto the logger rather than passed to the call
            log.bind(**{k: v for k, v in record.items() if not isinstance(v, dict | list)}).info(
                "proposal.built"
            )
            if index >= _DEEP_SAMPLE:
                continue
            # 7: a real formatted traceback through structlog's format_exc_info.
            # The raw `traceback.format_exc()` is deliberately NOT collected --
            # it is the test's own string, and it obviously contains the record;
            # what is under test is what structlog wrote after the chain ran.
            try:
                raise ValueError(f"cannot land {record!r}")
            except ValueError:
                log.error("ingest.source_failed", run_id=f"run-{index}", exc_info=True)
            # 8: an adapter rejection rendered exactly as recon.ingest renders it.
            # `natural_key` is a COMPOSITE OF THE RECORD'S OWN PII -- see
            # `_composite_key`. The probe used to pass the record's first value,
            # which is a bool or a timestamp, so it tested nothing.
            composite = _composite_key(record)
            failure = AdapterError(
                "duplicate_primary_key",
                f"{composite!r} appears 2 times in this generation (lines 1, 2); "
                f"a repeated primary key is a structural rejection",
                source_id=label,
                entity_type=label.split(".")[-1],
                natural_key=composite,
            )
            log.warning("ingest.record_rejected", run_id=f"run-{index}", **failure.log_fields())
            # position 1: personal data under an ALLOW-LISTED key. The allow-list
            # emits verbatim after a substring scrub, so anything the scrub
            # cannot see rides out. This is the position the reported leak used.
            log.warning(
                "ingest.record_rejected",
                run_id=f"run-{index}",
                evidence={"record": record},
                **{key: composite for key in _ALLOW_LISTED_KEYS},
            )
            # position 2: personal data in the EVENT NAME itself. An event name
            # is a string like any other, and nothing had ever put a value in
            # one.
            log.error(
                f"ingest.rejected {composite} :: {' '.join(_pii_strings(record)[:4])}",
                run_id=f"run-{index}",
                record=record,
            )
            # position 3: UNSTRUCTURED PROSE -- a sentence, no adjacent key, no
            # JSON, no repr. This is the half of the reported leak that survived
            # even after `natural_key` was tokenised, because the same string was
            # interpolated bare into `detail`.
            log.error(
                "ingest.record_rejected",
                run_id=f"run-{index}",
                natural_key=composite,
                detail=(
                    f"{composite} could not be matched; the record it came from is "
                    f"{' and '.join(_pii_strings(record)[:3])}, rejected on line {index}"
                ),
                evidence={"record": record},
            )
            # 9: an audit_log row -- every bound field, not just detail
            side.append(
                canonical_json(
                    audit_row(
                        actor="system:reconciler",
                        action="conflict.detected",
                        subject=f"{label}:{next(iter(record.values()))}",
                        body=record,
                        mode=mode,
                    )
                )
            )
            side.append(canonical_json(audit_detail(record, mode=mode)))
    return side


def _emit_everything(records: dict[str, list[dict[str, Any]]], mode: str) -> str:
    """Configure a chain in ``mode``, drive every path, return everything written."""
    stream = io.StringIO()
    configure_logging(mode=mode, stream=stream, cache=False)
    stream.truncate(0)
    stream.seek(0)
    side = _drive_every_path(get_logger("tests.privacy.leak_hunt"), records, mode)
    return stream.getvalue() + "\n".join(side)


def _needles(records: dict[str, list[dict[str, Any]]]) -> set[str]:
    """Every raw personal string in the dataset, plus its normalised twin.

    The normalised form is included because a redactor that stored
    ``norm_email(value)`` "for correlation" would still have written the address
    down; the local part is included because a preview built from a prefix is
    the classic way this leaks.
    """
    found: set[str] = set()
    for rows in records.values():
        for record in rows:
            for path, value in _walk(record, ""):
                leaf = path.rsplit(".", 1)[-1]
                if leaf not in PII_KEYS or not isinstance(value, str):
                    continue
                for candidate in (value, norm_email(value), norm_name(value)):
                    if candidate and len(candidate) >= 4:
                        found.add(candidate)
                if "@" in value:
                    local = value.split("@", 1)[0].strip().strip("\"'`")
                    if len(local) >= 4:
                        found.add(local)
    return found


def _walk(obj: Any, prefix: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    if isinstance(obj, dict):
        for name, value in obj.items():
            out.extend(_walk(value, f"{prefix}.{name}" if prefix else str(name)))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            out.extend(_walk(value, f"{prefix}[{index}]"))
    else:
        out.append((prefix, obj))
    return out


@pytest.fixture(scope="module")
def needles(dev_records: dict[str, list[dict[str, Any]]]) -> set[str]:
    values = _needles(dev_records)
    assert len(values) > 500, f"only {len(values)} needles; the hunt is too weak"
    return values


def test_safe_mode_log_leaks_nothing(
    dev_records: dict[str, list[dict[str, Any]]], needles: set[str]
) -> None:
    """No raw value, normalised value or email local part survives in the log."""
    blob = _emit_everything(dev_records, LOG_MODE_SAFE)
    leaked = sorted(needle for needle in needles if needle in blob)
    assert not leaked, f"{len(leaked)} value(s) leaked into the log, e.g. {leaked[:5]}"


def test_the_hunt_can_see_a_leak(
    dev_records: dict[str, list[dict[str, Any]]], needles: set[str]
) -> None:
    """Negative control: the same sweep over `full` mode MUST find the values.

    If this ever passes with an empty result, the search above is broken and its
    green means nothing.
    """
    blob = _emit_everything(dev_records, LOG_MODE_FULL)
    leaked = [needle for needle in needles if needle in blob]
    assert len(leaked) > 500, (
        "the leak hunt found almost nothing even in LOG_MODE=full, so it is not "
        "capable of detecting a leak in LOG_MODE=safe either"
    )


def test_the_safe_log_is_not_simply_empty(dev_records: dict[str, list[dict[str, Any]]]) -> None:
    """A green above must come from redaction, not from an unwritten log.

    It must also come from a sweep that really drove every path: each emission
    the hunt claims to exercise has to be visible in the output, or a path that
    quietly stopped emitting would turn the leak hunt green by doing less. That
    is not hypothetical -- the three positions the reported leak used were
    absent from this sweep entirely, and their absence is exactly what a green
    looked like.
    """
    blob = _emit_everything(dev_records, LOG_MODE_SAFE)
    assert blob.count("[pii:") > 5000, "the safe log carries almost no tokens"
    assert "run-7" in blob
    assert '"level":"info"' in blob
    for marker in (
        "conflict.detected",  # structured evidence + action payload
        "er.candidate_scored",  # mapping keyed by value, tuple under a text key
        "ingest.record_rejected",  # exception object + adapter rejection
        "ingest.source_failed",  # exc_info
        "proposal.built",  # bound context
        "Traceback (most recent call last)",  # a real formatted traceback
        '"body_sha256"',  # an audit_log row
        "duplicate_primary_key",  # AdapterError.log_fields(), with a real natural key
        '"key_class":"[pii:',  # position 1: PII under an allow-listed key
        '"event":"ingest.rejected [pii:',  # position 2: PII in the event name
        "could not be matched",  # position 3: PII in unstructured prose
    ):
        assert marker in blob, f"the hunt never emitted {marker!r}"


# ---------------------------------------------------------------------------
# the same sweep, against the configuration the running service installs
# ---------------------------------------------------------------------------


def test_the_running_service_leaks_nothing(
    dev_records: dict[str, list[dict[str, Any]]],
    needles: set[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No `mode=`, no `stream=`, no `configure_logging` -- just `create_app()`.

    The sweep above proves the redactor works when a test installs it. This one
    proves the service installs it: structlog is reset to the state a fresh
    interpreter is in, `create_app()` is what configures the process, and the
    records are driven through the module-level logger `recon.ingest` holds.
    """
    from recon.app import create_app

    structlog.reset_defaults()
    reset_logging_configuration()
    try:
        create_app()
        import recon.ingest

        uncache_logger(recon.ingest.log)
        side = _drive_every_path(recon.ingest.log, dev_records, LOG_MODE_SAFE)
        blob = capsys.readouterr().err + "\n".join(side)
    finally:
        structlog.reset_defaults()
        reset_logging_configuration()
        configure_logging_once()

    assert blob.count("[pii:") > 5000, "the service log carries almost no tokens"
    leaked = sorted(needle for needle in needles if needle in blob)
    assert not leaked, (
        f"{len(leaked)} value(s) leaked out of the RUNNING service, e.g. {leaked[:5]}"
    )


# ---------------------------------------------------------------------------
# the negative control, per entry point, as a PRECONDITION
# ---------------------------------------------------------------------------
#
# The second half of the reported defect. The sweep above configures a chain and
# drives it, which proves the chain redacts -- but a Keystone *process* is
# started five different ways, each installs its own sinks, and three of them
# (`recon.seed`, `recon.suite`, `recon.bench`) produced ZERO needle hits in
# `LOG_MODE=full` as well as in safe. A search that finds nothing when nothing
# is hidden is not a search, so their clean sweep proved nothing at all.
#
# So each entry point is now started for real, in its own interpreter, and then
# personal data is pushed through **every sink that process has**: structlog, the
# standard library's logging (uvicorn's sink), and the console chokepoint. The
# full-mode run must FIND the needles -- that is asserted first, and a failure
# there is reported as a broken hunt, not as a clean one -- and only then is the
# safe-mode run required to be empty.

#: `(label, the statements that start this entry point for real)`. Each takes
#: the cheapest path that still reaches `configure_logging_once()`.
_ENTRY_POINT_STARTERS: tuple[tuple[str, str], ...] = (
    ("recon.app", "from recon.app import create_app\ncreate_app()\n"),
    (
        "recon CLI",
        "import recon.__main__ as m\ntry:\n    m.cli(['version'])\nexcept SystemExit:\n    pass\n",
    ),
    ("recon.suite", "import recon.suite.__main__ as m\nm.main(['--list'])\n"),
    (
        "recon.bench",
        "import recon.bench.__main__ as m\ntry:\n    m.main([])\nexcept SystemExit:\n    pass\n",
    ),
    (
        "recon.seed",
        "import recon.seed.__main__ as m\n"
        "try:\n    m.main(['--profile', 'not-a-profile'])\nexcept SystemExit:\n    pass\n",
    ),
)

#: Pushed through the started process. One emission per sink, so a sink that is
#: not covered shows up as a needle in safe mode rather than as a silence.
_SWEEP_BODY = """
import json, logging, sys
import structlog
from recon.logging import console

records = json.load(open(sys.argv[1]))
log = structlog.get_logger("tests.privacy.entry_point_sweep")
access = logging.getLogger("uvicorn.access")
for label, rows in records.items():
    for record in rows:
        blob = json.dumps(record, sort_keys=True)
        # sink (a): a structlog event, structured and interpolated
        log.error(
            "ingest.record_rejected",
            source_id=label,
            evidence={"record": record},
            natural_key="|".join(
                str(v) for v in record.values() if isinstance(v, str) and len(v) >= 4
            )[:120],
            detail="rejected " + repr(record),
        )
        # sink (d): a stdlib record -- this is uvicorn's access line
        access.info('127.0.0.1 - "POST /internal/ingest?payload=%s HTTP/1.1" 422', blob)
        # sink (c): a direct terminal write, the scorecard's sink
        console("check detail: " + blob)
"""


def _run_entry_point_sweep(tmp_path: Any, label: str, starter: str, mode: str | None) -> str:
    """Start ``label`` for real in a fresh interpreter, push PII through it.

    Returns everything the process wrote to stdout and stderr -- the terminal,
    which is what a leak hunt is entitled to look at.
    """
    import subprocess
    import tempfile

    records_path = tmp_path / f"records-{label.replace('.', '_').replace(' ', '_')}.json"
    driver = tmp_path / f"driver-{label.replace('.', '_').replace(' ', '_')}-{mode}.py"
    driver.write_text(starter + _SWEEP_BODY)
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONHASHSEED": "0",
        "HOME": tempfile.gettempdir(),
        "PYTHONPATH": str(SERVICE_ROOT),
    }
    if mode is not None:
        env["LOG_MODE"] = mode
    result = subprocess.run(
        [sys.executable, str(driver), str(records_path)],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, f"{label} sweep failed:\n{result.stdout}\n{result.stderr}"
    return result.stdout + result.stderr


@pytest.fixture(scope="module")
def sweep_records(dev_records: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """A small slice of the real dataset, small enough to drive five subprocesses."""
    return {label: rows[:15] for label, rows in dev_records.items()}


@pytest.fixture(scope="module")
def sweep_needles(sweep_records: dict[str, list[dict[str, Any]]]) -> set[str]:
    values = _needles(sweep_records)
    assert len(values) > 100, f"only {len(values)} needles in the entry-point slice"
    return values


@pytest.fixture(scope="module")
def sweep_tree(
    tmp_path_factory: pytest.TempPathFactory, sweep_records: dict[str, list[dict[str, Any]]]
) -> Any:
    root = tmp_path_factory.mktemp("entry-point-sweep")
    for label, _ in _ENTRY_POINT_STARTERS:
        stem = label.replace(".", "_").replace(" ", "_")
        (root / f"records-{stem}.json").write_text(json.dumps(sweep_records))
    return root


@pytest.mark.parametrize(
    ("label", "starter"),
    _ENTRY_POINT_STARTERS,
    ids=[label for label, _ in _ENTRY_POINT_STARTERS],
)
def test_every_entry_point_negative_control_fires(
    label: str, starter: str, sweep_tree: Any, sweep_needles: set[str]
) -> None:
    """PRECONDITION: in `LOG_MODE=full`, this entry point must LEAK the needles.

    Three of the five used to produce zero hits in full mode, which means their
    clean safe-mode sweep was the output of a search that could not have found
    anything. A hunt that cannot fail is the same class of defect as a
    self-check that cannot fail, so this is asserted before the clean result is
    believed -- and it fails as *"the hunt is broken"*, not as *"the service
    leaks"*.
    """
    blob = _run_entry_point_sweep(sweep_tree, label, starter, LOG_MODE_FULL)
    found = sorted(needle for needle in sweep_needles if needle in blob)
    assert len(found) > 50, (
        f"the {label} sweep found only {len(found)} of {len(sweep_needles)} needles in "
        f"LOG_MODE=full, so it is not capable of detecting a leak in LOG_MODE=safe "
        f"either. This is a BROKEN HUNT, not a clean service."
    )


@pytest.mark.parametrize(
    ("label", "starter"),
    _ENTRY_POINT_STARTERS,
    ids=[label for label, _ in _ENTRY_POINT_STARTERS],
)
def test_every_entry_point_leaks_nothing_in_the_default_mode(
    label: str, starter: str, sweep_tree: Any, sweep_needles: set[str]
) -> None:
    """The same process, the same sinks, no LOG_MODE in the environment at all.

    Every sink the started process has is driven: a structlog event, a stdlib
    ``logging`` record (uvicorn's access line, which used to print the whole
    request verbatim), and a direct terminal write through
    ``recon.logging.console``.
    """
    blob = _run_entry_point_sweep(sweep_tree, label, starter, None)
    assert "[pii:" in blob, f"the {label} process wrote nothing redacted; did it emit at all?"
    leaked = sorted(needle for needle in sweep_needles if needle in blob)
    assert not leaked, (
        f"{len(leaked)} value(s) leaked out of a real {label} process in the default "
        f"(safe) mode, e.g. {leaked[:5]}"
    )


# ---------------------------------------------------------------------------
# the boundary of the claim, asserted rather than described
# ---------------------------------------------------------------------------


def test_the_documented_limit_is_real() -> None:
    """A bare name in prose, under no key, in no other field, is NOT removed.

    This test asserts a **leak**, on purpose. `recon.privacy`'s honest limits and
    `docs/retention-policy.md` §4.1 both say a personal name has no shape and
    that unstructured prose carrying nothing else is beyond the redactor. If that
    stopped being true the claim would be understating the control, and if the
    claim were quietly widened this would be the test that noticed. Everything
    the limit does NOT cover is asserted alongside it: the same sentence with the
    record present, with the key present, or with a shaped value in it, is clean.
    """
    from recon.privacy import redact

    bare = redact({"detail": "Zedail could not be matched to anyone"})
    assert "Zedail" in bare["detail"], (
        "the honest limit says a bare name in prose survives; if it no longer "
        "does, the limit is understating the control and must be rewritten"
    )

    # ...and each of the three things that DO close it
    with_key = redact({"detail": "last_name=Zedail could not be matched"})
    assert "Zedail" not in with_key["detail"]

    with_sibling = redact(
        {"detail": "Zedail could not be matched", "last_name": "Zedail"},
    )
    assert "Zedail" not in with_sibling["detail"]

    with_shape = redact({"detail": "HH-000997 born 2014-09-07 could not be matched"})
    assert "HH-000997" not in with_shape["detail"] and "2014-09-07" not in with_shape["detail"]
