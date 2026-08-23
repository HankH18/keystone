-- @rule_id: R-010
-- @rule_version: v1
-- @conflict: C10
-- @scope: stg_crm_contact
--
-- SS5.5 C10 -- merge-collapsed record.
--
-- "one CRM contact whose `('ext')` candidate and `('namedob')` candidate resolve to
--  **two different, non-null** students in `entity_link_candidates`"
-- entity_refs: exactly three -- `crm:contact:<id>`, the student reached by the `ext`
-- key, and the student reached by the `namedob` key. **No transitive expansion.**
--
-- SS4.7: `R-010` is evaluated over `entity_link_candidates`, **never** over
-- `entity_links` -- every candidate pair `match_keys` produced is persisted
-- regardless of outcome, and the collapse is visible only in the resolution the
-- cascade discarded. `er_candidate` is that table, materialized from
-- `recon.er.resolve`.
--
-- SS2.1 ruling 10: no `namedob` key is emitted unless first, last AND dob are all
-- present, so a contact with a missing or unparseable DOB carries no `namedob`
-- candidate at all and cannot reach this predicate.
--
-- FP guard (`G5`, `G21`): globally the `namedob` triple resolves to at most one
-- person except the tuples the C3/C10 planters registered, so a normal contact's two
-- key classes resolve to the same student or to none.
--
-- **`ext_count = 1 AND namedob_count = 1` is a deliberate narrowing beyond SS5.5.**
-- SS5.5's C10 row says only "two different, non-null students". This rule additionally
-- requires each key class to have resolved to EXACTLY ONE student, so a contact whose
-- `ext` or `namedob` key reached two or more students is left `ok` rather than
-- reported against an arbitrary `min()` of them. That population is empty on the
-- committed dataset (`G5` makes `student.id` independent of identity fields, and the
-- name-collision allowlist is what bounds `namedob`), so the two readings agree here.
-- Beyond that population the wider reading would have to pick which pair of students
-- to name in the three-ref `entity_refs`, and SS5.5 pins C10 at exactly three refs
-- with **no transitive expansion** -- there is no contract-defined answer, so the rule
-- declines rather than guessing.

WITH candidates AS (
    SELECT source_ref,
           count(*) FILTER (WHERE key_class = 'ext')                            AS ext_count,
           count(*) FILTER (WHERE key_class = 'namedob')                        AS namedob_count,
           min(resolved_ref COLLATE "C") FILTER (WHERE key_class = 'ext')       AS ext_ref,
           min(resolved_ref COLLATE "C") FILTER (WHERE key_class = 'namedob')   AS namedob_ref
      FROM er_candidate
     WHERE generation = %(generation)s
     GROUP BY source_ref
),
evaluated AS (
    SELECT c.source_ref,
           c.first_norm,
           c.last_norm,
           to_char(c.dob_norm, 'YYYY-MM-DD') AS dob_norm,
           candidates.ext_ref,
           candidates.namedob_ref,
           (
                candidates.ext_count = 1
            AND candidates.namedob_count = 1
            AND candidates.ext_ref <> candidates.namedob_ref
           ) AS fires
      FROM stg_crm_contact AS c
      LEFT JOIN candidates ON candidates.source_ref = c.source_ref
     WHERE c.generation = %(generation)s
)
SELECT e.source_ref                                              AS record_ref,
       'contact'                                                 AS entity_type,
       CASE WHEN COALESCE(e.fires, false) THEN 'conflict' ELSE 'ok' END AS verdict,
       CASE WHEN COALESCE(e.fires, false) THEN jsonb_build_object(
                'conflicts', jsonb_build_array(jsonb_build_object(
                    'conflict_type', 'C10',
                    'contact_refs',  jsonb_build_array(e.source_ref),
                    'student_refs',  jsonb_build_array(e.ext_ref, e.namedob_ref),
                    'observed_values', jsonb_build_object(
                        'ext_resolved_ref',     e.ext_ref,
                        'namedob_resolved_ref', e.namedob_ref,
                        'first_norm',           e.first_norm,
                        'last_norm',            e.last_norm,
                        'dob_norm',             e.dob_norm
                    )
                ))
            ) END                                                AS detail
  FROM evaluated AS e
