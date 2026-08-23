-- @rule_id: R-002
-- @rule_version: v1
-- @conflict: C2
-- @scope: stg_payment
--
-- SS5.5 C2 -- payment-with-no-person.
--
-- "payment links to no person by `P1..P3`" -- entity_refs: `payments:payment:<id>`.
--
-- SS4.3 is explicit that an unattributable payment is C2 and never a guess, so this
-- rule reads the cascade's own output (`er_payment_person`, materialized from
-- `recon.er.resolve`) and never re-derives an attribution of its own.
--
-- ABSENCE RULE (SS5.3): "links to no person" is absence, and an incomplete app-DB
-- load would make every payment in the file unattributable.
--
-- FP guard (`G6`): every non-planted payment satisfies one of `P1..P3` by
-- construction, and the joint `external_ref` + `metadata`-name gap is forbidden.

WITH scope AS (
    SELECT pay.source_ref,
           pay.payer_email_norm,
           pay.external_ref,
           (
                (pay.payment_metadata ->> 'student_first_name') IS NOT NULL
            AND (pay.payment_metadata ->> 'student_last_name')  IS NOT NULL
           ) AS name_pair_present,
           (link.payment_ref IS NULL) AS fires
      FROM stg_payment AS pay
      LEFT JOIN er_payment_person AS link
             ON link.payment_ref = pay.source_ref
     WHERE pay.generation = %(generation)s
)
SELECT scope.source_ref                                     AS record_ref,
       'payment'                                            AS entity_type,
       CASE WHEN scope.fires THEN 'conflict' ELSE 'ok' END  AS verdict,
       CASE WHEN scope.fires THEN jsonb_build_object(
                'conflicts', jsonb_build_array(jsonb_build_object(
                    'conflict_type',   'C2',
                    'payment_refs',    jsonb_build_array(scope.source_ref),
                    'observed_values', jsonb_build_object(
                        'payer_email_norm',          scope.payer_email_norm,
                        'external_ref',              scope.external_ref,
                        'metadata_name_pair_present', scope.name_pair_present
                    )
                ))
            ) END                                           AS detail
  FROM scope
