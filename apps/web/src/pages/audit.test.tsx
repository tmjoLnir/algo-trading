import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import Audit from './Audit'

/**
 * The audit trail on screen.
 *
 * The assertion that matters most is the one about 503: an unreadable record and
 * an empty one are different sentences, and only one of them means nothing
 * happened. Rendering "nothing recorded" during a database outage would tell the
 * reader the opposite of the truth, during exactly the incident they opened this
 * page to investigate.
 *
 * Design: ADR 0010.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function stub(status: number, body: unknown) {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => {
    return {
      ok: status < 400,
      status,
      statusText: 'stub',
      json: async () => body,
    } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Audit />
    </QueryClientProvider>,
  )
}

const ENTRY = {
  id: 12,
  at: '2026-08-18T12:00:00Z',
  actor: 'operator',
  action: 'login',
  target: '203.0.113.7',
  detail: { scope: 'full' },
}

describe('the trail', () => {
  it('lists what happened', async () => {
    stub(200, { entries: [ENTRY], next_before_id: null })
    renderPage()

    expect(await screen.findByText('login')).toBeTruthy()
    expect(screen.getByText('operator')).toBeTruthy()
    expect(screen.getByText('203.0.113.7')).toBeTruthy()
  })

  it('renders an action with no target as a dash, not as blank', async () => {
    // Signing out is not done *to* anything. That should read as "no object"
    // rather than as missing data (docs/DASHBOARD.md).
    stub(200, {
      entries: [{ ...ENTRY, action: 'logout', target: null, detail: {} }],
      next_before_id: null,
    })
    renderPage()

    await screen.findByText('logout')
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
  })

  it('says so when nothing has been recorded', async () => {
    stub(200, { entries: [], next_before_id: null })
    renderPage()

    expect(await screen.findByText(/nothing recorded yet/i)).toBeTruthy()
  })
})

describe('an unreadable record is not an empty one', () => {
  it('does NOT say "nothing recorded" when the trail cannot be read', async () => {
    stub(503, { detail: 'cannot read the audit trail' })
    renderPage()

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/could not be read/i)
    expect(alert.textContent).toMatch(/not the same as it being empty/i)
    expect(screen.queryByText(/nothing recorded yet/i)).toBeNull()
  })
})

describe('narrowing and paging', () => {
  it('asks the server for one kind of action', async () => {
    const fetchMock = stub(200, { entries: [], next_before_id: null })
    renderPage()
    await screen.findByText(/nothing recorded/i)

    fireEvent.change(screen.getByLabelText(/filter by action/i), {
      target: { value: 'login_failed' },
    })

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([url]) => String(url))
      expect(urls.some((url) => url.includes('action=login_failed'))).toBe(true)
    })
  })

  it('pages with the cursor the server handed back, not an offset', async () => {
    const fetchMock = stub(200, { entries: [ENTRY], next_before_id: 12 })
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /older/i }))

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([url]) => String(url))
      expect(urls.some((url) => url.includes('before_id=12'))).toBe(true)
      expect(urls.every((url) => !url.includes('offset'))).toBe(true)
    })
  })

  it('offers no "older" control when the page is the end of the record', async () => {
    stub(200, { entries: [ENTRY], next_before_id: null })
    renderPage()

    await screen.findByText('login')
    expect(screen.queryByRole('button', { name: /older/i })).toBeNull()
  })
})
