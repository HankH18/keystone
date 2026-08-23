"""The one graded store the apply and review-API suites both read.

Built once per process and memoized here rather than in a fixture, for the same
reason ``tests/er/dataset.py`` is: a session-scoped fixture imported into two
``conftest.py`` files is two fixture definitions and would build it twice.

**One database for the whole run, deliberately.** ``tests/er/scratchdb.use_database``
points the *process* at a DSN and clears every cached engine, so two suites that
each created their own scratch database would fight over ``DATABASE_URL`` and
whichever ran second would silently re-point the other's ``TestClient``. So this
module starts from :func:`tests.er.dataset.ensure_dataset` -- the committed
full-profile generation-3 dataset with the identity layer materialized -- and
adds, on top of it and in the same database:

* the committed invariant run, which must produce the **graded** conflict set
  (3,050 in golden's per-type distribution) -- asserted, because a suite that
  counts sensitive proposals in a weaker world proves nothing about the real one;
* one ``recon.reconciler.reconcile`` pass, which is what puts real proposals --
  real actions, real confidences, real ``sensitive`` flags -- in front of the
  gate under test.

Nothing here fabricates a proposal. Every row the apply tests act on came out of
the committed pipeline, so a green here is evidence about the shipped detector
and not about a fixture someone wrote to match the code.

**What this store is NOT, stated so no test over-claims from it.**
``ensure_dataset`` lands generation 3 only and materializes the identity layer
from it, and ``recon.resolve.materialize`` refuses to run twice for a generation
(the identity layer is append-only to ``recon_writer``; re-materializing would
duplicate it). So ``field_lineage`` here covers ONE generation, contract SS7's
A -> B -> A window has nothing to scan, the ``oscillation_observed`` signal is 0
for every conflict, and **no conflict escalates** (escalation is driven by that
same scan) -- which means the **confidence vector here is the no-oscillation
variant, not the graded one**, and ``conflicts.status`` is uniformly ``open``.
The conflict set is unaffected
(invariants read generation 3 alone, SS7) and so is every ``sensitive``
classification (contract SS6 makes it a pure function of the target field path),
which is what this suite asserts on. The graded three-generation confidence
vector is `tests/reconciler`'s subject, and it builds its own database for it.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN = REPO_ROOT / "golden"


@dataclass(frozen=True)
class HeldProposal:
    """One row of the held population, as the pipeline left it.

    A plain record rather than a live `Row` because :attr:`GradedStore.held` is a
    snapshot: see :func:`_held_population` for why it has to be one.
    """

    id: int
    confidence: Any
    status: str
    sensitive: bool
    type: str


@dataclass(frozen=True)
class GradedStore:
    """A migrated database holding the graded conflict AND proposal store."""

    dsn: str
    conflicts: int
    proposals: int
    by_type: dict[str, int]
    report: Any
    held: tuple[HeldProposal, ...]


_STATE: GradedStore | None = None


#: Every proposal R15 holds, by any of the four ways a proposal can be in that
#: population: the status the reconciler gave it, the `sensitive` column it
#: stamped, the conflict type it came from, or a target path on contract SS6's
#: `SENSITIVE_FIELDS`. Selected from columns `recon.apply` does not write, so a
#: bug in the gate cannot also shrink the set the gate is judged on.
_HELD_OR_SENSITIVE_TARGET = """
    SELECT p.id, p.confidence, p.status::text AS status, p.sensitive, c.type
      FROM proposals p
      JOIN conflicts c ON c.id = p.conflict_id
     WHERE p.status = 'sensitive_hold'
        OR p.sensitive
        OR c.type = 'C14'
        OR EXISTS (
            SELECT 1 FROM jsonb_object_keys(p.action -> 'set') AS k
             WHERE k = ANY(%(sensitive)s::text[]))
     ORDER BY p.id
"""


def _held_population(dsn: str) -> tuple[HeldProposal, ...]:
    """The held population **at the moment the pipeline finished**, snapshotted.

    Read here, and not from a fixture, because this database is shared with
    `tests/api` (see the module docstring) and one of its cases is
    ``test_the_auto_path_refuses_a_sensitive_proposal_even_once_approved``: a
    reviewer approving a held proposal over real HTTP, committed. That is
    legitimate -- R15 forces a held proposal to *human* review, it does not
    forbid the fix -- and it is **irreversible**: migration 0006's KS004 freezes
    ``decided_by`` once non-NULL and admits no arc back out of ``approved``, for
    the schema owner included.

    So a fixture that queried this population later would be reading a store two
    suites had legitimately moved on from, and
    ``test_the_held_population_is_the_graded_one`` would be asserting against a
    population that is no longer the graded one -- which is the very thing its
    name claims. The rows are real rows, read by the same SQL from the same
    columns; only the *moment* is pinned, to the one the assertions are about.
    """
    import psycopg
    from psycopg.rows import dict_row

    from recon.reference import SENSITIVE_FIELDS

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            _HELD_OR_SENSITIVE_TARGET, {"sensitive": sorted(SENSITIVE_FIELDS)}
        ).fetchall()
    return tuple(
        HeldProposal(
            id=row["id"],
            confidence=row["confidence"],
            status=row["status"],
            sensitive=row["sensitive"],
            type=row["type"],
        )
        for row in rows
    )


def ensure_store() -> GradedStore:
    """Build (once) and return the graded conflict + proposal store."""
    global _STATE
    if _STATE is not None:
        return _STATE

    from tests.er.dataset import ensure_dataset

    dataset = ensure_dataset()

    import psycopg

    from recon.invariants.runner import persist_run, run_invariants
    from recon.reconciler import reconcile

    with psycopg.connect(dataset.dsn) as conn:
        run = run_invariants(conn, run_id="t11-invariants")
        persist_run(conn, run)
        conn.commit()

    _assert_store_matches_golden(run)
    report = reconcile(run_id="t11-reconcile")

    with psycopg.connect(dataset.dsn) as conn:
        proposals = conn.execute("SELECT count(*) FROM proposals").fetchone()[0]
    assert proposals == report.proposed, (
        f"the reconciler reported {report.proposed} proposals and the table holds {proposals}"
    )

    _STATE = GradedStore(
        dsn=dataset.dsn,
        conflicts=len(run.conflicts),
        proposals=proposals,
        by_type=dict(run.by_type()),
        report=report,
        held=_held_population(dataset.dsn),
    )
    return _STATE


def _assert_store_matches_golden(run: Any) -> None:
    """The store must be the GRADED conflict set, or every count below is a lie.

    Borrowed verbatim in intent from ``tests/reconciler/conftest.py``, which
    documents the real failure it caught: a store with zero C4 made "every C4 is
    held" trivially true and every downstream assertion still passed.
    """
    golden = json.loads((GOLDEN / "conflicts.json").read_text())
    expected = dict(sorted(Counter(entry["type"] for entry in golden).items()))
    detected = run.by_type()
    assert detected == expected, (
        "the conflict store is not the graded set, so every proposal count in the "
        f"apply suite would be counting the wrong world.\n  detected: {detected}\n"
        f"  golden  : {expected}"
    )
