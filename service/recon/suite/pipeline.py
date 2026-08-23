"""The ONE graded pipeline run every check reads, and the precondition it asserts.

Why a shared run instead of a run per check
--------------------------------------------
The scorecard's rows are ten different questions about **the same pass**: the
golden diff, the clean sample, the join hashes, the proposal ledger, the mirror
digests and the two benchmark clocks all describe one detection + one
reconciliation. Running the detector once per row would cost four minutes and --
much worse -- would let two rows disagree about what happened without either
turning red. So the pipeline runs once, in a pinned order, and every check reads
the artifacts out of :class:`PipelineRun`.

The order, and why it is that order
------------------------------------
1. **precondition** -- :func:`assert_loaded`. The suite does NOT ingest and does
   NOT materialize; it grades a database that already holds the mirror and the
   identity layer (what ``POST /internal/sync`` builds, ``make sync`` locally).
   That is not a convenience: ``recon.resolve.materialize`` takes ~6 minutes on
   the full profile because its deferred provenance triggers validate 1.28M
   ``field_lineage`` rows at COMMIT, and a harness that re-did it every run would
   not be run. The precondition is therefore **asserted with numbers** -- per
   (source, entity_type, generation) landing counts against
   ``fixtures/manifest.json`` -- rather than assumed. An empty or half-loaded
   database fails the suite; it never produces a small green.
2. **reset the graded layer** -- ``conflicts``, ``invariant_results``,
   ``proposals``, ``proposal_events``, ``conflict_incidents``. These are exactly
   the tables this pass regenerates, so truncating them is what makes the suite
   repeatable instead of order-dependent: a second run of the harness must see
   the same "no proposals yet" starting state as the first, or ``proposal-safety``
   would silently become ``oscillation-dedup``. Nothing in the mirror
   (``raw_records``, ``ingest_runs``, ``stg_*``) and nothing in the identity
   layer (``entities``, ``entity_links``, ``field_lineage``) is touched, and
   ``mirror-unchanged`` proves the second half of that claim with a hash.
3. **invariant run A**, timed -- the golden diff, the clean sample, and the
   "<30s invariant pass" benchmark all read this one.
4. **persist** run A into ``conflicts`` / ``invariant_results``.
5. **invariant run B** on a *fresh connection* -- the determinism check's second
   detection. A fresh connection matters: :func:`recon.invariants.runner
   .run_invariants` leaves its ``er_*`` / ``ref_*`` TEMP tables behind, so re-using
   connection A would compare a run against its own cached scaffolding.
6. **two dry reconciles**, each inside a ``recon_writer`` transaction that is
   **rolled back**. Both start from zero proposals, so both are real, complete,
   3,050-proposal runs -- and neither leaves a row. This is what makes the
   confidence vector comparable three ways (dry A, dry B, and the committed run)
   instead of once.
7. **mirror digest -> ``run_once()`` -> mirror digest**. The committed run. The
   bracket is around the run that actually writes 3,050 proposals, which is the
   run R13's "the mirror is unchanged" claim is about.
8. **``run_once()`` again** -- R16's dedup: with every fingerprint already open,
   the second pass must propose zero.

What is deliberately NOT here
------------------------------
Ingestion. It needs an empty landing table, and emptying this database's landing
table would destroy the identity layer's provenance for every later check. The
ingestion benchmark runs the real ingest path inside its own **rolled-back**
transaction (``recon.bench.suite``), which is why it can measure the true rate
without the suite having to rebuild 100k records afterwards.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy import Connection, text

from recon.adapters.jsonl import default_fixtures_root
from recon.db import ROLE_RECON_WRITER, database_url, get_engine, role_connection
from recon.invariants.runner import InvariantRun, persist_run, run_invariants
from recon.logging import get_logger
from recon.reconciler import ReconcileReport, reconcile, run_once
from recon.suite.mirror import MIRROR_TABLES, MirrorDigest, mirror_digest

__all__ = [
    "GRADED_TABLES",
    "PipelineRun",
    "PreconditionFailed",
    "ProposalRow",
    "assert_loaded",
    "build_pipeline",
    "cached_pipeline",
    "pipeline",
    "reset_pipeline_cache",
]

log = get_logger("recon.suite.pipeline")

#: The tables one graded pass regenerates from scratch. Truncated before the
#: pass so the harness is repeatable; every other table is read-only to it.
GRADED_TABLES = (
    "proposal_events",
    "proposals",
    "conflict_incidents",
    "conflicts",
    "invariant_results",
)

#: ``raw_records`` columns that identify one manifest slice.
_LANDING_COUNTS = text(
    "SELECT source_id, entity_type, generation, count(*) AS n FROM raw_records GROUP BY 1, 2, 3"
)

_IDENTITY_COUNTS = text(
    "SELECT (SELECT count(*) FROM entities) AS entities, "
    "(SELECT count(*) FROM entity_links) AS links, "
    "(SELECT count(*) FROM field_lineage) AS lineage, "
    "(SELECT count(DISTINCT generation) FROM field_lineage) AS lineage_generations"
)

_CONFLICT_STATUS = text("SELECT status::text AS status, count(*) AS n FROM conflicts GROUP BY 1")

_PROPOSAL_ROWS = text(
    "SELECT p.id, p.fingerprint, p.status::text AS status, p.sensitive, p.action, "
    "       p.confidence::text AS confidence, c.type AS conflict_type, "
    "       c.disagreeing_fields, c.status::text AS conflict_status, c.oscillating "
    "FROM proposals p JOIN conflicts c ON c.id = p.conflict_id "
    "ORDER BY p.id"
)

#: The generation the whole contract calls "current state" (SS7 / D-9).
CURRENT_GENERATION = 3

#: ``field_lineage`` must cover these before R16's A->B->A scan is an answer.
REQUIRED_LINEAGE_GENERATIONS = 3


class PreconditionFailed(RuntimeError):
    """The database is not in the state the suite grades.

    Raised, never swallowed: ``recon.suite.__main__.run_check`` turns it into a
    FAIL row on every check that needed the pipeline. A suite that reported
    "0 conflicts, 0 false negatives" against an empty database would be the
    exact vacuous green this package exists to prevent.
    """


@dataclass(frozen=True, slots=True)
class ProposalRow:
    """One ``proposals`` row as the safety checks read it, joined to its conflict."""

    proposal_id: int
    fingerprint: str
    status: str
    sensitive: bool
    action: Mapping[str, Any]
    confidence: str
    conflict_type: str
    disagreeing_fields: tuple[str, ...]
    conflict_status: str
    oscillating: bool

    @property
    def target_paths(self) -> tuple[str, ...]:
        """The field paths this proposal authorises a write to (may be empty)."""
        assignments = self.action.get("set") if isinstance(self.action, Mapping) else None
        if not isinstance(assignments, Mapping):
            return ()
        return tuple(sorted(str(key) for key in assignments))


@dataclass(frozen=True, slots=True)
class Precondition:
    """What the suite found in the database before it graded anything."""

    landing: Mapping[str, int]
    entities: int
    links: int
    lineage: int
    lineage_generations: int

    def summary(self) -> str:
        return (
            f"landing {sum(self.landing.values())} records in {len(self.landing)} slices; "
            f"entities {self.entities}; links {self.links}; "
            f"lineage {self.lineage} rows over {self.lineage_generations} generations"
        )


@dataclass
class PipelineRun:
    """One graded pass. Built once per process; every check reads it."""

    started_at: datetime
    precondition: Precondition
    run_a: InvariantRun
    run_b: InvariantRun
    invariants_seconds: float
    invariants_b_seconds: float
    persist_seconds: float
    mirror_before: MirrorDigest
    mirror_after: MirrorDigest
    report_first: ReconcileReport
    report_second: ReconcileReport
    dry_a: ReconcileReport
    dry_b: ReconcileReport
    reconcile_seconds: float
    proposals: tuple[ProposalRow, ...]
    conflict_status: Mapping[str, int]
    fixtures_root: Path
    dsn_database: str
    notes: list[str] = field(default_factory=list)

    @property
    def full_pass_seconds(self) -> float:
        """SPEC's "full invariant/reconciliation pass": detect + persist + propose."""
        return self.invariants_seconds + self.persist_seconds + self.reconcile_seconds

    @property
    def run_id(self) -> str:
        return self.report_first.run_id


