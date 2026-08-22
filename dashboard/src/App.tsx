/**
 * Placeholder application shell (T-0).
 *
 * Deliberately minimal: a skip link, one <h1>, and a semantic <main>.
 * The conflicts/proposals surface, filters, TanStack Table and the
 * approve/reject actions all land in T-10.
 */
function App() {
  return (
    <>
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2 focus:text-slate-900 focus:outline focus:outline-2 focus:outline-slate-900"
      >
        Skip to content
      </a>
      <main id="main-content" className="mx-auto max-w-5xl p-8">
        <h1 className="text-3xl font-semibold tracking-tight">Keystone</h1>
      </main>
    </>
  )
}

export default App
