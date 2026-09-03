import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import type { HaltView } from '@/api/types'

/**
 * The chrome that does not scroll away.
 *
 * The run-mode banner and the halt banner have always been mounted above the
 * nav because whether this is real money and whether trading is stopped are the
 * two facts an operator must not have to go looking for. That held at the top
 * of the document and nowhere else: every screen here is a long table, so the
 * reader who is actually about to act — at row 200 of the order history — had
 * scrolled all three off the top.
 *
 * jsdom has no layout engine, so nothing here can observe an element actually
 * staying put. What these tests can hold onto is everything that decides
 * whether it will:
 *
 * 1. **The three pieces are one block.** Pinning them separately would let the
 *    halt banner slide under the tabs, and a stack of `top-0` siblings overlap
 *    at exactly the moment a halt appears.
 * 2. **The block is opaque.** Both banners are alpha-blended, so a pinned bar
 *    without a background of its own renders order rows through the words LIVE
 *    TRADING.
 * 3. **A tab change lands at the top of the new screen.** This is a consequence
 *    of the fix, not a separate wish: a frozen nav is reachable from the bottom
 *    of a long table, and React Router keeps the window's scroll offset across
 *    a route change.
 * 4. **A re-render is not a tab change.** The book re-reads on every socket
 *    frame; if that reset the scroll position, a halt landing elsewhere in the
 *    system would yank the page out from under whoever was reading it.
 */

/** Just enough `WebSocket` that `LiveStream` mounts without a real connection. */
class SilentWebSocket {
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  constructor(readonly url: string) {}
  send(): void {}
  close(): void {}
}

const GLOBAL_HALT: HaltView = {
  scope: 'global',
  reason: 'daily_loss_limit',
  detail: '-3.2%',
  engaged_at: '2026-08-18T12:00:00Z',
  engaged_by: 'risk',
  target: null,
}

function stubApi(routes: Record<string, { status: number; body?: unknown }>) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://test').pathname
      const route = routes[url] ?? { status: 404, body: { detail: 'not stubbed' } }
      return {
        ok: route.status < 400,
        status: route.status,
        statusText: 'stub',
        json: async () => route.body ?? {},
      } as Response
    }),
  )
}

function signedIn(book: Record<string, unknown>) {
  stubApi({
    '/api/v1/auth/me': { status: 200, body: { user: 'operator', scope: 'full' } },
    '/api/v1/dashboard/live': { status: 200, body: book },
  })
}

/**
 * A book with nothing in it, but every required field present.
 *
 * `run_mode: 'live'` because the loudest banner is the one worth asserting
 * against: if the pinned block holds the live warning it holds the quieter two.
 */
const EMPTY_BOOK = {
  account: null,
  active_halts: [],
  as_of: '2026-08-18T12:00:00Z',
  book_age_seconds: 0,
  book_as_of: '2026-08-18T12:00:00Z',
  data_feed_healthy: true,
  last_data_at: '2026-08-18T12:00:00Z',
  market_open: true,
  positions: [],
  recent_signals: [],
  run_mode: 'live',
  stale_after_seconds: 300,
  strategy: null,
  symbols: [],
  working_orders: [],
}

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

/** The pinned block — the `<header>` landmark the banners and tabs share. */
async function header(): Promise<HTMLElement> {
  return await screen.findByRole('banner')
}

beforeEach(() => {
  vi.stubGlobal('WebSocket', SilentWebSocket)
})

afterEach(() => {
  cleanup()
  document.documentElement.scrollTop = 0
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('the pinned header', () => {
  it('holds the run-mode banner, the halt banner and every tab in one block', async () => {
    signedIn({ ...EMPTY_BOOK, active_halts: [GLOBAL_HALT] })
    renderApp()

    const bar = await header()
    // All three, inside the same element. Separately pinned siblings would
    // overlap at `top-0` the moment the halt banner appeared.
    await within(bar).findByText(/LIVE TRADING/)
    await within(bar).findByText(/ALL TRADING HALTED/)
    for (const label of [
      'Dashboard',
      'Strategies',
      'Backtests',
      'Positions',
      'Orders',
      'Worker',
      'Analytics',
      'Audit',
    ]) {
      within(bar).getByRole('link', { name: label })
    }
  })

  it('is pinned, and opaque enough to be pinned over content', async () => {
    signedIn(EMPTY_BOOK)
    renderApp()

    const bar = await header()
    // A class assertion, reluctantly: jsdom resolves no layout, so there is no
    // scroll to observe and no computed stickiness to read back. These three
    // tokens are the whole mechanism — without `sticky top-0` the block scrolls
    // away as before, and without its own background the alpha-blended banners
    // let the page show through whatever they are pinned over.
    expect(bar.className).toContain('sticky')
    expect(bar.className).toContain('top-0')
    expect(bar.className).toContain('bg-slate-950')
  })
})

describe('scrolling, across a tab change', () => {
  it('lands at the top of the screen you asked for', async () => {
    signedIn(EMPTY_BOOK)
    renderApp()

    const bar = await header()
    document.documentElement.scrollTop = 640

    fireEvent.click(within(bar).getByRole('link', { name: 'Audit' }))

    // Not a preference. The offset belonged to the screen you left; carrying it
    // over opens the audit log 640px down, at whatever row happens to be there.
    await waitFor(() => expect(document.documentElement.scrollTop).toBe(0))
  })

  it('leaves the offset alone when the book re-reads under you', async () => {
    signedIn(EMPTY_BOOK)
    const { rerender } = renderApp()
    await header()

    document.documentElement.scrollTop = 640
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    rerender(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/']}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    // The socket re-reads the book on every frame it receives. If a render were
    // treated as a navigation, a halt engaged anywhere in the system would jump
    // the page of whoever was mid-table when it landed.
    expect(document.documentElement.scrollTop).toBe(640)
  })
})
