"""``mirror-unchanged``: the read-only mirror is byte-identical after a run.

R1 and R13 make one promise together: Keystone mirrors three sources read-only,
and *"production/mirror data is unchanged by the run"*. Nothing in the adapter
Protocol can write to a source, and the three Postgres roles stop the mirror
being rewritten through the boundary -- but neither of those is a check that
the mirror **did not move**, and that is the claim the scorecard has to carry.

It is also a claim other parts of the schema now cite. Migration 0006's
provenance floor (``KS009``) is honest about its limit: ``recon_writer`` holds
INSERT on ``raw_records`` because ingestion is its job, so fabricating a
canonical entity costs three INSERTs rather than being impossible -- and the
third one lands in the landing table. The value of that is entirely conditional
on something reading the landing table and noticing. This is that reader.

What this module implements TODAY
----------------------------------
``mirror_digest(conn)`` -- a deterministic content hash of every landing and
staging table, per table and combined. It is a real, exercised function: the
schema tests take a digest, insert one ``raw_records`` row, take another, and
assert the digest moved. ``compare()`` turns a before/after pair into a
scorecard row and names the tables that changed.

What it CANNOT do until the reconciler exists
-----------------------------------------------
"Across a reconciler run" needs a reconciler. ``recon.reconciler`` is T-9 and is
not written. ``reconciler_entrypoint()`` therefore raises
:class:`~recon.suite.checks.NotYetImplemented`, and ``check_mirror_unchanged``
reports **FAIL** with that reason rather than hashing the mirror twice with
nothing in between and reporting PASS. Those two runs would be trivially equal
and the row would be a lie -- a green that proves the opposite of what it
claims. The check fails loudly until the run it is supposed to bracket exists.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import Connection, text

from recon.suite.checks import CheckResult, NotYetImplemented

__all__ = [
    "CHECK_NAME",
    "LANDING_TABLES",
    "MIRROR_TABLES",
    "STAGING_TABLES",
    "MirrorDigest",
    "check_mirror_unchanged",
    "compare",
    "mirror_digest",
    "reconciler_entrypoint",
]

#: The name this check keeps. T-14 registers the rest of the scorecard around
#: it; DESIGN's proposal-safety check reuses this same digest.
CHECK_NAME = "mirror-unchanged"

#: The read-only landing surface: what the adapters put there, and the record
#: of the runs that put it there.
LANDING_TABLES = ("ingest_runs", "raw_records")

#: The normalized mirror of what each source actually emitted.
STAGING_TABLES = (
    "stg_crm_contact",
    "stg_crm_deal",
    "stg_student",
    "stg_enrollment",
    "stg_payment",
)

#: Everything the reconciler must leave exactly as it found it.
MIRROR_TABLES = LANDING_TABLES + STAGING_TABLES

#: Session settings that make a row's text rendering independent of the
#: connecting client. Without them a digest taken from a session in
#: ``Europe/London`` and one taken from ``UTC`` would differ on every timestamp
#: column, and the check would report a mirror change that never happened.
_DETERMINISTIC_RENDERING = (
    "SET TimeZone TO 'UTC'",
    "SET DateStyle TO 'ISO, YMD'",
    "SET IntervalStyle TO 'iso_8601'",
    "SET extra_float_digits TO 3",
)

#: Hash the multiset of rows, not the physical order: ``ORDER BY`` on the row
#: hash itself is stable under VACUUM, under a re-INSERT of identical content,
#: and needs no primary key -- which matters because ``raw_records`` has no
#: unique natural key by design (duplicate keys within a generation are
#: legitimate input, contract C11).
_DIGEST_SQL = """
    SELECT count(*) AS row_count,
           coalesce(md5(string_agg(h, '' ORDER BY h)), '') AS digest
    FROM (SELECT md5(t::text) AS h FROM {table} t) s
