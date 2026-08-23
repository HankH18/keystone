"""The shallow-merge trap, and "never to sources" as an executed assertion.

`entities.current` is a flat object with ONE nested object in it: `survived`,
whose nine keys are `recon.resolve.SURVIVED_PATHS`. Migration 0007's KS010 pins
the canonical write to `OLD.current || action->'set'`, and `||` is a **shallow**
merge -- so an action that writes `{"survived": {"one.field": v}}` does not
update one survived field, it replaces the whole map and erases the other eight.

The committed fix templates do not do this today (they write source-qualified
paths, which land as top-level keys), and that is asserted below rather than
assumed. `recon.apply.merge_preview` is the guard for the next template.
"""

from __future__ import annotations

import pytest

from recon.apply import (
    WRITABLE_TABLES,
    assert_sources_are_unwritable,
    merge_preview,
    source_tree_digest,
)
from recon.resolve import SURVIVED_PATHS

CURRENT = {
    "person_key": "abc",
    "registered": True,
    "survived": {path: f"value-of-{path}" for path in SURVIVED_PATHS},
}


def test_a_top_level_write_erases_nothing() -> None:
    """What every committed template actually does: add or replace a scalar key."""
    preview = merge_preview(CURRENT, {"appdb.enrollment.crm_deal_id": None})
    assert preview.safe
    assert preview.erased == ()


def test_writing_one_survived_field_erases_its_siblings() -> None:
    """The trap, demonstrated on the real nine-key map."""
    preview = merge_preview(CURRENT, {"survived": {"crm.contact.email": "new@example.test"}})
    assert not preview.safe
    assert set(preview.erased) == {
        f"survived.{path}" for path in SURVIVED_PATHS if path != "crm.contact.email"
    }
    assert len(preview.erased) == len(SURVIVED_PATHS) - 1 == 8


def test_carrying_the_whole_map_is_safe() -> None:
    """The correct way to write one survived field: carry all nine."""
    whole = dict(CURRENT["survived"])
    whole["crm.contact.email"] = "new@example.test"
    preview = merge_preview(CURRENT, {"survived": whole})
    assert preview.safe


def test_a_scalar_replacing_an_object_is_the_WORST_erasure_not_a_safe_one() -> None:
    """The guard's bypass, now closed. **This assertion used to say the opposite.**

    The previous version of this test asserted
    `merge_preview(CURRENT, {"survived": "not-an-object"}).safe` -- that a scalar
    replacing the nine-key map is reported safe -- with a docstring explaining
    that the guard "answers one question, which nested keys disappear, and
    nothing else". It was not describing a limitation; it was pinning a hole. The
    old `merge_preview` required BOTH sides to be Mappings before it looked, so
    `{"set": {"survived": "wiped"}}` reported no erasure, `apply_proposal` took
    the write, and a nine-key nested object became the string `"wiped"` -- a
    strictly larger loss than the object-over-object case the guard was built
    for, admitted because the destruction was more total.

    The guard now keys on the SHAPE CHANGE -- `survived` ceasing to be an object
    -- rather than on both sides being maps. All nine keys are erased and the key
    itself is reported `collapsed`.
    """
    preview = merge_preview(CURRENT, {"survived": "not-an-object"})
    assert not preview.safe
    assert preview.collapsed == ("survived",)
    assert set(preview.erased) == {f"survived.{path}" for path in SURVIVED_PATHS}
    assert len(preview.erased) == len(SURVIVED_PATHS) == 9


@pytest.mark.parametrize(
    "new_value",
    [
        pytest.param("wiped", id="string"),
        pytest.param(0, id="zero"),
        pytest.param(False, id="false"),
        pytest.param(None, id="null"),
        pytest.param([], id="empty_array"),
        pytest.param(["a"], id="array"),
    ],
)
def test_every_non_object_value_collapses_the_nested_map(new_value: object) -> None:
    """The guard is about the shape, so it must hold for every non-object value.

    `None` and `0` and `False` matter specifically: a guard written as
    `if new_value and not isinstance(new_value, Mapping)` would let the falsy
    ones through, and `{"survived": null}` erases the map exactly as `"wiped"`
    does.
    """
    preview = merge_preview(CURRENT, {"survived": new_value})
    assert not preview.safe
    assert preview.collapsed == ("survived",)


def test_an_empty_nested_map_replaced_by_a_scalar_is_still_unsafe() -> None:
    """`collapsed` and not only `erased`, because an empty map erases no sub-key.

    Reported through `collapsed` rather than through `erased`, so the shape
    change is refused even where there is no named sibling to lose. Without this
    the guard would be "count the sub-keys" wearing the clothes of a shape check.
    """
    preview = merge_preview({"survived": {}}, {"survived": "wiped"})
    assert preview.erased == ()
    assert preview.collapsed == ("survived",)
    assert not preview.safe


def test_a_scalar_replacing_a_scalar_is_still_safe() -> None:
    """The control. The guard must not have become "refuse every write"."""
    assert merge_preview(CURRENT, {"person_key": "def"}).safe
    assert merge_preview(CURRENT, {"registered": False}).safe
    assert merge_preview(CURRENT, {"a.new.key": "v"}).safe


