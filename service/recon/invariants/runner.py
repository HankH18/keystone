"""The invariant runner: execute every rule, stamp every record, fingerprint the rest.

SS3's last pipeline stage, in one call:

    stg_* + the SS4 cascade  --rules/*.sql-->  invariant_results  -->  conflicts

Four things it is responsible for and no rule can be:

1. **Per-record stamping (SS5.8).** Every `stg_*` row is stamped for every rule whose
   scope includes it, with `(rule_id, rule_version, run_id)`. A row in scope of zero
   rules gets the synthetic `R-000` row -- which is a real rule file, not a special
   case here.
2. **Completeness gating (SS5.3).** If any generation-3 load is incomplete the run is
   `degraded` and every ABSENCE rule (C1, C2, C5, C7, C8, C9, C13) is **skipped** --
   stamped `unchecked` / `source_incomplete`, never fired. Handing an absence rule an
   incomplete generation manufactures thousands of false positives; this is the whole
   reason ingest keeps a completeness ledger.

   **Deliberate deviation, recorded rather than discovered.** SS5.3 scopes the skip to
   the rules that depend on the absence of records "from source S"; the gate here is
   RUN-WIDE -- any incomplete `(source, entity_type)` skips all seven absence rules,
   not only those reading the failed source. Per-source scoping would need a
   rule-to-source dependency map SS5.3 does not pin (C8's predicate spans `crm` AND
   `payments`; C9's spans `crm` AND `appdb`), and getting it wrong in the permissive
   direction is a false-positive machine. The run-wide behaviour is strictly more
   conservative: it produces MORE `unchecked` and never a false conflict. See the
   ticket's `contract_gaps`.
3. **Precedence (SS5.7).** `recon.reference.apply_precedence` -- the SAME function the
   generator ran before writing `golden/` (`G32`). It is never re-implemented from the
   SS5.7 table: the contract takes C7 from a raw 875 down to 300 through three
   separate rules, and two slightly different filters would be up to 575 false
   positives against a golden count of 300.
4. **Fingerprinting (SS5.4).** `recon.reference.conflict_refs` builds `entity_refs` and
   `recon.reference.fingerprint` hashes it. `rules/*.sql` never build a fingerprint or
   an `observed_values` string (SS2.5).
5. **Oscillation (SS7).** `recon.invariants.oscillation` runs the `A -> B -> A` scan
   over `field_lineage`, keyed on `person_key`, and that is what sets `oscillating`
   on the surviving conflicts. SS5.4's field-exactness list excludes `oscillating`,
   so a hardcoded value there is a graded artifact column no golden diff can ever
   falsify -- see that module's docstring for the gap this closes and the one it
   reports rather than hides.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from psycopg import Connection

from recon.reference import (
    RULE_ID_BY_TYPE,
    apply_precedence,
    conflict_key,
    conflict_refs,
    fingerprint,
    sources_involved,
    validate_observed_values,
)

from .context import CURRENT_GENERATION, InvariantContext, build_context
from .oscillation import LineageScan, mark_oscillating, scan_field_lineage
from .rules import DB_VERDICT, RuleSpec, load_rules

__all__ = [
    "DetectedConflict",
    "InvariantRun",
    "RuleOutcome",
    "persist_run",
    "run_invariants",
]

_REF_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("identity_refs", "identity_refs"),
    ("enrollment_refs", "enrollment_refs"),
    ("payment_refs", "payment_refs"),
    ("contact_refs", "contact_refs"),
    ("student_refs", "student_refs"),
)


@dataclass(frozen=True, slots=True)
class DetectedConflict:
    """One detected conflict in `golden/conflicts.json`'s shape (SS8).

    The attribute names are the ones `apply_precedence` reads (`type`,
    `entity_refs`, `disagreeing_fields`), so the filter runs over these objects
    unchanged.
    """

    type: str
    rule_id: str
    entity_refs: tuple[str, ...]
    sources_involved: tuple[str, ...]
    disagreeing_fields: tuple[str, ...]
    observed_values: dict[str, Any]
    fingerprint: str
    expected_verdict: str = "conflict"
    #: SS7/SS8. Assigned by `recon.invariants.oscillation.mark_oscillating` from the
    #: `field_lineage` A -> B -> A scan, never by a rule and never left as a
    #: hardcoded constant: SS5.4's field-exactness list excludes `oscillating`, so a
    #: constant here is a graded artifact column no golden diff can falsify. The
    #: default is the "not oscillating" answer for a run whose lineage scan found
    #: nothing; `InvariantRun.lineage` records whether there was anything to find.
    oscillating: bool = False

    @property
    def key(self) -> tuple[str, tuple[str, ...]]:
        return conflict_key(self)

    def as_json(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "rule_id": self.rule_id,
            "entity_refs": list(self.entity_refs),
            "sources_involved": list(self.sources_involved),
            "disagreeing_fields": list(self.disagreeing_fields),
            "observed_values": self.observed_values,
            "expected_verdict": self.expected_verdict,
            "oscillating": self.oscillating,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """What one rule did on one run."""

    rule_id: str
    rule_version: str
    scope_table: str
    skipped: bool
    rows: int
    verdicts: Mapping[str, int]
    raw_conflicts: int
    elapsed_ms: float


@dataclass
class InvariantRun:
    """The whole run: per-record verdicts, surviving conflicts, and the timings."""

    run_id: str
    generation: int
    status: str
    incomplete: tuple[tuple[str, str], ...]
    outcomes: tuple[RuleOutcome, ...]
    #: `(rule_id, rule_version, record_ref, entity_type, verdict, unchecked_reason)`
    results: list[tuple[str, str, str, str, str, str | None]]
    raw_conflicts: list[DetectedConflict]
    conflicts: list[DetectedConflict]
    elapsed_ms: float = 0.0
    context: InvariantContext | None = field(default=None, repr=False)
    #: SS7's A -> B -> A scan result. `lineage.rows == 0` means `field_lineage` was
    #: empty, so every `oscillating` on this run is "no lineage to scan", not
    #: "scanned and found none". Reported so the two are never conflated.
    lineage: LineageScan | None = field(default=None, repr=False)

    @property
    def degraded(self) -> bool:
        return self.status == "degraded"

    @property
    def oscillating_count(self) -> int:
        return sum(1 for conflict in self.conflicts if conflict.oscillating)

    @property
    def fingerprints(self) -> tuple[str, ...]:
        return tuple(sorted(conflict.fingerprint for conflict in self.conflicts))

    def by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for conflict in self.conflicts:
            counts[conflict.type] = counts.get(conflict.type, 0) + 1
        return dict(sorted(counts.items()))


def _build_conflict(payload: Mapping[str, Any]) -> DetectedConflict:
    """Turn one rule's `detail.conflicts[]` element into a fingerprinted conflict.

    Every field a rule emits is a *component list*; `conflict_refs` assembles and
    validates them against SS5.5's per-type `entity_refs` shape, so an under- or
    over-specified rule raises here rather than silently mismatching the harness key.
    """
    conflict_type = str(payload["conflict_type"])
    components = {
        argument: tuple(payload.get(json_key) or ()) for json_key, argument in _REF_COMPONENTS
    }
    refs = conflict_refs(conflict_type, **components)
    paths = tuple(sorted(payload.get("disagreeing_fields") or ()))
    observed = dict(payload.get("observed_values") or {})
    validate_observed_values(conflict_type, observed)
    return DetectedConflict(
        type=conflict_type,
        rule_id=RULE_ID_BY_TYPE[conflict_type],
        entity_refs=refs,
        sources_involved=sources_involved(refs),
        disagreeing_fields=paths,
        observed_values=observed,
        fingerprint=fingerprint(conflict_type, refs, paths, observed),
    )


def _dedupe(conflicts: Iterable[DetectedConflict]) -> list[DetectedConflict]:
    """Collapse the per-record stamps of a pair-emitting rule onto one entry.

    SS5.2: C3 and C11 emit exactly one entry per unordered PAIR, while SS5.5's rule
    scope stamps one `invariant_results` row per *record*. Both members therefore
    carry the same conflict object, and the harness key `(type,
    tuple(sorted(entity_refs)))` is what makes them one conflict.

    A collision whose payloads disagree is a bug, not something to resolve by
    ordering: the two would produce two different fingerprints for one harness key
    and SS5.7 rule 11's uniqueness would be violated downstream. It raises.
    """
    seen: dict[tuple[str, tuple[str, ...]], DetectedConflict] = {}
    for conflict in conflicts:
        existing = seen.get(conflict.key)
        if existing is None:
            seen[conflict.key] = conflict
        elif existing.fingerprint != conflict.fingerprint:
            raise ValueError(
                f"two different fingerprints for one conflict key {conflict.key!r}: "
                f"{existing.fingerprint} vs {conflict.fingerprint} (SS5.7 rule 11)"
            )
    return [seen[key] for key in sorted(seen)]


def _execute(conn: Connection, spec: RuleSpec, generation: int, *, skipped: bool) -> Any:
    sql = spec.gated_sql() if skipped else spec.sql
    with conn.cursor() as cur:
        cur.execute(sql, {"generation": generation})
        return cur.fetchall()


def run_invariants(
    conn: Connection,
    *,
    run_id: str,
    generation: int = CURRENT_GENERATION,
    rules: Sequence[RuleSpec] | None = None,
    context: InvariantContext | None = None,
) -> InvariantRun:
    """Execute every rule against generation N and return the run.

    `conn` must be a connection the runner may create `TEMP` tables on; it is left
    with the `er_*` / `ref_*` tables in place, so a caller can inspect them (the
    determinism check re-runs on a fresh connection instead, which is the point).
    """
    started = time.perf_counter()
    specs = tuple(rules) if rules is not None else load_rules()
    ctx = context or build_context(conn, generation)

    outcomes: list[RuleOutcome] = []
    #: `(rule_id, rule_version, record_ref, entity_type, verdict, unchecked_reason)`
    results: list[tuple[str, str, str, str, str, str | None]] = []
    raw: list[DetectedConflict] = []

    for spec in specs:
        skipped = spec.is_absence_rule and ctx.degraded
        rule_started = time.perf_counter()
        rows = _execute(conn, spec, generation, skipped=skipped)
        verdicts: dict[str, int] = {}
        emitted = 0
        for record_ref, entity_type, verdict, detail in rows:
            verdicts[verdict] = verdicts.get(verdict, 0) + 1
            reason = detail.get("reason") if isinstance(detail, Mapping) else None
            results.append(
                (spec.rule_id, spec.rule_version, record_ref, entity_type, verdict, reason)
            )
            _check_detail(spec, verdict, detail)
            if verdict != "conflict":
                continue
            for payload in detail["conflicts"]:
                raw.append(_build_conflict(payload))
                emitted += 1
        outcomes.append(
            RuleOutcome(
                rule_id=spec.rule_id,
                rule_version=spec.rule_version,
                scope_table=spec.scope_table,
                skipped=skipped,
                rows=len(rows),
                verdicts=dict(sorted(verdicts.items())),
                raw_conflicts=emitted,
                elapsed_ms=(time.perf_counter() - rule_started) * 1000.0,
            )
        )

    # SS7: the `oscillating` column is decided by the `field_lineage` A -> B -> A
    # scan, and it is decided AFTER `PRECEDENCE` for the same reason `compound_with`
    # is populated after it (SS8) -- the flag belongs to entries that survive.
    lineage = scan_field_lineage(conn)
    deduped = _dedupe(raw)
    surviving = mark_oscillating(apply_precedence(deduped), lineage)

    return InvariantRun(
        run_id=run_id,
        generation=generation,
        status="degraded" if ctx.degraded else "ok",
        incomplete=ctx.incomplete,
        outcomes=tuple(outcomes),
        results=results,
        raw_conflicts=deduped,
        conflicts=list(surviving),
        elapsed_ms=(time.perf_counter() - started) * 1000.0,
        context=ctx,
        lineage=lineage,
    )


def _check_detail(spec: RuleSpec, verdict: str, detail: Any) -> None:
    """SS5.8: `detail.reason` is REQUIRED on `unchecked` and FORBIDDEN otherwise.

    Checked in the runner rather than trusted from the rule, because a missing
    reason is exactly the shape of an `unchecked` that is really a silent skip.
    """
    if verdict not in DB_VERDICT:
        raise ValueError(f"{spec.rule_id}: verdict {verdict!r} is not in SS5.8's closed set")
    if verdict == "unchecked":
        if not isinstance(detail, Mapping) or not detail.get("reason"):
            raise ValueError(f"{spec.rule_id}: `unchecked` requires detail.reason (SS5.8)")
        return
    if isinstance(detail, Mapping) and "reason" in detail:
        raise ValueError(
            f"{spec.rule_id}: detail.reason is forbidden on verdict {verdict!r} (SS5.8)"
        )
    if verdict == "conflict" and (not isinstance(detail, Mapping) or not detail.get("conflicts")):
        raise ValueError(f"{spec.rule_id}: verdict 'conflict' must carry detail.conflicts[]")


_INSERT_CONFLICTS = """
INSERT INTO conflicts
    (fingerprint, type, rule_id, entity_refs, sources, disagreeing_fields,
     observed_values, oscillating, first_seen_run, last_seen_run)
