"""The write set is a set of PATHS: nested writes, in the gate and in the database.

The hole this file is about
----------------------------
`entities.current` is a flat object that carries one nested object, `survived`
(`recon.resolve.SURVIVED_PATHS`), whose nine members are themselves
source-qualified contract paths -- six of them in contract SS6's
`SENSITIVE_FIELDS`. So an action of

    {"set": {"survived": {<all nine, with crm.contact.email,
                          appdb.student.status and appdb.enrollment.stage
                          replaced>}}}

has exactly ONE top-level key, `survived`, which is on neither committed list.
A gate reading top-level keys judges that key; `jsonb_exists_any` over the
top-level keys (migration 0012) sees nothing; `KS002` sees `sensitive = false`
and is satisfied. What the statement WRITES is three sensitive paths. Judging the
key list is the same shape of mistake as judging the conflict's classification
instead of the write, one level down.

Everything here is STRUCTURAL: each test constructs the row it is about, so no
seed change can make it vacuous, and each half carries the no-op control that
keeps the refusals from passing by refusing everything.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from recon.apply import (
    AUTO_APPLY_ACTOR,
    AUTO_APPLY_CASE_TYPES,
    EVIDENCE_SCHEMA,
    ProposalRecord,
    RollbackPath,
    auto_apply_decision,
    effective_write_paths,
    write_set_gate,
)
from recon.reference import AUTO_APPLY_ELIGIBLE, SENSITIVE_FIELDS
from recon.resolve import SURVIVED_PATHS, VIEW_FIELDS

OPEN_PATH = RollbackPath(known=True, detail="deliberately open", entity_exists=True)

COMPLETE_EVIDENCE = {
    "schema": EVIDENCE_SCHEMA,
    "completeness": {"incomplete_sources": [], "null_observed_values": []},
    "confidence": {"signals": {"partial_evidence": False, "partial_evidence_reasons": []}},
}

#: A canonical row shaped like a real one: the nested `survived` map with all
#: nine contract paths carrying a value. Written out rather than read from the
#: store so the attacks below exist whatever the seed does.
CURRENT = {
    "person_key": "00000000-0000-4000-8000-000000000001",
    "survived": {path: f"value-of-{path}" for path in SURVIVED_PATHS},
}

#: The three sensitive members the audit's proposal replaced.
REPLACED = (
    "crm.contact.email",
    "appdb.student.status",
    "appdb.enrollment.stage",
)


def nested_action(**replacements: Any) -> dict[str, Any]:
    """`{"set": {"survived": <the whole map, with `replacements` applied>}}`.

    The whole map, because contract SS5's shallow-merge rule requires it: `||`
    replaces a nested object wholesale, so a fix that carried only the member it
    changes would erase the other eight and `apply_proposal` refuses it
    (`shallow_merge_would_erase`). Carrying the map is therefore not a disguise
    the attacker chose -- it is the only representable form of a nested write.
    """
    survived = dict(CURRENT["survived"])
    survived.update(replacements)
    return {"set": {"survived": survived}}


def record(**overrides: Any) -> ProposalRecord:
    """A C6 lifecycle-only proposal that passes every condition, minus overrides.

    C6 with a `lifecycle` disagreement classifies ELIGIBLE (contract SS6: "C6
    lifecycle-only -> `crm.contact.lifecycle_stage`, eligible, CRM side only"),
    so nothing below is refused by the classification gate -- which is what makes
    every refusal here a statement about the write set.
    """
    base = {
        "id": 11,
        "conflict_id": 5,
        "conflict_type": "C6",
        "fingerprint": "fp-nested",
        "disagreeing_fields": ("crm.contact.lifecycle_stage", "appdb.student.status"),
        "action": nested_action(**{"crm.contact.lifecycle_stage": "customer"}),
        "confidence": Decimal("1.0000"),
        "evidence": COMPLETE_EVIDENCE,
        "status": "approved",
        "sensitive": False,
        "target_canonical_id": "00000000-0000-4000-8000-000000000001",
    }
    base.update(overrides)
    return ProposalRecord(**base)  # type: ignore[arg-type]


def reasons(decision: Any) -> list[str]:
    return [check.name for check in decision.failed]


# =====================================================================================
# the gate: what does this statement effectively write?
# =====================================================================================


def test_the_nested_attack_is_refused_as_a_sensitive_write() -> None:
    """The audit's own proposal: one innocent key, three sensitive fields replaced."""
    hostile = record(action=nested_action(**{path: "replaced" for path in REPLACED}))

    # The premises, asserted so this cannot pass for the wrong reason.
    assert list(hostile.assignments) == ["survived"], "the attack must present ONE key"
    assert "survived" not in SENSITIVE_FIELDS and "survived" not in AUTO_APPLY_ELIGIBLE
    assert all(path in SENSITIVE_FIELDS for path in REPLACED)

    decision = auto_apply_decision(hostile, OPEN_PATH, CURRENT)
    assert not decision.allowed, decision.detail
    assert decision.reason == "sensitive_write", decision.detail
    assert reasons(decision) == ["write_set_eligible"]
    for path in REPLACED:
        assert path in decision.detail, f"the refusal does not name {path}"


def test_judging_the_top_level_keys_admits_the_same_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: put the old rule back and the attack lands.

    Without this, the refusal above could be coming from any of R24's nine
    conditions and `effective_write_paths` could be a no-op. Here the ONLY thing
    replaced is the definition of "what does this write?" -- restored to the
    top-level key list the gate shipped with -- and the same proposal is
    ADMITTED. That is the defect, reproduced, and it is what the new function is
    measured against.
    """
    from recon import apply as apply_module

    def top_level_keys_only(assignments: Any, current: Any = None) -> tuple[Any, ...]:
        return tuple(apply_module.WritePath(key) for key in sorted(assignments))

    monkeypatch.setattr(apply_module, "effective_write_paths", top_level_keys_only)
    hostile = record(action=nested_action(**{path: "replaced" for path in REPLACED}))
    decision = auto_apply_decision(hostile, OPEN_PATH, CURRENT)
    assert not decision.allowed, (
        "the sabotaged gate still refused, so the refusal in the test above is not "
        "coming from effective_write_paths and this file proves nothing"
    )
    assert decision.reason == "write_off_allowlist", (
        "under the old rule the row is refused for the WRONG reason -- the key "
        f"`survived` being unlisted -- not for writing {list(REPLACED)}"
    )
    assert "crm.contact.email" not in decision.detail, (
        "the old rule cannot name the sensitive paths, because it never saw them"
    )


@pytest.mark.parametrize("path", sorted(SENSITIVE_FIELDS))
@pytest.mark.parametrize("conflict_type", sorted(AUTO_APPLY_CASE_TYPES))
def test_no_approved_case_type_may_nest_any_sensitive_path(conflict_type: str, path: str) -> None:
    """Every (approved type x sensitive path) pair again, one level down.

    `tests/apply/test_gate.py` runs this cross product as top-level keys. The
    same 60 attacks are run here THROUGH the nested container, because "the gate
    refuses X as a key" and "the gate refuses X as a path" were two different
    facts and only the first one was true.
    """
    hostile = record(
        conflict_type=conflict_type,
        disagreeing_fields=(),
        action={"set": {"survived": {**CURRENT["survived"], path: "replaced"}}},
    )
    decision = auto_apply_decision(hostile, OPEN_PATH, CURRENT)
    assert not decision.allowed, f"{conflict_type} nesting {path} was admitted"
    assert decision.reason == "sensitive_write", decision.detail
    assert path in decision.detail


def test_a_member_carried_through_unchanged_is_not_a_write() -> None:
    """The control the refusals above are measured against, and the demo's premise.

    Contract SS5 forces a nested fix to carry the whole map, and six of the nine
    members are sensitive. If carrying counted as writing, `survived` could never
    be fixed at all and every negative above would be passing vacuously. So the
    same shape, changing ONLY the eligible member, must be ADMITTED.
    """
    admitted = record(action=nested_action(**{"crm.contact.lifecycle_stage": "customer"}))
    paths = effective_write_paths(admitted.assignments, CURRENT)
    assert [path.display for path in paths] == ["survived->crm.contact.lifecycle_stage"]
    decision = auto_apply_decision(admitted, OPEN_PATH, CURRENT)
    assert decision.allowed, decision.detail


def test_dropping_a_member_counts_as_writing_it() -> None:
    """`||` replaces the map, so an omitted member is an erasure, not a carry."""
    truncated = {"set": {"survived": {"crm.contact.lifecycle_stage": "customer"}}}
    verdict = write_set_gate(truncated["set"], CURRENT)
    assert not verdict.cleared
    assert verdict.reason == "sensitive_write"
    assert "crm.contact.email" in verdict.detail


def test_without_the_canonical_row_every_member_counts() -> None:
    """`current=None` is the CONSERVATIVE input, never the permissive one."""
    action = nested_action(**{"crm.contact.lifecycle_stage": "customer"})
    verdict = write_set_gate(action["set"], None)
    assert not verdict.cleared
    assert verdict.reason == "sensitive_write"
    # ...and the same call WITH the row clears, so the difference is the row.
    assert write_set_gate(action["set"], CURRENT).cleared


@pytest.mark.parametrize("path", sorted(SENSITIVE_FIELDS))
def test_a_top_level_sensitive_write_is_refused_even_when_it_changes_nothing(path: str) -> None:
    """The edge the asymmetry creates, closed: naming it is writing it.

    A nested member is judged by whether the merge CHANGES it, because contract
    SS5 leaves its author no way to omit it. A top-level key is not: its author
    could simply have left it out, so it is written whenever it is named. Without
    that asymmetry an attacker could assign a sensitive path the exact value the
    row already holds, clear R15's gate, and then be one committed write away
    from a row on which the same action is no longer a no-op -- and T-11b's
    `sensitive_never_auto_applies` would have been traded for the nested fix.
    """
    current = {path: "the-value-already-there"}
    verdict = write_set_gate({path: "the-value-already-there"}, current)
    assert not verdict.cleared, f"{path} assigned its own current value was admitted"
    assert verdict.reason == "sensitive_write"
    assert verdict.paths == (path,)


def test_a_nested_write_that_changes_nothing_names_the_container() -> None:
    """A no-op nested write is refused, and the refusal names something real."""
    verdict = write_set_gate({"survived": dict(CURRENT["survived"])}, CURRENT)
    assert not verdict.cleared
    assert verdict.reason == "write_off_allowlist"
    assert verdict.paths == ("survived",)


def test_a_doubly_nested_write_is_refused_by_the_allowlist_not_inspected() -> None:
    """The honest boundary of "nested": one level, and everything below it refused.

    `entities.current` has exactly one nested level, so `effective_write_paths`
    descends exactly one. A write two levels down, carrying the whole map so
    nothing is erased, presents the leaf `x` -- an object -- which is on neither
    committed list, so it is refused by the allow-list arm rather than
    understood. Pinned as a test because "at any depth" is a claim this code does
    not support and the documentation must not make.
    """
    action = {"survived": {**CURRENT["survived"], "x": {"crm.contact.email": "attacker@evil.test"}}}
    verdict = write_set_gate(action, CURRENT)
    assert not verdict.cleared
    assert verdict.reason == "write_off_allowlist"
    assert verdict.paths == ("survived->x",)


def test_the_truncated_doubly_nested_variant_is_refused_more_strongly() -> None:
    """...and the version that does not carry the map is a sensitive write outright.

    Dropping the eight siblings erases six sensitive paths, so the stronger
    refusal fires first. Both variants are refused; only the reason differs, and
    naming the right one is the point of having named refusals.
    """
    action = {"survived": {"x": {"crm.contact.email": "attacker@evil.test"}}}
    verdict = write_set_gate(action, CURRENT)
    assert not verdict.cleared
    assert verdict.reason == "sensitive_write"

    from recon.apply import merge_preview

    preview = merge_preview(CURRENT, action)
    assert not preview.safe
    assert "survived.crm.contact.email" in preview.erased


#: Every non-object a top-level key can be assigned. Each one REPLACES the nested
#: map wholesale under `||`, so each one erases all nine members -- six of them in
#: `SENSITIVE_FIELDS`. The old rule descended only when the ASSIGNED value was an
#: object, so every one of these reported the single unlisted leaf `survived` and
#: the six erasures were invisible to the gate and to `KS013`.
NON_OBJECT_VALUES = [
    pytest.param("wiped", id="scalar"),
    pytest.param(7, id="number"),
    pytest.param(True, id="boolean"),
    pytest.param(None, id="json_null"),
    pytest.param([], id="empty_list"),
    pytest.param(["crm.contact.email"], id="list"),
]


@pytest.mark.parametrize("value", NON_OBJECT_VALUES)
def test_replacing_the_map_with_a_non_object_is_a_SENSITIVE_write(value: Any) -> None:
    """The blocker: the write set is a rule about the VALUE, not about the shape.

    `{"set": {"survived": <anything that is not an object>}}` destroys strictly
    MORE than the attack the nested gate was built for -- it erases all nine
    members rather than replacing three -- and the previous rule reported it as
    one unlisted key because it only descended into an assigned OBJECT. Refusing
    it as `write_off_allowlist` was the right outcome for the wrong reason, and
    the reason is what `KS013` and the reader of a refusal both act on.

    Every shape below now falls out of ONE comparison: the merged row against the
    row as it stands.
    """
    verdict = write_set_gate({"survived": value}, CURRENT)
    assert not verdict.cleared
    assert verdict.reason == "sensitive_write", verdict.detail
    erased = {path for path in SURVIVED_PATHS if path in SENSITIVE_FIELDS}
    assert erased, "no sensitive member in the survived map; this test is vacuous"
    assert set(verdict.leaves) >= erased | {"survived"}
    for path in erased:
        assert f"survived->{path}" in verdict.sensitive_paths
    # ...and the key itself is still reported, because its author NAMED it.
    assert "survived" in verdict.paths


@pytest.mark.parametrize("value", NON_OBJECT_VALUES)
def test_the_same_shapes_are_refused_without_the_canonical_row_too(value: Any) -> None:
    """`current=None` cannot make any of them ADMITTED -- only differently refused.

    With no row to diff against there is no map to erase, so the write set is the
    named key alone. That is the conservative direction: it is refused by the
    allow-list arm instead of by R15's, and nothing is admitted.
    """
    verdict = write_set_gate({"survived": value}, None)
    assert not verdict.cleared
    assert verdict.reason == "write_off_allowlist"
    assert verdict.paths == ("survived",)


# =====================================================================================
# the write must be the conflict type's COMMITTED fix target
# =====================================================================================


def test_an_approved_type_may_not_write_another_types_eligible_field() -> None:
    """C2 -> `crm.contact.grade`: every old condition held, and it auto-applied.

    `crm.contact.grade` is on the allow-list and is not sensitive, so neither R15
    gate objects; C2 is an approved case type, so condition 3 does not either.
    What is wrong with the row is that C2's committed template writes
    `payments.payment.external_ref` and nothing else (contract SS6's fix-target
    table). An eligible path the template does not write is still a re-targeting
    -- it is how a conflict of one type acquires another type's fix.
    """
    hostile = record(
        conflict_type="C2",
        disagreeing_fields=(),
        action={"set": {"crm.contact.grade": "7"}},
    )
    decision = auto_apply_decision(hostile, OPEN_PATH, CURRENT)
    assert not decision.allowed, decision.detail
    assert reasons(decision) == ["write_matches_fix_target"]
    assert "payments.payment.external_ref" in decision.detail


def test_the_committed_template_itself_is_admitted() -> None:
    """The control: C2 writing its OWN target passes the same check."""
    decision = auto_apply_decision(
        record(
            conflict_type="C2",
            disagreeing_fields=(),
            action={"set": {"payments.payment.external_ref": "ext-1"}},
        ),
        OPEN_PATH,
        CURRENT,
    )
    assert decision.allowed, decision.detail


def test_the_c4_retargeting_escape_is_refused_by_the_classifier_first() -> None:
    """Contract SS6/SS12 D-7, and WHICH condition refuses it.

    A C4 re-pointed at `crm.contact.external_id` carries `sensitive = false` and
    an eligible write set, so neither 0012 nor the write-set gate objects. What
    refuses it is the SENSITIVITY gate: `FIX_TARGETS['C4']` pins
    `crm.contact.email`, which is sensitive, so the classification holds the
    proposal before the action is read at all. (`docs/proposal-policy.md` SS8.4
    used to name condition 3, the approved-case-type check, which is a second
    reason but not the one that fires.)
    """
    decision = auto_apply_decision(
        record(
            conflict_type="C4",
            disagreeing_fields=(),
            action={"set": {"crm.contact.external_id": "x-1"}},
        ),
        OPEN_PATH,
        CURRENT,
    )
    assert not decision.allowed
    assert decision.reason == "sensitive_hold"
    assert reasons(decision) == ["not_sensitive"], (
        "the refusal came from something after the sensitivity gate; R15 must return first"
    )


# =====================================================================================
# migration 0013: the database judges the same thing
# =====================================================================================

KS013 = "KS013"
KS014 = "KS014"

_ENTITY_WITH_SURVIVED = text(
    """
    SELECT e.canonical_id::text AS canonical_id, e.current
      FROM entities e
     WHERE jsonb_typeof(e.current -> 'survived') = 'object'
     ORDER BY e.canonical_id
     LIMIT 1
    """
)

_INSERT_PROPOSAL = text(
    """
    INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence,
                           status, sensitive, created_run, target_canonical_id)
    SELECT c.id, :fingerprint, CAST(:action AS jsonb), 0.99, '{}'::jsonb,
           CAST(:status AS proposal_status), :sensitive, 'nested-write-set-probe',
           CAST(:canonical_id AS uuid)
      FROM conflicts c ORDER BY c.fingerprint LIMIT 1
    RETURNING id
    """
)

_INSERT_EVENT = text(
    """
    INSERT INTO proposal_events (proposal_id, canonical_id, event, before, after, actor)
    SELECT p.id, CAST(:canonical_id AS uuid), 'applied',
           CAST(:before AS jsonb), CAST(:after AS jsonb), :actor
      FROM proposals p WHERE p.id = :proposal_id
    RETURNING id
    """
)


def _sqlstate(error: BaseException) -> str | None:
    original = getattr(error, "orig", error)
    if isinstance(original, psycopg.Error):
        return original.sqlstate
    return None  # pragma: no cover - every raise here is a psycopg error


@pytest.fixture
def recon_conn(store: Any) -> Any:
    """A `recon_writer` connection that is ROLLED BACK. The proposing role."""
    from recon.db import ROLE_RECON_WRITER, role_connection

    with role_connection(ROLE_RECON_WRITER, commit=False) as conn:
        yield conn


@pytest.fixture(scope="session")
def canonical_row(store: Any, reader: Any) -> tuple[str, dict[str, Any]]:
    """A REAL canonical row and its REAL nine-member `survived` map."""
    with reader.connect() as conn:
        row = conn.execute(_ENTITY_WITH_SURVIVED).one()
    current = dict(row.current)
    assert set(current["survived"]) == set(SURVIVED_PATHS), (
        "the canonical row's survived map is not contract SS4.6's nine paths, so the "
        f"nested attacks below would not be about the real shape: {sorted(current['survived'])}"
    )
    return row.canonical_id, current


def _nested(current: dict[str, Any], **replacements: Any) -> str:
    survived = dict(current["survived"])
    survived.update(replacements)
    return json.dumps({"set": {"survived": survived}})


def test_the_database_refuses_the_nested_attack(
    recon_conn: Any, canonical_row: tuple[str, dict[str, Any]]
) -> None:
    """The row 0012's `jsonb_exists_any` over top-level keys still accepted."""
    canonical_id, current = canonical_row
    action = _nested(current, **{path: "replaced-by-the-attacker" for path in REPLACED})
    with pytest.raises(DBAPIError) as raised:
        recon_conn.execute(
            _INSERT_PROPOSAL,
            {
                "fingerprint": "nested-attack-probe",
                "action": action,
                "sensitive": False,
                "status": "pending",
                "canonical_id": canonical_id,
            },
        )
    recon_conn.rollback()
    assert _sqlstate(raised.value) == KS013
    assert "crm.contact.email" in str(raised.value)


