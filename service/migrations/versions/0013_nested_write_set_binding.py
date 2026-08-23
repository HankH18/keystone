"""The write set is a set of PATHS, not a set of top-level keys.

Revision ID: 0013_nested_write_set_binding
Revises: 0012_sensitive_write_set_binding
Create Date: 2026-08-23

The row 0012 still accepted
---------------------------
``ck_proposals_sensitive_covers_write_set`` (0012) reads the **top-level keys**
of ``action -> 'set'`` with ``jsonb_exists_any``. ``entities.current`` is not
flat: it carries one nested object, ``survived`` (``recon.resolve.SURVIVED_PATHS``),
whose nine members are themselves source-qualified contract paths and six of
which are in contract SS6's ``SENSITIVE_FIELDS``. So this row was accepted::

    INSERT INTO proposals (..., action, confidence, status, sensitive, ...)
    VALUES (..., '{"set": {"survived": {"crm.contact.email": "attacker@evil.test",
                                        "appdb.student.status": "withdrawn",
                                        "appdb.enrollment.stage": "refunded",
                                        ... the other six carried ...}}}'::jsonb,
            0.99, 'pending', false, ...)

It names exactly ONE key -- ``survived`` -- which is in neither committed set, so
``jsonb_exists_any`` sees nothing, ``KS002`` sees ``sensitive = false`` and is
satisfied, and R15's hold does not engage. What it WRITES is three
``SENSITIVE_FIELDS`` paths. Judging the key list is the same shape of mistake as
judging the conflict's classification instead of the write, one level down.

RULING 16 -- the database judges what the statement EFFECTIVELY WRITES
-----------------------------------------------------------------------
Two triggers, on the two legs where a write is decided, sharing one definition
of "effectively writes":

``keystone_proposals_nested_write_set`` (``BEFORE INSERT ON proposals``, ``KS013``)
    Re-derives the effective write paths of ``action -> 'set'`` against the
    target entity's stored ``current`` and refuses ``sensitive = false`` when any
    of them lands on a frozen SS6 path. Chained with ``KS002`` this is 0012's
    guarantee -- *writing a sensitive path forces the hold* -- extended from keys
    to paths. It judges the NESTED half only (``nested_only => true``): a BEFORE
    trigger runs ahead of a CHECK, so judging top-level keys here would take
    0012's own refusals away from it and re-badge them ``KS013``, leaving the
    VALIDATED table invariant untested and its constraint name absent from the
    error a caller sees. Each rule keeps the half it can decide.

``keystone_auto_apply_write_set`` (``BEFORE INSERT ON proposal_events``, ``KS014``)
    The other end, and the one that needs no join and no trust in the proposal
    row at all: an ``applied`` event whose ``actor`` is the unattended actor
    (``recon.apply.AUTO_APPLY_ACTOR``) may not carry a ``before``/``after`` pair
    that differs on a sensitive path -- top-level, or one level inside a nested
    object, which is the whole of ``entities.current``'s shape. R15's actual sentence --
    *the machine may never write a sensitive field unattended* -- asserted
    against the bytes the write produced rather than against an intention
    recorded elsewhere.

Why a trigger and not a CHECK
------------------------------
0012 is a CHECK because its question is answerable from the row. This one is not:
telling a nested member the action REPLACES from one it merely carries through
requires the current canonical value, and a CHECK may not read another table.
That distinction is load-bearing rather than incidental -- contract SS5's
shallow-merge rule requires a fix that writes one member of a nested map to carry
the WHOLE map, so a rule that counted every carried member as a write would make
every possible write to ``survived`` a sensitive write and no member of it could
ever be fixed, including the one eligible path that lives there
(``crm.contact.lifecycle_stage``).

The asymmetry is deliberate and is the same one ``recon.apply.effective_write_paths``
implements: a **top-level** key is written whenever it is named (the author could
have omitted it), a **nested member** is written only when the merge would change
it (contract SS5 gives the author no way to omit it).

``keystone_proposals_nested_write_set`` is ``SECURITY DEFINER`` because it reads
``entities`` on behalf of whichever role is inserting; the ledger trigger is not,
because it reads only ``NEW``. Neither is callable by ``SELECT`` -- a
trigger-returning function is not -- and PostgreSQL checks ``EXECUTE`` at
``CREATE TRIGGER`` time only, so the ``PUBLIC`` grant is revoked at no cost.

What these triggers do NOT do -- stated plainly
-------------------------------------------------
* **They bind new rows, not existing ones.** A CHECK created ``VALIDATED``
  re-checks every row already in the table; a trigger cannot. A database that
  already holds a violating proposal keeps it, and the enumeration query and the
  remediation are in ``docs/proposal-policy.md`` SS8.9 -- as is 0012's, which
  fails outright on such a database rather than installing.
* **They bind the owner's rows, not the owner.** Like 0012: a trigger is not a
  grant, so it fires for the schema owner exactly as for ``recon_writer``, and
  like 0012 the owner may ``DROP TRIGGER``. Defence in depth; the boundary is the
  three non-owner roles.
* **The allow-list half stays in code.** Nothing here requires a non-sensitive
  write to be in ``AUTO_APPLY_ELIGIBLE``, for 0012's reason: that set is R24's
  auto-apply condition, not a statement about which proposals may exist.

Project SQLSTATEs
------------------
``KS013`` -- a proposal claiming ``sensitive = false`` while effectively writing a
SS6 sensitive path reached through a nested object (the top-level half stays with
0012's CHECK).
``KS014`` -- an unattended (auto-apply) canonical write whose own before/after diff
touches a SS6 sensitive path, top-level or one level in.

Both descend exactly ONE level, because ``entities.current`` has exactly one
nested object. A doubly-nested action presents a leaf that is on neither
committed list, which R24's allow-list refuses and SS5's shallow-merge guard
refuses again; the deeper case is covered by being refused, not by being
understood.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_nested_write_set_binding"
down_revision: str | None = "0012_sensitive_write_set_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROPOSAL_TRIGGER = "keystone_proposals_nested_write_set"
EVENT_TRIGGER = "keystone_auto_apply_write_set"
PATHS_FUNCTION = "keystone_effective_write_paths"
DIFF_FUNCTION = "keystone_changed_paths"
PROPOSAL_TRIGGER_FUNCTION = "keystone_proposals_nested_write_set_check"
EVENT_TRIGGER_FUNCTION = "keystone_auto_apply_write_set_check"

#: ``recon.apply.AUTO_APPLY_ACTOR`` -- the actor an UNATTENDED apply stamps on its
#: ledger row. Frozen here for the same reason the path list is: a migration is a
#: historical artifact. ``tests/apply/test_nested_write_set.py`` asserts it still
#: equals the live constant.
AUTO_APPLY_ACTOR_AT_THIS_REVISION = "system:auto-apply"

#: Contract SS6's ``SENSITIVE_FIELDS`` as of this revision -- character for
#: character 0012's frozen list, and asserted equal to the live
#: ``recon.reference.SENSITIVE_FIELDS`` by the same drift alarm.
SENSITIVE_FIELDS_AT_THIS_REVISION: tuple[str, ...] = (
    # legal / identity
    "appdb.student.dob",
    "appdb.student.first_name",
    "appdb.student.last_name",
    "appdb.student.student_number",
    "crm.contact.dob",
    "crm.contact.first_name",
    "crm.contact.last_name",
    # billing ownership -- SS12 D-7
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
    """The frozen path list as a SQL ``text[]`` literal (0012's renderer)."""
    quoted = ", ".join(
        "'" + path.replace("'", "''") + "'" for path in SENSITIVE_FIELDS_AT_THIS_REVISION
    )
    return f"ARRAY[{quoted}]::text[]"


def upgrade() -> None:
    _install_path_functions()
    _install_proposal_trigger()
    _install_event_trigger()


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {EVENT_TRIGGER} ON proposal_events")
    op.execute(f"DROP TRIGGER IF EXISTS {PROPOSAL_TRIGGER} ON proposals")
    op.execute(f"DROP FUNCTION IF EXISTS {EVENT_TRIGGER_FUNCTION}()")
    op.execute(f"DROP FUNCTION IF EXISTS {PROPOSAL_TRIGGER_FUNCTION}()")
    op.execute(f"DROP FUNCTION IF EXISTS {DIFF_FUNCTION}(jsonb, jsonb)")
    # The full argument list, including the defaulted one: `DROP FUNCTION IF
    # EXISTS f(jsonb, jsonb)` does NOT match `f(jsonb, jsonb, boolean DEFAULT
    # false)` -- it matches nothing and no-ops silently, leaving the function
    # behind for the next `upgrade head` to collide with.
    op.execute(f"DROP FUNCTION IF EXISTS {PATHS_FUNCTION}(jsonb, jsonb, boolean)")


# ---------------------------------------------------------------------------
# the shared definition of "what does this statement effectively write?"
# ---------------------------------------------------------------------------
def _install_path_functions() -> None:
    """Two pure functions, mirroring ``recon.apply.effective_write_paths``.

    ``keystone_effective_write_paths(assignments, current)`` judges an ACTION
    against the row it would be merged onto; ``keystone_changed_paths(before,
    after)`` judges a completed write against its own ledger row. Both return
    **leaf** paths -- the source-qualified strings contract SS6's sets are
    written in -- so the membership test downstream is a plain ``= ANY``.
    """
    op.execute(
        f"""
        CREATE FUNCTION {PATHS_FUNCTION}(
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
                    -- A top-level key is written whenever it is NAMED: the author
                    -- could have omitted it, so naming it is a write even when the
                    -- value happens to equal what is already there.
                    CONTINUE WHEN nested_only;
                    result := result || entry.key;
                    CONTINUE;
                END IF;
                old_value := CASE
                    WHEN current_value IS NULL THEN NULL
                    ELSE current_value -> entry.key
                END;
                IF old_value IS NOT NULL AND jsonb_typeof(old_value) = 'object' THEN
                    -- A nested member is written only when the shallow merge would
                    -- CHANGE it: contract SS5 requires the whole map to be carried,
                    -- so a carried-unchanged sibling is not the author's choice.
                    SELECT coalesce(array_agg(name ORDER BY name), ARRAY[]::text[])
                      INTO members
                      FROM (
                            SELECT jsonb_object_keys(entry.value) AS name
                            UNION
                            SELECT jsonb_object_keys(old_value) AS name
                           ) AS keys
                     WHERE (entry.value -> name) IS DISTINCT FROM (old_value -> name);
                ELSE
                    -- No object there today (or no row at all): everything carried
                    -- is new, which is the conservative reading.
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
    )
    op.execute(
        f"""
        CREATE FUNCTION {DIFF_FUNCTION}(before_value jsonb, after_value jsonb)
        RETURNS text[]
        LANGUAGE plpgsql
        IMMUTABLE
        AS $function$
        DECLARE
            result text[] := ARRAY[]::text[];
            name text;
            old_value jsonb;
            new_value jsonb;
            members text[];
        BEGIN
            FOR name IN
                SELECT k FROM (
                    SELECT jsonb_object_keys(coalesce(before_value, '{{}}'::jsonb)) AS k
                    UNION
                    SELECT jsonb_object_keys(coalesce(after_value, '{{}}'::jsonb)) AS k
                ) AS keys ORDER BY k
            LOOP
                old_value := before_value -> name;
                new_value := after_value -> name;
                CONTINUE WHEN old_value IS NOT DISTINCT FROM new_value;
                IF jsonb_typeof(old_value) = 'object' OR jsonb_typeof(new_value) = 'object' THEN
                    SELECT coalesce(array_agg(sub ORDER BY sub), ARRAY[]::text[])
                      INTO members
                      FROM (
                            SELECT jsonb_object_keys(
                                CASE WHEN jsonb_typeof(old_value) = 'object'
                                     THEN old_value ELSE '{{}}'::jsonb END) AS sub
                            UNION
                            SELECT jsonb_object_keys(
                                CASE WHEN jsonb_typeof(new_value) = 'object'
                                     THEN new_value ELSE '{{}}'::jsonb END) AS sub
                           ) AS subkeys
                     WHERE (old_value -> sub) IS DISTINCT FROM (new_value -> sub);
                    IF array_length(members, 1) IS NULL THEN
                        result := result || name;
                    ELSE
                        result := result || members;
                    END IF;
                ELSE
                    result := result || name;
                END IF;
            END LOOP;
            RETURN result;
        END;
        $function$
        """
    )


# ---------------------------------------------------------------------------
# KS013 -- a proposal may not claim `sensitive = false` over a nested write
# ---------------------------------------------------------------------------
def _install_proposal_trigger() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {PROPOSAL_TRIGGER_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
        DECLARE
            target jsonb;
            written text[];
            offending text[];
        BEGIN
            IF NEW.sensitive THEN
                RETURN NEW;
            END IF;
            SELECT e.current INTO target
              FROM entities e
             WHERE e.canonical_id = NEW.target_canonical_id;
            -- nested_only: the TOP-LEVEL half of this question is already a table
            -- invariant (ck_proposals_sensitive_covers_write_set, migration 0012),
            -- and a BEFORE trigger runs ahead of a CHECK -- so judging top-level
            -- keys here would silently take that constraint's refusals away from it
            -- and rename them KS013. This trigger covers exactly what a CHECK
            -- cannot see: the paths reached THROUGH a nested object.
            written := {PATHS_FUNCTION}(
                coalesce(NEW.action -> 'set', '{{}}'::jsonb), target, true);
            SELECT coalesce(array_agg(p ORDER BY p), ARRAY[]::text[])
              INTO offending
              FROM unnest(written) AS p
             WHERE p = ANY({_path_array_sql()});
            IF array_length(offending, 1) IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS013',
                    MESSAGE = format(
                        'proposal claims sensitive = false but its action writes %s',
                        offending),
                    DETAIL = format(
                        'the effective write set is %s; contract SS6 lists %s in '
                        'SENSITIVE_FIELDS. R15 forbids a sensitive-field write at any '
                        'confidence, and a path reached through a nested object is '
                        'still a path this statement writes.',
                        written, offending),
                    HINT = 'a proposal that writes a sensitive path is born '
                           'sensitive_hold (sensitive = true, KS002) and goes to a human.';
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {PROPOSAL_TRIGGER_FUNCTION}() FROM PUBLIC")
    op.execute(
        f"""
        CREATE TRIGGER {PROPOSAL_TRIGGER}
        BEFORE INSERT ON proposals
        FOR EACH ROW EXECUTE FUNCTION {PROPOSAL_TRIGGER_FUNCTION}()
        """
    )
    op.execute(
        f"""
        COMMENT ON TRIGGER {PROPOSAL_TRIGGER} ON proposals IS
        'KS013. R15 bound to the EFFECTIVE write set rather than to the top-level keys '
        'of action->''set'': a proposal may not claim sensitive = false while the merge '
        'it authorises would change a contract SS6 SENSITIVE_FIELDS path, including one '
        'reached through a nested object such as `survived`. INSERT only: it judges the '
        'row against the entity as it stands at birth, and re-judging on UPDATE would '
        'let a later change to the entity block a reviewer''s status move.'
        """
    )


# ---------------------------------------------------------------------------
# KS014 -- an UNATTENDED write may not move a sensitive path, at any depth
# ---------------------------------------------------------------------------
def _install_event_trigger() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {EVENT_TRIGGER_FUNCTION}()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $function$
        DECLARE
            changed text[];
            offending text[];
        BEGIN
            IF NEW.event <> 'applied'
               OR NEW.actor <> '{AUTO_APPLY_ACTOR_AT_THIS_REVISION}' THEN
                RETURN NEW;
            END IF;
            changed := {DIFF_FUNCTION}(NEW.before, NEW.after);
            SELECT coalesce(array_agg(p ORDER BY p), ARRAY[]::text[])
              INTO offending
              FROM unnest(changed) AS p
             WHERE p = ANY({_path_array_sql()});
            IF array_length(offending, 1) IS NOT NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'KS014',
                    MESSAGE = format(
                        'an unattended apply would write the sensitive path(s) %s',
                        offending),
                    DETAIL = format(
                        'this applied event changes %s; contract SS6 lists %s in '
                        'SENSITIVE_FIELDS and R15 forbids the machine writing one at any '
                        'confidence. A human-approved manual apply (actor system:apply) '
                        'is a different act and is not refused here.',
                        changed, offending);
            END IF;
            RETURN NEW;
        END;
        $function$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {EVENT_TRIGGER_FUNCTION}() FROM PUBLIC")
    op.execute(
        f"""
        CREATE TRIGGER {EVENT_TRIGGER}
        BEFORE INSERT ON proposal_events
        FOR EACH ROW EXECUTE FUNCTION {EVENT_TRIGGER_FUNCTION}()
        """
    )
    op.execute(
        f"""
        COMMENT ON TRIGGER {EVENT_TRIGGER} ON proposal_events IS
        'KS014. R15 asserted against the BYTES an unattended write produced: an applied '
        'event whose actor is the auto-apply actor may not carry a before/after pair '
        'that differs on a contract SS6 SENSITIVE_FIELDS path, at any nesting depth. '
        'Needs no join and no trust in the proposal row: the diff is in the ledger row '
        'itself, and KS011/KS010 already bind that row to the canonical write.'
        """
    )
