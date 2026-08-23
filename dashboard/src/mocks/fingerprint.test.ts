/**
 * `canon_value` and the fingerprint payload, checked against the contract's OWN
 * worked examples (docs/invariant-contract.md §2.5 and §5.4).
 *
 * These bind: change a separator, drop the sequence escaping, or reorder a
 * section and these go red — which is the point, because the fingerprint is the
 * idempotency key the whole proposal pipeline is built on.
 */
import { describe, expect, it } from 'vitest'
import { canonValue, fingerprintPayload, sha256Hex } from './fingerprint'

const US = String.fromCharCode(0x1f)
const RS = String.fromCharCode(0x1e)

describe('canonValue — §2.5', () => {
  it('serialises None as a literal backslash-N, distinct from the string', () => {
    expect(canonValue(null)).toBe('\\N')
    expect(canonValue(null)).not.toBe(canonValue('\\N'))
  })

  it('dispatches bool before int', () => {
    expect(canonValue(true)).toBe('true')
    expect(canonValue(false)).toBe('false')
  })

  it('serialises ints as decimals with no separators', () => {
    expect(canonValue(1200137)).toBe('1200137')
    expect(canonValue(0)).toBe('0')
  })

  it('FORBIDS floats rather than serialising them non-deterministically', () => {
    expect(() => canonValue(1.5)).toThrow(/float is FORBIDDEN/)
  })

  it('passes an email, a name and a ref through byte-identically', () => {
    expect(canonValue('appdb:student:s7')).toBe('appdb:student:s7')
    expect(canonValue('parent@corp.com')).toBe('parent@corp.com')
  })

  it('escapes backslash first, then US, then RS', () => {
    expect(canonValue('a\\b')).toBe('a\\\\b')
    expect(canonValue(`a${US}b`)).toBe('a\\x1fb')
    expect(canonValue(`a${RS}b`)).toBe('a\\x1eb')
  })

  it("matches the contract's worked sequence examples exactly", () => {
    expect(canonValue([])).toBe(RS)
    expect(canonValue(['a'])).toBe(`${RS}a${RS}`)
    expect(canonValue(['b', 'a'])).toBe(`${RS}a${RS}b${RS}`)
  })

  it('is injective between a sequence and a scalar', () => {
    expect(canonValue([])).not.toBe(canonValue(''))
    expect(canonValue([''])).not.toBe(canonValue([]))
    expect(canonValue(['a'])).not.toBe(canonValue('a'))
  })

  it('re-escapes embedded separators so two shapes cannot collide', () => {
    expect(canonValue([`a${RS}b`])).not.toBe(canonValue(['a', 'b']))
    expect(canonValue([['a'], ['b']])).not.toBe(canonValue(['a', 'b']))
  })

  it('escapes each ELEMENT before joining, so a nested sequence stays decodable', () => {
    // §2.5: elements are re-escaped when embedded. Without that pass the inner
    // sequence's own separators would be indistinguishable from the outer
    // sequence's, and two different structures could share one fingerprint.
    expect(canonValue([['a']])).toBe(`${RS}\\x1ea\\x1e${RS}`)

    // The ONLY raw RS bytes in a canonical sequence are the leading marker and
    // one trailer per element — never one that came out of an element.
    const nested = canonValue([['a'], ['b']])
    expect([...nested].filter((c) => c === RS)).toHaveLength(3)
  })
})

describe('fingerprintPayload — §5.4', () => {
  it("reproduces the contract's byte-for-byte worked example", () => {
    const payload = fingerprintPayload({
      type: 'C8',
      entity_refs: ['appdb:student:s7'],
      disagreeing_fields: [],
      observed_values: {
        household_key: 'parent@corp.com',
        dropped_source: 'crm',
        eligible_member_count: 3,
      },
    })

    const expected =
      'C8' +
      '|' +
      'appdb:student:s7' +
      '|' +
      '' +
      '|' +
      'dropped_source=crm' +
      US +
      'eligible_member_count=3' +
      US +
      'household_key=parent@corp.com'

    expect(payload).toBe(expected)
  })

  it('has exactly four sections joined by three vertical lines', () => {
    const payload = fingerprintPayload({
      type: 'C11',
      entity_refs: ['payments:payment:b', 'payments:payment:a'],
      disagreeing_fields: [],
      observed_values: {},
    })
    expect(payload.split('|')).toHaveLength(4)
  })

  it('sorts entity refs and observed keys, so element order never reaches the digest', () => {
    const one = fingerprintPayload({
      type: 'C11',
      entity_refs: ['payments:payment:b', 'payments:payment:a'],
      disagreeing_fields: [],
      observed_values: { z: 1, a: 2 },
    })
    const two = fingerprintPayload({
      type: 'C11',
      entity_refs: ['payments:payment:a', 'payments:payment:b'],
      disagreeing_fields: [],
      observed_values: { a: 2, z: 1 },
    })
    expect(one).toBe(two)
    expect(one).toContain(`payments:payment:a${US}payments:payment:b`)
    expect(one).toContain(`a=2${US}z=1`)
  })

  it('produces a 64-character lower-case hex digest', async () => {
    const digest = await sha256Hex('C8|appdb:student:s7||dropped_source=crm')
    expect(digest).toMatch(/^[0-9a-f]{64}$/)
  })
})
