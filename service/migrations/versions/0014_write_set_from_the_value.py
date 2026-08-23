"""The write set is read off the VALUE the merge would produce, at every shape.

Revision ID: 0014_write_set_from_the_value
Revises: 0013_nested_write_set_binding
Create Date: 2026-08-23

The row 0013 still accepted
----------------------------
``keystone_effective_write_paths`` (0013) mirrored a Python rule that guarded on
the SHAPE of the value being assigned: it descended into a top-level key only
when ``jsonb_typeof(value) = 'object'``. So this row was accepted with
``sensitive = false, status = 'pending'``::

    action = '{"set": {"survived": "wiped"}}'

``survived`` is the one nested object ``entities.current`` carries
(``recon.resolve.SURVIVED_PATHS``, nine members, **six** of them in contract
SS6's ``SENSITIVE_FIELDS``). Replacing it with a scalar erases all nine. But the
assigned value is not an object, so 0013's function reported the single leaf
``survived`` -- which is on neither committed list -- ``KS013`` (which asks for
the NESTED half only) reported nothing at all, and 0012's key-level CHECK saw the
key ``survived``, also on neither list. **The most destructive shape of the
attack 0013 was written to stop was the one shape it did not judge.** A list, a
JSON ``null`` and an absent current value each landed in the same blind spot for
the same reason.

This is the third time one rule has been stated over a SHAPE and had to be
restated over a VALUE: ``recon.apply.merge_preview`` required both sides to be
objects (so the same scalar was reported *safe*), then ``effective_write_paths``
required the assigned side to be one, and now this function.

RULING 17 -- one comparison, not four branches
-----------------------------------------------
``keystone_effective_write_paths`` is rewritten (``CREATE OR REPLACE``, so both
of 0013's triggers pick it up with no ``CREATE TRIGGER`` churn) to the rule
``recon.apply.effective_write_paths`` now states:

    the write set is every leaf on which ``current || assignments`` would DIFFER
    from ``current``, one level deep -- plus every top-level key the action names
    with a non-object value, which is written whether or not it changes.

Mechanically, per named key, and the two clauses are independent:

* the action assigns a **non-object** there -> the top-level key is written. No
  comparison and no look at the row: its author could have omitted the key, so
  naming it is writing it. (Suppressed under ``nested_only``, which is the half
  ``KS013`` asks for -- 0012's CHECK owns the top-level half and a BEFORE trigger
  running ahead of it must not re-badge its refusals.)
* **either side is an object** -> the two memberships are compared and every
  member that is added, dropped or given a different value is written. A
  non-object side contributes no members, which is precisely what makes
  ``{"survived": "wiped"}`` yield all nine erasures and ``{"survived": {...}}``
  over a scalar, a list, a JSON ``null`` or an absent key yield every member it
  introduces -- out of ONE comparison instead of four special cases.
* an assigned object the merge would change nothing about reports the container
  key, so a refusal names something real instead of an empty write set.

The asymmetry (a top-level key is written when NAMED, a nested member only when
CHANGED) is unchanged and is not a shape guard: contract SS5's shallow merge
forces a fix that writes one member of a map to carry the WHOLE map, so counting
carried siblings would make every possible write to ``survived`` a sensitive
write and no member of it -- including the one eligible path that lives there,
``crm.contact.lifecycle_stage`` -- could ever be fixed.

What this changes for ``KS013``, measurably
--------------------------------------------
``{"set": {"survived": "wiped"}}`` (and the list, and the JSON ``null``) is now
refused at INSERT with ``KS013`` when the proposal claims ``sensitive = false``,
because the effective write set contains ``crm.contact.email``,
``appdb.student.status``, ``appdb.enrollment.stage``, ``appdb.student.first_name``,
``appdb.student.last_name`` and ``crm.deal.stage``.
``tests/apply/test_nested_write_set.py`` asserts it, drops the trigger inside a
rolled-back transaction and shows the row landing again, and re-runs the
Python/SQL parity comparison over every shape rather than over the five 0013
happened to cover.

``keystone_changed_paths`` (``KS014``) is NOT touched: it already descended
whenever EITHER side of the ledger diff was an object, which is the same rule.
It was the only one of the three that never had the bug.

What this migration does NOT do -- stated plainly
---------------------------------------------------
* **It binds new rows, not existing ones.** ``CREATE OR REPLACE`` changes what
  the triggers compute from now on; a database that already holds a proposal
  whose write set is only visible under the NEW rule keeps it. The enumeration
  query in ``docs/proposal-policy.md`` SS8.9 uses this function, so re-running it
  after this upgrade is what finds them -- and re-running it is not automatic.
* **It binds the owner's rows, not the owner.** Like 0012 and 0013: a function is
  not a grant, so it judges the schema owner exactly as it judges
  ``recon_writer``, and the owner may ``DROP TRIGGER`` or replace this function
  again. Defence in depth; the boundary is the three non-owner roles.
* **The allow-list half stays in code.** Nothing here requires a non-sensitive
  write to be in ``AUTO_APPLY_ELIGIBLE``, for 0012's reason.
* **It adds no rule about a member being ADDED to a nested map.** A look-alike
  member (``CRM.contact.email``, ``"crm.contact.email "``,
  ``crm.contact.ema`` + U+0131 + ``l``) writes a leaf that is on neither
  committed list, so R24's gate refuses it -- but a human-pressed apply is not
  behind R24's gate, and what refuses it there is
  ``recon.apply.merge_preview``'s ``introduced`` arm
  (``ApplyError('nested_member_introduced')``), which is Python. Recorded here
  because "the database backstops it" would be false.

``downgrade`` restores 0013's function body verbatim, so the revision is
reversible in both directions and ``downgrade base`` still tears the schema down.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_write_set_from_the_value"
down_revision: str | None = "0013_nested_write_set_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PATHS_FUNCTION = "keystone_effective_write_paths"

#: The rule as of THIS revision. One comparison per named key, driven by the
#: value the merge would produce rather than by the shape of either side.
_NEW_BODY = f"""
CREATE OR REPLACE FUNCTION {PATHS_FUNCTION}(
    assignments jsonb, current_value jsonb, nested_only boolean DEFAULT false)
