"""Two SS9.1 guards that reported PASS without checking anything (`G26`, `G27`).

**`sc_malformed_isolation` (G27)** ended in a comprehension over `for crm_id in ()` --
an empty tuple, so the `any(...)` was `False` on every possible input and the branch
that appends "a malformed case reuses a fixture PK" could not be taken. A
`# pragma: no cover - explicit guard, cheap` sat on it, which excused the coverage
report from noticing. The check printed PASS on every run while performing no check.

**`sc_timestamp_dirt_spread` (G26)** asserted `count > 0` and printed "~0.5% of each
entity type". One record per entity type satisfied `count > 0`; so did half the
dataset. A.3 asks for a *rate*, and the assertion measured its own presence.

Both are now real, and both are shown here to be capable of failing -- once against the
pure helper, and once end to end, by sabotaging a real `--profile dev` run and requiring
`SeedFailure` to name the check. A guard that has never been seen to fail is
indistinguishable from a guard that cannot.

Every seed run writes to `tmp_path`; nothing here can reach the committed `golden/`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from recon.seed import run as run_module
from recon.seed.build import Dataset, build_dataset
from recon.seed.malformed import build_malformed_cases
from recon.seed.plan import build_plan
from recon.seed.run import SeedFailure, run_seed
from recon.seed.selfcheck import (
    MALFORMED_PK_FIELDS,
    TIMESTAMP_DIRT_BAND,
    TIMESTAMP_DIRT_RATE,
    declared_primary_keys,
    fixture_primary_keys,
    malformed_pk_collisions,
    timestamp_dirt_problems,
)

SEED = 20260822

#: The five entity buckets `_apply_timestamp_dirt` draws from, spelled as the
#: self-check labels them.
BUCKETS: tuple[str, ...] = (
    "appdb.enrollment",
    "appdb.student",
    "crm.contact",
    "crm.deal",
    "payments.payment",
)


@pytest.fixture(scope="module")
def dev_dataset() -> Dataset:
    """A real `--profile dev` pass-1 dataset, built in process.

    Pass 1 only: these two guards read `dataset.*` record lists and the committed
    malformed corpus, and nothing they assert depends on resolution, the sweep or the
    golden set. Nothing is written to disk.
    """
    return build_dataset(SEED, build_plan("dev"))


# =========================================================================================
# G27 -- malformed-corpus isolation
# =========================================================================================


def test_the_committed_corpus_collides_with_no_generated_primary_key(
    dev_dataset: Dataset,
) -> None:
    """The property the check is supposed to hold: green, on real data, for real reasons."""
    keys = fixture_primary_keys(dev_dataset)
    assert malformed_pk_collisions(keys, build_malformed_cases()) == []
    # ...and the key sets are populated, so the green is not "nothing to compare against"
    for entity in MALFORMED_PK_FIELDS:
        assert keys[entity], f"no generated primary keys for {entity}"


def test_a_case_that_claims_a_real_primary_key_is_caught(dev_dataset: Dataset) -> None:
    """The check can FAIL. This is the input the empty-tuple version could not see.

    `MAL-019` and `MAL-020` are the `duplicate_primary_key` pair: their whole purpose is
    to produce one `409` against *each other*. Pointed at a generated contact instead,
    they would be a collision with the dataset -- and the fixture ids the corpus uses
    (`CRM-90000NN`) sit in a 9,000,000 band precisely so that cannot happen by accident.
    """
    real = str(dev_dataset.contacts[0]["crm_id"])
    poisoned = _poison_corpus(build_malformed_cases(), real)

    collisions = malformed_pk_collisions(fixture_primary_keys(dev_dataset), poisoned)

    assert collisions, f"a malformed case declaring the real PK {real} was not caught"
    assert all(real in entry for entry in collisions)
    assert any(entry.startswith("MAL-019") for entry in collisions)


@pytest.mark.parametrize("entity", sorted(MALFORMED_PK_FIELDS))
def test_every_entity_type_is_checked_not_just_contacts(dev_dataset: Dataset, entity: str) -> None:
    """The dead guard only ever built a contact key set; a deal or payment collision
    would have been invisible even had the loop iterated."""
    field = MALFORMED_PK_FIELDS[entity]
    real = next(iter(sorted(fixture_primary_keys(dev_dataset)[entity])))
    case = {
        "case_id": f"SYNTH-{entity}",
        "entity_type": entity,
        "kind": "duplicate_primary_key",
        "raw": f'{{"{field}":"{real}","note":"synthetic"}}',
    }
    assert malformed_pk_collisions(fixture_primary_keys(dev_dataset), [case])


def test_a_reference_to_a_real_contact_is_not_a_collision(dev_dataset: Dataset) -> None:
    """The distinction the check turns on, pinned so it cannot be "simplified" away.

    Three deal cases carry `"associated_contact_ids":["CRM-0000001"]`, and that IS a
    real generated contact. It is a foreign key on a payload the adapter rejects before
    it can land, not a claim to *be* that contact -- so a guard that searched the whole
    raw string for any fixture id (which is the shape the dead one had) would fail the
    seed run on every profile. The assertion below is what stops someone restoring that
    shape and then widening the corpus to make the red go away.
    """
    contact_ids = fixture_primary_keys(dev_dataset)["contact"]
    referencing = [
        case for case in build_malformed_cases() if "associated_contact_ids" in str(case["raw"])
    ]
    assert referencing, "no malformed case references a contact any more"
    assert any(any(ref in str(case["raw"]) for ref in ("CRM-0000001",)) for case in referencing)
    assert "CRM-0000001" in contact_ids, "the referenced id is not a real generated PK"
    assert malformed_pk_collisions(fixture_primary_keys(dev_dataset), referencing) == []


def test_primary_keys_are_read_out_of_payloads_that_do_not_parse() -> None:
    """`json.loads` is not available here: most of this corpus is deliberately broken.

    A truncated body, a trailing comma and a non-object line are the three shapes that
    would silently return "no primary key" under a parser -- and "no primary key" is
    indistinguishable from "no collision".
    """
    truncated = {
        "case_id": "T",
        "entity_type": "contact",
        "raw": '{"crm_id":"CRM-9000012","email":"truncated@example.test","first_name":"Ada"',
    }
    trailing = {
        "case_id": "T2",
        "entity_type": "contact",
        "raw": '{"crm_id":"CRM-9000015","email":"trailing@example.test",}',
    }
    non_object = {"case_id": "T3", "entity_type": "contact", "raw": '["CRM-9000021","a@b.test"]'}
    assert declared_primary_keys(truncated) == ["CRM-9000012"]
    assert declared_primary_keys(trailing) == ["CRM-9000015"]
    assert declared_primary_keys(non_object) == []
    # an entity type with no known key field yields nothing rather than raising
    assert declared_primary_keys({"case_id": "T4", "entity_type": "nope", "raw": "{}"}) == []


def test_the_real_seed_run_fails_when_a_malformed_case_claims_a_fixture_pk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end (`G31`): the guard is wired into a run that really stops.

    The helper being correct proves nothing about the check unless the check calls it,
    so this sabotages `build_malformed_cases` in the module `run_seed` actually reads it
    from, and requires the run to fail, to name `sc_malformed_isolation`, and to write
    no `golden/` tree.
    """
    seen: dict[str, Dataset] = {}

    def capture(seed: int, plan: Any) -> Dataset:
        dataset = build_dataset(seed, plan)
        seen["dataset"] = dataset
        return dataset

    def poisoned() -> list[dict[str, Any]]:
        return _poison_corpus(build_malformed_cases(), str(seen["dataset"].contacts[0]["crm_id"]))

    monkeypatch.setattr(run_module, "build_dataset", capture)
    monkeypatch.setattr(run_module, "build_malformed_cases", poisoned)

    with pytest.raises(SeedFailure) as excinfo:
        run_seed(seed=SEED, profile="dev", out_dir=tmp_path, quiet=True)

    message = str(excinfo.value)
    assert "sc_malformed_isolation" in message
    assert "reuse a fixture PK" in message
    assert not (tmp_path / "golden" / "conflicts.json").exists()


