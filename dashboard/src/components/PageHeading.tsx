import type { ReactNode } from 'react'

/**
 * The one `<h1>` per route.
 *
 * `tabIndex={-1}` and a stable id so the router can move focus here after a
 * client-side navigation: without it a keyboard user activates a link and their
 * focus stays on a link that no longer exists, with nothing announced.
 */
export function PageHeading({ children }: { children: ReactNode }) {
  return (
    <h1 id="page-heading" tabIndex={-1}>
      {children}
    </h1>
  )
}
