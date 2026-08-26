"""`WRITE_NAME_TOKENS` covers the ordinary names for a write (R1, R24).

`tests/ingest/test_read_only_port.py` asserts that no adapter carries a
write-shaped attribute; this module is about the *list* that decides what
"write-shaped" means. The list had eleven entries, and the docstring of
`recon.apply.assert_sources_are_unwritable` named exactly what they missed::

    **``WRITE_NAME_TOKENS`` is a substring list, not a decision procedure**, and
    this function is exactly as exhaustive as that list is. An adapter with
    ``def persist(...)``, ``def commit(...)``, ``def flush(...)`` or
    ``def sync(...)`` carries no listed token and passes here.

An honest note in a docstring is not a control. Those are the *ordinary* names
for the operation the port forbids -- an ORM session writes with `commit` and
`flush`, a client library writes with `persist`, a two-way connector writes with
`sync` -- so a connector that wrote back through any of them passed a check whose
entire purpose was catching it. The tests below build such connectors and require
the check to refuse them, which is a property of the list rather than a reading
of it.

The last test is the honest half. `emit` belongs on the list by the same
argument, and getting it there cost a rename: a read-side counter in
`recon/adapters/faults.py` was called `emitted`, and because the match is on
*substrings*, listing the token would have failed the structural check on an
attribute counting records handed to a caller -- a read, not a write. The counter
is `records_handed_over` now and the token is listed. That test asserts both
halves, so neither can be undone quietly.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from recon.adapters import (
    WRITE_NAME_TOKENS,
    RawRecord,
    ReadOnlyAdapter,
    stub_records,
)
from recon.adapters.faults import FaultInjectingAdapter

#: The four names `recon/apply.py` had named as uncovered, plus this codebase's
#: own word for the write (`recon.ingest._land_records`, `Landing`, `landed`) and
#: `emit`, which needed a read-side rename in `recon/adapters/faults.py` before it
#: could be listed at all.
_NEWLY_COVERED = ("persist", "commit", "flush", "sync", "land", "emit")


def _write_shaped(names: set[str]) -> set[str]:
    """`tests/ingest/test_read_only_port.py`'s predicate, over one namespace."""
    return {name for name in names if any(token in name.lower() for token in WRITE_NAME_TOKENS)}


class _Connector:
    """A minimally conforming source, so a subclass differs only by its writer."""

    source_id = "hubspot"

    def generations(self) -> list[int]:
        return [1]

    def read(self, generation: int) -> Iterator[RawRecord]:
        yield from stub_records(1)


@pytest.mark.parametrize("verb", _NEWLY_COVERED)
def test_a_connector_that_writes_back_through_an_ordinary_verb_is_caught(verb: str) -> None:
    """The defect, one method at a time: `def commit(self)` on a source.

    Built as a real class rather than asserted against the tuple, because the
    tuple is not what protects anything -- the substring match over a class's
    whole MRO is, and a token that is present but never matched would pass a
    membership assertion and catch nothing.
    """
    connector = type("WritingConnector", (_Connector,), {verb: lambda self: None})

    names: set[str] = set(dir(connector))
    for klass in connector.__mro__:
        names.update(vars(klass))

    assert _write_shaped(names) == {verb}, (
        f"a source exposing {verb}() is not recognised as write-shaped; "
        f"WRITE_NAME_TOKENS = {WRITE_NAME_TOKENS}"
    )


@pytest.mark.parametrize("verb", _NEWLY_COVERED)
def test_the_apply_side_sweep_refuses_the_same_connector(verb: str, monkeypatch: Any) -> None:
    """The other consumer of the list, driven for real.

    `recon.apply.assert_sources_are_unwritable` reads the same tuple over whatever
    `build_adapters()` returns. Registering a writing connector and requiring the
    sweep to raise is what makes this a check rather than a list -- and it is the
    arm that would still be blind if the registry, not the tokens, were the gap.
    """
    from recon import apply as apply_module
    from recon.adapters import jsonl as jsonl_module

    connector = type("WritingConnector", (_Connector,), {verb: lambda self: None})

    def factory(root: Any = None, **kwargs: Any) -> Any:
        return connector()

    monkeypatch.setitem(jsonl_module.SOURCE_ADAPTERS, "hubspot", factory)

    with pytest.raises(AssertionError, match=verb):
        apply_module.assert_sources_are_unwritable()


def test_the_shipped_adapters_are_still_clean_under_the_wider_list() -> None:
    """Widening a substring list is how a legitimate name starts failing.

    Asserted over the classes *and* the constructed instances, exactly as
    `test_read_only_port.py` does, because `__init__` is where a counter or a
    cached handle appears.
    """
    from pathlib import Path

    from recon.adapters import AppDbAdapter, CrmAdapter, JsonlSnapshotAdapter, PaymentsAdapter

    root = Path("/nonexistent-tree")
    for adapter_class in (JsonlSnapshotAdapter, CrmAdapter, AppDbAdapter, PaymentsAdapter):
        names: set[str] = set(dir(adapter_class))
        for klass in adapter_class.__mro__:
            names.update(vars(klass))
        assert _write_shaped(names) == set(), adapter_class.__name__

    for instance in (CrmAdapter(root), AppDbAdapter(root), PaymentsAdapter(root)):
        assert _write_shaped(set(vars(instance))) == set(), type(instance).__name__


def test_emit_is_listed_and_no_read_side_name_collides_with_it() -> None:
    """A measured collision, closed -- and pinned closed in both directions.

    `emit` belongs on the list ("emit a write downstream" is the same class of
    verb as `persist`) and could not be listed while
    `FaultInjectingAdapter.emitted` existed: that counts records the adapter
    *handed over*, which is a read, and `WRITE_NAME_TOKENS` matches on
    substrings. The counter is `records_handed_over` now, so the token is listed.

    Both halves are asserted because either can be undone on its own, and each
    undoing looks locally reasonable. Drop the token and the guard silently
    narrows back. Reintroduce a read-side name containing `emit` and the next
    person to run the structural test meets a failure on a legitimate attribute,
    where the tempting fix is to drop the token rather than rename the
    attribute -- so this test says which one to do.
    """
    assert "emit" in WRITE_NAME_TOKENS, (
        "`emit` was removed from WRITE_NAME_TOKENS, so a source that writes back "
        "through `def emit(...)` is unguarded again. If it was removed because a "
        "read-side attribute collided with it, rename that attribute instead -- "
        "`recon.adapters.faults.FaultInjectingAdapter.records_handed_over` is the "
        "worked example"
    )

    adapter = FaultInjectingAdapter(mode="ok", records=stub_records(1))
    colliding = {name for name in set(dir(adapter)) | set(vars(adapter)) if "emit" in name.lower()}
    assert colliding == set(), (
        f"{sorted(colliding)} collides with the `emit` token and would fail the "
        "structural no-write test on an attribute that is not a write. Name it "
        "for the read it counts, as `records_handed_over` is; do not drop the token"
    )
    assert isinstance(adapter, ReadOnlyAdapter)
    assert adapter.records_handed_over == 0, (
        "the read-side counter was deleted rather than renamed, so nothing counts "
        "a *partial* read any more (tests/ingest/test_bounded_failure.py reads it)"
    )
