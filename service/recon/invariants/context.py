"""The run context the SQL rules read: the generation-3 world, resolved.

SS3's pipeline is `stg_* + entities --rules/*.sql--> invariant_results`. The rules
therefore need three things the `stg_*` tables do not carry on their own:

* the **cascade output** (SS4) -- who resolved to whom, by which `method`, and every
  discarded `match_keys` candidate `R-010` is evaluated over;
* the **person view** (SS4.1, SS5.2) -- identity refs, and SS4.6's survived
  per-source record;
* the **committed reference data** (SS2.2, SS2.3) -- the fee schedule,
  `PAID_IMPLYING_STAGES`, `LIFECYCLE_TO_FUNNEL`, `COMPARED_FIELDS` and
  `SENSITIVE_FIELDS`.

All three are materialized here into session-scoped `TEMP` tables and **imported,
never re-implemented**: the cascade is `recon.er.resolve` (the same call the seed
generator makes in pass 2, `G31`) and every constant comes off `recon.reference`.
A rule that needed one of them to be re-derived in SQL would be a second
implementation of a shared module, which is exactly the generator/detector drift
SS0 exists to prevent.

Temp tables also give the runner **database isolation for free**: two runs on one
database cannot see each other's cascade output, because a temp table lives in the
connection's own `pg_temp` schema.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from psycopg import Connection

from recon.er import Resolution, Snapshot, resolve
from recon.reference import (
    C11_WINDOW_SECONDS,
    COMPARED_FIELDS,
    ENROLLMENT_GRADE_FLOOR,
    FEE_SCHEDULE,
    GRADE_ORDER,
    LIFECYCLE_TO_FUNNEL,
    PAID_IMPLYING_STAGES,
    SENSITIVE_FIELDS,
    household_members_appdb,
    make_ref,
    parse_ref,
)

from .rules import SCOPE_ENTITY_TYPES

__all__ = [
    "ANALYZE_LOCK_TIMEOUT_MS",
    "CURRENT_GENERATION",
    "ER_TABLES",
    "REFERENCE_TABLES",
    "STAGING_TABLES",
    "InvariantContext",
    "analyze_staging",
    "build_context",
    "load_snapshot",
    "read_completeness",
]

#: SS7: current state is generation 3, globally. Invariants read it and nothing else.
CURRENT_GENERATION: int = 3

#: SS3's five `stg_*` tables -- the generation-stamped, normalized staging tables the
#: rules scan. Derived from SS5.5's rule-scope table (`rules.SCOPE_ENTITY_TYPES`), so a
#: sixth staging table is added in one place rather than in a literal per consumer.
STAGING_TABLES: tuple[str, ...] = tuple(sorted(SCOPE_ENTITY_TYPES))

ER_TABLES: tuple[str, ...] = (
    "er_person",
    "er_person_ref",
    "er_contact_student",
    "er_payment_person",
    "er_payment_enrollment",
    "er_deal_person",
    "er_candidate",
    "er_household",
)

REFERENCE_TABLES: tuple[str, ...] = (
    "ref_fee_schedule",
    "ref_paid_implying_stage",
    "ref_lifecycle_funnel",
    "ref_compared_field",
    "ref_sensitive_field",
    "ref_constant",
)

_REF_CLASS_OF: tuple[tuple[str, str], ...] = (
    ("appdb:student:", "student"),
    ("appdb:enrollment:", "enrollment"),
    ("crm:contact:", "contact"),
    ("crm:deal:", "deal"),
    ("payments:payment:", "payment"),
)

_DDL = """
CREATE TEMP TABLE er_person (
    person_key               text PRIMARY KEY,
    anchor_ref               text NOT NULL,
    student_ref              text,
    contact_count            integer NOT NULL,
    payment_count            integer NOT NULL,
    enrollment_count         integer NOT NULL,
    deal_count               integer NOT NULL,
    survived_contact_ref     text,
    survived_enrollment_ref  text,
    survived_deal_ref        text,
    identity_refs            jsonb
);
CREATE TEMP TABLE er_person_ref (
    person_key   text NOT NULL,
    ref          text NOT NULL,
    ref_class    text NOT NULL,
    is_identity  boolean NOT NULL
);
CREATE TEMP TABLE er_contact_student (
    contact_ref  text PRIMARY KEY,
    student_ref  text NOT NULL,
    person_key   text NOT NULL,
    method       text NOT NULL
);
CREATE TEMP TABLE er_payment_person (
    payment_ref  text PRIMARY KEY,
    student_ref  text NOT NULL,
    person_key   text NOT NULL,
    method       text NOT NULL
);
CREATE TEMP TABLE er_payment_enrollment (
    payment_ref     text PRIMARY KEY,
    enrollment_ref  text NOT NULL,
    method          text NOT NULL
);
CREATE TEMP TABLE er_deal_person (
    deal_ref    text NOT NULL,
    deal_id     text NOT NULL,
    person_key  text NOT NULL
);
CREATE TEMP TABLE er_candidate (
    source_ref    text NOT NULL,
    key_class     text NOT NULL,
    resolved_ref  text NOT NULL,
    -- `R-010` is the one rule whose entire input is `er_candidate` rather than a
    -- `stg_*` scan, so without this column it is the one rule that does not state
    -- its own generation scope -- correct today only because `build_context` fills
    -- the table from `resolve(load_snapshot(conn, 3))`. A caller convention is not
    -- the same guarantee as a predicate; SS7 makes generation 3 the current state
    -- and the rule now says so itself.
    generation    smallint NOT NULL
);
CREATE TEMP TABLE er_household (
    household_key  text NOT NULL,
    student_ref    text NOT NULL
);
CREATE TEMP TABLE ref_fee_schedule (
    program_norm  text NOT NULL,
    payment_type  text NOT NULL,
    amount_cents  bigint NOT NULL
);
CREATE TEMP TABLE ref_paid_implying_stage (stage_funnel text PRIMARY KEY);
CREATE TEMP TABLE ref_lifecycle_funnel (
    lifecycle_norm  text PRIMARY KEY,
    funnel          text
);
CREATE TEMP TABLE ref_compared_field (
    logical           text PRIMARY KEY,
    left_path         text NOT NULL,
    right_path        text NOT NULL,
    wholly_sensitive  boolean NOT NULL,
    unmapped_reason   text NOT NULL
);
CREATE TEMP TABLE ref_sensitive_field (path text PRIMARY KEY);
CREATE TEMP TABLE ref_constant (name text PRIMARY KEY, value bigint NOT NULL);
"""

#: SS4.6 survivorship, materialized once instead of restated in every rule that
#: needs it: "the survived per-source value is taken from the record with the
#: lexicographically smallest source ref (byte order)". `COLLATE "C"` makes "byte
#: order" literal rather than a property of whatever locale the cluster was
#: initialised with -- an ICU or en_US collation ignores `-` at the primary level,
#: which would silently pick a different contact out of a C3 duplicate pair.
_SURVIVORSHIP = """
UPDATE er_person AS p
   SET survived_contact_ref = s.contact_ref,
       survived_enrollment_ref = s.enrollment_ref,
       survived_deal_ref = s.deal_ref,
       identity_refs = s.identity_refs
  FROM (
        SELECT person_key,
               min(ref COLLATE "C") FILTER (WHERE ref_class = 'contact')    AS contact_ref,
               min(ref COLLATE "C") FILTER (WHERE ref_class = 'enrollment') AS enrollment_ref,
               min(ref COLLATE "C") FILTER (WHERE ref_class = 'deal')       AS deal_ref,
               COALESCE(
                   jsonb_agg(ref ORDER BY ref COLLATE "C") FILTER (WHERE is_identity),
                   '[]'::jsonb
               ) AS identity_refs
          FROM er_person_ref
         GROUP BY person_key
       ) AS s
 WHERE s.person_key = p.person_key
