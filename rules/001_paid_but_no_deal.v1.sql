-- @rule_id: R-001
-- @rule_version: v1
-- @conflict: C1
-- @scope: stg_student
--
-- SS5.5 C1 -- paid-but-no-deal.
--
-- "person has >=1 `paid` payment **and** >=1 enrollment, but 0 `D2`-linked CRM deals"
-- entity_refs: identity refs.
--
-- ABSENCE RULE (SS5.3): the predicate turns on the *absence* of a deal, so an
-- incomplete generation-3 CRM load would fire it on every paid+enrolled person in
-- the dataset. The runner skips it and stamps `source_incomplete` instead.
--
-- FP guard (SS5.5, `G9`/`G10`/`G11`/`G36`): deals are allocated *from* this
-- invariant, so the only deal-less paid+enrolled populations are the 500 planted C1
-- and the 75 C8 `crm`-drops -- the latter removed by `PRECEDENCE` 8 in the runner.
-- `D2` is the only deal-to-person link rule (SS4.5): `enrollment.crm_deal_id` is the
-- pointer under test by C9 and is never read here.

WITH scope AS (
    SELECT s.source_ref,
           p.person_key,
           p.identity_refs,
           p.enrollment_count,
           p.deal_count,
           p.survived_enrollment_ref
      FROM stg_student AS s
      LEFT JOIN er_person AS p
             ON p.student_ref = s.source_ref
     WHERE s.generation = %(generation)s
),
paid AS (
    SELECT link.person_key,
           jsonb_agg(link.payment_ref ORDER BY link.payment_ref COLLATE "C") AS payment_refs
      FROM er_payment_person AS link
      JOIN stg_payment AS pay
             ON pay.source_ref = link.payment_ref
            AND pay.generation = %(generation)s
     WHERE pay.status = 'paid'
     GROUP BY link.person_key
),
evaluated AS (
    SELECT scope.source_ref,
           scope.identity_refs,
           scope.survived_enrollment_ref,
           scope.deal_count,
           paid.payment_refs,
           (
                paid.payment_refs IS NOT NULL
            AND scope.enrollment_count > 0
            AND scope.deal_count = 0
           ) AS fires
      FROM scope
      LEFT JOIN paid ON paid.person_key = scope.person_key
)
SELECT e.source_ref                                    AS record_ref,
       'student'                                       AS entity_type,
       CASE WHEN e.fires THEN 'conflict' ELSE 'ok' END AS verdict,
       CASE WHEN e.fires THEN jsonb_build_object(
                'conflicts', jsonb_build_array(jsonb_build_object(
                    'conflict_type',   'C1',
                    'identity_refs',   e.identity_refs,
                    'observed_values', jsonb_build_object(
                        'paid_payment_refs', e.payment_refs,
                        'enrollment_ref',    e.survived_enrollment_ref,
                        'd2_deal_count',     e.deal_count
                    )
                ))
            ) END                                      AS detail
  FROM evaluated AS e
