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

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from recon.er import LINK_METHODS
from recon.resolve import LINEAGE_PATH_MAPS, LINEAGE_PATHS, METHOD_ANCHOR, METHOD_MEMBER
from tests.er.dataset import FIXTURES, Dataset

#: SS5.5 -- C10's committed minimum in the full profile.
C10_MINIMUM = 50

#: The generation the fixture tree is enumerated from -- SS7's current state, the
#: one the identity layer is built from.
FIXTURE_GENERATION = "gen3"


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
    assert {"payments.payment.payer_email", "payments.payment.amount_cents"} <= paths

    without = _scalar(
        reader,
        "SELECT count(*) FROM entities e WHERE NOT EXISTS ("
        " SELECT 1 FROM field_lineage fl WHERE fl.canonical_id = e.canonical_id)",
    )
    # R1: "every record carries source id, ingest timestamp, and field-level
    # lineage" -- so the number of entities with nothing to say is ZERO.
    #
    # This assertion used to read `without == unattributed`, where `unattributed`
    # counted the entities whose only record is a payment. That was the defect
    # written down as the contract: it *required* an entity backed by a payment to
    # carry no lineage, because payments had no path map. Closing that gap is what
    # makes the honest number 0, and 0 is strictly stronger than the count it
    # replaces -- the old form was satisfied by any number of lineage-less entities
    # so long as the two counts happened to match.
    assert without == 0, f"{without} entities carry no field-level lineage at all (R1)"

    # ...and the entities that had none before are the ones to name explicitly.
    unattributed = _scalar(
        reader,
        "SELECT count(*) FROM entities "
        "WHERE starts_with(current ->> 'anchor_ref', 'payments:payment:')",
    )
    assert unattributed > 0, "the dataset plants no unattributed payments (C2) to check"
    payment_only_without = _scalar(
        reader,
        "SELECT count(*) FROM entities e"
        " WHERE starts_with(e.current ->> 'anchor_ref', 'payments:payment:')"
        "   AND NOT EXISTS (SELECT 1 FROM field_lineage fl"
        "                    WHERE fl.canonical_id = e.canonical_id"
        "                      AND fl.source_id = 'payments')",
    )
    assert payment_only_without == 0, (
        f"{payment_only_without} of {unattributed} unattributed-payment entities carry no "
        "lineage from the payments source"
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


def test_every_payment_record_carries_its_own_lineage(reader: Engine) -> None:
    """R1 over the third source: no payment landed without field-level lineage.

    Counted against `stg_payment` rather than against a constant, so the assertion
    is "every record the payments source landed" and not "every record the writer
    felt like describing". Both halves matter: a payment with no lineage row at all
    is the gap this closed, and a payment with *some* of its paths would be a map
    that silently stopped covering a field.
    """
    orphaned = _scalar(
        reader,
        "SELECT count(*) FROM stg_payment p WHERE p.generation = 3 AND NOT EXISTS ("
        " SELECT 1 FROM field_lineage fl WHERE fl.generation = p.generation"
        "   AND fl.source_ref = 'payments:payment:' || p.payment_id)",
    )
    assert orphaned == 0, f"{orphaned} landed payment(s) carry no field-level lineage (R1)"

    expected = len(LINEAGE_PATH_MAPS[("payments", "payment")])
    partial = _scalar(
        reader,
        "SELECT count(*) FROM ("
        " SELECT source_ref FROM field_lineage"
        "  WHERE source_id = 'payments' AND generation = 3"
        "  GROUP BY source_ref HAVING count(DISTINCT field) <> :expected) x",
        expected=expected,
    )
    assert partial == 0, f"{partial} payment(s) carry fewer than {expected} lineage paths"


def test_an_entity_that_owns_a_payment_names_the_payments_source(reader: Engine) -> None:
    """The endpoint-visible fact: a person with a payment has `payments` lineage.

    The audited symptom was an entity whose lineage `source_id` values were only
    `{appdb, crm}` even though its view listed a payment. This asserts the join the
    reviewer actually makes -- view says paid, lineage says who said so -- over
    every such entity rather than over one hand-picked row.
    """
    silent = _scalar(
        reader,
        "SELECT count(*) FROM entities e"
        " WHERE jsonb_array_length(e.current -> 'payments') > 0"
        "   AND NOT EXISTS (SELECT 1 FROM field_lineage fl"
        "                    WHERE fl.canonical_id = e.canonical_id"
        "                      AND fl.source_id = 'payments')",
    )
    assert silent == 0, (
        f"{silent} entities list a payment in their canonical view but no payments "
        "lineage row explains it"
    )

    with reader.connect() as conn:
        sources = {
            row.source_id
            for row in conn.execute(
                text(
                    "SELECT DISTINCT fl.source_id FROM field_lineage fl"
                    " WHERE fl.canonical_id = ("
                    "  SELECT e.canonical_id FROM entities e"
                    "   WHERE jsonb_array_length(e.current -> 'payments') > 0"
                    "   ORDER BY e.canonical_id LIMIT 1)"
                )
            )
        }
    assert "payments" in sources, f"a payment-owning entity's lineage names only {sorted(sources)}"


def test_payment_lineage_observed_ts_is_the_payment_moment(reader: Engine) -> None:
    """`observed_ts` on a payment row is the payment's own `occurred_at`.

    `stg_payment` has no `updated_at` (migration 0001 gave it `occurred_at`), so
    this is the payments-side twin of the student check above: the source's moment,
    never the moment the pipeline wrote the row down.
    """
    mismatched = _scalar(
        reader,
        "SELECT count(*) FROM field_lineage fl JOIN stg_payment p"
        " ON p.generation = fl.generation AND fl.source_ref = 'payments:payment:' || p.payment_id"
        " WHERE fl.source_id = 'payments' AND p.occurred_at IS NOT NULL"
        "   AND fl.observed_ts <> p.occurred_at",
    )
    assert mismatched == 0


# ======================================================================================
# R1's coverage guard, anchored to the fixtures rather than to the declaration
# ======================================================================================
#
# The guard this replaces asserted `union(the four path maps) == set(LINEAGE_PATHS)`
# while `LINEAGE_PATHS` was *defined* as a union of the same declarations. It was
# green by construction: an entire source with no path map -- which is what payments
# was -- satisfied it perfectly, and so did the DB-backed test above, because that
# one subtracts the same constant. R1 (`docs/SPEC.md`) says "every record carries
# source id, ingest timestamp, and **field-level lineage**", and payments is one of
# the three mandated sources, so "the declaration agrees with itself" was never the
# question worth asking.
#
# What is asked instead: the fixture tree on disk is the source schema. Every key of
# every gen-3 record is enumerated from `fixtures/<source>/gen3/<record>.jsonl` and
# must land in exactly one of three classifications below. A source that no map
# declares fails. A field a source starts emitting fails until someone classifies it.

#: Fields that MUST carry a declared lineage path. Restated from the Appendix-A
#: schemas -- deliberately NOT read from `recon.resolve`, because a guard that reads
#: the declaration it is checking cannot fail.
REQUIRED_FIELDS: dict[tuple[str, str], frozenset[str]] = {
    ("appdb", "student"): frozenset({"first_name", "last_name", "dob", "grade", "status"}),
    ("appdb", "enrollment"): frozenset({"program", "stage"}),
    ("crm", "contact"): frozenset(
        {"first_name", "last_name", "dob", "grade", "lifecycle_stage", "email"}
    ),
    ("crm", "deal"): frozenset({"stage"}),
    # R1 + SS1.3: the payment record's reportable fields, all eight of them.
    ("payments", "payment"): frozenset(
        {
            "payer_email",
            "payer_name",
            "amount_cents",
            "currency",
            "type",
            "status",
            "occurred_at",
            "external_ref",
        }
    ),
}

#: Fields that carry no lineage path because they are not values a source *asserts
#: about a person*: the record's own identity, a foreign key, the envelope
#: timestamps every row's `observed_ts` already carries, or a nested object
#: `canon_value` (SS2.5) has no case for. Each entry states its reason, so removing
#: one is a decision someone has to write down.
STRUCTURAL_FIELDS: dict[tuple[str, str], dict[str, str]] = {
    ("appdb", "student"): {
        "id": "the record's identity; carried by field_lineage.source_ref",
        "household_id": "join key; the household is carried on the view as household_key",
        "created_at": "envelope timestamp; carried by field_lineage.observed_ts",
        "updated_at": "envelope timestamp; carried by field_lineage.observed_ts",
    },
    ("appdb", "enrollment"): {
        "id": "the record's identity; carried by field_lineage.source_ref",
        "student_id": "join key (SS1.4); materialized as the member entity_links row",
        "crm_deal_id": "join key; materialized as the D2 entity_links row",
        "created_at": "envelope timestamp; carried by field_lineage.observed_ts",
        "updated_at": "envelope timestamp; carried by field_lineage.observed_ts",
    },
    ("crm", "contact"): {
        "crm_id": "the record's identity; carried by field_lineage.source_ref",
        "external_id": "join key; its resolution is entity_link_candidates.key_class='ext'",
        "created_at": "envelope timestamp; carried by field_lineage.observed_ts",
        "updated_at": "envelope timestamp; carried by field_lineage.observed_ts",
    },
    ("crm", "deal"): {
        "deal_id": "the record's identity; carried by field_lineage.source_ref",
        "associated_contact_ids": "membership (SS4.5); materialized as entity_links rows",
        "created_at": "envelope timestamp; carried by field_lineage.observed_ts",
        "updated_at": "envelope timestamp; carried by field_lineage.observed_ts",
    },
    ("payments", "payment"): {
        "payment_id": "the record's identity; carried by field_lineage.source_ref",
        "metadata": "a nested object; canon_value (SS2.5) has no dict case",
        "created_at": "envelope timestamp; carried by field_lineage.observed_ts",
        "updated_at": "envelope timestamp; carried by field_lineage.observed_ts",
    },
}

#: The remaining gap, PUBLISHED rather than implied: real value fields that no path
#: map declares today. Listing them is the point -- the count is a number a reviewer
#: can read, and covering one means deleting a line here, which cannot happen by
#: accident. Nothing in this table may also be declared (asserted below), so it can
#: never become a place to hide a field that is in fact covered.
UNCOVERED_FIELDS: dict[tuple[str, str], frozenset[str]] = {
    ("appdb", "student"): frozenset(
        {
            "guardian_email",
            "guardian2_email",
            "student_number",
            "enrollment_year",
            "communication_opt_out",
        }
    ),
    ("appdb", "enrollment"): frozenset({"billing_owner_email", "deposit_paid_at"}),
    ("crm", "contact"): frozenset({"marketing_consent", "state"}),
    ("crm", "deal"): frozenset({"amount", "name", "pipeline"}),
    ("payments", "payment"): frozenset({"refunded_at"}),
}


def _require_fixture_tree() -> Path:
    """The committed fixture tree, or a loud stop.

    Skipped only when the tree is genuinely absent (a fresh clone: `fixtures/` is
    gitignored). `KEYSTONE_REQUIRE_DB` -- which every gate run sets -- turns that
    skip into a failure, because a coverage guard that silently did not run is the
    same green as a coverage guard that passed.
    """
    if (FIXTURES / "manifest.json").is_file():
        return FIXTURES
    message = (
        f"no committed fixture tree at {FIXTURES}: run `make seed` (or "
        "`uv run python -m recon.seed --profile full`) before the T-5 suites."
    )
    if os.environ.get("KEYSTONE_REQUIRE_DB", "").strip().lower() not in {
        "",
        "0",
        "false",
        "no",
        "off",
    }:
        pytest.fail(message)
    pytest.skip(message)


def _fixture_fields() -> dict[tuple[str, str], set[str]]:
    """`(source_id, record_class) -> every key any gen-3 record of it carries`.

    Read from the JSONL the adapters read. Every line, not a sample: a key that
    appears on one record in forty thousand is still a field the source emits.
    """
    root = _require_fixture_tree()
    fields: dict[tuple[str, str], set[str]] = {}
    for path in sorted(root.glob(f"*/{FIXTURE_GENERATION}/*.jsonl")):
        key = (path.parent.parent.name, path.stem)
        seen = fields.setdefault(key, set())
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    seen.update(json.loads(line))
    assert fields, f"no {FIXTURE_GENERATION} fixture files under {root}"
    return fields


def test_every_source_field_is_classified() -> None:
    """Every fixture key is required, structural, or a published gap -- no fourth bin.

    This is what makes the next test's `REQUIRED_FIELDS` table honest. Without it,
    a source that started emitting a new field, or a whole new source, would simply
    not appear in any table and nothing would notice.
    """
    fixture_fields = _fixture_fields()
    problems: list[str] = []

    for key in sorted(fixture_fields):
        source, record = key
        required = REQUIRED_FIELDS.get(key, frozenset())
        structural = frozenset(STRUCTURAL_FIELDS.get(key, {}))
        uncovered = UNCOVERED_FIELDS.get(key, frozenset())
        classified = required | structural | uncovered

        unclassified = sorted(fixture_fields[key] - classified)
        if unclassified:
            problems.append(f"{source}/{record}: unclassified source field(s) {unclassified}")
        phantom = sorted(classified - fixture_fields[key])
        if phantom:
            problems.append(f"{source}/{record}: classified but not in the fixtures {phantom}")
        overlap = sorted(
            (required & structural) | (required & uncovered) | (structural & uncovered)
        )
        if overlap:
            problems.append(f"{source}/{record}: field(s) in two classifications {overlap}")

    assert not problems, "\n".join(problems)


def test_every_required_source_field_has_a_declared_lineage_path() -> None:
    """R1: every source record class the fixtures hold declares lineage for its fields.

    The record classes are discovered from the **fixture tree**, then looked up in
    `LINEAGE_PATH_MAPS`. A source with no entry there is a source that declares
    nothing and writes nothing -- which is precisely how payments passed the guard
    this replaced -- and it fails here on its own line.
    """
    fixture_fields = _fixture_fields()
    problems: list[str] = []

    for key in sorted(fixture_fields):
        source, record = key
        declared = LINEAGE_PATH_MAPS.get(key)
        if declared is None:
            problems.append(
                f"{source}/{record}: no entry in LINEAGE_PATH_MAPS, so none of its "
                f"{len(fixture_fields[key])} fields carry lineage (R1)"
            )
            continue

        columns = set(declared.values())
        missing = sorted(REQUIRED_FIELDS.get(key, frozenset()) - columns)
        if missing:
            problems.append(f"{source}/{record}: required field(s) with no lineage path {missing}")

        # Every declared path is source-qualified as `<source>.<record>.<field>` and
        # reads a column the fixtures actually carry -- a path pointing at a field
        # the source does not emit would write `\N` forever and look like a null.
        for path, column in sorted(declared.items()):
            if path != f"{source}.{record}.{column}":
                problems.append(f"{source}/{record}: path {path!r} does not name column {column!r}")
            if column not in fixture_fields[key]:
                problems.append(f"{source}/{record}: path {path!r} reads a field no record has")

        # The published gap is really a gap: nothing listed as uncovered is declared.
        contradicted = sorted(UNCOVERED_FIELDS.get(key, frozenset()) & columns)
        if contradicted:
            problems.append(
                f"{source}/{record}: field(s) listed as UNCOVERED but declared {contradicted}"
            )

    assert not problems, "\n".join(problems)


def test_payments_is_covered_to_its_required_extent() -> None:
    """The gap this ticket closed, stated as its own assertion.

    Named separately from the sweep above so the failure reads "payments lost its
    lineage" rather than "some source is missing some field".
    """
    declared = LINEAGE_PATH_MAPS.get(("payments", "payment"))
    assert declared is not None, "payments declares no lineage path map at all (R1)"
    assert set(declared.values()) == REQUIRED_FIELDS[("payments", "payment")]
    assert set(declared) == {
        "payments.payment.amount_cents",
        "payments.payment.currency",
        "payments.payment.external_ref",
        "payments.payment.occurred_at",
        "payments.payment.payer_email",
        "payments.payment.payer_name",
        "payments.payment.status",
        "payments.payment.type",
    }
    assert set(declared) <= set(LINEAGE_PATHS)


def test_the_lineage_vocabulary_still_covers_what_reads_it() -> None:
    """`LINEAGE_PATHS` is what the maps write, and it still covers its two readers.

    `LINEAGE_PATHS` is now derived from the path maps rather than from
    `COMPARED_FIELD_PATHS`, so these two containments stopped being tautologies of
    the definition and became things that can actually break: drop a compared path
    from a map and R16's A -> B -> A scan goes blind to a field a conflict names;
    drop a survived path and the endpoint shows a value whose provenance it cannot
    give. `COMPARED_FIELD_PATHS` itself is unchanged and unchangeable from here --
    `recon.resolve` no longer imports it -- which is what keeps the committed
    0-false-positive / 0-false-negative result out of this ticket's blast radius.
    """
    from recon.reference import COMPARED_FIELD_PATHS
    from recon.resolve import SURVIVED_PATHS

    written = {path for paths in LINEAGE_PATH_MAPS.values() for path in paths}
    assert written == set(LINEAGE_PATHS)
    assert set(COMPARED_FIELD_PATHS) <= written
    assert set(SURVIVED_PATHS) <= written
    # The compared vocabulary is SS2.4's twelve paths, and lineage coverage did not
    # add a thirteenth: adding one would change which conflicts exist.
    assert len(COMPARED_FIELD_PATHS) == 12
    assert not {path for path in COMPARED_FIELD_PATHS if path.startswith("payments.")}