@pytest.mark.parametrize("path", sorted(SENSITIVE_FIELDS & set(SURVIVED_PATHS)))
def test_the_database_refuses_every_nested_sensitive_member(
    recon_conn: Any, canonical_row: tuple[str, dict[str, Any]], path: str
) -> None:
    """Each sensitive member of `survived`, replaced on its own, refused by name."""
    canonical_id, current = canonical_row
    with pytest.raises(DBAPIError) as raised:
        recon_conn.execute(
            _INSERT_PROPOSAL,
            {
                "fingerprint": f"nested-attack-{path}",
                "action": _nested(current, **{path: "replaced"}),
                "sensitive": False,
                "status": "pending",
                "canonical_id": canonical_id,
            },
        )
    recon_conn.rollback()
    assert _sqlstate(raised.value) == KS013
    assert path in str(raised.value)


@pytest.mark.parametrize("value", NON_OBJECT_VALUES)
def test_the_database_refuses_replacing_the_map_with_a_non_object(
    recon_conn: Any, canonical_row: tuple[str, dict[str, Any]], value: Any
) -> None:
    """**The row migration 0013 still accepted, and 0014 refuses.**

    `{"set": {"survived": <non-object>}}` erases all nine members of the real
    canonical map, six of them in `SENSITIVE_FIELDS`. 0013's
    `keystone_effective_write_paths` descended only into an assigned OBJECT, so
    with `nested_only` (which is the half `KS013` asks for) it emitted NOTHING at
    all for this action, and 0012's key-level CHECK saw only the unlisted key
    `survived`. The most destructive form of the attack the trigger exists to
    stop was the one form it did not judge, and the proposal landed
    `sensitive = false, status = 'pending'`.
    """
    canonical_id, _current = canonical_row
    with pytest.raises(DBAPIError) as raised:
        recon_conn.execute(
            _INSERT_PROPOSAL,
            {
                "fingerprint": f"nested-collapse-{value!r}",
                "action": json.dumps({"set": {"survived": value}}),
                "sensitive": False,
                "status": "pending",
                "canonical_id": canonical_id,
            },
        )
    recon_conn.rollback()
    assert _sqlstate(raised.value) == KS013
    for path in sorted(SENSITIVE_FIELDS & set(SURVIVED_PATHS)):
        assert path in str(raised.value), f"the refusal does not name the erased {path}"


