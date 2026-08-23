"""What the materialization actually put in the four identity tables.

The interesting assertions here are the ones about rows that were **discarded**.
`entity_link_candidates` has to retain every match-key resolution the cascade
rejected, because `R-010` (C10, merge-collapsed record) is evaluated over exactly
those rows -- one contact whose `ext` key and `namedob` key reach two different
students. A materialization that persisted only accepted links would look
perfectly healthy here and delete a whole conflict class two tickets downstream,
so the test does not merely count discarded rows: it reconstructs C10's predicate
over the table and shows the count collapses to zero if the discarded rows are
excluded.
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from recon.er import LINK_METHODS
from recon.resolve import LINEAGE_PATHS, METHOD_ANCHOR, METHOD_MEMBER
from tests.er.dataset import Dataset

#: SS5.5 -- C10's committed minimum in the full profile.
C10_MINIMUM = 50


def _scalar(reader: Engine, sql: str, **params: object) -> int:
    with reader.connect() as conn:
        return int(conn.execute(text(sql), params).scalar_one())


def test_every_canonical_row_has_link_provenance(reader: Engine, dataset: Dataset) -> None:
    """KS008, asserted rather than assumed: no entity without an `entity_links` row."""
    orphans = _scalar(
        reader,
        "SELECT count(*) FROM entities e "
        "WHERE NOT EXISTS (SELECT 1 FROM entity_links el WHERE el.canonical_id = e.canonical_id)",
    )
    assert orphans == 0
    assert _scalar(reader, "SELECT count(*) FROM entities") == dataset.report.persons


def test_every_link_names_an_ingested_record(reader: Engine) -> None:
    """KS009: every link's `(source_id, source_key, generation)` is in landing."""
    dangling = _scalar(
        reader,
        "SELECT count(*) FROM entity_links el WHERE NOT EXISTS ("
        " SELECT 1 FROM raw_records rr WHERE rr.source_id = el.source_id"
        "   AND rr.natural_key = el.source_key AND rr.generation = el.generation)",
    )
    assert dangling == 0


def test_link_methods_are_the_committed_vocabulary(reader: Engine) -> None:
    """`method` is a cascade rule id, or one of the two structural values.

    `R-004` reads C4's method straight off this column, so a value outside the
    vocabulary is not cosmetic -- it is a rule reading something that means
    nothing.
    """
    allowed = set(LINK_METHODS) | {METHOD_ANCHOR, METHOD_MEMBER}
    with reader.connect() as conn:
        found = {
            row.method for row in conn.execute(text("SELECT DISTINCT method FROM entity_links"))
        }
    assert found <= allowed, f"unexpected entity_links.method values: {sorted(found - allowed)}"
    # The cascade rules that can appear on a one-row-per-source-ref table.
    assert {"L1", "L2", "L3"} & found, "no contact_student links were materialized"
    assert {"P1", "P2", "P3"} & found, "no payment_person links were materialized"
    assert "D2" in found, "no deal_person links were materialized"


def test_links_are_one_row_per_source_record(reader: Engine) -> None:
    """`uq_entity_links_source_generation` holds, and covers every landed record."""
    duplicates = _scalar(
        reader,
        "SELECT count(*) FROM (SELECT generation, source_id, source_key FROM entity_links"
        " GROUP BY 1,2,3 HAVING count(*) > 1) x",
    )
    assert duplicates == 0

    unlinked = _scalar(
        reader,
        "SELECT count(*) FROM raw_records rr WHERE rr.generation = 3 AND NOT EXISTS ("
        " SELECT 1 FROM entity_links el WHERE el.source_id = rr.source_id"
        "   AND el.source_key = rr.natural_key AND el.generation = rr.generation)",
    )
    assert unlinked == 0, (
        f"{unlinked} landed generation-3 record(s) belong to no canonical entity; "
        "the ref -> canonical index the query endpoint reads would 404 on them"
    )


def test_discarded_candidates_are_retained_and_load_bearing(reader: Engine) -> None:
    """R-010's population exists in `entity_link_candidates`, and only there.

    C10 is "one CRM contact whose `ext` candidate and `namedob` candidate resolve
    to two different students". The first query counts that population over the
    whole table; the second counts it over accepted rows only. The first must meet
    C10's committed minimum and the second must be zero -- which is what makes
    "retain the discarded rows" a requirement rather than a preference.
    """
    collapsed = """
        SELECT count(*) FROM (
            SELECT source_ref
              FROM entity_link_candidates
             WHERE generation = 3
               AND starts_with(source_ref, 'crm:contact:')
               AND key_class IN ('ext', 'namedob')
               {extra}
             GROUP BY source_ref
            HAVING count(DISTINCT resolved_ref) FILTER (WHERE key_class = 'ext') = 1
               AND count(DISTINCT resolved_ref) FILTER (WHERE key_class = 'namedob') = 1
               AND count(DISTINCT resolved_ref) = 2
        ) x
    """
    everything = _scalar(reader, collapsed.format(extra=""))
    accepted_only = _scalar(reader, collapsed.format(extra="AND accepted"))

    assert everything >= C10_MINIMUM, (
        f"only {everything} merge-collapsed contacts are visible in "
        f"entity_link_candidates; SS5.5 plants at least {C10_MINIMUM}"
    )
    assert accepted_only == 0, (
        "the C10 population is visible among accepted candidates, which means this "
        "test would pass even if the discarded rows had been dropped"
    )
    assert _scalar(reader, "SELECT count(*) FROM entity_link_candidates WHERE NOT accepted") > 0


