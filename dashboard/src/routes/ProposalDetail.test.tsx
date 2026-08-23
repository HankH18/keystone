/**
 * The reviewer's decision: approve / reject round-tripping to the API, the
 * error state when it fails, and the structural rule that a held proposal is
 * never offered an auto-apply.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'
import { ApiError } from '../lib/api'
import {
  evidenceOnlyConflict,
  gradeConflict,
  makeClient,
  nameConflict,
  renderApp,
} from '../test/harness'
import type { KeystoneApi, Proposal } from '../lib/contract'

async function firstProposal(client: KeystoneApi): Promise<Proposal> {
  const page = await client.listProposals({ page_size: 1 })
  return page.items[0]
}

/**
 * The mock derives each proposal's status deterministically from its
 * fingerprint, so a test that needs an undecided proposal asks for one rather
 * than assuming index 0 happens to be pending.
 */
async function pendingProposal(client: KeystoneApi): Promise<Proposal> {
  const page = await client.listProposals({ status: 'pending', page_size: 1 })
  if (page.items.length === 0) {
    throw new Error('fixture produced no pending proposal')
  }
  return page.items[0]
}

/** Enough grade-only (non-sensitive) conflicts that some land pending. */
function gradeConflicts(count: number, offset = 0) {
  return Array.from({ length: count }, (_, i) => gradeConflict(offset + i))
}

beforeEach(() => {
  window.history.replaceState({}, '', '/')
})

describe('/proposals/:id — approve and reject', () => {
  it('approves a proposal, posts to the API, and shows the new status', async () => {
    const user = userEvent.setup()
    const base = makeClient(gradeConflicts(20))
    const proposal = await pendingProposal(base)

    const approved: string[] = []
    const client: KeystoneApi = {
      ...base,
      approveProposal: (id) => {
        approved.push(id)
        return base.approveProposal(id)
      },
    }

    renderApp(client, `/proposals/${proposal.id}`)

    const button = await screen.findByRole('button', { name: 'Approve proposal' })
    await user.click(button)

    await waitFor(() => expect(approved).toEqual([proposal.id]))
    await waitFor(() =>
      expect(screen.getAllByText('Approved').length).toBeGreaterThan(0),
    )
    await waitFor(() =>
      expect(screen.getByTestId('live-region')).toHaveTextContent(
        /is now Approved/,
      ),
    )
    // Read back from the service, not from optimistic local state.
    expect((await base.getProposal(proposal.id)).status).toBe('approved')
  })

  it('rejects a proposal and reads the rejection back', async () => {
    const user = userEvent.setup()
    const base = makeClient(gradeConflicts(20, 100))
    const proposal = await pendingProposal(base)

    renderApp(base, `/proposals/${proposal.id}`)

    await user.click(
      await screen.findByRole('button', { name: 'Reject proposal' }),
    )

    await waitFor(() =>
      expect(screen.getAllByText('Rejected').length).toBeGreaterThan(0),
    )
    expect((await base.getProposal(proposal.id)).status).toBe('rejected')
  })

  it('shows an accessible error and rolls the optimistic status back when the call fails', async () => {
    const user = userEvent.setup()
    const base = makeClient(gradeConflicts(20, 200))
    const proposal = await pendingProposal(base)
    const before = proposal.status

    let attempts = 0
    const client: KeystoneApi = {
      ...base,
      approveProposal: (id) => {
        attempts += 1
        if (attempts === 1) {
          return Promise.reject(
            new ApiError({
              type: 'about:blank',
              title: 'Bad Gateway',
              status: 502,
              detail: 'The reconciliation service did not answer.',
            }),
          )
        }
        return base.approveProposal(id)
      },
    }

    renderApp(client, `/proposals/${proposal.id}`)
    await user.click(
      await screen.findByRole('button', { name: 'Approve proposal' }),
    )

    const alert = await screen.findByTestId('decision-error')
    expect(alert).toHaveAttribute('role', 'alert')
    expect(alert).toHaveTextContent('502 Bad Gateway')
    expect(alert).toHaveTextContent('The reconciliation service did not answer.')
    expect(alert).toHaveTextContent('Nothing was changed.')
    await waitFor(() =>
      expect(screen.getByTestId('live-region')).toHaveTextContent(
        /Could not approve/,
      ),
    )
    // The service was never told to change anything.
    expect((await base.getProposal(proposal.id)).status).toBe(before)

    await user.click(within(alert).getByRole('button', { name: 'Try again' }))
    await waitFor(() =>
      expect(screen.queryByTestId('decision-error')).not.toBeInTheDocument(),
    )
    expect((await base.getProposal(proposal.id)).status).toBe('approved')
  })
})