def test_a_held_proposal_may_still_collapse_the_map(
    recon_conn: Any, canonical_row: tuple[str, dict[str, Any]]
) -> None:
    """The control for the refusals above: KS013 judges the write SET, not the shape.

    Declared honestly (`sensitive = true`, born `sensitive_hold`) the identical
    action lands, because R15 forces human review rather than forbidding the fix.
    Without this the parametrized refusals could be satisfied by a trigger that
    rejected every non-object assignment outright.
    """
    canonical_id, _ = canonical_row
    landed = recon_conn.execute(
        _INSERT_PROPOSAL,
        {
            "fingerprint": "nested-collapse-held-probe",
            "action": json.dumps({"set": {"survived": "wiped"}}),
            "sensitive": True,
            "status": "sensitive_hold",
            "canonical_id": canonical_id,
        },
    ).scalar_one()
    recon_conn.rollback()
    assert landed


def test_dropping_the_trigger_makes_the_collapse_land_again(
    reader: Any, canonical_row: tuple[str, dict[str, Any]]
) -> None:
    """SABOTAGE for the shape 0014 added, inside a rolled-back transaction.

    Without it, "0014 closed the collapse hole" would rest on the row being
    refused by *something*, which is exactly how the 0013 entry in
    `docs/proposal-policy.md` SS8.7 came to name a control that did not exist.
    """
    canonical_id, _ = canonical_row
    parameters = {
        "fingerprint": "nested-collapse-sabotage",
        "action": json.dumps({"set": {"survived": "wiped"}}),
        "sensitive": False,
        "status": "pending",
        "canonical_id": canonical_id,
    }
    with reader.connect() as conn:
        with pytest.raises(DBAPIError) as raised:
            conn.execute(_INSERT_PROPOSAL, parameters)
        conn.rollback()
        assert _sqlstate(raised.value) == KS013
        try:
            conn.execute(text("DROP TRIGGER keystone_proposals_nested_write_set ON proposals"))
            landed = conn.execute(_INSERT_PROPOSAL, parameters).scalar_one()
            assert landed, "the row did not land with the trigger dropped"
        finally:
            conn.rollback()