VALUES (%(fingerprint)s, %(type)s, %(rule_id)s, %(entity_refs)s, %(sources)s,
        %(disagreeing_fields)s, %(observed_values)s, %(oscillating)s, %(run)s, %(run)s)
ON CONFLICT (fingerprint) DO UPDATE SET last_seen_run = EXCLUDED.last_seen_run
"""


def persist_run(conn: Connection, run: InvariantRun) -> None:
    """Write `invariant_results` and `conflicts` for one run.

    `verdict` is translated at this boundary and nowhere else: SS5.8 pins the
    vocabulary as `ok` / `conflict` / `unchecked`, while the committed
    `invariant_verdict` Postgres enum (migration 0001) spells the first two
    `pass` / `fail`. See `rules.DB_VERDICT`.

    The per-record `detail` column carries SS5.8's `reason` on every `unchecked` row
    and NULL everywhere else -- required on `unchecked`, forbidden otherwise. The
    conflict payload is not repeated here; it is materialized in `conflicts`.
    """
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        with cur.copy(
            "COPY invariant_results "
            "(run_id, rule_id, rule_version, record_ref, entity_type, verdict, detail) "
            "FROM STDIN"
        ) as copy:
            for rule_id, version, record_ref, entity_type, verdict, reason in run.results:
                copy.write_row(
                    (
                        run.run_id,
                        rule_id,
                        version,
                        record_ref,
                        entity_type,
                        DB_VERDICT[verdict],
                        None if reason is None else Jsonb({"reason": reason}),
                    )
                )
        for conflict in run.conflicts:
            cur.execute(
                _INSERT_CONFLICTS,
                {
                    "fingerprint": conflict.fingerprint,
                    "type": conflict.type,
                    "rule_id": conflict.rule_id,
                    "entity_refs": Jsonb(list(conflict.entity_refs)),
                    "sources": Jsonb(list(conflict.sources_involved)),
                    "disagreeing_fields": Jsonb(list(conflict.disagreeing_fields)),
                    "observed_values": Jsonb(conflict.observed_values),
                    "oscillating": conflict.oscillating,
                    "run": run.run_id,
                },
            )
