"""The registry is typed to the PORT, so a non-JSONL connector is a first-class
source (R1).

`SOURCE_ADAPTERS` was `dict[str, type[JsonlSnapshotAdapter]]` and `build_adapters`
returned `dict[str, JsonlSnapshotAdapter]`. All three shipped sources are JSONL
classes, so the annotation was *true* -- and that is the problem: it made an
accident of the fixtures into the contract. The module docstring promises "a real
HubSpot connector implements `source_id` / `generations()` / `read()` against an
HTTP client instead of a directory, and every consumer in this package keeps
working", and the type said otherwise.

It is not only a documentation defect. The standing structural no-write check
(`recon.apply.assert_sources_are_unwritable`) sweeps **whatever `build_adapters`
returns**, so what the registry admits is what that check covers. A connector
that could not be registered without a cast is a connector that gets bolted on
somewhere else, and then it is outside the sweep -- which is the shape of every
control this project has already shipped twice: present in the route table,
inspecting nothing.

`tests/ingest/test_read_only_port.py` proves an in-memory source *satisfies* the
port. This module proves the **registry** takes one: it registers a non-JSONL
connector, builds through the real factory, and drives the real ingest, health
and no-write paths over it.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from recon.adapters import RawRecord, ReadOnlyAdapter, build_adapters, stub_records
from recon.adapters.jsonl import SOURCE_ADAPTERS, SourceAdapterFactory


class HttpishConnector:
    """A source with no filesystem: the substitution the port promises.

    Deliberately **not** a `JsonlSnapshotAdapter` subclass and deliberately not a
    subclass of anything -- inheriting from the JSONL class would prove the old
    annotation, not the new one.
    """

    source_id = "hubspot"

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        on_reject: Any = None,
        max_payload_bytes: int = 0,
    ) -> None:
        self.calls: list[int] = []

    def generations(self) -> list[int]:
        return [901]

    def read(self, generation: int) -> Iterator[RawRecord]:
        self.calls.append(generation)
        yield from stub_records(3, source_id="hubspot", entity_type="contact", generation=901)


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """`HttpishConnector` in the real registry, for the length of one test."""
    monkeypatch.setitem(SOURCE_ADAPTERS, "hubspot", HttpishConnector)
    yield


# ----------------------------------------------------------------------------------
# the annotations themselves
# ----------------------------------------------------------------------------------


def test_the_registry_is_not_typed_to_the_jsonl_class() -> None:
    """Read off the source, because the annotation IS the defect here.

    `from __future__ import annotations` makes these strings at runtime, so this
    is a string comparison on purpose -- there is nothing else to inspect, and a
    check that could not see the annotation could not see the bug either.
    """
    import recon.adapters.jsonl as jsonl_module

    annotation = jsonl_module.__annotations__["SOURCE_ADAPTERS"]
    assert "JsonlSnapshotAdapter" not in annotation, annotation
    assert "SourceAdapterFactory" in annotation, annotation


def test_build_adapters_returns_the_port_not_the_implementation() -> None:
    returned = inspect.signature(build_adapters).return_annotation
    assert "JsonlSnapshotAdapter" not in returned, returned
    assert "ReadOnlyAdapter" in returned, returned


def test_the_factory_protocol_describes_the_call_build_adapters_makes() -> None:
    """A Protocol nothing is checked against is decoration.

    `SourceAdapterFactory.__call__` must accept exactly the arguments
    `build_adapters` passes, or the type is a comment. Compared parameter by
    parameter rather than by eye.
    """
    factory_params = inspect.signature(SourceAdapterFactory.__call__).parameters
    assert set(factory_params) - {"self"} == {"root", "on_reject", "max_payload_bytes"}

    builder_params = inspect.signature(build_adapters).parameters
    assert set(builder_params) == {"root", "on_reject", "max_payload_bytes"}


# ----------------------------------------------------------------------------------
# ...and the behaviour they are supposed to describe
# ----------------------------------------------------------------------------------


def test_a_non_jsonl_connector_can_be_registered_and_built(registered: None) -> None:
    """The whole point: `build_adapters` yields it alongside the three."""
    built = build_adapters(None)
    assert sorted(built) == ["appdb", "crm", "hubspot", "payments"]

    connector = built["hubspot"]
    from recon.adapters import JsonlSnapshotAdapter

    assert not isinstance(connector, JsonlSnapshotAdapter)
    assert isinstance(connector, ReadOnlyAdapter)


def test_the_registered_connector_drives_the_real_ingest_path(registered: None) -> None:
    """Registered, built, and actually read from -- not merely constructed.

    The status is `partial`, and that is the pipeline being right rather than the
    connector being wrong: `("hubspot", "contact")` has no `stg_*` table, so
    `LoadResult.known_short` is true by construction -- `rules/*.sql` read
    staging, and a generation no rule can see is *provably* not whole (SS5.3).
    A new source is a schema change as well as a registry entry, and this is
    where the pipeline says so instead of reporting a clean sync over rows
    nothing can read. What the registry is responsible for is asserted directly:
    the records came out, and they came out of *this* object.
    """
    from recon.ingest import ingest_source

    connector = build_adapters(None)["hubspot"]
    result = ingest_source(connector, 901, run_id="registry-protocol", persist=False)

    assert result.records_ok == 3
    assert connector.calls == [901]  # type: ignore[attr-defined]
    assert result.error is None
    assert result.status == "partial"
    assert [load.staged for load in result.loads] == [False]
    assert all(load.known_short for load in result.loads)


def test_the_registered_connector_drives_the_real_health_path(registered: None) -> None:
    from recon.health import probe_source

    health = probe_source(build_adapters(None)["hubspot"], timeout=2.0)
    assert health["status"] == "ok"


def test_the_no_write_sweep_now_covers_the_registered_connector(registered: None) -> None:
    """The consequence that matters: the standing check follows the registry.

    Named in the return value, so this is not "the sweep did not raise" (which a
    sweep that inspected nothing would also satisfy) -- it is "the sweep looked
    at this class".
    """
    from recon.apply import assert_sources_are_unwritable

    inspected = assert_sources_are_unwritable()
    assert "HttpishConnector" in inspected, inspected
