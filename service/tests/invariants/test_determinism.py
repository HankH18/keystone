"""Determinism is graded (CLAUDE.md): two runs produce an identical fingerprint set.

Proved, not asserted. The second run happens on a **separate connection**, so it
builds its own `er_*` / `ref_*` temp tables from scratch and re-executes every rule
file -- nothing is carried over from the first run but the database contents. Any
dependence on `set` iteration order, dict insertion order, `PYTHONHASHSEED`, or an
`ORDER BY`-less query that Postgres happened to return in a different order would
show up here as a different digest, a different ref list, or a different order.
"""

from __future__ import annotations

import psycopg

from recon.invariants.runner import run_invariants


def _second_run(dsn: str):
    with psycopg.connect(dsn) as conn:
        return run_invariants(conn, run_id="t6-determinism")


def test_two_runs_produce_an_identical_fingerprint_set(invariant_run, ingested_dsn) -> None:
    again = _second_run(ingested_dsn)
    assert again.fingerprints == invariant_run.fingerprints
    assert len(set(again.fingerprints)) == len(again.fingerprints)


def test_two_runs_produce_an_identical_ordered_conflict_list(invariant_run, ingested_dsn) -> None:
    """SS8 sorts `golden/conflicts.json` by `(type, tuple(sorted(entity_refs)))`, and
    `apply_precedence` returns that order, so the *sequence* is comparable and not
    only the set."""
    again = _second_run(ingested_dsn)
    assert [conflict.as_json() for conflict in again.conflicts] == [
        conflict.as_json() for conflict in invariant_run.conflicts
    ]


def test_two_runs_stamp_the_same_per_record_verdicts(invariant_run, ingested_dsn) -> None:
    """SS5.8's per-record verdicts are the grading contract, so they are graded for
    determinism too -- not just the conflicts that survive."""
    again = _second_run(ingested_dsn)
    assert sorted(again.results) == sorted(invariant_run.results)


def test_the_shuffled_snapshot_argument_holds_for_entity_refs(invariant_run) -> None:
    """`entity_refs` is a **sorted set** (SS5.4), so no conflict can carry an order
    that a differently-ordered scan would move."""
    for conflict in invariant_run.conflicts:
        refs = list(conflict.entity_refs)
        assert refs == sorted(set(refs))


_SUBPROCESS_PROBE = """
import hashlib, json, os, sys
import psycopg
from recon.invariants.runner import run_invariants

with psycopg.connect(sys.argv[1]) as conn:
    run = run_invariants(conn, run_id="determinism-subprocess")
payload = json.dumps(
    [conflict.as_json() for conflict in run.conflicts],
    sort_keys=True,
    separators=(",", ":"),
)
print(hashlib.sha256(payload.encode()).hexdigest())
"""


def test_a_separate_process_at_a_different_hash_seed_agrees(invariant_run, ingested_dsn) -> None:
    """The strongest available form: two OS processes, each at Python's own randomized
    `PYTHONHASHSEED`.

    CLAUDE.md forbids `set` iteration order or insertion-dependent dict ordering on a
    graded path. Inside one process a hash-order dependency is *stable* and therefore
    invisible; only a second process at a different seed can see it. The digest covers
    the whole conflict payload -- refs, disagreeing fields and observed values -- not
    just the fingerprints.
    """
    import hashlib
    import json
    import subprocess
    import sys

    expected = hashlib.sha256(
        json.dumps(
            [conflict.as_json() for conflict in invariant_run.conflicts],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    digests = set()
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS_PROBE, ingested_dsn],
            capture_output=True,
            text=True,
            check=True,
            env={k: v for k, v in __import__("os").environ.items() if k != "PYTHONHASHSEED"},
        )
        digests.add(completed.stdout.strip())

    assert digests == {expected}
