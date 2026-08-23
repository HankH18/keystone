-- @rule_id: R-005
-- @rule_version: v1
-- @conflict: C5
-- @scope: stg_student
--
-- SS5.5 C5 -- record-in-one-source-only.
--
-- "student with `STATUS_TO_FUNNEL(status) == enrolled` **and** no `entity_links`
--  contact **and** no `P1..P3`-attributed payment" -- entity_refs: identity refs.
--
-- `STATUS_TO_FUNNEL(status)` is materialized upstream as `stg_student.status_compare`
-- (it is the column ingest fills with `STATUS_TO_FUNNEL[norm_enum('status', ...)]`),
-- so the rule reads a canonical token and never folds `active` into `enrolled`
-- itself. An unmappable status leaves `status_compare` NULL, which cannot equal
-- 'enrolled' and therefore cannot fire.
--
-- ABSENCE RULE (SS5.3): two of its three clauses are absences.
--
-- FP guard (`G16`, `G2`): legitimately partial-presence students carry
-- `status IN {prospect, applied, withdrawn}` only; `enrolled`/`active` with no
-- contact and no payment occurs on a planted C5 and nowhere else.

WITH scope AS (
    SELECT s.source_ref,
           s.status_compare,
           p.identity_refs,
           COALESCE(p.contact_count, 0) AS contact_count,
           COALESCE(p.payment_count, 0) AS payment_count
      FROM stg_student AS s
      LEFT JOIN er_person AS p
             ON p.student_ref = s.source_ref
     WHERE s.generation = %(generation)s
),
evaluated AS (
    SELECT scope.*,
           (
                scope.status_compare = 'enrolled'
            AND scope.contact_count = 0
            AND scope.payment_count = 0
           ) AS fires
      FROM scope
)
SELECT e.source_ref                                    AS record_ref,
       'student'                                       AS entity_type,
       CASE WHEN e.fires THEN 'conflict' ELSE 'ok' END AS verdict,
       CASE WHEN e.fires THEN jsonb_build_object(
                'conflicts', jsonb_build_array(jsonb_build_object(
                    'conflict_type',   'C5',
                    'identity_refs',   e.identity_refs,
                    'observed_values', jsonb_build_object(
                        'status_funnel',            e.status_compare,
                        'linked_contact_count',     e.contact_count,
                        'attributed_payment_count', e.payment_count
                    )
                ))
            ) END                                      AS detail
  FROM evaluated AS e
