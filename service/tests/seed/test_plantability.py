"""The plantability failure path itself (SS10 `G31`).

`G31` is only worth anything if an unplantable conflict actually stops the run. These
tests sabotage a *real* plant -- by making the link its rule presumes impossible -- and
assert the seed fails loudly, names the conflict, and writes no `golden/` tree.

This is the test that converts a silent scorecard failure into a loud seed failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recon.seed import run as run_module
from recon.seed.build import Plant, build_dataset
from recon.seed.run import SeedFailure, run_seed


def _sabotage(monkeypatch: pytest.MonkeyPatch, mutate) -> None:
    def wrapped(seed: int, plan):
        dataset = build_dataset(seed, plan)
        mutate(dataset)
        return dataset

    monkeypatch.setattr(run_module, "build_dataset", wrapped)


def test_a_c4_plant_whose_l3_link_cannot_form_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give one C4 contact a hard key: it now links by `L1`, so `R-004` can never see it."""

    def mutate(dataset) -> None:
        plant = next(p for p in dataset.plants if p.conflict_type == "C4")
        crm_id = plant.contact_ids[0]
        contact = next(c for c in dataset.contacts if str(c["crm_id"]) == crm_id)
        student = next(child for child in dataset.children if child.contact is contact)
        contact["external_id"] = student.student_id

    _sabotage(monkeypatch, mutate)
    with pytest.raises(SeedFailure) as excinfo:
        run_seed(seed=20260822, profile="dev", out_dir=tmp_path, quiet=True)

    message = str(excinfo.value)
    assert "sc_plantability" in message
    assert "UNPLANTABLE" in message
    assert "C4" in message
    assert not (tmp_path / "golden" / "conflicts.json").exists(), (
        "G31: an unplantable conflict must never reach golden/"
    )


def test_a_plant_with_no_conflict_at_all_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Register a C13 plant on a student who holds no refunded payment."""

    def mutate(dataset) -> None:
        clean = next(
            child
            for child in dataset.children
            if child.household.role == "plain" and not child.roles and child.payments
        )
        dataset.plants.append(
            Plant(
                conflict_type="C13",
                student_id=clean.student_id,
                enrollment_id=str(clean.enrollment["id"]),
                payment_ids=(str(clean.payments[0]["payment_id"]),),
            )
        )

    _sabotage(monkeypatch, mutate)
    with pytest.raises(SeedFailure) as excinfo:
        run_seed(seed=20260822, profile="dev", out_dir=tmp_path, quiet=True)

    message = str(excinfo.value)
    assert "UNPLANTABLE" in message and "C13" in message
    assert not (tmp_path / "golden").exists() or not list((tmp_path / "golden").glob("*.json"))


def test_a_surplus_conflict_fails_the_construction_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manufacture one unplanted C1 -- the sweep must refuse the surplus, not absorb it."""

    def mutate(dataset) -> None:
        household = next(
            h
            for h in dataset.households
            if h.bucket == "tri" and h.size == 1 and h.role == "plain" and h.deal_id
        )
        deal_id = household.deal_id
        dataset.deals[:] = [d for d in dataset.deals if str(d["deal_id"]) != deal_id]
        household.deal_id = None
        for child in household.children:
            if child.enrollment is not None:
                child.enrollment["crm_deal_id"] = None

    _sabotage(monkeypatch, mutate)
    with pytest.raises(SeedFailure) as excinfo:
        run_seed(seed=20260822, profile="dev", out_dir=tmp_path, quiet=True)

    message = str(excinfo.value)
    assert "sc_construction_sweep" in message or "sc_deal_coverage" in message
    assert not (tmp_path / "golden" / "conflicts.json").exists()


def test_the_unsabotaged_dev_run_is_green(tmp_path: Path) -> None:
    """Control: the same call succeeds when nothing is sabotaged."""
    result = run_seed(seed=20260822, profile="dev", out_dir=tmp_path, quiet=True)
    assert result.report.passed
    assert result.golden_entries > 0
    assert (tmp_path / "golden" / "conflicts.json").exists()


def test_the_dev_profile_refuses_to_overwrite_the_repository_tree() -> None:
    """SS9: "All gates, benchmarks, and the committed `golden/` files are `full`".

    `make seed` ran `--profile dev` with no `--out`, so the one documented one-command
    seed (README step 6) overwrote the committed full-profile `fixtures/` and `golden/`
    with a ~6,000-record dataset that fails every A.4 minimum. The guard is here rather
    than only in the Makefile because the Makefile is not the only caller.
    """
    with pytest.raises(SeedFailure) as excinfo:
        run_seed(seed=20260822, profile="dev", out_dir=None, quiet=True)
    assert "--out" in str(excinfo.value)


def test_a_failed_run_leaves_no_fixture_tree_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SS9.1: a rejected dataset must not be left on disk beside a valid `golden/`."""

    def mutate(dataset) -> None:
        plant = next(p for p in dataset.plants if p.conflict_type == "C4")
        crm_id = plant.contact_ids[0]
        contact = next(c for c in dataset.contacts if str(c["crm_id"]) == crm_id)
        student = next(child for child in dataset.children if child.contact is contact)
        contact["external_id"] = student.student_id

    _sabotage(monkeypatch, mutate)
    with pytest.raises(SeedFailure):
        run_seed(seed=20260822, profile="dev", out_dir=tmp_path, quiet=True)
    assert not (tmp_path / "golden").exists()
    assert not (tmp_path / "fixtures").exists()


def test_a_seed_run_preserves_a_gitkeep_marker(tmp_path: Path) -> None:
    """`reset_directory` used to `rmtree` the whole directory, deleting tracked files.

    Both `fixtures/.gitkeep` and `golden/.gitkeep` are git-tracked (`.gitignore` even
    carries an explicit `!fixtures/.gitkeep` un-ignore) and every seed run destroyed them.
    """
    for name in ("fixtures", "golden"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
        (tmp_path / name / ".gitkeep").write_text("")
    (tmp_path / "fixtures" / "stale.jsonl").write_text("{}\n")

    run_seed(seed=20260822, profile="dev", out_dir=tmp_path, quiet=True)

    assert (tmp_path / "fixtures" / ".gitkeep").is_file()
    assert (tmp_path / "golden" / ".gitkeep").is_file()
    # ... while a stale GENERATED file is still removed.
    assert not (tmp_path / "fixtures" / "stale.jsonl").exists()