RETURNS text[]
LANGUAGE plpgsql
IMMUTABLE
AS $function$
DECLARE
    result text[] := ARRAY[]::text[];
    entry record;
    old_value jsonb;
    new_members jsonb;
    old_members jsonb;
    members text[];
BEGIN
    IF assignments IS NULL OR jsonb_typeof(assignments) <> 'object' THEN
        RETURN result;
    END IF;
    FOR entry IN SELECT key, value FROM jsonb_each(assignments) ORDER BY key
    LOOP
        old_value := CASE
            WHEN current_value IS NULL THEN NULL
            ELSE current_value -> entry.key
        END;
        -- The two sides' MEMBERSHIPS. A non-object side has none, which is what
        -- collapses list / scalar / null / absent / object into one comparison.
        new_members := CASE
            WHEN jsonb_typeof(entry.value) = 'object' THEN entry.value ELSE '{{}}'::jsonb END;
        old_members := CASE
            WHEN jsonb_typeof(old_value) = 'object' THEN old_value ELSE '{{}}'::jsonb END;

        -- Clause 1: the action assigns a NON-object here, so this key is a leaf
        -- its author chose to name -- and naming it is writing it, whatever the
        -- row holds today. `nested_only` is KS013's half: the top-level question
        -- is already migration 0012's VALIDATED CHECK and a BEFORE trigger must
        -- not take its refusals away from it.
        IF jsonb_typeof(entry.value) <> 'object' AND NOT nested_only THEN
            result := result || entry.key;
        END IF;

        -- Clause 2: either side is an object -> every member the merge would
        -- change, ADD or DROP. `||` replaces a nested object wholesale, so an
        -- omitted member is an erasure, not a carry.
        IF jsonb_typeof(entry.value) = 'object' OR jsonb_typeof(old_value) = 'object' THEN
            SELECT coalesce(array_agg(name ORDER BY name), ARRAY[]::text[])
              INTO members
              FROM (
                    SELECT jsonb_object_keys(new_members) AS name
                    UNION
                    SELECT jsonb_object_keys(old_members) AS name
                   ) AS keys
             WHERE (new_members -> name) IS DISTINCT FROM (old_members -> name);
            IF array_length(members, 1) IS NOT NULL THEN
                result := result || members;
            ELSIF jsonb_typeof(entry.value) = 'object' AND NOT nested_only THEN
                -- An assigned object that changes nothing: name the container, so
                -- the refusal points at something real instead of an empty set.
                result := result || entry.key;
            END IF;
        END IF;
    END LOOP;
    SELECT coalesce(array_agg(DISTINCT p ORDER BY p), ARRAY[]::text[])
      INTO result
      FROM unnest(result) AS p;
    RETURN result;
