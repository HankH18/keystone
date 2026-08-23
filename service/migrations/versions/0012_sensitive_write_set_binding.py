"""`proposals.sensitive` is bound to what `action->'set'` actually WRITES.

Revision ID: 0012_sensitive_write_set_binding
Revises: 0011_link_provenance_index
Create Date: 2026-08-23

The hole this closes, stated as the row that used to be accepted
-----------------------------------------------------------------
``keystone_proposal_born_pending`` (``KS002``, migration 0005/0006) binds
``proposals.sensitive`` to the birth **status**: a row with ``sensitive = true``
and any birth status other than ``sensitive_hold`` is refused. Nothing bound
``sensitive`` to the field paths the ``action`` actually writes, so this row was
accepted by every committed constraint::

    INSERT INTO proposals (..., action, confidence, status, sensitive, ...)
    VALUES (..., '{"set": {"crm.contact.dob": "2010-01-01"}}'::jsonb,
            0.99, 'pending', false, ...)

``recon/sensitive.py`` said so in prose, and
``tests/reconciler/test_reconcile_run.py::test_the_database_refuses_the_row_the_code_refuses``
(renamed from ``test_the_database_accepts_the_row_the_code_refuses``)
pinned it as a **measurement** -- it asserted the bad row IS accepted, precisely
so the gap could not be quietly overstated, and asked to be flipped the day a
migration closed it. This is that migration; that test now asserts the refusal.

It mattered because it was the *only* thing standing behind two Python checks.
Every other guarantee in this project is a trigger or a grant: ``KS001`` the
citation, ``KS002`` the birth status, ``KS004`` the transition graph, ``KS010``
the written content, ``KS012`` the reversal. R15 -- the requirement the brief
grades -- was in-process only, in ``recon.sensitive.classify`` and in
``recon.reconciler._assert_action_matches_classification``, both of them Python
in the same process as the code they guard. A red team then produced the write
that neither of them was looking at: the auto-apply gate classified the
**conflict** and never inspected the **action**, and a ``C2`` proposal carrying
``{"set": {"crm.contact.email": "..."}}`` at confidence 0.99 was auto-applied.

RULING 15 -- the write set decides ``sensitive``, and the database says so
--------------------------------------------------------------------------
``ck_proposals_sensitive_covers_write_set``::

    sensitive OR NOT jsonb_exists_any(coalesce(action -> 'set', '{}'), <SS6 list>)

Read directly: *a proposal may not claim* ``sensitive = false`` *while naming a*
``SENSITIVE_FIELDS`` *path in its action*. Chained with ``KS002`` -- ``sensitive``
implies born ``sensitive_hold`` -- the database now enforces the whole of R15's
antecedent: **writing a sensitive path forces the hold**, no matter what
classified the conflict, and at every confidence.

A ``CHECK`` rather than a trigger, on purpose. 0007's
``ck_proposals_action_vocabulary`` is the precedent and the reason is the same:
a CHECK is a *table invariant*, evaluated for every INSERT and UPDATE by every
principal, created VALIDATED so it binds existing rows as well as future ones,
and unlike a grant it is not something a role can be given an exception to. It
raises ``23514`` rather than a ``KS`` SQLSTATE, so the tests assert on the
**constraint name**, which is carried in the error's diagnostics and is as exact
as a project SQLSTATE would be.

``jsonb_exists_any(...)`` and not the ``?|`` operator it backs: the two are the
same function, and spelling it out keeps a literal ``?`` out of a statement that
travels through a DBAPI where ``?`` is a parameter marker in some paramstyles.

The path list is FROZEN HERE, and a test binds it to the live constant
-----------------------------------------------------------------------
:data:`SENSITIVE_FIELDS_AT_THIS_REVISION` is contract SS6's set as of this
revision, written out rather than imported from ``recon.reference``. A migration
is a historical artifact: one that imported the live constant would silently
change what an old database enforces whenever someone edited a Python set, which
is the opposite of what a migration is for.

The cost of freezing is drift, so drift is made loud instead of prevented:
``tests/apply/test_write_set_backstop.py`` reads the installed constraint's own
definition back out of ``pg_get_constraintdef()``, parses the literals out of it,
and asserts the set equals ``recon.reference.SENSITIVE_FIELDS`` today. Adding a
path to SS6 without a follow-up migration is therefore a RED test naming both
sets, not a quiet weakening of the backstop.

What this constraint does NOT cover -- stated plainly
------------------------------------------------------
* **It binds the schema owner's ROWS, not the schema owner.** A CHECK is
  evaluated for the owner exactly as for ``recon_writer``; there is no
  ``BYPASSRLS``-shaped escape and no grant that turns it off. But the owner may
  ``ALTER TABLE proposals DROP CONSTRAINT`` it, because the owner is who runs
  migrations. So owner-level enforcement here is **defence in depth, not a
  boundary**: it makes the bad row unrepresentable for every principal that
  writes rows, and it does not pretend to constrain the principal that writes
  DDL. The actual boundary in this project is the three-role separation, where
  ``recon_writer`` / ``review_writer`` / ``apply_writer`` are non-owner logins
  holding column-scoped grants -- and for all three, this constraint is absolute.
* **The allow-list half stays in code.** The constraint refuses
  ``sensitive = false`` over a ``SENSITIVE_FIELDS`` path. It does not require
  every non-sensitive write to be in ``AUTO_APPLY_ELIGIBLE``: that set is R24's
  auto-apply allow-list, not a statement about which proposals may exist, and a
  human-reviewed manual apply of an unlisted path is legitimate. R24's gate
  (``recon.apply.write_set_gate``) is where the allow-list binds, and it is code.
* **It does not stop a HELD proposal writing a non-sensitive path.** The C4
  re-targeting escape of contract SS6/SS12 D-7 -- a C4 re-pointed at
  ``crm.contact.external_id`` -- would carry ``sensitive = false`` and an
  eligible write set, so this constraint is silent on it. What refuses it is the
  classifier (``FIX_TARGETS['C4']`` pins ``crm.contact.email``) plus R24's
  approved-case-type condition. Recorded in ``docs/proposal-policy.md`` SS8.

Project SQLSTATEs (unchanged by this revision; it raises 23514)
----------------------------------------------------------------
``KS001``-``KS012`` as listed in 0007-0010. This revision adds a named CHECK, not
a new error class, because a CHECK's constraint name is already an exact handle.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_sensitive_write_set_binding"
down_revision: str | None = "0011_link_provenance_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT_NAME = "ck_proposals_sensitive_covers_write_set"
TABLE = "proposals"

#: Contract SS6's ``SENSITIVE_FIELDS``, **as of this revision**, frozen into the
#: migration on purpose (see the module docstring). Kept in the contract's own
#: grouping and sorted within each group so a future diff is readable.
SENSITIVE_FIELDS_AT_THIS_REVISION: tuple[str, ...] = (
    # legal / identity
    "appdb.student.dob",
    "appdb.student.first_name",
    "appdb.student.last_name",
    "appdb.student.student_number",
    "crm.contact.dob",
    "crm.contact.first_name",
    "crm.contact.last_name",
    # billing ownership -- SS12 D-7: "the payer or billing-owner of any payment
    # OR ACCOUNT". This is the group the red team's write landed in.
    "appdb.enrollment.billing_owner_email",
    "appdb.student.guardian2_email",
    "appdb.student.guardian_email",
    "crm.contact.email",
    "payments.payment.payer_email",
    "payments.payment.payer_name",
    # financially-consequential status -- SS12 D-8
    "appdb.enrollment.deposit_paid_at",
    "appdb.enrollment.stage",
    "appdb.student.status",
    "crm.deal.stage",
    "payments.payment.status",
    # consent / compliance
    "appdb.student.communication_opt_out",
    "crm.contact.marketing_consent",
)


def _path_array_sql() -> str:
    """The frozen path list as a SQL ``text[]`` literal.

    Every element is a committed contract path -- lowercase, dot-separated, drawn
    from a Python tuple in this file and never from user input -- but the quoting
    is still done by doubling any apostrophe rather than by trusting that, so the
    rendering is correct by construction rather than by inspection of the data.
    """
    quoted = ", ".join(
        "'" + path.replace("'", "''") + "'" for path in SENSITIVE_FIELDS_AT_THIS_REVISION
    )
    return f"ARRAY[{quoted}]::text[]"


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE {TABLE} ADD CONSTRAINT {CONSTRAINT_NAME} CHECK (
            sensitive
            OR NOT jsonb_exists_any(
                coalesce(action -> 'set', '{{}}'::jsonb), {_path_array_sql()})
        )
        """
    )
    op.execute(
        f"""
        COMMENT ON CONSTRAINT {CONSTRAINT_NAME} ON {TABLE} IS
        'R15, bound to the WRITE SET rather than to the classification: a proposal '
        'may not claim sensitive = false while action->''set'' names a contract SS6 '
        'SENSITIVE_FIELDS path. Chained with KS002 (sensitive implies born '
        'sensitive_hold) this makes "writing a sensitive path forces the hold" a '
        'property of the table rather than of two Python call sites. The path list is '
        'frozen at revision 0012; tests/apply/test_write_set_backstop.py reads it back '
        'out of pg_get_constraintdef() and fails loudly if recon.reference has drifted.'
        """
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT_NAME, TABLE, type_="check")