def test_the_installed_function_is_the_0014_rule_and_not_the_0013_one(reader: Any) -> None:
    """A direct probe of the SQL, so "0014 is applied" is not inferred from a refusal.

    Under 0013 this call returned `{survived}`; under 0014 it returns the members
    the collapse erases. Asserted on the function rather than through a trigger,
    because a trigger refusal can come from anywhere and this cannot.
    """
    with reader.connect() as conn:
        paths = conn.execute(
            text(
                "SELECT keystone_effective_write_paths("
                '\'{"survived": "wiped"}\'::jsonb, '
                '\'{"survived": {"crm.contact.email": "a"}}\'::jsonb, true)'
            )
        ).scalar_one()
    assert sorted(paths) == ["crm.contact.email"], (
        "keystone_effective_write_paths still reports the 0013 answer for a "
        f"non-object replacing an object: {sorted(paths)}"
    )


def test_a_look_alike_member_is_refused_by_the_allowlist_and_never_admitted() -> None:
    """The reader-facing half: an ADDED member that impersonates a real one.

    `survived`'s membership is the closed set `SURVIVED_PATHS` and the entity
    endpoints project the map WHOLE, so a tenth member whose key differs from a
    genuine path only by case, by surrounding whitespace or by a unicode
    homoglyph is rendered right beside the genuine one, carrying an attacker's
    value under a name a human reads as real. Every one of them writes a leaf on
    NEITHER committed list, so R24's allow-list arm refuses it -- which is the
    whole point of eligibility being an allow-list rather than the complement of
    `SENSITIVE_FIELDS`.
    """
    genuine = "crm.contact.email"
    look_alikes = [
        "CRM.contact.email",
        "Crm.Contact.Email",
        f"{genuine} ",
        f" {genuine}",
        "crm.contact.ema\u0131l",
        "crm.contact.\u0435mail",
    ]
    for key in look_alikes:
        assert key != genuine
        assert key not in SENSITIVE_FIELDS
        assert key not in AUTO_APPLY_ELIGIBLE
        action = {"survived": {**CURRENT["survived"], key: "attacker@evil.test"}}
        paths = effective_write_paths(action, CURRENT)
        assert [path.display for path in paths] == [f"survived->{key}"]
        verdict = write_set_gate(action, CURRENT)
        assert not verdict.cleared, f"{key!r} was admitted"
        assert verdict.reason == "write_off_allowlist", verdict.detail

    # ...and the control: the SAME shape, adding nothing, is admitted.
    admitted = {"survived": {**CURRENT["survived"], "crm.contact.lifecycle_stage": "customer"}}
    assert write_set_gate(admitted, CURRENT).cleared


