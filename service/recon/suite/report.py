"""Rendering the scorecard, and the artifact ``GET /api/scorecard`` serves.

Two outputs, one source
------------------------
:func:`render` produces the human table written to ``docs/scorecard.txt`` and
printed to the terminal. :func:`payload` produces the JSON written next to it as
``docs/scorecard.json`` and served verbatim by :mod:`recon.api.scorecard`. Both
are built from the same list of :class:`~recon.suite.checks.CheckResult` rows and
the same :class:`~recon.suite.pipeline.PipelineRun`, so the file a grader reads
and the body the dashboard reconciles against cannot disagree.

The JSON shape is pinned by the dashboard
-------------------------------------------
``dashboard/src/lib/contract.ts`` assumption **A4**::

    {generated_at, run_id,
     conflicts: {total, by_type},
     proposals: {total, by_status},
     checks: Record<string, boolean>}

and its `consequence` line -- "a different shape leaves the overview reporting
Mismatch for every type, or its error state" -- is why the keys here are not
negotiable. Extra keys (``benchmarks``, ``passed``, ``database``) are additive;
the TypeScript interface ignores what it does not declare.

**The counts are a snapshot on purpose.** The Overview route fetches each
conflict-type figure twice -- once from this scorecard and once as the ``total``
of the matching ``/api/conflicts`` query -- and flags a row that disagrees. That
comparison is only worth making because this side is the harness's *record of a
run* and the other side is the service's *live view*: a mismatch means the
database moved since the suite last ran. Serving live counts here would make both
sides the same query and the reconciliation vacuous.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recon import __version__
from recon.suite.checks import CheckResult
from recon.suite.pipeline import PipelineRun

__all__ = [
    "SCORECARD_DIR_ENV",
    "SCORECARD_JSON",
    "SCORECARD_TXT",
    "payload",
    "render",
    "scorecard_dir",
    "write_scorecard",
]

#: Where the two artifacts live. Overridable so a test never writes into `docs/`.
SCORECARD_DIR_ENV = "KEYSTONE_SCORECARD_DIR"

SCORECARD_TXT = "scorecard.txt"
SCORECARD_JSON = "scorecard.json"

_REPO_ROOT = Path(__file__).resolve().parents[3]

_NAME_WIDTH = 30
_STATUS_WIDTH = 6


def scorecard_dir() -> Path:
    """The committed ``docs/`` directory, or ``KEYSTONE_SCORECARD_DIR``."""
    override = os.environ.get(SCORECARD_DIR_ENV)
    if override:
        return Path(override)
    return _REPO_ROOT / "docs"


def _wrap(text: str, width: int, indent: int) -> list[str]:
    """Hard-wrap a detail string, keeping whole words where it can."""
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            lines.append(current)
        while len(word) > width:
            lines.append(word[:width])
            word = word[width:]
        current = word
    if current:
        lines.append(current)
    return [lines[0]] + [" " * indent + line for line in lines[1:]] if lines else [""]


def render(
    results: Sequence[CheckResult],
    *,
    run: PipelineRun | None = None,
    benchmarks: Sequence[str] = (),
    width: int = 96,
    notes: Sequence[str] = (),
) -> str:
    """The human scorecard: one row per check, then the totals."""
    generated = datetime.now(tz=UTC).isoformat(timespec="seconds")
    title = f"Keystone reconciliation suite -- scorecard (v{__version__})"
    rule = "=" * width
    lines = [rule, title, f"generated {generated}"]
    if run is not None:
        lines.append(
            f"database {run.dsn_database}   run {run.run_id}   "
            f"dataset {sum(run.precondition.landing.values())} landed records / "
            f"{run.precondition.entities} entities"
        )
    lines.append(rule)

    indent = _NAME_WIDTH + _STATUS_WIDTH + 2
    detail_width = max(24, width - indent)

    bench_names = set(benchmarks)
    sections: list[tuple[str, list[CheckResult]]] = [
        ("CHECKS", [row for row in results if row.name not in bench_names]),
        ("BENCHMARKS", [row for row in results if row.name in bench_names]),
    ]
    for heading, rows in sections:
        if not rows:
            continue
        lines.append(f"{heading:<{_NAME_WIDTH}} {'STATUS':<{_STATUS_WIDTH}} DETAIL")
        lines.append("-" * width)
        for row in rows:
            body = _wrap(row.detail, detail_width, indent)
            lines.append(f"{row.name:<{_NAME_WIDTH}} {row.status:<{_STATUS_WIDTH}} {body[0]}")
            lines.extend(body[1:])
        lines.append("-" * width)

    failed = [row for row in results if not row.ok]
    lines.append(f"{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        lines.append("FAILED: " + ", ".join(row.name for row in failed))
    for note in list(run.notes if run is not None else []) + list(notes):
        wrapped = _wrap(note, width - 6, 6)
        lines.append(f"note: {wrapped[0]}")
        lines.extend(wrapped[1:])
    lines.append(rule)
    return "\n".join(lines) + "\n"


def payload(
    results: Sequence[CheckResult],
    *,
    run: PipelineRun | None = None,
    benchmarks: Sequence[str] = (),
) -> dict[str, Any]:
    """The A4 body: what ``GET /api/scorecard`` serves and what the file holds."""
    bench_names = set(benchmarks)
    by_type: Mapping[str, int] = {}
    by_status: dict[str, int] = {}
    conflicts_total = 0
    proposals_total = 0
    run_id = "no-run"
    database = None

    if run is not None:
        by_type = dict(run.report_first.by_type)
        conflicts_total = run.report_first.conflicts_seen
        for row in run.proposals:
            by_status[row.status] = by_status.get(row.status, 0) + 1
        proposals_total = len(run.proposals)
        run_id = run.run_id
        database = run.dsn_database

    return {
        "generated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "run_id": run_id,
        "conflicts": {"total": conflicts_total, "by_type": dict(sorted(by_type.items()))},
        "proposals": {"total": proposals_total, "by_status": dict(sorted(by_status.items()))},
        "checks": {row.name: row.ok for row in results},
        "benchmarks": {
            row.name: {"passed": row.ok, "detail": row.detail}
            for row in results
            if row.name in bench_names
        },
        "details": {row.name: row.detail for row in results},
        "passed": all(row.ok for row in results) and bool(results),
        "suite_version": __version__,
        "database": database,
    }


def write_scorecard(
    results: Sequence[CheckResult],
    *,
    run: PipelineRun | None = None,
    benchmarks: Sequence[str] = (),
    notes: Sequence[str] = (),
    directory: Path | None = None,
) -> tuple[Path, Path]:
    """Write both artifacts and return their paths.

    Written even when rows are red. A scorecard that only appears on success
    would leave the one run a reviewer most needs to read with no record at all,
    and would let a stale green file outlive the failure that replaced it.
    """
    target = directory or scorecard_dir()
    target.mkdir(parents=True, exist_ok=True)

    text_path = target / SCORECARD_TXT
    json_path = target / SCORECARD_JSON
    text_path.write_text(
        render(results, run=run, benchmarks=benchmarks, notes=notes), encoding="utf-8"
    )
    json_path.write_text(
        json.dumps(payload(results, run=run, benchmarks=benchmarks), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return text_path, json_path