def _poison_corpus(cases: list[dict[str, Any]], real_crm_id: str) -> list[dict[str, Any]]:
    """The committed corpus with the duplicate-PK pair repointed at a real contact."""
    return [
        dict(case, raw=str(case["raw"]).replace("CRM-9000019", real_crm_id))
        if case["case_id"] in {"MAL-019", "MAL-020"}
        else case
        for case in cases
    ]


# =========================================================================================
# G26 -- the out-of-order-timestamp rate
# =========================================================================================


def test_the_real_dataset_sits_inside_the_band(dev_dataset: Dataset) -> None:
    """Green on real data, and *near the middle*, not scraping an edge.

    A band that a real profile only just satisfies is a band that will go red on an
    unrelated volume change, and the fix under pressure is to widen it. The measured
    rates at this seed are 0.398%-0.500%; the band is 0.3%-0.7%.
    """
    skewed, totals = _dirt_counts(dev_dataset)
    assert timestamp_dirt_problems(skewed, totals) == []
    low, high = TIMESTAMP_DIRT_BAND
    assert low < TIMESTAMP_DIRT_RATE < high
    for label in BUCKETS:
        rate = skewed[label] / totals[label]
        assert low < rate <= TIMESTAMP_DIRT_RATE, f"{label} at {rate:.5f}"


