import type { ButtonHTMLAttributes, ReactNode } from 'react'

/**
 * A button that goes INERT without going unfocusable.
 *
 * The native `disabled` attribute removes an element from the tab order, so a
 * control that disables itself in response to its own activation throws the
 * keyboard user's focus to `<body>` — "I pressed Approve and my cursor
 * vanished". Every inert state here is `aria-disabled` plus a guarded handler:
 * announced as disabled, still focusable, focus never lost.
 *
 * (Filters and pagination use it for the same reason: clearing the last filter
 * or paging to the last page would otherwise disable the control under the
 * user's own focus.)
 */
export interface ButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'disabled'> {
  inert?: boolean
  children: ReactNode
}

export function Button({
  inert = false,
  onClick,
  className = 'button',
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      type="button"
      className={className}
      aria-disabled={inert || undefined}
      onClick={(event) => {
        if (inert) {
          event.preventDefault()
          return
        }
        onClick?.(event)
      }}
      {...rest}
    >
      {children}
    </button>
  )
}
