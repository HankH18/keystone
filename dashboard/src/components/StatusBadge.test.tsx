/**
 * The status renderer (R12).
 *
 * These assert the TEXT LABEL and the ICON — never a CSS class. A test that
 * asserted `.status-badge--approved` would still pass with no accessible name
 * at all, which is exactly the failure mode this requirement exists to prevent.
 * The label is queried as visible text, so it must actually be in the
 * accessibility tree.
 */
import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StatusBadge } from './StatusBadge'
import { PROPOSAL_STATUSES } from '../lib/contract'
import { CONFLICT_STATUS_META, PROPOSAL_STATUS_META } from '../lib/statusMeta'

const EXPECTED_LABEL_AND_ICON: Record<string, { label: string; icon: string }> = {
  pending: { label: 'Pending review', icon: 'clock' },
  approved: { label: 'Approved', icon: 'check' },
  rejected: { label: 'Rejected', icon: 'cross' },
  applied: { label: 'Applied', icon: 'double-check' },
  rolled_back: { label: 'Rolled back', icon: 'undo' },
  sensitive_hold: { label: 'Held for human review', icon: 'shield' },
}

describe('StatusBadge — proposal statuses', () => {
  it.each(PROPOSAL_STATUSES)(
    'renders %s with its own visible label and its own icon',
    (status) => {
      const { container } = render(<StatusBadge status={status} />)
      const expected = EXPECTED_LABEL_AND_ICON[status]

      // 1. The label is real, visible text.
      expect(screen.getByText(expected.label)).toBeVisible()

      // 2. The icon is present, is the RIGHT icon, and is hidden from the
      //    accessibility tree because the label already carries the meaning.
      const icon = container.querySelector('svg[data-status-icon]')
      expect(icon).not.toBeNull()
      expect(icon).toHaveAttribute('data-status-icon', expected.icon)
      expect(icon).toHaveAttribute('aria-hidden', 'true')
    },
  )

  it('gives all six states distinct labels and distinct icons', () => {
    const labels = PROPOSAL_STATUSES.map((s) => PROPOSAL_STATUS_META[s].label)
    const icons = PROPOSAL_STATUSES.map((s) => PROPOSAL_STATUS_META[s].icon)
    expect(new Set(labels).size).toBe(PROPOSAL_STATUSES.length)
    expect(new Set(icons).size).toBe(PROPOSAL_STATUSES.length)
  })

  it('reads sensitive_hold as a deliberate hold, not a failure', () => {
    render(<StatusBadge status="sensitive_hold" />)
    const label = screen.getByText('Held for human review')
    expect(label).toBeVisible()
    expect(label.textContent?.toLowerCase()).not.toMatch(
      /fail|error|blocked|denied|rejected/,
    )
    expect(PROPOSAL_STATUS_META.sensitive_hold.description).toMatch(
      /not a failure/i,
    )
  })

  it('renders the label as text content, not as a colour-only cue', () => {
    const { container } = render(<StatusBadge status="rejected" />)
    const badge = container.querySelector('.status-badge') as HTMLElement
    // Strip the icon: what remains must still say which state this is.
    expect(within(badge).getByText('Rejected')).toBeInTheDocument()
    expect(badge.textContent?.trim()).toBe('Rejected')
  })
})

describe('StatusBadge — conflict statuses', () => {
  it('labels an open conflict', () => {
    const { container } = render(<StatusBadge status="open" kind="conflict" />)
    expect(screen.getByText('Open')).toBeVisible()
    expect(container.querySelector('svg[data-status-icon]')).toHaveAttribute(
      'data-status-icon',
      'dot',
    )
  })

  it('labels an oscillation escalation in words', () => {
    render(<StatusBadge status="escalated:oscillation" kind="conflict" />)
    expect(
      screen.getByText(CONFLICT_STATUS_META['escalated:oscillation'].label),
    ).toBeVisible()
    expect(
      CONFLICT_STATUS_META['escalated:oscillation'].label.toLowerCase(),
    ).toContain('oscillat')
  })
})

describe('StatusBadge — unknown values', () => {
  it('shows a status this build does not know rather than crashing or blanking', () => {
    const { container } = render(<StatusBadge status="quarantined" />)
    expect(screen.getByText('quarantined')).toBeVisible()
    expect(container.querySelector('svg[data-status-icon]')).toHaveAttribute(
      'data-status-icon',
      'question',
    )
    expect(container.querySelector('.status-badge')).toHaveAttribute(
      'data-status-value',
      'quarantined',
    )
  })
})
