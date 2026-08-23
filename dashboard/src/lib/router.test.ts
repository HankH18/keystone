import { describe, expect, it } from 'vitest'
import { matchRoute } from './router'

describe('matchRoute', () => {
  it('routes the five real routes', () => {
    expect(matchRoute('/')).toEqual({ name: 'overview', params: {} })
    expect(matchRoute('/overview')).toEqual({ name: 'overview', params: {} })
    expect(matchRoute('/conflicts')).toEqual({ name: 'conflicts', params: {} })
    expect(matchRoute('/proposals')).toEqual({ name: 'proposals', params: {} })
    expect(matchRoute('/conflicts/abc-123')).toEqual({
      name: 'conflict-detail',
      params: { id: 'abc-123' },
    })
    expect(matchRoute('/proposals/abc-123')).toEqual({
      name: 'proposal-detail',
      params: { id: 'abc-123' },
    })
  })

  it('tolerates a trailing slash', () => {
    expect(matchRoute('/conflicts/')).toEqual({ name: 'conflicts', params: {} })
  })

  it('decodes an encoded id', () => {
    expect(matchRoute('/proposals/a%2Fb')).toEqual({
      name: 'proposal-detail',
      params: { id: 'a/b' },
    })
  })

  it('falls through to not-found rather than guessing', () => {
    expect(matchRoute('/nope').name).toBe('not-found')
    expect(matchRoute('/conflicts/a/b').name).toBe('not-found')
  })
})