def test_the_manual_apply_path_refuses_a_look_alike_member(
    canonical_row: tuple[str, dict[str, Any]],
) -> None:
    """...and the arm R24's gate does NOT cover: a human pressing APPLY.

    A reviewer applying by hand is not behind R24's allow-list, so refusing the
    added member has to live on the statement BOTH apply paths go through.
    `merge_preview` reports it as `introduced` and `apply_proposal` raises
    `nested_member_introduced` -- the same placement, and the same reason, as
    contract SS5's `shallow_merge_would_erase` guard beside it.
    """
    from recon.apply import merge_preview

    _, current = canonical_row
    spoofed = {"survived": {**current["survived"], "CRM.contact.email": "attacker@evil.test"}}
    preview = merge_preview(current, spoofed)
    assert not preview.safe
    assert preview.erased == ()
    assert preview.collapsed == ()
    assert preview.introduced == ("survived.CRM.contact.email",)

    # the control: carrying exactly the nine members it already has is safe.
    carried = {"survived": {**current["survived"], "crm.contact.lifecycle_stage": "customer"}}
    assert merge_preview(current, carried).safe


def test_the_database_still_accepts_the_eligible_nested_write(
    recon_conn: Any, canonical_row: tuple[str, dict[str, Any]]
) -> None:
    """The control. The trigger refuses the write SET, not the nesting.

    Without this the refusals above would be satisfied by a trigger that rejected
    every action containing an object, which would delete the only observable
    auto-apply the contract has (SS6's `crm.contact.lifecycle_stage`, a member of
    `survived`).
    """
    canonical_id, current = canonical_row
    landed = recon_conn.execute(
        _INSERT_PROPOSAL,
        {
            "fingerprint": "nested-eligible-probe",
            "action": _nested(
                current, **{"crm.contact.lifecycle_stage": "a-different-lifecycle-value"}
            ),
            "sensitive": False,
            "status": "pending",
            "canonical_id": canonical_id,
        },
    ).scalar_one()
    recon_conn.rollback()
    assert landed


def test_a_held_proposal_may_still_carry_the_nested_sensitive_write(
    recon_conn: Any, canonical_row: tuple[str, dict[str, Any]]
) -> None:
    """R15 forces HUMAN review; it does not forbid the fix from existing."""
    canonical_id, current = canonical_row
    landed = recon_conn.execute(
        _INSERT_PROPOSAL,
        {
            "fingerprint": "nested-held-probe",
            "action": _nested(current, **{"crm.contact.email": "new@example.test"}),
            "sensitive": True,
            "status": "sensitive_hold",
            "canonical_id": canonical_id,
        },
    ).scalar_one()
    recon_conn.rollback()
    assert landed


def test_dropping_the_trigger_makes_the_nested_attack_land_again(
    reader: Any, canonical_row: tuple[str, dict[str, Any]]
) -> None:
    """SABOTAGE, inside a transaction that is rolled back (DDL included).

    Without this every refusal above could be coming from some other rule on the
    table and migration 0013 could be a no-op. Run as the schema OWNER because
    DROP TRIGGER is DDL -- which is also the honest statement of this backstop's
    scope: it binds every principal's ROWS, and the principal who writes
    migrations can remove it.
    """
    canonical_id, current = canonical_row
    action = _nested(current, **{"crm.contact.email": "attacker@evil.test"})
    parameters = {
        "fingerprint": "nested-sabotage-probe",
        "action": action,
        "sensitive": False,
        "status": "pending",
        "canonical_id": canonical_id,
    }

    with reader.connect() as conn:
        with pytest.raises(DBAPIError) as raised:
            conn.execute(_INSERT_PROPOSAL, parameters)
        conn.rollback()
        assert _sqlstate(raised.value) == KS013

        try:
            conn.execute(text("DROP TRIGGER keystone_proposals_nested_write_set ON proposals"))
            landed = conn.execute(_INSERT_PROPOSAL, parameters).scalar_one()
            assert landed, (
                "the row did not land even with the trigger dropped, so the refusals in "
                "this file are coming from something else"
            )
        finally:
            conn.rollback()

    with reader.connect() as conn:
        with pytest.raises(DBAPIError) as reraised:
            conn.execute(_INSERT_PROPOSAL, parameters)
        conn.rollback()
    assert _sqlstate(reraised.value) == KS013


