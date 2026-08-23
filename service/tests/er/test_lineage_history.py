"""`field_lineage` across generations 1-3: what each source said, and when.

Run against a **three-generation** dev-profile dataset, because this is the only
place `materialize`'s default (`lineage_generations=(1, 2, 3)`) is exercised at
all -- the full-profile dataset the rest of the suite uses lands generation 3
alone, following `tests/invariants/conftest.py` and SS7's "current state is
generation 3". Without this module the driver's default path would never run, and
"lineage retains history" would be a docstring rather than a check.

Two facts are asserted here and nowhere else:

* the A -> B -> A shape R4/R16 detect is **present and findable** in the table --
  the same value re-asserted after a change, which is what oscillation dedup keys
  on;
* `person_key` does not move between generations (SS4.1, SS9.2). It is a pure
  function of `anchor_ref`, so a person whose anchor class changed would split its
  own lineage in two -- silently, and only visibly here.
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from tests.er.dataset import Dataset

#: SS7 / SS9.1 -- a structural minimum, identical in both profiles.
REASSERTING_FIELD_MINIMUM = 25

_GENERATIONS = text("SELECT DISTINCT generation FROM field_lineage ORDER BY generation")

#: One row per `(canonical_id, field, source_ref)` whose generation-1 value is
#: re-asserted at generation 3 after a different generation-2 value.
_OSCILLATIONS = text(
    """
    WITH scan AS (
        SELECT canonical_id, field, source_ref,
               max(value_text) FILTER (WHERE generation = 1) AS v1,
               max(value_text) FILTER (WHERE generation = 2) AS v2,
               max(value_text) FILTER (WHERE generation = 3) AS v3
          FROM field_lineage
         GROUP BY canonical_id, field, source_ref
    )
    SELECT canonical_id::text AS canonical_id, field, source_ref, v1, v2, v3
      FROM scan
     WHERE v1 IS NOT NULL AND v2 IS NOT NULL AND v3 IS NOT NULL
       AND v1 = v3 AND v1 <> v2
     ORDER BY canonical_id, field
    """
)

_STUDENT_KEYS = text(
    """
    SELECT source_ref,
           count(DISTINCT canonical_id) AS keys,
           count(DISTINCT generation)   AS generations
      FROM field_lineage
     WHERE starts_with(source_ref, 'appdb:student:')
     GROUP BY source_ref
    """
)


def test_all_three_generations_are_retained(
    history_reader: Engine, history_dataset: Dataset
) -> None:
    """Generations 1-3 are all in the table; nothing overwrote an earlier snapshot."""
    with history_reader.connect() as conn:
        generations = [row.generation for row in conn.execute(_GENERATIONS)]
    assert generations == [1, 2, 3]
    assert history_dataset.report.lineage > 0


def test_the_oscillation_shape_is_findable(history_reader: Engine) -> None:
    """A -> B -> A is present, and the scan that finds it is a plain window read."""
    with history_reader.connect() as conn:
        rows = conn.execute(_OSCILLATIONS).fetchall()

    pairs = {(row.canonical_id, row.field) for row in rows}
    assert len(pairs) >= REASSERTING_FIELD_MINIMUM, (
        f"only {len(pairs)} re-asserting (person, field) pairs are visible in "
        f"field_lineage; SS7 plants at least {REASSERTING_FIELD_MINIMUM} in every profile"
    )
    for row in rows[:5]:
        assert row.v1 == row.v3 and row.v1 != row.v2
    print(f"\nre-asserting (person_key, field) pairs found: {len(pairs)}")


def test_person_key_is_stable_across_generations(history_reader: Engine) -> None:
    """One student ref, one `canonical_id`, in every generation it appears in."""
    with history_reader.connect() as conn:
        rows = conn.execute(_STUDENT_KEYS).fetchall()

    assert rows, "no student lineage rows were written"
    split = [row.source_ref for row in rows if row.keys != 1]
    assert not split, (
        f"{len(split)} student ref(s) carry more than one person_key across generations, "
        f"e.g. {split[:3]}: lineage for those persons is split in two"
    )
    assert any(row.generations == 3 for row in rows), (
        "no student appears in all three generations; the stability check saw no history"
    )
