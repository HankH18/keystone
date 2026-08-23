-- @rule_id: R-012
-- @rule_version: v1
-- @conflict: C12
-- @scope: stg_payment
--
-- SS5.5 C12 -- wrong-amount payment.
--
-- "`amount_cents` != the fee-schedule amount for `(program, type)`, where
--  `program = program_norm` of the `E1`/`E2`-attributed enrollment; if no enrollment
--  is attributed, `norm_enum('program', metadata.program)`; if that is null or
--  unmappable, `unchecked`." -- entity_refs: identity refs + payment ref.
--
-- Both program sources are materialized: `stg_enrollment.program_norm` and
-- `stg_payment.program_norm` (which ingest fills with
-- `norm_enum('program', metadata.program)`). The fee schedule itself is read from
-- `ref_fee_schedule`, materialized from `recon.reference.FEE_SCHEDULE` -- restating
-- twelve amounts in SQL is the drift SS0 forbids, and a one-cent divergence would
-- move the whole C12 population.
--
-- SS4.4: the `unchecked` reason for the program fallback failing is
-- `enrollment_unattributed`. SS7's general rule -- "a well-formed record carrying an
-- unrecognised enum value ... every rule scoping it yields `unchecked` with
-- `unmapped_enum`" -- covers a present-but-unmappable payment `type`, which is the
-- other way this rule can fail to have an expected amount.
--
-- FP guard (`G13`, `G34`): every non-planted payment's `amount_cents` is exactly the
-- fee-schedule value for its `(program, type)`, and `metadata.program` equals the
-- attributed enrollment's `program` on EVERY payment, C12 included.

WITH scope AS (
    SELECT pay.source_ref,
           pay.amount_cents,
           pay.type          AS payment_type,
           pay.type_norm,
           COALESCE(enrollment.program_norm, pay.program_norm) AS program_norm,
           link.person_key,
           -- SS4.1: for a payment the cascade attributes to NO person, that payment's
           -- own `payments:payment:<id>` IS an identity ref. Falling back to it keeps
           -- the fires predicate exactly SS5.5's C12 (which carries no
           -- resolvable-person clause) and hands SS5.7 rule 3 -- "C2 over C12/C11: an
           -- unattributable payment cannot have a wrong amount" -- the suppression the
           -- contract assigns it, instead of the rule silently doing rule 3's job by
           -- never firing. Gating on `identity_refs IS NOT NULL` here would make
           -- PRECEDENCE rule 3 dead code and under-count SS9.1(b)'s RAW C12 column on
           -- any reseed where a C2 plant carried a resolvable `metadata.program`.
           COALESCE(person.identity_refs, jsonb_build_array(pay.source_ref))
                                                               AS identity_refs
      FROM stg_payment AS pay
      LEFT JOIN er_payment_enrollment AS attribution
             ON attribution.payment_ref = pay.source_ref
      LEFT JOIN stg_enrollment AS enrollment
             ON enrollment.source_ref = attribution.enrollment_ref
            AND enrollment.generation = %(generation)s
      LEFT JOIN er_payment_person AS link
             ON link.payment_ref = pay.source_ref
      LEFT JOIN er_person AS person
             ON person.person_key = link.person_key
     WHERE pay.generation = %(generation)s
),
evaluated AS (
    SELECT scope.*,
           fee.amount_cents AS expected_amount_cents,
           (
                scope.program_norm IS NOT NULL
            AND fee.amount_cents IS NOT NULL
            AND scope.amount_cents IS NOT NULL
            AND scope.amount_cents <> fee.amount_cents
           ) AS fires,
           (scope.program_norm IS NULL)                                   AS program_unknown,
           (scope.type_norm IS NULL AND scope.payment_type IS NOT NULL)    AS type_unmapped
      FROM scope
      LEFT JOIN ref_fee_schedule AS fee
             ON fee.program_norm = scope.program_norm
            AND fee.payment_type = scope.payment_type
)
SELECT e.source_ref                                       AS record_ref,
       'payment'                                          AS entity_type,
       CASE WHEN e.fires           THEN 'conflict'
            WHEN e.program_unknown THEN 'unchecked'
            WHEN e.type_unmapped   THEN 'unchecked'
            ELSE 'ok' END                                  AS verdict,
       CASE WHEN e.fires THEN jsonb_build_object(
                'conflicts', jsonb_build_array(jsonb_build_object(
                    'conflict_type',   'C12',
                    'identity_refs',   e.identity_refs,
                    'payment_refs',    jsonb_build_array(e.source_ref),
                    'observed_values', jsonb_build_object(
                        'amount_cents',          e.amount_cents,
                        'expected_amount_cents', e.expected_amount_cents,
                        'program_norm',          e.program_norm,
                        'type',                  e.payment_type
                    )
                ))
            )
            WHEN e.program_unknown THEN jsonb_build_object('reason', 'enrollment_unattributed')
            WHEN e.type_unmapped   THEN jsonb_build_object('reason', 'unmapped_enum')
            ELSE NULL END                                  AS detail
  FROM evaluated AS e
