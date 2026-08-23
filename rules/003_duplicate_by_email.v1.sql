-- @rule_id: R-003
-- @rule_version: v1
-- @conflict: C3
-- @scope: stg_crm_contact
--
-- SS5.5 C3 -- duplicate-by-email (in-source).
--
-- "two CRM contacts, generation 3, equal `email_norm` **and** equal
--  `(first_norm, last_norm)` **and** (`dob_norm` equal or either null)"
-- entity_refs: the two contact refs, sorted.
--
-- SS5.2: C3 emits exactly **one entry per unordered pair**. The scope of this rule
-- is one row per contact (SS5.5's scope table), so both members of a pair carry the
-- same conflict object and the runner de-duplicates on the SS5.4 harness key
-- `(type, sorted(entity_refs))`. `G8` forbids any 3-or-more-way collision, so the
-- pair count is never ambiguous -- but a contact caught in two pairs would still be
-- stamped once, with both conflicts in `detail.conflicts`.
--
-- The DOB clause is SS5.1's pinned comparison form, negated: a comparison runs only
-- when both sides are non-null, so "either null" is *not* a disagreement. Writing it
-- as `a IS DISTINCT FROM b` would make a null DOB exclude the pair, which is the
-- opposite of the contract and is why the rule lint bans that operator outright.
--
-- FP guard (`G5`, `G23`, `G8`): siblings share the guardian email but are guaranteed
-- to differ in `(first_norm, last_norm)`, and no other same-`email_norm` contact pair
-- may exist.

WITH scope AS (
    SELECT c.source_ref,
           c.email_norm,
           c.first_norm,
           c.last_norm,
           c.dob_norm
      FROM stg_crm_contact AS c
     WHERE c.generation = %(generation)s
),
pairs AS (
    SELECT a.source_ref AS left_ref,
           b.source_ref AS right_ref,
           a.email_norm,
           a.first_norm,
           a.last_norm,
           a.dob_norm AS left_dob,
           b.dob_norm AS right_dob
      FROM scope AS a
      JOIN scope AS b
             ON b.email_norm = a.email_norm
            AND b.first_norm = a.first_norm
            AND b.last_norm  = a.last_norm
            AND (a.source_ref COLLATE "C") < (b.source_ref COLLATE "C")
     WHERE a.email_norm IS NOT NULL
       AND a.first_norm IS NOT NULL
       AND a.last_norm  IS NOT NULL
       AND NOT (a.dob_norm IS NOT NULL AND b.dob_norm IS NOT NULL AND a.dob_norm <> b.dob_norm)
),
entries AS (
    SELECT pairs.left_ref  AS member_ref, pairs.* FROM pairs
     UNION ALL
    SELECT pairs.right_ref AS member_ref, pairs.* FROM pairs
),
grouped AS (
    SELECT entries.member_ref,
           jsonb_agg(
               jsonb_build_object(
                   'conflict_type',   'C3',
                   'contact_refs',    jsonb_build_array(entries.left_ref, entries.right_ref),
                   'observed_values', jsonb_build_object(
                       'email_norm', entries.email_norm,
                       'first_norm', entries.first_norm,
                       'last_norm',  entries.last_norm,
                       'dob_norm_a', to_char(entries.left_dob,  'YYYY-MM-DD'),
                       'dob_norm_b', to_char(entries.right_dob, 'YYYY-MM-DD')
                   )
               )
               ORDER BY entries.left_ref COLLATE "C", entries.right_ref COLLATE "C"
           ) AS conflicts
      FROM entries
     GROUP BY entries.member_ref
)
SELECT scope.source_ref                                                AS record_ref,
       'contact'                                                       AS entity_type,
       CASE WHEN grouped.conflicts IS NULL THEN 'ok' ELSE 'conflict' END AS verdict,
       CASE WHEN grouped.conflicts IS NULL THEN NULL
            ELSE jsonb_build_object('conflicts', grouped.conflicts) END  AS detail
  FROM scope
  LEFT JOIN grouped ON grouped.member_ref = scope.source_ref
