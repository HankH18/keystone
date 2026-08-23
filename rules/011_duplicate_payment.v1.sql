-- @rule_id: R-011
-- @rule_version: v1
-- @conflict: C11
-- @scope: stg_payment
--
-- SS5.5 C11 -- duplicate payment.
--
-- "two payments with equal `(payer_email_norm, amount_cents, type)` whose
--  `occurred_at` differ by `< 600s` **and** which both resolve by `P1..P3` to the
--  **same** person. If either resolves to no person, C11 does not fire (C2 covers
--  it)." -- entity_refs: the two payment refs, sorted.
--
-- SS5.2 pins the window as `abs(occurred_at delta) < C11_WINDOW_SECONDS`, STRICTLY,
-- and the constant is read from `ref_constant` (materialized from
-- `recon.reference.C11_WINDOW_SECONDS`) rather than written as 600 in this file.
-- C11 uses `occurred_at` only -- never `created_at`/`updated_at`, whose ~0.5 percent
-- out-of-order dirt (A.3, `G26`) is never evidence of a conflict.
--
-- SS12 D-3: the `payment_id`-repeat branch of A.4 is DELETED. A repeated PK is a
-- structural 4xx at the adapter, and both rows would produce the identical ref
-- string, so the "two payment refs" set would collapse to one element.
--
-- The same-person clause is the FP guard (`G7`, `G8`): siblings in a multi-child
-- household share `payer_email_norm` and the flat `fee` = 10000, and a sibling pair
-- resolves to two DIFFERENT persons. `G7` deliberately budgets a non-empty
-- population of sibling pairs inside the 600s window, so dropping this clause
-- produces a loud false-positive population rather than one stray pair.
-- Planted pairs are <=300s apart; every legitimate same-person repeat is >=1200s.
--
-- SS5.2: one entry per unordered pair. Both members are stamped with the same
-- conflict object and the runner de-duplicates on the SS5.4 harness key.
--
-- **`COALESCE(payer_email_norm, '')` / `COALESCE(type, '')` is a deliberate NULL
-- convention beyond SS5.5's text, not the contract.** SS5.5 says "equal
-- `(payer_email_norm, amount_cents, type)`"; SQL `=` is UNKNOWN on a NULL, so without
-- the COALESCE two payments that both normalize to a NULL payer email could never
-- pair. With it they compare EQUAL. The population is empty on the committed dataset
-- (0 payments whose `payer_email` normalizes to NULL), and either reading is
-- FP-safe here because the `person_key` equality still has to hold -- but the two
-- readings are not the same rule, and this one is the one implemented. Note this is
-- a join key, not a SS5.1 `COMPARED_FIELDS` comparison, so SS5.1's pinned
-- `a IS NOT NULL AND b IS NOT NULL AND a <> b` form does not govern it.

WITH scope AS (
    SELECT pay.source_ref,
           COALESCE(pay.payer_email_norm, '') AS payer_email_norm,
           pay.amount_cents,
           COALESCE(pay.type, '')             AS payment_type,
           pay.occurred_at,
           link.person_key
      FROM stg_payment AS pay
      JOIN er_payment_person AS link
             ON link.payment_ref = pay.source_ref
     WHERE pay.generation = %(generation)s
),
pairs AS (
    SELECT a.source_ref AS left_ref,
           b.source_ref AS right_ref,
           a.payer_email_norm,
           a.amount_cents,
           a.payment_type,
           abs(EXTRACT(EPOCH FROM (a.occurred_at - b.occurred_at)))::bigint AS delta_seconds
      FROM scope AS a
      JOIN scope AS b
             ON b.payer_email_norm = a.payer_email_norm
            AND b.amount_cents     = a.amount_cents
            AND b.payment_type     = a.payment_type
            AND b.person_key       = a.person_key
            AND (a.source_ref COLLATE "C") < (b.source_ref COLLATE "C")
     WHERE abs(EXTRACT(EPOCH FROM (a.occurred_at - b.occurred_at)))
           < (SELECT value FROM ref_constant WHERE name = 'c11_window_seconds')
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
                   'conflict_type',   'C11',
                   'payment_refs',    jsonb_build_array(entries.left_ref, entries.right_ref),
                   'observed_values', jsonb_build_object(
                       'payer_email_norm',           entries.payer_email_norm,
                       'amount_cents',               entries.amount_cents,
                       'type',                       entries.payment_type,
                       'occurred_at_delta_seconds',  entries.delta_seconds
                   )
               )
               ORDER BY entries.left_ref COLLATE "C", entries.right_ref COLLATE "C"
           ) AS conflicts
      FROM entries
     GROUP BY entries.member_ref
)
SELECT pay.source_ref                                                  AS record_ref,
       'payment'                                                       AS entity_type,
       CASE WHEN grouped.conflicts IS NULL THEN 'ok' ELSE 'conflict' END AS verdict,
       CASE WHEN grouped.conflicts IS NULL THEN NULL
            ELSE jsonb_build_object('conflicts', grouped.conflicts) END  AS detail
  FROM stg_payment AS pay
  LEFT JOIN grouped ON grouped.member_ref = pay.source_ref
 WHERE pay.generation = %(generation)s
