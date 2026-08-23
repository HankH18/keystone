"""R15 over the WHOLE graded store: every held proposal, and none can reach apply.

Not a sample and not a hand-made shape -- every `sensitive_hold` proposal the
committed pipeline produced from the committed fixtures, plus every proposal
whose action writes a path in contract SS6's `SENSITIVE_FIELDS`, is put through
R24's gate and must be refused for the sensitivity reason.

The population is selected by SQL, from columns the module under test does not
write, so a bug in the gate cannot also shrink the set the gate is judged on.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from recon.apply import RollbackPath, auto_apply_decision, load_proposal
from recon.reference import SENSITIVE_FIELDS

#: Deliberately WIDE OPEN, and never consulted: if a held proposal is still
#: refused with a rollback path this permissive, the refusal came from the
#: sensitivity gate and not from a downstream condition that happened to fail.
OPEN_PATH = RollbackPath(known=True, detail="deliberately open", entity_exists=True)


@pytest.fixture(scope="session")
def held(store: Any) -> list[Any]:
    """Every held proposal, as the graded pipeline left it.

    The SQL and the moment it runs both live in `tests.apply.store`, which reads
    this population the instant `recon.reconciler.reconcile` finishes and before
    any suite has touched the store. It has to: this database is shared with
    `tests/api`, whose
    `test_the_auto_path_refuses_a_sensitive_proposal_even_once_approved` has a
    reviewer approve a held proposal over real HTTP and commit -- legitimate
    under R15, and irreversible under KS004. Querying here instead would make
    `test_the_held_population_is_the_graded_one` assert about a population that
    is no longer the graded one.
    """
    rows = list(store.held)
    assert rows, "no held proposal exists in the store, so every assertion here is vacuous"
    return rows


def test_the_held_population_is_the_graded_one(held: list[Any], store: Any) -> None:
    """The counts contract SS6 predicts, from the real run.

    "All 250 C4 proposals are `sensitive_hold`, not `pending`" is contract SS6's
    own stated consequence, and C14 is 50 by the committed golden distribution.
    Asserting the numbers is what stops this suite passing against a store that
    quietly contains none of them.
    """
    by_type: dict[str, int] = {}
    for row in held:
        by_type[row.type] = by_type.get(row.type, 0) + 1
    assert by_type["C4"] == 250, f"contract SS6 pins 250 held C4 proposals, store has {by_type}"
    assert by_type["C14"] == store.by_type["C14"] == 50
    assert all(row.status == "sensitive_hold" for row in held)
    assert all(row.sensitive for row in held)


def test_every_c14_is_held(held: list[Any], store: Any) -> None:
    """Contract SS6: "**every** C14"."""
    c14 = [row for row in held if row.type == "C14"]
    assert len(c14) == store.by_type["C14"]
    assert {row.status for row in c14} == {"sensitive_hold"}


def test_no_held_proposal_can_reach_the_confidence_gate(held: list[Any], reader: Any) -> None:
    """The whole population, one at a time, through R24's real gate."""
    with reader.connect() as conn:
        records = [load_proposal(conn, row.id) for row in held]
    refused = 0
    highest = Decimal("0")
    for record in records:
        assert record is not None
        highest = max(highest, record.confidence)
        decision = auto_apply_decision(record, OPEN_PATH)
        assert not decision.allowed, f"proposal {record.id} ({record.conflict_type}) was admitted"
        assert decision.reason == "sensitive_hold", (
            f"proposal {record.id} was refused for {decision.reason!r}, not for being "
            "sensitive: it must be held by R15 and not merely by a low score"
        )
        assert len(decision.checks) == 1, (
            f"proposal {record.id} evaluated {[c.name for c in decision.checks]} -- the "
            "sensitivity gate must return before any eligibility condition"
        )
        refused += 1
    assert refused == len(held)
    # Recorded so the proof carries a real number rather than an adjective.
    assert highest > 0


def test_no_held_proposal_writes_a_path_off_the_sensitive_list(
    held: list[Any], reader: Any
) -> None:
    """The C4 re-targeting prohibition (contract SS6, SS12 D-7), checked on real rows.

    A C4 retargeted at `crm.contact.external_id` would silently reclassify all
    250 of them as auto-appliable. Every held action key is checked against
    `SENSITIVE_FIELDS`, so the escape is caught by data and not by reading code.
    """
    with reader.connect() as conn:
        records = [load_proposal(conn, row.id) for row in held]
    for record in records:
        assert record is not None
        for path in record.assignments:
            assert path in SENSITIVE_FIELDS, (
                f"held proposal {record.id} ({record.conflict_type}) writes {path!r}, which "
                "is not a sensitive path -- a held proposal that writes an eligible field "
                "is the re-targeting escape contract SS6 forbids"
            )


def test_approving_a_held_proposal_still_does_not_unlock_auto_apply(
    reader: Any, review_conn: Any
) -> None:
    """R15 forces HUMAN review; it does not make an approval an auto-apply licence.

    A reviewer may approve a `sensitive_hold` proposal -- that is what "forced to
    human review" means. What must never follow is the machine taking it
    unattended, so the gate is asked again with the status already `approved`.
    Rolled back: this test proves a rule, it does not need to leave a decision.
    """
    row = review_conn.execute(
        text("SELECT id FROM proposals WHERE status = 'sensitive_hold' ORDER BY id LIMIT 1")
    ).one()
    review_conn.execute(
        text(
            "UPDATE proposals SET status = 'approved', decided_by = 'reviewer:t', "
            "decided_at = now() WHERE id = :id"
        ),
        {"id": row.id},
    )
    record = load_proposal(review_conn, row.id)
    assert record is not None
    assert record.status == "approved"
    decision = auto_apply_decision(record, OPEN_PATH)
    assert not decision.allowed
    assert decision.reason == "sensitive_hold"
