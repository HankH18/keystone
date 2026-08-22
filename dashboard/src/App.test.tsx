import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import App from './App'

describe('App shell', () => {
  it('renders the Keystone heading as the page h1', () => {
    render(<App />)
    const heading = screen.getByRole('heading', { level: 1, name: 'Keystone' })
    expect(heading).toBeInTheDocument()
  })

  it('renders a skip link that targets the main landmark', () => {
    render(<App />)
    const skipLink = screen.getByRole('link', { name: /skip to content/i })
    expect(skipLink).toBeInTheDocument()
    expect(skipLink).toHaveAttribute('href', '#main-content')

    const main = screen.getByRole('main')
    expect(main).toHaveAttribute('id', 'main-content')
  })
})
