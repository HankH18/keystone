/**
 * A ~100-line History-API router.
 *
 * Deliberately not a dependency: the dashboard has six routes and needs
 * exactly three things from a router — a pathname, path params, and a query
 * string that survives a reload so filters and page numbers are shareable.
 * A router library would add bundle and API surface without adding any of that.
 *
 * Accessibility notes that are part of the design, not decoration:
 *   - `<Link>` renders a real `<a href>`, so it is focusable, announced as a
 *     link, and works with middle-click / open-in-new-tab. Modified clicks fall
 *     through to the browser.
 *   - After a client-side navigation the router moves focus to the new page's
 *     `<h1>` (see `useFocusOnRouteChange`), so keyboard and screen-reader users
 *     are not left at the top of a page they cannot tell has changed.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type AnchorHTMLAttributes,
  type ReactNode,
} from 'react'

export interface RouteMatch {
  name: RouteName
  params: Record<string, string>
}

export type RouteName =
  | 'overview'
  | 'conflicts'
  | 'conflict-detail'
  | 'proposals'
  | 'proposal-detail'
  | 'audit'
  | 'not-found'

interface LocationState {
  pathname: string
  search: string
}

interface RouterValue extends LocationState {
  match: RouteMatch
  navigate: (to: string, options?: { replace?: boolean }) => void
}

const RouterContext = createContext<RouterValue | null>(null)

export function matchRoute(pathname: string): RouteMatch {
  const path = pathname.replace(/\/+$/, '') || '/'
  if (path === '/' || path === '/overview') return { name: 'overview', params: {} }
  if (path === '/conflicts') return { name: 'conflicts', params: {} }
  if (path === '/proposals') return { name: 'proposals', params: {} }
  if (path === '/audit') return { name: 'audit', params: {} }
  const conflict = /^\/conflicts\/([^/]+)$/.exec(path)
  if (conflict) {
    return { name: 'conflict-detail', params: { id: decodeURIComponent(conflict[1]) } }
  }
  const proposal = /^\/proposals\/([^/]+)$/.exec(path)
  if (proposal) {
    return { name: 'proposal-detail', params: { id: decodeURIComponent(proposal[1]) } }
  }
  return { name: 'not-found', params: {} }
}

function readLocation(): LocationState {
  return {
    pathname: window.location.pathname,
    search: window.location.search,
  }
}

export function Router({ children }: { children: ReactNode }) {
  const [location, setLocation] = useState<LocationState>(readLocation)

  useEffect(() => {
    const onPopState = () => setLocation(readLocation())
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const navigate = useCallback(
    (to: string, options?: { replace?: boolean }) => {
      if (options?.replace) window.history.replaceState({}, '', to)
      else window.history.pushState({}, '', to)
      setLocation(readLocation())
    },
    [],
  )

  const value = useMemo<RouterValue>(
    () => ({ ...location, match: matchRoute(location.pathname), navigate }),
    [location, navigate],
  )

  return <RouterContext.Provider value={value}>{children}</RouterContext.Provider>
}

export function useRouter(): RouterValue {
  const value = useContext(RouterContext)
  if (!value) throw new Error('useRouter must be used inside <Router>')
  return value
}

export type LinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & {
  to: string
  replace?: boolean
}

export function Link({ to, replace, onClick, ...rest }: LinkProps) {
  const { navigate } = useRouter()
  return (
    <a
      href={to}
      onClick={(event) => {
        onClick?.(event)
        if (
          event.defaultPrevented ||
          event.button !== 0 ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey
        ) {
          return
        }
        event.preventDefault()
        navigate(to, { replace })
      }}
      {...rest}
    />
  )
}

/** Read/merge the query string. Filters and pagination live here, not in state. */
export function useQueryParams() {
  const { search, pathname, navigate } = useRouter()
  const params = useMemo(() => new URLSearchParams(search), [search])

  const setParams = useCallback(
    (next: Record<string, string | undefined>, options?: { replace?: boolean }) => {
      const merged = new URLSearchParams(search)
      for (const [key, value] of Object.entries(next)) {
        if (value === undefined || value === '') merged.delete(key)
        else merged.set(key, value)
      }
      const qs = merged.toString()
      navigate(qs ? `${pathname}?${qs}` : pathname, options)
    },
    [navigate, pathname, search],
  )

  return { params, setParams }
}
