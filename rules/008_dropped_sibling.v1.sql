-- @rule_id: R-008
-- @rule_version: v1
-- @conflict: C8
-- @scope: stg_student
--
-- SS5.5 C8 -- dropped sibling.
--
-- "household (SS4.8, |household_members_appdb(k)| >= 2) where **exactly one**
--  eligible child is absent from **exactly one** of the downstream sources
--  {crm, payments} in which *all* other eligible children are present."
-- entity_refs: the dropped child's identity refs.
--
-- **Presence is defined, not assumed** (SS5.5 C8): present in `crm` iff the child
-- has >=1 `entity_links` row with `link_class = 'contact_student'`; present in
-- `payments` iff >=1 payment is attributed to it by `P1..P3`. Both are read off the
-- materialized cascade output, and `G2` binds the generator's mask to the same two
-- functions. `appdb` presence is definitional and the app DB is never the dropped
-- source.
--
-- Households come from `er_household`, materialized from
-- `recon.reference.household_members_appdb` -- grouping by exact `household_key`,
-- never a transitive closure and never union-find (SS4.8).
-- `appdb.student.household_id` is a corroborating signal only and is deliberately
-- not read: SS1.3 says C8 detection must not depend on it.
--
-- Eligibility (the FP guard, `G22`): a child is excluded from the mask when
-- `grade_ord < GRADE_ORDER[ENROLLMENT_GRADE_FLOOR]`, or `status = 'withdrawn'`, or
-- its enrollment sits at `withdrawn`/`refunded`. `grade_ord` is materialized
-- upstream and the floor is read from `ref_constant`, so no rule computes an
-- ordinal. The `IS NOT NULL` on `grade_ord` is explicit because `GRADE_ORDER['K']`
-- is 0 -- a truthiness test would silently exclude every kindergartener.
--
-- **The `eligible_count >= 2` gate is load-bearing and is NOT restated in SS5.5.**
-- SS5.5's C8 row gates on `|household_members_appdb(k)| >= 2` and then asks for
-- "exactly one eligible child absent from exactly one of the downstream sources in
-- which *all other* eligible children are present". With exactly ONE eligible child,
-- "all OTHER eligible children are present" is VACUOUSLY TRUE, so the literal text
-- admits every multi-child household that happens to have one eligible member --
-- measured on the committed gen-3 fixtures that is 45 further households, and two of
-- their children sit in `golden/clean-sample.json`, so the SS8 probe would trip too.
-- The `>= 2` reading is what the generator applies; this rule states it so the
-- agreement is written down on both sides rather than being an unwritten one.
-- Raised against SS5.5's C8 row as a contract ambiguity (see the ticket's
-- `contract_gaps`); the SQL is deliberately the narrower, FP-safe reading.
--
-- ABSENCE RULE (SS5.3): the whole predicate is an absence test across two sources.

WITH member AS (
    SELECT h.household_key,
           h.student_ref,
           s.grade_ord,
           s.status_norm,
           person.identity_refs,
           COALESCE(person.contact_count, 0) AS contact_count,
           COALESCE(person.payment_count, 0) AS payment_count,
           person.survived_enrollment_ref
      FROM er_household AS h
      JOIN stg_student AS s
             ON s.source_ref = h.student_ref
            AND s.generation = %(generation)s
      LEFT JOIN er_person AS person
             ON person.student_ref = h.student_ref
),
sized AS (
    SELECT household_key, count(*) AS member_count
      FROM member
     GROUP BY household_key
),
eligible AS (
    SELECT member.*
      FROM member
      JOIN sized ON sized.household_key = member.household_key
      LEFT JOIN stg_enrollment AS enrollment
             ON enrollment.source_ref = member.survived_enrollment_ref
            AND enrollment.generation = %(generation)s
     WHERE sized.member_count >= 2
       AND member.grade_ord IS NOT NULL
       AND member.grade_ord >= (SELECT value FROM ref_constant
                                 WHERE name = 'enrollment_grade_floor_ord')
       AND NOT COALESCE(member.status_norm = 'withdrawn', false)
       AND NOT COALESCE(enrollment.stage_funnel IN ('withdrawn', 'refunded'), false)
),
counted AS (
    SELECT household_key,
           count(*)                                        AS eligible_count,
           count(*) FILTER (WHERE contact_count = 0)        AS absent_from_crm,
           count(*) FILTER (WHERE payment_count = 0)        AS absent_from_payments
      FROM eligible
     GROUP BY household_key
),
household AS (
    -- "exactly one of the downstream sources": when BOTH sources are each missing
    -- exactly one eligible child the household has two candidate drops, which is
    -- not the C8 shape, so `dropped_source` stays NULL and nothing fires.
    SELECT household_key,
           eligible_count,
           CASE
               WHEN absent_from_crm = 1 AND absent_from_payments = 1 THEN NULL
               WHEN absent_from_crm = 1      THEN 'crm'
               WHEN absent_from_payments = 1 THEN 'payments'
           END AS dropped_source
      FROM counted
     WHERE eligible_count >= 2
),
dropped AS (
    SELECT eligible.student_ref,
           eligible.identity_refs,
           household.household_key,
           household.eligible_count,
           household.dropped_source
      FROM household
      JOIN eligible ON eligible.household_key = household.household_key
     WHERE household.dropped_source IS NOT NULL
       AND ((household.dropped_source = 'crm'      AND eligible.contact_count = 0)
         OR (household.dropped_source = 'payments' AND eligible.payment_count = 0))
)
SELECT s.source_ref                                              AS record_ref,
       'student'                                                 AS entity_type,
       CASE WHEN dropped.student_ref IS NULL THEN 'ok' ELSE 'conflict' END AS verdict,
       CASE WHEN dropped.student_ref IS NULL THEN NULL
            ELSE jsonb_build_object(
                'conflicts', jsonb_build_array(jsonb_build_object(
                    'conflict_type',   'C8',
                    'identity_refs',   dropped.identity_refs,
                    'observed_values', jsonb_build_object(
                        'household_key',         dropped.household_key,
                        'dropped_source',        dropped.dropped_source,
                        'eligible_member_count', dropped.eligible_count
                    )
                ))
            ) END                                                AS detail
  FROM stg_student AS s
  LEFT JOIN dropped ON dropped.student_ref = s.source_ref
 WHERE s.generation = %(generation)s