describe('/proposals/:id — the sensitive hold', () => {
  it('never offers an apply affordance for a held proposal', async () => {
    const base = makeClient([nameConflict(1)])
    const proposal = await firstProposal(base)
    expect(proposal.status).toBe('sensitive_hold')

    renderApp(base, `/proposals/${proposal.id}`)

    await screen.findByTestId('sensitive-hold-notice')
    // Not merely disabled — absent. A disabled control still advertises the action.
    expect(
      screen.queryByRole('button', { name: /apply/i }),
    ).not.toBeInTheDocument()
    expect(screen.getAllByText('Held for human review').length).toBeGreaterThan(0)
    expect(screen.getByTestId('sensitive-hold-notice')).toHaveTextContent(
      /never be auto-applied/i,
    )
  })

  it('still lets a human approve or reject the hold', async () => {
    const base = makeClient([nameConflict(2)])
    const proposal = await firstProposal(base)
    renderApp(base, `/proposals/${proposal.id}`)

    expect(
      await screen.findByRole('button', { name: 'Approve proposal' }),
    ).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Reject proposal' })).toBeEnabled()
  })

  it('offers no apply affordance for an evidence-only proposal either', async () => {
    // C1 has no committed fix template that writes a field (§6), so there is
    // nothing an apply could do — and no control offering it.
    const base = makeClient([evidenceOnlyConflict(1)])
    const proposal = await firstProposal(base)
    expect(proposal.action.kind).toBe('evidence_only')
    expect(proposal.sensitive).toBe(false)

    renderApp(base, `/proposals/${proposal.id}`)

    await screen.findByRole('button', { name: 'Approve proposal' })
    expect(
      screen.queryByRole('button', { name: /apply/i }),
    ).not.toBeInTheDocument()
    expect(await screen.findByTestId('evidence-only-notice')).toHaveTextContent(
      /nothing to apply/i,
    )
  })

  it('offers apply only once a non-sensitive field-writing proposal is approved', async () => {
    const user = userEvent.setup()
    const base = makeClient(gradeConflicts(20, 300))
    const proposal = await pendingProposal(base)

    renderApp(base, `/proposals/${proposal.id}`)
    await screen.findByRole('button', { name: 'Approve proposal' })
    expect(
      screen.queryByRole('button', { name: 'Apply approved fix' }),
    ).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Approve proposal' }))
    expect(
      await screen.findByRole('button', { name: 'Apply approved fix' }),
    ).toBeEnabled()
  })
})

describe('/proposals/:id — what the reviewer can see', () => {
  it('shows confidence as a two-decimal tabular figure, the fingerprint, the evidence and the status', async () => {
    const base = makeClient([gradeConflict(9)])
    const proposal = await firstProposal(base)

    renderApp(base, `/proposals/${proposal.id}`)

    const confidence = await screen.findByTestId('confidence')
    expect(confidence.textContent).toMatch(/^\d\.\d{2}$/)
    expect(confidence).toHaveClass('num')

    expect(screen.getByTestId('fingerprint')).toHaveTextContent(
      proposal.fingerprint,
    )
    expect(screen.getByTestId('evidence-packet')).toHaveTextContent(
      'observed_values',
    )
    expect(screen.getByTestId('evidence-packet')).toHaveTextContent('R-006')
    expect(
      screen.getByRole('heading', { level: 2, name: 'Evidence packet' }),
    ).toBeInTheDocument()
  })
})

describe('/conflicts/:id — the row detail', () => {
  it('shows the disagreeing sources and the source-qualified field paths', async () => {
    const base = makeClient([gradeConflict(11)])
    const conflicts = await base.listConflicts({ page_size: 1 })
    const conflict = conflicts.items[0]

    renderApp(base, `/conflicts/${conflict.id}`)

    expect(await screen.findByTestId('disagreeing-sources')).toHaveTextContent(
      'App DB + CRM',
    )

    const fields = await screen.findByTestId('disagreeing-fields')
    expect(fields).toHaveTextContent('crm.contact.grade')
    expect(fields).toHaveTextContent('appdb.student.grade')
    expect(fields).toHaveTextContent('grade')
    // The §6 classification is shown next to the path, so the reviewer knows
    // why a hold is a hold.
    expect(fields).toHaveTextContent('auto-apply eligible')

    expect(screen.getByTestId('fingerprint')).toHaveTextContent(
      conflict.fingerprint,
    )
    expect(screen.getAllByText('Open').length).toBeGreaterThan(0)
  })

  it('marks a wholly-sensitive conflict s paths as sensitive', async () => {
    const base = makeClient([nameConflict(11)])
    const conflicts = await base.listConflicts({ page_size: 1 })

    renderApp(base, `/conflicts/${conflicts.items[0].id}`)

    const fields = await screen.findByTestId('disagreeing-fields')
    expect(fields).toHaveTextContent('crm.contact.first_name')
    expect(fields).toHaveTextContent('sensitive')
    expect(await screen.findByTestId('sensitive-hold-notice')).toBeInTheDocument()
  })
})
