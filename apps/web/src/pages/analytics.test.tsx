import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import Analytics from './Analytics'
import PerformancePanel from '@/components/PerformancePanel'
import AttributionTable from '@/components/AttributionTable'
import TradesTable from '@/components/TradesTable'
import type {
  AttributionResponse,
  PerformanceResponse,
  TradeView,
  TradesResponse,
} from '@/api/types'

/**
 * What the analytics screen states, and what it refuses to.
 *
 * Every assertion below is a rule from docs/ANALYTICS.md or docs/DASHBOARD.md
 * rather than a property of the code that happens to be easy to check. The four
 * that matter:
 *
 * 1. **A period with no closed trades is a sentence, not a wall of zeros.**
 *    `compute_all` returns 0.0 for every ratio it cannot compute; rendering
 *    nineteen of them says "flat performance" when what happened is that
 *    nothing finished.
 * 2. **An unmeasured excursion is a dash and a measured zero is a zero.** Null
 *    means we did not look; zero means the trade only ever went one way. The
 *    server keeps them apart on purpose and the screen must not merge them.
 * 3. **Money is formatted from its string, never through a float.** A P&L of
 *    `1234.565` truncates to `1,234.56` — the display is a view of an exact
 *    value held elsewhere, and inventing the hundredth that rounding would add
 *    is how a screen and a statement come to disagree by a cent nobody can find.
 * 4. **One failed panel does not take the screen down.** These are three
 *    independent reads of the same stored history.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const METRICS: PerformanceResponse['metrics'] = {
  total_return: 0.1234,
  cagr: 0.42,
  sharpe: 1.35,
  sortino: 2.1,
  calmar: 0.9,
  max_drawdown: -0.0821,
  max_drawdown_duration_days: 6,
  volatility: 0.18,
  win_rate: 0.6,
  profit_factor: 1.8,
  expectancy: 42.5,
  avg_win: 180.25,
  avg_loss: -95.5,
  largest_win: 600.0,
  largest_loss: -310.0,
  num_trades: 5,
  avg_holding_period_hours: 30.5,
  exposure_pct: 0.335,
  turnover: 3.2,
}

const PERFORMANCE: PerformanceResponse = {
  metrics: METRICS,
  start: '2026-07-20T00:00:00Z',
  end: '2026-08-19T23:59:59Z',
  equity_points: 6,
  periods_per_year: 252,
}

const TRADE: TradeView = {
  trade_id: 'AAPL-1',
  strategy_id: 'sma_cross',
  symbol: 'AAPL',
  side: 'long',
  entry_ts: '2026-08-10T13:30:00Z',
  exit_ts: '2026-08-12T20:00:00Z',
  entry_price: '100.00',
  exit_price: '110.00',
  qty: '10',
  gross_pnl: '100.00',
  fees: '1.50',
  net_pnl: '1234.565',
  return_pct: '0.0125',
  holding_period_hours: 54.5,
  exit_reason: 'take_profit',
  max_favorable_excursion: '150.00',
  max_adverse_excursion: '-20.00',
}

function tradesResponse(trades: TradeView[], excursionsOmitted = false): TradesResponse {
  return {
    trades,
    excursions_omitted: excursionsOmitted,
    start: '2026-07-20T00:00:00Z',
    end: '2026-08-19T23:59:59Z',
  }
}

const ATTRIBUTION: AttributionResponse = {
  by: 'exit_reason',
  rows: [
    {
      key: 'take_profit',
      net_pnl: '900.00',
      num_trades: 3,
      win_rate: 1.0,
      avg_pnl: '300.00',
      contribution_pct: 42.5,
    },
    {
      key: 'stop_loss',
      net_pnl: '-400.00',
      num_trades: 2,
      win_rate: 0.0,
      avg_pnl: '-200.00',
      contribution_pct: -18.9,
    },
  ],
  start: '2026-07-20T00:00:00Z',
  end: '2026-08-19T23:59:59Z',
}

describe('the metric set', () => {
  it('says nothing closed rather than reporting a period of zeros', () => {
    // The refusal this panel exists for. Every ratio is legitimately 0.0 when
    // there are no trades, and a grid of them reads as a flat month.
    const empty: PerformanceResponse = {
      ...PERFORMANCE,
      metrics: { ...METRICS, num_trades: 0 },
      equity_points: 0,
    }
    render(<PerformancePanel data={empty} />)

    expect(screen.getByText(/No round trips closed in this period/)).toBeTruthy()
    expect(screen.queryByText('Sharpe')).toBeNull()
  })

  it('states the annualisation basis, because every ratio scales with it', () => {
    render(<PerformancePanel data={PERFORMANCE} />)
    expect(screen.getByText(/252/)).toBeTruthy()
    expect(screen.getByText(/periods per year/)).toBeTruthy()
  })

  it('labels max drawdown as the realised one', () => {
    render(<PerformancePanel data={PERFORMANCE} />)
    // Shallower than what the account lived through, because the curve behind
    // it steps only when a round trip closes.
    expect(screen.getByText(/Max drawdown \(realised\)/)).toBeTruthy()
  })

  it('shows a return with a sign as well as a colour', () => {
    render(<PerformancePanel data={PERFORMANCE} />)
    expect(screen.getByText(/\+12\.34%/)).toBeTruthy()
  })
})

describe('the trade list', () => {
  it('renders money from its string without rounding it', () => {
    render(<TradesTable data={tradesResponse([TRADE])} />)
    // Truncated, not rounded: 1234.565 is not 1,234.57.
    expect(screen.getByText(/1,234\.56$/)).toBeTruthy()
    expect(screen.queryByText(/1,234\.57/)).toBeNull()
  })

  it('carries an arrow beside the P&L, not colour alone', () => {
    render(<TradesTable data={tradesResponse([TRADE])} />)
    expect(screen.getByText(/▲/)).toBeTruthy()
  })

  it('renders an unmeasured excursion as a dash and a measured zero as zero', () => {
    // The distinction the nullable column exists for. Zero says the trade never
    // went against us, which is the most flattering reading of "we did not
    // measure" (docs/ANALYTICS.md).
    render(
      <TradesTable
        data={tradesResponse([
          { ...TRADE, max_adverse_excursion: null, max_favorable_excursion: '0' },
        ])}
      />,
    )
    expect(screen.getByText('—')).toBeTruthy()
    expect(screen.getByText('0.00')).toBeTruthy()
  })

  it('says why the excursion columns are empty when the server did not measure them', () => {
    render(<TradesTable data={tradesResponse([TRADE], true)} />)
    expect(screen.getByText(/MAE and MFE were not measured/)).toBeTruthy()
  })

  it('reports an unknown exit reason as unknown rather than guessing one', () => {
    // A wrong exit reason is worse than a missing one — it is the number that
    // decides whether a strategy's stops are misplaced.
    render(<TradesTable data={tradesResponse([{ ...TRADE, exit_reason: 'unknown' }])} />)
    expect(screen.getByText('unknown')).toBeTruthy()
  })

  it('distinguishes an empty period from an open position', () => {
    render(<TradesTable data={tradesResponse([])} />)
    expect(screen.getByText(/No round trips closed in this period/)).toBeTruthy()
    expect(screen.getByText(/A position still open is not a trade/)).toBeTruthy()
  })
})

describe('attribution', () => {
  it('renders contribution as the already-scaled percentage the server sent', () => {
    // `contribution_pct` arrives multiplied by 100 and denominated in the
    // period's absolute P&L. Scaling it again here would read as 4,250%.
    render(<AttributionTable data={ATTRIBUTION} />)
    expect(screen.getByText('42.5%')).toBeTruthy()
    expect(screen.queryByText(/4,250/)).toBeNull()
  })

  it('renders a win rate fraction as a percentage', () => {
    render(<AttributionTable data={ATTRIBUTION} />)
    const row = screen.getByText('take_profit').closest('tr')
    expect(row).toBeTruthy()
    expect(within(row as HTMLElement).getByText('100%')).toBeTruthy()
  })

  it('names the timezone on an hour grouping', () => {
    // The hour is grouped in UTC while every timestamp elsewhere on the screen
    // is local. A bare `14` beside a local-time trade list invites a comparison
    // between two different clocks.
    render(
      <AttributionTable
        data={{
          ...ATTRIBUTION,
          by: 'hour',
          rows: [{ ...ATTRIBUTION.rows[0]!, key: '14' }],
        }}
      />,
    )
    expect(screen.getByText('14:00 UTC')).toBeTruthy()
  })

  it('says nothing to attribute rather than showing an empty table', () => {
    render(<AttributionTable data={{ ...ATTRIBUTION, rows: [] }} />)
    expect(screen.getByText(/Nothing to attribute/)).toBeTruthy()
  })
})

function stubRoutes(handler: (url: string) => { status: number; body: unknown }) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const { status, body } = handler(url)
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
      <Analytics />
    </QueryClientProvider>,
  )
}

describe('the page', () => {
  it('asks all three endpoints for the same explicit window', async () => {
    // Sent explicitly rather than left to the server's default, so the three
    // panels are demonstrably describing one period rather than three that
    // happen to coincide.
    const fetchMock = stubRoutes((url) => {
      if (url.includes('/performance')) return { status: 200, body: PERFORMANCE }
      if (url.includes('/attribution')) return { status: 200, body: ATTRIBUTION }
      return { status: 200, body: tradesResponse([TRADE]) }
    })
    renderPage()
    await screen.findByText('AAPL')

    const urls = fetchMock.mock.calls.map(([input]) => String(input))
    expect(urls).toHaveLength(3)
    const windows = urls.map((url) => {
      const params = new URLSearchParams(url.split('?')[1])
      return `${params.get('start')}..${params.get('end')}`
    })
    expect(new Set(windows).size).toBe(1)
    expect(windows[0]).not.toContain('null')
  })

  it('keeps the trade list when attribution fails', async () => {
    // Three independent reads of the same stored history. One failing is not a
    // reason to blank the other two.
    stubRoutes((url) => {
      if (url.includes('/attribution')) return { status: 500, body: { detail: 'boom' } }
      if (url.includes('/performance')) return { status: 200, body: PERFORMANCE }
      return { status: 200, body: tradesResponse([TRADE]) }
    })
    renderPage()

    expect(await screen.findByText('AAPL')).toBeTruthy()
    expect(screen.getByText(/Could not load the attribution breakdown/)).toBeTruthy()
  })

  it('shows what the server said when a dimension is refused', async () => {
    // A 422 naming the dimensions that exist, rather than an empty table — the
    // endpoint answers that way so "you asked for something that does not
    // exist" cannot read as "this period made nothing".
    stubRoutes((url) => {
      if (url.includes('/attribution')) {
        return { status: 422, body: { detail: "cannot attribute by 'nonsense'" } }
      }
      if (url.includes('/performance')) return { status: 200, body: PERFORMANCE }
      return { status: 200, body: tradesResponse([]) }
    })
    renderPage()

    expect(await screen.findByText(/cannot attribute by 'nonsense'/)).toBeTruthy()
  })
})
