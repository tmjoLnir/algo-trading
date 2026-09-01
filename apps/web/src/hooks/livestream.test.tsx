import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from '../App'
import LiveStream from '@/components/LiveStream'
import { SESSION_KEY } from '@/api/session'

/**
 * The live socket: where it is held, and what happens when it drops.
 *
 * The socket is an enhancement — the aggregate read is the source of truth — so
 * nothing here is about delivery. It is about the two ways a *connection* can be
 * wrong that no screen shows:
 *
 * 1. **A gap nobody repairs.** Redis pub/sub has no replay, so everything
 *    published while the socket was down reached nobody. The dashboard used to
 *    poll and repaired that within five minutes by accident; nothing polls now
 *    (ADR 0022), so unless the reconnect re-reads, the tab keeps rendering the
 *    pre-outage book — including a halt banner describing trading as it was
 *    before the disconnection.
 * 2. **A socket that is not there at all.** Holding it on the dashboard route
 *    meant the API logged `clients=0` for every other screen, while
 *    `HaltBanner` — mounted above the nav precisely so it is on all of them —
 *    went on stating that halts reach it by push.
 *
 * Both are invisible from the screen, which is why they are asserted here.
 */

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** Just enough `WebSocket`, and a handle on every one the app opened. */
class FakeWebSocket {
  static opened: FakeWebSocket[] = []

  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  sent: string[] = []
  closedByTheClient = false

  constructor(readonly url: string) {
    FakeWebSocket.opened.push(this)
  }

  send(data: string): void {
    this.sent.push(data)
  }

  close(): void {
    this.closedByTheClient = true
  }
}

/** The socket the app is currently holding — the last one it opened. */
function current(): FakeWebSocket {
  const socket = FakeWebSocket.opened.at(-1)
  if (!socket) throw new Error('the app never opened a socket')
  return socket
}

/** The two halves of a handshake the hook reacts to, inside `act`. */
async function serverAccepts(socket: FakeWebSocket): Promise<void> {
  await act(async () => {
    socket.onopen?.()
  })
}

/** One frame from the server, as the socket hands it over. */
async function serverSends(socket: FakeWebSocket, frame: unknown): Promise<void> {
  await act(async () => {
    socket.onmessage?.({ data: typeof frame === 'string' ? frame : JSON.stringify(frame) })
  })
}

/** 1006 is what a browser reports for a handshake nginx answered 502 to. */
async function serverDrops(socket: FakeWebSocket, code = 1006): Promise<void> {
  await act(async () => {
    socket.onclose?.({ code })
  })
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

/** Let the in-flight reads settle without moving the clock. */
async function settle(): Promise<void> {
  await advance(0)
}

function stubApi(routes: Record<string, { status: number; body?: unknown }>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://test').pathname
    const match = Object.keys(routes).find((path) => url === path)
    const route = match ? routes[match]! : { status: 404, body: { detail: 'not stubbed' } }
    return {
      ok: route.status < 400,
      status: route.status,
      statusText: 'stub',
      json: async () => route.body ?? {},
    } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/**
 * A book holding nothing, which is what most of these tests want.
 *
 * The symbol list is an effect dependency, so a book that *does* hold something
 * legitimately rebuilds the socket once on first load — the moment the positions
 * arrive. That is real behaviour and it is asserted below, but it makes socket
 * counts ambiguous everywhere else, so the connection tests read an empty book
 * and count sockets exactly.
 */
const EMPTY_BOOK = { positions: [], active_halts: [], stale_after_seconds: 300 }
const BOOK = { positions: [{ symbol: 'AAPL' }], active_halts: [], stale_after_seconds: 300 }

function bookReads(fetchMock: ReturnType<typeof stubApi>): number {
  return fetchMock.mock.calls.filter((call) => String(call[0]).includes('/api/v1/dashboard/live'))
    .length
}

function renderStream() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const result = render(
    <QueryClientProvider client={queryClient}>
      <LiveStream />
    </QueryClientProvider>,
  )
  return { ...result, queryClient }
}

beforeEach(() => {
  FakeWebSocket.opened = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
})

describe('the socket the session holds', () => {
  it('opens one, on this page origin', async () => {
    stubApi({ '/api/v1/dashboard/live': { status: 200, body: EMPTY_BOOK } })
    const { container } = renderStream()

    expect(FakeWebSocket.opened.length).toBe(1)
    expect(current().url).toMatch(/^ws:\/\/.+\/ws$/)
    // It is a connection, not a component. Anything it rendered would be a
    // stray node above the run-mode banner on every screen in the app.
    expect(container.innerHTML).toBe('')
  })

  it('subscribes to what the book holds', async () => {
    stubApi({ '/api/v1/dashboard/live': { status: 200, body: BOOK } })
    renderStream()

    // The symbol list is an effect dependency, so the socket opened on mount is
    // rebuilt once — when the first read comes back with positions in it.
    await waitFor(() => expect(FakeWebSocket.opened.length).toBe(2))
    await serverAccepts(current())

    expect(JSON.parse(current().sent[0]!)).toEqual({
      type: 'subscribe',
      channels: ['quotes', 'fills'],
      symbols: ['AAPL'],
    })
  })
})

describe('the reconnect gap', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it('does not re-read the book on the first connect', async () => {
    // Nothing has been missed yet: the query mounting alongside this socket is
    // reading the book at the same moment, so a read here would be a second
    // request for an answer already in flight.
    const fetchMock = stubApi({ '/api/v1/dashboard/live': { status: 200, body: EMPTY_BOOK } })
    renderStream()
    await settle()

    await serverAccepts(current())
    await settle()

    expect(bookReads(fetchMock)).toBe(1)
  })

  it('re-reads the book when the socket comes back', async () => {
    // The one that matters. Everything published while the socket was down
    // reached nobody and pub/sub will not replay it, so asking is the only way
    // this tab learns about a fill — or a halt — from that window.
    const fetchMock = stubApi({ '/api/v1/dashboard/live': { status: 200, body: EMPTY_BOOK } })
    renderStream()
    await settle()
    await serverAccepts(current())
    await settle()
    expect(bookReads(fetchMock)).toBe(1)

    await serverDrops(current())
    await advance(1_100)
    await serverAccepts(current())
    await settle()

    expect(bookReads(fetchMock)).toBe(2)
  })

  it('re-reads once per reconnection, not once per failed attempt', async () => {
    // A server refusing connections — nginx answering 502 for an API that has
    // not finished starting, which is what the restart in the logs produced —
    // closes each attempt without ever opening it. Reading on the close rather
    // than on the open would hammer the API that is already the reason the
    // socket is down.
    const fetchMock = stubApi({ '/api/v1/dashboard/live': { status: 200, body: EMPTY_BOOK } })
    renderStream()
    await settle()
    await serverAccepts(current())
    await settle()

    for (let refused = 0; refused < 3; refused++) {
      await serverDrops(current())
      await advance(31_000)
    }
    await settle()

    expect(bookReads(fetchMock)).toBe(1)
  })
})

