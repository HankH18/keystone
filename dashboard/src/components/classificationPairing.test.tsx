/**
 * The disagreeing-fields table names WHICH SIDE each §6 classification belongs to.
 *
 * Found by driving the deployed dashboard, not by a unit test: conflict 2001
 * (C6, `crm.contact.lifecycle_stage` vs `appdb.student.status`) rendered its
 * Classification cell as the bare string
 *
 *     auto-apply eligible / sensitive
 *
 * from `{fieldClassification(row.left)} / {fieldClassification(row.right)}`.
 * Both halves were correct and neither was attributable. Sensitivity is the one
 * property that decides whether a fix may ever be automated (brief, "Sensitive
 * fields (normative)"), so a reviewer who reads the first half and stops has
 * drawn precisely the wrong conclusion — and the column header said only
 * "Classification", offering no way to recover the mapping.
 *
 * The existing suites assert `toHaveTextContent('auto-apply eligible')` and
 * `toHaveTextContent('sensitive')`, which a bare-slash rendering satisfies just
 * as well as a labelled one. That is why this file exists: it binds the
 * ATTRIBUTION, which is the part that was wrong.
 */
import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { gradeConflict, makeClient, nameConflict, renderApp } from '../test/harness'

async function fieldsTableText(seed: ReturnType<typeof nameConflict>): Promise<string> {
  const client = makeClient([seed])
  const conflicts = await client.listConflicts({ page_size: 1 })
  renderApp(client, `/conflicts/${conflicts.items[0].id}`)
  const table = await screen.findByTestId('disagreeing-fields')
  return table.textContent ?? ''
}

describe('disagreeing fields — each classification names its own source', () => {
  it('attributes the sensitive classification to a named side', async () => {
    const text = await fieldsTableText(nameConflict(11))

    // The classification is still shown...
    expect(text).toContain('sensitive')
    // ...and it is attributable. Every classification is preceded by the source
    // it describes, so no reader has to guess which path it applies to.
    expect(text).toMatch(/CRM — (sensitive|auto-apply eligible|not eligible for auto-apply)/)
    expect(text).toMatch(/App DB — (sensitive|auto-apply eligible|not eligible for auto-apply)/)
  })

  it('never renders two classifications as one unattributed slash-joined phrase', async () => {
    const text = await fieldsTableText(gradeConflict(12))

    // This is the exact shape the deployed build shipped. `X / Y` with no source
    // on either side is what this test exists to keep out.
    expect(text).not.toMatch(
      /(sensitive|auto-apply eligible|not eligible for auto-apply)\s*\/\s*(sensitive|auto-apply eligible|not eligible for auto-apply)/,
    )
  })
})
