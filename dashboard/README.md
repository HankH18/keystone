# Keystone dashboard

Reviewer surface for the Keystone reconciliation service: conflicts and
proposals with filters, evidence, confidence, and approve/reject.

Stack: Vite + React 19 + TypeScript, Tailwind v4 (CSS-first, no PostCSS config),
TanStack Query + Table, vitest + Testing Library, ESLint, Playwright + axe-core.

## Commands

| Command | What it does |
| --- | --- |
| `pnpm dev` | Dev server on :5173 |
| `pnpm build` | `tsc -b` project references, then `vite build` |
| `pnpm preview` | Serve the production build on :4173 |
| `pnpm lint` | `eslint .` |
| `pnpm test` | `vitest run` (jsdom) |
| `pnpm test:watch` | vitest watch mode |
| `pnpm test:coverage` | vitest with v8 coverage |
| `pnpm test:a11y` | Playwright + axe-core; **skips with exit 0 if Chromium is absent** |

## Configuration

Copy `.env.example` to `.env.local`. The dashboard talks to the HTTP API only
(never Postgres) via `src/lib/api.ts`, which reads `VITE_API_BASE_URL` and sends
`VITE_API_KEY` as the `X-Api-Key` header.

## Status

T-0 scaffold: placeholder shell only (skip link, `<h1>`, semantic `<main>`).
Views, routing and API endpoint helpers land in T-10.
