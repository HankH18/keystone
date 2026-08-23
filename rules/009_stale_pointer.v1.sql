-- @rule_id: R-009
-- @rule_version: v1
-- @conflict: C9
-- @scope: stg_enrollment
--
-- SS5.5 C9 -- stale pointer.
--
-- "`enrollment.crm_deal_id` names a deal **absent from the generation-3 CRM
--  snapshot**, **or** names a deal whose `D2`-resolved person set is **non-empty and
--  does not contain** the enrollment's person. An empty person set yields
--  `verdict='unchecked'`, `detail.reason='deal_unresolved'`."
-- entity_refs: the **enrollment's** person's identity refs + `appdb:enrollment:<id>`.
-- The mispointed deal and its person appear in `observed_values`, never in refs.
--
-- SS7: absence of a `natural_key` from a source's gen-3 snapshot IS a deletion, and
-- that is how C9's non-existent deal is represented. `D2` is the only deal-to-person
-- link rule (SS4.5) -- `enrollment.crm_deal_id` is the pointer under test and may
-- never be used as a link rule, which is why the person set comes from
-- `er_deal_person` and not from this column.
--
-- `C9.deal_person_refs` is one `anchor_ref` per resolved person, sorted -- never each
-- person's identity-ref set and never a `person_key` (SS5.4 ruling 16).
--
-- ABSENCE RULE (SS5.3): branch one is literally "absent from the gen-3 snapshot".
--
-- FP guard (`G9`, `G20`): a NULL `crm_deal_id` (~40 percent) is not a conflict, and a
-- household deal listing every sibling contact resolves to a person set *containing*
-- the enrollment's person.

WITH enrollments AS (
    SELECT e.source_ref,
           e.crm_deal_id,
           person.person_key,
           person.identity_refs
      FROM stg_enrollment AS e
      LEFT JOIN er_person_ref AS holder
             ON holder.ref = e.source_ref
      LEFT JOIN er_person AS person
             ON person.person_key = holder.person_key
     WHERE e.generation = %(generation)s
),
deal_persons AS (
    SELECT link.deal_id,
           jsonb_agg(person.anchor_ref ORDER BY person.anchor_ref COLLATE "C") AS anchor_refs,
           count(*) AS person_count
      FROM er_deal_person AS link
      JOIN er_person AS person ON person.person_key = link.person_key
     GROUP BY link.deal_id
),
evaluated AS (
    SELECT enrollments.*,
           (deal.deal_id IS NOT NULL)                       AS deal_present,
           COALESCE(deal_persons.anchor_refs, '[]'::jsonb)   AS deal_person_refs,
           COALESCE(deal_persons.person_count, 0)            AS person_count,
           EXISTS (
               SELECT 1 FROM er_deal_person AS d2
                WHERE d2.deal_id = enrollments.crm_deal_id
                  AND d2.person_key = enrollments.person_key
           )                                                 AS names_this_person
      FROM enrollments
      LEFT JOIN stg_crm_deal AS deal
             ON deal.deal_id = enrollments.crm_deal_id
            AND deal.generation = %(generation)s
      LEFT JOIN deal_persons ON deal_persons.deal_id = enrollments.crm_deal_id
),
decided AS (
    SELECT evaluated.*,
           (
                evaluated.crm_deal_id IS NOT NULL
            AND evaluated.identity_refs IS NOT NULL
            AND evaluated.deal_present
            AND evaluated.person_count = 0
           ) AS unresolved,
           (
                evaluated.crm_deal_id IS NOT NULL
            AND evaluated.identity_refs IS NOT NULL
            AND (
                    NOT evaluated.deal_present
                 OR (evaluated.person_count > 0 AND NOT evaluated.names_this_person)
                )
           ) AS fires
      FROM evaluated
)
SELECT d.source_ref                                       AS record_ref,
       'enrollment'                                       AS entity_type,
       CASE WHEN d.fires      THEN 'conflict'
            WHEN d.unresolved THEN 'unchecked'
            ELSE 'ok' END                                  AS verdict,
       CASE WHEN d.fires THEN jsonb_build_object(
                'conflicts', jsonb_build_array(jsonb_build_object(
                    'conflict_type',   'C9',
                    'identity_refs',   d.identity_refs,
                    'enrollment_refs', jsonb_build_array(d.source_ref),
                    'observed_values', jsonb_build_object(
                        'enrollment.crm_deal_id', d.crm_deal_id,
                        'deal_present_gen3',      d.deal_present,
                        'deal_person_refs',       d.deal_person_refs
                    )
                ))
            )
            WHEN d.unresolved THEN jsonb_build_object('reason', 'deal_unresolved')
            ELSE NULL END                                  AS detail
  FROM decided AS d