describe('the reconnect ladder', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    stubApi({ '/api/v1/dashboard/live': { status: 200, body: EMPTY_BOOK } })
  })

  it('doubles the wait each attempt and stops at the cap', async () => {
    // Jitter pinned at its longest, so these are the ceilings themselves.
    vi.spyOn(Math, 'random').mockReturnValue(1)
    renderStream()
    await serverAccepts(current())

    for (const wait of [1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000]) {
      const before = FakeWebSocket.opened.length
      await serverDrops(current())

      await advance(wait - 1)
      expect(FakeWebSocket.opened.length).toBe(before)
      await advance(1)
      expect(FakeWebSocket.opened.length).toBe(before + 1)
    }
  })

  it('waits less than the ceiling rather than exactly it', async () => {
    // Jitter, and the reason for it: every tab whose socket dropped for the
    // same reason dropped at the same instant, so an unjittered ladder has them
    // all knock again together — hardest at the moment the API is least able to
    // answer. `atp_core.ws.backoff_delay` makes the same argument on the server.
    vi.spyOn(Math, 'random').mockReturnValue(0)
    renderStream()
    await serverAccepts(current())

    await serverDrops(current())
    await advance(499)
    expect(FakeWebSocket.opened.length).toBe(1)
    await advance(1)
    expect(FakeWebSocket.opened.length).toBe(2)
  })

  it('starts the ladder again once a connection succeeds', async () => {
    // Otherwise a tab that had a bad afternoon reconnects at the 30s ceiling
    // for the rest of the session, and the next blip costs half a minute of
    // liveness for no reason.
    vi.spyOn(Math, 'random').mockReturnValue(1)
    renderStream()
    await serverAccepts(current())

    await serverDrops(current())
    await advance(1_000)
    await serverDrops(current())
    await advance(2_000)
    await serverAccepts(current())

    await serverDrops(current())
    await advance(999)
    expect(FakeWebSocket.opened.length).toBe(3)
    await advance(1)
    expect(FakeWebSocket.opened.length).toBe(4)
  })

  it('stops for good on 1008 and drops the session', async () => {
    // 1008 is the server refusing the socket on policy — no valid session
    // (ADR 0008). Every reconnect would send the same cookie and be refused the
    // same way; the login screen is the only thing that can fix it.
    const { queryClient } = renderStream()
    await serverAccepts(current())

    await serverDrops(current(), 1008)
    await advance(60_000)

    expect(FakeWebSocket.opened.length).toBe(1)
    expect(queryClient.getQueryData(SESSION_KEY)).toBeNull()
  })

  it('closes the socket and cancels a pending reconnect when it goes away', async () => {
    const { unmount } = renderStream()
    await serverAccepts(current())

    await serverDrops(current())
    unmount()
    await advance(60_000)

    expect(FakeWebSocket.opened.length).toBe(1)
    expect(FakeWebSocket.opened[0]!.closedByTheClient).toBe(true)
  })
})

