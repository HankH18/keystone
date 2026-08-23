-- @rule_id: R-014
-- @rule_version: v1
-- @conflict: C14
-- @scope: stg_student
--
-- SS5.5 C14 -- sensitive-field-only fix.
--
-- "linked person with >=1 disagreeing `COMPARED_FIELDS` comparison whose
--  disagreeing-path set is **non-empty and wholly a subset of `SENSITIVE_FIELDS`**.
--  The empty set never fires C14." -- entity_refs: identity refs.
--
-- `bool_and(wholly_sensitive) FILTER (WHERE disagrees)` is NULL when nothing
-- disagrees, and `COALESCE(..., false)` turns that into the contract's "the empty
-- set never fires C14" rather than SQL's three-valued shrug. The `disagreeing_count
-- > 0` clause states the same thing a second time on purpose: this is the one
-- predicate in SS5.5 whose degenerate case is called out by name.
--
-- SS2.4's partition table is what makes the subset test well-typed: `name_first`,
-- `name_last`, `dob` and `stage` are wholly sensitive rows; `grade` and `lifecycle`
-- are not. The sensitivity of each path is read from `ref_compared_field`, which is
-- materialized from `recon.reference` -- `SENSITIVE_FIELDS` is never restated here.
--
-- FP guard (`G17`, `G37`): the empty set is excluded by the predicate itself, a NULL
-- operand is `unchecked`, and name/DOB plants are forced onto `L1` so the pair is
-- resolvable despite the disagreement. The 50 C14 entries mechanically induced on
-- the C10 persons are removed by `PRECEDENCE` rule 2 in the runner (`G21`).

-- SS2.4's `COMPARED_FIELDS` is the ONLY producer of `disagreeing_fields`, and its
-- six rows are read out of `ref_compared_field` -- materialized from
-- `recon.reference.COMPARED_FIELDS` -- so the path strings, the sensitivity
-- partition and the SS5.1 `unmapped_reason` all come from the committed table
-- rather than from a literal in this file.
--
-- SS5.2: C6/C14 compare **survived values across sources only**, one conflict per
-- person per generation. Survivorship is SS4.6's lowest-source-ref rule,
-- materialized once onto `er_person` (`survived_contact_ref` / `survived_deal_ref` /
-- `survived_enrollment_ref`) instead of being restated here and in R-006.
--
-- SS5.1: a comparison is evaluated **only when both sides normalize to a non-NULL
-- value**; a NULL operand is `unchecked`, never a disagreement. That is why the
-- disagreement test is spelled `a IS NOT NULL AND b IS NOT NULL AND a <> b` and why
-- the lint refuses `IS DISTINCT FROM`. The `unchecked` reason is a function of the
-- ROW and of whether the SOURCE value was NULL -- never of a guess about the value's
-- contents -- so each branch carries its raw column alongside its normalized one.

WITH scope AS (
    SELECT s.source_ref,
           s.generation,
           p.identity_refs,
           COALESCE(p.contact_count, 0) AS contact_count,
           p.survived_contact_ref,
           p.survived_deal_ref,
           p.survived_enrollment_ref,
           s.first_name     AS student_first_raw,
           s.first_norm     AS student_first,
           s.last_name      AS student_last_raw,
           s.last_norm      AS student_last,
           s.dob            AS student_dob_raw,
           s.dob_norm       AS student_dob,
           s.grade          AS student_grade_raw,
           s.grade_norm     AS student_grade,
           s.status         AS student_status_raw,
           s.status_compare AS student_status
      FROM stg_student AS s
      LEFT JOIN er_person AS p
             ON p.student_ref = s.source_ref
     WHERE s.generation = %(generation)s
),
sides AS (
    SELECT scope.*,
           contact.first_name      AS contact_first_raw,
           contact.first_norm      AS contact_first,
           contact.last_name       AS contact_last_raw,
           contact.last_norm       AS contact_last,
           contact.dob             AS contact_dob_raw,
           contact.dob_norm        AS contact_dob,
           contact.grade           AS contact_grade_raw,
           contact.grade_norm      AS contact_grade,
           contact.lifecycle_stage AS contact_lifecycle_raw,
           lifecycle.funnel        AS contact_lifecycle,
           deal.stage              AS deal_stage_raw,
           deal.stage_funnel       AS deal_stage,
           enrollment.stage        AS enrollment_stage_raw,
           enrollment.stage_funnel AS enrollment_stage
      FROM scope
      LEFT JOIN stg_crm_contact AS contact
             ON contact.source_ref = scope.survived_contact_ref
            AND contact.generation = scope.generation
      LEFT JOIN ref_lifecycle_funnel AS lifecycle
             ON lifecycle.lifecycle_norm = contact.lifecycle_norm
      LEFT JOIN stg_crm_deal AS deal
             ON deal.source_ref = scope.survived_deal_ref
            AND deal.generation = scope.generation
      LEFT JOIN stg_enrollment AS enrollment
             ON enrollment.source_ref = scope.survived_enrollment_ref
            AND enrollment.generation = scope.generation
),
comparisons AS (
    SELECT source_ref, 'name_first' AS logical,
           contact_first AS left_canon, student_first AS right_canon,
           (contact_first_raw IS NULL) AS left_raw_null,
           (student_first_raw IS NULL) AS right_raw_null,
           to_jsonb(contact_first) AS left_value, to_jsonb(student_first) AS right_value
      FROM sides
     UNION ALL
    SELECT source_ref, 'name_last',
           contact_last, student_last,
           (contact_last_raw IS NULL), (student_last_raw IS NULL),
           to_jsonb(contact_last), to_jsonb(student_last)
      FROM sides
     UNION ALL
    SELECT source_ref, 'dob',
           to_char(contact_dob, 'YYYY-MM-DD'), to_char(student_dob, 'YYYY-MM-DD'),
           (contact_dob_raw IS NULL), (student_dob_raw IS NULL),
           to_jsonb(to_char(contact_dob, 'YYYY-MM-DD')),
           to_jsonb(to_char(student_dob, 'YYYY-MM-DD'))
      FROM sides
     UNION ALL
    SELECT source_ref, 'grade',
           contact_grade, student_grade,
           (contact_grade_raw IS NULL), (student_grade_raw IS NULL),
           to_jsonb(contact_grade), to_jsonb(student_grade)
      FROM sides
     UNION ALL
    SELECT source_ref, 'stage',
           deal_stage, enrollment_stage,
           (deal_stage_raw IS NULL), (enrollment_stage_raw IS NULL),
           to_jsonb(deal_stage), to_jsonb(enrollment_stage)
      FROM sides
     UNION ALL
    SELECT source_ref, 'lifecycle',
           contact_lifecycle, student_status,
           (contact_lifecycle_raw IS NULL), (student_status_raw IS NULL),
           to_jsonb(contact_lifecycle), to_jsonb(student_status)
      FROM sides
),
evaluated AS (
    SELECT c.source_ref,
           cf.left_path,
           cf.right_path,
           cf.wholly_sensitive,
           c.left_value,
           c.right_value,
           (c.left_canon IS NOT NULL AND c.right_canon IS NOT NULL
            AND c.left_canon <> c.right_canon)                       AS disagrees,
           (c.left_canon IS NOT NULL AND c.right_canon IS NOT NULL)  AS ran,
           CASE
               WHEN (c.left_canon  IS NULL AND c.left_raw_null)
                 OR (c.right_canon IS NULL AND c.right_raw_null) THEN 'missing_operand'
               ELSE cf.unmapped_reason
           END                                                       AS reason
      FROM comparisons AS c
      JOIN ref_compared_field AS cf ON cf.logical = c.logical
),
rolled AS (
    SELECT source_ref,
           count(*) FILTER (WHERE disagrees)                  AS disagreeing_count,
           count(*) FILTER (WHERE ran)                        AS evaluated_count,
           COALESCE(bool_and(wholly_sensitive)
                    FILTER (WHERE disagrees), false)          AS wholly_sensitive,
           -- SS5.1: `missing_operand` > `unparseable_value` > `unmapped_enum`,
           -- spelled as the pinned order rather than derived from an ordinal.
           CASE
               WHEN bool_or(reason = 'missing_operand')   FILTER (WHERE NOT ran)
                    THEN 'missing_operand'
               WHEN bool_or(reason = 'unparseable_value') FILTER (WHERE NOT ran)
                    THEN 'unparseable_value'
               ELSE 'unmapped_enum'
           END                                                AS reason
      FROM evaluated
     GROUP BY source_ref
),
observed AS (
    -- `COLLATE "C"` on the DISTINCT argument, not decoration: Postgres orders a
    -- DISTINCT aggregate's input by the column's DEFAULT collation, so on an ICU or
    -- en_US cluster this one array would come back in a different order from every
    -- other aggregate in the rule set. `ORDER BY ... COLLATE "C"` is rejected here
    -- ("in an aggregate with DISTINCT, ORDER BY expressions must appear in argument
    -- list"), so the collation goes on the argument itself. The runner re-sorts
    -- `disagreeing_fields` in Python, so this is defence in depth -- but the
    -- discipline stated in context.py's `_SURVIVORSHIP` is that byte order is
    -- literal everywhere, and this was the one place it was not applied.
    SELECT source_ref,
           jsonb_object_agg(path, value)          AS observed_values,
           jsonb_agg(DISTINCT path COLLATE "C")   AS paths
      FROM (
            SELECT source_ref, left_path AS path, left_value AS value
              FROM evaluated WHERE disagrees
             UNION ALL
            SELECT source_ref, right_path, right_value
              FROM evaluated WHERE disagrees
           ) AS flattened
     GROUP BY source_ref
),
decided AS (
    SELECT sides.source_ref,
           sides.identity_refs,
           rolled.evaluated_count,
           rolled.reason,
           observed.observed_values,
           observed.paths,
           (
                sides.contact_count > 0
            AND rolled.disagreeing_count > 0
            AND rolled.wholly_sensitive
           ) AS fires
      FROM sides
      JOIN rolled   ON rolled.source_ref = sides.source_ref
      LEFT JOIN observed ON observed.source_ref = sides.source_ref
)
SELECT d.source_ref                                       AS record_ref,
       'student'                                          AS entity_type,
       CASE WHEN d.fires                THEN 'conflict'
            WHEN d.evaluated_count > 0  THEN 'ok'
            ELSE 'unchecked' END                          AS verdict,
       CASE WHEN d.fires THEN jsonb_build_object(
                'conflicts', jsonb_build_array(jsonb_build_object(
                    'conflict_type',      'C14',
                    'identity_refs',      d.identity_refs,
                    'disagreeing_fields', d.paths,
                    'observed_values',    d.observed_values
                ))
            )
            WHEN d.evaluated_count > 0 THEN NULL
            ELSE jsonb_build_object('reason', d.reason) END AS detail
  FROM decided AS d
