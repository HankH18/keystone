"""SS7's A -> B -> A scan over `field_lineage`, and the `oscillating` flag it decides.

    "**Oscillation detection**: window scan of `field_lineage` per `(person_key,
     field)` for the pattern `A, B, A` across ascending generations. `field_lineage`,
     the A,B,A scan and R16's fingerprint dedup are **all keyed on `person_key`**
     (SS4.1), which is stable across generations. On oscillation the conflict is
     marked `escalated:oscillation` and the reconciler **must not** re-propose the
     identical fix (R16)."

SS8 puts the answer in the graded artifact -- `golden/conflicts.json` carries
`"oscillating": true` on the entries whose field oscillated -- and SS7 makes that
column the input to R4/R16. It is therefore not a decoration on the conflict row: it
decides whether a proposal is escalated and whether the identical fix may be
re-proposed.

**Why this module exists at all.** `oscillating` used to be a `bool = False` default
on :class:`~recon.invariants.runner.DetectedConflict` that nothing ever assigned, and
`persist_run` wrote that constant into the NOT NULL `conflicts.oscillating` column.
SS5.4's field-exactness list -- `disagreeing_fields`, `sources_involved`,
`observed_values.keys`, `observed_values`, `expected_verdict` -- deliberately excludes
`oscillating`, so the golden diff is structurally incapable of seeing the divergence:
the graded column was uniformly wrong against golden's 25 and the harness stayed
green. Deriving it here means the column is an ANSWER (right or wrong, and testable
either way) rather than an unfalsifiable claim.

**The known gap, stated rather than hidden.** The invariant suite ingests generation 3
only (SS7: "Invariants read generation 3 only"), so `field_lineage` -- which SS3 says
retains generations 1-3 -- has no rows and this scan correctly returns the empty set.
Nothing in the repository writes `field_lineage` yet; populating it from generations
1-2 belongs to the ingest/ER ticket, not to the invariant engine.
:class:`LineageScan` therefore reports `rows` alongside `oscillating`, so a caller can
tell "scanned, nothing oscillates" from "there was nothing to scan" -- the distinction
a bare `False` destroys. `tests/invariants/test_oscillation.py` asserts both halves:
that the scan works when handed lineage, and that the committed gen-3-only pipeline
produces 0 against golden's 25.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from psycopg import Connection

from recon.reference import person_key

__all__ = [
    "LINEAGE_TABLE",
    "OSCILLATION_TYPES",
    "LineageScan",
    "mark_oscillating",
    "scan_field_lineage",
]

#: SS3. Written by the ingest/ER path; read here and nowhere else in the engine.
LINEAGE_TABLE = "field_lineage"

#: SS7 marks a conflict oscillating "where the conflict's **field** oscillated", so
#: only the two types that carry `disagreeing_fields` (SS2.4: "No rule other than
#: `R-006` / `R-014` populates `disagreeing_fields`") have a field to test. Every
#: `oscillating: true` entry in the committed `golden/conflicts.json` is a C6, which
#: is the same partition seen from the generator's side.
OSCILLATION_TYPES: frozenset[str] = frozenset({"C6", "C14"})

#: One row per `(canonical_id, field, generation)`, the value taken from the
#: lexicographically smallest `source_ref` -- SS4.6's committed survivorship tiebreak,
#: reused so a person carrying two records of one source cannot make the scan depend
#: on row order. `COLLATE "C"` keeps "smallest" byte order rather than a property of
#: the cluster's locale. The A -> B -> A test compares `value_text` (SS3's
#: `value_canon`) for STRING equality, exactly as SS7 pins it.
_SCAN_SQL = f"""
WITH per_generation AS (
    SELECT DISTINCT ON (canonical_id, field, generation)
           canonical_id, field, generation, value_text
      FROM {LINEAGE_TABLE}
     ORDER BY canonical_id, field, generation, source_ref COLLATE "C" NULLS LAST
),
windowed AS (
    SELECT canonical_id,
           field,
           value_text,
           lag(value_text, 1) OVER w AS previous,
           lag(value_text, 2) OVER w AS before_previous
      FROM per_generation
    WINDOW w AS (PARTITION BY canonical_id, field ORDER BY generation)
)
SELECT DISTINCT canonical_id::text, field
  FROM windowed
 WHERE value_text IS NOT NULL
   AND previous IS NOT NULL
   AND before_previous IS NOT NULL
   AND before_previous = value_text
   AND previous <> value_text
"""

_COUNT_SQL = f"SELECT count(*) FROM {LINEAGE_TABLE}"


@dataclass(frozen=True, slots=True)
class LineageScan:
    """The A -> B -> A result, plus how much lineage it actually saw.

    `rows == 0` with `pairs == frozenset()` is **not** the same statement as "nothing
    oscillates": it is "there was no lineage to scan". Keeping the two apart is the
    whole point -- a scan with no input that reports a confident `False` is the defect
    this module replaced.
    """

    #: `(canonical_id, field_path)` pairs matching SS7's `A, B, A`.
    pairs: frozenset[tuple[str, str]]
    #: Total `field_lineage` rows visible to the scan.
    rows: int

    @property
    def had_input(self) -> bool:
        return self.rows > 0

    def oscillates(self, canonical_id: str, field_path: str) -> bool:
        return (canonical_id, field_path) in self.pairs


def scan_field_lineage(conn: Connection) -> LineageScan:
    """SS7's window scan of `field_lineage`, per `(person_key, field_path)`."""
    with conn.cursor() as cur:
        cur.execute(_COUNT_SQL)
        rows = int(cur.fetchone()[0])
        cur.execute(_SCAN_SQL)
        pairs = frozenset((str(row[0]), str(row[1])) for row in cur.fetchall())
    return LineageScan(pairs=pairs, rows=rows)


def mark_oscillating(conflicts: Iterable, scan: LineageScan) -> list:
    """Return `conflicts` with `oscillating` set from `scan` (SS7, SS8).

    Keyed on `person_key` (SS4.1) -- `uuid5(KEYSTONE_NS, anchor_ref(entity_refs))` --
    because SS7 pins `field_lineage`, this scan and R16's dedup to that one key, and
    it is the only key stable across generations. A conflict whose `entity_refs`
    carry no identity ref (none of SS5.5's types can, but the helper raises rather
    than guessing) is left unmarked.
    """
    result: list = []
    for conflict in conflicts:
        oscillating = False
        if scan.pairs and conflict.type in OSCILLATION_TYPES and conflict.disagreeing_fields:
            key = _person_key_or_none(conflict.entity_refs)
            if key is not None:
                oscillating = any(
                    scan.oscillates(key, path) for path in conflict.disagreeing_fields
                )
        result.append(
            conflict
            if conflict.oscillating == oscillating
            else replace(conflict, oscillating=oscillating)
        )
    return result


def _person_key_or_none(refs: Sequence[str]) -> str | None:
    try:
        return str(person_key(refs))
    except ValueError:  # no identity ref -- SS4.1 has no person to key on
        return None
