/**
 * Status vocabulary: the label, the plain-English description and the icon
 * silhouette for every state. Kept out of the component file so both the
 * renderer and the filter controls read from ONE list.
 *
 * R12: status is never colour alone. The label here is the accessible name;
 * the icon and the border treatment (index.css) are the other two channels.
 */
import type { ProposalStatus } from './contract'

export type StatusIconName =
  | 'clock'
  | 'check'
  | 'cross'
  | 'double-check'
  | 'undo'
  | 'shield'
  | 'dot'
  | 'zigzag'
  | 'question'

export interface StatusMeta {
  /** The visible, accessible label. */
  label: string
  /** One line of plain English for detail views and tooltip-free help text. */
  description: string
  icon: StatusIconName
}

/** PINNED vocabulary — DESIGN §Data models, `proposals.status`. */
export const PROPOSAL_STATUS_META: Record<ProposalStatus, StatusMeta> = {
  pending: {
    label: 'Pending review',
    description: 'Waiting for a reviewer decision. Nothing has been written.',
    icon: 'clock',
  },
  approved: {
    label: 'Approved',
    description: 'A reviewer approved the fix. It has not been applied yet.',
    icon: 'check',
  },
  rejected: {
    label: 'Rejected',
    description: 'A reviewer rejected the fix. Nothing will be written.',
    icon: 'cross',
  },
  applied: {
    label: 'Applied',
    description: 'The fix was written to the canonical layer and is reversible.',
    icon: 'double-check',
  },
  rolled_back: {
    label: 'Rolled back',
    description: 'The fix was applied and then reversed from its stored before-state.',
    icon: 'undo',
  },
  sensitive_hold: {
    label: 'Held for human review',
    description:
      'The fix targets a sensitive field, so it is held for a person by design. This is not a failure, and it can never auto-apply.',
    icon: 'shield',
  },
}

/** ASSUMED vocabulary (contract.ts A6) — unknown values degrade gracefully. */
export const CONFLICT_STATUS_META: Record<string, StatusMeta> = {
  open: {
    label: 'Open',
    description: 'Detected on the latest run and not yet resolved.',
    icon: 'dot',
  },
  'escalated:oscillation': {
    label: 'Escalated — oscillating',
    description:
      'The underlying field flipped A to B and back across generations, so the conflict is escalated instead of re-proposed.',
    icon: 'zigzag',
  },
}

export function statusMeta(
  status: string,
  vocabulary: Record<string, StatusMeta>,
): StatusMeta & { known: boolean } {
  const found = vocabulary[status]
  if (found) return { ...found, known: true }
  // Never crash on a status the service adds later; show it verbatim.
  return {
    label: status,
    description: 'Status reported by the service that this dashboard build does not recognise.',
    icon: 'question',
    known: false,
  }
}

