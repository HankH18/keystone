"""What is a green apply evidence OF? Only of something a reader can see.

The blindness this file is about
---------------------------------
Contract SS6's `AUTO_APPLY_ELIGIBLE` is written in source-qualified paths.
`recon.resolve.VIEW_FIELDS` is the key set the entity PROJECTION is built from,
and the two share no member. So an apply of `{"set": {"crm.contact.grade": "7"}}`
adds a NEW TOP-LEVEL KEY to `entities.current` that nothing projects: the row
moves, its digest moves, `test_apply_lifecycle.py` goes green -- and no value any
reader shows has changed. R24's "applies only to Keystone's canonical layer" is
satisfied while being observably empty.

**Who the readers actually are**, because this file used to say "every reader
projects ... the object the dashboard renders" and that was false:

* `recon.api.entities._view_of` -- the body of `GET /api/entities` and
  `GET /api/entities/{key}`;
* `recon.suite.golden` -- the R10 join check, which diffs that same projection
  against the committed `golden/expected-views.json`.

The **dashboard is not one of them**. `dashboard/src/lib/httpClient.ts` calls
`/api/conflicts`, `/api/proposals`, the three decision verbs and
`/api/scorecard`, and never touches the entities endpoint at all.

What is observable, and why it is a re-shaping and not a widening
------------------------------------------------------------------
Exactly one eligible path lives inside the view: `crm.contact.lifecycle_stage`,
a member of the nested `survived` map (`recon.resolve.SURVIVED_PATHS`) that
`VIEW_FIELDS` does project. It is contract SS6's committed fix target for a
lifecycle-only C6 -- "eligible (CRM side only)" -- so nothing is widened to reach
it: the same conflict, the same target, the same allow-list, written where the
canonical layer actually keeps the field instead of beside it. That form is
representable only because R24's gate judges the PATHS a statement writes rather
than the keys it names (`tests/apply/test_nested_write_set.py`).

**`recon.reconciler` now EMITS that form.** Nothing here hand-writes a proposal:
the rows below came out of the committed pipeline, in the committed shape, with
the committed confidence.

The honest limit, measured rather than estimated
--------------------------------------------------
**No proposal the shipped pipeline can auto-apply is observable, and that is
structural rather than a property of this dataset.**

* The only eligible path inside the view is the fix target of a **C6**, and no
  C6 or C14 can ever reach R24's 0.95 floor: contract SS2.4 makes those two the
  only types that populate `disagreeing_fields`, the model's `disagreeing_field`
  term is `-0.10` per disagreeing comparison ROW, and the v2 formula clamps the
  POSITIVE half at 1.0000 before subtracting penalties -- so the ceiling for any
  conflict carrying one disagreeing row is exactly **0.9000**.
  `test_no_c6_can_ever_reach_the_auto_apply_floor` derives that from the loaded
  model rather than from the store, and the store agrees: 120 lifecycle-only C6
  proposals, best score 0.9000.
* The proposals R24 WOULD admit once approved write exactly
  `{appdb.enrollment.crm_deal_id}` -- 50 C9s -- and that path is not projected.

So this file proves the observable write end to end through the **manual** apply
path, which is the path R15 and R24 leave to a human, and pins the unattended
path's refusal on the real row rather than fabricating a score to get past it.
`docs/proposal-policy.md` SS8.10 records what it would take to close the gap
legitimately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from recon.api.entities import _view_of
from recon.apply import (
    AUTO_APPLY_CONFIDENCE_FLOOR,
    ApplyError,
    apply_proposal,
    auto_apply,
    effective_write_paths,
    evaluate_auto_apply,
    load_proposal,
    merge_preview,
    rollback_proposal,
)
from recon.apply import AutoApplyRefused as _AutoApplyRefused
from recon.confidence import ConfidenceModel, load_model
from recon.db import ROLE_REVIEW_WRITER, role_connection
from recon.reference import AUTO_APPLY_ELIGIBLE
from recon.resolve import SURVIVED_PATHS, VIEW_FIELDS

TARGET_PATH = "crm.contact.lifecycle_stage"
CONTAINER = "survived"

_OBSERVABLE_PROPOSALS = text(
    """
    SELECT p.id,
           p.confidence,
           p.status::text AS status,
           p.sensitive,
           p.action,
           p.target_canonical_id::text AS canonical_id,
           c.type
      FROM proposals p
      JOIN conflicts c ON c.id = p.conflict_id
      JOIN entities e ON e.canonical_id = p.target_canonical_id
     WHERE jsonb_typeof(p.action #> '{set,survived}') = 'object'
       AND NOT EXISTS (SELECT 1 FROM proposal_events pe WHERE pe.proposal_id = p.id)
     ORDER BY p.confidence DESC, p.id
    """
)

_ADMISSIBLE = text(
    """
    SELECT p.id
      FROM proposals p
     WHERE p.status = 'pending'
       AND NOT p.sensitive
       AND p.confidence >= 0.95
       AND p.action -> 'set' <> '{}'::jsonb
       AND NOT EXISTS (SELECT 1 FROM proposal_events pe WHERE pe.proposal_id = p.id)
     ORDER BY p.id
    """
)

_APPROVE = text(
    """
    UPDATE proposals
       SET status = 'approved', decided_by = :decided_by, decided_at = :decided_at
     WHERE id = :proposal_id AND status = 'pending'
    RETURNING id
    """
)

_ENTITY_CURRENT = text(
    "SELECT current FROM entities WHERE canonical_id = CAST(:canonical_id AS uuid)"
)


@pytest.fixture(scope="session")
def observable_proposals(store: Any, reader: Any) -> list[Any]:
    """Every proposal the COMMITTED reconciler emitted in the nested form.

    Selected by the database's own view of the shape -- an action whose `set`
    carries an object at `survived` -- so a bug in `recon.apply` cannot also
    choose the population it is measured on. Nothing here is constructed.
    """
    with reader.connect() as conn:
        rows = list(conn.execute(_OBSERVABLE_PROPOSALS))
    assert rows, (
        "the committed reconciler emitted no proposal writing into `survived`, so "
        "nothing in this file is about the shipped pipeline"
    )
    return rows


def _entity_view(reader: Any, canonical_id: str) -> dict[str, Any]:
    """The entity as a READER sees it: `recon.api.entities`' own projection.

    Deliberately the API's private `_view_of` and not a re-implementation. The
    claim is "a reviewer can see this", and the only way to check that claim is
    to look through the function they look through.
    """
    with reader.connect() as conn:
        current = conn.execute(_ENTITY_CURRENT, {"canonical_id": canonical_id}).scalar_one()
    return _view_of(dict(current))


# =====================================================================================
# the shipped pipeline emits the observable form
# =====================================================================================


def test_the_committed_reconciler_emits_the_nested_form(
    observable_proposals: list[Any], reader: Any
) -> None:
    """Not a test fixture's shape -- the shipped template's.

    Each row: one top-level key (`survived`), the WHOLE nine-member map carried
    per contract SS5, exactly one EFFECTIVE write path, and that path's leaf is
    contract SS6's committed fix target. The population count is a property of
    this store and is asserted only non-empty; the SHAPE is the structural claim
    and is asserted on every row.
    """
    with reader.connect() as conn:
        for row in observable_proposals:
            assignments = dict(row.action["set"])
            assert set(assignments) == {CONTAINER}
            assert set(assignments[CONTAINER]) == set(SURVIVED_PATHS), (
                "the action does not carry the whole map; contract SS5's shallow merge "
                "would erase the members it omits"
            )
            current = dict(
                conn.execute(_ENTITY_CURRENT, {"canonical_id": row.canonical_id}).scalar_one()
            )
            written = effective_write_paths(assignments, current)
            assert [path.display for path in written] == [f"{CONTAINER}->{TARGET_PATH}"], (
                f"proposal {row.id} writes {[p.display for p in written]}"
            )
            assert written[0].leaf in AUTO_APPLY_ELIGIBLE
            assert row.type == "C6"
            assert not row.sensitive
            # ...and it changes nothing else the merge would touch.
            assert merge_preview(current, assignments).safe


DASHBOARD_SRC = Path(__file__).resolve().parents[3] / "dashboard" / "src"


def test_the_readers_this_file_names_are_the_readers_that_exist() -> None:
    """The documentation claim, turned into a checked one.

    `docs/proposal-policy.md` SS4 and `recon.apply`'s docstring used to call
    `VIEW_FIELDS` "the exact key set every reader projects ... the object the
    dashboard renders". The dashboard does not render it: its client calls
    `/api/conflicts`, `/api/proposals`, the three decision verbs and
    `/api/scorecard`, and never the entities endpoint. That mattered -- it
    overstated who would NOTICE an invisible write, which is how the
    observability gap stayed comfortable for a whole ticket.

    So the claim is asserted rather than written down: no file under
    `dashboard/src` may reference the entities endpoint, and the two readers this
    file names must both actually import `VIEW_FIELDS`.
    """
    assert DASHBOARD_SRC.is_dir(), f"no dashboard source tree at {DASHBOARD_SRC}"
    sources = sorted(
        path
        for path in DASHBOARD_SRC.rglob("*.ts*")
        if path.is_file() and not path.name.endswith(".d.ts")
    )
    assert sources, "no dashboard TypeScript sources found; this test is vacuous"
    # the control: the endpoints it DOES call are found by the same search.
    joined = "\n".join(path.read_text() for path in sources)
    assert "/api/proposals" in joined and "/api/conflicts" in joined, (
        "the search found neither endpoint the dashboard is known to call, so a "
        "clean result below would prove nothing"
    )
    offenders = [
        path.relative_to(DASHBOARD_SRC).as_posix()
        for path in sources
        if "/api/entities" in path.read_text()
    ]
    assert not offenders, (
        f"{offenders} reference the entities endpoint, so the dashboard IS a reader of "
        "VIEW_FIELDS after all and docs/proposal-policy.md SS4 needs re-deriving"
    )

    import recon.api.entities as entities_api
    import recon.suite.golden as suite_golden

    assert entities_api.VIEW_FIELDS is VIEW_FIELDS
    assert suite_golden.VIEW_FIELDS is VIEW_FIELDS


def test_exactly_one_eligible_path_lives_inside_the_view() -> None:
    """Why the shape above is a re-shaping and not a widening of the allow-list."""
    assert not set(AUTO_APPLY_ELIGIBLE) & set(VIEW_FIELDS)
    assert set(AUTO_APPLY_ELIGIBLE) & set(SURVIVED_PATHS) == {TARGET_PATH}
    assert CONTAINER in VIEW_FIELDS


def test_the_top_level_form_would_still_be_invisible(
    observable_proposals: list[Any], reader: Any
) -> None:
    """The measurement that makes the re-shaping worth doing, on a real row.

    The same fix expressed the old way -- `{"set": {"crm.contact.lifecycle_stage":
    ...}}` -- is merged exactly as `KS010` requires and projected through the
    reader's own function. The raw row changes; the projection does not.
    """
    row = observable_proposals[0]
    value = row.action["set"][CONTAINER][TARGET_PATH]
    with reader.connect() as conn:
        before = dict(
            conn.execute(_ENTITY_CURRENT, {"canonical_id": row.canonical_id}).scalar_one()
        )
    after = {**before, TARGET_PATH: value}
    assert after != before, "the top-level merge changed nothing, so this measures nothing"
    assert TARGET_PATH not in VIEW_FIELDS
    assert _view_of(after) == _view_of(before), (
        "the top-level form IS visible in the entity view after all; the premise of "
        "this file no longer holds and its conclusion needs re-deriving"
    )
    # ...and the nested form, on the same row, is not invisible.
    nested = {**before, CONTAINER: dict(row.action["set"][CONTAINER])}
    assert _view_of(nested) != _view_of(before)


# =====================================================================================
# the honest limit: nothing the machine may take unattended is observable
# =====================================================================================


def test_no_c6_can_ever_reach_the_auto_apply_floor() -> None:
    """Structural, from the committed model -- not a property of this seed.

    Contract SS2.4: only `R-006` (C6) and `R-014` (C14) populate
    `disagreeing_fields`, and a conflict of either type has at least one
    disagreeing comparison row by definition. The model's `disagreeing_field`
    term is negative, and `confidence.yaml`'s v2 formula is
    `clamp01(clamp01(base + positives) + negatives)` -- the positive half is
    clamped to 1.0000 BEFORE the penalties are subtracted, so the best any
    conflict carrying one disagreeing row can score is `1.0000 + weight`.

    With the committed weight of `-0.10` that ceiling is **0.9000**, which is
    below R24's 0.95 floor. The one eligible path inside the entity view is the
    fix target of a C6, so no proposal the machine may take unattended can be
    observable -- and no seed change can alter that, only a model change.
    """
    model: ConfidenceModel = load_model()
    term = model.signal("disagreeing_field")
    assert term.sign == "negative" and term.weight < 0, (
        "the disagreeing-field term is no longer a penalty; re-derive this"
    )
    assert "clamp01(clamp01(base" in model.formula, (
        f"the formula is now {model.formula!r}: the positive half may no longer be "
        "clamped before the penalties, so this ceiling does not follow"
    )
    ceiling = model.clamp_max + term.weight
    assert ceiling < AUTO_APPLY_CONFIDENCE_FLOOR, (
        f"a single disagreeing row now leaves a ceiling of {ceiling}, which reaches "
        f"R24's floor {AUTO_APPLY_CONFIDENCE_FLOOR}: a C6 CAN auto-apply now and "
        "docs/proposal-policy.md SS8.10 needs re-deriving"
    )
    assert ceiling == Decimal("0.9000")


def test_the_store_agrees_with_that_ceiling(observable_proposals: list[Any]) -> None:
    """...and the measurement, so the derivation is checked against reality.

    A count, and therefore a property of THIS store -- which is why the structural
    claim is the test above and this one only confirms it did not diverge.
    """
    best = max(Decimal(str(row.confidence)) for row in observable_proposals)
    assert best == Decimal("0.9000"), (
        f"{len(observable_proposals)} observable-form proposals, best score {best}: "
        "that is not the derived ceiling, so the model or the data has moved"
    )
    assert best < AUTO_APPLY_CONFIDENCE_FLOOR


def test_every_write_the_gate_would_admit_today_lands_outside_the_view(
    store: Any, reader: Any
) -> None:
    """The scope of the blindness, measured over the whole store rather than one row.

    Every proposal R24 would admit **once a reviewer approves it** -- its only
    failing condition being `status_appliable` -- is collected, and the union of
    the paths they write is compared with `VIEW_FIELDS`. The answer today is a
    single path, `appdb.enrollment.crm_deal_id`, and it is not projected.

    The path SET is the structural claim; the population behind it is a property
    of this store and is only asserted non-empty.
    """
    admissible: dict[int, list[str]] = {}
    with reader.connect() as conn:
        for row in conn.execute(_ADMISSIBLE):
            decision = evaluate_auto_apply(conn, row.id)
            if {check.name for check in decision.failed} <= {"status_appliable"}:
                record = load_proposal(conn, row.id)
                assert record is not None
                current = dict(
                    conn.execute(
                        _ENTITY_CURRENT, {"canonical_id": record.target_canonical_id}
                    ).scalar_one()
                )
                admissible[row.id] = [
                    path.leaf for path in effective_write_paths(record.assignments, current)
                ]
    assert admissible, (
        "no proposal in the store would be admitted even after approval, so this "
        "measurement has an empty denominator"
    )
    written = {path for paths in admissible.values() for path in paths}
    assert written == {"appdb.enrollment.crm_deal_id"}, (
        f"the admissible write set is {sorted(written)}; the paragraph in "
        "docs/proposal-policy.md SS8.10 that names it needs re-deriving"
    )
    assert not written & set(VIEW_FIELDS), (
        f"{sorted(written & set(VIEW_FIELDS))} IS projected, so the observability gap "
        "has closed on its own and SS8.10 should say so"
    )


def test_the_real_row_fails_the_gate_on_its_SCORE_and_nothing_else(
    reader: Any, observable_proposals: list[Any]
) -> None:
    """Everything except the number is admissible on the REAL row.

    The conflict, its classification, the committed fix target, the whole
    nine-member write, the evidence packet and the rollback path all clear R24 --
    so "the only thing between this proposal and an unattended apply is the
    model's arithmetic" is a measurement and not a hope. `status_appliable` is
    the reviewer's to fix; `confidence_floor` is not fixable at all (see the
    ceiling test above).
    """
    row = observable_proposals[0]
    with reader.connect() as conn:
        decision = evaluate_auto_apply(conn, row.id)
    assert not decision.allowed
    assert {check.name for check in decision.failed} == {
        "confidence_floor",
        "status_appliable",
    }, decision.detail


# =====================================================================================
# the deliverable: an eligible, non-sensitive apply a reader can SEE, and its reversal
# =====================================================================================


def test_the_real_proposal_applies_visibly_and_rolls_back(
    reader: Any, observable_proposals: list[Any]
) -> None:
    """Approve -> apply -> read the view -> roll back -> read the view. COMMITS.

    Every leg is the real one on a REAL committed proposal: `review_writer`
    decides, `recon.apply.apply_proposal` writes as `apply_writer` through
    `KS010`/`KS011`'s citation, and the value is read back through
    `recon.api.entities._view_of` -- the reader's own projection, not a
    re-implementation.

    **This is the manual path, and that is the honest one to demonstrate.** The
    unattended path refuses this row on its score and no score in the store
    clears the floor, so an auto-apply demonstration here would have to fabricate
    a confidence -- which is the overclaim the previous version of this file
    made. What auto-apply does with the row is pinned below instead.
    """
    row = observable_proposals[0]
    canonical_id = row.canonical_id
    original_view = _entity_view(reader, canonical_id)
    old_value = original_view[CONTAINER][TARGET_PATH]
    new_value = row.action["set"][CONTAINER][TARGET_PATH]
    assert new_value != old_value, (
        "the committed proposal writes the value the row already holds, so applying "
        "it would demonstrate nothing"
    )

    with role_connection(ROLE_REVIEW_WRITER) as conn:
        approved = conn.execute(
            _APPROVE,
            {
                "proposal_id": row.id,
                "decided_by": "reviewer:observability-suite",
                "decided_at": datetime.now(UTC),
            },
        ).fetchone()
    assert approved is not None, "the reviewer could not approve the committed proposal"

    result = apply_proposal(row.id)
    assert not result.auto

    # ---- what a reader sees now -------------------------------------------
    applied_view = _entity_view(reader, canonical_id)
    assert applied_view[CONTAINER][TARGET_PATH] == new_value, (
        "the apply is still invisible in the entity view"
    )
    assert applied_view != original_view
    for path in SURVIVED_PATHS:
        if path == TARGET_PATH:
            continue
        assert applied_view[CONTAINER][path] == original_view[CONTAINER][path], (
            f"the nested write erased or altered the sibling {path}"
        )
    for field in VIEW_FIELDS:
        if field == CONTAINER:
            continue
        assert applied_view[field] == original_view[field], f"{field} moved unexpectedly"

    # ---- and the reversal, read back the same way -------------------------
    reversal = rollback_proposal(row.id)
    assert reversal.byte_identical
    restored_view = _entity_view(reader, canonical_id)
    assert restored_view == original_view, "the rollback did not restore the reader's view"
    assert restored_view[CONTAINER][TARGET_PATH] == old_value

    with reader.connect() as conn:
        assert load_proposal(conn, row.id).status == "rolled_back"


def test_the_unattended_path_refuses_the_same_row_on_its_score(
    reader: Any, observable_proposals: list[Any]
) -> None:
    """R24 on the real row, through `auto_apply` itself rather than through the gate.

    Approved by a human and otherwise admissible, it is still refused -- by
    `confidence_floor` and by nothing else -- and the refusal names the single
    condition it evaluated. Rolled back; nothing is committed.
    """
    row = observable_proposals[1]
    with role_connection(ROLE_REVIEW_WRITER, commit=False) as conn:
        conn.execute(
            _APPROVE,
            {
                "proposal_id": row.id,
                "decided_by": "reviewer:observability-suite",
                "decided_at": datetime.now(UTC),
            },
        )
        record = load_proposal(conn, row.id)
        assert record is not None
        assert record.status == "approved"
        conn.rollback()

    with pytest.raises(_AutoApplyRefused) as raised:
        auto_apply(row.id)
    decision = raised.value.decision
    assert not decision.allowed
    assert "confidence_floor" in {check.name for check in decision.failed}
    assert str(AUTO_APPLY_CONFIDENCE_FLOOR) in decision.detail


def test_the_manual_path_refuses_a_look_alike_member_on_a_real_row(
    reader: Any, observable_proposals: list[Any]
) -> None:
    """The reader-facing hole, closed on the statement BOTH apply paths go through.

    R24's allow-list refuses an ADDED member (its leaf is on neither committed
    list), but a reviewer pressing APPLY is not behind R24. Without a guard, an
    approved proposal carrying all nine genuine members plus a case variant of
    `crm.contact.email` would land, and the entity endpoints would render the
    attacker's value beside the genuine one under a name a human reads as real.

    Nothing is committed: `apply_proposal` refuses before any statement runs.
    """
    row = observable_proposals[2]
    survived = dict(row.action["set"][CONTAINER])
    spoofed = {**survived, "CRM.contact.email": "attacker@evil.test"}
    with reader.connect() as conn:
        current = dict(
            conn.execute(_ENTITY_CURRENT, {"canonical_id": row.canonical_id}).scalar_one()
        )
    preview = merge_preview(current, {CONTAINER: spoofed})
    assert not preview.safe
    assert preview.introduced == (f"{CONTAINER}.CRM.contact.email",)
    assert preview.erased == ()

    import json as _json

    from recon.db import ROLE_RECON_WRITER

    insert = text(
        """
        INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence,
                               status, sensitive, created_run, target_canonical_id)
        SELECT p.conflict_id, :fingerprint, CAST(:action AS jsonb), p.confidence,
               p.evidence, 'pending', false, 'look-alike-probe', p.target_canonical_id
          FROM proposals p WHERE p.id = :source
        RETURNING id
        """
    )
    with role_connection(ROLE_RECON_WRITER) as conn:
        spoof_id = conn.execute(
            insert,
            {
                "fingerprint": f"look-alike-{row.id}",
                "action": _json.dumps({"set": {CONTAINER: spoofed}}),
                "source": row.id,
            },
        ).scalar_one()
    with role_connection(ROLE_REVIEW_WRITER) as conn:
        conn.execute(
            _APPROVE,
            {
                "proposal_id": spoof_id,
                "decided_by": "reviewer:observability-suite",
                "decided_at": datetime.now(UTC),
            },
        )

    with pytest.raises(ApplyError) as raised:
        apply_proposal(spoof_id)
    assert raised.value.reason == "nested_member_introduced"
    assert "CRM.contact.email" in raised.value.detail

    # ...and R24 refuses it too, for the allow-list reason.
    with reader.connect() as conn:
        decision = evaluate_auto_apply(conn, spoof_id)
    assert not decision.allowed
    assert decision.reason == "write_off_allowlist"
    assert "CRM.contact.email" in decision.detail

    # the CONTROL: the genuine action on the same row is applied without complaint
    # by the same guard, so the refusal above is about the added member and not
    # about the nested shape.
    assert merge_preview(current, {CONTAINER: survived}).safe


def test_the_manual_path_still_refuses_a_write_that_erases_a_sibling(
    reader: Any, observable_proposals: list[Any]
) -> None:
    """Contract SS5's guard, unchanged, on a real row -- the other arm of `safe`."""
    row = observable_proposals[3]
    survived = dict(row.action["set"][CONTAINER])
    dropped = "appdb.enrollment.program"
    truncated = {path: value for path, value in survived.items() if path != dropped}
    with reader.connect() as conn:
        current = dict(
            conn.execute(_ENTITY_CURRENT, {"canonical_id": row.canonical_id}).scalar_one()
        )
    preview = merge_preview(current, {CONTAINER: truncated})
    assert not preview.safe
    assert preview.erased == (f"{CONTAINER}.{dropped}",)
    assert preview.introduced == ()