"""


@dataclass(frozen=True)
class MirrorDigest:
    """Per-table row counts and content hashes for the whole mirror."""

    digests: dict[str, str]
    row_counts: dict[str, int]

    def combined(self) -> str:
        """One string identifying the whole mirror, tables in fixed order."""
        return "|".join(
            f"{table}:{self.row_counts[table]}:{self.digests[table]}" for table in MIRROR_TABLES
        )

    def changed_tables(self, other: MirrorDigest) -> tuple[str, ...]:
        """Tables whose content or row count differs between the two digests."""
        return tuple(
            table
            for table in MIRROR_TABLES
            if self.digests.get(table) != other.digests.get(table)
            or self.row_counts.get(table) != other.row_counts.get(table)
        )


def mirror_digest(conn: Connection, tables: Sequence[str] = MIRROR_TABLES) -> MirrorDigest:
    """Content-hash every mirror table over ``conn``.

    ``tables`` is validated against :data:`MIRROR_TABLES` rather than
    interpolated blindly: the names go into the SQL text because a table name
    cannot be a bind parameter, so the allowlist is what keeps that safe.
    """
    unknown = [table for table in tables if table not in MIRROR_TABLES]
    if unknown:
        raise ValueError(f"not mirror tables: {unknown}")

    for statement in _DETERMINISTIC_RENDERING:
        conn.execute(text(statement))

    digests: dict[str, str] = {}
    row_counts: dict[str, int] = {}
    for table in tables:
        row = conn.execute(text(_DIGEST_SQL.format(table=table))).one()
        digests[table] = row.digest
        row_counts[table] = row.row_count
    return MirrorDigest(digests=digests, row_counts=row_counts)


def compare(before: MirrorDigest, after: MirrorDigest) -> CheckResult:
    """Turn a before/after pair into the scorecard row.

    Names the tables that moved, and by how many rows, because "the mirror
    changed" is not actionable and "``raw_records`` gained 1 row" is.
    """
    changed = before.changed_tables(after)
    if not changed:
        total = sum(before.row_counts.values())
        return CheckResult.passed(
            CHECK_NAME,
            f"{len(MIRROR_TABLES)} landing/staging tables, {total} rows, byte-unchanged",
        )

    detail = ", ".join(
        f"{table} ({before.row_counts.get(table)} -> {after.row_counts.get(table)} rows)"
        for table in changed
    )
    return CheckResult.failed(CHECK_NAME, f"the run modified the read-only mirror: {detail}")


def reconciler_entrypoint() -> Callable[[], object]:
    """Return the callable that performs one reconciler run.

    Raises :class:`NotYetImplemented` while ``recon.reconciler`` does not exist
    (T-9). The alternative -- hashing the mirror twice with nothing in between
    -- would report PASS for a run that never happened, which is exactly the
    vacuous green this check exists to prevent.
    """
    try:
        from recon.reconciler import run_once  # type: ignore[attr-defined]
    except ImportError as exc:
        raise NotYetImplemented(
            "recon.reconciler does not exist yet (T-9), so there is no run to "
            "bracket. The mirror digest itself is implemented and exercised "
            "(recon.suite.mirror.mirror_digest); what is missing is the run in "
            "the middle. This check FAILS rather than hashing an unchanged "
            "database twice and reporting a pass nobody earned."
        ) from exc
    return run_once


def check_mirror_unchanged(connect: Callable[[], Connection] | None = None) -> CheckResult:
    """Hash the mirror, run the reconciler, hash it again, assert equality.

    ``connect`` is injectable so the schema tests can drive this over the same
    role-scoped connections everything else uses; by default it opens the
    ordinary application engine.

    The reconciler entrypoint is resolved **before** the first connection is
    opened, deliberately. The missing piece is ``recon.reconciler`` (T-9), and
    that is true whether or not a database is reachable; resolving it second
    meant a run with no ``DATABASE_URL`` reported ``DatabaseNotConfigured``
    instead -- a scorecard row that reads like an infrastructure problem for a
    check whose actual blocker is unwritten code. The row must name the real
    reason, and now does so from any environment.
    """
    run = reconciler_entrypoint()

    if connect is None:
        from recon.db import get_engine

        connect = get_engine().connect

    with connect() as conn:
        before = mirror_digest(conn)

    run()

    with connect() as conn:
        after = mirror_digest(conn)
    return compare(before, after)