# -------------------------------------------------------------------------------------
# KS014 -- the OTHER leg: an unattended write, judged by its own before/after
# -------------------------------------------------------------------------------------


@pytest.fixture
def apply_writer_conn(store: Any) -> Any:
    from recon.db import ROLE_APPLY_WRITER, role_connection

    with role_connection(ROLE_APPLY_WRITER, commit=False) as conn:
        yield conn


def _ledger_probe(
    conn: Any, canonical_row: tuple[str, dict[str, Any]], *, actor: str, **replacements: Any
) -> Any:
    canonical_id, current = canonical_row
    after = dict(current)
    after["survived"] = {**current["survived"], **replacements}
    assert after != current, (
        f"the probe's replacement {replacements} equals what the row already holds, so "
        "the ledger row would describe a write of nothing and KS014 would have nothing "
        "to judge"
    )
    proposal_id = conn.execute(text("SELECT id FROM proposals ORDER BY id LIMIT 1")).scalar_one()
    return conn.execute(
        _INSERT_EVENT,
        {
            "proposal_id": proposal_id,
            "canonical_id": canonical_id,
            "before": json.dumps(current),
            "after": json.dumps(after),
            "actor": actor,
        },
    ).scalar_one()


def test_an_unattended_write_that_moves_a_sensitive_path_is_refused(
    apply_writer_conn: Any, canonical_row: tuple[str, dict[str, Any]]
) -> None:
    """R15 asserted against the BYTES, with no join and no trust in the proposal row."""
    with pytest.raises(DBAPIError) as raised:
        _ledger_probe(
            apply_writer_conn,
            canonical_row,
            actor=AUTO_APPLY_ACTOR,
            **{"crm.contact.email": "attacker@evil.test"},
        )
    apply_writer_conn.rollback()
    assert _sqlstate(raised.value) == KS014
    assert "crm.contact.email" in str(raised.value)


def test_an_unattended_write_of_the_eligible_path_is_accepted(
    apply_writer_conn: Any, canonical_row: tuple[str, dict[str, Any]]
) -> None:
    """The control: the same actor, the same nesting, an eligible member."""
    landed = _ledger_probe(
        apply_writer_conn,
        canonical_row,
        actor=AUTO_APPLY_ACTOR,
        **{"crm.contact.lifecycle_stage": "a-different-lifecycle-value"},
    )
    apply_writer_conn.rollback()
    assert landed


def test_a_human_pressed_apply_is_not_refused_by_KS014(
    apply_writer_conn: Any, canonical_row: tuple[str, dict[str, Any]]
) -> None:
    """The documented scope: R15 forbids the MACHINE writing unattended.

    A reviewer may approve a `sensitive_hold` proposal and apply it, and that act
    is signed and attributed. `KS014` is keyed on the unattended actor precisely
    so it refuses the automation and not the human -- stated as a test because a
    trigger that also refused the human would silently delete contract SS6's
    entire C4 fix template.
    """
    from recon.apply import APPLY_ACTOR

    landed = _ledger_probe(
        apply_writer_conn,
        canonical_row,
        actor=APPLY_ACTOR,
        **{"crm.contact.email": "reviewed@example.test"},
    )
    apply_writer_conn.rollback()
    assert landed
    assert APPLY_ACTOR != AUTO_APPLY_ACTOR


def test_dropping_the_ledger_trigger_makes_the_sensitive_write_land(
    reader: Any, canonical_row: tuple[str, dict[str, Any]]
) -> None:
    """SABOTAGE for KS014, rolled back, DDL included."""
    with reader.connect() as conn:
        with pytest.raises(DBAPIError) as raised:
            _ledger_probe(
                conn,
                canonical_row,
                actor=AUTO_APPLY_ACTOR,
                **{"crm.deal.stage": "a-different-deal-stage"},
            )
        conn.rollback()
        assert _sqlstate(raised.value) == KS014

        try:
            conn.execute(text("DROP TRIGGER keystone_auto_apply_write_set ON proposal_events"))
            landed = _ledger_probe(
                conn,
                canonical_row,
                actor=AUTO_APPLY_ACTOR,
                **{"crm.deal.stage": "a-different-deal-stage"},
            )
            assert landed, "the event did not land even with the trigger dropped"
        finally:
            conn.rollback()


# -------------------------------------------------------------------------------------
# the frozen lists, and the code/database parity that makes two rules one rule
# -------------------------------------------------------------------------------------

_FUNCTION_DEF = text("SELECT pg_get_functiondef(CAST(:name AS regproc)) AS definition")


@pytest.mark.parametrize(
    "function",
    ["keystone_proposals_nested_write_set_check", "keystone_auto_apply_write_set_check"],
)
def test_the_installed_triggers_still_match_the_contract(reader: Any, function: str) -> None:
    """The drift alarm the frozen path list is paid for by (0012's, for 0013)."""
    with reader.connect() as conn:
        definition = conn.execute(_FUNCTION_DEF, {"name": function}).scalar_one()
    # The LARGEST `ARRAY[...]` literal in the function body is the frozen path
    # list; the small empty ones are `coalesce(array_agg(...), ARRAY[]::text[])`.
    # A trigger function's body is stored VERBATIM, so the literals read as the
    # migration wrote them -- `ARRAY['a', 'b']::text[]`, with no per-element cast
    # to anchor on (unlike `pg_get_constraintdef`, which re-renders and adds one;
    # that is what 0012's alarm keys off).
    arrays = re.findall(r"ARRAY\[(.*?)\]::text\[\]", definition, re.DOTALL)
    assert arrays, f"could not find the path array in {function}"
    enforced = max((set(re.findall(r"'((?:[^']|'')*)'", body)) for body in arrays), key=len)
    assert enforced == set(SENSITIVE_FIELDS), (
        f"{function} enforces a different sensitive-path set from the one the code "
        "classifies on.\n"
        f"  only in the database   : {sorted(enforced - set(SENSITIVE_FIELDS))}\n"
        f"  only in recon.reference: {sorted(set(SENSITIVE_FIELDS) - enforced)}"
    )


