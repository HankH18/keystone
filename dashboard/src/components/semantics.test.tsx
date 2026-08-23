/**
 * The accessibility semantics that carry meaning, bound to assertions.
 *
 * axe cannot catch any of these: a table whose scroll container loses its
 * `aria-label`, a `<th scope="row">` demoted to a `<td>`, or a pagination
 * `<nav>` with no accessible name are all VALID HTML that pass every automated
 * rule while quietly destroying how the page reads in a screen reader. They
 * were deletable with both suites green. They are not any more.
 */
import { screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { gradeConflict, makeClient, nameConflict, renderApp } from '../test/harness'

const entries = [
  ...Array.from({ length: 12 }, (_, i) => gradeConflict(i)),
  ...Array.from({ length: 7 }, (_, i) => nameConflict(i)),
]

beforeEach(() => {
  window.history.replaceState({}, '', '/')
})

describe('table semantics', () => {
  it('puts the conflicts table in a NAMED, focusable scroll region', async () => {
    renderApp(makeClient(entries), '/conflicts?page_size=5')
    await screen.findByRole('table', { name: /conflicts/i })

    // The name must be the caption text, not "region" or nothing: a keyboard
    // user who tabs into a horizontally scrollable box has to be told what box.
    const region = screen.getByRole('region', {
      name: 'Conflicts — 19 match the current filters',
    })
    expect(region).toHaveAttribute('tabindex', '0')
    expect(within(region).getByRole('table')).toBeInTheDocument()
  })

  it('puts the proposals table in a NAMED, focusable scroll region', async () => {
    renderApp(makeClient(entries), '/proposals?page_size=5')
    await screen.findByRole('table', { name: /proposals/i })

    const region = screen.getByRole('region', {
      name: 'Proposals — 19 match the current filters',
    })
    expect(region).toHaveAttribute('tabindex', '0')
  })

  it('gives the conflicts table a caption', async () => {
    renderApp(makeClient(entries), '/conflicts?page_size=5')
    const table = await screen.findByRole('table', { name: /conflicts/i })
    expect(table.querySelector('caption')?.textContent).toMatch(
      /Conflicts — 19 match the current filters/,
    )
  })

  it.each([
    ['/conflicts?page_size=5', /conflicts/i],
    ['/proposals?page_size=5', /proposals/i],
  ])(
    'starts every row of %s with a <th scope="row">, not a <td>',
    async (url, name) => {
      renderApp(makeClient(entries), url)
      const table = await screen.findByRole('table', { name })
      const bodyRows = within(table).getAllByRole('row').slice(1)
      expect(bodyRows).toHaveLength(5)

      for (const row of bodyRows) {
        const first = row.firstElementChild
        expect(first?.tagName).toBe('TH')
        // Without scope="row" the header does not attach to its row, and every
        // cell after it is announced with no idea which record it belongs to.
        expect(first).toHaveAttribute('scope', 'row')
        expect(within(row).getAllByRole('rowheader')).toHaveLength(1)
      }
    },
  )

  it('marks every column header with scope="col"', async () => {
    renderApp(makeClient(entries), '/conflicts?page_size=5')
    const table = await screen.findByRole('table', { name: /conflicts/i })
    const headerRow = within(table).getAllByRole('row')[0]
    const headers = within(headerRow).getAllByRole('columnheader')
    expect(headers.length).toBeGreaterThan(4)
    for (const header of headers) {
      expect(header).toHaveAttribute('scope', 'col')
    }
  })
})

describe('landmark names', () => {
  it.each([
    ['/conflicts?page_size=5', 'conflicts pagination'],
    ['/proposals?page_size=5', 'proposals pagination'],
  ])('names the pagination nav on %s "%s"', async (url, name) => {
    renderApp(makeClient(entries), url)
    await screen.findByRole('table')

    // Two <nav>s on the page. Unnamed, a screen reader's landmark list reads
    // "navigation, navigation" and the reviewer has to enter one to find out.
    const pagination = screen.getByRole('navigation', { name })
    expect(within(pagination).getByRole('button', { name: 'Next page' })).toBeInTheDocument()

    const navs = screen.getAllByRole('navigation')
    const names = navs.map((nav) => nav.getAttribute('aria-label'))
    expect(names).toContain('Primary')
    expect(names).toContain(name)
    expect(new Set(names).size).toBe(names.length)
  })
})
