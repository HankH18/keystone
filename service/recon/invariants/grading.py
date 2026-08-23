"""The golden diff -- the brief's TWO categories, and nothing else.

    "the golden set of seeded conflicts is caught exactly -- no false negatives, no
     false positives -- verified by an automated test"

SS5.4 pins how a detection is matched to a golden entry and what the two categories
are:

* the match key is `(type, tuple(sorted(entity_refs)))`;
* a detected conflict matching **no** golden entry is a **false positive**, and a
  golden entry matched by **no** detection is a **false negative** -- *regardless* of
  whether any of its refs intersects `golden/clean-sample.json`;
* for every matched pair the harness **additionally** asserts equality of
  `sorted(disagreeing_fields)`, `sorted(sources_involved)`,
  `sorted(observed_values.keys())` and `expected_verdict`. A mismatch fails the
  suite and is printed as a **field-exactness detail line** -- it is **not** a third
  category.

SS8 adds the stricter probe on top: a `golden/clean-sample.json` entity is
**FLAGGED iff any detected conflict's `entity_refs` INTERSECTS its identity refs**,
and every such intersection is one false positive. The intersection predicate is
SS5.7 ruling 9's, the same one `apply_precedence` uses, so the suppression count and
the false-positive count stay consistent.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from recon.invariants.runner import DetectedConflict

__all__ = [
    "CleanSampleResult",
    "FieldMismatch",
    "GoldenDiff",
    "golden_dir",
    "grade_clean_sample",
    "grade_run",
    "load_clean_sample",
    "load_golden",
]

GOLDEN_DIR_ENV = "KEYSTONE_GOLDEN_DIR"

ConflictKey = tuple[str, tuple[str, ...]]


def golden_dir() -> Path:
    """The committed `golden/` tree (repo root)."""
    override = os.environ.get(GOLDEN_DIR_ENV)
    if override:
        return Path(override).resolve()
    return Path(__file__).resolve().parents[3] / "golden"


def load_golden(directory: Path | None = None) -> list[dict[str, Any]]:
    """`golden/conflicts.json` -- the grading contract, 3,050 entries at `full`."""
    root = directory or golden_dir()
    return json.loads((root / "conflicts.json").read_text(encoding="utf-8"))


def load_clean_sample(directory: Path | None = None) -> list[dict[str, Any]]:
    """`golden/clean-sample.json` -- 1,000 entities asserted conflict-free."""
    root = directory or golden_dir()
    return json.loads((root / "clean-sample.json").read_text(encoding="utf-8"))


def _key(entry: Mapping[str, Any] | DetectedConflict) -> ConflictKey:
    if isinstance(entry, Mapping):
        return (str(entry["type"]), tuple(sorted(entry["entity_refs"])))
    return entry.key


@dataclass(frozen=True, slots=True)
class FieldMismatch:
    """One SS5.4 field-exactness disagreement on a MATCHED pair. Not a category."""

    key: ConflictKey
    field_name: str
    golden: Any
    detected: Any

    def line(self) -> str:
        return (
            f"    field-exactness {self.key[0]} {self.key[1]!r}: {self.field_name} "
            f"golden={self.golden!r} detected={self.detected!r}"
        )


@dataclass
class GoldenDiff:
    """The two categories, plus the field-exactness detail lines."""

    matched: int
    false_negatives: list[ConflictKey]
    false_positives: list[ConflictKey]
    mismatches: list[FieldMismatch]
    golden_total: int
    detected_total: int

    @property
    def passed(self) -> bool:
        return not (self.false_negatives or self.false_positives or self.mismatches)

    def counts_by_type(self, keys: Sequence[ConflictKey]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for conflict_type, _refs in keys:
            counts[conflict_type] = counts.get(conflict_type, 0) + 1
        return dict(sorted(counts.items()))

    def report(self, *, limit: int = 20) -> str:
        lines = [
            f"golden entries: {self.golden_total}   detected: {self.detected_total}   "
            f"matched: {self.matched}",
            f"FALSE NEGATIVES: {len(self.false_negatives)} "
            f"{self.counts_by_type(self.false_negatives)}",
            f"FALSE POSITIVES: {len(self.false_positives)} "
            f"{self.counts_by_type(self.false_positives)}",
        ]
        for label, keys in (
            ("FN", self.false_negatives),
            ("FP", self.false_positives),
        ):
            for key in keys[:limit]:
                lines.append(f"    {label} {key[0]} {list(key[1])}")
            if len(keys) > limit:
                lines.append(f"    ... {len(keys) - limit} more {label}")
        if self.mismatches:
            lines.append(f"field-exactness detail lines: {len(self.mismatches)}")
            lines.extend(mismatch.line() for mismatch in self.mismatches[:limit])
        return "\n".join(lines)


def grade_run(
    detected: Iterable[DetectedConflict],
    golden: Sequence[Mapping[str, Any]] | None = None,
) -> GoldenDiff:
    """Diff a detected conflict set against `golden/conflicts.json` (SS5.4)."""
    golden_entries = list(golden if golden is not None else load_golden())
    golden_by_key: dict[ConflictKey, Mapping[str, Any]] = {}
    for entry in golden_entries:
        key = _key(entry)
        if key in golden_by_key:  # SS5.7 rule 11 -- never silently deduped
            raise ValueError(f"duplicate golden key {key!r} (SS5.7 rule 11)")
        golden_by_key[key] = entry

    detected_by_key: dict[ConflictKey, DetectedConflict] = {}
    for conflict in detected:
        key = conflict.key
        if key in detected_by_key:
            raise ValueError(f"duplicate detected key {key!r} (SS5.7 rule 11)")
        detected_by_key[key] = conflict

    false_negatives = sorted(set(golden_by_key) - set(detected_by_key))
    false_positives = sorted(set(detected_by_key) - set(golden_by_key))

    mismatches: list[FieldMismatch] = []
    for key in sorted(set(golden_by_key) & set(detected_by_key)):
        expected, found = golden_by_key[key], detected_by_key[key]
        for name, want, got in (
            (
                "disagreeing_fields",
                sorted(expected.get("disagreeing_fields") or ()),
                sorted(found.disagreeing_fields),
            ),
            (
                "sources_involved",
                sorted(expected.get("sources_involved") or ()),
                sorted(found.sources_involved),
            ),
            (
                "observed_values.keys",
                sorted(expected.get("observed_values") or {}),
                sorted(found.observed_values),
            ),
            (
                "expected_verdict",
                expected.get("expected_verdict"),
                found.expected_verdict,
            ),
            (
                "observed_values",
                expected.get("observed_values") or {},
                json.loads(json.dumps(found.observed_values, sort_keys=True, default=str)),
            ),
        ):
            if want != got:
                mismatches.append(FieldMismatch(key, name, want, got))

    return GoldenDiff(
        matched=len(set(golden_by_key) & set(detected_by_key)),
        false_negatives=false_negatives,
        false_positives=false_positives,
        mismatches=mismatches,
        golden_total=len(golden_by_key),
        detected_total=len(detected_by_key),
    )


@dataclass
class CleanSampleResult:
    """SS8's stricter probe over the 1,000-entity conflict-free subsample."""

    sampled: int
    flagged: list[tuple[tuple[str, ...], ConflictKey]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.flagged

    def report(self, *, limit: int = 20) -> str:
        lines = [
            f"clean sample: {self.sampled} entities   FLAGGED: {len(self.flagged)}",
        ]
        for identity, key in self.flagged[:limit]:
            lines.append(f"    FLAGGED {list(identity)} by {key[0]} {list(key[1])}")
        if len(self.flagged) > limit:
            lines.append(f"    ... {len(self.flagged) - limit} more")
        return "\n".join(lines)


def _identity_refs(entity: Any) -> tuple[str, ...]:
    if isinstance(entity, Mapping):
        for name in ("identity_refs", "entity_refs", "refs"):
            value = entity.get(name)
            if value:
                return tuple(value)
        raise ValueError(f"clean-sample entity carries no identity refs: {entity!r}")
    return tuple(entity)


def grade_clean_sample(
    detected: Iterable[DetectedConflict],
    sample: Sequence[Any] | None = None,
) -> CleanSampleResult:
    """SS8: an entity is FLAGGED iff any detected `entity_refs` INTERSECTS its refs."""
    entities = list(sample if sample is not None else load_clean_sample())
    conflicts = list(detected)

    index: dict[str, list[DetectedConflict]] = {}
    for conflict in conflicts:
        for ref in conflict.entity_refs:
            index.setdefault(ref, []).append(conflict)

    result = CleanSampleResult(sampled=len(entities))
    for entity in entities:
        identity = _identity_refs(entity)
        # One flag per (entity, conflict) pair. A conflict that names TWO of an
        # entity's identity refs is one intersection of one conflict with one entity,
        # not two false positives -- SS8's unit is "any detected conflict's
        # `entity_refs` INTERSECTS that entity's identity refs".
        hits = {conflict.key for ref in identity for conflict in index.get(ref, ())}
        result.flagged.extend((identity, key) for key in sorted(hits))
    result.flagged.sort()
    return result
