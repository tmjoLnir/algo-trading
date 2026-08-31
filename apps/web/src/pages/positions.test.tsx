import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import Positions from './Positions'
import type { AccountView, PositionView, StoredBookView } from '@/api/types'

/**
 * What the stored-book screen states, and what it refuses to.
 *
 * This page reads the book the worker wrote to the database rather than the one
 * it published to Redis, which is the whole reason it exists: the published
 * copy is gone the moment the worker stops, and this one is not. The price of
 * that is an answer which can be arbitrarily old, so every assertion here is
 * about the screen being honest about *when*.
 *
 * 1. **The age leads, and past a threshold it is a warning.** A stored book
 *    rendered as though it were current is the failure ADR 0007 exists to
 *    prevent, moved from a cache to a table.
 * 2. **Never written is not empty.** "You hold nothing" and "nobody has ever
 *    said what you hold" are different sentences and only one is safe to act on.
 * 3. **An unreadable book is not an empty one either** — the same rule the audit
 *    page follows for a 503.
 * 4. **Two ages, not one.** A tab that read a second ago against a worker that
 *    stopped an hour ago is fresh by one measure and useless by the other. Both
 *    advance on their own clock, because since ADR 0022 nothing re-reads on a
 *    schedule and an age that only moved when something fetched would sit still
 *    for as long as the tab is open.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const ACCOUNT: AccountView = {
  equity: '101234.5678',
  cash: '90000.00',
  gross_exposure: '11234.56',
  net_exposure: '11234.56',
  leverage: '0.1110',
  realized_pnl: '250.00',
  unrealized_pnl: '134.56',
  day_pnl: null,
  day_pnl_pct: null,
  open_position_count: 1,
  unmarked_symbols: [],
}

const POSITION: PositionView = {
  symbol: 'AAPL',
  qty: '10',
  avg_entry_price: '100.00',
  last_price: '110.00',
  market_value: '1100.00',
  unrealized_pnl: '100.00',
  unrealized_pnl_pct: '0.1000',
  realized_pnl: '0',
  fees_paid: '0',
  stop_loss_price: '90.00',
  take_profit_price: '130.00',
  distance_to_stop_pct: '2.0000',
  opened_at: '2026-08-02T14:30:00Z',
}

function book(overrides: Partial<StoredBookView> = {}): StoredBookView {
  return {
    as_of: '2026-08-19T14:30:00Z',
    age_seconds: 42,
    account: ACCOUNT,
    positions: [POSITION],
    run_mode: 'paper',
    ...overrides,
  }
}

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
      <Positions />
    </QueryClientProvider>,
  )
}

describe('the age', () => {
  it('leads with how long ago the worker wrote the book', async () => {
    stub(200, book({ age_seconds: 42 }))
    renderPage()

    expect(await screen.findByText(/The worker last recorded this book/)).toBeTruthy()
    expect(screen.getByText('42s')).toBeTruthy()
  })

  it('turns the age into a warning once the book is old', async () => {
    // The worker writes a snapshot every evaluation, so a book this old means
    // it has missed several — a fact about the worker, which is what this
    // screen is being read for.
    stub(200, book({ age_seconds: 3 * 3600 }))
    renderPage()

    expect(await screen.findByText(/missed several/)).toBeTruthy()
    expect(screen.getByText(/Treat every figure below as history/)).toBeTruthy()
  })

  it('does not warn on a book the worker wrote moments ago', async () => {
    stub(200, book({ age_seconds: 20 }))
    renderPage()

    await screen.findByText(/The worker last recorded this book/)
    expect(screen.queryByText(/Treat every figure below as history/)).toBeNull()
  })

  it('shows the tab read age separately from the book age', async () => {
    // Fresh by one measure and useless by the other (docs/DASHBOARD.md).
    stub(200, book())
    renderPage()

    expect(await screen.findByText(/this tab read/)).toBeTruthy()
  })

  it('ages the book on its own clock, with nothing re-reading', async () => {
    /**
     * The regression this whole change could have shipped.
     *
     * `age_seconds` is how old the book was *when we read it*, and it stops
     * moving the moment it arrives. While a 5-minute poll existed, a fresh one
     * arrived before anybody noticed. With manual refresh, judging staleness by
     * that frozen number means a book that was current when the tab loaded can
     * never become stale — which is precisely the tab-left-open-across-a-sleeping
     * -laptop case the warning exists for (docs/LOCAL_HOSTING.md §1).
     *
     * So: read a book that is comfortably fresh, then let ten minutes pass with
     * no fetch at all, and the screen must have worked out on its own that what
     * it is showing is now too old to act on.
     */
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      stub(200, book({ age_seconds: 30 }))
      renderPage()

      await screen.findByText(/The worker last recorded this book/)
      expect(screen.queryByText(/Treat every figure below as history/)).toBeNull()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(10 * 60 * 1000)
      })

      expect(screen.getByText(/Treat every figure below as history/)).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('a book nobody has written', () => {
  it('says so rather than reporting no positions', async () => {
    // Saying "flat" here would tell the reader the opposite of the truth.
    stub(200, book({ as_of: null, age_seconds: null, account: null, positions: [] }))
    renderPage()

    expect(await screen.findByText(/No book has ever been written/)).toBeTruthy()
    expect(screen.getByText(/This is not "you hold nothing"/)).toBeTruthy()
  })

  it('renders a written but empty book as an empty one', async () => {
    // The other half: a real snapshot holding nothing has an age and an
    // account, and "flat" is then the true answer.
    stub(200, book({ positions: [] }))
    renderPage()

    expect(await screen.findByText(/Flat — no open positions/)).toBeTruthy()
    expect(screen.queryByText(/No book has ever been written/)).toBeNull()
  })
})

describe('an unreadable book', () => {
  it('says nothing can be concluded, rather than showing an empty book', async () => {
    stub(503, { detail: 'the database could not be reached' })
    renderPage()

    expect(await screen.findByText(/Could not read the stored book/)).toBeTruthy()
    expect(screen.getByText(/this is not "you hold nothing"/i)).toBeTruthy()
  })
})

describe('the figures', () => {
  it('renders money from its string without rounding', async () => {
    stub(200, book())
    renderPage()

    // 101234.5678 truncates to 101,234.56 — not 101,234.57.
    expect(await screen.findByText('101,234.56')).toBeTruthy()
    expect(screen.queryByText('101,234.57')).toBeNull()
  })

  it('names which run mode the book belongs to', async () => {
    stub(200, book())
    renderPage()

    expect(await screen.findByText(/paper/)).toBeTruthy()
  })

  it('warns when a position carries no mark', async () => {
    // Non-empty means equity and exposure both under-report.
    stub(200, book({ account: { ...ACCOUNT, unmarked_symbols: ['TSLA'] } }))
    renderPage()

    expect(await screen.findByText(/No mark for TSLA/)).toBeTruthy()
    expect(screen.getByText(/understates exposure and equity/)).toBeTruthy()
  })

  it('sends the reader to the dashboard for day P&L rather than showing zero', async () => {
    // It needs the session's opening equity, which is a question about the
    // history rather than about this snapshot. Zero is a value a reader acts on.
    stub(200, book())
    renderPage()

    expect(await screen.findByText(/Day P&L is on the dashboard/)).toBeTruthy()
  })
})
