"""The port is read-only, and it is swappable (R1).

DESIGN.md: "Adapters expose **no write methods** -- the Protocol has none; adding
one is a design violation." TASKS.md T-4 acceptance 6 asks for a "grep-level
check". A grep is not enough and would pass while the property is false: it reads
one file's *text*, so it misses an inherited method, a method installed on the
class at import time, a mixin pulled in from elsewhere, and an attribute assigned
in `__init__` -- and it fires on the word appearing inside a comment. These tests
introspect the **classes and their instances** instead, walking the whole MRO, so
what is asserted is the object a caller actually gets.

The second half matters as much as the first. A port nothing else can implement
is not a port, it is a class with extra ceremony -- so there is a test that builds
a source out of an in-memory list, with no filesystem anywhere, and drives the
real ingest and health paths through it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from recon.adapters import (
    WRITE_NAME_TOKENS,
    AppDbAdapter,
    CrmAdapter,
    FaultInjectingAdapter,
    JsonlSnapshotAdapter,
    PaymentsAdapter,
    RawRecord,
    ReadOnlyAdapter,
    build_adapters,
    stub_records,
)
from recon.health import probe_source
from recon.ingest import ingest_source

ADAPTER_CLASSES = (
    JsonlSnapshotAdapter,
    CrmAdapter,
    AppDbAdapter,
    PaymentsAdapter,
    FaultInjectingAdapter,
)


def _class_attribute_names(adapter_class: type) -> set[str]:
    """Every name reachable on the class, including inherited ones."""
    names: set[str] = set(dir(adapter_class))
    for klass in adapter_class.__mro__:
        names.update(vars(klass))
    return names


def _write_shaped(names: set[str]) -> set[str]:
    return {name for name in names if any(token in name.lower() for token in WRITE_NAME_TOKENS)}


@pytest.mark.parametrize("adapter_class", ADAPTER_CLASSES, ids=lambda c: c.__name__)
def test_no_adapter_class_exposes_a_write_shaped_attribute(adapter_class: type) -> None:
    offenders = _write_shaped(_class_attribute_names(adapter_class))
    assert offenders == set(), (
        f"{adapter_class.__name__} exposes {sorted(offenders)}; adapters are read-only "
        "and the Protocol has no write member (DESIGN Interfaces/ReadOnlyAdapter)"
    )


def test_the_protocol_itself_has_no_write_member() -> None:
    assert _write_shaped(_class_attribute_names(ReadOnlyAdapter)) == set()


def test_the_protocol_has_exactly_the_three_pinned_members() -> None:
    """DESIGN pins `source_id`, `generations()`, `read(generation)` -- and no fourth."""
    assert set(ReadOnlyAdapter.__protocol_attrs__) == {"source_id", "generations", "read"}


def test_instances_gain_no_write_shaped_attribute_at_construction(tmp_path: Path) -> None:
    """A class can be clean while `__init__` bolts a writer onto the instance."""
    instances = [
        CrmAdapter(tmp_path),
        AppDbAdapter(tmp_path),
        PaymentsAdapter(tmp_path),
        FaultInjectingAdapter(mode="ok", records=stub_records(1)),
    ]
    for instance in instances:
        offenders = _write_shaped(set(vars(instance)) | _class_attribute_names(type(instance)))
        assert offenders == set(), f"{type(instance).__name__} instance exposes {sorted(offenders)}"


def test_every_shipped_adapter_satisfies_the_protocol(tmp_path: Path) -> None:
    for adapter in build_adapters(tmp_path).values():
        assert isinstance(adapter, ReadOnlyAdapter)


def test_the_three_sources_are_the_three_the_spec_names() -> None:
    assert sorted(build_adapters().keys()) == ["appdb", "crm", "payments"]


# ----------------------------------------------------------------------------------
# swappability
# ----------------------------------------------------------------------------------


class InMemorySource:
    """A source with no filesystem at all -- the swappability proof.

    If a future HubSpot connector can be dropped in behind the same Protocol, so
    can this. It implements the three members and nothing else.
    """

    def __init__(self, source_id: str, records: dict[int, list[RawRecord]]) -> None:
        self.source_id = source_id
        self._records = records

    def generations(self) -> list[int]:
        return sorted(self._records)

    def read(self, generation: int) -> Iterator[RawRecord]:
        yield from self._records.get(generation, [])


def test_a_non_filesystem_source_drives_the_real_ingest_and_health_paths() -> None:
    records = stub_records(5, source_id="crm", entity_type="contact", generation=901)
    source = InMemorySource("crm", {901: list(records)})

    assert isinstance(source, ReadOnlyAdapter)
    assert _write_shaped(_class_attribute_names(InMemorySource)) == set()

    result = ingest_source(source, 901, run_id="swap-test", persist=False)
    assert result.records_ok == 5
    assert result.status == "ok"

    health = probe_source(source, timeout=2.0)
    assert health["status"] == "ok"
    assert health["latency_ms"] >= 0


def test_the_fixture_path_is_injected_not_hardcoded(tmp_path: Path, seed_tree: object) -> None:
    """Two adapters of the same class, two roots, two different answers."""
    from tests.ingest.conftest import FixtureTree

    assert isinstance(seed_tree, FixtureTree)
    populated = CrmAdapter(seed_tree.root)
    empty = CrmAdapter(tmp_path)

    assert populated.generations() == list(seed_tree.generations)
    assert empty.generations() == []
    assert populated.root != empty.root