def test_the_frozen_auto_apply_actor_still_matches_the_code(reader: Any) -> None:
    """`KS014` is keyed on a literal actor; if the code renames it, the trigger sleeps."""
    with reader.connect() as conn:
        definition = conn.execute(
            _FUNCTION_DEF, {"name": "keystone_auto_apply_write_set_check"}
        ).scalar_one()
    assert f"'{AUTO_APPLY_ACTOR}'" in definition, (
        f"the trigger is keyed on an actor that is not recon.apply.AUTO_APPLY_ACTOR "
        f"({AUTO_APPLY_ACTOR!r}); every unattended write would pass it unexamined"
    )


#: **Every SHAPE**, on both sides, crossed. The 0013 version listed ten hand-picked
#: pairs and the one shape it happened to miss -- a non-object assigned OVER an
#: object -- was the hole migration 0014 exists to close. So the cases are
#: GENERATED from the value taxonomy instead of chosen: an absent key, a JSON
#: `null`, a boolean, a number, a string, an empty list, a list, an empty object,
#: and objects that add / drop / change / carry a member. A shape nobody thought of
#: cannot fall out of a cross product the way it fell out of a list.
PARITY_SHAPES: dict[str, Any] = {
    "absent": ...,
    "null": None,
    "false": False,
    "zero": 0,
    "one": 1,
    "true": True,
    "string": "wiped",
    "empty_list": [],
    "list": ["crm.contact.email"],
    "empty_object": {},
    "object_a": {"crm.contact.email": "a"},
    "object_b": {"crm.contact.email": "b"},
    "object_two": {"crm.contact.email": "a", "crm.contact.lifecycle_stage": "c"},
    "object_deep": {"x": {"crm.contact.email": "e"}},
    "object_look_alike": {"crm.contact.email": "a", "crm.contact.Email": "a"},
}

PARITY_CASES = [
    pytest.param(
        {"survived": assigned},
        None if held is ... and side_is_none else ({} if held is ... else {"survived": held}),
        id=f"{assigned_name}-over-{held_name}{'-no-row' if side_is_none else ''}",
    )
    for assigned_name, assigned in PARITY_SHAPES.items()
    if assigned is not ...
    for held_name, held in PARITY_SHAPES.items()
    for side_is_none in ((True, False) if held is ... else (False,))
]

#: ...plus the top-level (non-nesting) key shapes, which is the other half of the
#: rule and the one migration 0012's CHECK keys on.
PARITY_CASES += [
    pytest.param({"crm.contact.grade": "7"}, None, id="top-level-eligible-no-row"),
    pytest.param({"crm.contact.grade": "7"}, {"crm.contact.grade": "7"}, id="top-level-unchanged"),
    pytest.param(
        {"crm.contact.email": "x@y.z"},
        {"crm.contact.email": "x@y.z"},
        id="top-level-sensitive-unchanged",
    ),
    pytest.param(
        {"crm.contact.email": "x"}, {"crm.contact.email": {"a": 1}}, id="scalar-over-object-leaf"
    ),
    pytest.param({"survived": True}, {"survived": {"k": 1}}, id="bool-over-number-member"),
    pytest.param({"survived": {"k": True}}, {"survived": {"k": 1}}, id="member-bool-vs-number"),
    pytest.param({"survived": {"k": 1}}, {"survived": {"k": 1.0}}, id="member-int-vs-float"),
    pytest.param({}, {"a": 1}, id="empty-action"),
    pytest.param({"a": 1, "survived": {"crm.contact.email": "z"}}, CURRENT, id="two-keys"),
]


@pytest.mark.parametrize(("assignments", "current"), PARITY_CASES)
@pytest.mark.parametrize("nested_only", [False, True])
def test_the_python_gate_and_the_database_agree_on_the_write_set(
    reader: Any, assignments: dict[str, Any], current: dict[str, Any] | None, nested_only: bool
) -> None:
    """Two implementations of one rule, asserted equal rather than assumed equal.

    `recon.apply.effective_write_paths` decides whether the machine applies;
    `keystone_effective_write_paths` decides whether the row may EXIST (`KS013`).
    A drift between them is a hole in whichever is the more permissive, and
    nothing else in this suite would see it.

    Both arguments are exercised: `nested_only = true` is the half `KS013` asks
    for -- the paths reached THROUGH a container, with the bare top-level keys
    left to migration 0012's CHECK -- and on the Python side that half is exactly
    the write paths that carry a container.
    """
    with reader.connect() as conn:
        enforced = conn.execute(
            text(
                "SELECT keystone_effective_write_paths("
                "CAST(:a AS jsonb), CAST(:c AS jsonb), :n) AS paths"
            ),
            {
                "a": json.dumps(assignments),
                "c": None if current is None else json.dumps(current),
                "n": nested_only,
            },
        ).scalar_one()
    derived = sorted(
        {
            path.leaf
            for path in effective_write_paths(assignments, current)
            if path.container is not None or not nested_only
        }
    )
    assert sorted(set(enforced)) == derived, (
        f"the database and recon.apply disagree about what {assignments} writes onto "
        f"{current} (nested_only={nested_only})"
    )


def _the_rule_as_of_0013(assignments: dict[str, Any], current: dict[str, Any] | None) -> set[str]:
    """Migration 0013's rule, re-implemented here to be compared against.

    Written out rather than imported because it no longer exists in the code --
    which is the point: the only way to assert "the repair cannot have ADMITTED
    anything" is to keep the thing it replaced and compare.
    """
    written: set[str] = set()
    for key, new_value in assignments.items():
        if not isinstance(new_value, Mapping):
            written.add(key)
            continue
        old_value = None if current is None else current.get(key)
        if isinstance(old_value, Mapping):
            members = {
                sub
                for sub in set(new_value) | set(old_value)
                if sub not in new_value or sub not in old_value or new_value[sub] != old_value[sub]
            }
        else:
            members = set(new_value)
        written |= members or {key}
    return written


