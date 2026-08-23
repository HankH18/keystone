-- @rule_id: R-004
-- @rule_version: v1
-- @conflict: C4
-- @scope: stg_crm_contact
--
-- SS5.5 C4 -- same-person-different-emails.
--
-- "contact<->student where `entity_links.method == 'L3'` (read off `entity_links`,
--  never re-derived) **and** `norm_email(contact.email)` is not one of the student's
--  normalized guardian emails" -- entity_refs: identity refs.
--
-- `method` is written by `recon/er.py` and is **never** re-derived by a rule
-- (SS4.7): this joins `er_contact_student`, which is materialized straight off
-- `recon.er.resolve`'s accepted links. Re-deriving "would L3 have fired?" in SQL is
-- exactly the second implementation SS0 forbids.
--
-- FP guard (`G3`, `G4`): dot / `+alias` variation is emitted only on gmail, where it
-- normalizes equal and the pair links by `L2`; on every other domain one person's
-- addresses are byte-identical after `norm_email`. The `COALESCE(... , false)` is
-- load-bearing -- `guardian2_email_norm` is NULL on ~40 percent of students, and an
-- unguarded `=` against it yields NULL, which `NOT` would leave NULL and the row
-- would silently fall out of both branches.

WITH scope AS (
    SELECT c.source_ref,
           c.email_norm,
           link.method,
           link.person_key,
           s.email_norm            AS guardian_email_norm,
           s.guardian2_email_norm  AS guardian2_email_norm
      FROM stg_crm_contact AS c
      LEFT JOIN er_contact_student AS link
             ON link.contact_ref = c.source_ref
      LEFT JOIN stg_student AS s
             ON s.source_ref = link.student_ref
            AND s.generation = %(generation)s
     WHERE c.generation = %(generation)s
),
evaluated AS (
    SELECT scope.*,
           person.identity_refs,
           (
                scope.method = 'L3'
            AND NOT (
                    COALESCE(scope.email_norm = scope.guardian_email_norm, false)
                 OR COALESCE(scope.email_norm = scope.guardian2_email_norm, false)
                )
           ) AS fires
      FROM scope
      LEFT JOIN er_person AS person ON person.person_key = scope.person_key
),
guardians AS (
    SELECT evaluated.source_ref,
           COALESCE(jsonb_agg(g.value ORDER BY g.value COLLATE "C"), '[]'::jsonb) AS emails
      FROM evaluated
      LEFT JOIN LATERAL (
           SELECT DISTINCT value
             FROM unnest(ARRAY[evaluated.guardian_email_norm, evaluated.guardian2_email_norm])
                  AS t(value)
            WHERE value IS NOT NULL
      ) AS g ON true
     GROUP BY evaluated.source_ref
)
SELECT e.source_ref                                       AS record_ref,
       'contact'                                          AS entity_type,
       CASE WHEN e.fires THEN 'conflict' ELSE 'ok' END     AS verdict,
       CASE WHEN e.fires THEN jsonb_build_object(
                'conflicts', jsonb_build_array(jsonb_build_object(
                    'conflict_type',   'C4',
                    'identity_refs',   e.identity_refs,
                    'observed_values', jsonb_build_object(
                        'contact_email_norm',           e.email_norm,
                        'student_guardian_email_norms', guardians.emails,
                        'link_method',                  e.method
                    )
                ))
            ) END                                         AS detail
  FROM evaluated AS e
  JOIN guardians ON guardians.source_ref = e.source_ref
