#!/usr/bin/env node
/**
 * Regenerates the MOCK seed from the REAL committed grading artifacts in
 * `<repo>/golden/`.
 *
 * The dashboard's mock service (src/mocks/**) is seeded from these files so its
 * shapes, its fourteen conflict types and its volumes are the system's, not
 * invented. This script is the only writer of src/mocks/seed/**; never
 * hand-edit those files.
 *
 * Run:  pnpm mock:seed
 *
 * What it emits (all under src/mocks/seed/):
 *   golden-conflicts.json  the 3,050 golden entries, `compound_with` removed
 *                          (golden-side metadata the HTTP API never returns —
 *                          contract §8 says the detector does not emit it)
 *   golden-summary.json    counts + self-check booleans from manifest-summary
 *   provenance.json        sha256 of each source file, so a reader can prove
 *                          the seed came from golden/ and detect drift
 */
import { createHash } from 'node:crypto'
import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const dashboardRoot = resolve(here, '..')
const repoRoot = resolve(dashboardRoot, '..')
const goldenDir = join(repoRoot, 'golden')
const outDir = join(dashboardRoot, 'src', 'mocks', 'seed')

function sha256(buf) {
  return createHash('sha256').update(buf).digest('hex')
}

function readGolden(name) {
  const path = join(goldenDir, name)
  const raw = readFileSync(path)
  return { path, raw, json: JSON.parse(raw.toString('utf8')) }
}

const conflicts = readGolden('conflicts.json')
const summary = readGolden('manifest-summary.json')

if (!Array.isArray(conflicts.json)) {
  throw new Error('golden/conflicts.json is not an array — refusing to seed')
}

// Strip golden-only metadata. Everything that survives is a shape the
// detector/API side actually carries (contract §8).
const entries = conflicts.json.map((entry) => ({
  type: entry.type,
  rule_id: entry.rule_id,
  entity_refs: entry.entity_refs,
  sources_involved: entry.sources_involved,
  disagreeing_fields: entry.disagreeing_fields,
  observed_values: entry.observed_values,
  oscillating: entry.oscillating,
}))

const s = summary.json
const summaryOut = {
  profile: s.profile,
  seed: s.seed,
  golden_entries: s.golden_entries,
  entity_count: s.entity_count,
  conflict_counts: s.conflict_counts,
  conflict_minimums: s.conflict_minimums,
  clean_sample_size: s.clean_sample_size,
  fully_consistent_entity_fraction: s.fully_consistent_entity_fraction,
  self_check: s.self_check,
}

const provenance = {
  generated_by: 'dashboard/scripts/build-mock-seed.mjs',
  sources: {
    'golden/conflicts.json': sha256(conflicts.raw),
    'golden/manifest-summary.json': sha256(summary.raw),
  },
  entry_count: entries.length,
}

function writeJson(name, value) {
  // Stable formatting so a re-run of an unchanged golden tree is a no-op diff.
  writeFileSync(join(outDir, name), `${JSON.stringify(value, null, 0)}\n`)
}

writeJson('golden-conflicts.json', entries)
writeJson('golden-summary.json', summaryOut)
writeFileSync(
  join(outDir, 'provenance.json'),
  `${JSON.stringify(provenance, null, 2)}\n`,
)

console.log(
  `wrote ${entries.length} golden entries + summary + provenance to src/mocks/seed/`,
)