# =====================================================================================
# the third arm: a member the nested map did NOT have
# =====================================================================================


@pytest.mark.parametrize(
    "spoof",
    [
        pytest.param("CRM.contact.email", id="upper_case"),
        pytest.param("Crm.Contact.Email", id="title_case"),
        pytest.param("crm.contact.email ", id="trailing_space"),
        pytest.param(" crm.contact.email", id="leading_space"),
        pytest.param("crm.contact.ema\u0131l", id="dotless_i_homoglyph"),
        pytest.param("crm.contact.\u0435mail", id="cyrillic_e_homoglyph"),
    ],
)
def test_a_member_the_map_did_not_have_is_reported_introduced(spoof: str) -> None:
    """`safe` is not "erases nothing": ADDING to `survived` is its own hole.

    `survived`'s membership is the closed set `SURVIVED_PATHS`, and the entity
    endpoints project the map WHOLE -- every member it happens to contain. An
    action that carries all nine genuine members and adds a tenth whose key
    differs from a real one only by case, by surrounding whitespace or by a
    unicode homoglyph destroys nothing and erases nothing, so the erasure guard
    is silent on it. What a reader then sees is the attacker's value beside the
    genuine one, under a name a human reads as the genuine one.
    """
    assert spoof not in SURVIVED_PATHS
    preview = merge_preview(CURRENT, {"survived": {**CURRENT["survived"], spoof: "attacker"}})
    assert preview.erased == ()
    assert preview.collapsed == ()
    assert preview.introduced == (f"survived.{spoof}",)
    assert not preview.safe


def test_carrying_exactly_the_members_that_exist_introduces_nothing() -> None:
    """The control: the only representable nested fix is still safe.

    Contract SS5 forces a nested fix to carry the WHOLE map, so if carrying it
    counted as introducing anything, `survived` could never be fixed at all and
    every assertion above would be passing vacuously.
    """
    carried = {**CURRENT["survived"], "crm.contact.lifecycle_stage": "customer"}
    preview = merge_preview(CURRENT, {"survived": carried})
    assert preview.introduced == ()
    assert preview.safe


def test_introducing_a_member_into_a_key_that_holds_no_object_is_not_this_guard() -> None:
    """The honest boundary: this is a rule about ADDING to an object that exists.

    Turning a key that held no object into one introduces no sibling to be
    confused with, so it is judged by R24's write-set gate -- which sees every
    member such an action carries as a written path -- and not here.
    """
    preview = merge_preview({"person_key": "abc"}, {"survived": {"anything": 1}})
    assert preview.introduced == ()
    assert preview.safe


def test_the_apply_path_writes_exactly_one_table() -> None:
    """R24: "applies only to Keystone's canonical layer -- never to sources"."""
    assert {"entities"} == WRITABLE_TABLES


def test_no_source_adapter_can_be_written_to() -> None:
    """The other half of "never to sources", executed rather than asserted in prose.

    **The names are the evidence.** `build_adapters` returns a *dict*, and the
    previous version of this function iterated it directly -- which yields its
    KEYS -- so it introspected the three strings `"crm"`, `"appdb"`,
    `"payments"`, found no `str` attribute containing a write token, and returned
    `("str", "str", "str")`. Every assertion here passed and nothing had been
    inspected. Asserting the returned CLASS NAMES, rather than merely that there
    are three of them, is what makes that failure impossible to repeat.
    """
    checked = assert_sources_are_unwritable()
    assert checked == ("AppDbAdapter", "CrmAdapter", "PaymentsAdapter"), (
        f"assert_sources_are_unwritable() inspected {checked}. Anything other than "
        "the three adapter classes means it walked over something that is not an "
        "adapter -- which is how it came to check three strings and pass"
    )
    for name in checked:
        assert name != "str"


def test_the_real_adapters_are_the_ones_build_adapters_returns() -> None:
    """The names above are not a hand-written list: they are the built objects'."""
    from recon.adapters import build_adapters

    built = build_adapters(None)
    assert isinstance(built, dict), (
        "build_adapters no longer returns a dict; the unwrapping in "
        "recon.apply._adapter_objects is written for one"
    )
    assert assert_sources_are_unwritable() == tuple(
        sorted(type(adapter).__name__ for adapter in built.values())
    )


def test_the_assertion_would_fail_on_a_writable_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sabotage: give an adapter a write method and the assertion must go red."""

    class WritableSource:
        source_id = "rogue"

        def generations(self) -> tuple[int, ...]:  # pragma: no cover - never called
            return ()

        def read(self, generation: int) -> object:  # pragma: no cover - never called
            raise NotImplementedError

        def write_back(self, record: object) -> None:  # pragma: no cover - never called
            raise NotImplementedError

    import recon.adapters as adapters_module

    monkeypatch.setattr(adapters_module, "build_adapters", lambda _root=None: [WritableSource()])
    with pytest.raises(AssertionError, match="looks like a write method"):
        assert_sources_are_unwritable()


def test_the_assertion_catches_a_write_bound_onto_the_INSTANCE(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer handed in at construction carries no such attribute on any class.

    `self.save_to = sink.save` appears nowhere in an MRO, so a check that walked
    only `cls.__mro__` would report the adapter clean. The instance dict is
    inspected too.
    """

    class SmuggledWriter:
        source_id = "rogue"

        def __init__(self) -> None:
            self.upsert_hook = lambda record: None

        def generations(self) -> tuple[int, ...]:  # pragma: no cover - never called
            return ()

        def read(self, generation: int) -> object:  # pragma: no cover - never called
            raise NotImplementedError

    import recon.adapters as adapters_module

    monkeypatch.setattr(adapters_module, "build_adapters", lambda _root=None: [SmuggledWriter()])
    with pytest.raises(AssertionError, match="looks like a write method"):
        assert_sources_are_unwritable()