END;
$function$
"""

#: Migration 0013's body, character for character, so `downgrade` restores the
#: rule this revision replaced rather than a paraphrase of it.
_BODY_AT_0013 = f"""
CREATE OR REPLACE FUNCTION {PATHS_FUNCTION}(
    assignments jsonb, current_value jsonb, nested_only boolean DEFAULT false)
RETURNS text[]
LANGUAGE plpgsql
IMMUTABLE
AS $function$
DECLARE
    result text[] := ARRAY[]::text[];
    entry record;
    old_value jsonb;
    members text[];
BEGIN
    IF assignments IS NULL OR jsonb_typeof(assignments) <> 'object' THEN
        RETURN result;
    END IF;
    FOR entry IN SELECT key, value FROM jsonb_each(assignments) ORDER BY key
    LOOP
        IF jsonb_typeof(entry.value) <> 'object' THEN
            CONTINUE WHEN nested_only;
            result := result || entry.key;
            CONTINUE;
        END IF;
        old_value := CASE
            WHEN current_value IS NULL THEN NULL
            ELSE current_value -> entry.key
        END;
        IF old_value IS NOT NULL AND jsonb_typeof(old_value) = 'object' THEN
            SELECT coalesce(array_agg(name ORDER BY name), ARRAY[]::text[])
              INTO members
              FROM (
                    SELECT jsonb_object_keys(entry.value) AS name
                    UNION
                    SELECT jsonb_object_keys(old_value) AS name
                   ) AS keys
             WHERE (entry.value -> name) IS DISTINCT FROM (old_value -> name);
        ELSE
            SELECT coalesce(array_agg(name ORDER BY name), ARRAY[]::text[])
              INTO members
              FROM jsonb_object_keys(entry.value) AS name;
        END IF;
        IF array_length(members, 1) IS NULL THEN
            CONTINUE WHEN nested_only;
            result := result || entry.key;
        ELSE
            result := result || members;
        END IF;
    END LOOP;
    RETURN result;
END;
$function$
"""

_COMMENT = f"""
COMMENT ON FUNCTION {PATHS_FUNCTION}(jsonb, jsonb, boolean) IS
'RULING 17. The effective write set of an action against the row it would be '
'merged onto: every leaf on which `current || assignments` would differ from '
'`current`, one level deep, plus every top-level key the action names with a '
'non-object value. Read off the VALUE, never off the shape of either side -- '
'the shape-guarded version admitted `{{"survived": "wiped"}}`, a scalar erasing '
'a nine-key nested map six of whose members are sensitive. Mirrors '
'recon.apply.effective_write_paths; tests/apply/test_nested_write_set.py '
'asserts the two equal over every shape.'
"""


def upgrade() -> None:
    op.execute(_NEW_BODY)
    op.execute(_COMMENT)


def downgrade() -> None:
    op.execute(_BODY_AT_0013)
    op.execute(f"COMMENT ON FUNCTION {PATHS_FUNCTION}(jsonb, jsonb, boolean) IS NULL")
