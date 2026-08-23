-- @rule_id: R-007
-- @rule_version: v1
-- @conflict: C7
-- @scope: stg_enrollment
--
-- SS5.5 C7 -- enrolled-but-unpaid.
--
-- "enrollment `stage_funnel IN PAID_IMPLYING_STAGES` **and** no `paid` payment of
--  type `deposit|tuition` attributed to *that enrollment* by `E1`/`E2`.
--  `deposit_paid_at` is **never** a trigger -- it is a retained historical fact on
--  `refunded`/`withdrawn` enrollments and appears in `observed_values` only."
-- entity_refs: identity refs + `appdb:enrollment:<id>`.
--
-- The v1 catalogue's `(or deposit_paid_at non-null)` clause is DELETED (SS13). It is
-- read here, and only into `observed_values`.
--
-- C7 is **enrollment**-scoped, not payment-scoped (SS4.4): an unattributed payment
-- simply does not count toward the paid test and never makes C7 `unchecked`.
--
-- ABSENCE RULE (SS5.3): "no paid deposit/tuition" is absence, and an incomplete
-- payments load would fire it on every enrolled child in the dataset.
--
-- FP guard (`G38`, `G14`, `G35`): a paid-implying stage is drawn only for children of
-- payments-present households, so no `{appdb, crm}` or `{appdb}`-only enrollment can
-- reach this predicate. The raw population is 875; `PRECEDENCE` 4/5/8 take it to 300
-- in the runner.

WITH enrollments AS (
    SELECT e.source_ref,
           e.stage_funnel,
           to_char(e.deposit_paid_at AT TIME ZONE 'UTC',
                   'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS deposit_paid_at,
           person.identity_refs
      FROM stg_enrollment AS e
      LEFT JOIN er_person_ref AS enrollment_ref
             ON enrollment_ref.ref = e.source_ref
      LEFT JOIN er_person AS person
             ON person.person_key = enrollment_ref.person_key
     WHERE e.generation = %(generation)s
),
paid AS (
    SELECT link.enrollment_ref,
           count(*) AS paid_count
      FROM er_payment_enrollment AS link
      JOIN stg_payment AS pay
             ON pay.source_ref = link.payment_ref
            AND pay.generation = %(generation)s
     WHERE pay.status = 'paid'
       AND pay.type IN ('deposit', 'tuition')
     GROUP BY link.enrollment_ref
),
evaluated AS (
    SELECT enrollments.*,
           COALESCE(paid.paid_count, 0) AS paid_count,
           (
                enrollments.stage_funnel IN (SELECT stage_funnel FROM ref_paid_implying_stage)
            AND paid.enrollment_ref IS NULL
            AND enrollments.identity_refs IS NOT NULL
           ) AS fires
      FROM enrollments
      LEFT JOIN paid ON paid.enrollment_ref = enrollments.source_ref
)
SELECT e.source_ref                                    AS record_ref,
       'enrollment'                                    AS entity_type,
       CASE WHEN e.fires THEN 'conflict' ELSE 'ok' END AS verdict,
       CASE WHEN e.fires THEN jsonb_build_object(
                'conflicts', jsonb_build_array(jsonb_build_object(
                    'conflict_type',   'C7',
                    'identity_refs',   e.identity_refs,
                    'enrollment_refs', jsonb_build_array(e.source_ref),
                    'observed_values', jsonb_build_object(
                        'enrollment.stage_funnel',     e.stage_funnel,
                        'enrollment.deposit_paid_at',  e.deposit_paid_at,
                        'paid_deposit_payment_count',  e.paid_count
                    )
                ))
            ) END                                      AS detail
  FROM evaluated AS e
