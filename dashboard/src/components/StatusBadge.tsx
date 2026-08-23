/**
 * Status rendering. R12: status is NEVER colour alone.
 *
 * Every state carries three independent channels:
 *   1. a TEXT LABEL, always rendered, always the accessible name;
 *   2. an ICON with a distinct silhouette (`data-status-icon`), aria-hidden
 *      because the label already carries the meaning;
 *   3. a distinct BORDER treatment (dashed / solid / double / dotted / pill,
 *      see index.css) so the states are separable in greyscale.
 *
 * A colourblind reviewer, a greyscale printout and a screen reader all lose
 * nothing. `sensitive_hold` in particular is labelled "Held for human review",
 * not "blocked" or "failed" — it is a deliberate hold, not an error.
 */
import type { ReactNode } from 'react'
import {
  CONFLICT_STATUS_META,
  PROPOSAL_STATUS_META,
  statusMeta,
  type StatusIconName,
} from '../lib/statusMeta'

const ICON_PATHS: Record<StatusIconName, ReactNode> = {
  clock: (
    <>
      <circle cx="8" cy="8" r="6.25" fill="none" strokeWidth="1.6" />
      <path d="M8 4.4V8l2.6 1.9" fill="none" strokeWidth="1.6" strokeLinecap="round" />
    </>
  ),
  check: (
    <>
      <circle cx="8" cy="8" r="6.25" fill="none" strokeWidth="1.6" />
      <path d="M4.9 8.3 7 10.4l4.1-4.5" fill="none" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
    </>
  ),
  cross: (
    <>
      <circle cx="8" cy="8" r="6.25" fill="none" strokeWidth="1.6" />
      <path d="M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8" fill="none" strokeWidth="1.9" strokeLinecap="round" />
    </>
  ),
  'double-check': (
    <>
      <rect x="1.5" y="1.5" width="13" height="13" rx="1.5" fill="none" strokeWidth="1.6" />
      <path d="M3.6 8.2 5.4 10l3-3.4" fill="none" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M7.6 10 9 11.4l3.6-4.4" fill="none" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
    </>
  ),
  undo: (
    <>
      <path
        d="M3.2 8a4.8 4.8 0 1 0 1.6-3.6"
        fill="none"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
      <path d="M2.2 1.8v3.2h3.2" fill="none" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
    </>
  ),
  shield: (
    <>
      <path
        d="M8 1.4 2.6 3.4v4.3c0 3.2 2.3 5.6 5.4 6.9 3.1-1.3 5.4-3.7 5.4-6.9V3.4Z"
        fill="none"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path d="M6.6 6.4v3.6M9.4 6.4v3.6" fill="none" strokeWidth="1.7" strokeLinecap="round" />
    </>
  ),
  dot: (
    <>
      <circle cx="8" cy="8" r="6.25" fill="none" strokeWidth="1.6" />
      <circle cx="8" cy="8" r="2.6" fill="currentColor" stroke="none" />
    </>
  ),
  zigzag: (
    <>
      <path
        d="M1.6 11.2 4.4 4.8l2.8 6.4 2.8-6.4 2.8 6.4"
        fill="none"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </>
  ),
  question: (
    <>
      <circle cx="8" cy="8" r="6.25" fill="none" strokeWidth="1.6" />
      <path
        d="M6.2 6.2a1.9 1.9 0 1 1 2.4 1.9c-.5.2-.7.6-.7 1.1v.4"
        fill="none"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      <circle cx="8" cy="11.6" r="0.9" fill="currentColor" stroke="none" />
    </>
  ),
}

export function StatusIcon({ name }: { name: StatusIconName }) {
  return (
    <svg
      className="status-icon"
      data-status-icon={name}
      width="16"
      height="16"
      viewBox="0 0 16 16"
      stroke="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      {ICON_PATHS[name]}
    </svg>
  )
}

export interface StatusBadgeProps {
  status: string
  /** Which vocabulary to read the label from. */
  kind?: 'proposal' | 'conflict'
}

export function StatusBadge({ status, kind = 'proposal' }: StatusBadgeProps) {
  const meta = statusMeta(
    status,
    kind === 'proposal' ? PROPOSAL_STATUS_META : CONFLICT_STATUS_META,
  )
  return (
    <span
      className="status-badge"
      data-status={meta.known ? status : 'unknown'}
      data-status-value={status}
    >
      <StatusIcon name={meta.icon} />
      <span>{meta.label}</span>
    </span>
  )
}
