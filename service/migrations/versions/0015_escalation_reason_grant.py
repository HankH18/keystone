"""`recon_writer` may write the escalation reason it is required to record.

Revision ID: 0015_escalation_reason_grant
Revises: 0014_write_set_from_the_value
Create Date: 2026-08-24

The gap this closes
-------------------
Migration 0004 narrowed ``recon_writer``'s table-level UPDATE on ``conflicts`` to
a column list -- correctly, because a table grant silently covers columns added
later and re-detection must not be able to rewrite what a conflict *is*. The list
it chose was ``("status", "last_seen_run")``::

    REVOKE UPDATE ON conflicts FROM "recon_writer";
    GRANT UPDATE (status, last_seen_run) ON conflicts TO "recon_writer";

``escalation_reason`` (``conflicts``, migration 0001) was left out of it, and
R16's escalation is the one write that needs it. ``recon.reconciler._escalate``
wants to issue one statement::

    UPDATE conflicts SET status = 'escalated', escalation_reason = :reason ...

which Postgres refuses **wholesale** with SQLSTATE ``42501``. Raised inside
``reconcile()``'s transaction, that rolled the entire run back and wrote zero
proposals the first time any conflict oscillated. The reconciler was therefore
made to ask the catalogue what it holds
(``has_column_privilege(current_user, 'conflicts', 'escalation_reason',
'UPDATE')``, once per run) and to escalate with the columns it actually has,
writing the reason to the ``conflict.escalated`` audit row and leaving the column
NULL. That kept the run alive; it did not make the column true.

The visible consequence is on the reviewer surface. ``recon.api.review`` renders
the status the dashboard's vocabulary expects out of the row::

    WHEN c.escalation_reason IS NOT NULL THEN 'escalated:' || c.escalation_reason
    WHEN c.oscillating                   THEN 'escalated:oscillation'
    ELSE 'escalated'

so today every escalated conflict is served through the *second* branch -- it
works only because ``oscillating`` happens to be settable on INSERT and
oscillation happens to be the only escalation reason there is. The first branch,
the general one, has never been reachable by the principal that writes the rows.

What this migration does
------------------------
Adds ``escalation_reason`` to that column-scoped grant, and nothing else. The
grant stays column-scoped: this is a third named column, not a return to a table
grant. ``fingerprint``, ``type``, ``entity_refs``, ``oscillating`` and
``first_seen_run`` remain ungranted, so 0004's actual rule -- *re-detection
advances a conflict, it does not redefine one* -- is unchanged, and the negative
tests that assert each of those is refused still pass unmodified.

``REVOKE`` then ``GRANT`` rather than a bare additive ``GRANT``: a column-scoped
grant is additive in Postgres, so the ``REVOKE`` is not strictly required, but
re-issuing the whole list from one constant is what makes the grant readable as
the complete answer to "what may this role update?" -- which is exactly the
property 0004 was written to establish.

What goes red, deliberately
---------------------------
``tests/reconciler/test_oscillation.py::test_the_escalation_reason_column_is_ungranted_to_recon_writer``
exists to pin the degraded state and says so in its own docstring: *"When that
migration lands this test turns red, which is the point: the remediation is then
applied deliberately (flip both assertions and assert the column is populated)
rather than leaving a stale caveat in the docs."* This is that migration; the
test is flipped in the same commit, and
``tests/schema/test_write_boundary_hardening.py::test_the_conflicts_update_grant_is_exactly_the_advancing_columns``
-- which reads the live grant out of ``information_schema.column_privileges``
and compares it to a literal set -- is updated to the new three-column list in
the same commit for the same reason.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_escalation_reason_grant"
down_revision: str | None = "0014_write_set_from_the_value"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RECON_WRITER = "recon_writer"

#: 0004's ``CONFLICT_UPDATE_COLUMNS``, plus the reason the escalation is required
#: to record. Still a closed list, and still narrower than the table: what a
#: conflict *is* (``fingerprint``, ``type``, ``entity_refs``, ``first_seen_run``)
#: and what the lineage scan decided (``oscillating``) stay ungranted.
CONFLICT_UPDATE_COLUMNS = ("status", "last_seen_run", "escalation_reason")

#: What 0004 left behind, restored verbatim by :func:`downgrade`.
CONFLICT_UPDATE_COLUMNS_AT_0004 = ("status", "last_seen_run")


def _columns(names: Sequence[str]) -> str:
    return ", ".join(f'"{name}"' for name in names)


def _rescope(columns: Sequence[str]) -> None:
    op.execute(f'REVOKE UPDATE ON conflicts FROM "{RECON_WRITER}"')
    op.execute(f'GRANT UPDATE ({_columns(columns)}) ON conflicts TO "{RECON_WRITER}"')


def upgrade() -> None:
    _rescope(CONFLICT_UPDATE_COLUMNS)


def downgrade() -> None:
    _rescope(CONFLICT_UPDATE_COLUMNS_AT_0004)
