import { useCallback, useMemo, useState, type ReactNode } from 'react'
import { AnnouncerContext } from '../lib/announcer'

/**
 * One polite live region for the whole app.
 *
 * Async state changes — a page of results arriving, a decision landing, a
 * request failing — are announced here. A sighted reviewer sees the table
 * repaint; without this a screen-reader reviewer gets silence.
 */
export function AnnouncerProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState('')
  const announce = useCallback((next: string) => setMessage(next), [])
  const value = useMemo(() => ({ message, announce }), [message, announce])
  return (
    <AnnouncerContext.Provider value={value}>
      {children}
      <div
        className="visually-hidden"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        data-testid="live-region"
      >
        {message}
      </div>
    </AnnouncerContext.Provider>
  )
}