def test_a_bucket_with_no_dirt_at_all_is_caught() -> None:
    """0% is outside the band. `count > 0` caught this one too -- it is the floor."""
    problems = timestamp_dirt_problems(
        {"crm.contact": 0, "crm.deal": 4}, {"crm.contact": 1000, "crm.deal": 800}
    )
    assert [p.split(":")[0] for p in problems] == ["crm.contact"]
    assert "0.0%" in problems[0]


def test_ten_times_too_much_dirt_is_caught_and_the_old_assertion_missed_it() -> None:
    """The discriminating case, and the reason the assertion had to change.

    5% out-of-order timestamps satisfies `count > 0` for every bucket, so the check as
    written printed PASS beside the sentence "~0.5% of each entity type" while the
    dataset carried ten times what A.3 asks for.
    """
    skewed = {label: 50 for label in BUCKETS}
    totals = {label: 1000 for label in BUCKETS}
    assert all(count > 0 for count in skewed.values()), "the old predicate is satisfied"
    problems = timestamp_dirt_problems(skewed, totals)
    assert len(problems) == len(BUCKETS)
    assert all("5.0%" in problem for problem in problems)


def test_an_empty_bucket_is_a_problem_not_a_division_by_zero() -> None:
    assert timestamp_dirt_problems({"crm.deal": 0}, {"crm.deal": 0}) == [
        "crm.deal: no records at all"
    ]


def test_the_problem_message_names_the_bucket_the_counts_and_the_band() -> None:
    """A rate failure that does not say which entity type costs a debugging round trip."""
    (problem,) = timestamp_dirt_problems({"crm.deal": 1}, {"crm.deal": 1000})
    assert problem.startswith("crm.deal: 1/1000 = 0.1%")
    assert "0.3%-0.7%" in problem


def test_the_real_seed_run_fails_when_a_bucket_loses_its_dirt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: un-skew every contact and the run must stop."""

    def mutate(dataset: Dataset) -> None:
        for record in dataset.contacts:
            if str(record["updated_at"]) < str(record["created_at"]):
                record["created_at"], record["updated_at"] = (
                    record["updated_at"],
                    record["created_at"],
                )

    _sabotage(monkeypatch, mutate)
    with pytest.raises(SeedFailure) as excinfo:
        run_seed(seed=SEED, profile="dev", out_dir=tmp_path, quiet=True)
    message = str(excinfo.value)
    assert "sc_timestamp_dirt_spread" in message
    assert "crm.contact: 0/" in message
    assert not (tmp_path / "golden" / "conflicts.json").exists()


def test_the_real_seed_run_fails_when_a_bucket_carries_far_too_much_dirt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, on the case the old assertion passed: 10% of contacts skewed.

    Every bucket still has `count > 0`, so `sc_timestamp_dirt_spread` used to print
    PASS. It now fails the run, and the detail names the rate it measured.
    """

    def mutate(dataset: Dataset) -> None:
        for record in dataset.contacts[: len(dataset.contacts) // 10]:
            created, updated = str(record["created_at"]), str(record["updated_at"])
            if updated < created:
                continue
            record["created_at"], record["updated_at"] = updated, created

    _sabotage(monkeypatch, mutate)
    with pytest.raises(SeedFailure) as excinfo:
        run_seed(seed=SEED, profile="dev", out_dir=tmp_path, quiet=True)
    message = str(excinfo.value)
    assert "sc_timestamp_dirt_spread" in message
    assert "out-of-order timestamps, outside" in message
    assert not (tmp_path / "golden" / "conflicts.json").exists()


def _sabotage(monkeypatch: pytest.MonkeyPatch, mutate: Any) -> None:
    """Break a real dataset between pass 1 and the self-check (`tests/seed/test_plantability`)."""

    def wrapped(seed: int, plan: Any) -> Dataset:
        dataset = build_dataset(seed, plan)
        mutate(dataset)
        return dataset

    monkeypatch.setattr(run_module, "build_dataset", wrapped)


def _dirt_counts(dataset: Dataset) -> tuple[dict[str, int], dict[str, int]]:
    """The same two mappings `_g24_g30` builds, from the same record lists."""
    records = {
        "crm.contact": dataset.contacts,
        "crm.deal": dataset.deals,
        "appdb.student": dataset.students,
        "payments.payment": dataset.payments,
        "appdb.enrollment": dataset.enrollments,
    }
    skewed = {
        label: sum(1 for row in rows if str(row.get("updated_at")) < str(row.get("created_at")))
        for label, rows in records.items()
    }
    return skewed, {label: len(rows) for label, rows in records.items()}
