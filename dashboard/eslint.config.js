import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  {
    ignores: ['dist', 'coverage', 'playwright-report', 'test-results'],
  },
  {
    files: ['**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2023,
      globals: globals.browser,
    },
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    // v7 exposes the flat-config shape under `configs.flat`; the top-level
    // `configs.recommended*` entries are still legacy eslintrc objects.
    extends: [reactHooks.configs.flat['recommended-latest']],
    plugins: { 'react-refresh': reactRefresh },
    rules: {
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true },
      ],
    },
  },
  {
    // `src/lib/**` holds contract types, hooks and pure helpers, not component
    // modules. Fast Refresh boundaries do not apply there, and forcing one
    // export per file would scatter the contract across a dozen modules.
    files: ['src/lib/**/*.{ts,tsx}'],
    rules: { 'react-refresh/only-export-components': 'off' },
  },
  {
    // Node-side tooling config files.
    files: ['*.config.{ts,js}', 'scripts/**/*.{ts,js,mjs}'],
    languageOptions: {
      globals: globals.node,
    },
  },
)