def _would_0013_refuse(assignments: dict[str, Any], current: dict[str, Any] | None) -> bool:
    """The 0013 gate's verdict: any leaf sensitive, or any leaf off the allow-list."""
    leaves = _the_rule_as_of_0013(assignments, current)
    return any(leaf in SENSITIVE_FIELDS for leaf in leaves) or any(
        leaf not in AUTO_APPLY_ELIGIBLE for leaf in leaves
    )


@pytest.mark.parametrize(("assignments", "current"), PARITY_CASES)
def test_the_repair_can_only_REFUSE_MORE_never_less(
    assignments: dict[str, Any], current: dict[str, Any] | None
) -> None:
    """The safety property of the repair itself, over every generated shape.

    A rewrite of the rule that decides what R15 forbids is only safe if it
    refuses everything the rule it replaced refused. Asserted rather than
    reasoned, and asserted on the VERDICT rather than on the path set -- the two
    rules deliberately name different things for the same action (0013 named the
    container `survived` where this one names the members the collapse erases),
    so a subset test over leaves would be comparing labels, not decisions.
    """
    if _would_0013_refuse(assignments, current):
        verdict = write_set_gate(assignments, current)
        assert not verdict.cleared, (
            f"{assignments} onto {current} was refused by the 0013 rule and is ADMITTED "
            f"now: {verdict.reason} / {list(verdict.paths)} -- that is a HOLE"
        )


def test_the_repair_is_strictly_wider_somewhere() -> None:
    """...and the control: a rule identical to 0013's would have fixed nothing.

    Names the shapes on which the two differ, so "the repair widened the write
    set" is a measurement rather than a claim -- and so a future edit that
    silently reverts to the 0013 answer is red here as well as in the parity
    test.
    """
    widened = [
        case.id
        for case in PARITY_CASES
        if {path.leaf for path in effective_write_paths(*case.values)}
        > _the_rule_as_of_0013(*case.values)
    ]
    assert widened, "the current rule equals the 0013 rule everywhere; nothing was repaired"

    # the shape the blocker named: a non-object replacing the nested map. The old
    # rule saw ONE unlisted key; the new one sees the nine erasures, six of them
    # in SENSITIVE_FIELDS, so the REASON changes as well as the paths.
    assert _the_rule_as_of_0013({"survived": "wiped"}, CURRENT) == {"survived"}
    collapse = {path.leaf for path in effective_write_paths({"survived": "wiped"}, CURRENT)}
    assert collapse == set(SURVIVED_PATHS) | {"survived"}
    assert write_set_gate({"survived": "wiped"}, CURRENT).reason == "sensitive_write"


def test_the_eligible_paths_and_the_entity_view_overlap_in_exactly_one_place() -> None:
    """The measurement behind SS4.10, so the doc's claim is checked and not asserted.

    Contract SS6's eligible paths are source-qualified; `recon.resolve.VIEW_FIELDS`
    is the key set every reader projects. They share NO top-level member, which is
    why a `{"set": {"crm.contact.grade": ...}}` auto-apply is invisible. The one
    eligible path that lives in the view lives INSIDE `survived`.
    """
    assert not (set(AUTO_APPLY_ELIGIBLE) & set(VIEW_FIELDS))
    assert set(AUTO_APPLY_ELIGIBLE) & set(SURVIVED_PATHS) == {"crm.contact.lifecycle_stage"}
    assert "survived" in VIEW_FIELDS


# =====================================================================================
# the write set is re-asked UNDER THE ROW LOCK, and only for the machine
# =====================================================================================

_INSERT_APPLIABLE = text(
    """
    INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence,
                           status, sensitive, created_run, target_canonical_id)
    SELECT c.id, :fingerprint, CAST(:action AS jsonb), 0.99, CAST(:evidence AS jsonb),
           'pending', false, 'under-lock-probe', CAST(:canonical_id AS uuid)
      FROM conflicts c ORDER BY c.fingerprint LIMIT 1
    RETURNING id
    """
)

_APPROVE = text(
    """
    UPDATE proposals SET status = 'approved', decided_by = :who, decided_at = now()
     WHERE id = :proposal_id AND status = 'pending'
    RETURNING id
    """
)


def test_an_unattended_apply_re_asks_the_write_set_under_the_lock(
    reader: Any, canonical_row: tuple[str, dict[str, Any]]
) -> None:
    """R24's gate reads the row OUTSIDE the `FOR UPDATE`; the apply re-reads it inside.

    The window is real: between `evaluate_auto_apply`'s read and `apply_proposal`'s
    lock, another committed apply can move a nested member and turn a
    carried-unchanged sibling into a replacement -- a write the gate never saw.
    Rather than argue about the width of that window, `apply_proposal` asks the
    same question again against the locked value whenever the write is
    unattended.

    Driven here through the branch itself: `apply_proposal(..., auto=True)` on a
    proposal whose action is off the allow-list. The machine is refused by name.
    The CONTROL below is the same row applied by a human, which must succeed --
    a human-reviewed manual apply of an unlisted path is legitimate (SS8.4), and
    a check that refused it would be enforcing R24's allow-list against people.
    """
    from recon.apply import ApplyError, apply_proposal, rollback_proposal
    from recon.db import ROLE_RECON_WRITER, ROLE_REVIEW_WRITER, role_connection

    canonical_id, _ = canonical_row
    evidence = json.dumps(COMPLETE_EVIDENCE)
    with role_connection(ROLE_RECON_WRITER) as conn:
        proposal_id = conn.execute(
            _INSERT_APPLIABLE,
            {
                "fingerprint": "under-lock-unlisted-probe",
                "action": '{"set": {"crm.deal.pipeline": "not-on-either-list"}}',
                "evidence": evidence,
                "canonical_id": canonical_id,
            },
        ).scalar_one()
    with role_connection(ROLE_REVIEW_WRITER) as conn:
        assert conn.execute(_APPROVE, {"proposal_id": proposal_id, "who": "reviewer:t"}).fetchone()

    with pytest.raises(ApplyError) as raised:
        apply_proposal(proposal_id, auto=True)
    assert raised.value.reason == "write_set_refused_under_lock"
    assert "crm.deal.pipeline" in raised.value.detail

    # The control: the SAME row, applied by a human, lands -- and is reversed so
    # the store is left exactly as it was found.
    result = apply_proposal(proposal_id)
    assert result.before_digest != result.after_digest
    reversal = rollback_proposal(proposal_id)
    assert reversal.byte_identical
