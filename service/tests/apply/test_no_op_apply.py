"""An action that merges to the value the canonical row already holds is refused.

`_require_appliable` refuses an action that is *empty* (`{"set": {}}`); this is the
same rule for an action that is empty in EFFECT. Before the refusal existed,
`POST /api/proposals/{id}/apply?auto=true` on such a proposal answered 200, wrote a
`proposal_events` row whose `before` and `after` were the same bytes, spent the
single-use citation and audited `proposal.auto_applied` -- a ledger entry
indistinguishable from one that moved a value. It also inverted the reversal stack:
`KS012`'s "not on top" refusal compares digests, and a no-op event on top carries the
digest of the one beneath it, so the earlier proposal could be reversed out from
under an applied, unreversed one.

**The case is constructed, and it has to be.** No proposal the committed pipeline
authors is value-identical against the store it was authored from -- 0 of the 807
proposals carrying a non-empty `set`, checked with
`(e.current || (p.action->'set')) = e.current`. The case arises from two proposals
that write one path the same way, whichever lands second, so the row below is
inserted rather than found, following the same pattern
`tests/apply/test_nested_write_set.py` uses for the actions the reconciler never
authors either.

Both tests run inside ONE transaction that is rolled back, so the store is unchanged
and no citation is spent. The deferred citation triggers (`KS001`, `KS011`, `KS012`)
judge at COMMIT and are therefore not exercised here; what is exercised is
`recon.apply.apply_proposal`'s own refusal, which is where the guard lives.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

#: Keys excluded from "pick a real top-level string to re-assert". `tenant` decides
#: which client key may see the row and `household_key` is a personal value; neither
#: belongs in a probe action even one that is rolled back.
_NOT_A_PROBE_TARGET = frozenset({"tenant", "household_key"})

_ENTITY = text(
    """
    SELECT e.canonical_id::text AS canonical_id, e.current
      FROM entities e
     ORDER BY e.canonical_id
     LIMIT 1
    """
)

#: Born `pending`, as `KS002` requires of every proposal. The conflict is a real one,
#: chosen by fingerprint order so the row is the same on every run.
_INSERT_PROPOSAL = text(
    """
    INSERT INTO proposals (conflict_id, fingerprint, action, confidence, evidence,
                           status, sensitive, created_run, target_canonical_id)
    SELECT c.id, :fingerprint, CAST(:action AS jsonb), 0.99, '{}'::jsonb,
           'pending', false, 'no-op-apply-probe', CAST(:canonical_id AS uuid)
      FROM conflicts c ORDER BY c.fingerprint LIMIT 1
    RETURNING id
    """
)

_APPROVE = text(
    """
    UPDATE proposals
       SET status = 'approved', decided_by = :decided_by, decided_at = now()
     WHERE id = :id
    """
)

_EVENTS = text("SELECT count(*) AS n FROM proposal_events WHERE proposal_id = :id")

_CURRENT = text("SELECT current::text AS t FROM entities WHERE canonical_id = CAST(:c AS uuid)")


def _probe_target(current: dict[str, Any]) -> tuple[str, str]:
    """A real top-level key of `entities.current` holding a real string value."""
    for key in sorted(current):
        if key not in _NOT_A_PROBE_TARGET and isinstance(current[key], str):
            return key, current[key]
    raise AssertionError(f"no top-level string on the canonical row: {sorted(current)}")


@pytest.fixture
def owner_conn(store: Any) -> Any:
    """One connection, one transaction, rolled back.

    The schema owner, deliberately: these two tests are about the Python refusal in
    `apply_proposal`, and inserting a proposal, deciding it and applying it are three
    different roles that cannot see each other's uncommitted work. Which principal
    may do which is `tests/schema/test_role_permissions.py`'s subject, and that the
    endpoints use them is `tests/api/test_write_role_binding.py`'s.
    """
    from recon.db import get_engine

    with get_engine().connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


def _probe(conn: Any, *, fingerprint: str, value_for: Any) -> tuple[int, str, str]:
    """Insert an approved proposal writing `value_for(current_value)` at a real key."""
    row = conn.execute(_ENTITY).one()
    current = dict(row.current)
    key, held = _probe_target(current)
    action = json.dumps({"set": {key: value_for(held)}}, sort_keys=True)
    proposal_id = int(
        conn.execute(
            _INSERT_PROPOSAL,
            {
                "fingerprint": fingerprint,
                "action": action,
                "canonical_id": row.canonical_id,
            },
        ).scalar_one()
    )
    conn.execute(_APPROVE, {"id": proposal_id, "decided_by": "reviewer:no-op-probe"})
    return proposal_id, row.canonical_id, key


def test_an_action_that_re_asserts_the_held_value_is_refused(owner_conn: Any) -> None:
    """The refusal, and that it costs the citation nothing."""
    from recon.apply import ApplyError, apply_proposal

    proposal_id, canonical_id, key = _probe(
        owner_conn, fingerprint="no-op-apply-probe", value_for=lambda held: held
    )
    before = owner_conn.execute(_CURRENT, {"c": canonical_id}).scalar_one()

    with pytest.raises(ApplyError) as raised:
        apply_proposal(proposal_id, conn=owner_conn)

    assert raised.value.reason == "no_op", raised.value.detail
    assert str(proposal_id) in raised.value.detail
    assert owner_conn.execute(_EVENTS, {"id": proposal_id}).scalar_one() == 0, (
        "the refused apply still wrote a ledger row"
    )
    assert (
        owner_conn.execute(
            text("SELECT status::text FROM proposals WHERE id = :id"), {"id": proposal_id}
        ).scalar_one()
        == "approved"
    ), "the refused apply still spent the citation"
    assert owner_conn.execute(_CURRENT, {"c": canonical_id}).scalar_one() == before, (
        f"the refused apply still wrote {key} onto the canonical row"
    )


def test_the_same_probe_applies_when_the_value_actually_changes(owner_conn: Any) -> None:
    """The control: same construction, one character different, and it lands.

    Without this, `no_op` would be satisfied by an `apply_proposal` that refused
    everything the probe hands it.
    """
    from recon.apply import apply_proposal

    proposal_id, canonical_id, key = _probe(
        owner_conn, fingerprint="no-op-apply-control", value_for=lambda held: f"{held}-moved"
    )
    before = owner_conn.execute(_CURRENT, {"c": canonical_id}).scalar_one()

    result = apply_proposal(proposal_id, conn=owner_conn)

    assert result.before_digest != result.after_digest
    assert owner_conn.execute(_EVENTS, {"id": proposal_id}).scalar_one() == 1
    assert owner_conn.execute(_CURRENT, {"c": canonical_id}).scalar_one() != before
    assert (
        json.loads(owner_conn.execute(_CURRENT, {"c": canonical_id}).scalar_one())[key]
        != (json.loads(before)[key])
    )