"""

_INDEXES = (
    "CREATE INDEX ON er_person_ref (person_key)",
    "CREATE INDEX ON er_person_ref (ref)",
    "CREATE INDEX ON er_person (student_ref)",
    "CREATE INDEX ON er_contact_student (student_ref)",
    "CREATE INDEX ON er_payment_person (student_ref)",
    "CREATE INDEX ON er_payment_person (person_key)",
    "CREATE INDEX ON er_payment_enrollment (enrollment_ref)",
    "CREATE INDEX ON er_deal_person (deal_ref)",
    "CREATE INDEX ON er_deal_person (deal_id)",
    "CREATE INDEX ON er_deal_person (person_key)",
    "CREATE INDEX ON er_candidate (generation, source_ref, key_class)",
    "CREATE INDEX ON er_household (household_key)",
    "CREATE INDEX ON er_household (student_ref)",
)


@dataclass(frozen=True, slots=True)
class InvariantContext:
    """Everything one invariant run is evaluated against."""

    generation: int
    snapshot: Snapshot
    resolution: Resolution
    #: SS5.3 -- `(source_id, entity_type)` whose generation-N load is not complete.
    incomplete: tuple[tuple[str, str], ...]

    @property
    def degraded(self) -> bool:
        """SS5.3: any incomplete generation-3 load degrades the whole run."""
        return bool(self.incomplete)


# ======================================================================================
# reading the generation-3 world out of `stg_*`
# ======================================================================================

_CONTACT_SQL = """
SELECT crm_id, email, first_name, last_name, external_id, dob
  FROM stg_crm_contact WHERE generation = %(generation)s ORDER BY crm_id COLLATE "C"
