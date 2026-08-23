-- @rule_id: R-013
-- @rule_version: v1
-- @conflict: C13
-- @scope: stg_payment
--
-- SS5.5 C13 -- refund not reflected.
--
-- "payment `status='refunded'` where (a) it is the person's **most recent** payment
--  of that `type` on the `E1`/`E2`-attributed enrollment, (b) **no** later `paid`
--  payment of the same `type` exists for that person, (c) `refunded_at` post-dates
--  the enrollment row's `updated_at`, and (d) the enrollment `stage_funnel IN
--  PAID_IMPLYING_STAGES` **and** `STATUS_TO_FUNNEL(student.status) == enrolled`."
-- entity_refs: identity refs + payment ref + `appdb:enrollment:<id>`.
--
-- Clause (c) is the **single** read of `updated_at` any rule is permitted (SS1, `G26`):
-- the ~0.5 percent out-of-order timestamp dirt is never applied to an enrollment whose
-- student holds a `refunded` payment, so this clause is dirt-free by construction.
-- No rule anywhere treats an out-of-order timestamp as evidence *of* a conflict.
--
-- SS4.4: an unattributed enrollment yields `unchecked` with
-- `detail.reason='enrollment_unattributed'` -- never a conflict.
--
-- ABSENCE RULE (SS5.3): clauses (a) and (b) are absences over the person's payments.
--
-- Clause (b) reads "no later **paid** payment of the same `type`", so the
-- `superseded` EXISTS carries `sibling.status = 'paid'`. Without it a later
-- *refunded* payment of the same type would also suppress C13, which is not what the
-- clause says. On the committed dataset the two forms agree (no refunded payment is
-- superseded by a later refund of the same type), so this states the contract rather
-- than changing the answer -- which is exactly why it was worth stating.
--
-- FP guard (`G14`, `G15`): every non-planted refunded payment is either superseded by
-- a later `paid` payment of the same type (>=1200s later) or has its enrollment moved
-- to `refunded`/`withdrawn` AND its student status out of `{enrolled, active}`.
-- Partially-reflected refunds are never planted, since the AND predicate cannot see
-- them.

WITH refunded AS (
    SELECT pay.source_ref,
           pay.type          AS payment_type,
           pay.occurred_at,
           pay.refunded_at,
           link.person_key,
           person.identity_refs,
           person.student_ref,
           attribution.enrollment_ref
      FROM stg_payment AS pay
      LEFT JOIN er_payment_person AS link
             ON link.payment_ref = pay.source_ref
      LEFT JOIN er_person AS person
             ON person.person_key = link.person_key
      LEFT JOIN er_payment_enrollment AS attribution
             ON attribution.payment_ref = pay.source_ref
     WHERE pay.generation = %(generation)s
       AND pay.status = 'refunded'
),
context AS (
    SELECT refunded.*,
           enrollment.stage_funnel,
           to_char(enrollment.updated_at AT TIME ZONE 'UTC',
                   'YYYY-MM-DD"T"HH24:MI:SS"Z"')                    AS enrollment_updated_at,
           to_char(refunded.refunded_at AT TIME ZONE 'UTC',
                   'YYYY-MM-DD"T"HH24:MI:SS"Z"')                    AS refunded_at_text,
           enrollment.updated_at                                     AS enrollment_updated_ts,
           student.status                                            AS student_status,
           student.status_compare                                    AS student_status_funnel,
           EXISTS (
               SELECT 1
                 FROM er_payment_person AS sibling_link
                 JOIN stg_payment AS sibling
                        ON sibling.source_ref = sibling_link.payment_ref
                       AND sibling.generation = %(generation)s
                WHERE sibling_link.person_key = refunded.person_key
                  AND sibling.type = refunded.payment_type
                  AND sibling.status = 'paid'
                  AND sibling.occurred_at > refunded.occurred_at
           )                                                         AS superseded
      FROM refunded
      LEFT JOIN stg_enrollment AS enrollment
             ON enrollment.source_ref = refunded.enrollment_ref
            AND enrollment.generation = %(generation)s
      LEFT JOIN stg_student AS student
             ON student.source_ref = refunded.student_ref
            AND student.generation = %(generation)s
),
decided AS (
    SELECT context.*,
           (context.person_key IS NULL OR context.enrollment_ref IS NULL) AS unattributed,
           (
                context.person_key IS NOT NULL
            AND context.enrollment_ref IS NOT NULL
            AND context.identity_refs IS NOT NULL
            AND NOT context.superseded
            AND context.refunded_at IS NOT NULL
            AND context.enrollment_updated_ts IS NOT NULL
            AND context.refunded_at > context.enrollment_updated_ts
            AND context.stage_funnel IN (SELECT stage_funnel FROM ref_paid_implying_stage)
            AND context.student_status_funnel = 'enrolled'
           ) AS fires
      FROM context
)
SELECT pay.source_ref                                     AS record_ref,
       'payment'                                          AS entity_type,
       CASE WHEN d.fires        THEN 'conflict'
            WHEN d.unattributed THEN 'unchecked'
            ELSE 'ok' END                                  AS verdict,
       CASE WHEN d.fires THEN jsonb_build_object(
                'conflicts', jsonb_build_array(jsonb_build_object(
                    'conflict_type',   'C13',
                    'identity_refs',   d.identity_refs,
                    'payment_refs',    jsonb_build_array(d.source_ref),
                    'enrollment_refs', jsonb_build_array(d.enrollment_ref),
                    'observed_values', jsonb_build_object(
                        'refunded_at',              d.refunded_at_text,
                        'enrollment.updated_at',    d.enrollment_updated_at,
                        'enrollment.stage_funnel',  d.stage_funnel,
                        'student.status',           d.student_status
                    )
                ))
            )
            WHEN d.unattributed THEN jsonb_build_object('reason', 'enrollment_unattributed')
            ELSE NULL END                                  AS detail
  FROM stg_payment AS pay
  LEFT JOIN decided AS d ON d.source_ref = pay.source_ref
 WHERE pay.generation = %(generation)s
