import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiBase, wsUrl } from './origin'

/**
 * These two functions decide which host every request and every live update
 * goes to, and they are baked into the bundle at build time. Getting them wrong
 * does not fail loudly: the dashboard renders, and then talks to nothing — or,
 * worse, keeps polling successfully while the socket is silently blocked, which
 * on screen is a dashboard that is merely five minutes stale.
 */

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('apiBase', () => {
  it('is empty by default, so requests stay on the page origin', () => {
    expect(apiBase(undefined)).toBe('')
  })

  it('honours an explicit origin for an API served elsewhere', () => {
    expect(apiBase('https://api.example.internal')).toBe('https://api.example.internal')
  })

  it('reads VITE_API_BASE_URL when no argument is given', () => {
    vi.stubEnv('VITE_API_BASE_URL', 'https://api.example.internal')
    expect(apiBase()).toBe('https://api.example.internal')
  })

  it('treats an empty VITE_API_BASE_URL as same-origin rather than a bad prefix', () => {
    vi.stubEnv('VITE_API_BASE_URL', '')
    expect(apiBase()).toBe('')
  })
})

describe('wsUrl', () => {
  it('derives ws:// from an http page', () => {
    expect(wsUrl({ protocol: 'http:', host: 'localhost:5173' }, undefined)).toBe(
      'ws://localhost:5173/ws',
    )
  })

  it('derives wss:// from an https page', () => {
    // The one that matters: a browser refuses a ws:// socket from an https
    // page as mixed content, so a hardcoded scheme loses every live update the
    // day the dashboard goes behind TLS — while the poll keeps working and
    // hides it.
    expect(wsUrl({ protocol: 'https:', host: 'atp.example.internal' }, undefined)).toBe(
      'wss://atp.example.internal/ws',
    )
  })

  it('keeps a non-default port', () => {
    expect(wsUrl({ protocol: 'http:', host: '10.0.0.4:8080' }, undefined)).toBe(
      'ws://10.0.0.4:8080/ws',
    )
  })

  it('honours an explicit VITE_WS_URL over the page origin', () => {
    vi.stubEnv('VITE_WS_URL', 'wss://elsewhere.example/ws')
    expect(wsUrl({ protocol: 'http:', host: 'localhost:5173' })).toBe('wss://elsewhere.example/ws')
  })

  it('falls back to the page origin when VITE_WS_URL is empty rather than building "undefined"', () => {
    vi.stubEnv('VITE_WS_URL', '')
    expect(wsUrl({ protocol: 'http:', host: 'localhost:5173' })).toBe('ws://localhost:5173/ws')
  })
})