"""
_DEAL_SQL = """
SELECT deal_id, associated_contact_ids
  FROM stg_crm_deal WHERE generation = %(generation)s ORDER BY deal_id COLLATE "C"
"""
_STUDENT_SQL = """
SELECT student_id, first_name, last_name, dob, guardian_email, guardian2_email
  FROM stg_student WHERE generation = %(generation)s ORDER BY student_id COLLATE "C"
"""
_ENROLLMENT_SQL = """
SELECT enrollment_id, student_id, program
  FROM stg_enrollment WHERE generation = %(generation)s ORDER BY enrollment_id COLLATE "C"
"""
_PAYMENT_SQL = """
SELECT payment_id, external_ref, payer_email, payment_metadata
  FROM stg_payment WHERE generation = %(generation)s ORDER BY payment_id COLLATE "C"
"""


def load_snapshot(conn: Connection, generation: int = CURRENT_GENERATION) -> Snapshot:
    """Read the generation-N snapshot out of `stg_*` in the shape `recon.er` expects.

    Only the fields the SS4 cascade and `match_keys` consume are selected, and they
    are the **raw** columns: `recon.er` calls the committed `norm_*` itself, so
    handing it a pre-normalized value would put a second normalizer on the path.
    """
    params = {"generation": generation}
    with conn.cursor() as cur:
        cur.execute(_CONTACT_SQL, params)
        contacts = [
            {
                "crm_id": row[0],
                "email": row[1],
                "first_name": row[2],
                "last_name": row[3],
                "external_id": row[4],
                "dob": row[5],
            }
            for row in cur.fetchall()
        ]
        cur.execute(_DEAL_SQL, params)
        deals = [{"deal_id": row[0], "associated_contact_ids": row[1] or []} for row in cur]
        cur.execute(_STUDENT_SQL, params)
        students = [
            {
                "id": row[0],
                "first_name": row[1],
                "last_name": row[2],
                "dob": row[3],
                "guardian_email": row[4],
                "guardian2_email": row[5],
            }
            for row in cur.fetchall()
        ]
        cur.execute(_ENROLLMENT_SQL, params)
        enrollments = [{"id": row[0], "student_id": row[1], "program": row[2]} for row in cur]
        cur.execute(_PAYMENT_SQL, params)
        payments = [
            {
                "payment_id": row[0],
                "external_ref": row[1],
                "payer_email": row[2],
                "metadata": row[3],
            }
            for row in cur.fetchall()
        ]
    return Snapshot(
        generation=generation,
        contacts=contacts,
        deals=deals,
        students=students,
        enrollments=enrollments,
        payments=payments,
    )


def read_completeness(
    conn: Connection, generation: int = CURRENT_GENERATION
) -> tuple[tuple[str, str], ...]:
    """SS5.3: the `(source_id, entity_type)` loads that are not complete at generation N.

    A **missing** ledger row counts as incomplete. `source_generations` is the whole
    reason ingest tracks completeness, and "no row" is precisely the state of a
    source that never finished -- reading absence as success would hand every
    absence rule a truncated snapshot.

    The expected pairs are read off `recon.adapters.SOURCE_ENTITY_TYPES`, the
    manifest the adapters themselves are built from, rather than a literal here. A
    literal is a gate that silently stops covering a sixth ingested entity type: the
    new pair would be outside the set, so its absence from `source_generations` could
    never degrade the run, and the failure mode is a clean green on a truncated load.
    """
    from recon.adapters import SOURCE_ENTITY_TYPES

    expected = {
        (source_id, entity_type)
        for source_id, entity_types in SOURCE_ENTITY_TYPES.items()
        for entity_type in entity_types
    }
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_id, entity_type, complete FROM source_generations "
            "WHERE generation = %(generation)s",
            {"generation": generation},
        )
        rows = {(row[0], row[1]): bool(row[2]) for row in cur.fetchall()}
    incomplete = {pair for pair in expected if not rows.get(pair, False)}
    return tuple(sorted(incomplete))


# ======================================================================================
# materialization
# ======================================================================================


def _ref_class(ref: str) -> str:
    for prefix, name in _REF_CLASS_OF:
        if ref.startswith(prefix):
            return name
    raise ValueError(f"{ref!r} is not a source ref (SS4.1)")


def _copy(conn: Connection, table: str, columns: Sequence[str], rows: Any) -> None:
    statement = f"COPY {table} ({', '.join(columns)}) FROM STDIN"
    with conn.cursor() as cur, cur.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)


#: How long `analyze_staging` waits for a table lock before giving up on that table.
#: Short on purpose -- see :func:`analyze_staging` for why blocking is the worse
#: failure and why the wait is bounded rather than absent.
ANALYZE_LOCK_TIMEOUT_MS: int = 250

_UNANALYZED_SQL = """
SELECT c.relname
  FROM pg_class AS c
  JOIN pg_namespace AS n ON n.oid = c.relnamespace
 WHERE c.relname = ANY(%(tables)s)
   AND n.nspname = ANY(current_schemas(false))
   AND c.reltuples < 0
 ORDER BY c.relname COLLATE "C"
