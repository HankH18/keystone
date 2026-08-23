/**
 * `canon_value` and the conflict fingerprint payload, re-implemented from the
 * PINNED specification in docs/invariant-contract.md §2.5 and §5.4.
 *
 * This lives under src/mocks/ because only the mock needs it: the real API
 * returns `conflicts.fingerprint` as a column. It is implemented faithfully
 * (rather than with an invented id scheme) so the mock's fingerprints are the
 * system's own values, and it is unit-tested against the contract's own
 * worked byte-for-byte examples.
 */

const SECTION_SEPARATOR = '|'
/**
 * U+001F UNIT SEPARATOR — §5.4's intra-section joiner. Built from a char
 * code, not written literally: an invisible control character in source is a
 * byte nobody can review.
 */
const INTRA_SECTION_JOINER = String.fromCharCode(0x1f)
/** U+001E RECORD SEPARATOR — §2.5's sequence element separator. Ditto. */
const ELEMENT_SEPARATOR = String.fromCharCode(0x1e)

/** §2.5: backslash pass FIRST, then raw US, then raw RS. */
function escapeString(value: string): string {
  return value
    .split('\\')
    .join('\\\\')
    .split(INTRA_SECTION_JOINER)
    .join('\\x1f')
    .split(ELEMENT_SEPARATOR)
    .join('\\x1e')
}

/** §2.5 sequence case: backslash pass first, then raw RS becomes text `\x1e`. */
function escapeElement(value: string): string {
  return value
    .split('\\')
    .join('\\\\')
    .split(ELEMENT_SEPARATOR)
    .join('\\x1e')
}

export function canonValue(value: unknown): string {
  if (value === null || value === undefined) return '\\N'
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'number') {
    if (!Number.isInteger(value)) {
      throw new Error('canonValue: float is FORBIDDEN (invariant-contract §2.5)')
    }
    return String(value)
  }
  if (typeof value === 'string') return escapeString(value)
  if (Array.isArray(value)) {
    const elements = value.map((item) => escapeElement(canonValue(item)))
    elements.sort()
    return (
      ELEMENT_SEPARATOR + elements.map((e) => e + ELEMENT_SEPARATOR).join('')
    )
  }
  throw new TypeError(`canonValue: unsupported type ${typeof value}`)
}

export interface FingerprintInput {
  type: string
  entity_refs: string[]
  disagreeing_fields: string[]
  observed_values: Record<string, unknown>
}

/** §5.4: exactly four sections joined by three literal `|`. */
export function fingerprintPayload(input: FingerprintInput): string {
  const refs = input.entity_refs.map(canonValue)
  refs.sort()
  const fields = input.disagreeing_fields.map(canonValue)
  fields.sort()
  const keys = Object.keys(input.observed_values)
  keys.sort()
  const observed = keys.map(
    (k) => `${k}=${canonValue(input.observed_values[k])}`,
  )
  return [
    input.type,
    refs.join(INTRA_SECTION_JOINER),
    fields.join(INTRA_SECTION_JOINER),
    observed.join(INTRA_SECTION_JOINER),
  ].join(SECTION_SEPARATOR)
}

/** sha256, lower-case hex, 64 characters (§5.4). */
export async function sha256Hex(payload: string): Promise<string> {
  const bytes = new TextEncoder().encode(payload)
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

/** Mock-only: a stable uuid-shaped surrogate primary key for a fingerprint. */
export function fingerprintToUuid(fingerprint: string): string {
  const h = fingerprint.slice(0, 32)
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20, 32)}`
}

/** FNV-1a. Used ONLY for the mock's own deterministic pseudo-attributes. */
export function fnv1a(value: string): number {
  let hash = 0x811c9dc5
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return hash >>> 0
}