describe('where the socket is held', () => {
  /**
   * The regression this exists for: `useDashboardStream` was called by
   * `Dashboard`, so navigating anywhere else closed the socket. `HaltBanner`
   * sits above the nav on every screen and states that halts reach it by push —
   * which was true on `/` and on none of the other six routes.
   */
  function renderAppAt(path: string) {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  }

  it.each(['/', '/strategies', '/positions', '/orders', '/analytics', '/audit'])(
    'is open on %s',
    async (path) => {
      stubApi({
        '/api/v1/auth/me': { status: 200, body: { user: 'operator', scope: 'full' } },
        '/api/v1/dashboard/live': { status: 200, body: EMPTY_BOOK },
      })
      renderAppAt(path)

      await waitFor(() => expect(FakeWebSocket.opened.length).toBeGreaterThanOrEqual(1))
    },
  )

  it('is not opened before anyone is signed in', async () => {
    // A socket opened ahead of the session is refused with 1008, and the ladder
    // correctly reads that as "sign out" — a login screen for somebody who was
    // in the middle of signing in.
    stubApi({ '/api/v1/auth/me': { status: 401, body: { detail: 'not authenticated' } } })
    renderAppAt('/')

    await waitFor(() => expect(document.body.textContent).toMatch(/sign in/i))
    expect(FakeWebSocket.opened.length).toBe(0)
  })
})

describe('what re-reads the book', () => {
  /**
   * The socket carries two kinds of message and only one of them is news.
   *
   * A quote is a price, rendered beside the book as its own clearly-labelled
   * live figure — it must not cost a read, or a busy symbol would refetch the
   * whole dashboard on every tick. A fill, a halt and a gap each mean the book
   * itself is no longer what was last read, and the aggregate read is the only
   * authoritative way to find out what it is now.
   */
  beforeEach(() => {
    vi.useFakeTimers()
  })

  async function connected() {
    const fetchMock = stubApi({ '/api/v1/dashboard/live': { status: 200, body: EMPTY_BOOK } })
    renderStream()
    await settle()
    await serverAccepts(current())
    await settle()
    expect(bookReads(fetchMock)).toBe(1)
    return fetchMock
  }

  it('a gap does — the API saying it does not know what it missed either', async () => {
    // The outage this socket cannot detect for itself: the API's own
    // subscription to the producers dropped and recovered while this
    // connection stayed open throughout, so nothing here would otherwise ever
    // ask. What was published in the meantime might have been a halt.
    const fetchMock = await connected()

    await serverSends(current(), { type: 'gap', seconds: 12.4 })
    await settle()

    expect(bookReads(fetchMock)).toBe(2)
  })

  it('a fill does', async () => {
    const fetchMock = await connected()

    await serverSends(current(), { type: 'fill', order_id: 'o-1', symbol: 'AAPL' })
    await settle()

    expect(bookReads(fetchMock)).toBe(2)
  })

  it('a halt does', async () => {
    const fetchMock = await connected()

    await serverSends(current(), { type: 'halt', scope: 'global', reason: 'daily_loss' })
    await settle()

    expect(bookReads(fetchMock)).toBe(2)
  })

  it('a quote does not', async () => {
    // A tick is not the book changing. Refetching on one would put the whole
    // dashboard behind every price update on a busy symbol.
    const fetchMock = await connected()

    await serverSends(current(), {
      type: 'quote',
      symbol: 'AAPL',
      bid: '100.00',
      ask: '100.02',
      ts: '2026-08-31T15:00:00Z',
    })
    await settle()

    expect(bookReads(fetchMock)).toBe(1)
  })

  it('a frame we cannot read does not, and does not tear the socket down', async () => {
    // A malformed frame is a server bug. Reconnecting into the same bug would
    // turn it into a loop against an API that is answering fine.
    const fetchMock = await connected()
    const socket = current()

    await serverSends(socket, 'not json at all')
    await serverSends(socket, { type: 'something-we-have-never-heard-of' })
    await settle()

    expect(bookReads(fetchMock)).toBe(1)
    expect(FakeWebSocket.opened.length).toBe(1)
    expect(socket.closedByTheClient).toBe(false)
  })
})