"""


def analyze_staging(conn: Connection) -> tuple[str, ...]:
    """ANALYZE any `stg_*` table that has never been analyzed. Returns what it did.

    **Why the invariant pass cares.** The five staging tables the rules scan are
    freshly `COPY`ed by ingest and carry NO statistics of their own. The planner then
    estimates `stg_payment WHERE generation = 3` at ~62 rows against an actual 18,000
    and picks a nested loop for `R-013`'s correlated sibling scan -- ~4.7s instead of
    ~1.7s, against a 30s budget. Worse, the number is not REPRODUCIBLE: autovacuum
    races the run, so on a fresh ingest `last_analyze` is NULL for all five while
    `last_autoanalyze` is set on however many autovacuum happened to reach first, and
    byte-identical input measures differently run to run. A perf gate whose value
    depends on a background worker is not a gate.

    **Why it is conditional and lock-bounded.** `ANALYZE` takes a
    `SHARE UPDATE EXCLUSIVE` lock and, inside a transaction block, holds it until that
    transaction ends. The caller's transaction is not this function's to control -- a
    long-lived connection that ran one invariant pass and has not committed would
    otherwise pin all five tables and BLOCK every other connection's
    :func:`build_context` indefinitely. That is a far worse failure than a slow plan:
    a hang has no error message. So:

    * tables that already have statistics (`pg_class.reltuples >= 0`) are skipped
      outright, which is the steady state and takes no lock at all;
    * each remaining table is analyzed inside its own savepoint under a
      `lock_timeout`, and a table someone else is holding is left alone rather than
      waited on. A skipped table plans on whatever statistics exist -- the behaviour
      before this function existed -- and the run still completes.

    `current_schemas(false)` scopes the lookup to the search path, so a `stg_*` table
    in another schema (or a same-named TEMP table) cannot be mistaken for the one the
    rules read.
    """
    from psycopg import errors

    with conn.cursor() as cur:
        cur.execute(_UNANALYZED_SQL, {"tables": list(STAGING_TABLES)})
        pending = [row[0] for row in cur.fetchall()]

    analyzed: list[str] = []
    for table in pending:
        try:
            # A savepoint, so a lock timeout aborts only this ANALYZE and not the
            # caller's transaction. `SET LOCAL` is restored explicitly on the success
            # path and rolled back with the savepoint on the failure path, so the
            # caller's `lock_timeout` is the same on the way out as on the way in.
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(f"SET LOCAL lock_timeout = '{ANALYZE_LOCK_TIMEOUT_MS}ms'")
                cur.execute(f"ANALYZE {table}")
                cur.execute("SET LOCAL lock_timeout = DEFAULT")
        except errors.LockNotAvailable:
            continue
        analyzed.append(table)
    return tuple(analyzed)


def materialize(conn: Connection, resolution: Resolution, snapshot: Snapshot) -> None:
    """Create and fill the `er_*` and `ref_*` temp tables the rules read."""
    with conn.cursor() as cur:
        cur.execute(_DDL)

    _copy(
        conn,
        "er_person",
        (
            "person_key",
            "anchor_ref",
            "student_ref",
            "contact_count",
            "payment_count",
            "enrollment_count",
            "deal_count",
        ),
        (
            (
                person.person_key,
                person.anchor_ref,
                person.student_ref,
                len(person.contact_refs),
                len(person.payment_refs),
                len(person.enrollment_refs),
                len(person.deal_refs),
            )
            for person in resolution.persons
        ),
    )
    _copy(
        conn,
        "er_person_ref",
        ("person_key", "ref", "ref_class", "is_identity"),
        (
            (person.person_key, ref, _ref_class(ref), ref in set(person.identity_refs))
            for person in resolution.persons
            for ref in person.refs
        ),
    )
    _copy(
        conn,
        "er_contact_student",
        ("contact_ref", "student_ref", "person_key", "method"),
        (
            (link.source_ref, link.resolved_ref, link.canonical_id, link.method)
            for link in resolution.links
            if link.link_class == "contact_student"
        ),
    )
    _copy(
        conn,
        "er_payment_person",
        ("payment_ref", "student_ref", "person_key", "method"),
        (
            (link.source_ref, link.resolved_ref, link.canonical_id, link.method)
            for link in resolution.links
            if link.link_class == "payment_person"
        ),
    )
    _copy(
        conn,
        "er_payment_enrollment",
        ("payment_ref", "enrollment_ref", "method"),
        (
            (link.source_ref, link.resolved_ref, link.method)
            for link in resolution.links
            if link.link_class == "payment_enrollment"
        ),
    )
    _copy(
        conn,
        "er_deal_person",
        ("deal_ref", "deal_id", "person_key"),
        (
            (deal_ref, parse_ref(deal_ref)[2], key)
            for deal_ref, keys in sorted(resolution.deal_persons.items())
            for key in keys
        ),
    )
    _copy(
        conn,
        "er_candidate",
        ("source_ref", "key_class", "resolved_ref", "generation"),
        [
            (*row, resolution.generation)
            for row in sorted(
                {
                    (candidate.source_ref, candidate.key_class, candidate.resolved_ref)
                    for candidate in resolution.candidates
                }
            )
        ],
    )
    households = household_members_appdb(snapshot.students)
    _copy(
        conn,
        "er_household",
        ("household_key", "student_ref"),
        (
            (key, make_ref("appdb", "student", member["id"]))
            for key, members in sorted(households.items())
            for member in members
        ),
    )

    _copy(
        conn,
        "ref_fee_schedule",
        ("program_norm", "payment_type", "amount_cents"),
        sorted(
            (program, payment_type, cents)
            for (program, payment_type), cents in sorted(FEE_SCHEDULE.items())
        ),
    )
    _copy(
        conn,
        "ref_paid_implying_stage",
        ("stage_funnel",),
        ((stage,) for stage in sorted(PAID_IMPLYING_STAGES)),
    )
    _copy(
        conn,
        "ref_lifecycle_funnel",
        ("lifecycle_norm", "funnel"),
        ((value, LIFECYCLE_TO_FUNNEL[value]) for value in sorted(LIFECYCLE_TO_FUNNEL)),
    )
    _copy(
        conn,
        "ref_compared_field",
        ("logical", "left_path", "right_path", "wholly_sensitive", "unmapped_reason"),
        (
            (row.logical, row.left_path, row.right_path, row.wholly_sensitive, row.unmapped_reason)
            for row in COMPARED_FIELDS
        ),
    )
    _copy(
        conn,
        "ref_sensitive_field",
        ("path",),
        ((path,) for path in sorted(SENSITIVE_FIELDS)),
    )
    _copy(
        conn,
        "ref_constant",
        ("name", "value"),
        (
            ("c11_window_seconds", C11_WINDOW_SECONDS),
            ("enrollment_grade_floor_ord", GRADE_ORDER[ENROLLMENT_GRADE_FLOOR]),
        ),
    )

    analyze_staging(conn)

    with conn.cursor() as cur:
        cur.execute(_SURVIVORSHIP)
        for statement in _INDEXES:
            cur.execute(statement)
        cur.execute("ANALYZE er_person")
        cur.execute("ANALYZE er_person_ref")
        cur.execute("ANALYZE er_payment_person")
        cur.execute("ANALYZE er_payment_enrollment")
        cur.execute("ANALYZE er_candidate")
        cur.execute("ANALYZE er_household")
        cur.execute("ANALYZE er_deal_person")


def build_context(conn: Connection, generation: int = CURRENT_GENERATION) -> InvariantContext:
    """Load generation N, run the SS4 cascade over it, and materialize both for SQL."""
    snapshot = load_snapshot(conn, generation)
    resolution = resolve(snapshot)
    materialize(conn, resolution, snapshot)
    return InvariantContext(
        generation=generation,
        snapshot=snapshot,
        resolution=resolution,
        incomplete=read_completeness(conn, generation),
    )