def test_the_assertion_refuses_to_inspect_anything_that_is_not_an_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression test for the bug itself: strings must not pass for adapters.

    This is what the shipped function actually did -- it was handed the dict's
    keys. Reproduced here by handing it the keys directly. It must refuse rather
    than sail through them and report success.
    """
    import recon.adapters as adapters_module

    monkeypatch.setattr(
        adapters_module, "build_adapters", lambda _root=None: ["crm", "appdb", "payments"]
    )
    with pytest.raises(AssertionError, match="is not a source adapter"):
        assert_sources_are_unwritable()


def test_the_assertion_refuses_an_empty_adapter_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loop over nothing is a pass that proves nothing, so it is a failure."""
    import recon.adapters as adapters_module

    monkeypatch.setattr(adapters_module, "build_adapters", lambda _root=None: {})
    with pytest.raises(AssertionError, match="inspect nothing"):
        assert_sources_are_unwritable()


def test_the_fixture_tree_digest_names_every_source_file() -> None:
    """`source_tree_digest` measures the tree; here it is measured to be non-vacuous.

    The before/after comparison across a real committed apply lives in
    `test_apply_lifecycle.py` (it needs a database and a proposal). This asserts
    the instrument itself has something to say -- a digest of an empty mapping
    would make that comparison pass without observing anything.
    """
    digests = source_tree_digest()
    assert len(digests) >= 3, digests
    assert all(len(value) == 64 for value in digests.values())
    assert any(name.endswith(".jsonl") for name in digests), sorted(digests)


def test_the_fixture_tree_digest_notices_a_changed_byte(tmp_path: object) -> None:
    """Sabotage the instrument: change one byte and the digest must differ.

    Run on a throwaway tree, never on `fixtures/` -- the committed tree is a
    graded artifact and a test that edited it would be the very write R24
    forbids.
    """
    from pathlib import Path

    root = Path(str(tmp_path))
    (root / "sub").mkdir()
    target = root / "sub" / "crm.jsonl"
    target.write_text('{"a": 1}\n')
    before = source_tree_digest(root)
    assert list(before) == ["sub/crm.jsonl"]

    target.write_text('{"a": 2}\n')
    after = source_tree_digest(root)
    assert after != before, "the digest did not notice a changed byte"


def test_the_assertion_names_a_slotted_adapter_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `__slots__` adapter used to raise `TypeError`, not the named AssertionError.

    `vars(obj)` raises `TypeError: vars() argument must have __dict__ attribute`
    on an instance whose class defines `__slots__` and no `__dict__`, and the
    instance-namespace walk called it directly. So the one adapter shape that
    most looks like a deliberate attempt to hide an attribute made R24's "never
    to sources" assertion **crash** rather than judge -- and a crash is not a
    refusal a reader can act on: it names the wrong rule, in the wrong file, with
    no offending attribute in the message.

    The slot itself is still caught, because a slot is a data descriptor on the
    CLASS and the MRO walk sees it.
    """

    class SlottedWriter:
        __slots__ = ("source_id", "write_back")

        def __init__(self) -> None:
            self.source_id = "rogue"
            self.write_back = None

        def generations(self) -> tuple[int, ...]:  # pragma: no cover - never called
            return ()

        def read(self, generation: int) -> object:  # pragma: no cover - never called
            raise NotImplementedError

    import recon.adapters as adapters_module

    monkeypatch.setattr(adapters_module, "build_adapters", lambda _root=None: [SlottedWriter()])
    with pytest.raises(AssertionError, match="looks like a write method"):
        assert_sources_are_unwritable()


def test_a_clean_slotted_adapter_still_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-op control: without it the test above could pass on the TypeError.

    A slotted adapter carrying nothing write-shaped must be ACCEPTED and its
    class name returned. `TypeError` is not an `AssertionError`, so a regression
    to `vars(adapter)` turns this red rather than leaving it silently green.
    """

    class SlottedReader:
        __slots__ = ("source_id",)

        def __init__(self) -> None:
            self.source_id = "clean"

        def generations(self) -> tuple[int, ...]:  # pragma: no cover - never called
            return ()

        def read(self, generation: int) -> object:  # pragma: no cover - never called
            raise NotImplementedError

    import recon.adapters as adapters_module

    monkeypatch.setattr(adapters_module, "build_adapters", lambda _root=None: [SlottedReader()])
    assert assert_sources_are_unwritable() == ("SlottedReader",)
