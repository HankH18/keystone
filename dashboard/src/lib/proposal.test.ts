/**
 * What `proposals.action` may actually CONTAIN, and what the dashboard reads.
 *
 * ===========================================================================
 * A10 was structurally impossible as written, and this file is the proof.
 * ===========================================================================
 * `service/migrations/versions/0007_action_content_binding.py` creates
 * `ck_proposals_action_vocabulary` -- and creates it VALIDATED, so it binds
 * rows that already exist:
 *
 *     CHECK (CASE WHEN jsonb_typeof(action) = 'object'
 *                 THEN jsonb_exists(action, 'set')
 *                      AND jsonb_typeof(action -> 'set') = 'object'
 *                      AND action - 'set' = '{}'::jsonb
 *                 ELSE false END)
 *
 * `action - 'set' = '{}'::jsonb` means: remove the `set` key and NOTHING is
 * left. Exactly one top-level key, named `set`. A `target_path` sibling is
 * refused by the database, so the old `targetPath()` -- which read
 * `action.target_path` -- returned `null` for every proposal the service could
 * ever have written, forever, and silently.
 *
 * The write set is a set of PATHS and it is not the same as the key set:
 * `entities.current` holds one nested object, `survived`, so
 * `{"set": {"survived": {...}}}` presents ONE key while landing on the members
 * it carries. `writePaths()` mirrors `recon.apply.effective_write_paths` with
 * no entity row in hand -- the CONSERVATIVE reading, which can only widen the
 * set, never narrow it -- and `WritePath.display`'s `container->leaf` rendering.
 */
import { describe, expect, it } from 'vitest'
import { actionShape, fieldClassification, writePaths, writesAField } from './proposal'

/** The shape migration 0007 permits, and the only one the service can write. */
const SET_ONE = { set: { 'crm.contact.grade': '5' } }

/** What the dashboard used to read. The database refuses this row. */
const FORBIDDEN_TARGET_PATH = { target_path: 'crm.contact.grade' }

describe('writePaths — the committed {"set": {...}} action vocabulary', () => {
  it('reads a single write off action.set, not off a target_path sibling', () => {
    expect(writePaths(SET_ONE)).toEqual([
      { leaf: 'crm.contact.grade', container: null, display: 'crm.contact.grade' },
    ])
    expect(writesAField(SET_ONE)).toBe(true)
    expect(actionShape(SET_ONE)).toBe('writes')
  })

  it('treats {"set": {}} as genuinely evidence-only', () => {
    expect(writePaths({ set: {} })).toEqual([])
    expect(writesAField({ set: {} })).toBe(false)
    expect(actionShape({ set: {} })).toBe('evidence-only')
  })

  it('reports EVERY path a multi-assignment action writes, in a stable order', () => {
    const action = {
      set: {
        'payments.payment.external_ref': 'pi_9',
        'appdb.enrollment.crm_deal_id': 'D-1',
      },
    }
    expect(writePaths(action).map((path) => path.display)).toEqual([
      'appdb.enrollment.crm_deal_id',
      'payments.payment.external_ref',
    ])
    // Same members, opposite insertion order: byte-identical answer.
    const reordered = {
      set: {
        'appdb.enrollment.crm_deal_id': 'D-1',
        'payments.payment.external_ref': 'pi_9',
      },
    }
    expect(writePaths(reordered)).toEqual(writePaths(action))
  })

  it('descends the one nested container and names the leaves, never just the key', () => {
    // recon.reconciler._fix emits this form for NESTED_FIX_TARGETS. Reading the
    // top-level key alone would report `survived`, which is on neither §6 list.
    const action = {
      set: {
        survived: {
          'crm.contact.lifecycle_stage': 'customer',
          'crm.contact.email': 'a@b.example',
        },
      },
    }
    expect(writePaths(action)).toEqual([
      {
        leaf: 'crm.contact.email',
        container: 'survived',
        display: 'survived->crm.contact.email',
      },
      {
        leaf: 'crm.contact.lifecycle_stage',
        container: 'survived',
        display: 'survived->crm.contact.lifecycle_stage',
      },
    ])
    // The LEAF is what §6 classifies, not the container.
    expect(fieldClassification(writePaths(action)[1].leaf)).toBe(
      'auto-apply eligible',
    )
  })

  it('reports an assigned-but-empty container as the container itself', () => {
    expect(writePaths({ set: { survived: {} } })).toEqual([
      { leaf: 'survived', container: null, display: 'survived' },
    ])
  })

  it('reads a non-object assignment as a write of the key it names', () => {
    expect(writePaths({ set: { survived: 'wiped' } })).toEqual([
      { leaf: 'survived', container: null, display: 'survived' },
    ])
    expect(writePaths({ set: { 'crm.contact.grade': null } })).toEqual([
      { leaf: 'crm.contact.grade', container: null, display: 'crm.contact.grade' },
    ])
  })

  it('refuses to read the DB-forbidden target_path shape as a field write', () => {
    expect(writePaths(FORBIDDEN_TARGET_PATH)).toEqual([])
    expect(writesAField(FORBIDDEN_TARGET_PATH)).toBe(false)
    // And it is NOT quietly called evidence-only: an action with no `set` is
    // unreadable, and the UI says so rather than inventing a verdict.
    expect(actionShape(FORBIDDEN_TARGET_PATH)).toBe('unreadable')
  })

  it('is unreadable, never a crash, for anything that is not the committed shape', () => {
    for (const value of [null, undefined, 'set', 42, [], { set: null }, { set: [] }]) {
      expect(writePaths(value)).toEqual([])
      expect(actionShape(value)).toBe('unreadable')
      expect(writesAField(value)).toBe(false)
    }
  })
})