def test_candidates_cover_every_key_class(reader: Engine, dataset: Dataset) -> None:
    """`key_class` is `match_keys`' committed vocabulary, all three of it."""
    with reader.connect() as conn:
        classes = {
            row.key_class
            for row in conn.execute(text("SELECT DISTINCT key_class FROM entity_link_candidates"))
        }
    assert classes == {"ext", "email", "namedob"}
    assert _scalar(reader, "SELECT count(*) FROM entity_link_candidates") == (
        dataset.report.candidates
    )


def test_lineage_covers_the_committed_paths(reader: Engine) -> None:
    """`field_lineage` names source-qualified paths, and every entity has some."""
    with reader.connect() as conn:
        paths = {
            row.field for row in conn.execute(text("SELECT DISTINCT field FROM field_lineage"))
        }
    unexpected = sorted(paths - set(LINEAGE_PATHS))
    assert not unexpected, f"unexpected lineage paths: {unexpected}"
    assert {"crm.contact.email", "appdb.student.grade", "appdb.enrollment.stage"} <= paths

    without = _scalar(
        reader,
        "SELECT count(*) FROM entities e WHERE NOT EXISTS ("
        " SELECT 1 FROM field_lineage fl WHERE fl.canonical_id = e.canonical_id)",
    )
    # The only entities with nothing to say are unattributed payments: no contact,
    # no student, no enrollment, no deal -- so no source-qualified path exists.
    unattributed = _scalar(
        reader,
        "SELECT count(*) FROM entities "
        "WHERE starts_with(current ->> 'anchor_ref', 'payments:payment:')",
    )
    assert without == unattributed, (
        f"{without} entities carry no lineage but only {unattributed} are unattributed payments"
    )


def test_lineage_values_are_canonically_serialized(reader: Engine) -> None:
    """`value_text` is `canon_value` output -- never a raw repr, never NULL-as-empty."""
    with reader.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT field, value_text FROM field_lineage "
                "WHERE field = 'appdb.student.grade' AND value_text <> '\\N' LIMIT 50"
            )
        ).fetchall()
    assert rows, "no student-grade lineage rows were written"
    for row in rows:
        assert row.value_text is not None
        assert row.value_text.strip() == row.value_text


def test_lineage_observed_ts_is_the_source_timestamp(reader: Engine) -> None:
    """`observed_ts` is when the *source* said it, not when we wrote it down."""
    mismatched = _scalar(
        reader,
        "SELECT count(*) FROM field_lineage fl JOIN stg_student s"
        " ON s.generation = fl.generation AND fl.source_ref = 'appdb:student:' || s.student_id"
        " WHERE starts_with(fl.field, 'appdb.student.')"
        "   AND fl.observed_ts <> COALESCE(s.updated_at, s.created_at)",
    )
    assert mismatched == 0


def test_declared_lineage_vocabulary_matches_what_is_written() -> None:
    """`LINEAGE_PATHS` is exactly the union of the four per-source column maps.

    The docstring in `recon.resolve` promises these do not drift; a path declared
    but never written would leave the endpoint's lineage quietly incomplete, and a
    path written but not declared would slip past
    `test_lineage_covers_the_committed_paths` above only because that test reads
    the same constant.
    """
    from recon.reference import COMPARED_FIELD_PATHS
    from recon.resolve import (
        _CONTACT_PATHS,
        _DEAL_PATHS,
        _ENROLLMENT_PATHS,
        _STUDENT_PATHS,
    )

    written = set(_STUDENT_PATHS) | set(_CONTACT_PATHS) | set(_ENROLLMENT_PATHS) | set(_DEAL_PATHS)
    assert written == set(LINEAGE_PATHS)
    # SS2.4's twelve comparison paths are all written, so R16's A -> B -> A scan can
    # see every field a conflict can ever name...
    assert set(COMPARED_FIELD_PATHS) <= written
    # ...plus the two survived paths that are not compared fields, so the endpoint
    # can name the provenance of every value in `survived`.
    assert {"crm.contact.email", "appdb.enrollment.program"} <= written
