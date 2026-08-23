-- @rule_id: R-000
-- @rule_version: v1
-- @conflict: -
-- @scope: stg_crm_deal
--
-- SS5.8 -- records with no applicable invariant (R8).
--
-- "Every `stg_*` row is stamped in `invariant_results` for every rule whose scope
--  includes it. A row in scope of **zero** rules gets one synthetic row
--  (rule_id='R-000', verdict='unchecked', detail.reason='no_rule_in_scope')."
--
-- `stg_crm_deal` is in the scope of no rule (SS5.5's rule-scope table), so every
-- deal row carries this stamp. It is not a claim that deals are consistent; it is
-- the explicit statement that nothing checked them, which is the difference
-- between "checked and clean" and "never looked at".

SELECT d.source_ref                                        AS record_ref,
       'deal'                                              AS entity_type,
       'unchecked'                                         AS verdict,
       jsonb_build_object('reason', 'no_rule_in_scope')    AS detail
  FROM stg_crm_deal AS d
 WHERE d.generation = %(generation)s