def _dsn() -> str:
    """``DATABASE_URL`` in the plain libpq spelling ``psycopg.connect`` wants."""
    return database_url().render_as_string(hide_password=False).replace("+psycopg", "")


def _database_name() -> str:
    return database_url().database or "?"


def _expected_landing(root: Path) -> dict[str, int]:
    """``fixtures/manifest.json``'s per-generation expected counts, flattened.

    Keyed ``"<source>.<entity_type>@gen<N>"`` -- the same key the observed side
    builds, so a missing slice shows up as a missing key rather than as a total
    that happens to add up.
    """
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise PreconditionFailed(
            f"no fixtures manifest at {manifest_path}. The suite grades a database "
            "loaded from the committed fixtures tree; without the manifest it cannot "
            "say whether the load was complete, and a suite that cannot say that must "
            "not report a pass. Run `python -m recon.seed --profile full` first."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected: dict[str, int] = {}
    for gen_label, counts in sorted(manifest.get("expected_counts", {}).items()):
        for qualified, number in sorted(counts.items()):
            expected[f"{qualified}@{gen_label}"] = int(number)
    if not expected:
        raise PreconditionFailed(f"{manifest_path} carries no expected_counts block")
    return expected


def assert_loaded(conn: Connection, root: Path | None = None) -> Precondition:
    """Assert the mirror and the identity layer hold the committed dataset.

    The comparison is per slice, against ``fixtures/manifest.json``. Three
    distinct half-loaded states used to look identical to a suite that only asked
    "is ``raw_records`` non-empty?": a run that ingested generation 3 only (the
    identity layer then holds no A->B->A history and every oscillation verdict is
    a confident false), a run whose CRM source timed out mid-load, and a run
    against a database someone truncated between checks.
    """
    root = root or default_fixtures_root()
    expected = _expected_landing(root)

    observed: dict[str, int] = {}
    for row in conn.execute(_LANDING_COUNTS):
        observed[f"{row.source_id}.{row.entity_type}@gen{row.generation}"] = int(row.n)

    missing = sorted(set(expected) - set(observed))
    extra = sorted(set(observed) - set(expected))
    wrong = sorted(
        f"{key} expected {expected[key]} landed {observed[key]}"
        for key in set(expected) & set(observed)
        if expected[key] != observed[key]
    )
    if missing or extra or wrong:
        raise PreconditionFailed(
            "the landing table does not match fixtures/manifest.json: "
            f"missing slices {missing}; unexpected slices {extra}; wrong counts {wrong}. "
            "Load the database first (`POST /internal/sync`, or `make sync`), then "
            "re-run the suite."
        )

    identity = conn.execute(_IDENTITY_COUNTS).one()
    if not (identity.entities and identity.links and identity.lineage):
        raise PreconditionFailed(
            f"the identity layer is empty (entities={identity.entities}, "
            f"links={identity.links}, field_lineage={identity.lineage}). The mirror is "
            "loaded but nothing was materialized, so every cross-source view, every "
            "join check and every oscillation verdict would be computed from nothing. "
            "Run the materialize step (`POST /internal/sync` does both)."
        )
    if identity.lineage_generations < REQUIRED_LINEAGE_GENERATIONS:
        raise PreconditionFailed(
            f"field_lineage covers {identity.lineage_generations} generation(s), and "
            f"R16's A->B->A scan needs {REQUIRED_LINEAGE_GENERATIONS}. A lineage table "
            "materialized for generation 3 alone is NON-EMPTY and structurally "
            "incapable of holding the pattern -- the exact shape that once made 3,050 "
            "proposals record a confident 'not oscillating' that nothing could falsify."
        )

    return Precondition(
        landing=observed,
        entities=int(identity.entities),
        links=int(identity.links),
        lineage=int(identity.lineage),
        lineage_generations=int(identity.lineage_generations),
    )


def reset_graded_layer() -> None:
    """Empty the tables this pass regenerates. Owner principal, by design.

    ``recon_writer`` holds no DELETE on ``conflicts`` or ``proposals`` and must
    not: R13's write boundary is that the reconciler can add work for a human and
    can never retract it. Clearing a harness workspace is an operator action and
    uses an operator's credentials -- the same split ``recon.bench.ingest
    .truncate_landing`` already makes.
    """
    tables = ", ".join(GRADED_TABLES)
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    log.info("suite.graded_layer_reset", label=tables)


@contextmanager
def _psycopg(dsn: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(dsn) as conn:
        yield conn


def _dry_reconcile(label: str) -> ReconcileReport:
    """A complete reconcile inside a transaction that is rolled back.

    Real in every way that is graded -- real ``recon_writer`` principal, real
    inserts, real triggers, real confidence model -- and it leaves nothing
    behind, which is what lets the determinism check compare two *independent*
    scoring passes instead of comparing one pass to itself.
    """
    with role_connection(ROLE_RECON_WRITER, commit=False) as conn:
        report = reconcile(conn=conn)
    log.info("suite.dry_reconcile", label=label, proposed_count=report.proposed)
    return report


def _read_proposals(conn: Connection) -> tuple[ProposalRow, ...]:
    rows: list[ProposalRow] = []
    for row in conn.execute(_PROPOSAL_ROWS):
        rows.append(
            ProposalRow(
                proposal_id=int(row.id),
                fingerprint=str(row.fingerprint),
                status=str(row.status),
                sensitive=bool(row.sensitive),
                action=dict(row.action or {}),
                confidence=str(row.confidence),
                conflict_type=str(row.conflict_type),
                disagreeing_fields=tuple(row.disagreeing_fields or ()),
                conflict_status=str(row.conflict_status),
                oscillating=bool(row.oscillating),
            )
        )
    return tuple(rows)


def build_pipeline() -> PipelineRun:
    """Run the graded pass once. See the module docstring for the pinned order."""
    started_at = datetime.now(tz=UTC)
    dsn = _dsn()
    root = default_fixtures_root()
    engine = get_engine()

    with engine.connect() as conn:
        precondition = assert_loaded(conn, root)
    log.info("suite.precondition_ok", label=precondition.summary())

    reset_graded_layer()

    # -- detection A, timed. This clock is SPEC's invariant-pass benchmark.
    clock = time.perf_counter()
    with _psycopg(dsn) as conn:
        run_a = run_invariants(conn, run_id="suite-detect-a", generation=CURRENT_GENERATION)
    invariants_seconds = time.perf_counter() - clock

    clock = time.perf_counter()
    with _psycopg(dsn) as conn:
        persist_run(conn, run_a)
        conn.commit()
    persist_seconds = time.perf_counter() - clock

    # -- detection B, fresh connection: the determinism check's second run.
    clock = time.perf_counter()
    with _psycopg(dsn) as conn:
        run_b = run_invariants(conn, run_id="suite-detect-b", generation=CURRENT_GENERATION)
    invariants_b_seconds = time.perf_counter() - clock

    # -- two dry proposals runs, both rolled back, both from zero proposals.
    dry_a = _dry_reconcile("determinism-a")
    dry_b = _dry_reconcile("determinism-b")

    # -- the committed run, bracketed by the mirror digest.
    with engine.connect() as conn:
        mirror_before = mirror_digest(conn)
    clock = time.perf_counter()
    report_first = run_once()
    reconcile_seconds = time.perf_counter() - clock
    with engine.connect() as conn:
        mirror_after = mirror_digest(conn)

    # -- R16: with every fingerprint already open, a second pass proposes zero.
    report_second = run_once()

    with engine.connect() as conn:
        proposals = _read_proposals(conn)
        conflict_status = {str(row.status): int(row.n) for row in conn.execute(_CONFLICT_STATUS)}

    notes: list[str] = []
    if not report_first.escalation_reason_persisted:
        notes.append(
            "conflicts.escalation_reason was not writable by recon_writer; the "
            "escalation reason is in the conflict.escalated audit row only"
        )

    return PipelineRun(
        started_at=started_at,
        precondition=precondition,
        run_a=run_a,
        run_b=run_b,
        invariants_seconds=invariants_seconds,
        invariants_b_seconds=invariants_b_seconds,
        persist_seconds=persist_seconds,
        mirror_before=mirror_before,
        mirror_after=mirror_after,
        report_first=report_first,
        report_second=report_second,
        dry_a=dry_a,
        dry_b=dry_b,
        reconcile_seconds=reconcile_seconds,
        proposals=proposals,
        conflict_status=conflict_status,
        fixtures_root=root,
        dsn_database=_database_name(),
        notes=notes,
    )


_CACHE: dict[str, PipelineRun] = {}


def pipeline() -> PipelineRun:
    """The process-wide graded pass, built on first use.

    Cached rather than re-run: every check must be describing the same pass, and
    two rows that silently graded two different runs is a defect this harness has
    no way to notice from the outside.
    """
    if "run" not in _CACHE:
        _CACHE["run"] = build_pipeline()
    return _CACHE["run"]


def cached_pipeline() -> PipelineRun | None:
    """The graded pass **if one has already been built**, else ``None``.

    Never builds one. The scorecard header and the JSON body want the run's
    identifiers, and asking for them must not be the thing that starts a
    four-minute pipeline in a run where every check already failed.
    """
    return _CACHE.get("run")


def reset_pipeline_cache() -> None:
    """Drop the cached pass. For tests that drive the pipeline more than once."""
    _CACHE.clear()


def mirror_table_summary() -> str:
    """The mirror surface, named, for the scorecard detail line."""
    return f"{len(MIRROR_TABLES)} tables ({', '.join(MIRROR_TABLES)})"


def golden_dir_note() -> str:
    """Which ``golden/`` tree the run graded against -- committed, or overridden."""
    override = os.environ.get("KEYSTONE_GOLDEN_DIR")
    return f"golden dir override {override}" if override else "committed golden/"
