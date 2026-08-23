/**
 * The conflicts route, driven through the real UI: filtering, pagination and
 * the error state.
 *
 * These bind on behaviour a reviewer can see — the row count, the row contents,
 * the pagination summary, the request the client actually received — not on
 * internal state.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { ApiError } from '../lib/api'
import {
  gradeConflict,
  makeClient,
  nameConflict,
  paymentConflict,
  renderApp,
} from '../test/harness'
import type { ConflictQuery, KeystoneApi } from '../lib/contract'

const entries = [
  ...Array.from({ length: 12 }, (_, i) => gradeConflict(i)),
  ...Array.from({ length: 7 }, (_, i) => nameConflict(i)),
  ...Array.from({ length: 5 }, (_, i) => paymentConflict(i)),
]

function rowCount(): number {
  const table = screen.getByRole('table', { name: /conflicts/i })
  return within(table).getAllByRole('row').length - 1 // minus the header row
}

beforeEach(() => {
  window.history.replaceState({}, '', '/')
})

describe('/conflicts — pagination', () => {
  it('asks the SERVER for one page and renders exactly that page', async () => {
    const base = makeClient(entries)
    const seen: ConflictQuery[] = []
    const client: KeystoneApi = {
      ...base,
      listConflicts: (query, signal) => {
        seen.push(query)
        return base.listConflicts(query, signal)
      },
    }

    renderApp(client, '/conflicts?page_size=5')

    await screen.findByRole('table', { name: /conflicts/i })
    await waitFor(() => expect(rowCount()).toBe(5))

    expect(seen.at(-1)).toMatchObject({ page: 1, page_size: 5 })
    expect(screen.getByTestId('pagination-status')).toHaveTextContent(
      'Showing 1–5 of 24 conflicts. Page 1 of 5.',
    )
  })

  it('advances to the next page and shows different rows', async () => {
    const user = userEvent.setup()
    renderApp(makeClient(entries), '/conflicts?page_size=5')

    await screen.findByRole('table', { name: /conflicts/i })
    const firstPageCells = within(
      screen.getByRole('table', { name: /conflicts/i }),
    )
      .getAllByRole('row')
      .slice(1)
      .map((row) => row.textContent)

    await user.click(screen.getByRole('button', { name: 'Next page' }))

    await waitFor(() =>
      expect(screen.getByTestId('pagination-status')).toHaveTextContent(
        'Showing 6–10 of 24 conflicts. Page 2 of 5.',
      ),
    )
    const secondPageCells = within(
      screen.getByRole('table', { name: /conflicts/i }),
    )
      .getAllByRole('row')
      .slice(1)
      .map((row) => row.textContent)
    expect(secondPageCells).not.toEqual(firstPageCells)
    expect(window.location.search).toContain('page=2')
  })

  it('marks Previous/Next inert at the ends WITHOUT removing them from the tab order', async () => {
    renderApp(makeClient(entries), '/conflicts?page_size=25')

    await screen.findByRole('table', { name: /conflicts/i })
    const previous = screen.getByRole('button', { name: 'Previous page' })
    const next = screen.getByRole('button', { name: 'Next page' })

    await waitFor(() =>
      expect(previous).toHaveAttribute('aria-disabled', 'true'),
    )
    expect(next).toHaveAttribute('aria-disabled', 'true')
    // Still focusable: a control that disables itself under the user's own
    // focus throws a keyboard user back to <body>.
    expect(previous).not.toBeDisabled()
    previous.focus()
    expect(previous).toHaveFocus()
  })
})

describe('/conflicts — filtering', () => {
  it('filters by conflict type on the SERVER and narrows the table', async () => {
    const user = userEvent.setup()
    const base = makeClient(entries)
    const seen: ConflictQuery[] = []
    const client: KeystoneApi = {
      ...base,
      listConflicts: (query, signal) => {
        seen.push(query)
        return base.listConflicts(query, signal)
      },
    }

    renderApp(client, '/conflicts?page_size=25')
    await screen.findByRole('table', { name: /conflicts/i })
    await waitFor(() => expect(rowCount()).toBe(24))

    await user.selectOptions(
      screen.getByLabelText('Conflict type'),
      'C14',
    )

    await waitFor(() => expect(rowCount()).toBe(7))
    expect(seen.at(-1)).toMatchObject({ type: 'C14' })
    expect(window.location.search).toContain('type=C14')
  })

  it('filters by source', async () => {
    const user = userEvent.setup()
    renderApp(makeClient(entries), '/conflicts?page_size=25')
    await screen.findByRole('table', { name: /conflicts/i })
    await waitFor(() => expect(rowCount()).toBe(24))

    await user.selectOptions(screen.getByLabelText('Source'), 'payments')
    await waitFor(() => expect(rowCount()).toBe(5))
  })

  it('filters by status', async () => {
    const user = userEvent.setup()
    const withOscillation = [
      ...entries,
      { ...gradeConflict(500), oscillating: true },
    ]
    renderApp(makeClient(withOscillation), '/conflicts?page_size=25')
    await screen.findByRole('table', { name: /conflicts/i })
    await waitFor(() => expect(rowCount()).toBe(25))

    await user.selectOptions(
      screen.getByLabelText('Status'),
      'escalated:oscillation',
    )
    await waitFor(() => expect(rowCount()).toBe(1))
  })

  it('returns to page 1 when a filter changes', async () => {
    const user = userEvent.setup()
    renderApp(makeClient(entries), '/conflicts?page_size=5&page=3')
    await screen.findByRole('table', { name: /conflicts/i })
    await waitFor(() =>
      expect(screen.getByTestId('pagination-status')).toHaveTextContent(
        'Page 3 of 5',
      ),
    )

    await user.selectOptions(screen.getByLabelText('Conflict type'), 'C6')
    await waitFor(() =>
      expect(screen.getByTestId('pagination-status')).toHaveTextContent(
        'Page 1 of',
      ),
    )
    expect(window.location.search).not.toContain('page=3')
  })

  it('clears every filter with one control', async () => {
    const user = userEvent.setup()
    renderApp(makeClient(entries), '/conflicts?page_size=25&type=C14')
    await screen.findByRole('table', { name: /conflicts/i })
    await waitFor(() => expect(rowCount()).toBe(7))

    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    await waitFor(() => expect(rowCount()).toBe(24))
  })

  it('says so plainly when nothing matches', async () => {
    const user = userEvent.setup()
    renderApp(makeClient(entries), '/conflicts?page_size=25')
    await screen.findByRole('table', { name: /conflicts/i })

    await user.selectOptions(screen.getByLabelText('Conflict type'), 'C13')
    await waitFor(() =>
      expect(
        screen.getByText('No conflicts match these filters.'),
      ).toBeInTheDocument(),
    )
  })
})

describe('/conflicts — error state', () => {
  it('shows a visible, announced, retryable error when the service fails', async () => {
    const user = userEvent.setup()
    const base = makeClient(entries)
    let failures = 0
    const client: KeystoneApi = {
      ...base,
      listConflicts: (query, signal) => {
        if (failures === 0) {
          failures += 1
          return Promise.reject(
            new ApiError({
              type: 'about:blank',
              title: 'Service Unavailable',
              status: 503,
              detail: 'The reconciliation service is not reachable.',
            }),
          )
        }
        return base.listConflicts(query, signal)
      },
    }

    renderApp(client, '/conflicts')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not load conflicts')
    expect(alert).toHaveTextContent('503 Service Unavailable')
    expect(alert).toHaveTextContent('The reconciliation service is not reachable.')

    await user.click(within(alert).getByRole('button', { name: 'Retry' }))
    await screen.findByRole('table', { name: /conflicts/i })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('announces each new page of results in the live region', async () => {
    renderApp(makeClient(entries), '/conflicts?page_size=5')
    await screen.findByRole('table', { name: /conflicts/i })
    await waitFor(() =>
      expect(screen.getByTestId('live-region')).toHaveTextContent(
        'Showing 5 of 24 matching conflicts, page 1.',
      ),
    )
    const region = screen.getByTestId('live-region')
    expect(region).toHaveAttribute('aria-live', 'polite')
  })
})

describe('app shell', () => {
  it('has one h1, a skip link that targets main, and labelled landmarks', async () => {
    renderApp(makeClient(entries), '/conflicts')

    const skip = screen.getByRole('link', { name: /skip to main content/i })
    expect(skip).toHaveAttribute('href', '#main-content')
    expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content')

    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(
      screen.getByRole('heading', { level: 1, name: 'Conflicts' }),
    ).toBeInTheDocument()

    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: 'Conflicts' }),
    ).toHaveAttribute('aria-current', 'page')
  })

  it('does not show the mock banner when the real client is in use', () => {
    // VITE_USE_MOCK_API is unset in the test environment, which is the point:
    // the default build path is the real client.
    renderApp(makeClient(entries), '/conflicts')
    expect(screen.queryByTestId('mock-banner')).not.toBeInTheDocument()
  })
})
